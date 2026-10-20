"""Local synthetic R2 validation. Preparation is offline; execution needs approval.

The publisher is a killable child. Cleanup never races its upload threads and uses
an independently signed capability for only the locally journaled object keys.
"""
import argparse
import faulthandler
from dataclasses import dataclass
import hashlib
import json
import os
from pathlib import Path
import re
import subprocess
import sys
import time
from threading import RLock
from urllib.parse import parse_qs, unquote, urlsplit

from .chords import canonical
from . import record_archive
from .r2_session import SessionScope, BoundedClient, mint, _endpoint, _session
from .s3 import S3Store, _code,STREAMED_PUT_BYTES
from .streams import file_ref
from .publication_trace import phase,error_details,response_details

FILES = 1939
RAW_BYTES = 154493961
REMOTE_BYTES = 150*1024**2+65536
EXECUTION_SECONDS = 600
CLEANUP_SECONDS = 300
SESSION_SECONDS = 1200


def implementation_sha256():
    root=Path(__file__).parent
    files=('archive_validation.py','record_archive.py','s3.py','r2_session.py','reload_launch.py','publication_trace.py')
    return hashlib.sha256(canonical({name:hashlib.sha256((root/name).read_bytes()).hexdigest() for name in files})).hexdigest()


def save(path, value):
    path = Path(path)
    raw=canonical(value)
    if len(raw)>65536: raise ValueError('Validation metadata exceeds bound')
    temporary = path.with_suffix('.partial')
    with temporary.open('wb') as stream:
        stream.write(raw); stream.flush(); os.fsync(stream.fileno())
    temporary.replace(path)
    handle=os.open(path.parent,os.O_RDONLY)
    try: os.fsync(handle)
    finally: os.close(handle)


def load(path):
    with Path(path).open('rb') as stream:
        raw = stream.read(65537)
    if len(raw)>65536:
        raise ValueError('Validation metadata exceeds bound')
    return json.loads(raw)


class DurableTrace:
    def __init__(self,path):
        self.path=Path(path);self.lock=RLock();self.events=[]
        with self.path.open('x'):pass
        self.record({'stage':'trace','status':'started'})

    def record(self,event):
        with self.lock:
            if len(self.events)>=192:raise ValueError('Publication telemetry event cap exhausted')
            self.events.append(dict(event,utc_epoch=time.time()))
            save(self.path,{'events':self.events})


def lower_limit(value, ceiling):
    if type(value) is not int or not 1 <= value <= ceiling:
        raise ValueError('Validation limit must be a positive integer within the original ceiling')
    return value


def prepare(directory, *, execution_seconds=EXECUTION_SECONDS, publication_requests_max=32):
    execution_seconds = lower_limit(execution_seconds, EXECUTION_SECONDS)
    publication_requests_max = lower_limit(publication_requests_max, 32)
    directory = Path(directory).resolve()
    directory.parent.mkdir(parents=True,exist_ok=True)
    directory.mkdir(mode=0o700)  # Never adopt an existing run or its objects.
    source = directory/'source'; source.mkdir()
    unit, remainder = divmod(RAW_BYTES, FILES)
    for number in range(FILES):
        path = source/f'camera-state/{number:06}.bin'; path.parent.mkdir(exist_ok=True)
        path.write_bytes(bytes([number%251])*(unit+(number<remainder)))
    descriptor = record_archive.seal_directory(source, directory/'episode.tar')
    if descriptor['archive']['size']>REMOTE_BYTES-65536:
        raise ValueError('Synthetic archive exceeds approved size')
    prefix = 'chords/mechanism/mechanism-'+os.urandom(8).hex()
    plan = {'kind':'coppertop-synthetic-archive-validation', 'version':1,
        'implementation_sha256':implementation_sha256(),'python':sys.executable,
        'bucket':'coppertop', 'prefix':prefix, 'directory':str(directory),
        'files':FILES, 'raw_bytes':RAW_BYTES, 'descriptor':descriptor,
        'execution_seconds':execution_seconds, 'cleanup_seconds':CLEANUP_SECONDS,
        'publisher_session_seconds':SESSION_SECONDS, 'cleanup_session_seconds':SESSION_SECONDS,
        'publication_requests_max':publication_requests_max, 'cleanup_requests_max':16,
        'storage_class':'STANDARD',
        'single_put_threshold_bytes':STREAMED_PUT_BYTES,
        'remote_bytes_max':REMOTE_BYTES, 'cloud_compute_usd_max':'0',
        'r2_invoice_allowance_usd':'5', 'automatic_retries':0,
        'cleanup':'abort_owned_upload_delete_exact_test_keys_verify_absence',
        'retention':'local_source_preserved_remote_cleanup_failure_reported_no_expiry_guarantee'}
    save(directory/'plan.json',plan)
    approval = {'plan_sha256':hashlib.sha256(canonical(plan)).hexdigest(),
        'execute':False, 'derive_publisher_session':False, 'derive_exact_cleanup_session':False,
        'delete_synthetic_remote_objects_on_failure':False, 'r2_invoice_allowance_usd':'5',
        'accept_cleanup_failure_storage_until_operator_resolution':False}
    save(directory/'approval.example.json',approval)
    return approval['plan_sha256']


