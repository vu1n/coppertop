"""One local, approval-bound reload attempt. Preparation never reads credentials."""
import argparse
import copy
import hashlib
import json
import re
import shlex
import signal
import subprocess
import sys
import time
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path
from urllib.request import Request, build_opener, HTTPRedirectHandler
from urllib.parse import urlsplit

from .chords import canonical, object_path
from .cloud_stage import StageManifest
from .gpu_launch import historical_total
from .gpu_smoke import RELEASE, MODEL_REPO, MODEL_REVISION
from .modal_image import SEED_IMAGE_ID, build_recipe, prepare_image, create_image_gate
from .modal_reload import CHECKPOINT_RUN_ID, ORIGINAL_CONTAINER_ID, create_reload_functions, execute_reload
from .modal_staging import stop_verifier
from .r2_session import SessionScope, R2StoreFactory, mint, _endpoint
from .streams import ObjectRef

CHECKPOINT_PREFIX = 'chords/gpu-reliability/b4538c99-0202-45a5-b637-075953ea4040'
RECEIPT = 'dae51455e3e64352b15121b75cb7f54e6b3d96c7bf9f6599c661e5c4a02df811'
HEAD = 'f7a1d0ddad3422f6ae4d9bb1d48f68d8bf8d0f164fa0f6b9f3223744982053c5'
ENV_NAMES = {'CLOUDFLARE_API_TOKEN', 'CLOUDFLARE_ACCOUNT_ID', 'CLOUDFLARE_R2_URL', 'R2_BUCKET'}


def read_json(path, limit=262144):
    with Path(path).open('rb') as stream:
        raw = stream.read(limit + 1)
    if len(raw) > limit:
        raise ValueError('Local metadata exceeds bound')
    return json.loads(raw)


def save(path, value):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + '.partial')
    temporary.write_bytes(canonical(value))
    temporary.replace(path)


