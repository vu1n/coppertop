"""Single approved RunPod rollout with journaled ownership and explicit cleanup.

Preparation is offline. REST, S3 and publication transports are injected so local
tests establish control behavior, not remote runtime availability.
"""
import argparse
from datetime import datetime, timezone
from decimal import Decimal
import json
import os
from pathlib import Path
import re
import signal
import time
from urllib.error import HTTPError
from urllib.request import Request, build_opener, HTTPRedirectHandler

from .chords import canonical, digest
from .official_recording_worker import plan as worker_plan
from .reload_launch import hard_deadline, read_json
from .runpod_staging import DATACENTERS

LIMITS = {'setup_seconds':900,'run_seconds':1200,'publish_seconds':900,
          'cleanup_seconds':180,'controller_seconds':3000,'handoff_seconds':3300,'worker_seconds':560,
          'scratch_bytes':2*1024**3,'retained_bytes':1024**3,'objects':6000,
          'single_object_bytes':64*1024**2,'network_volume_gb':3,
          'container_disk_gb':100,'retries':0}


def save(path,value):
    """Persist ownership intent across controller crashes, before remote mutations."""
    path=Path(path);path.parent.mkdir(parents=True,exist_ok=True)
    temporary=path.with_suffix(path.suffix+'.partial')
    with temporary.open('wb') as target:
        target.write(canonical(value));target.flush();os.fsync(target.fileno())
    temporary.replace(path)
    descriptor=os.open(path.parent,os.O_RDONLY)
    try: os.fsync(descriptor)
    finally: os.close(descriptor)


def identifier(value):
    if not isinstance(value,str) or not re.fullmatch(r'[A-Za-z0-9_-]{1,128}',value):
        raise ValueError('Bounded provider identity required')
    return value


def amount(value):
    if not isinstance(value,str) or not re.fullmatch(r'[0-9]+(?:\.[0-9]+)?',value):
        raise ValueError('Explicit nonnegative decimal amount required')
    return Decimal(value)


def cost_bound(prices):
    """Reservations include hourly rounding and full first storage month, no tiers."""
    # Price discovery/setup/export/build/registry are a separate approved phase.
    gpu = amount(prices['gpu_hour_usd']) # entire setup + run + cleanup, rounded to hour
    disk = amount(prices['container_gb_month_usd'])*100
    volume = amount(prices['volume_gb_month_usd'])*3
    r2 = amount(prices['r2_gb_month_usd'])*2 # 1 GiB rounds up to two billable GB
    requests = amount(prices['r2_request_reservation_usd'])
    transfer = amount(prices['transfer_reservation_usd'])
    return str(gpu+disk+volume+r2+requests+transfer)


def prepare(config):
    """Bind exact OCI image, real CPU validation receipt, prices and code to approval."""
    if set(config) != {'run_id','image','runtime_export_sha256','cpu_admission_sha256',
                        'datacenter','gpu','prices','additional_cap_usd','r2_bucket'}:
        raise ValueError('Exact RunPod configuration fields required')
    if (not re.fullmatch(r'mechanism-[0-9a-f]{16}',config['run_id'])
        or not re.fullmatch(r'[A-Za-z0-9./_-]+@sha256:[0-9a-f]{64}',config['image'])
        or config['datacenter'] not in DATACENTERS
        or config['gpu'] not in ('NVIDIA RTX 6000 Ada Generation','NVIDIA A100-SXM4-80GB')
        or any(not re.fullmatch(r'[0-9a-f]{64}',config[name]) for name in ('runtime_export_sha256','cpu_admission_sha256'))):
        raise ValueError('Immutable image, runtime receipt and supported GPU/datacenter required')
    identifier(config['r2_bucket'])
    prices=config['prices']
    if set(prices) != {'checked_utc','gpu_hour_usd','container_gb_month_usd','volume_gb_month_usd',
                       'r2_gb_month_usd','r2_request_reservation_usd','transfer_reservation_usd'}:
        raise ValueError('Complete compute/storage/publication prices required')
    checked=datetime.fromisoformat(prices['checked_utc'])
    if checked.tzinfo is None: raise ValueError('UTC quote timestamp required')
    bound=cost_bound(prices)
    if amount(config['additional_cap_usd']) < amount(bound):
        raise ValueError('Approved reservation cannot cover complete cost bound')
    worker=worker_plan(full=True)
    root=Path(__file__).resolve().parents[1]
    files=[*sorted(p.relative_to(root).as_posix() for p in (root/'coppertop').glob('*.py')),
           'containers/runpod-runtime.json','containers/Dockerfile.runpod','containers/Dockerfile.runpod.dockerignore','tools/run_cpu_image_repair.py',
           'requirements-gpu-smoke.lock','requirements-simulator.lock','requirements-r2.txt',
           'containers/gpu-smoke-pins.json']
    plan={'schema_version':1,'provider':'RunPod','kind':'official_full_rollout_recording',
          'config':config,'limits':dict(LIMITS),'worker':worker,'worker_sha256':digest(canonical(worker)),
          'runtime_sha256':{name:digest((root/name).read_bytes()) for name in files},
          'volume_name':'coppertop-'+config['run_id']+'-evidence',
          'pod_name':'coppertop-'+config['run_id'],
          'r2_prefix':'chords/runpod/'+config['run_id'], 'cost_bound_usd':bound,
          'paid_authorized':False,'setup_build_and_bulk_transfer_included':False,
          'full_publication_live_verified':False}
    return {'plan':plan,'plan_sha256':digest(canonical(plan))}


