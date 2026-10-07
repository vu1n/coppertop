"""Exact-approval single-use Modal binding; bulk output stays Volume→CPU→R2."""
import argparse
from datetime import datetime, timezone
from decimal import Decimal
import json
import os
from pathlib import Path
import re
import subprocess
import sys
import time
from types import SimpleNamespace

from .chords import canonical, digest, object_path, LocalStore
from .cloud_roundtrip import _metadata
from .gpu_launch import historical_total
from .mechanism_runtime import plan as worker_plan
from .modal_reload import _container
from .modal_staging import stop_verifier
from .performance_cloud import simulator_preflight, loopback_gate
from .performance_runtime import SIMULATOR, source_gate, MODEL
from .image_check import require_native_interpreter, require_dependencies, pinned_dependencies
from .r2_session import SessionScope, R2StoreFactory, mint
from .reload_launch import (read_json, save, cli_json, hard_deadline, refresh_billing,
    selected_env, parent_credentials, delete_owned_volume, stop_and_audit_owned_app)
from .streams import SMALL_BYTES, read_small

MOUNT = '/coppertop-mechanism'
ADDITIONAL = Decimal('6')
CUMULATIVE = Decimal('64.82')


def selected_worker(workload):
    if workload == 'mechanism': return worker_plan()
    if workload == 'official_recording':
        from .official_recording_worker import plan
        return plan()
    raise ValueError('Known bounded workload required')


def prepare(ledger, attempt, workload='mechanism'):
    """Pure preparation: no credentials, signing, provider calls or allocation."""
    if not re.fullmatch(r'mechanism-[0-9a-f]{16}', attempt):
        raise ValueError('Dedicated mechanism attempt required')
    worker = selected_worker(workload)
    cumulative = CUMULATIVE if workload == 'mechanism' else Decimal('70.82')
    if Decimal(str(ledger['cumulative_cap'])) != cumulative or historical_total(ledger)+ADDITIONAL > cumulative:
        raise ValueError('Preserved holds plus $6 must fit proposed cumulative ceiling')
    scope = SessionScope('mechanism', 'coppertop', 'chords/mechanism/'+attempt)
    p = {'kind': 'frozen_mechanism_cloud_feasibility', 'attempt_id': attempt,
        'app_name': 'coppertop-'+attempt, 'volume_name': 'coppertop-'+attempt+'-outputs',
        'environment': 'main', 'region': 'us', 'image': worker['image'],
        'worker': worker, 'worker_sha256': digest(canonical(worker)),
        'ledger_sha256': digest(canonical(ledger)), 'retained_holds_usd': str(historical_total(ledger)),
        'additional_cap_usd': str(ADDITIONAL), 'cumulative_cap_usd': str(cumulative),
        'resources': {'gpu': 'L40S', 'gpu_cpu': 4, 'gpu_memory_mib': 65536,
            'gate_cpu': 4, 'gate_memory_mib': 16384, 'publisher_cpu': 2, 'publisher_memory_mib': 4096,
            'single_use_containers': True, 'retries': 0, 'scaledown_seconds': 2,
            'startup_seconds_each': 300, 'gate_seconds': 610, 'worker_seconds': 250, 'gpu_phase_seconds': 270,
            'publisher_seconds': 300, 'controller_seconds': 2700,
            'temporary_volume_bytes': 32*1024**2, 'metadata_bytes': 65536},
        'auth': {'ttl_seconds': 3600, 'session_count': 1, 'bucket': scope.bucket,
            'role': scope.role, 'actions': list(scope.actions), **scope.paths},
        'pricing': {'checked_utc': '2026-10-07T12:04:04+00:00',
            'modal_url': 'https://modal.com/pricing', 'r2_url': 'https://developers.cloudflare.com/r2/pricing/',
            'l40s_per_second': '.000542', 'cpu_core_second': '.0000131', 'gib_second': '.00000222',
            'region_multiplier_bound': '1.75', 'r2_incremental_rounded_requests_usd': '4.86',
            'r2_first_month_storage_unit_usd': '.015', 'compute_storage_egress_envelope_usd': '1.00'},
        'retention': 'Keep ≤24MiB immutable R2 evidence in Standard storage; no R2 deletion. Delete only new owned Volume after publication attempt and zero-active audit. Preserve cached images and all prior artifacts.',
        'paid_authorized': False, 'new_image_builds': False, 'new_model_or_asset_downloads': False}
    if workload != 'mechanism':
        p.update(kind='official_recording_cloud_acceptance', workload=workload)
    return {'cloud_plan': p, 'cloud_plan_sha256': digest(canonical(p)), 'scope': scope}


