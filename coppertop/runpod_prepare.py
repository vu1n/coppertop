"""One approval-bound CPU preparation; official SDKs, no registry or GPU resources."""
import argparse
from datetime import datetime, timezone
from decimal import Decimal
import hashlib
import io
import json
import os
from pathlib import Path
import shutil
import subprocess
import tarfile
import time

from .chords import canonical, digest
from .reload_launch import hard_deadline, read_json, stop_and_audit_owned_app
from .runpod_bundle import BOUNDS, GIB, validate_descriptor
from .runpod_cloud import RestAPI, amount, identifier, save
from .runpod_relay import Relay, copy_shards, feasibility
from .runpod_runtime import contract
from .runpod_staging import RunpodVolumeConfig
from .streams import file_ref

ROOT=Path(__file__).resolve().parents[1]
LIMITS={'overall_seconds':21600,'source_seconds':7200,'relay_seconds':7200,'CPU_seconds':7200,
    'export_seconds':3600,'CPU_work_seconds':6300,'cleanup_seconds':300,'rootfs_bytes':70*GIB,
    'export_bytes':50*GIB,'volume_gb':60,'container_gb':100,'local_free_bytes':64*GIB,
    'source_spool_bytes':BOUNDS['spool'],'source_spool_parts':800,'source_metadata_commands':2048,
    'source_free_bytes':2*BOUNDS['debs']+2*BOUNDS['code']+BOUNDS['manifest']+BOUNDS['spool']+8*1024**2+2*GIB,
    'retention_days':14,'automatic_retries':0}

# The tiny public start command authenticates the code archive before importing
# it. No shell, URL fetch, provider key, tar.extractall or network listener.
CONTROL_START=r"""import hashlib,os,pathlib,tarfile,subprocess
p=pathlib.Path(os.environ['COPPERTOP_CACHE_BUNDLE'])/'control.tar'
assert p.is_file() and not p.is_symlink() and p.stat().st_size<=16777216
assert hashlib.sha256(p.read_bytes()).hexdigest()==os.environ['COPPERTOP_CONTROL_SHA256']
d=pathlib.Path('/tmp/coppertop-control');d.mkdir()
with tarfile.open(p) as a:
 for m in a:
  parts=m.name.split('/')
  assert m.isreg() and all(x not in ('','.','..') for x in parts) and not m.name.startswith('/') and '\\' not in m.name
  q=d/m.name;q.parent.mkdir(parents=True,exist_ok=True)
  with q.open('xb') as out:out.write(a.extractfile(m).read(16777217))
os.environ['PYTHONPATH']=str(d)
subprocess.run(['/opt/conda/bin/python','-m','coppertop.runpod_bootstrap','--bundle',os.environ['COPPERTOP_CACHE_BUNDLE']],check=True,env=os.environ)
if os.environ.get('COPPERTOP_BOOTSTRAP_MODE')=='rollout':
 subprocess.run(['/opt/conda/bin/python','-m','coppertop.runpod_worker'],check=True,env=os.environ)
"""


def control_files():
    files=[*sorted((ROOT/'coppertop').glob('*.py')),*sorted((ROOT/'tools').glob('*.py')),
        *sorted((ROOT/'containers').glob('*.json')),
        ROOT/'requirements-gpu-smoke.lock',ROOT/'requirements-simulator.lock',ROOT/'requirements-r2.txt']
    if sum(p.stat().st_size for p in files)>BOUNDS['code']:raise ValueError('Code package bound')
    return files


def pack_control(output):
    output=Path(output)
    with output.open('xb') as stream,tarfile.open(fileobj=stream,mode='w',format=tarfile.PAX_FORMAT) as archive:
        for path in control_files():
            if path.is_symlink():raise ValueError('Code link rejected')
            data=path.read_bytes();member=tarfile.TarInfo(path.relative_to(ROOT).as_posix());member.size=len(data);member.mode=0o644
            archive.addfile(member,io.BytesIO(data))
    if output.stat().st_size>BOUNDS['code']:raise ValueError('Control tar bound')
    return file_ref(output).to_dict()


