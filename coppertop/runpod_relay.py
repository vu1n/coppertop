"""Local owned-volume relay, shared seekable multipart transport and durable budgets."""
from datetime import datetime, timezone
import json
import math
from pathlib import Path
import resource
import sys
import threading
import time

from .chords import canonical, digest
from .multipart import UploadState
from .reload_launch import hard_deadline, read_json
from .runpod_bundle import BOUNDS, GIB, validate_descriptor, validate_manifest
from .runpod_cloud import identifier, save
from .runpod_evidence import missing
from .runpod_staging import RunpodStager
from .s3 import S3Store
from .streams import ObjectRef, checked, file_chunks, file_ref, CHUNK_BYTES

REQUESTS=4096
PAYLOAD=50*GIB+96*1024**2


def check_memory():
    peak=resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
    peak=peak if sys.platform=='darwin' else peak*1024
    if peak>512*1024**2:raise MemoryError('Local controller observed RSS exceeds512MiB; stop rather than expand')
    return peak


def feasibility(samples,*,bytes_each,seconds_left,now=None):
    """Recent real tiny probe, 2x measured duration plus60s overhead; no promise."""
    now=now or datetime.now(timezone.utc)
    if not samples or seconds_left<=60:raise ValueError('Fresh measured throughput required')
    estimate=60
    for item in samples:
        age=(now-datetime.fromisoformat(item['checked_utc'])).total_seconds()
        size=item['bytes'];seconds=item['seconds']
        if (not 0<=age<=900 or type(size) is not int or not 8*1024**2<=size<=16*1024**2
            or isinstance(seconds,bool) or not isinstance(seconds,(int,float)) or not math.isfinite(seconds) or seconds<=0):
            raise ValueError('Real fresh bounded throughput sample required')
        estimate+=2*bytes_each*seconds/size
    if estimate>seconds_left:raise ValueError('Measured conservative transfer ETA exceeds deadline; do not begin bulk work')
    return {'conservative_seconds':estimate,'seconds_left':seconds_left,'safety_factor':2,
        'Mac_must_remain_online':True,'throughput_is_not_guaranteed':True}


class NamedStore(S3Store):
    def __init__(self,client,bucket,key,ref):
        super().__init__(client,bucket);self.named_key=key;self.ref=ref
    def key(self,sha):
        if sha!=self.ref.sha256:raise ValueError('Different object outside owned scope')
        return self.named_key


class GuardedClient:
    """Reserve requests/bytes and creation intent before dispatch, even on failure."""
    def __init__(self,client,relay):self.client=client;self.relay=relay;self.lock=threading.RLock()
    def __getattr__(self,name):
        if name not in ('put_object','get_object','head_object','create_multipart_upload','upload_part',
                        'list_parts','complete_multipart_upload','abort_multipart_upload','list_multipart_uploads','delete_object'):
            raise ValueError('Unsupported relay operation')
        def call(**kwargs):
            r=self.relay
            with self.lock:
                r.check()
                key=kwargs.get('Key');prefix=kwargs.get('Prefix')
                if kwargs.get('Bucket')!=r.config.volume_id or (key is not None and key not in r.allowed) or (prefix is not None and prefix!=r.prefix+'/'):
                    raise ValueError('Relay operation outside exact owned volume/key scope')
                state=r.state;state['requests']+=1
                if getattr(r,'cleanup_deadline',None) is not None:
                    if name not in ('abort_multipart_upload','list_parts','list_multipart_uploads'):raise ValueError('Explicit cleanup only permits multipart reconciliation/abort')
                    state['cleanup_requests']=state.get('cleanup_requests',0)+1
                    if state['cleanup_requests']>64:raise ValueError('Owned upload cleanup64request bound')
                elif state['requests']>REQUESTS:raise ValueError('Relay request budget exhausted')
                body=kwargs.get('Body',b'')
                if body and type(body) is not bytes:raise ValueError('Relay multipart uses bounded immutable bytes')
                state['written_bytes']+=len(body)
                if body and state['written_bytes']>PAYLOAD:raise ValueError('Relay upload byte budget exhausted')
                if name=='get_object':
                    state['read_bytes']+=r.allowed[key]
                    if state['read_bytes']>PAYLOAD:raise ValueError('Relay readback byte budget exhausted')
                if name=='create_multipart_upload':state['pending_create']=key
                r.persist()
            if name=='complete_multipart_upload':
                with hard_deadline(300):result=getattr(self.client,name)(**kwargs)
            else:result=getattr(self.client,name)(**kwargs)
            with self.lock:
                if name=='create_multipart_upload':
                    # The ID is durable before the shared uploader can send a part.
                    state['created_uploads'][key]=result['UploadId'];state['pending_create']=None;r.persist()
            return result
        return call