def prepare(plan, ledger, root):
    """Pure local validation, source/checkpoint manifests and exact review hash."""
    if (plan.get('provider') != 'Modal' or plan.get('region') != 'us'
        or plan.get('environment') != 'main' or Decimal(plan['additional_cap']) != 8
        or Decimal(plan['cumulative_cap']) not in (30, 38) or plan.get('holds_released') != 0
        or plan.get('checkpoint_run_id') != CHECKPOINT_RUN_ID
        or plan.get('source_release') != RELEASE or plan.get('checkpoint_receipt_sha256') != RECEIPT
        or plan.get('original_training_task_id') != ORIGINAL_CONTAINER_ID
        or plan.get('original_training_identity_verified') is not True
        or plan.get('image', {}).get('cache_seed') != SEED_IMAGE_ID
        or plan['image'].get('model') != MODEL_REPO or plan['image'].get('model_revision') != MODEL_REVISION):
        raise ValueError('Pinned reload scope differs')
    attempt = plan['attempt_id']
    if not re.fullmatch(r'reload-[0-9a-f]{16}', attempt):
        raise ValueError('Exact bounded reload attempt required')
    if plan.get('app_name') != 'coppertop-reload-only-'+attempt:
        raise ValueError('Dedicated owned App name required')
    storage = plan['storage']
    if (storage.get('input_volume') != 'coppertop-' + attempt + '-inputs'
        or storage.get('output_volume') is not None or storage.get('total_bytes_ceiling') != 3 * 1024**3):
        raise ValueError('Only one dedicated new input Volume is allowed')
    limits = plan['limits']
    expected = {'updated_image_build_cpu_seconds':600, 'image_build_startup_seconds':300,
        'image_gate_cpu_seconds':1200, 'cpu_stage_seconds':600,
        'cpu_checkpoint_restage_seconds':1800, 'cpu_report_publish_seconds':300,
        'function_startup_seconds_each':300, 'gpu_function_seconds':300,
        'native_reload_subprocess_seconds':120, 'scale_down_seconds':2,
        'shutdown_audit_budget_seconds_each':60, 'controller_wall_seconds':7200,
        'r2_total_requests':2000, 'r2_new_write_bytes':65536,
        'modal_outbound_bytes':1073741824, 'max_gpu_containers':1, 'automatic_retries':0}
    if limits != expected:
        raise ValueError('Reviewed phase limits differ')
    cap = Decimal(str(ledger.get('cumulative_cap', '30')))
    if cap != Decimal(plan['cumulative_cap']):
        raise ValueError('Plan and ledger cumulative caps differ')
    total = historical_total(ledger)
    if total != Decimal(str(plan['prior_conservative_holds'])) or total + 8 > cap:
        raise ValueError('Historical holds differ or exceed cumulative cap')
    objects = plan['exact_input_objects']
    if not isinstance(objects, list) or len(objects) != 22:
        raise ValueError('Exactly 22 pinned input objects required')
    paths = {}
    for item in objects:
        if set(item) != {'path','sha256','size'} or item['path'] in paths:
            raise ValueError('Duplicate or malformed input reference')
        ref = ObjectRef(item['sha256'], item['size'])
        if item['path'] not in ('chords/' + object_path(ref.sha256), CHECKPOINT_PREFIX + '/' + object_path(ref.sha256)):
            raise ValueError('Object outside pinned canonical namespaces')
        paths[item['path']] = ref
    source_objects = {path.removeprefix('chords/'): ref for path, ref in paths.items() if path.startswith('chords/objects/')}
    if len(source_objects) != 17 or object_path(RELEASE) not in source_objects:
        raise ValueError('Pinned 17-object canonical source required')
    artifacts = plan['checkpoint_artifacts']
    if set(artifacts) != {'head.safetensors','validation.safetensors','head-manifest.json','result.json'}:
        raise ValueError('Four native checkpoint artifacts required')
    refs = {name:ObjectRef(value['sha256'], value['size']) for name,value in artifacts.items()}
    if refs['head.safetensors'] != ObjectRef(HEAD, 1230711168):
        raise ValueError('Preserved head differs')
    if {CHECKPOINT_PREFIX+'/'+object_path(ref.sha256):ref for ref in refs.values()} | {CHECKPOINT_PREFIX+'/'+object_path(RECEIPT):ObjectRef(RECEIPT,818)} != {p:r for p,r in paths.items() if p.startswith(CHECKPOINT_PREFIX+'/')}:
        raise ValueError('Checkpoint artifacts and immutable receipt differ')
    scopes = (SessionScope('source','coppertop','chords',tuple(p for p in paths if p.startswith('chords/objects/'))),
        SessionScope('checkpoint','coppertop',CHECKPOINT_PREFIX,tuple(p for p in paths if p.startswith(CHECKPOINT_PREFIX+'/'))),
        SessionScope('report','coppertop','chords/gpu-reload/'+attempt))
    auth = plan['authentication']
    if (auth.get('ttl_seconds') != 7200 or auth.get('bucket') != 'coppertop'
        or auth.get('session_count') != 3
        or set(auth['source_session']['objectPaths']) != set(scopes[0].object_paths)
        or set(auth['checkpoint_session']['objectPaths']) != set(scopes[1].object_paths)
        or any(set(auth[k]['actions']) != {'GetObject','HeadObject'} for k in ('source_session','checkpoint_session'))
        or set(auth['write_session']['actions']) != {'GetObject','HeadObject','PutObject'}
        or auth['write_session']['prefixPaths'] != [scopes[-1].prefix+'/objects/']):
        raise ValueError('Approved authentication scope differs')
    recipe = build_recipe(Path(root), region='us')
    recipe_hash = hashlib.sha256(canonical(recipe)).hexdigest()
    return {'plan_sha256':hashlib.sha256(canonical(plan)).hexdigest(),
        'recipe_sha256':recipe_hash, 'ledger_sha256':hashlib.sha256(canonical(ledger)).hexdigest(),
        'historical_conservative_usd':str(total), 'full_hold_usd':'8',
        'conservative_after_hold_usd':str(total+8), 'cloud_actions_performed':False,
        'runtime_dependency_alignment': recipe['dependency_alignment'],
        'source':StageManifest(CHECKPOINT_RUN_ID,RELEASE,source_objects),
        'checkpoint':StageManifest(CHECKPOINT_RUN_ID,RELEASE,refs), 'scopes':scopes}


def require_approval(prepared, plan, approved):
    if (approved != prepared['plan_sha256'] or plan['image'].get('recipe_sha256') != prepared['recipe_sha256']):
        raise ValueError('Exact approved plan and current reviewed code recipe required')