def verify_control(path,expected):
    from .runpod_bundle import safe_name
    path=Path(path)
    if path.is_symlink() or path.stat().st_size>BOUNDS['code']:raise ValueError('Bounded ordinary control archive required')
    seen=set()
    with tarfile.open(path) as archive:
        for member in archive:
            name=safe_name(member.name)
            if not member.isreg() or name in seen or name not in expected or member.size!=expected[name]['size']:
                raise ValueError('Code archive membership/type/size differs from approved plan')
            raw=archive.extractfile(member).read(BOUNDS['code']+1)
            if len(raw)!=member.size or digest(raw)!=expected[name]['sha256']:raise ValueError('Code archive hash differs from approved plan')
            seen.add(name)
    if seen!=set(expected):raise ValueError('Code archive incomplete')
    return file_ref(path).to_dict()


def validate_quote(value,*,now=None):
    now=now or datetime.now(timezone.utc)
    if not isinstance(value,dict) or set(value)!={'type_id','flavor_id','vcpus','ram_gib','hour_usd','datacenter','availability','checked_utc'}:
        raise ValueError('Exact real CPU quote required')
    identifier(value['type_id']);age=(now-datetime.fromisoformat(value['checked_utc'])).total_seconds()
    if (value['flavor_id'] not in ('cpu3c','cpu3g','cpu3m','cpu5c','cpu5g','cpu5m')
        or not (value['type_id']==value['flavor_id'] or value['type_id'].startswith(value['flavor_id']+'-'))
        or value['vcpus'] not in (4,8) or not Decimal(16)<=amount(value['ram_gib'])<=Decimal(32)
        or value['datacenter']!='US-WA-1' or value['availability'] not in ('LOW','MEDIUM','HIGH')
        or not 0<=age<=900 or not Decimal(0)<amount(value['hour_usd'])<=Decimal('.50')):
        raise ValueError('CPU quote exceeds scope, expired or unavailable')
    return value


def prepare(config):
    if set(config)!={'attempt','CPU_quote','base_metadata','package_admission','cleanup_owner'}:
        raise ValueError('Exact public preparation config required')
    identifier(config['attempt'])
    if not isinstance(config['cleanup_owner'],str) or not 1<=len(config['cleanup_owner'])<=128:raise ValueError('Named cleanup owner required')
    gates=[]
    if config['CPU_quote'] is None:gates.append('Exact current CPU flavor/RAM/price/availability absent;403 is not bypassed')
    else:validate_quote(config['CPU_quote'])
    base=config['base_metadata']
    if base is None:gates.append('Public-base platform/compressed layer bytes not verified')
    elif base.get('image')!=contract()['base'] or base.get('platform')!='linux/amd64' or not 0<base.get('compressed_bytes',0)<=20*GIB:
        raise ValueError('Pinned public base metadata differs/exceeds20GiB')
    closure=config['package_admission']
    if closure is None:gates.append('Signed exact offline Ubuntu closure missing, including libopengl0=1.4.0-1')
    elif (closure.get('offline_bytes_hashes_locked') is not True or closure.get('signature_verification_stage')!='source CPU before export' or closure.get('architecture')!='amd64'
          or closure.get('release')!='jammy' or type(closure.get('bytes')) is not int
          or not 0<closure['bytes']<=2*GIB):raise ValueError('Verified bounded offline closure required')
    if closure is not None:
        from .streams import ObjectRef
        ObjectRef(closure['lock_sha256'],0)
    p={'schema_version':1,'kind':'runpod_cpu_preparation','config':config,'limits':LIMITS,
        'base_image':contract()['base'],'source_image':contract()['validated_seed'],
        'code':{p.relative_to(ROOT).as_posix():file_ref(p).to_dict() for p in control_files()},
        'runtime_contract_sha256':digest(canonical(contract())),
        'additional_cap_usd':'12.00','cost_bound_usd':'9.768056','financial_reservation_released':False,
        'pod_name':'coppertop-'+config['attempt']+'-cpu','volume_name':'coppertop-'+config['attempt']+'-cache',
        'paid_authorized':False,'ready_for_approval':not gates,'gates':gates,
        'GPU':False,'registry':False,'Mac_online_required':True,'cleanup_scheduler':False}
    return {'plan':p,'plan_sha256':digest(canonical(p))}