def validate_plan(plan, directory):
    if (plan.get('kind')!='coppertop-synthetic-archive-validation' or plan.get('version')!=1
        or plan.get('bucket')!='coppertop' or plan.get('directory')!=str(Path(directory).resolve())
        or not re.fullmatch(r'chords/mechanism/mechanism-[0-9a-f]{16}',plan.get('prefix',''))):
        raise ValueError('Validation ownership differs')
    expected = {'files':FILES,'raw_bytes':RAW_BYTES,
        'execution_seconds':lower_limit(plan.get('execution_seconds'), EXECUTION_SECONDS),
        'cleanup_seconds':CLEANUP_SECONDS,'publisher_session_seconds':SESSION_SECONDS,
        'cleanup_session_seconds':SESSION_SECONDS,
        'publication_requests_max':lower_limit(plan.get('publication_requests_max'), 32),
        'cleanup_requests_max':16,'remote_bytes_max':REMOTE_BYTES,'cloud_compute_usd_max':'0',
        'storage_class':'STANDARD',
        'single_put_threshold_bytes':STREAMED_PUT_BYTES,
        'r2_invoice_allowance_usd':'5','automatic_retries':0,
        'cleanup':'abort_owned_upload_delete_exact_test_keys_verify_absence',
        'retention':'local_source_preserved_remote_cleanup_failure_reported_no_expiry_guarantee'}
    expected.update({'implementation_sha256':implementation_sha256(),'python':sys.executable})
    if set(plan)!={'kind','version','bucket','prefix','directory','descriptor'}|set(expected) or any(plan.get(k)!=v for k,v in expected.items()):
        raise ValueError('Validation bounds differ')
    ref = record_archive.archive_ref(plan['descriptor'])
    if ref.size>REMOTE_BYTES-65536 or file_ref(Path(directory)/'episode.tar')!=ref:
        raise ValueError('Prepared archive changed')


def validate_approval(plan, approval):
    expected = {'plan_sha256':hashlib.sha256(canonical(plan)).hexdigest(), 'execute':True,
        'derive_publisher_session':True,'derive_exact_cleanup_session':True,
        'delete_synthetic_remote_objects_on_failure':True,'r2_invoice_allowance_usd':'5',
        'accept_cleanup_failure_storage_until_operator_resolution':True}
    if set(approval)!=set(expected) or any(type(approval[k]) is not type(v) or approval[k]!=v for k,v in expected.items()):
        raise ValueError('Exact live validation and cleanup authorization required')


@dataclass(frozen=True)
class CleanupScope(SessionScope):
    """Separate exact-object capability; no write, download or bucket listing."""
    def __post_init__(self):
        publisher=SessionScope('mechanism_archive',self.bucket,self.prefix)
        if self.role!='archive_validation_cleanup' or not 1<=len(self.object_paths)<=2 or len(set(self.object_paths))!=len(self.object_paths):
            raise ValueError('At most two exact cleanup objects required')
        for key in self.object_paths:
            if not publisher.allows(key):
                raise ValueError('Cleanup object outside owned prefix')

    @property
    def actions(self):
        return ('HeadObject','DeleteObject','AbortMultipartUpload','ListParts')

    @property
    def paths(self):
        return {'objectPaths':list(self.object_paths),'prefixPaths':[]}

    def allows(self,key):
        return key in self.object_paths


def sdk_client(endpoint, credentials):
    import boto3
    from botocore.config import Config
    return boto3.client('s3',endpoint_url=endpoint,region_name='auto',
        aws_access_key_id=credentials.access_key_id,aws_secret_access_key=credentials.secret_access_key,
        aws_session_token=credentials.session_token,
        config=Config(connect_timeout=30,read_timeout=60,retries={'total_max_attempts':1},
                      s3={'addressing_style':'path'}))