class Relay:
    def __init__(self,config,client,*,owner,journal,resume=False,memory_check=None):
        RunpodStager(config,client) # Existing endpoint/region/socket/no-retry admission.
        self.config=config;self.owner=owner;self.prefix='coppertop/cache/'+identifier(owner['attempt'])
        self.memory_check=memory_check or check_memory
        if owner.get('volume_id')!=config.volume_id or owner.get('allocated_gb')!=60 or owner.get('datacenter')!=config.datacenter:
            raise ValueError('Exact newly owned60GB volume required')
        self.journal=Path(journal)
        if self.journal.is_symlink():raise ValueError('Ordinary local journal required')
        if self.journal.exists():
            if not resume:raise ValueError('Explicit resume required; no repeated staging')
            self.state=read_json(self.journal,1024**2)
            if self.state['owner']!=owner:raise ValueError('Resume ownership differs')
        else:
            if resume:raise ValueError('Resume requires existing journal')
            self.state={'owner':owner,'started_utc':datetime.now(timezone.utc).isoformat(),'requests':0,
                'written_bytes':0,'read_bytes':0,'created_uploads':{},'uploads':{},'verified':[],
                'pending_create':None,'status':'incomplete','copied_bytes':0}
            self.journal.parent.mkdir(parents=True,exist_ok=True)
            with self.journal.open('xb') as out:out.write(canonical(self.state))
            self.persist()
        self.allowed={self.prefix+'/owner.json':len(canonical(owner))}
        known=self.state.get('bundle_identity')
        if known:
            for ref in known['descriptor']['shards']:self.allowed[self.prefix+'/bundle/'+ref['name']]=ref['size']
            self.allowed[self.prefix+'/bundle/control.tar']=known['control']['size']
            for name in ('bundle.json','manifest.json'):self.allowed[self.prefix+'/bundle/'+name]=BOUNDS['manifest']
        self.client=GuardedClient(client,self)
    def persist(self):save(self.journal,self.state)
    def check(self):
        self.memory_check()
        if getattr(self,'cleanup_deadline',None) is not None:
            if time.monotonic()>=self.cleanup_deadline:raise TimeoutError('Explicit upload cleanup deadline')
            return
        elapsed=(datetime.now(timezone.utc)-datetime.fromisoformat(self.state['started_utc'])).total_seconds()
        if not 0<=elapsed<7200:raise TimeoutError('Cumulative relay deadline exceeded; no automatic reset')
    def metadata(self,key,raw):
        if len(raw)>BOUNDS['manifest']:raise ValueError('Metadata bound')
        self.allowed[key]=len(raw)
        try:response=self.client.get_object(Bucket=self.config.volume_id,Key=key)
        except Exception as error:
            if not missing(error):raise
            self.client.put_object(Bucket=self.config.volume_id,Key=key,Body=raw)
            response=self.client.get_object(Bucket=self.config.volume_id,Key=key)
        body=response['Body']
        try:
            if response['ContentLength']!=len(raw) or body.read(len(raw)+1)!=raw:raise ValueError('Existing owned metadata differs')
        finally:body.close()
    def adopt_scope(self,bundle,control):
        descriptor=validate_descriptor(read_json(Path(bundle)/'bundle.json',65536))
        value=validate_manifest(read_json(Path(bundle)/'manifest.json',BOUNDS['manifest']))
        if descriptor['manifest_sha256']!=digest(canonical(value)):raise ValueError('Manifest identity differs')
        files={'manifest.json':Path(bundle)/'manifest.json','bundle.json':Path(bundle)/'bundle.json','control.tar':Path(control)}
        files.update({s['name']:Path(bundle)/s['name'] for s in descriptor['shards']})
        if file_ref(control).size>BOUNDS['code']:raise ValueError('Bootstrap code bound')
        identity={'descriptor':descriptor,'control':file_ref(control).to_dict()}
        if self.state.get('bundle_identity') not in (None,identity):raise ValueError('Resumed immutable source differs')
        self.state['bundle_identity']=identity;self.persist()
        for name,path in files.items():self.allowed[self.prefix+'/bundle/'+name]=file_ref(path).size
        return descriptor,files
    def stage(self,bundle,control,*,samples):
        descriptor,files=self.adopt_scope(bundle,control);self.check()
        elapsed=(datetime.now(timezone.utc)-datetime.fromisoformat(self.state['started_utc'])).total_seconds()
        feasibility(samples,bytes_each=descriptor['tar_bytes'],seconds_left=7200-elapsed)
        with hard_deadline(7200-elapsed):
            self.metadata(self.prefix+'/owner.json',canonical(self.owner))
            if self.state.get('pending_create'):raise ValueError('Lost multipart creation ID; explicit reconcile/abort before continuation')
            for name,path in files.items():
                self.check();key=self.prefix+'/bundle/'+name;ref=file_ref(path)
                store=NamedStore(self.client,self.config.volume_id,key,ref)
                if key in self.state['verified']:
                    # A durable full SHA receipt is reused by this exclusive
                    # writer, not redownloaded outside the cumulative budget.
                    # The CPU restore always rehashes every mounted shard.
                    if store.stat(ref.sha256)!=ref:raise ValueError('Verified staged shard size changed')
                    continue
                existing=self.state['uploads'].get(key)
                known=self.state['created_uploads'].get(key)
                if not existing and known:existing=UploadState(self.config.volume_id+'/'+key,known,ref,16*1024**2).to_dict()
                upload=UploadState.from_dict(existing) if existing else None
                def persist(value):self.state['uploads'][key]=value;self.persist()
                if ref.size<=8*1024**2 and upload is None:
                    self.metadata(key,path.read_bytes())
                else:
                    store.multipart_file(ref,path,state=upload,persist=persist,concurrency=4,
                        observe=lambda event:save(self.journal.parent/'relay-telemetry.json',event))
                self.state['verified'].append(key);self.persist()
            ready={'owner':self.owner,'bundle':descriptor,'control':file_ref(control).to_dict(),
                'all_staged_bytes_verified':True,'extracted_on_volume':False}
            self.metadata(self.prefix+'/ready.json',canonical(ready))
            self.state.update(status='staged',ready=ready);self.persist()
        return ready
    def reconcile_lost_create(self):
        key=self.state.get('pending_create')
        if not key:return
        response=self.client.list_multipart_uploads(Bucket=self.config.volume_id,Prefix=self.prefix+'/',MaxUploads=16)
        if response.get('IsTruncated') or len(response.get('Uploads',[]))>16:raise ValueError('Owned upload inventory bound')
        values=[v for v in response.get('Uploads',[]) if v['Key']==key]
        if len(values)!=1:raise ValueError('Unresolved upload creation; preserve source and volume, never recreate')
        self.state['created_uploads'][key]=values[0]['UploadId'];self.state['pending_create']=None;self.persist()
    def abort_owned(self):
        """Explicit cleanup only. Unknown create IDs remain unresolved, no broad delete."""
        self.cleanup_deadline=time.monotonic()+300
        try:
            with hard_deadline(300):
                if self.state.get('pending_create'):self.reconcile_lost_create()
                for key,upload in self.state['created_uploads'].items():
                    try:self.client.abort_multipart_upload(Bucket=self.config.volume_id,Key=key,UploadId=upload)
                    except Exception as error:
                        if getattr(error,'response',{}).get('Error',{}).get('Code')!='NoSuchUpload':raise
                    try:self.client.list_parts(Bucket=self.config.volume_id,Key=key,UploadId=upload)
                    except Exception as error:
                        if getattr(error,'response',{}).get('Error',{}).get('Code')=='NoSuchUpload':continue
                        raise
                    raise ValueError('Upload absence unverified')
                self.state['uploads_absent']=True;self.persist()
        finally:self.cleanup_deadline=None


