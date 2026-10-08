"""External CPU RunPod S3 handoff and existing immutable R2 publication seam."""
import os
from pathlib import Path
import time

from .chords import LocalStore, canonical, digest, object_path
from .mechanism_cloud import output_inventory
from .record_archive import archive_ref, publish_archive, unpack_archive, MAX_FILES, MAX_RAW_BYTES
from .multipart import UploadState
from .streams import file_ref, read_small
from .reload_launch import read_json
from .runpod_staging import RunpodVolumeConfig, RunpodStager
from .streams import ObjectRef, checked, CHUNK_BYTES


def missing(error):
    return getattr(error,'response',{}).get('Error',{}).get('Code') in ('NoSuchKey','NotFound','404')


def abort_publication(publisher,state):
    """Exact journaled upload only; absent upload after completion/abort is safe."""
    try:publisher.abort(state)
    except Exception as error:
        if getattr(error,'response',{}).get('Error',{}).get('Code')!='NoSuchUpload':raise
    try:
        publisher.client.list_parts(Bucket=publisher.bucket,Key=publisher.key(state.ref.sha256),UploadId=state.upload_id)
    except Exception as error:
        if getattr(error,'response',{}).get('Error',{}).get('Code')=='NoSuchUpload':return
        raise
    raise ValueError('R2 multipart absence not verified')