def require_approval(prepared,approval):
    if prepare(prepared['plan']['config'])!=prepared:raise ValueError('Preparation code/config changed')
    if not prepared['plan']['ready_for_approval']:raise ValueError('Missing CPU/package/base admission; paid step blocked before credentials')
    if (approval.get('plan_sha256')!=prepared['plan_sha256'] or approval.get('additional_cap_usd')!='12.00'
        or any(approval.get(k) is not True for k in ('execute_CPU_preparation','transfer_frozen_bytes',
            'create_one_volume_and_CPU_Pod','retain_up_to14days_then_delete_owned_volume','accept_failure_storage_until_cleanup'))):
        raise ValueError('Fresh concrete CPU preparation, transfer and retention approval required')


def cpu_payload(prepared,volume,control_ref,*,quote=None):
    p=prepared['plan'];q=validate_quote(quote or p['config']['CPU_quote'])
    return {'name':p['pod_name'],'imageName':p['base_image'],'computeType':'CPU','cloudType':'SECURE',
        'cpuFlavorIds':[q['flavor_id']],'vcpuCount':q['vcpus'],'dataCenterIds':['US-WA-1'],
        'networkVolumeId':identifier(volume),'volumeMountPath':'/workspace','containerDiskInGb':100,'volumeInGb':0,
        'interruptible':False,'ports':[],'supportPublicIp':False,'globalNetworking':False,'dockerEntrypoint':[],
        'dockerStartCmd':['/opt/conda/bin/python','-c',CONTROL_START],
        'env':{'COPPERTOP_PLAN_SHA256':prepared['plan_sha256'],
               'COPPERTOP_CACHE_BUNDLE':'/workspace/coppertop/cache/'+p['config']['attempt']+'/bundle',
               'COPPERTOP_CONTROL_SHA256':control_ref['sha256']}}


def owns_volume(value,prepared):
    return value.get('name')==prepared['plan']['volume_name'] and value.get('size')==60 and value.get('dataCenterId')=='US-WA-1'


def validate_cache(cache):
    fields={'attempt','plan_sha256','volume_id','volume_name','allocated_gb','datacenter','cleanup_owner',
        'manifest_sha256','tar_sha256','CPU_receipt_sha256','control_sha256','retention_until_utc'}
    if set(cache)!=fields or cache['allocated_gb']!=60 or cache['datacenter']!='US-WA-1':raise ValueError('Exact owned cache receipt required')
    for name in ('attempt','volume_id','volume_name'):identifier(cache[name])
    for name in ('plan_sha256','manifest_sha256','tar_sha256','CPU_receipt_sha256','control_sha256'):
        from .streams import ObjectRef
        ObjectRef(cache[name],0)
    remaining=cache['retention_until_utc']-datetime.now(timezone.utc).timestamp()
    if not 0<remaining<=14*86400:raise ValueError('Prepared cache retention expired/invalid')
    return cache


def stop_CPU(api,state,prepared,journal):
    """Reconcile lost create once, then delete exact Pod and observe absence."""
    with hard_deadline(300):
        if state.get('pending')=='pods' and not state.get('pod_id'):
            values=api.request('GET','pods')
            matches=[v for v in values if v.get('name')==prepared['plan']['pod_name'] and v.get('env',{}).get('COPPERTOP_PLAN_SHA256')==prepared['plan_sha256'] and (v.get('networkVolumeId') or v.get('networkVolume',{}).get('id'))==state.get('volume_id')]
            if len(matches)!=1:raise ValueError('CPU creation unresolved; no replacement or volume deletion')
            state['pod_id']=identifier(matches[0]['id']);save(journal,state)
        pod=state.get('pod_id')
        if pod:
            value=api.request('GET','pods/'+pod)
            if value:
                if (value.get('name')!=prepared['plan']['pod_name'] or value.get('imageName',value.get('image'))!=prepared['plan']['base_image']
                    or value.get('env',{}).get('COPPERTOP_PLAN_SHA256')!=prepared['plan_sha256'] or (value.get('networkVolumeId') or value.get('networkVolume',{}).get('id'))!=state['volume_id']):
                    raise ValueError('CPU Pod ownership changed')
                api.request('DELETE','pods/'+pod)
            while api.request('GET','pods/'+pod) is not None:time.sleep(1)
        state['pod_absent']=True;save(journal,state)
        if state.get('pending')=='pods':state['pending']=None;save(journal,state)


