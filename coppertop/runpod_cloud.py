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
from .record_archive import MAX_ARCHIVE_BYTES, archive_ref
from .archive_retention import validate_retention

LIMITS = {'setup_seconds':900,'run_seconds':1200,'publish_seconds':900,
          'cleanup_seconds':180,'controller_seconds':3000,'handoff_seconds':3300,'worker_seconds':560,
          'scratch_bytes':2*1024**3+96*1024**2,'retained_bytes':2*1024**3,'raw_bytes':1024**3,
          'archive_bytes':MAX_ARCHIVE_BYTES,'objects':6000,'multipart_part_bytes':16*1024**2,'multipart_concurrency':4,
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


def live_archive_verified():
    root=Path(__file__).resolve().parents[1]
    value=read_json(root/'containers/archive-live-admission.json',4096)
    return (value.get('status')=='passed' and value.get('full_SHA256_readback') is True
        and value.get('receipt_verified') is True and bool(value.get('file_sha256'))
        and all(digest((root/name).read_bytes())==sha for name,sha in value['file_sha256'].items()))


def cost_bound(prices,cache=False):
    """Reservations include hourly rounding and full first storage month, no tiers."""
    # Price discovery/setup/export/build/registry are a separate approved phase.
    gpu = amount(prices['gpu_hour_usd'])*(3 if cache else 1)
    disk = amount(prices['container_gb_month_usd'])*100
    volume = amount(prices['volume_gb_month_usd'])*(60 if cache else 3)
    r2 = amount(prices['r2_gb_month_usd'])*2 # 1 GiB rounds up to two billable GB
    requests = amount(prices['r2_request_reservation_usd'])
    transfer = amount(prices['transfer_reservation_usd'])
    return str(gpu+disk+volume+r2+requests+transfer)


def prepare(config):
    """Bind exact OCI image, real CPU validation receipt, prices and code to approval."""
    fields={'run_id','image','runtime_export_sha256','cpu_admission_sha256',
                        'datacenter','gpu','prices','additional_cap_usd','r2_bucket','retention'}
    if set(config) not in (fields,fields|{'runtime_cache'}):
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
    cache=config.get('runtime_cache')
    if cache:
        from .runpod_prepare import validate_cache
        from .runpod_runtime import contract
        validate_cache(cache)
        if config['image']!=contract()['base'] or config['runtime_export_sha256']!=cache['manifest_sha256'] or config['datacenter']!=cache['datacenter']:
            raise ValueError('Pinned base/cache manifest/datacenter differ')
    bound=cost_bound(prices,bool(cache))
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
          'r2_prefix':'chords/mechanism/'+config['run_id'], 'cost_bound_usd':bound,
          'archive_retention':validate_retention(config['retention']) if config['retention'] is not None else None,
          'retention_enforcement':'explicit ongoing storage acceptance; named owner; no automatic expiry service',
          'paid_authorized':False,'setup_build_and_bulk_transfer_included':False,
          'full_publication_live_verified':live_archive_verified()}
    if cache:
        plan['volume_name']=cache['volume_name']
        plan['limits'].update(setup_seconds=6300,controller_seconds=8400,handoff_seconds=8700,network_volume_gb=60)
        plan['cache_reuse']=True
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

    def cpu_quote(self,expected):
        from .runpod_prepare import validate_quote
        # The recorded approval can be older after export. Validate its shape at
        # its original clock, then fetch a fresh quote without broadening scope.
        validate_quote(expected,now=datetime.fromisoformat(expected['checked_utc']))
        count=expected['vcpus']
        response=self._send('GET',f'https://api.runpod.io/v2/catalog/cpus?include=AVAILABILITY&product=POD&vcpuCount={count}')
        values=response.get('cpus',[]) if isinstance(response,dict) else []
        matches=[v for v in values if v.get('id')==expected['type_id']]
        if len(matches)!=1:raise ValueError('Exact CPU flavor unavailable; no alternate route')
        value=matches[0]
        dc=[d for d in value.get('dataCenters',[]) if d.get('id')=='US-WA-1']
        if len(dc)!=1 or not value['vcpu']['min']<=count<=value['vcpu']['max']:raise ValueError('CPU datacenter/count unavailable')
        quote={'type_id':value['id'],'flavor_id':expected['flavor_id'],'vcpus':count,
            'ram_gib':str(Decimal(str(value['ramGbPerVcpu']))*count*Decimal(10**9)/Decimal(1024**3)),
            'hour_usd':str(Decimal(str(value['price']['securePerVcpu']))*count),
            'datacenter':'US-WA-1','availability':dc[0]['availability'],
            'checked_utc':datetime.now(timezone.utc).isoformat()}
        return validate_quote(quote)