def selected_env(path):
    values = {}
    with Path(path).open() as source:
        for line in source:
            match = re.match(r'^\s*(?:export\s+)?([A-Za-z_][A-Za-z0-9_]*)\s*=\s*(.*)$', line)
            if not match or match[1] not in ENV_NAMES:
                continue
            if match[1] in values:
                raise ValueError('Duplicate selected local configuration')
            parts = shlex.split(match[2], comments=True)
            if len(parts) != 1 or any(c in parts[0] for c in ('$','`','\r','\n')):
                raise ValueError('Unsupported selected local configuration')
            values[match[1]] = parts[0]
    if not (ENV_NAMES - {'R2_BUCKET'}) <= set(values):
        raise ValueError('Existing local Cloudflare token, account and R2 URL required')
    if values.get('R2_BUCKET', 'coppertop') != 'coppertop':
        raise ValueError('Configured bucket differs from approved coppertop bucket')
    url = urlsplit(values['CLOUDFLARE_R2_URL'])
    if url.path not in ('', '/', '/coppertop', '/coppertop/') or url.query or url.fragment:
        raise ValueError('Existing R2 URL route differs')
    values['CLOUDFLARE_R2_URL'] = url.scheme + '://' + url.netloc
    host, account = _endpoint(values['CLOUDFLARE_R2_URL'])
    if values['CLOUDFLARE_ACCOUNT_ID'].lower() != account:
        raise ValueError('Existing R2 account origin differs')
    return values


def parent_credentials(values):
    """Only verify the existing token; never create or alter credentials."""
    class NoRedirect(HTTPRedirectHandler):
        def redirect_request(self, *args, **kwargs):
            raise ValueError('Token verification redirect rejected')
    opener = build_opener(NoRedirect)
    token = values['CLOUDFLARE_API_TOKEN']
    for path in ('user/tokens/verify', 'accounts/'+values['CLOUDFLARE_ACCOUNT_ID']+'/tokens/verify'):
        request = Request('https://api.cloudflare.com/client/v4/'+path,
                          headers={'Authorization':'Bearer '+token})
        try:
            with opener.open(request, timeout=30) as response:
                raw = response.read(65537)
                if len(raw)>65536:
                    raise ValueError('Verification response exceeds bound')
                payload = json.loads(raw)
        except Exception:
            continue
        result = payload.get('result', {})
        if (payload.get('success') is True and result.get('status') == 'active'
            and re.fullmatch(r'[0-9a-fA-F]{32}', result.get('id',''))):
            return result['id'], hashlib.sha256(token.encode()).hexdigest()
    raise ValueError('Existing parent token verification failed')


def cli_json(args, runner=subprocess.run, limit=2*1024**2):
    process = runner(args, capture_output=True, timeout=60)
    if process.returncode != 0 or len(process.stdout)>limit:
        raise RuntimeError('Bounded provider metadata command failed')
    return json.loads(process.stdout)


def reconcile(ledger, rows, *, app_id=None, reservation_key=None, additional_cap=8):
    additional_cap = Decimal(str(additional_cap))
    if additional_cap not in (Decimal('0.10'), 8, 10):
        raise ValueError('Reviewed full reservation required')
    if not isinstance(rows,list) or len(rows)>4096:
        raise ValueError('Bounded billing rows required')
    result = copy.deepcopy(ledger)
    historical_total(result)
    totals = {}
    for row in rows:
        if not isinstance(row,dict) or set(row) != {'object_id','description','environment','interval_start','cost'}:
            raise ValueError('Pinned Modal billing JSON schema required')
        identity = row['object_id']
        description = row['description']
        cost = Decimal(str(row['cost']))
        if not cost.is_finite() or cost<0:
            raise ValueError('Invalid billing cost')
        relevant = identity in result['apps'] or identity == app_id or description.startswith(('coppertop','xr1'))
        if relevant:
            if row['environment'] != 'main' or not re.fullmatch(r'ap-[A-Za-z0-9]+',identity):
                raise ValueError('Relevant billing identity scope differs')
            if identity not in result['apps'] and identity != app_id:
                raise ValueError('Unaccounted relevant historical spend')
            totals[identity] = totals.get(identity,Decimal(0))+cost
    for identity,row in result['apps'].items():
        row['known_reported_usd'] = str(max(Decimal(str(row['known_reported_usd'])),totals.get(identity,Decimal(0))))
        if row['state']=='unsettled_bounded' and Decimal(row['known_reported_usd'])>Decimal(str(row['reservation_usd'])):
            raise ValueError('Prior app exceeds retained hold')
    if reservation_key is not None:
        hold=result['apps'].get(reservation_key)
        if not hold or hold['state']!='unsettled_bounded' or Decimal(str(hold.get('reservation_usd',0)))!=additional_cap:
            raise ValueError('Current full reservation missing')
    additional=0 if reservation_key is not None else additional_cap
    cap = Decimal(str(result.get('cumulative_cap', '30')))
    approved_caps = (30, 38) if additional_cap == 8 else (48,)
    if cap not in approved_caps or historical_total(result)+additional>cap or totals.get(app_id,Decimal(0))>additional_cap:
        raise ValueError('Fresh billing exceeds full approved holds')
    return result, str(totals.get(app_id,Decimal(0)))