def output_inventory(directory):
    """Only digest-addressed objects and bounded metadata can leave the worker."""
    directory = Path(directory)
    if directory.is_symlink(): raise ValueError('Output symlink rejected')
    files = list(directory.rglob('*'))
    if any(path.is_symlink() for path in files) or sum(p.stat().st_size for p in files if p.is_file()) > 24*1024**2:
        raise ValueError('Output filesystem exceeds immutable publication bound')
    objects = {}
    local = LocalStore(directory/'store')
    for path in (directory/'store'/'objects').glob('*/*'):
        if not path.is_file() or path.relative_to(directory/'store').as_posix() != object_path(path.name):
            raise ValueError('Malformed immutable output path')
        ref = local.stat(path.name)
        if ref.size > SMALL_BYTES: raise ValueError('Single immutable PUT exceeds4MiB')
        if digest(local.get(ref.sha256)) != ref.sha256: raise ValueError('Output checksum differs')
        objects[ref.sha256] = ref.size
    if len(objects) > 64 or sum(objects.values()) > 24*1024**2-65536:
        raise ValueError('Publication object count/bytes exceed session')
    return objects


def publish_directory(directory, record, store):
    """CPU-only upload with immutable writes and complete byte/hash readback."""
    objects = {} if record.get('artifact_error_type') else output_inventory(directory)
    if objects != record['objects']: raise ValueError('Committed output inventory differs')
    local = LocalStore(Path(directory)/'store')
    for key in objects:
        raw = local.get(key)
        if store.put(raw) != key or store.get(key) != raw:
            raise ValueError('Immutable artifact readback differs')
    raw = canonical(record)
    if len(raw) > 65536: raise ValueError('Report metadata bound exceeded')
    key = store.put(raw)
    if key != digest(raw) or read_small(store, key, 65536) != raw:
        raise ValueError('Immutable feasibility report readback differs')
    return key