class NoRedirect(HTTPRedirectHandler):
    def redirect_request(self,*args,**kwargs): return None


class RestAPI:
    """Official REST v1; finite per-call timeout, no retries, no response/log secrets."""
    def __init__(self,token,*,opener=None):
        self.token=token
        self.opener=opener or build_opener(NoRedirect())

    def request(self,method,resource,payload=None):
        if not re.fullmatch(r'(pods|networkvolumes)(/[A-Za-z0-9_-]{1,128})?',resource):
            raise ValueError('Unsupported resource path')
        return self._send(method,'https://rest.runpod.io/v1/'+resource,payload)

    def _send(self,method,url,payload=None):
        request=Request(url,
            data=canonical(payload) if payload is not None else None,method=method,
            headers={'Authorization':'Bearer '+self.token,'Content-Type':'application/json',
                     'User-Agent':'Coppertop/0.1 RunPod bounded controller'})
        try:
            with self.opener.open(request,timeout=20) as response:
                raw=response.read(1024**2+1)
            if len(raw)>1024**2: raise ValueError('Provider response exceeds bound')
            return json.loads(raw) if raw else None
        except HTTPError as error:
            error.close()
            if error.code==404 and method=='GET': return None
            raise RuntimeError('RunPod HTTP status '+str(error.code)) from None

    def quote(self,gpu,datacenter):
        response=self._send('GET','https://api.runpod.io/v2/catalog/gpus?include=AVAILABILITY&product=POD&count=1&cloud=SECURE')
        values=response.get('gpus',[]) if isinstance(response,dict) else []
        matches=[value for value in values if value.get('id')==gpu]
        if len(matches)!=1: raise ValueError('Exact GPU catalog record unavailable')
        value=matches[0]
        if (value.get('secure') is not True or value.get('availability')=='NONE'
            or not any(d.get('id')==datacenter and d.get('availability')!='NONE' for d in value.get('dataCenters',[]))
            or not any(v.get('version')=='12.8' and v.get('available') is True for v in value.get('cudaVersions',[]))):
            raise ValueError('Exact datacenter GPU/CUDA capacity unavailable')
        return {'gpu':gpu,'datacenter':datacenter,'gpu_hour_usd':str(value['price']['secure']),
                'checked_utc':datetime.now(timezone.utc).isoformat(),'availability':value['availability']}


def pod_payload(prepared,volume_id):
    p=prepared['plan']; c=p['config']
    return {'name':p['pod_name'],'imageName':c['image'],'cloudType':'SECURE',
        'computeType':'GPU','gpuTypeIds':[c['gpu']],'gpuCount':1,'interruptible':False,
        'allowedCudaVersions':['12.8'],'dataCenterIds':[c['datacenter']],
        'networkVolumeId':identifier(volume_id),'volumeMountPath':'/workspace',
        'containerDiskInGb':100,'volumeInGb':0,'minRAMPerGPU':64,'minVCPUPerGPU':4,
        'ports':[],'supportPublicIp':False,'globalNetworking':False,
        'dockerEntrypoint':[], 'dockerStartCmd':['/opt/conda/bin/python','-m','coppertop.runpod_worker'],
        'env':{'COPPERTOP_RUN_ID':c['run_id'],'COPPERTOP_PLAN_SHA256':prepared['plan_sha256'],
               'COPPERTOP_RUNTIME_IMAGE':c['image']}}