def refresh_billing(ledger, output, *, app_id=None, reservation_key=None, runner=subprocess.run, now=None, additional_cap=8):
    now = now or datetime.now(timezone.utc)
    if now.tzinfo is None:
        raise ValueError('UTC billing clock required')
    rows = cli_json([sys.executable,'-m','modal','billing','report','--start',ledger['billing_start_utc'],
        '--end',(now+timedelta(days=1)).date().isoformat(),'--resolution','h','--tz','UTC','--json'],runner)
    updated, cost = reconcile(ledger, rows, app_id=app_id,reservation_key=reservation_key,additional_cap=additional_cap)
    save(Path(output)/'billing.json', {'fetched_at_utc':now.isoformat(),'rows':rows,'current_app_reported_usd':cost})
    save(Path(output)/'ledger.json',updated)
    return updated


@contextmanager
def hard_deadline(seconds):
    """Scoped POSIX alarm covers SDK calls too; restore the caller's timer."""
    started=time.monotonic()
    old_handler=signal.getsignal(signal.SIGALRM)
    old_timer=signal.getitimer(signal.ITIMER_REAL)
    def expired(signum,frame):
        raise TimeoutError('Controller hard phase deadline reached')
    signal.signal(signal.SIGALRM,expired)
    signal.setitimer(signal.ITIMER_REAL,min(seconds,old_timer[0]) if old_timer[0] else seconds)
    try:
        yield
    finally:
        signal.setitimer(signal.ITIMER_REAL,0)
        signal.signal(signal.SIGALRM,old_handler)
        if old_timer[0]:
            remaining=old_timer[0]-(time.monotonic()-started)
            if remaining>0:
                signal.setitimer(signal.ITIMER_REAL,remaining,old_timer[1])


def delete_owned_volume(sdk, name, identity):
    """Never adopt or delete a same-name resource with a different identity."""
    if not identity:
        raise ValueError('Created Volume identity required')
    with hard_deadline(60):
        current=sdk.Volume.from_name(name,environment_name='main',create_if_missing=False)
        current.hydrate()
        if current.object_id!=identity:
            raise ValueError('Owned Volume identity changed; no deletion')
        sdk.Volume.objects.delete(name,environment_name='main',allow_missing=False)


def stop_and_audit_owned_app(app_id, *, runner=subprocess.run):
    """One exact App only; stopping is not sufficient without both empty lists."""
    if not re.fullmatch(r'ap-[A-Za-z0-9]+', app_id):
        raise ValueError('Owned App identity invalid')
    process = runner([sys.executable, '-m', 'modal', 'app', 'stop', app_id,
                      '--env', 'main', '--yes'], capture_output=True, timeout=60)
    message = process.stdout+process.stderr
    stopped = process.returncode == 0 or (len(message)<=65536 and b'App is already stopped.' in message)
    containers = cli_json([sys.executable, '-m', 'modal', 'container', 'list',
                          '--app-id', app_id, '--env', 'main', '--json'], runner, 65536)
    script = 'import modal,json,sys; print(json.dumps([s.object_id for s in modal.Sandbox.list(app_id=sys.argv[1])]))'
    sandboxes = cli_json([sys.executable, '-c', script, app_id], runner, 65536)
    if not stopped or containers != [] or sandboxes != []:
        raise RuntimeError('Owned App still has active resources; no Volume deletion')
    return {'app_stopped': True, 'active_containers': 0, 'active_sandboxes': 0}