def create_functions(sdk, prepared, image, volume, store_factory, session, runner=subprocess.run):
    """Local registration only; exactly three sequential single-use functions."""
    if sdk.__version__ != '1.6.1': raise ValueError('Modal1.6.1 required')
    p = prepared['cloud_plan']
    mount = volume.with_mount_options(sub_path='/'+p['attempt_id'])
    app = sdk.App(p['app_name'], secrets=[], volumes={}, include_source=False)
    common = dict(image=image, region='us', serialized=True, retries=0, single_use_containers=True,
        min_containers=0, buffer_containers=0, max_containers=1, scaledown_window=2, startup_timeout=300)
    def binding():
        if selected_worker(p.get('workload','mechanism')) != p['worker']: raise ValueError('Mounted worker plan differs')
    @app.function(**dict(common, name='mechanism-cache-gate', cpu=(4,4), memory=(16384,16384),
        gpu=None, timeout=610, secrets=[], block_network=True, volumes={}))
    def gate():
        binding(); require_native_interpreter(); loopback_gate(); source_gate()
        require_dependencies(pinned_dependencies(Path('/app/requirements-gpu-smoke.lock'), Path('/app/requirements-r2.txt')))
        from .smoke_runtime import verify_snapshot
        verify_snapshot(MODEL, read_json(Path('/app/containers/gpu-smoke-pins.json'))['model'])
        result = simulator_preflight(runner)
        return {'container_id': _container(os.environ.get('MODAL_TASK_ID')), 'status': result['status'],
                'simulator': result, 'plan_sha256': prepared['cloud_plan_sha256']}
    @app.function(**dict(common, name='mechanism-feasibility', cpu=(4,4), memory=(65536,65536),
        gpu='L40S', timeout=270, secrets=[], block_network=True, volumes={MOUNT: mount}))
    def evaluate():
        binding()
        target = Path(MOUNT)/'outputs'
        env = {key: os.environ[key] for key in ('PATH','HOME','LD_LIBRARY_PATH','NVIDIA_VISIBLE_DEVICES','CUDA_VISIBLE_DEVICES') if key in os.environ}
        env.update(HF_HUB_OFFLINE='1', TRANSFORMERS_OFFLINE='1', MUJOCO_GL='egl', PYTHONPATH='/app')
        with (Path('/tmp')/(p['attempt_id']+'-worker.log')).open('wb') as log:
            try:
                module = 'coppertop.official_recording_worker' if p.get('workload') == 'official_recording' else 'coppertop.mechanism_runtime'
                identity_args = ['--run-id',p['attempt_id']] if p.get('workload') == 'official_recording' else []
                process = runner([SIMULATOR, '-m', module, '--execute-local',
                    '--plan-sha256', p['worker_sha256'], '--output', str(target), *identity_args],
                    stdout=log, stderr=log, env=env, timeout=250)
                passed = process.returncode == 0
            except subprocess.TimeoutExpired:
                passed = False
        result_path = target/('result.json' if passed else 'failure.json')
        result = read_json(result_path, 65536) if result_path.is_file() else {'status':'failed','comparison_valid':False}
        recording = p.get('workload') == 'official_recording'
        expected_status, expected_queries = ('recording_acceptance',1) if recording else ('bounded_feasibility',5)
        if passed and (result.get('status') != expected_status or result.get('query_calls') != expected_queries):
            raise ValueError('Feasibility gate result differs')
        record = {'container_id': _container(os.environ.get('MODAL_TASK_ID')),
            'plan_sha256': prepared['cloud_plan_sha256'], 'attempt_id': p['attempt_id'],
            'status': 'passed' if passed else 'failed', 'result': result,
            'objects': output_inventory(target), 'worker_plan': p['worker']}
        # Publish even failed live gates; never report them as policy failures.
        record['query_journal'] = read_json(target/'queries.json', 65536) if (target/'queries.json').is_file() else {'calls':[]}
        if len(canonical(record)) > 65536: raise ValueError('Metadata envelope exceeds bound')
        save(Path(MOUNT)/'record.json', record); volume.commit()
        return record
    @app.function(**dict(common, name='mechanism-cpu-publisher', cpu=(2,2), memory=(4096,4096),
        gpu=None, timeout=300, secrets=list(session), volumes={MOUNT: mount.with_mount_options(read_only=True)}))
    def publish(record):
        binding()
        incoming = digest(canonical(record))
        if record.get('recovery') is True:
            # Failed provider RPCs cannot bypass durable failure publication. Only
            # committed Volume bytes are available; missing/uncommitted data is explicit.
            record = dict(record)
            path = Path(MOUNT)/'record.json'
            if path.is_file():
                try: record['committed_record'] = read_json(path, 32768)
                except (OSError, ValueError): record['committed_record_unavailable'] = True
            target = Path(MOUNT)/'outputs'
            try: record['objects'] = output_inventory(target)
            except (OSError, ValueError) as error:
                record['objects'] = {}; record['artifact_error_type'] = type(error).__name__
            journal = target/'queries.json'
            if journal.is_file():
                try: record['query_journal'] = read_json(journal, 32768)
                except (OSError, ValueError): record['query_journal_unavailable'] = True
        elif read_json(Path(MOUNT)/'record.json', 65536) != record:
            raise ValueError('Committed feasibility record differs')
        key = publish_directory(Path(MOUNT)/'outputs', record, store_factory('mechanism'))
        return {'container_id': _container(os.environ.get('MODAL_TASK_ID')), 'report_key': key,
                'source_record_sha256': incoming, 'published_status': record['status'],
                'plan_sha256': prepared['cloud_plan_sha256']}
    return SimpleNamespace(app=app, gate=gate, evaluate=evaluate, publish=publish)


def execute_phases(functions, prepared, output, *, admission, invoke, stopped, cleanup):
    state = {'status':'started', 'phases':{}, 'automatic_retries':0}
    error = None
    try:
        admission()
        for name, function, args, bound in (('gate', functions.gate, (), 920),
                ('evaluate', functions.evaluate, (), 580), ('publish', functions.publish, None, 610)):
            if name == 'publish': args = (state['phases']['evaluate'],)
            try: result = invoke(function, args, bound)
            except Exception as caught:
                if name != 'evaluate': raise
                result = {'container_id':None, 'plan_sha256':prepared['cloud_plan_sha256'],
                    'attempt_id':prepared['cloud_plan']['attempt_id'], 'status':'failed', 'recovery':True,
                    'objects':{}, 'result':{'status':'failed','comparison_valid':False,'error_type':type(caught).__name__}}
            _metadata(result)
            state['phases'][name] = result; save(Path(output)/'outcome.json', state)
            if result.get('plan_sha256') != prepared['cloud_plan_sha256'] or stopped(result['container_id']) is not True:
                raise ValueError('Phase binding/stop verification failed')
            if name == 'gate' and result.get('status') != 'passed':
                raise ValueError('CPU gate failed; GPU forbidden')
            if name == 'publish':
                prior=state['phases']['evaluate']; expected=digest(canonical(prior))
                if (result.get('source_record_sha256') != expected or result.get('published_status') != prior['status']
                        or not re.fullmatch('[0-9a-f]{64}', result.get('report_key',''))
                        or (not prior.get('recovery') and result['report_key'] != expected)):
                    raise ValueError('Publication receipt differs')
        state['status'] = state['phases']['evaluate']['status']
        state['durable_report_verified'] = True
    except BaseException as caught:
        error = caught; state.update(status='failed', error_type=type(caught).__name__)
    finally:
        try:
            state['cleanup'] = receipt = cleanup()
            if any(receipt.get(k) is not True for k in ('app_stopped','volume_deleted')) or any(type(receipt.get(k)) is not int or receipt[k] != 0 for k in ('active_containers','active_sandboxes')):
                raise ValueError('Owned cleanup not verified')
        except BaseException as caught:
            state.update(status='failed', cleanup_error_type=type(caught).__name__)
            if error is None: error = caught
        save(Path(output)/'outcome.json', state)
    if error is not None: raise error
    return state