def _resource_matches(kind,value,prepared,volume_id=None):
    p=prepared['plan']; c=p['config']
    if kind=='networkvolumes':
        return value.get('name')==p['volume_name'] and value.get('dataCenterId')==c['datacenter'] and value.get('size')==3
    return (value.get('name')==p['pod_name'] and value.get('imageName')==c['image']
            and value.get('env',{}).get('COPPERTOP_PLAN_SHA256')==prepared['plan_sha256']
            and (value.get('networkVolumeId') or value.get('networkVolume',{}).get('id'))==volume_id)


def cleanup(api,evidence,state,prepared,path):
    """Terminate, verify absent, abort uploads, delete exact volume, verify absent.

    A failed cleanup remains a charged hold. Never delete a volume while an owned
    Pod's absence is unknown. Re-running this function does not allocate resources.
    """
    result={}
    with hard_deadline(LIMITS['cleanup_seconds']):
        for kind,field in (('networkvolumes','volume_id'),('pods','pod_id')):
            if not state.get(field) and state.get('pending')==kind:
                try:
                    values=api.request('GET',kind)
                    matches=[v for v in values if _resource_matches(kind,v,prepared,state.get('volume_id'))]
                    if len(matches)>1: raise ValueError('Ambiguous created resource')
                    if matches: state[field]=identifier(matches[0]['id']); save(path,state)
                    else: result[field]='absent_after_ambiguous_create'
                except TimeoutError: raise
                except Exception as error: result[field]='unresolved_'+type(error).__name__
        try:
            pod=state.get('pod_id')
            if pod:
                current=api.request('GET','pods/'+pod)
                if current is not None:
                    if not _resource_matches('pods',current,prepared,state.get('volume_id')):
                        raise ValueError('Pod ownership changed')
                    api.request('DELETE','pods/'+pod)
                until=time.monotonic()+60
                while api.request('GET','pods/'+pod) is not None:
                    if time.monotonic()>=until: raise TimeoutError('Pod deletion not verified')
                    time.sleep(1)
            if result.get('pod_id','').startswith('unresolved'): raise ValueError('Pod identity unresolved')
            result['pod_absent']=True
        except TimeoutError: raise
        except Exception as error: result['pod_error']=type(error).__name__
        if result.get('pod_absent') is True and state.get('volume_id'):
            volume=state['volume_id']
            try:
                current=api.request('GET','networkvolumes/'+volume)
                if current is not None:
                    if not _resource_matches('networkvolumes',current,prepared): raise ValueError('Volume ownership changed')
                    # Include any upload that was interrupted before its ID was journaled.
                    try:
                        with hard_deadline(100): evidence(volume).abort_incomplete()
                    except Exception as error: result['multipart_error']=type(error).__name__
                    api.request('DELETE','networkvolumes/'+volume)
                until=time.monotonic()+60
                while api.request('GET','networkvolumes/'+volume) is not None:
                    if time.monotonic()>=until: raise TimeoutError('Volume deletion not verified')
                    time.sleep(1)
                result['volume_absent']=True
            except TimeoutError: raise
            except Exception as error: result['volume_error']=type(error).__name__
        elif not state.get('volume_id') and not result.get('volume_id','').startswith('unresolved'):
            result['volume_absent']=True
    state['cleanup']=result;save(path,state)
    return result