class JournalClient:
    def __init__(self, client, plan, directory,observe=lambda event:None):
        self.client,self.plan,self.directory = client,plan,Path(directory)
        self.keys=[]; self.requests=0; self.written=0
        self.multipart_keys=[]
        self.lock=RLock()
        self.observe=observe

    def __getattr__(self, name):
        target = getattr(self.client,name)
        def call(**kwargs):
            with self.lock:
                if self.requests>=self.plan['publication_requests_max']:
                    raise ValueError('Publication request cap exhausted')
                self.requests+=1
            if name in ('create_multipart_upload','put_object'):
                kwargs['StorageClass']='STANDARD'
                key = kwargs['Key']
                # Establish absence before assuming cleanup ownership. Never adopt a
                # matching content hash already present in this random attempt prefix.
                if key not in self.keys:
                    if self.requests>=self.plan['publication_requests_max']:
                        raise ValueError('Publication request cap exhausted')
                    self.requests+=1
                    try:
                        with phase(self.observe,'rpc.ownership_head') as facts:
                            response=self.client.head_object(Bucket=kwargs['Bucket'],Key=key)
                            facts.update(response_details(response))
                    except Exception as error:
                        if _code(error) not in ('NoSuchKey','NotFound','404'): raise
                    else: raise ValueError('Fresh test destination already exists')
                    self.keys.append(key)
                if name=='create_multipart_upload':self.multipart_keys.append(key)
                save(self.directory/'owned-keys.json',{'keys':self.keys,'multipart_keys':self.multipart_keys})
            if name in ('upload_part','put_object'):
                with self.lock:
                    size=len(kwargs['Body']) if type(kwargs['Body']) is bytes else kwargs['ContentLength']
                    self.written+=size
                    if self.written>self.plan['remote_bytes_max']:
                        raise ValueError('Validation remote byte cap exhausted')
            part=kwargs.get('PartNumber')
            suffix='.'+str(part) if part is not None else ''
            with phase(self.observe,'rpc.'+name+suffix) as facts:
                response=target(**kwargs);facts.update(response_details(response))
            if name=='head_object' and kwargs['Key'] not in self.keys:
                raise ValueError('Fresh test destination already exists')
            return response
        return call


def publish_child(directory, endpoint, approval_path):
    directory = Path(directory); plan=load(directory/'plan.json'); validate_plan(plan,directory)
    validate_approval(plan,load(approval_path))
    scope=SessionScope('mechanism_archive',plan['bucket'],plan['prefix'])
    values,expiry=_session(os.environ,endpoint,scope,time.time())
    from .r2_session import TemporaryCredentials
    client=BoundedClient(sdk_client(endpoint,TemporaryCredentials(*values,expiry)),scope,endpoint,expiry)
    trace=DurableTrace(directory/'publisher-telemetry.json')
    journal=JournalClient(client,plan,directory,trace.record)
    store=S3Store(journal,plan['bucket'],prefix=plan['prefix'])
    stacks=(directory/'publisher-stacks.log').open('xb',buffering=0)
    watched={'archive_put','source_recheck','complete_multipart','archive_get_headers','archive_hash_readback',
             'receipt_put','receipt_verify','final_receipt_verify'}
    stack_stage=None
    def observe(event):
        nonlocal stack_stage
        trace.record(event)
        if event['stage'] in watched:
            if event['status']=='started':
                # One in-process snapshot per long phase; frame locals and
                # credentials are excluded. This is not a cloud scheduler.
                faulthandler.dump_traceback_later(60,repeat=False,file=stacks)
                stack_stage=event['stage']
            elif event['status'] in ('completed','failed') and stack_stage==event['stage']:
                faulthandler.cancel_dump_traceback_later();stack_stage=None
    try:
        result=record_archive.publish_archive(directory/'episode.tar',plan['descriptor'],store,
            record={'synthetic':True,'files':FILES,'raw_bytes':RAW_BYTES},
            persist=lambda state:save(directory/'upload-state.json',state),observe=observe)
        with phase(observe,'final_receipt_verify'):
            record_archive.read_completion(store,result['report_key'])
        save(directory/'publication.json',{'verified':True,'result':result,
            'requests':journal.requests,'write_bytes':journal.written})
    except Exception as error:
        save(directory/'publication.json',{'verified':False,**error_details(error),
            'requests':journal.requests,'write_bytes':journal.written})
        raise
    finally:
        faulthandler.cancel_dump_traceback_later();stacks.close()