def pod_payload(prepared,volume_id):
    p=prepared['plan']; c=p['config']
    result={'name':p['pod_name'],'imageName':c['image'],'cloudType':'SECURE',
        'computeType':'GPU','gpuTypeIds':[c['gpu']],'gpuCount':1,'interruptible':False,
        'allowedCudaVersions':['12.8'],'dataCenterIds':[c['datacenter']],
        'networkVolumeId':identifier(volume_id),'volumeMountPath':'/workspace',
        'containerDiskInGb':100,'volumeInGb':0,'minRAMPerGPU':64,'minVCPUPerGPU':4,
        'ports':[],'supportPublicIp':False,'globalNetworking':False,
        'dockerEntrypoint':[], 'dockerStartCmd':['/opt/conda/bin/python','-m','coppertop.runpod_worker'],
        'env':{'COPPERTOP_RUN_ID':c['run_id'],'COPPERTOP_PLAN_SHA256':prepared['plan_sha256'],
               'COPPERTOP_RUNTIME_IMAGE':c['image']}}
    if c.get('runtime_cache'):
        from .runpod_prepare import CONTROL_START
        cache=c['runtime_cache'];result['dockerStartCmd']=['/opt/conda/bin/python','-c',CONTROL_START]
        result['env'].update(COPPERTOP_BOOTSTRAP_MODE='rollout',COPPERTOP_CONTROL_SHA256=cache['control_sha256'],
            COPPERTOP_CACHE_BUNDLE='/workspace/coppertop/cache/'+cache['attempt']+'/bundle')
    return result


def _resource_matches(kind,value,prepared,volume_id=None):
    p=prepared['plan']; c=p['config']
    if kind=='networkvolumes':
        return value.get('name')==p['volume_name'] and value.get('dataCenterId')==c['datacenter'] and value.get('size')==p['limits']['network_volume_gb']
    return (value.get('name')==p['pod_name'] and value.get('imageName',value.get('image'))==c['image']
            and value.get('env',{}).get('COPPERTOP_PLAN_SHA256')==prepared['plan_sha256']
            and (value.get('networkVolumeId') or value.get('networkVolume',{}).get('id'))==volume_id)


def cleanup(api,evidence,state,prepared,path,*,delete_unpublished=False):
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
                    if prepared['plan']['config'].get('runtime_cache'):
                        result.update(cache_retained=True,volume_id=volume,allocated_gb=60,
                            stage_retained=not state.get('publication_verified',False),
                            monthly_allocated_volume_usd=str(amount(prepared['plan']['config']['prices']['volume_gb_month_usd'])*60),
                            required_action='recover any unpublished evidence; explicit owned-cache cleanup by original retention deadline',
                            automatic_expiry_enforced=False)
                        state['cleanup']=result;save(path,state);return result
                    source_possible=bool(state.get('pod_creation_attempted') or state.get('pod_id'))
                    if source_possible and not state.get('publication_verified'):
                        policy=validate_retention(prepared['plan'].get('archive_retention'))
                        if not delete_unpublished or (state.get('r2_upload_recovery_required') and not state.get('r2_upload_abort_verified')):
                            result.update(stage_retained=True,volume_id=volume,allocated_gb=3,
                                max_stage_bytes=policy['max_bytes'],cleanup_owner=policy['cleanup_owner'],
                                ongoing_storage_charges_accepted=True,release_condition=policy['release_condition'],
                                rate_usd_per_allocated_gb_month=prepared['plan']['config']['prices']['volume_gb_month_usd'],
                                monthly_allocated_volume_usd=str(amount(prepared['plan']['config']['prices']['volume_gb_month_usd'])*3),
                                required_action='explicitly resume/verify publication, or approve deletion of unpublished source',
                                automatic_expiry_enforced=False,r2_upload_recovery_required=bool(state.get('r2_upload_recovery_required')),
                                r2_upload_state_unresolved=bool(state.get('r2_upload_state_unresolved')))
                            try:
                                measurement=evidence(volume).stage_size(prepared)
                                if not isinstance(measurement,dict) or len(canonical(measurement))>1024:raise ValueError('Bounded size metadata required')
                                result['stage_size']=measurement
                            except Exception as error:result['stage_size']={'unavailable':type(error).__name__}
                            state['cleanup']=result;save(path,state);save(Path(path).parent/'retention.json',result);return result
                        result['unpublished_deletion_explicitly_permitted']=True
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