class Evidence:
    def __init__(self,config,client):
        self.stager=RunpodStager(config,client)
        self.client=client;self.config=config

    def _read(self,key,limit):
        response=self.client.get_object(Bucket=self.config.volume_id,Key=key)
        body=response['Body']
        try:
            if response['ContentLength']>limit: raise ValueError('Evidence metadata exceeds bound')
            raw=body.read(limit+1)
            if len(raw)>limit or len(raw)!=response['ContentLength']: raise ValueError('Evidence metadata length differs')
            return raw
        finally: body.close()

    @staticmethod
    def base(prepared):
        return 'coppertop/runs/'+prepared['plan']['config']['run_id']

    def stage_plan(self,prepared):
        if self.config.datacenter!=prepared['plan']['config']['datacenter']:
            raise ValueError('Plan and volume datacenter differ')
        key=self.base(prepared)+'/plan.json';raw=canonical(prepared)
        if len(raw)>65536: raise ValueError('Plan exceeds transport bound')
        self.client.put_object(Bucket=self.config.volume_id,Key=key,Body=raw)
        if self._read(key,65536)!=raw: raise ValueError('Plan staging readback differs')

    def wait_receipt(self,prepared,pod_id,seconds):
        import json
        until=time.monotonic()+seconds
        while time.monotonic()<until:
            try: raw=self._read(self.base(prepared)+'/ready.json',1024)
            except Exception as error:
                if not missing(error): raise
                time.sleep(2);continue
            receipt=json.loads(raw)
            if (set(receipt)!={'pod_id','plan_sha256','status','record_sha256','archive','durable_archive_verified'}
                or receipt['pod_id']!=pod_id or receipt['plan_sha256']!=prepared['plan_sha256']
                or receipt['status'] not in ('passed','failed')):
                raise ValueError('Sealed evidence identity differs')
            ObjectRef(receipt['record_sha256'],0)
            if receipt['durable_archive_verified'] is not True:raise ValueError('Archive stage acknowledgement required')
            archive_ref({'schema_version':1,'format':'coppertop-episode-tar','archive':receipt['archive']})
            return receipt
        raise TimeoutError('RunPod evidence readiness deadline')

    def upload_state(self,prepared):
        import json
        try:raw=self._read(self.base(prepared)+'/upload-state.json',65536)
        except Exception as error:
            if missing(error):return None
            raise
        return UploadState.from_dict(json.loads(raw))

    def publish(self,prepared,receipt,directory,publisher,*,resume=False):
        """GPU must be absent. Preserve one verified archive and safely restored originals."""
        import json
        started=time.monotonic()
        base=self.base(prepared)
        raw=self._read(base+'/record.json',65536)
        if digest(raw)!=receipt['record_sha256']: raise ValueError('Record checksum differs')
        record=json.loads(raw)
        if canonical(record)!=raw: raise ValueError('Sealed record encoding differs')
        if (record.get('pod_id')!=receipt['pod_id'] or record.get('plan_sha256')!=prepared['plan_sha256']
            or record.get('status')!=receipt['status'] or record.get('run_id')!=prepared['plan']['config']['run_id']):
            raise ValueError('Record identity differs')
        if record['status']=='passed':
            result=record.get('result',{})
            controls=result.get('controls',0)
            if (result.get('status')!='recording_acceptance' or type(controls) is not int or not 1<=controls<=600
                or result.get('query_calls')!=(controls+15)//16 or result.get('source_frames')!=controls+1
                or type(result.get('query_calls')) is not int or type(result.get('source_frames')) is not int
                or result.get('horizon_reached')!=(controls==600) or result.get('real_simulation') is not True
                or result.get('cadence')!=16 or result.get('horizon')!=600
                or any(type(result.get(name)) is not bool for name in ('success','terminated','truncated','horizon_reached'))
                or not any(result[name] for name in ('success','terminated','truncated','horizon_reached'))):
                raise ValueError('Complete full rollout result differs')
        if (type(record.get('object_count')) is not int or not 1<=record['object_count']<=6000
            or type(record.get('object_bytes')) is not int or not 0<=record['object_bytes']<=MAX_RAW_BYTES):
            raise ValueError('Evidence aggregate declaration differs')
        descriptor=record.get('archive',{})
        ref=archive_ref(descriptor)
        if (record.get('durable_archive_verified') is not True or receipt.get('durable_archive_verified') is not True
            or receipt.get('archive')!=ref.to_dict()
            or type(descriptor.get('file_count')) is not int or not 1<=descriptor['file_count']<=MAX_FILES
            or type(descriptor.get('raw_bytes')) is not int or not 0<descriptor['raw_bytes']<=MAX_RAW_BYTES):
            raise ValueError('Bounded verified archive checkpoint required')
        ObjectRef(descriptor['manifest_sha256'],0)
        # The local bundle, record and restored original files survive any R2 timeout.
        from .runpod_cloud import save
        directory=Path(directory);directory.mkdir(parents=True,exist_ok=True)
        save(directory/'source-record.json',record);save(directory/'source-receipt.json',receipt)
        archive=directory/'episode.tar'
        if not archive.exists():
            response=self.client.get_object(Bucket=self.config.volume_id,Key=base+'/archive/episode.tar')
            body=response['Body']
            try:
                if response['ContentLength']!=ref.size:raise ValueError('Archive length differs')
                partial=directory/'episode.tar.partial'
                with partial.open('xb') as target:
                    for chunk in checked(iter(lambda:body.read(CHUNK_BYTES),b''),ref):target.write(chunk)
                    target.flush();os.fsync(target.fileno())
                partial.replace(archive)
            finally:body.close()
        if file_ref(archive)!=ref:raise ValueError('Local archive checksum differs')
        restored=directory/'outputs'
        marker=directory/'fetch-complete.json'
        if restored.exists():
            previous=read_json(marker,1024)
            if previous.get('archive')!=ref.to_dict() or previous.get('record_sha256')!=receipt['record_sha256']:
                raise ValueError('Existing restoration lacks matching complete fetch receipt')
        else:unpack_archive(archive,descriptor,restored)
        # Safe unpack verifies all original files. Verify the native CAS inventory
        # and accepted release attribution as well, before any R2 mutation.
        local=LocalStore(restored/'store')
        inventory=record.get('inventory');ObjectRef(inventory,0)
        inventory_raw=read_small(local,inventory,1024**2);objects=json.loads(inventory_raw)
        if not isinstance(objects,dict):raise ValueError('Evidence inventory differs')
        objects[inventory]=len(inventory_raw)
        if (len(objects)!=record.get('object_count') or sum(objects.values())!=record.get('object_bytes')
            or len(objects)>6000 or any(type(size) is not int or not 0<=size<=64*1024**2 for size in objects.values())
            or sum(objects.values())>MAX_RAW_BYTES):raise ValueError('Evidence aggregate or inventory differs')
        for key,size in objects.items():ObjectRef(key,size)
        if output_inventory(restored,full=True)!=objects:raise ValueError('Restored CAS inventory differs')
        if record['status']=='passed' and record['result'].get('release') not in objects:
            raise ValueError('Accepted Chords release absent from sealed inventory')
        if not marker.exists():save(marker,{'plan_sha256':prepared['plan_sha256'],
            'record_sha256':receipt['record_sha256'],'archive':ref.to_dict(),'object_count':len(objects),
            'object_bytes':sum(objects.values()),'checksums_verified':True,'full_publication_verified':False,
            'restored_directory':str(restored)})
        journal=directory/'upload-state.json'
        remote_upload=self.upload_state(prepared)
        if (journal.exists() or remote_upload is not None) and not resume:raise ValueError('Explicit multipart resume approval required')
        upload=UploadState.from_dict(read_json(journal,65536)) if journal.exists() else remote_upload
        def persist(value):
            raw=canonical(value)
            if len(raw)>65536:raise ValueError('Multipart journal exceeds metadata bound')
            save(journal,value)
            key=base+'/upload-state.json'
            self.client.put_object(Bucket=self.config.volume_id,Key=key,Body=raw)
            if self._read(key,65536)!=raw:raise ValueError('Staged multipart journal commit not verified')
        save(directory/'r2-attempt.json',{'archive':ref.to_dict(),'publication_verified':False})
        publication=publish_archive(archive,descriptor,publisher,record=record,state=upload,
            persist=persist,concurrency=4,publisher_started=started)
        publication['archive_verified']=True
        save(directory/'publication-complete.json',publication)
        return publication

    def recovery_receipt(self,prepared,pod_id):
        """Only sealed record bytes are recoverable; an absent ready marker is explicit."""
        import json
        raw=self._read(self.base(prepared)+'/record.json',65536)
        record=json.loads(raw)
        if (record.get('pod_id')!=pod_id or record.get('plan_sha256')!=prepared['plan_sha256']
            or record.get('status') not in ('passed','failed')):
            raise ValueError('Recovery record identity differs')
        return {'pod_id':pod_id,'plan_sha256':prepared['plan_sha256'],
                'status':record['status'],'record_sha256':digest(raw),
                'archive':archive_ref(record['archive']).to_dict(),'durable_archive_verified':record.get('durable_archive_verified')}

    def abort_incomplete(self):
        """Owned volume only; a bounded list, including unjournaled upload IDs."""
        count=0;markers={}
        while True:
            response=self.client.list_multipart_uploads(Bucket=self.config.volume_id,MaxUploads=100,**markers)
            for upload in response.get('Uploads',[]):
                count+=1
                if count>100: raise ValueError('Incomplete upload inventory exceeds bound')
                self.client.abort_multipart_upload(Bucket=self.config.volume_id,Key=upload['Key'],UploadId=upload['UploadId'])
            if not response.get('IsTruncated'): break
            markers={'KeyMarker':response['NextKeyMarker'],'UploadIdMarker':response['NextUploadIdMarker']}
        if self.client.list_multipart_uploads(Bucket=self.config.volume_id,MaxUploads=1).get('Uploads'):
            raise ValueError('Incomplete uploads remain')

    def stage_size(self,prepared):
        """One bounded page for the six-file archive stage, never dataset recursion."""
        prefix=self.base(prepared)+'/'
        response=self.client.list_objects_v2(Bucket=self.config.volume_id,Prefix=prefix,MaxKeys=32)
        entries=response.get('Contents',[])
        if response.get('IsTruncated') or len(entries)>32:raise ValueError('Stage inventory exceeds 32-entry bound')
        seen=set();observed=0
        for entry in entries:
            key,size=entry['Key'],entry['Size']
            if (not isinstance(key,str) or not key.startswith(prefix) or key in seen
                or type(size) is not int or size<0):raise ValueError('Invalid retained stage entry')
            seen.add(key);observed+=size
        return {'size_bytes':observed,'within_size_authorization':observed<=prepared['plan']['limits']['retained_bytes'],
                'scope':prefix,'entire_allocated_volume_usage_measured':False}