class CleanupClient:
    def __init__(self,client,scope,endpoint,expiry,deadline,clock=time.time,monotonic=time.monotonic,
                 observe=lambda event:None):
        self.client,self.scope,self.endpoint=client,scope,endpoint
        self.expiry,self.deadline,self.clock,self.monotonic=expiry,deadline,clock,monotonic
        self.requests=0
        self.observe=observe
        client.meta.events.register('before-send.s3',self.guard)

    def guard(self,request,**unused):
        self.live()
        url=urlsplit(request.url); host,_=_endpoint(self.endpoint)
        key=unquote(url.path).removeprefix('/'+self.scope.bucket+'/')
        query=parse_qs(url.query,keep_blank_values=True)
        ordinary=not url.query and request.method in ('HEAD','DELETE')
        multipart=(request.method in ('GET','DELETE') and set(query)=={'uploadId'}
                   and len(query['uploadId'])==1 and 0<len(query['uploadId'][0])<=2048)
        if url.scheme!='https' or url.netloc!=host or url.fragment or not self.scope.allows(key) or not (ordinary or multipart):
            raise ValueError('Cleanup destination or operation differs')

    def live(self):
        if self.clock()>=self.expiry or self.monotonic()>=self.deadline:
            raise TimeoutError('Cleanup session or deadline expired')

    def call(self,name,**kwargs):
        self.live()
        if name not in ('head_object','delete_object','abort_multipart_upload','list_parts') or kwargs.get('Bucket')!=self.scope.bucket or not self.scope.allows(kwargs.get('Key')):
            raise ValueError('Cleanup outside exact owned objects')
        if self.requests>=16: raise ValueError('Cleanup request cap exhausted')
        self.requests+=1
        with phase(self.observe,'cleanup.'+name) as facts:
            response=getattr(self.client,name)(**kwargs)
            facts.update(response_details(response));return response


def cleanup_owned(client,plan,keys,state,*,multipart_expected=True):
    """Attempt every exact-key cleanup; ambiguity is an explicit failed result."""
    errors=[]
    if state:
        from .multipart import UploadState
        try:
            saved=UploadState.from_dict(state)
            key=saved.destination.removeprefix(plan['bucket']+'/')
            if key not in keys or saved.destination!=plan['bucket']+'/'+key or saved.ref!=record_archive.archive_ref(plan['descriptor']) or len(saved.upload_id)>2048:
                raise ValueError('Upload journal outside approved archive')
        except Exception as error:
            errors.append({'operation':'validate_upload',**error_details(error)})
        else:
            try:
                client.call('abort_multipart_upload',Bucket=plan['bucket'],Key=key,UploadId=saved.upload_id)
            except Exception as error:
                if _code(error)!='NoSuchUpload': errors.append({'operation':'abort',**error_details(error)})
            try:
                client.call('list_parts',Bucket=plan['bucket'],Key=key,UploadId=saved.upload_id)
            except Exception as error:
                if _code(error)!='NoSuchUpload': errors.append({'operation':'verify_abort',**error_details(error)})
            else: errors.append({'operation':'verify_abort','error_type':'UploadStillExists'})
    elif keys and multipart_expected:
        # A lost CreateMultipartUpload response leaves no upload ID. Exact-object
        # listing cannot discover it; broader bucket listing is deliberately denied.
        errors.append({'operation':'verify_upload','error_type':'UploadIdentityUnknown'})
    for key in keys:
        try:
            client.call('delete_object',Bucket=plan['bucket'],Key=key)
            try: client.call('head_object',Bucket=plan['bucket'],Key=key)
            except Exception as error:
                if _code(error) not in ('NoSuchKey','NotFound','404'): raise
            else: raise ValueError('Deleted test object still exists')
        except Exception as error:
            errors.append({'operation':'delete_verify','key':key,**error_details(error)})
    return {'verified':not errors,'keys':keys,'errors':errors,'requests':client.requests,
            'source_preserved':True,'remote_bytes_max':plan['remote_bytes_max']}


def run_publisher(command,environment,*,runner=subprocess.run,execution_seconds=EXECUTION_SECONDS):
    """subprocess.run kills and waits on timeout before cleanup may start."""
    execution_seconds = lower_limit(execution_seconds, EXECUTION_SECONDS)
    try:
        process=runner(command,env=environment,timeout=execution_seconds,
                       cwd=str(Path(__file__).resolve().parent.parent),
                       stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL)
        return {'exit_code':process.returncode,'timed_out':False}
    except subprocess.TimeoutExpired:
        return {'exit_code':None,'timed_out':True}
    except (Exception,KeyboardInterrupt) as error:
        return {'exit_code':None,'timed_out':False,'error_type':type(error).__name__}