def execute(prepared,approved_sha256,directory,*,api,evidence,publisher,cpu_receipt,now=None):
    """One new Pod/volume only. No retries, auto repair, or fallback GPU/provider."""
    p=prepared['plan']; c=p['config']
    if (digest(canonical(p))!=approved_sha256 or prepared['plan_sha256']!=approved_sha256
        or prepare(c)!=prepared or digest(canonical(cpu_receipt))!=c['cpu_admission_sha256']
        or cpu_receipt.get('status')!='passed' or cpu_receipt.get('cuda_executed') is not False
        or cpu_receipt.get('render_reset_executed') is not False
        or cpu_receipt.get('worker_sha256')!=p['worker_sha256']
        or cpu_receipt.get('runtime_export_sha256')!=c['runtime_export_sha256']
        or cpu_receipt.get('simulator',{}).get('status')!='passed'
        or any(cpu_receipt.get(name) is not True for name in ('model_checksums_verified','full_sources_verified','system_libraries_loaded'))
        or any(cpu_receipt.get('native',{}).get(name) is not True for name in ('active_closure_checked','pip_check','imports_verified','cuda_abi_verified'))):
        raise ValueError('Exact code/configuration and actual CPU receipt approval required')
    now=now or datetime.now(timezone.utc)
    age=(now-datetime.fromisoformat(c['prices']['checked_utc'])).total_seconds()
    if not 0<=age<=900: raise ValueError('Capacity and price quote must be refreshed within 15 minutes')
    if publisher.bucket!=c['r2_bucket'] or publisher.prefix!=p['r2_prefix']:
        raise ValueError('Exact fresh scoped R2 destination required')
    expires=getattr(publisher,'session_expires_utc',None)
    if not isinstance(expires,datetime) or expires.tzinfo is None or not 3540<=(expires-now).total_seconds()<=3600:
        raise ValueError('Fresh R2 session with 3540 to 3600 seconds remaining required')
    from .image_check import pinned_dependencies
    root=Path(__file__).resolve().parents[1]
    native=cpu_receipt.get('native',{})
    expected=pinned_dependencies(root/'requirements-gpu-smoke.lock',root/'requirements-r2.txt')
    versions=native.get('versions',{})
    if (native.get('status')!='passed' or any(versions.get(name)!=version for name,version in expected.items())
        or any(versions.get(name,'').split('+')[0]!=version for name,version in
               (('torch','2.8.0'),('torchvision','0.23.0'),('flash-attn','2.8.3')))):
        raise ValueError('Actual native CPU inventory pins differ')
    directory=Path(directory); directory.mkdir(parents=True,exist_ok=True)
    journal=directory/'state.json'
    state={'prepared':prepared,'pending':None,'consumed':True,'status':'running','holds_released':False}
    with journal.open('xb') as target:
        target.write(canonical(state));target.flush();os.fsync(target.fileno())
    began=time.monotonic()
    try:
        with hard_deadline(LIMITS['controller_seconds']):
            with hard_deadline(LIMITS['setup_seconds']):
                quote=api.quote(c['gpu'],c['datacenter'])
                if (quote.get('gpu')!=c['gpu'] or quote.get('datacenter')!=c['datacenter']
                    or amount(quote['gpu_hour_usd'])>amount(c['prices']['gpu_hour_usd'])):
                    raise ValueError('Current capacity quote exceeds approved scope/price')
                state['live_quote']=quote;save(journal,state)
                for kind,name in (('pods',p['pod_name']),('networkvolumes',p['volume_name'])):
                    if any(v.get('name')==name for v in api.request('GET',kind)):
                        raise ValueError('Owned name already exists; recover instead of relaunch')
                state['pending']='networkvolumes';save(journal,state)
                value=api.request('POST','networkvolumes',{'name':p['volume_name'],'size':3,'dataCenterId':c['datacenter']})
                state['volume_id']=identifier(value['id']);state['pending']=None;save(journal,state)
                transport=evidence(state['volume_id'])
                transport.stage_plan(prepared)
                state['pending']='pods';save(journal,state)
                pod=api.request('POST','pods',pod_payload(prepared,state['volume_id']))
                state['pod_id']=identifier(pod['id']);state['pending']=None;save(journal,state)
                if amount(str(pod['costPerHr']))>amount(c['prices']['gpu_hour_usd']):
                    raise ValueError('Allocated Pod exceeds quoted GPU price')
            with hard_deadline(LIMITS['run_seconds']):
                receipt=transport.wait_receipt(prepared,state['pod_id'],LIMITS['run_seconds'])
                state['receipt']=receipt;save(journal,state)
            # Stop GPU billing before any CPU evidence fetch/publication.
            current=api.request('GET','pods/'+state['pod_id'])
            if current is not None:
                if not _resource_matches('pods',current,prepared,state['volume_id']): raise ValueError('Pod ownership changed')
                api.request('DELETE','pods/'+state['pod_id'])
            with hard_deadline(60):
                while api.request('GET','pods/'+state['pod_id']) is not None: time.sleep(1)
            state['pod_absent']=True;save(journal,state)
            with hard_deadline(LIMITS['publish_seconds']):
                state['publication_started']=True;save(journal,state)
                state['report_key']=transport.publish(prepared,receipt,directory/'fetch',publisher)
                state['status']='passed' if receipt['status']=='passed' else 'failed'
                save(journal,state)
    except BaseException as error:
        state['status']='failed';state['error_type']=type(error).__name__;save(journal,state)
    finally:
        # Scheduling/ready-marker failures may still leave a sealed record. Fetch
        # it once after verified termination, before deleting the evidence volume.
        if state.get('pod_id') and state.get('volume_id') and not state.get('publication_started'):
            try:
                with hard_deadline(60):
                    current=api.request('GET','pods/'+state['pod_id'])
                    if current is not None:
                        if not _resource_matches('pods',current,prepared,state['volume_id']): raise ValueError('Pod ownership changed')
                        api.request('DELETE','pods/'+state['pod_id'])
                    while api.request('GET','pods/'+state['pod_id']) is not None: time.sleep(1)
                state['pod_absent']=True;state['publication_started']=True;save(journal,state)
                remaining=min(LIMITS['publish_seconds'],LIMITS['handoff_seconds']-(time.monotonic()-began))
                if remaining<=0: raise TimeoutError('Overall evidence handoff deadline')
                with hard_deadline(remaining):
                    transport=evidence(state['volume_id'])
                    receipt=transport.recovery_receipt(prepared,state['pod_id'])
                    state['report_key']=transport.publish(prepared,receipt,directory/'fetch',publisher)
                    state['evidence_recovered']=True;save(journal,state)
            except BaseException as error: state['recovery_error']=type(error).__name__;save(journal,state)
        try: cleanup(api,evidence,state,prepared,journal)
        except BaseException as error:
            state['cleanup']={'error_type':type(error).__name__};save(journal,state)
    if not state.get('report_key'):
        # Even allocation/startup failures receive a durable bounded metadata report.
        try:
            with hard_deadline(60):
                from .streams import read_small
                raw=canonical({'provider':'RunPod','run_id':c['run_id'],'plan_sha256':approved_sha256,
                    'status':'failed','error_type':state.get('error_type','EvidenceUnavailable'),
                    'cleanup':state['cleanup'],'evidence_available':False,'automatic_retries':0})
                key=publisher.put(raw)
                if key!=digest(raw) or read_small(publisher,key,65536)!=raw: raise ValueError('Recovery report readback differs')
                state['recovery_report_key']=key
        except BaseException as error: state['publication_error']=type(error).__name__
    marker=directory/'fetch/fetch-complete.json'
    if marker.is_file():
        state['local_evidence']=dict(read_json(marker,1024),directory=str(directory/'fetch'))
    state['runtime_status']=state['status']
    if state['cleanup'].get('pod_absent') is not True or state['cleanup'].get('volume_absent') is not True:
        state['status']='failed';state['cleanup_incomplete']=True
    state['completed']=True;save(journal,state)
    return state


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--config',type=Path)
    parser.add_argument('--output',type=Path,required=True)
    parser.add_argument('--execute',action='store_true')
    parser.add_argument('--recover',type=Path)
    parser.add_argument('--approved-plan-sha256')
    parser.add_argument('--cpu-receipt',type=Path)
    args=parser.parse_args()
    if not args.execute and not args.recover:
        if not args.config: parser.error('--config required')
        save(args.output,prepare(read_json(args.config)));return
    if args.execute and not args.recover:
        if not args.config or not args.cpu_receipt: parser.error('--config and --cpu-receipt required')
        prepared=prepare(read_json(args.config))
        if prepared['plan']['full_publication_live_verified'] is not True:
            parser.error('Full publication is blocked pending the shared A100 publisher diagnosis and a newly validated plan')
    def terminate(signum,frame): raise KeyboardInterrupt('Controller termination requested')
    signal.signal(signal.SIGTERM,terminate)
    # Only execution/recovery reads explicitly supplied credentials. No .env parsing,
    # credential creation, external messaging, or credential delivery to the Pod.
    from .runpod_evidence import make_transports
    api,evidence,publisher=make_transports()
    if args.recover:
        state=read_json(args.recover,1024**2);prepared=state['prepared']
        if digest(canonical(prepared['plan']))!=args.approved_plan_sha256: parser.error('Exact recovery approval required')
        cleanup(api,evidence,state,prepared,args.recover);return
    state=execute(prepared,args.approved_plan_sha256,args.output,
        api=api,evidence=evidence,publisher=publisher,cpu_receipt=read_json(args.cpu_receipt))
    if state['status']!='passed' or state.get('cleanup',{}).get('volume_absent') is not True:
        raise SystemExit(1)


if __name__ == '__main__': main()
