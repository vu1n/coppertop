"""One approved Modal smoke; injected SDK keeps preparation entirely offline."""
import hashlib
import json
import time
import uuid
from datetime import datetime, timezone
from decimal import Decimal
from .chords import object_path
from .publication import release_objects
from .gpu_smoke import RELEASE
from .smoke_runtime import MAX_DELTA_BYTES

GPU_SECONDS = 900
PROCESS_SECONDS = 600
SDK_VERSION = '1.6.1'
# A proposal, not authorization: the caller must supply this exact new approval.
RELIABILITY_APPROVAL = {'provider': 'modal', 'paid': True, 'input_upload': True,
    'output_retrieval': True, 'additional_usd': '6.5', 'overall_usd': '22',
    'purpose': 'checkpoint-reliability', 'durable_output': True, 'fresh_process': True}
RELOAD_SECONDS = 120


def check_sdk(sdk):
    if getattr(sdk,'__version__',None)!=SDK_VERSION:
        raise ValueError('Reviewed Modal SDK 1.6.1 required before allocating')


def historical_total(ledger):
    if ledger.get('unbounded_spend') or ledger.get('schema_version') != 2:
        raise ValueError('Historical billing must be bounded and reconciled')
    total = Decimal(0)
    for row in ledger['apps'].values():
        if row['state'] not in ('accepted_report_floor', 'unsettled_bounded'):
            raise ValueError('Unknown historical billing state')
        floor = Decimal(str(row['known_reported_usd']))
        hold = Decimal(str(row.get('reservation_usd', 0)))
        if not floor.is_finite() or not hold.is_finite() or min(floor, hold) < 0:
            raise ValueError('Invalid billing amount')
        total += max(floor, hold)
    return total


def authorize(approval, billing, now):
    if approval not in ({'provider': 'modal', 'paid': True, 'input_upload': True,
                    'output_retrieval': True, 'additional_usd': '8', 'overall_usd': '20'}, RELIABILITY_APPROVAL):
        raise ValueError('Explicit paid and transfer authorization required')
    fetched = datetime.fromisoformat(billing['fetched_at_utc'].replace('Z', '+00:00'))
    if fetched.tzinfo is None or not 0 <= (now-fetched).total_seconds() <= 900:
        raise ValueError('Fresh billing reconciliation required')
    reservation = Decimal(approval['additional_usd'])
    if historical_total(billing['ledger']) + reservation > Decimal(approval['overall_usd']):
        raise ValueError('Historical holds plus reservation exceed overall cap')
    return reservation


def preflight(receipt, approval, billing, recipe_sha256, now):
    authorize(approval, billing, now)
    if receipt.get('status') != 'passed' or receipt.get('recipe_sha256') != recipe_sha256:
        raise ValueError('Exact CPU-validated image receipt required')
    for field in ('snapshot_verified', 'imports_verified', 'processor_parity_verified', 'entrypoint_verified'):
        if receipt.get(field) is not True:
            raise ValueError('CPU image validation incomplete: ' + field)
    if not isinstance(receipt.get('image_id'), str) or not receipt['image_id'].startswith('im-'):
        raise ValueError('Retained immutable Modal image required')


def stop_owned(sdk, app, sandbox, tags, *, sleep=time.sleep):
    # Discovery closes a create-response-loss path without touching another job.
    owned = [sandbox] if sandbox is not None else []
    if sandbox is not None: sandbox.terminate(wait=False)
    for attempt in range(4):
        found = list(sdk.Sandbox.list(app_id=app.app_id, tags=tags))
        by_id = {s.object_id: s for s in owned + found}
        owned = list(by_id.values())
        for item in owned:
            if item.poll() is None:
                item.terminate(wait=False)
        active = [s for s in owned if s.poll() is None]
        discovered = list(sdk.Sandbox.list(app_id=app.app_id, tags=tags))
        if not active and not any(s.poll() is None for s in discovered):
            return {'stopped': True, 'sandbox_ids': sorted(by_id), 'verification_attempts': attempt+1}
        if attempt < 3: sleep(5)
    raise RuntimeError('Shutdown could not be verified; do not launch another job')


