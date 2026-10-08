"""External CPU RunPod S3 handoff and existing immutable R2 publication seam."""
import os
from pathlib import Path
import time

from .chords import LocalStore, canonical, digest, object_path
from .mechanism_cloud import publish_directory
from .reload_launch import read_json
from .runpod_staging import RunpodVolumeConfig, RunpodStager
from .streams import ObjectRef, checked, CHUNK_BYTES


def missing(error):
    return getattr(error,'response',{}).get('Error',{}).get('Code') in ('NoSuchKey','NotFound','404')


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
            if (set(receipt)!={'pod_id','plan_sha256','status','record_sha256'}
                or receipt['pod_id']!=pod_id or receipt['plan_sha256']!=prepared['plan_sha256']
                or receipt['status'] not in ('passed','failed')):
                raise ValueError('Sealed evidence identity differs')
            ObjectRef(receipt['record_sha256'],0)
            return receipt
        raise TimeoutError('RunPod evidence readiness deadline')

    def publish(self,prepared,receipt,directory,publisher):
        """GPU must already be absent. Fetch only checksum-addressed sealed objects."""
        import json
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
        inventory=record.get('inventory')
        ObjectRef(inventory,0)
        inventory_raw=self._read(base+'/outputs/store/'+object_path(inventory),1024**2)
        if digest(inventory_raw)!=inventory: raise ValueError('Inventory checksum differs')
        objects=json.loads(inventory_raw)
        if not isinstance(objects,dict): raise ValueError('Evidence inventory differs')
        objects[inventory]=len(inventory_raw)
        if len(objects)!=record.get('object_count') or sum(objects.values())!=record.get('object_bytes'):
            raise ValueError('Inventory totals differ')
        if (not isinstance(objects,dict) or len(objects)+2>6000
            or any(type(size) is not int or not 0<=size<=64*1024**2 for size in objects.values())
            or sum(objects.values())+2*65536>1024**3):
            raise ValueError('Evidence aggregate publication bound exceeded')
        # All identities and aggregate bounds are checked before any local/R2 write.
        for key,size in objects.items(): ObjectRef(key,size)
        if record['status']=='passed' and record['result'].get('release') not in objects:
            raise ValueError('Accepted Chords release absent from sealed inventory')
        from .runpod_cloud import save
        # Preserve the control record as well as payloads on external CPU before
        # shared publication begins. A GPU/volume deletion cannot erase this copy.
        save(Path(directory)/'source-record.json',record)
        save(Path(directory)/'source-receipt.json',receipt)
        local=LocalStore(Path(directory)/'store')
        for key,size in objects.items():
            response=self.client.get_object(Bucket=self.config.volume_id,Key=base+'/outputs/store/'+object_path(key))
            body=response['Body']
            try:
                if response['ContentLength']!=size: raise ValueError('Evidence object length differs')
                ref=ObjectRef(key,size)
                chunks=iter(lambda:body.read(CHUNK_BYTES),b'')
                local.put_stream(ref,checked(chunks,ref))
            finally: body.close()
        save(Path(directory)/'fetch-complete.json',{'plan_sha256':prepared['plan_sha256'],
            'record_sha256':receipt['record_sha256'],'object_count':len(objects),
            'object_bytes':sum(objects.values()),'checksums_verified':True,
            'full_publication_verified':False})
        return publish_directory(Path(directory),record,publisher,full=True)

    def recovery_receipt(self,prepared,pod_id):
        """Only sealed record bytes are recoverable; an absent ready marker is explicit."""
        import json
        raw=self._read(self.base(prepared)+'/record.json',65536)
        record=json.loads(raw)
        if (record.get('pod_id')!=pod_id or record.get('plan_sha256')!=prepared['plan_sha256']
            or record.get('status') not in ('passed','failed')):
            raise ValueError('Recovery record identity differs')
        return {'pod_id':pod_id,'plan_sha256':prepared['plan_sha256'],
                'status':record['status'],'record_sha256':digest(raw)}

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


def runpod_s3_access_key(values):
    current=values.get('RUNPOD_S3_ACCESS_KEY_ID')
    existing=values.get('RUNPOD_S3_ACCESS_KEY')
    if current and existing and current!=existing:raise ValueError('Conflicting RunPod S3 identity aliases')
    if not (current or existing):raise ValueError('Existing RunPod S3 identity required')
    return current or existing


def make_transports():
    """Explicit credentials supplied after approval; never mint or send them to GPU."""
    import boto3
    from botocore.config import Config
    from .runpod_cloud import RestAPI
    from .s3 import S3Store
    from datetime import datetime,timezone
    expires=datetime.fromisoformat(os.environ['R2_TEMP_EXPIRES_UTC'])
    if expires.tzinfo is None: raise ValueError('Explicit R2 session expiration required')
    options=Config(connect_timeout=10,read_timeout=20,retries={'total_max_attempts':1},
                   s3={'addressing_style':'path'})
    api=RestAPI(os.environ['RUNPOD_API_KEY'])
    datacenter=os.environ['RUNPOD_DATACENTER']
    def evidence(volume_id):
        config=RunpodVolumeConfig(volume_id,datacenter,f'https://s3api-{datacenter.lower()}.runpod.io/')
        client=boto3.client('s3',endpoint_url=config.endpoint,region_name=datacenter,config=options,
            aws_access_key_id=runpod_s3_access_key(os.environ),aws_secret_access_key=os.environ['RUNPOD_S3_SECRET_ACCESS_KEY'])
        return Evidence(config,client)
    # These must be an already approved one-hour session scoped to the exact prefix.
    from .s3 import R2Config
    r2=R2Config(os.environ['R2_ENDPOINT'],os.environ['R2_BUCKET'],os.environ['R2_PREFIX'])
    client=boto3.client('s3',endpoint_url=r2.endpoint,region_name='auto',config=options,
        aws_access_key_id=os.environ['R2_TEMP_ACCESS_KEY_ID'],aws_secret_access_key=os.environ['R2_TEMP_SECRET_ACCESS_KEY'],
        aws_session_token=os.environ['R2_TEMP_SESSION_TOKEN'])
    publisher=S3Store(client,r2.bucket,prefix=r2.prefix)
    publisher.session_expires_utc=expires
    return api,evidence,publisher