def copy_shards(filesystem,remote,local,descriptor,*,journal,sample,seconds_left):
    """Public Modal file API, one <5GB file at a time; partial local bytes preserved."""
    validate_descriptor(descriptor);local=Path(local);local.mkdir(parents=True,exist_ok=True)
    state=read_json(journal,1024**2) if Path(journal).exists() else {'descriptor':descriptor,'bytes_reserved':0,'complete':[]}
    if state['descriptor']!=descriptor:raise ValueError('Copy resume source differs')
    feasibility([sample],bytes_each=descriptor['tar_bytes'],seconds_left=seconds_left)
    with hard_deadline(seconds_left):
        for ref in descriptor['shards']:
            path=local/ref['name']
            if path.exists():
                if path.is_symlink() or file_ref(path)!=ObjectRef(ref['sha256'],ref['size']):raise ValueError('Local resumed shard differs')
                continue
            partial=path.with_suffix('.partial')
            if partial.exists():raise ValueError('Partial copy requires explicit removal after source reconciliation')
            state['bytes_reserved']+=ref['size']
            if state['bytes_reserved']>55*GIB:raise ValueError('Cumulative Modal copy/egress budget exceeded')
            save(journal,state);filesystem.copy_to_local(remote+'/'+ref['name'],partial)
            if partial.is_symlink() or file_ref(partial)!=ObjectRef(ref['sha256'],ref['size']):raise ValueError('Copied shard differs')
            partial.replace(path);state['complete'].append(ref['name']);save(journal,state)
    return state