def launch(sdk, app, receipt, approval, billing, recipe_sha256, store,
           output, reservation_path, *, now=None, sleep=time.sleep, durable_store=None):
    """Caller owns authenticated app context; this function never creates keys/secrets."""
    check_sdk(sdk)
    now = now or datetime.now(timezone.utc)
    preflight(receipt, approval, billing, recipe_sha256, now)
    reliability = approval == RELIABILITY_APPROVAL
    if reliability != (durable_store is not None):
        raise ValueError('Reliability requires an explicitly bound durable output store')
    if reliability:
        from .s3 import S3Store
        if not isinstance(durable_store, S3Store) or not durable_store.prefix.startswith('chords/gpu-reliability/'):
            raise ValueError('Existing R2 store must be bound to a dedicated reliability output prefix')
    reservation = str(Decimal(approval['additional_usd']))
    keys = release_objects(store, RELEASE, max_objects=17, max_bytes=1205778)
    output.mkdir(parents=True, exist_ok=False)
    run_id = str(uuid.uuid4()); tags = {'coppertop-smoke': run_id}
    record = {'run_id': run_id, 'status': 'reserved', 'reservation_usd': reservation,
              'historical_conservative_usd': str(historical_total(billing['ledger'])),
              'release': RELEASE, 'app_id':app.app_id,'image_id': receipt['image_id'], 'automatic_retries': 0}
    # Exclusive, retained reservation prevents a second attempt through this path.
    reservation_path.parent.mkdir(parents=True, exist_ok=True)
    if reservation_path.exists():
        prior=json.loads(reservation_path.read_text())
        if prior.get('stage')!='cpu_passed' or prior.get('reservation_usd')!=reservation or prior.get('release')!=RELEASE or prior.get('image_id')!=receipt['image_id'] or prior.get('recipe_sha256')!=recipe_sha256:
            raise FileExistsError('Attempt already held/used or CPU receipt differs')
        with reservation_path.with_suffix('.gpu-once').open('x'):pass
    else:
        with reservation_path.open('x') as target:
            json.dump(record, target, indent=2);target.write('\n')
    sandbox = None
    record.update(runtime_status='not_started', retrieval_status='not_started', artifacts={})

    def phase(name, artifact=None):
        record['phase'] = name
        if artifact is not None:
            record['artifact'] = artifact
        (output/'launch.json').write_text(json.dumps(record, indent=2)+'\n')

    try:
        phase('create')
        image = sdk.Image.from_id(receipt['image_id'])
        sandbox = sdk.Sandbox.create('sleep', str(GPU_SECONDS), app=app, image=image,
            # The hard lifetime covers both compute and artifact transfer.
            tags=tags, gpu='L40S', timeout=GPU_SECONDS, idle_timeout=None,
            cpu=(4,4), memory=(65536,65536), block_network=True,
            volumes={}, secrets=[], encrypted_ports=[], h2_ports=[], unencrypted_ports=[],
            include_oidc_identity_token=False)
        record['sandbox_id'] = sandbox.object_id
        phase('input_upload')
        for key in keys:
            sandbox.filesystem.write_bytes(store.get(key), '/tmp/coppertop-input/' + object_path(key))
        phase('runtime')
        record['runtime_status'] = 'running'
        process = sandbox.exec('/opt/conda/bin/python', '-m', 'coppertop.smoke_runtime',
            '--execute-cuda', '--store', '/tmp/coppertop-input', '--model', '/opt/model',
            '--pins', '/opt/gpu-smoke-pins.json', '--evaluator', '/opt/evaluator.py',
            '--output', '/tmp/coppertop-output', '--max-seconds', str(PROCESS_SECONDS),
            timeout=PROCESS_SECONDS,env={'PYTHONPATH':'/app','HF_HUB_OFFLINE':'1',
            'TRANSFORMERS_OFFLINE':'1','HF_HOME':'/tmp/coppertop-hf','HF_MODULES_CACHE':'/tmp/coppertop-hf/modules'})
        process.wait()
        record['exit_code'] = process.returncode
        if process.returncode != 0:
            record['runtime_status'] = 'failed'
            raise RuntimeError('Native smoke failed')
        record['runtime_status'] = 'exited_zero'
        record['retrieval_status'] = 'incomplete'
        artifacts = [('result.json',65536), ('head-manifest.json',65536), ('head.safetensors',MAX_DELTA_BYTES)]
        if reliability: artifacts.append(('validation.safetensors',65536))
        for name, cap in artifacts:
            remote = '/tmp/coppertop-output/' + name
            phase('artifact_stat', name)
            size = sandbox.filesystem.stat(remote).size
            if type(size) is not int or not 0 < size <= cap: raise ValueError('Remote artifact size exceeds cap')
            destination = output / name
            phase('artifact_copy', name)
            sandbox.filesystem.copy_to_local(remote, str(destination))
            phase('artifact_length', name)
            if destination.stat().st_size != size: raise ValueError('Artifact retrieval length mismatch')
            record['artifacts'][name] = {'bytes': size, 'retrieved': True}
            if name == 'result.json':
                result = json.loads(destination.read_text())
                if result.get('status') != 'passed' or result.get('release') != RELEASE:
                    raise ValueError('Native result identity/status mismatch')
                record['runtime_status'] = 'passed'
        manifest = json.loads((output/'head-manifest.json').read_text())
        phase('artifact_verify', 'head.safetensors')
        h = hashlib.sha256()
        with (output/'head.safetensors').open('rb') as source:
            while chunk := source.read(1024**2): h.update(chunk)
        if result.get('status') != 'passed' or result.get('release') != RELEASE or manifest.get('release') != RELEASE:
            raise ValueError('Native result identity/status mismatch')
        if h.hexdigest() != result.get('checkpoint_sha256') or h.hexdigest() != manifest.get('checkpoint_sha256'):
            raise ValueError('Retrieved head checksum mismatch')
        if reliability:
            from .smoke_artifacts import publish_smoke_checkpoint, fetch_smoke_checkpoint
            phase('durable_publish')
            record['durable_status'] = 'incomplete'
            checkpoint_key = publish_smoke_checkpoint(durable_store, output, result, manifest, run_id)
            record.update(durable_status='verified', retrieval_status='verified', durable_checkpoint_key=checkpoint_key)
            phase('durable_fetch')
            fetched = output/'durable-fetched'
            durable = fetch_smoke_checkpoint(durable_store, checkpoint_key, fetched, run_id)
            phase('reload_input_upload')
            for name in ('head.safetensors','validation.safetensors'):
                sandbox.filesystem.copy_from_local(str(fetched/name), '/tmp/coppertop-reloaded/'+name)
            head = durable['artifacts']['head.safetensors']
            case = durable['artifacts']['validation.safetensors']
            phase('fresh_process_reload')
            reload_process = sandbox.exec('/opt/conda/bin/python', '-m', 'coppertop.smoke_reload',
                '--execute-cuda', '--store', '/tmp/coppertop-input', '--model', '/opt/model',
                '--pins', '/opt/gpu-smoke-pins.json', '--evaluator', '/opt/evaluator.py',
                '--head', '/tmp/coppertop-reloaded/head.safetensors',
                '--validation', '/tmp/coppertop-reloaded/validation.safetensors',
                '--checkpoint-sha256', head['sha256'], '--checkpoint-bytes', str(head['size']),
                '--validation-sha256', case['sha256'], '--validation-bytes', str(case['size']),
                '--output', '/tmp/coppertop-reloaded/verification', '--max-seconds', str(RELOAD_SECONDS),
                timeout=RELOAD_SECONDS, env={'PYTHONPATH':'/app','HF_HUB_OFFLINE':'1',
                    'TRANSFORMERS_OFFLINE':'1','HF_HOME':'/tmp/coppertop-hf','HF_MODULES_CACHE':'/tmp/coppertop-hf/modules'})
            reload_process.wait()
            if reload_process.returncode != 0: raise RuntimeError('Fresh process reload failed')
            phase('fresh_process_receipt')
            size = sandbox.filesystem.stat('/tmp/coppertop-reloaded/verification/result.json').size
            if type(size) is not int or not 0 < size <= 65536: raise ValueError('Reload report exceeds cap')
            # Bound the report at its source; the generic SDK copy has no caller byte limit.
            reader = sandbox.exec('/opt/conda/bin/python', '-c',
                "import sys\nfrom pathlib import Path\n"
                "with Path(sys.argv[1]).open('rb') as source: data=source.read(65537)\n"
                "if not 0<len(data)<=65536 or len(data)!=int(sys.argv[2]): raise ValueError('Report size differs')\n"
                "sys.stdout.write(data.decode('utf-8'))\n",
                '/tmp/coppertop-reloaded/verification/result.json', str(size), timeout=30)
            payload = reader.stdout.read().encode('utf-8')
            reader.wait()
            if reader.returncode != 0 or len(payload) != size or len(payload) > 65536:
                raise ValueError('Bounded reload report transfer failed')
            with (output/'reload-result.json').open('xb') as target: target.write(payload)
            fresh = json.loads((output/'reload-result.json').read_text())
            if (fresh.get('status') != 'passed' or fresh.get('release') != RELEASE
                or fresh.get('checkpoint_sha256') != head['sha256']
                or fresh.get('validation_sha256') != case['sha256']
                or fresh.get('fresh_process_reload_verified') is not True
                or fresh.get('velocity_exact') is not True or fresh.get('velocity_max_abs_difference') != 0
                or type(fresh.get('process_pid')) is not int or fresh['process_pid'] <= 0
                or type(result.get('process_pid')) is not int or fresh['process_pid'] == result['process_pid']):
                raise ValueError('Fresh-process prediction receipt differs')
            record['fresh_process_reload_verified'] = True
            report_key = durable_store.put((output/'reload-result.json').read_bytes())
            from .streams import read_small
            if read_small(durable_store, report_key, 65536) != (output/'reload-result.json').read_bytes():
                raise ValueError('Durable reload report read-back mismatch')
            record['durable_reload_report_key'] = report_key
        if not reliability: record['retrieval_status'] = 'verified'
        record.update(status='passed', phase='complete')
    except BaseException as error:
        record.update(status='failed', error_type=type(error).__name__, failed_phase=record['phase'])
        raise
    finally:
        try:
            record['shutdown'] = stop_owned(sdk, app, sandbox, tags, sleep=sleep)
        except BaseException:
            record['status'] = 'shutdown_unverified'
            raise
        finally:
            (output/'launch.json').write_text(json.dumps(record, indent=2)+'\n')
    return record