def execute(prepared,approval,output,*,api,source,transport,debs,control):
    """Injected real adapters; local tests replace them. No allocation until gates."""
    require_approval(prepared,approval);p=prepared['plan'];output=Path(output)
    source.validate_inputs(prepared,debs,control)
    output.mkdir(parents=True,exist_ok=True);journal=output/'state.json'
    with journal.open('xb') as out:out.write(canonical({'consumed':True,'plan_sha256':prepared['plan_sha256']}));out.flush();os.fsync(out.fileno())
    state={'consumed':True,'prepared':prepared,'status':'failed','holds_released':False,'pending':None}
    save(journal,state) # Recoverable scope is durable before source/provider work.
    try:
        with hard_deadline(21600):
            quote=api.cpu_quote(p['config']['CPU_quote'])
            validate_quote(quote)
            if any(quote[k]!=p['config']['CPU_quote'][k] for k in ('type_id','flavor_id','vcpus','datacenter')) or amount(quote['hour_usd'])>amount(p['config']['CPU_quote']['hour_usd']):
                raise ValueError('Current CPU quote differs/increased')
            # Complete local copy and stop the source before renting RunPod storage/CPU.
            bundle=source.collect(prepared,output/'bundle',debs=debs,control=control)
            source.close();state['source_stopped']=True;save(journal,state)
            quote=api.cpu_quote(p['config']['CPU_quote']);validate_quote(quote)
            if amount(quote['hour_usd'])>amount(p['config']['CPU_quote']['hour_usd']):raise ValueError('CPU rate increased after export; no volume allocation')
            for kind,name in (('networkvolumes',p['volume_name']),('pods',p['pod_name'])):
                if any(v.get('name')==name for v in api.request('GET',kind)):raise ValueError('Resource name collision; explicit recovery only')
            state['pending']='networkvolumes';save(journal,state)
            value=api.request('POST','networkvolumes',{'name':p['volume_name'],'size':60,'dataCenterId':'US-WA-1'})
            state['volume_id']=identifier(value['id']);state['pending']=None;save(journal,state)
            owner={'attempt':p['config']['attempt'],'plan_sha256':prepared['plan_sha256'],'volume_id':state['volume_id'],
                'volume_name':p['volume_name'],'allocated_gb':60,'datacenter':'US-WA-1','cleanup_owner':p['config']['cleanup_owner']}
            relay=transport(owner,output/'relay-state.json',False)
            # Real upload/readback probes after allocation, before any bulk upload.
            samples=transport.probe(relay)
            ready=relay.stage(bundle,control,samples=samples);state['staged']=ready;save(journal,state)
            quote=api.cpu_quote(p['config']['CPU_quote']);validate_quote(quote)
            if amount(quote['hour_usd'])>amount(p['config']['CPU_quote']['hour_usd']):raise ValueError('CPU rate increased after staging; keep cache, no compute allocation')
            with hard_deadline(6900):
                state['pending']='pods';save(journal,state)
                value=api.request('POST','pods',cpu_payload(prepared,state['volume_id'],file_ref(control).to_dict(),quote=quote))
                state['pod_id']=identifier(value['id']);state['pending']=None;save(journal,state)
                if amount(str(value['costPerHr']))>amount(quote['hour_usd']):raise ValueError('Allocated CPU price increased')
                if (value.get('cpuFlavorId')!=quote['flavor_id'] or value.get('vcpuCount')!=quote['vcpus']
                    or not Decimal(16)<=Decimal(str(value.get('memoryInGb',0)))*10**9/2**30<=Decimal(32)):
                    raise ValueError('Allocated CPU flavor/vCPU/RAM differs; stop rather than substitute')
                receipt=transport.wait_CPU(relay,prepared,state['pod_id'],6300)
                if (receipt.get('status')!='passed' or receipt.get('manifest_sha256')!=ready['bundle']['manifest_sha256']
                    or receipt.get('tar_sha256')!=ready['bundle']['tar_sha256'] or receipt.get('extracted_on_network_volume') is not False):
                    raise ValueError('Restored CPU receipt differs')
                state['CPU_receipt']=receipt;save(output/'cpu-receipt.json',receipt);state['status']='passed'
                state['cache']=dict(owner,manifest_sha256=receipt['manifest_sha256'],tar_sha256=receipt['tar_sha256'],
                    CPU_receipt_sha256=digest(canonical(receipt)),control_sha256=file_ref(control).sha256,
                    retention_until_utc=(datetime.now(timezone.utc).timestamp()+14*86400))
                save(output/'owned-cache.json',state['cache'])
    except BaseException as error:state['error_type']=type(error).__name__
    finally:
        try:source.close();state['source_stopped']=True
        except BaseException as error:state['source_cleanup_error']=type(error).__name__
        try:stop_CPU(api,state,prepared,journal)
        except BaseException as error:state['CPU_cleanup_error']=type(error).__name__
        # Failed staging/source is kept; cleanup never discards the only bytes.
        state['local_source_preserved']=True;state['volume_retained']=bool(state.get('volume_id') or state.get('pending')=='networkvolumes')
        state['ongoing_volume_month_usd']='4.20' if state['volume_retained'] else '0'
        if not state.get('pod_absent') or not state.get('source_stopped'):state['status']='failed'
        save(journal,state)
    return state