def _run_approved(plan, prepared, ledger, env_file, output, root, *, sdk=None, runner=subprocess.run):
    """Caller already checked approval. No retraining, fallback or repeated attempt."""
    if sdk is None:
        import modal as sdk
    if sdk.__version__!='1.6.1':
        raise ValueError('Modal SDK 1.6.1 required')
    values=selected_env(env_file)
    access,secret=parent_credentials(values)
    endpoint=values['CLOUDFLARE_R2_URL']
    sessions={scope.role:[sdk.Secret.from_dict(mint(access,secret,endpoint=endpoint,scope=scope).environment())]
              for scope in prepared['scopes']}
    del access,secret,values
    # A seed lookup is allowed only in this approved lane. No model fallback.
    with hard_deadline(60):
        seed=sdk.Image.from_id(SEED_IMAGE_ID)
    image=prepare_image(sdk,seed=seed,seed_id=SEED_IMAGE_ID,root=Path(root),region='us')
    if image.recipe_sha256!=prepared['recipe_sha256']:
        raise ValueError('Reviewed source changed before allocation')
    name=plan['storage']['input_volume']
    volume=sdk.Volume.from_name(name,environment_name='main',create_if_missing=False)
    factory=R2StoreFactory(endpoint,prepared['scopes'])
    functions=create_reload_functions(sdk,volume=volume,image=image.image,store_factory=factory,
        transfer_secrets=sessions,source=prepared['source'],checkpoint=prepared['checkpoint'],
        checkpoint_key=RECEIPT,original_container_id=ORIGINAL_CONTAINER_ID,
        attempt_id=plan['attempt_id'],region='us')
    gate=create_image_gate(functions.app,prepared=image,input_volume=volume,run_id=CHECKPOINT_RUN_ID,
                           attempt_id=plan['attempt_id'],region='us')
    owned={'attempted':False,'created':False,'volume_id':None,'context':None,'entered':False}
    def ownership():
        save(Path(output)/'resource-ownership.json', {'volume_name':name,'environment':'main',
            'allocation_attempted':owned['attempted'],'creation_confirmed':owned['created'],
            'volume_id':owned['volume_id'],'orphan_possible':owned['attempted'] and not owned['volume_id']})
    current_ledger=ledger
    pending_key='pending-'+plan['attempt_id']
    def admission():
        nonlocal current_ledger
        current_ledger=refresh_billing(current_ledger,output,reservation_key=pending_key,runner=runner)
        save(Path(output)/'reservation.json',{'additional_usd':'8','overall_usd':str(plan['cumulative_cap']),'released_usd':'0',
            'plan_sha256':prepared['plan_sha256'],'recipe_sha256':prepared['recipe_sha256'],
            'prior_holds_usd':prepared['historical_conservative_usd']})
        with hard_deadline(60):
            owned['attempted']=True
            ownership()
            sdk.Volume.objects.create(name,environment_name='main',allow_existing=False)
            owned['created']=True
            ownership()
            volume.hydrate()
            owned['volume_id']=volume.object_id
            ownership()
        owned['context']=functions.app.run(name=plan['app_name'],environment_name='main')
        with hard_deadline(900):
            owned['context'].__enter__()
        owned['entered']=True
        bind_app_hold()
    def bind_app_hold():
        identity=functions.app.app_id
        if identity and pending_key in current_ledger['apps']:
            if not re.fullmatch(r'ap-[A-Za-z0-9]+',identity) or identity in current_ledger['apps']:
                raise ValueError('New owned App identity collides with existing ledger')
            current_ledger['apps'][identity]=current_ledger['apps'].pop(pending_key)
            current_ledger['apps'][identity]['description']='coppertop-reload-only'
            save(Path(output)/'ledger.json',current_ledger)
    def current_key():
        return functions.app.app_id if functions.app.app_id in current_ledger['apps'] else pending_key
    def stopped(identity):
        return stop_verifier(functions.app.app_id,environment='main',
                             command=(sys.executable,'-m','modal'),runner=runner)(identity)
    def invoke(function,args,timeout):
        nonlocal current_ledger
        if function is functions.verify:
            current_ledger=refresh_billing(current_ledger,output,app_id=functions.app.app_id,reservation_key=current_key(),runner=runner)
            if Decimal(current_ledger['apps'][functions.app.app_id]['known_reported_usd']) > Decimal('.75'):
                raise ValueError('CPU reported cost exceeds bounded pre-GPU envelope')
        return function.spawn(*args).get(timeout=timeout)
    def image_gate():
        return gate.spawn(prepared['source'].to_dict(),image.image.object_id).get(timeout=1560)
    def cleanup(success):
        nonlocal current_ledger
        bind_app_hold()
        app_id=functions.app.app_id
        context_error=None
        if owned['context'] and owned['entered']:
            try:
                with hard_deadline(60):
                    owned['context'].__exit__(None,None,None)
            except BaseException as error:
                context_error=type(error).__name__
            owned['entered']=False
        if app_id:
            stop_and_audit_owned_app(app_id, runner=runner)
        if owned['created']:
            if not owned['volume_id']:
                with hard_deadline(60):
                    rebound=sdk.Volume.from_name(name,environment_name='main',create_if_missing=False)
                    rebound.hydrate()
                    owned['volume_id']=rebound.object_id
                    ownership()
            delete_owned_volume(sdk,name,owned['volume_id'])
        elif owned['attempted']:
            raise RuntimeError('Volume creation outcome unconfirmed; resource ownership requires audit')
        current_ledger=refresh_billing(current_ledger,output,app_id=app_id,reservation_key=current_key(),runner=runner)
        return {'active_containers':0,'active_sandboxes':0,'app_stopped':True,
                'volume_deleted':True,'created_volume_deleted':owned['created'],'holds_released':0,
                'context_exit_error_type':context_error}
    return execute_reload(functions,Path(output),admission=admission,image_gate=image_gate,
                          stop_verify=stopped,cleanup=cleanup,invoke=invoke,reservation=read_json(Path(output)/'consumed.json'))