def execute(prepared, ledger, root, env_file, approved_sha256, *, sdk=None, runner=subprocess.run):
    """One exact approval, one retained hold, no fallback/retry or shared cleanup."""
    checked = prepare(ledger, prepared['cloud_plan']['attempt_id'], prepared['cloud_plan'].get('workload','mechanism'))
    if (approved_sha256 != prepared['cloud_plan_sha256']
            or digest(canonical(prepared['cloud_plan'])) != approved_sha256
            or checked['cloud_plan_sha256'] != approved_sha256):
        raise ValueError('Exact proposed plan/code/ledger approval required')
    prepared = checked  # Never sign a caller-supplied scope merely matching a hash field.
    pricing_time = datetime.fromisoformat(prepared['cloud_plan']['pricing']['checked_utc'])
    if not 0 <= (datetime.now(timezone.utc)-pricing_time).total_seconds() <= 86400:
        raise ValueError('Pricing review expired; reprepare approval before launch')
    if sdk is None: import modal as sdk
    if sdk.__version__ != '1.6.1': raise ValueError('Modal1.6.1 required')
    p = prepared['cloud_plan']; root = Path(root).resolve()
    output = root/'.local/mechanism'/p['attempt_id']
    if any(path.is_symlink() for path in (root/'.local', output.parent, output)):
        raise ValueError('Dedicated ignored attempt directory required')
    output.mkdir(parents=True, exist_ok=True)
    with (output/'consumed.json').open('x') as marker: marker.write(canonical({'plan_sha256':approved_sha256}).decode())
    owned = {'created':False, 'attempted':False, 'volume_id':None, 'context':None, 'entered':False}
    functions = volume = None
    current = ledger
    pending = 'pending-'+p['attempt_id']
    deadline=time.monotonic()+2700
    def ownership():
        save(output/'ownership.json', {k:v for k,v in owned.items() if k != 'context'} | {'app_id': functions.app.app_id if functions else None,'volume_name':p['volume_name']})
    def bind():
        if functions and functions.app.app_id and pending in current['apps']:
            identity = functions.app.app_id
            if not re.fullmatch(r'ap-[A-Za-z0-9]+', identity) or identity in current['apps']: raise ValueError('App ledger identity collision')
            current['apps'][identity] = current['apps'].pop(pending); save(output/'ledger.json', current); ownership()
    def billing():
        nonlocal current
        bind(); identity = functions.app.app_id if functions else None
        key = identity if identity in current['apps'] else pending
        current = refresh_billing(current, output, app_id=identity, reservation_key=key, runner=runner, additional_cap=6)
    def admission():
        billing()
        owned['attempted']=True; ownership()
        with hard_deadline(60):
            sdk.Volume.objects.create(p['volume_name'], environment_name='main', allow_existing=False)
            owned['created']=True; volume.hydrate(); owned['volume_id']=volume.object_id; ownership()
        owned['context']=functions.app.run(name=p['app_name'], environment_name='main')
        with hard_deadline(300): owned['context'].__enter__()
        owned['entered']=True; bind()
    def invoke(function, args, bound):
        billing()
        remaining=deadline-time.monotonic()
        if function is functions.evaluate and remaining < 580+610+240:
            raise TimeoutError('Insufficient whole-attempt time for GPU/publication/cleanup')
        if remaining <= 0: raise TimeoutError('Controller deadline exhausted')
        return function.spawn(*args).get(timeout=min(bound,remaining))
    def stopped(identity):
        if identity is None:
            # A failed RPC supplied no task ID. Prove the exact app has no GPU
            # task/sandbox left before its single CPU recovery publisher runs.
            # Do not terminate individual tasks: Modal can reschedule cancelled
            # inputs even with retries=0. Wait for the bounded Function instead.
            idle_deadline=time.monotonic()+120
            while time.monotonic() < idle_deadline:
                containers=cli_json([sys.executable,'-m','modal','container','list','--app-id',functions.app.app_id,'--env','main','--json'],runner,65536)
                script='import modal,json,sys; print(json.dumps([s.object_id for s in modal.Sandbox.list(app_id=sys.argv[1])]))'
                sandboxes=cli_json([sys.executable,'-c',script,functions.app.app_id],runner,65536)
                if containers == [] and sandboxes == []: return True
                time.sleep(2)
            return False
        return stop_verifier(functions.app.app_id, environment='main', command=(sys.executable,'-m','modal'), runner=runner)(identity)
    def cleanup():
        bind(); ownership()
        context_error = None
        if owned['entered']:
            try:
                with hard_deadline(60): owned['context'].__exit__(None,None,None)
            except BaseException as caught: context_error=type(caught).__name__
            owned['entered']=False
        receipt = {'app_stopped':True,'active_containers':0,'active_sandboxes':0}
        if functions and functions.app.app_id: receipt=stop_and_audit_owned_app(functions.app.app_id, runner=runner)
        if owned['created']:
            if not owned['volume_id']:
                with hard_deadline(60): volume.hydrate(); owned['volume_id']=volume.object_id; ownership()
            delete_owned_volume(sdk,p['volume_name'],owned['volume_id'])
        elif owned['attempted']: raise RuntimeError('Unconfirmed Volume creation requires ownership audit')
        billing()
        return dict(receipt,volume_deleted=True,holds_released=0,context_exit_error_type=context_error)
    try:
        with hard_deadline(2700):
            current = refresh_billing(ledger, output, runner=runner, additional_cap=6)
            current['apps'][pending]={'state':'unsettled_bounded','known_reported_usd':'0','reservation_usd':'6','description':p['app_name']}
            save(output/'ledger.json',current)
            for command, field in (('app','description'),('volume','name')):
                rows = cli_json([sys.executable,'-m','modal',command,'list','--env','main','--json'],runner)
                if any(row.get(field) == p['app_name' if command == 'app' else 'volume_name'] for row in rows): raise ValueError('Dedicated resource already exists')
            values=selected_env(env_file); access,secret=parent_credentials(values); endpoint=values['CLOUDFLARE_R2_URL']
            scope=prepared['scope']
            session=[sdk.Secret.from_dict(mint(access,secret,endpoint=endpoint,scope=scope,ttl_seconds=3600).environment())]
            del values,access,secret
            with hard_deadline(60): image=sdk.Image.from_id(p['image'])
            if image.object_id != p['image']: raise ValueError('Cached image differs')
            for name, expected in p['worker']['runtime_sha256'].items():
                path=root/name
                if path.is_symlink() or digest(path.read_bytes()) != expected: raise ValueError('Approved upload code differs')
                if not name.startswith('coppertop/'):
                    image=image.add_local_file(path,'/app/'+name,copy=False)
            image=image.add_local_dir(root/'coppertop','/app/coppertop',copy=False,
                ignore=lambda path: path.suffix != '.py')
            volume=sdk.Volume.from_name(p['volume_name'],environment_name='main',create_if_missing=False)
            functions=create_functions(sdk,prepared,image,volume,R2StoreFactory(endpoint,(scope,)),session,runner)
            return execute_phases(functions,prepared,output,admission=admission,invoke=invoke,stopped=stopped,cleanup=cleanup)
    except BaseException as error:
        save(output/'launch-failure.json',{'status':'failed','error_type':type(error).__name__,'consumed':True,'automatic_retries':0})
        raise


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--ledger',type=Path,required=True)
    parser.add_argument('--attempt',required=True)
    parser.add_argument('--workload',choices=('mechanism','official_recording'),default='mechanism')
    parser.add_argument('--execute',action='store_true')
    parser.add_argument('--approved-cloud-plan-sha256')
    parser.add_argument('--env-file',type=Path,default=Path.home()/'code/coppertop/.env.local')
    args=parser.parse_args(); ledger=read_json(args.ledger); prepared=prepare(ledger,args.attempt,args.workload)
    if not args.execute:
        print(json.dumps({key:value for key,value in prepared.items() if key!='scope'})); return 0
    result = execute(prepared,ledger,Path(__file__).resolve().parent.parent,args.env_file,args.approved_cloud_plan_sha256)
    return 0 if result.get('status') == 'passed' else 1


if __name__ == '__main__': raise SystemExit(main())