def delete_cache(api,relay,state,*,approved_plan_sha256,surviving_source_verified):
    """Explicit exact-volume deletion; Pod absent and another source verified first."""
    prepared=state['prepared']
    if (approved_plan_sha256!=prepared['plan_sha256'] or surviving_source_verified is not True
        or state.get('pod_absent') is not True or state.get('pending')):raise ValueError('Exact cleanup approval and surviving source required')
    value=api.request('GET','networkvolumes/'+state['volume_id'])
    if value:
        if not owns_volume(value,prepared):raise ValueError('Owned volume identity changed')
        if any((p.get('networkVolumeId') or p.get('networkVolume',{}).get('id'))==state['volume_id'] for p in api.request('GET','pods')):
            raise ValueError('Owned cache still attached to a Pod; no deletion')
        relay.abort_owned();api.request('DELETE','networkvolumes/'+state['volume_id'])
    with hard_deadline(300):
        while api.request('GET','networkvolumes/'+state['volume_id']) is not None:time.sleep(1)
    return {'volume_absent':True,'local_source_preserved':True,'holds_released':False}


def recover_source(state,journal,*,stopper=None):
    """Stop only the source App bound to this attempt's durable creation journal."""
    if state.get('source_stopped'):return
    source_path=Path(journal).parent/'bundle/source-state.json'
    if not source_path.exists():return # Source work was never admitted/started.
    if source_path.is_symlink():raise ValueError('Ordinary source ownership journal required')
    value=read_json(source_path,4096);prepared=state['prepared']
    expected='coppertop-'+prepared['plan']['config']['attempt']+'-export'
    if value.get('plan_sha256')!=prepared['plan_sha256'] or value.get('app_name')!=expected:
        raise ValueError('Source App ownership differs; no adoption')
    if not value.get('app_id'):
        raise ValueError('Source App creation ID unresolved; exact named operator reconciliation required')
    with hard_deadline(180):
        state['source_cleanup']= (stopper or stop_and_audit_owned_app)(value['app_id'])
    state['source_stopped']=True;save(journal,state)


def recover(api,state,journal,*,approved_plan_sha256):
    """One bounded ownership reconciliation, no replacement resources or relaunch."""
    p=state['prepared'];plan=p['plan']
    if approved_plan_sha256!=p['plan_sha256'] or digest(canonical(plan))!=approved_plan_sha256:
        raise ValueError('Exact recovery approval required before provider reads')
    source_error=None
    try:recover_source(state,journal)
    except BaseException as error:
        source_error=error;state['source_cleanup_error']=type(error).__name__;save(journal,state)
    with hard_deadline(300):
        if state.get('pending')=='networkvolumes' and not state.get('volume_id'):
            values=api.request('GET','networkvolumes');matches=[v for v in values if owns_volume(v,p)]
            if len(matches)!=1:raise ValueError('Volume creation unresolved; no new volume or wider adoption')
            state['volume_id']=identifier(matches[0]['id']);state['pending']=None;save(journal,state)
        stop_CPU(api,state,p,journal)
    if source_error:raise RuntimeError('Source cleanup remains unresolved; preserved scope requires operator reconciliation') from None
    return state


def local_bundle_verified(directory,descriptor):
    from .runpod_bundle import validate_manifest
    from .streams import ObjectRef
    directory=Path(directory);actual=validate_descriptor(read_json(directory/'bundle.json',65536))
    if actual!=descriptor:raise ValueError('Surviving bundle identity differs')
    value=validate_manifest(read_json(directory/'manifest.json',BOUNDS['manifest']))
    if digest(canonical(value))!=actual['manifest_sha256']:raise ValueError('Surviving manifest differs')
    for ref in actual['shards']:
        path=directory/ref['name']
        if path.is_symlink() or file_ref(path)!=ObjectRef(ref['sha256'],ref['size']):raise ValueError('Surviving source checksum differs')
    return True