def execute(directory,approval_path,env_file):
    directory=Path(directory).resolve(); plan=load(directory/'plan.json')
    validate_plan(plan,directory); validate_approval(plan,load(approval_path))
    # Prevent reusing this prefix even when execution fails before first upload.
    with (directory/'execution-started.json').open('x') as stream:
        json.dump({'started_utc_epoch':time.time()},stream)
    from .reload_launch import selected_env,parent_credentials,hard_deadline
    with hard_deadline(120):
        values=selected_env(env_file); access,secret=parent_credentials(values)
    endpoint=values['CLOUDFLARE_R2_URL']
    scope=SessionScope('mechanism_archive',plan['bucket'],plan['prefix'])
    credentials=mint(access,secret,endpoint=endpoint,scope=scope,ttl_seconds=SESSION_SECONDS)
    environment={key:value for key,value in os.environ.items() if not any(x in key.upper() for x in ('TOKEN','SECRET','ACCESS_KEY','API_KEY'))}
    environment.update(credentials.environment())
    outcome=run_publisher([sys.executable,'-m','coppertop.archive_validation','--child',str(directory),
                          '--endpoint',endpoint,'--approval',str(Path(approval_path).resolve())],environment,
                          execution_seconds=plan['execution_seconds'])
    ownership=load(directory/'owned-keys.json') if (directory/'owned-keys.json').exists() else {'keys':[],'multipart_keys':[]}
    keys=ownership['keys']
    if not isinstance(keys,list) or len(keys)>2 or len(set(keys))!=len(keys) or any(not scope.allows(key) for key in keys):
        raise ValueError('Owned-key journal outside prepared prefix')
    multipart_keys=ownership.get('multipart_keys',keys[:1])
    if not isinstance(multipart_keys,list) or len(multipart_keys)>1 or any(key not in keys for key in multipart_keys):
        raise ValueError('Multipart ownership differs')
    state_error=None
    try: state=load(directory/'upload-state.json') if (directory/'upload-state.json').exists() else None
    except Exception as error:
        state=None;state_error=type(error).__name__
    if keys:
        cleanup_scope=CleanupScope('archive_validation_cleanup',plan['bucket'],plan['prefix'],tuple(keys))
        try:
            with hard_deadline(CLEANUP_SECONDS):
                cleanup_credentials=mint(access,secret,endpoint=endpoint,scope=cleanup_scope,ttl_seconds=SESSION_SECONDS)
                client=CleanupClient(sdk_client(endpoint,cleanup_credentials),cleanup_scope,endpoint,
                                     cleanup_credentials.expires_at,time.monotonic()+CLEANUP_SECONDS,
                                     observe=DurableTrace(directory/'cleanup-telemetry.json').record)
                cleanup=cleanup_owned(client,plan,keys,state,multipart_expected=bool(multipart_keys))
                if state_error:
                    cleanup['verified']=False
                    cleanup['errors'].append({'operation':'read_upload_journal','error_type':state_error})
        except Exception as error:
            cleanup={'verified':False,'keys':keys,**error_details(error),'source_preserved':True}
    else: cleanup={'verified':True,'keys':[],'source_preserved':True,'requests':0}
    publication=load(directory/'publication.json') if (directory/'publication.json').exists() else {'verified':False}
    result={'publisher_process':outcome,'publication':publication,'cleanup':cleanup,
            'local_directory':str(directory),'prefix':plan['prefix'],'bucket':plan['bucket']}
    save(directory/'result.json',result)
    return result


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    mode=parser.add_mutually_exclusive_group(required=True)
    mode.add_argument('--prepare',type=Path); mode.add_argument('--execute',type=Path)
    mode.add_argument('--child',type=Path,help=argparse.SUPPRESS)
    parser.add_argument('--approval',type=Path);parser.add_argument('--env-file',type=Path)
    parser.add_argument('--endpoint')
    parser.add_argument('--execution-seconds',type=int)
    parser.add_argument('--publication-requests-max',type=int)
    args=parser.parse_args()
    if not args.prepare and (args.execution_seconds is not None or args.publication_requests_max is not None):
        parser.error('Reduced limits belong to --prepare and are enforced by the approved plan')
    if args.prepare:
        print(json.dumps({'plan_sha256':prepare(args.prepare,
            execution_seconds=EXECUTION_SECONDS if args.execution_seconds is None else args.execution_seconds,
            publication_requests_max=32 if args.publication_requests_max is None else args.publication_requests_max)}))
    elif args.child:
        if not args.approval or not args.endpoint: parser.error('Internal child requires approval and endpoint')
        publish_child(args.child,args.endpoint,args.approval)
    else:
        if not args.approval or not args.env_file: parser.error('--execute requires --approval and --env-file')
        result=execute(args.execute,args.approval,args.env_file);print(json.dumps(result))
        if not result['publication']['verified'] or not result['cleanup']['verified']: sys.exit(1)


if __name__=='__main__': main()