def prepare_cached(sdk, app, cache_id, approval, billing, root, evaluator, store,
                   output, reservation_path, *, now=None, sleep=time.sleep):
    """Bounded warm-cache preparation. Missing cached weights fail; never download them."""
    from .image_check import recipe_digest
    check_sdk(sdk)
    now=now or datetime.now(timezone.utc);reservation=str(authorize(approval,billing,now))
    pins=json.loads((root/'containers/gpu-smoke-pins.json').read_text())
    # Enforce the reviewed evaluator blob before it enters compute.
    with evaluator.open('rb') as source:data=source.read(32769)
    if len(data)>32768 or hashlib.sha1(b'blob '+str(len(data)).encode()+b'\0'+data).hexdigest()!=pins['evaluator_git_blob']:
        raise ValueError('Pinned evaluator required')
    keys=release_objects(store,RELEASE,max_objects=17,max_bytes=1205778)
    names=['requirements-gpu-smoke.lock','containers/Dockerfile.gpu-smoke','containers/gpu-smoke-pins.json']
    names+=sorted(str(p.relative_to(root)) for p in (root/'coppertop').glob('*.py'))
    code={name:(root/name).read_bytes() for name in names}
    if sum(map(len,code.values()))>1024**2:raise ValueError('Preparation code upload exceeds cap')
    output.mkdir(parents=True,exist_ok=False);reservation_path.parent.mkdir(parents=True,exist_ok=True)
    record={'stage':'cpu_reserved','reservation_usd':reservation,'recipe_sha256':recipe_digest(root),
            'release':RELEASE,'app_id':app.app_id,'cache_id':cache_id}
    with reservation_path.open('x') as target:json.dump(record,target,indent=2)
    sb=None;tags={'coppertop-smoke':str(uuid.uuid4())}
    try:
        sb=sdk.Sandbox.create('sleep','1200',app=app,image=sdk.Image.from_id(cache_id),tags=tags,
            timeout=1200,idle_timeout=60,cpu=(4,4),memory=(16384,16384),gpu=None,
            secrets=[],volumes={},encrypted_ports=[],h2_ports=[],unencrypted_ports=[],
            outbound_domain_allowlist=['pypi.org','files.pythonhosted.org'],include_oidc_identity_token=False)
        for name,content in code.items():sb.filesystem.write_bytes(content,'/app/'+name)
        sb.filesystem.write_bytes(data,'/opt/evaluator.py')
        sb.filesystem.write_bytes(code['containers/gpu-smoke-pins.json'],'/opt/gpu-smoke-pins.json')
        for key in keys:sb.filesystem.write_bytes(store.get(key),'/tmp/coppertop-input/'+object_path(key))
        bootstrap="""import os,subprocess,sys,hashlib,json
from pathlib import Path
if not Path('/work/model/model.safetensors.index.json').is_file():raise ValueError('Warm checkpoint cache absent; no download/fallback')
p=Path('/opt/model')
if not p.exists():p.symlink_to('/work/model',target_is_directory=True)
pin=json.loads(Path('/opt/gpu-smoke-pins.json').read_text())['flash_attention_wheel']
w=Path('/work')/pin['name'];h=hashlib.sha256()
if w.stat().st_size!=pin['size']:raise ValueError('Cached wheel size differs')
with w.open('rb') as source:
 while chunk:=source.read(1024**2):h.update(chunk)
if h.hexdigest()!=pin['digest'].split(':')[1]:raise ValueError('Cached wheel checksum differs')
subprocess.run([sys.executable,'-m','pip','install','--no-deps','--force-reinstall',str(w)],check=True,timeout=100)
subprocess.run([sys.executable,'-m','pip','install','--no-deps','--only-binary=:all:','--retries','0','--timeout','15','--index-url','https://pypi.org/simple','-r','/app/requirements-gpu-smoke.lock'],check=True,timeout=400)
subprocess.run([sys.executable,'-m','pip','check'],check=True,timeout=20)
os.environ.update(PYTHONPATH='/app',HF_HUB_OFFLINE='1',TRANSFORMERS_OFFLINE='1',HF_HOME='/tmp/coppertop-hf',HF_MODULES_CACHE='/tmp/coppertop-hf/modules')
subprocess.run([sys.executable,'-m','coppertop.image_check','--root','/app','--store','/tmp/coppertop-input','--model','/opt/model','--pins','/opt/gpu-smoke-pins.json','--evaluator','/opt/evaluator.py','--image-id',sys.argv[1],'--output','/tmp/image-receipt.json'],check=True,timeout=500)
"""
        process=sb.exec('/opt/conda/bin/python','-c',bootstrap,cache_id,timeout=1050)
        process.wait()
        if process.returncode!=0:raise RuntimeError('CPU cache/image gate failed; no GPU')
        if not 0<sb.filesystem.stat('/tmp/image-receipt.json').size<=65536:raise ValueError('CPU receipt size cap')
        sb.filesystem.copy_to_local('/tmp/image-receipt.json',str(output/'image-receipt.json'))
        receipt=json.loads((output/'image-receipt.json').read_text())
        preflight(receipt,approval,billing,record['recipe_sha256'],now)
        # Only a CPU-validated filesystem is retained; never snapshot credentials.
        prepared=sb.snapshot_filesystem(timeout=30,ttl=3600)
        receipt['image_id']=prepared.object_id;record.update(stage='cpu_passed',image_id=prepared.object_id)
        (output/'image-receipt.json').write_text(json.dumps(receipt,indent=2)+'\n')
        return receipt
    except BaseException as error:
        record.update(stage='cpu_failed',error_type=type(error).__name__);raise
    finally:
        try:record['shutdown']=stop_owned(sdk,app,sb,tags,sleep=sleep)
        except BaseException:
            record['stage']='shutdown_unverified';raise
        finally:
            reservation_path.write_text(json.dumps(record,indent=2)+'\n')
            (output/'cpu-launch.json').write_text(json.dumps(record,indent=2)+'\n')