def runpod_s3_access_key(values):
    current=values.get('RUNPOD_S3_ACCESS_KEY_ID')
    existing=values.get('RUNPOD_S3_ACCESS_KEY')
    if current and existing and current!=existing:raise ValueError('Conflicting RunPod S3 identity aliases')
    if not (current or existing):raise ValueError('Existing RunPod S3 identity required')
    return current or existing


def make_transports(*,publication=True):
    """Explicit credentials supplied after approval; never mint or send them to GPU."""
    import boto3
    from botocore.config import Config
    from .runpod_cloud import RestAPI
    from .s3 import S3Store
    from datetime import datetime,timezone
    options=Config(connect_timeout=10,read_timeout=20,retries={'total_max_attempts':1},
                   s3={'addressing_style':'path'})
    api=RestAPI(os.environ['RUNPOD_API_KEY'])
    datacenter=os.environ['RUNPOD_DATACENTER']
    def evidence(volume_id):
        config=RunpodVolumeConfig(volume_id,datacenter,f'https://s3api-{datacenter.lower()}.runpod.io/')
        client=boto3.client('s3',endpoint_url=config.endpoint,region_name=datacenter,config=options,
            aws_access_key_id=runpod_s3_access_key(os.environ),aws_secret_access_key=os.environ['RUNPOD_S3_SECRET_ACCESS_KEY'])
        return Evidence(config,client)
    if not publication:return api,evidence,None
    expires=datetime.fromisoformat(os.environ['R2_TEMP_EXPIRES_UTC'])
    if expires.tzinfo is None:raise ValueError('Explicit R2 session expiration required')
    # These must be an already approved one-hour session scoped to the exact prefix.
    from .s3 import R2Config
    r2=R2Config(os.environ['R2_ENDPOINT'],os.environ['R2_BUCKET'],os.environ['R2_PREFIX'])
    client=boto3.client('s3',endpoint_url=r2.endpoint,region_name='auto',config=options,
        aws_access_key_id=os.environ['R2_TEMP_ACCESS_KEY_ID'],aws_secret_access_key=os.environ['R2_TEMP_SECRET_ACCESS_KEY'],
        aws_session_token=os.environ['R2_TEMP_SESSION_TOKEN'])
    from .r2_session import BoundedClient,SessionScope
    scope=SessionScope('mechanism_archive',r2.bucket,r2.prefix)
    guarded=BoundedClient(client,scope,r2.endpoint,expires.timestamp())
    publisher=S3Store(guarded,r2.bucket,prefix=r2.prefix)
    publisher.session_expires_utc=expires
    return api,evidence,publisher