def run_approved(plan, prepared, ledger, env_file, output, root, *, sdk=None, runner=subprocess.run):
    """Consume the authorization before credential reads, signing or provider access."""
    output = Path(output).resolve()
    expected = Path(root).resolve()/'.local'/'reload-only'/plan['attempt_id']
    if output != expected or any(path.is_symlink() for path in (Path(root)/'.local', expected.parent, expected)):
        raise ValueError('Dedicated ignored attempt directory required')
    output.mkdir(parents=True, exist_ok=True)
    with (output/'consumed.json').open('x') as marker:
        marker.write(json.dumps({'run_id':CHECKPOINT_RUN_ID,'checkpoint_key':RECEIPT,
            'plan_sha256':prepared['plan_sha256'],
            'recipe_sha256':prepared['recipe_sha256'],'automatic_retries':0}))
    try:
        with hard_deadline(7200):
            refreshed=refresh_billing(ledger,output,runner=runner)
            pending='pending-'+plan['attempt_id']
            if pending in refreshed['apps']:
                raise ValueError('Attempt already holds a reservation')
            refreshed['apps'][pending]={'state':'unsettled_bounded','known_reported_usd':'0',
                'reservation_usd':'8','description':'coppertop-reload-only',
                'acceptance_basis':'Full approved reload hold; no automatic release even if allocation fails'}
            save(output/'ledger.json',refreshed)
            save(output/'reservation.json',{'additional_usd':'8','overall_usd':str(plan['cumulative_cap']),'released_usd':'0',
                'plan_sha256':prepared['plan_sha256'],'recipe_sha256':prepared['recipe_sha256'],
                'prior_holds_usd':prepared['historical_conservative_usd']})
            return _run_approved(plan,prepared,refreshed,env_file,output,root,sdk=sdk,runner=runner)
    except BaseException as error:
        save(output/'launch-failure.json', {'status':'failed','error_type':type(error).__name__,
            'consumed':True,'automatic_retries':0})
        raise


def main(argv=None):
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--plan',type=Path,required=True)
    parser.add_argument('--ledger',type=Path,required=True)
    parser.add_argument('--env-file',type=Path,default=Path.home()/'code/coppertop/.env.local')
    parser.add_argument('--output',type=Path,help='Must be project/.local/reload-only/<approved-attempt-id>')
    parser.add_argument('--approved-plan-sha256')
    mode=parser.add_mutually_exclusive_group()
    mode.add_argument('--prepare',action='store_true')
    mode.add_argument('--execute',action='store_true')
    args=parser.parse_args(argv)
    root=Path(__file__).resolve().parents[1]
    try:
        plan=read_json(args.plan)
        ledger=read_json(args.ledger)
        prepared=prepare(plan,ledger,root)
        public={key:value for key,value in prepared.items() if key not in ('source','checkpoint','scopes')}
        if not args.execute:
            print(json.dumps(public,sort_keys=True))
            return 0
        require_approval(prepared,plan,args.approved_plan_sha256)
        output=args.output or root/'.local'/'reload-only'/plan['attempt_id']
        result=run_approved(plan,prepared,ledger,args.env_file,output,root)
        print(json.dumps({'status':result['status'],'output':str(output)}))
        return 0
    except Exception as error:
        # Provider exceptions can include URLs, headers or request bodies.
        detail = type(error).__name__ if args.execute else type(error).__name__+': '+str(error)
        print('Reload-only blocked: '+detail,file=sys.stderr)
        return 1


if __name__=='__main__':
    raise SystemExit(main())