def main():
    parser=argparse.ArgumentParser(description=__doc__);parser.add_argument('--config',type=Path)
    parser.add_argument('--output',type=Path);parser.add_argument('--execute',action='store_true')
    parser.add_argument('--approval',type=Path);parser.add_argument('--debs',type=Path)
    parser.add_argument('--recover',type=Path);parser.add_argument('--approved-plan-sha256')
    parser.add_argument('--abort-uploads',action='store_true');parser.add_argument('--resume-staging',action='store_true')
    parser.add_argument('--delete-cache',action='store_true')
    args=parser.parse_args()
    if args.recover:
        state=read_json(args.recover,1024**2);p=state['prepared']
        if args.approved_plan_sha256!=p['plan_sha256'] or digest(canonical(p['plan']))!=p['plan_sha256']:
            parser.error('Exact recovery approval required before credential access')
        if sum((args.abort_uploads,args.resume_staging,args.delete_cache))>1:parser.error('Resume, abort and destructive deletion are separate approvals')
        from .runpod_source import LiveTransport
        api=RestAPI(os.environ['RUNPOD_API_KEY']);recover(api,state,args.recover,approved_plan_sha256=args.approved_plan_sha256)
        if args.abort_uploads or args.resume_staging or args.delete_cache:
            plan=p['plan'];owner={'attempt':plan['config']['attempt'],'plan_sha256':p['plan_sha256'],'volume_id':state['volume_id'],
                'volume_name':plan['volume_name'],'allocated_gb':60,'datacenter':'US-WA-1','cleanup_owner':plan['config']['cleanup_owner']}
            relay_path=args.recover.parent/'relay-state.json'
            if not relay_path.exists() and not args.delete_cache:parser.error('Durable transfer journal required; no inferred resume')
            relay=LiveTransport()(owner,relay_path,relay_path.exists())
            if args.abort_uploads:relay.abort_owned()
            if args.resume_staging:
                relay.reconcile_lost_create()
                state['staged']=relay.stage(args.recover.parent/'bundle',args.recover.parent/'control.tar',samples=relay.state.get('probe_samples',[]))
                state['status']='staged_CPU_not_launched';save(args.recover,state)
                # No automatic CPU retry or quote/financial extension on recovery.
            if args.delete_cache:
                descriptor=state.get('staged',{}).get('bundle') or read_json(args.recover.parent/'bundle/bundle.json',65536)
                with hard_deadline(300):
                    surviving=local_bundle_verified(args.recover.parent/'bundle',descriptor)
                    response=relay.client.client.list_objects_v2(Bucket=state['volume_id'],Prefix='coppertop/runs/',MaxKeys=1)
                    if response.get('Contents') or response.get('IsTruncated'):parser.error('GPU evidence exists; preserve it and use separately approved full evidence cleanup')
                    state['cleanup']=delete_cache(api,relay,state,approved_plan_sha256=args.approved_plan_sha256,surviving_source_verified=surviving)
                    state['volume_retained']=False;save(args.recover,state)
        return
    if not args.config:parser.error('--config required for preparation')
    if not args.output:parser.error('--output required for preparation')
    if args.abort_uploads or args.resume_staging or args.delete_cache:parser.error('--recover required')
    prepared=prepare(read_json(args.config))
    if not args.execute:save(args.output,prepared);return
    require_approval(prepared,read_json(args.approval) if args.approval else {})
    from .runpod_source import ModalSource,LiveTransport
    args.output.mkdir(parents=True,exist_ok=True);control=args.output/'control.tar';pack_control(control)
    source=ModalSource()
    source.validate_inputs(prepared,args.debs,control)
    # Credentials are read only here, after all public approval gates.
    api=RestAPI(os.environ['RUNPOD_API_KEY']);transport=LiveTransport()
    result=execute(prepared,read_json(args.approval),args.output,api=api,source=source,transport=transport,debs=args.debs,control=control)
    if result['status']!='passed':raise SystemExit(1)


if __name__=='__main__':main()