def accept_publication(state,publication,receipt):
    from .streams import ObjectRef
    if (not isinstance(publication,dict) or publication.get('archive_verified') is not True
        or publication.get('archive')!=receipt.get('archive')):
        raise ValueError('Verified archive completion required before source deletion')
    ObjectRef(publication['report_key'],0)
    state.update(report_key=publication['report_key'],archive_publication=publication,publication_verified=True)


def execute(prepared,approved_sha256,directory,*,api,evidence,publisher,cpu_receipt,now=None):
    """One new Pod/volume only. No retries, auto repair, or fallback GPU/provider."""
    p=prepared['plan']; c=p['config']
    policy=validate_retention(p.get('archive_retention'))
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
    if publisher is None:
        if not c.get('runtime_cache'):raise ValueError('Publisher required for the short prebuilt-image path')
    else:
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
    state={'prepared':prepared,'pending':None,'consumed':True,'status':'running','holds_released':False,
           'publication_verified':False}
    with journal.open('xb') as target:
        target.write(canonical(state));target.flush();os.fsync(target.fileno())
    began=time.monotonic()
    try:
        with hard_deadline(p['limits']['controller_seconds']):
            with hard_deadline(p['limits']['setup_seconds']):
                quote=api.quote(c['gpu'],c['datacenter'])
                if (quote.get('gpu')!=c['gpu'] or quote.get('datacenter')!=c['datacenter']
                    or amount(quote['gpu_hour_usd'])>amount(c['prices']['gpu_hour_usd'])):
                    raise ValueError('Current capacity quote exceeds approved scope/price')
                state['live_quote']=quote;save(journal,state)
                cache=c.get('runtime_cache')
                if cache:
                    state['volume_id']=cache['volume_id'];save(journal,state)
                    value=api.request('GET','networkvolumes/'+state['volume_id'])
                    if value is None or not _resource_matches('networkvolumes',value,prepared):raise ValueError('Prepared owned cache unavailable/changed')
                    pods=api.request('GET','pods')
                    if any(v.get('name')==p['pod_name'] or (v.get('networkVolumeId') or v.get('networkVolume',{}).get('id'))==state['volume_id'] for v in pods):raise ValueError('Cache is in use; no second writer')
                else:
                    for kind,name in (('pods',p['pod_name']),('networkvolumes',p['volume_name'])):
                        if any(v.get('name')==name for v in api.request('GET',kind)):
                            raise ValueError('Owned name already exists; recover instead of relaunch')
                    state['pending']='networkvolumes';save(journal,state)
                    value=api.request('POST','networkvolumes',{'name':p['volume_name'],'size':3,'dataCenterId':c['datacenter']})
                    state['volume_id']=identifier(value['id']);state['pending']=None;save(journal,state)
                transport=evidence(state['volume_id'])
                if cache:transport.require_cache(prepared,cpu_receipt)
                transport.stage_plan(prepared)
                state['pending']='pods';state['pod_creation_attempted']=True;save(journal,state)
                pod=api.request('POST','pods',pod_payload(prepared,state['volume_id']))
                state['pod_id']=identifier(pod['id']);state['pending']=None;save(journal,state)
                if amount(str(pod['costPerHr']))>amount(c['prices']['gpu_hour_usd']):
                    raise ValueError('Allocated Pod exceeds quoted GPU price')
                if cache:transport.wait_bootstrap(prepared,state['pod_id'],p['limits']['setup_seconds'])
            with hard_deadline(LIMITS['run_seconds']):
                receipt=transport.wait_receipt(prepared,state['pod_id'],LIMITS['run_seconds'])
                if receipt.get('durable_archive_verified') is not True:raise ValueError('Verified archive stage required before GPU handoff')
                archive_ref({'schema_version':1,'format':'coppertop-episode-tar','archive':receipt['archive']})
                state['receipt']=receipt;state['runtime_status']=receipt['status'];save(journal,state)
            # Stop GPU billing before any CPU evidence fetch/publication.
            current=api.request('GET','pods/'+state['pod_id'])
            if current is not None:
                if not _resource_matches('pods',current,prepared,state['volume_id']): raise ValueError('Pod ownership changed')
                api.request('DELETE','pods/'+state['pod_id'])
            with hard_deadline(60):
                while api.request('GET','pods/'+state['pod_id']) is not None: time.sleep(1)
            state['pod_absent']=True;save(journal,state)
            if publisher is None:
                state.update(publication_deferred=True,status='awaiting_publication')
                save(journal,state)
            else:
                with hard_deadline(LIMITS['publish_seconds']):
                    state['publication_started']=True;save(journal,state)
                    publication=transport.publish(prepared,receipt,directory/'fetch',publisher)
                    accept_publication(state,publication,receipt)
                    state['status']='passed' if receipt['status']=='passed' else 'failed'
                    save(journal,state)
    except BaseException as error:
        state['status']='failed';state['error_type']=type(error).__name__;save(journal,state)
    finally:
        # Scheduling/ready-marker failures may still leave a sealed record. Fetch
        # it once after verified termination, before deleting the evidence volume.
        if publisher is not None and state.get('pod_id') and state.get('volume_id') and not state.get('publication_started'):
            try:
                with hard_deadline(60):
                    current=api.request('GET','pods/'+state['pod_id'])
                    if current is not None:
                        if not _resource_matches('pods',current,prepared,state['volume_id']): raise ValueError('Pod ownership changed')
                        api.request('DELETE','pods/'+state['pod_id'])
                    while api.request('GET','pods/'+state['pod_id']) is not None: time.sleep(1)
                state['pod_absent']=True;state['publication_started']=True;save(journal,state)
                remaining=min(LIMITS['publish_seconds'],p['limits']['handoff_seconds']-(time.monotonic()-began))
                if remaining<=0: raise TimeoutError('Overall evidence handoff deadline')
                with hard_deadline(remaining):
                    transport=evidence(state['volume_id'])
                    receipt=transport.recovery_receipt(prepared,state['pod_id'])
                    publication=transport.publish(prepared,receipt,directory/'fetch',publisher)
                    accept_publication(state,publication,receipt)
                    state['runtime_status']=receipt['status']
                    state['evidence_recovered']=True;save(journal,state)
            except BaseException as error: state['recovery_error']=type(error).__name__;save(journal,state)
        upload_journal=directory/'fetch/upload-state.json'
        if (upload_journal.is_file() or (directory/'fetch/r2-attempt.json').is_file()) and not state.get('publication_verified'):
            state['r2_upload_recovery_required']=True
            state['r2_upload_state_unresolved']=not upload_journal.is_file()
            state['r2_upload_journal']=str(upload_journal)
            save(journal,state)
        try: cleanup(api,evidence,state,prepared,journal)
        except BaseException as error:
            state['cleanup']={'error_type':type(error).__name__};save(journal,state)
    if publisher is not None and not state.get('report_key'):
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
    state.setdefault('runtime_status','unknown')
    state['publication_status']='verified' if state.get('publication_verified') else 'unverified'
    if state['cleanup'].get('pod_absent') is not True or (state['cleanup'].get('volume_absent') is not True and not state['cleanup'].get('stage_retained') and not state['cleanup'].get('cache_retained')):
        state['status']='failed';state['cleanup_incomplete']=True
    state['completed']=True;save(journal,state)
    return state


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--config',type=Path)
    parser.add_argument('--output',type=Path,required=True)
    parser.add_argument('--execute',action='store_true')
    parser.add_argument('--recover',type=Path)
    parser.add_argument('--resume-publication',action='store_true')
    parser.add_argument('--delete-unpublished',action='store_true')
    parser.add_argument('--approved-plan-sha256')
    parser.add_argument('--cpu-receipt',type=Path)
    args=parser.parse_args()
    if (args.resume_publication or args.delete_unpublished) and not args.recover:parser.error('Explicit recovery journal required')
    if args.resume_publication and args.delete_unpublished:parser.error('Resume and destructive cleanup are separate approvals')
    if not args.execute and not args.recover:
        if not args.config: parser.error('--config required')
        save(args.output,prepare(read_json(args.config)));return
    if args.execute and not args.recover:
        if not args.config or not args.cpu_receipt: parser.error('--config and --cpu-receipt required')
        prepared=prepare(read_json(args.config))
        if prepared['plan']['full_publication_live_verified'] is not True:
            parser.error('Full publication is blocked pending live archive validation and a newly approved plan')
        if args.approved_plan_sha256!=prepared['plan_sha256']:
            parser.error('Exact fresh rollout approval required before credential access')
        cpu_receipt=read_json(args.cpu_receipt,65536)
        if digest(canonical(cpu_receipt))!=prepared['plan']['config']['cpu_admission_sha256']:
            parser.error('Exact real CPU receipt required before credential access')
    if args.recover:
        state=read_json(args.recover,1024**2);prepared=state['prepared']
        if digest(canonical(prepared['plan']))!=args.approved_plan_sha256:parser.error('Exact recovery approval required')
        validate_retention(prepared['plan'].get('archive_retention'))
    def terminate(signum,frame): raise KeyboardInterrupt('Controller termination requested')
    signal.signal(signal.SIGTERM,terminate)
    # Only execution/recovery reads explicitly supplied credentials. No .env parsing,
    # credential creation, external messaging, or credential delivery to the Pod.
    from .runpod_evidence import make_transports
    api,evidence,publisher=make_transports(publication=not args.recover and not prepared['plan']['config'].get('runtime_cache'))
    if args.recover:
        cleanup(api,evidence,state,prepared,args.recover)
        if state.get('publication_verified'):
            if state['cleanup'].get('pod_absent') is not True or not (state['cleanup'].get('volume_absent') or state['cleanup'].get('cache_retained')):raise SystemExit(1)
            return
        if not state.get('publication_verified') and state.get('publication_started'):
            # A crashed controller may not have saved its final upload disposition.
            local=args.recover.parent/'fetch/upload-state.json'
            if local.is_file():known=read_json(local,65536)
            else:
                known_upload=evidence(state['volume_id']).upload_state(prepared)
                known=known_upload.to_dict() if known_upload is not None else None
            state['r2_upload_recovery_required']=True
            state['r2_upload_state_unresolved']=known is None
            save(args.recover,state)
            if known is None and (args.resume_publication or args.delete_unpublished):
                parser.error('Multipart state unresolved; reconcile any orphan under separate exact-key authorization before upload or source deletion')
        if args.resume_publication or (args.delete_unpublished and state.get('r2_upload_recovery_required')):
            if state['cleanup'].get('pod_absent') is not True:parser.error('Verified GPU absence required before resume')
            _,_,publisher=make_transports(publication=True)
            c=prepared['plan']['config'];now=datetime.now(timezone.utc)
            expires=getattr(publisher,'session_expires_utc',None)
            if (publisher.bucket!=c['r2_bucket'] or publisher.prefix!=prepared['plan']['r2_prefix']
                or not isinstance(expires,datetime) or expires.tzinfo is None
                or not 3540<=(expires-now).total_seconds()<=3600):parser.error('Fresh exact archive session required for explicit resume')
        if args.resume_publication:
            with hard_deadline(LIMITS['publish_seconds']):
                transport=evidence(state['volume_id']);receipt=transport.recovery_receipt(prepared,state['pod_id'])
                result=transport.publish(prepared,receipt,args.recover.parent/'fetch',publisher,resume=True)
                accept_publication(state,result,receipt);state['r2_upload_recovery_required']=False
                state['publication_deferred']=False;state['status']='passed' if receipt['status']=='passed' else 'failed';save(args.recover,state)
            cleanup(api,evidence,state,prepared,args.recover)
        elif args.delete_unpublished:
            if state.get('r2_upload_recovery_required'):
                from .multipart import UploadState
                local=args.recover.parent/'fetch/upload-state.json'
                upload=UploadState.from_dict(read_json(local,65536)) if local.is_file() else evidence(state['volume_id']).upload_state(prepared)
                if upload is None:parser.error('Owned multipart journal unavailable; retain stage for recovery')
                from .runpod_evidence import abort_publication
                with hard_deadline(60):abort_publication(publisher,upload)
                state['r2_upload_abort_verified']=True;save(args.recover,state)
            cleanup(api,evidence,state,prepared,args.recover,delete_unpublished=True)
        if state['cleanup'].get('pod_absent') is not True or state['cleanup'].get('volume_error'):raise SystemExit(1)
        return
    state=execute(prepared,args.approved_plan_sha256,args.output,
        api=api,evidence=evidence,publisher=publisher,cpu_receipt=read_json(args.cpu_receipt))
    if state['status']!='passed' or not (state.get('cleanup',{}).get('volume_absent') or state.get('cleanup',{}).get('cache_retained')):
        raise SystemExit(1)


if __name__ == '__main__': main()
