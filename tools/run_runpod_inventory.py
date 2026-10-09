"""One approved CPU inventory of an existing immutable Modal image; no build/export."""
import argparse
from datetime import datetime, timedelta, timezone
from decimal import Decimal
import hashlib
import inspect
import json
import os
from pathlib import Path
import re
import signal
import subprocess
import sys
import tempfile
import time
import uuid

IMAGE='im-uMab88d0cwA6MALkcTf5VW'
LIMITS={'cpu_cores':2,'memory_mib':16384,'execution_seconds':900,'startup_seconds':300,
        'cleanup_seconds':180,'controller_seconds':1200,'result_bytes':262144,'retries':0}
TREES=('/opt/conda','/usr/local','/work/sim','/work/xiaomi','/work/robocasa',
       '/work/robosuite','/work/model','/opt/model','/work/build-evidence')


def encoded(value):return (json.dumps(value,sort_keys=True,separators=(',',':'),allow_nan=False)+'\n').encode()
def sha(value):return hashlib.sha256(encoded(value)).hexdigest()


def save(path,value):
    path=Path(path);path.parent.mkdir(parents=True,exist_ok=True)
    with path.with_suffix('.partial').open('wb') as stream:
        stream.write(encoded(value));stream.flush();os.fsync(stream.fileno())
    path.with_suffix('.partial').replace(path)


def prepare():
    rate=(Decimal('0.0000131')*2+Decimal('0.00000222')*16)*Decimal('1.75')
    p={'kind':'runpod_preparation_cpu_inventory','attempt':'inventory-'+uuid.uuid4().hex[:16],
       'image':IMAGE,'environment':'main','region':'us','limits':LIMITS,
       'approved_cap_usd':'0.20','compute_bound_usd':str(rate*1380),
       'metadata_egress_bound_usd':str(Decimal(262144)/Decimal(1024**3)*Decimal('0.04')),
       'launcher_sha256':hashlib.sha256(Path(__file__).read_bytes()).hexdigest(),
       'gpu':None,'secrets':[],'volumes':{},'new_image_builds':False,
       'downloads':False,'bulk_transfers':False,'model_loading':False,
       'approval_basis':'Fresh exact approval required; previous single $0.20 attempt is consumed; no automatic retry',
       'source_pins':{'xiaomi':'0dd7aef8dc87296246aae812a1f59ccb708e5546',
          'robocasa':'456174f62b89b8fca99eaaf33949c29fec9cfc2a',
          'robosuite':'5ce6643f3092639d08f7b0f90ed1c6a84f50552c'}}
    p['app_name']='coppertop-runpod-'+p['attempt']
    return {'plan':p,'plan_sha256':sha(p)}


def interpreter_inventory():
    """Run inside each actual interpreter. Imports do not instantiate model/CUDA."""
    import contextlib, ctypes, importlib.metadata, importlib.util, platform, sysconfig
    versions={}
    for distribution in importlib.metadata.distributions():
        name=re.sub(r'[-_.]+','-',distribution.metadata['Name']).lower()
        version=distribution.version
        if not re.fullmatch(r'[A-Za-z0-9.+!-]{1,128}',version):raise ValueError('Version field invalid')
        if name in versions and versions[name]!=version:raise ValueError('Duplicate package versions')
        versions[name]=version
    if len(versions)>512:raise ValueError('Package inventory exceeds bound')
    result={'executable':sys.executable,'resolved_executable':str(Path(sys.executable).resolve()),
       'prefix':sys.prefix,'base_prefix':sys.base_prefix,'python':platform.python_version(),
       'machine':platform.machine(),'libc':list(platform.libc_ver()),'soabi':sysconfig.get_config_var('SOABI'),
       'stdlib':sysconfig.get_path('stdlib'),'versions':dict(sorted(versions.items())),
       'model_instantiated':False,'cuda_executed':False}
    check=subprocess.run([sys.executable,'-m','pip','--isolated','check'],timeout=60,
                         stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL)
    result['pip_check_exit_code']=check.returncode
    imports={}
    with open(os.devnull,'w') as sink,contextlib.redirect_stdout(sink),contextlib.redirect_stderr(sink):
        names=('torch','torchvision','flash_attn','transformers','av','pyarrow','numpy') if sys.prefix=='/opt/conda' else ('torch','torchvision','mujoco','av','numpy','cv2','robocasa','robosuite')
        for name in names:
            try:
                module=__import__(name);imports[name]={'status':'passed','file':getattr(module,'__file__',None)}
                if name=='torch':
                    result['torch']={'version':module.__version__,'cuda_build':module.version.cuda,
                        'cxx11_abi':bool(module._C._GLIBCXX_USE_CXX11_ABI)}
            except Exception as error:imports[name]={'status':'failed','error_type':type(error).__name__}
    result['imports']=imports
    libraries={}
    for name in ('libEGL.so.1','libOpenGL.so.0','libGL.so.1','libGLESv2.so.2','libGLdispatch.so.0',
                 'libglib-2.0.so.0','libstdc++.so.6','libpython3.11.so.1.0'):
        try:ctypes.CDLL(name);libraries[name]='loaded'
        except OSError:libraries[name]='unavailable'
    result['libraries']=libraries
    paths=sorted({line.split()[-1] for line in Path('/proc/self/maps').read_text().splitlines()
                  if len(line.split())>=6 and line.split()[-1].startswith('/')})
    result['loaded_library_paths']=[path for path in paths if any(name.split('.so')[0] in Path(path).name for name in libraries)]
    return result


def tree_inventory(root,deadline):
    root=Path(root)
    if not root.exists():return {'status':'missing'}
    resolved=root.resolve();logical=allocated=files=links=directories=0;seen=set();examples=[]
    for base,dirs,names in os.walk(resolved,followlinks=False):
        if time.monotonic()>=deadline:raise TimeoutError('Inventory deadline')
        directories+=1
        for name in [*dirs,*names]:
            path=Path(base)/name;metadata=path.lstat()
            if path.is_symlink():
                links+=1
                if len(examples)<16:examples.append({'path':str(path),'target':os.readlink(path),'resolved':str(path.resolve())})
            elif path.is_file():
                files+=1;logical+=metadata.st_size
                key=(metadata.st_dev,metadata.st_ino)
                if key not in seen:seen.add(key);allocated+=metadata.st_blocks*512
        if files>1000000:raise ValueError('Tree file count exceeds inventory bound')
    return {'status':'inventoried','resolved':str(resolved),'root_link':os.readlink(root) if root.is_symlink() else None,
        'regular_files':files,'symlinks':links,'directories':directories,
        'logical_file_bytes':logical,'allocated_bytes_unique_inode':allocated,'symlink_examples':examples}


def child_code():
    return ('import json, os, re, subprocess, sys\nfrom pathlib import Path\n'+
            inspect.getsource(encoded)+'\n'+inspect.getsource(interpreter_inventory)+
            '\nprint(encoded(interpreter_inventory()).decode())\n')


def validate(prepared):
    p=prepared['plan']
    reference=prepare()['plan']
    variable={'attempt','app_name','launcher_sha256'}
    if (sha(p)!=prepared['plan_sha256'] or
        {k:v for k,v in p.items() if k not in variable} != {k:v for k,v in reference.items() if k not in variable} or
        not re.fullmatch('inventory-[0-9a-f]{16}',p['attempt']) or
        p['app_name']!='coppertop-runpod-'+p['attempt'] or
        hashlib.sha256(Path(__file__).read_bytes()).hexdigest()!=p['launcher_sha256']):
        raise ValueError('Exact approved inventory scope and current code required')


def inventory_worker(p,code):
    """One CPU function; manager network blocked; only sanitized structured output."""
    began=time.monotonic();deadline=began+880
    result={'status':'passed','plan_sha256':sha(p),'image':p['image'],'gpu_allocated':False,
            'bulk_output':False,'model_instantiated':False,'cuda_executed':False}
    environment={'PATH':'/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin',
        'HOME':'/root','PYTHONDONTWRITEBYTECODE':'1','HF_HUB_OFFLINE':'1','TRANSFORMERS_OFFLINE':'1',
        'HF_HUB_DISABLE_TELEMETRY':'1','TOKENIZERS_PARALLELISM':'false','OMP_NUM_THREADS':'2','MKL_NUM_THREADS':'2'}
    def remaining(limit):
        available=deadline-time.monotonic()
        if available<=0:raise TimeoutError('Inventory deadline')
        return min(limit,available)
    interpreters={}
    for python in ('/opt/conda/bin/python','/work/sim/bin/python'):
        with tempfile.TemporaryFile() as out:
            process=subprocess.run([python,'-c',code],env=environment,stdout=out,stderr=subprocess.DEVNULL,
                timeout=remaining(180))
            size=out.tell();out.seek(0);raw=out.read(65537)
        if process.returncode!=0 or size>65536:raise ValueError('Interpreter inventory failed or oversized')
        interpreters[python]=json.loads(raw)
    result['interpreters']=interpreters
    result['trees']={root:tree_inventory(root,deadline) for root in TREES}
    distinct={item['resolved']:item['logical_file_bytes'] for item in result['trees'].values() if item['status']=='inventoried'}
    result['distinct_tree_logical_bytes']=sum(distinct.values())
    result['sources']={}
    for name,expected in p['source_pins'].items():
        root='/work/'+name
        revision=subprocess.run(['git','-C',root,'rev-parse','HEAD'],capture_output=True,timeout=remaining(60))
        dirty=subprocess.run(['git','-C',root,'status','--porcelain','--untracked-files=no'],capture_output=True,timeout=remaining(120))
        actual=revision.stdout.decode().strip() if revision.returncode==0 else None
        if actual is not None and not re.fullmatch('[0-9a-f]{40}',actual):raise ValueError('Source revision invalid')
        result['sources'][name]={'revision':actual,'expected':expected,'matches':actual==expected,
                                'tracked_clean':dirty.returncode==0 and not dirty.stdout}
    packages=subprocess.run(['dpkg-query','-W','-f=${binary:Package}\t${Version}\t${Architecture}\t${db:Status-Abbrev}\n'],
        capture_output=True,timeout=remaining(30))
    if packages.returncode or len(packages.stdout)>128*1024:raise ValueError('System inventory failed or oversized')
    result['system_packages']=[dict(zip(('package','version','architecture','status'),line.split('\t')))
                               for line in packages.stdout.decode().splitlines() if len(line.split('\t'))==4]
    receipt=Path('/work/build-evidence/assets.json')
    with receipt.open('rb') as source:raw=source.read(32*1024**2+1)
    if len(raw)>32*1024**2:raise ValueError('Extraction receipt exceeds metadata bound')
    assets=json.loads(raw)
    result['assets']={'status':assets.get('status'),'receipt_sha256':hashlib.sha256(raw).hexdigest(),
        'archives':[{'status':a.get('status'),'expected_bytes':a.get('expected_bytes'),'received_bytes':a.get('received_bytes'),
                     'files':len(a.get('files',[])),'extracted_bytes':sum(f['bytes'] for f in a.get('files',[]))}
                    for a in assets.get('archives',[])],
        'archive_presence_checked':False,'content_checksums_revalidated':False}
    ffmpeg=subprocess.run(['ffmpeg','-version'],capture_output=True,timeout=remaining(10))
    line=ffmpeg.stdout.decode().splitlines()[0] if ffmpeg.returncode==0 else ''
    result['ffmpeg_version']=line[:200]
    result['elapsed_seconds']=time.monotonic()-began
    if len(encoded(result))>p['limits']['result_bytes']:raise ValueError('Inventory output exceeds approved bound')
    return encoded(result)


def collect(function,p):
    # Async spawn uses a smaller negotiated inline limit and may require an
    # external blob upload from the network-blocked container. Use the public
    # synchronous API with one explicitly bounded byte payload instead.
    raw=function.remote(p,child_code())
    if not isinstance(raw,bytes) or len(raw)>p['limits']['result_bytes']:
        raise ValueError('Bounded inline inventory bytes required')
    result=json.loads(raw)
    if result['plan_sha256']!=sha(p):raise ValueError('Inventory receipt identity differs')
    return result


def define(sdk,p):
    if sdk.__version__!='1.6.1':raise ValueError('Pinned Modal 1.6.1 required')
    app=sdk.App(p['app_name'],secrets=[],volumes={},include_source=False)
    function=app.function(name='runpod-runtime-inventory',image=sdk.Image.from_id(p['image']),
        cpu=(2,2),memory=(16384,16384),gpu=None,secrets=[],volumes={},timeout=900,startup_timeout=300,
        region='us',block_network=True,single_use_containers=True,min_containers=0,buffer_containers=0,
        max_containers=1,retries=0,scaledown_window=2,include_source=True)(inventory_worker)
    return app,function


def execute(prepared,output,approved=None):
    if approved!=prepared['plan_sha256']:raise ValueError('Fresh exact inventory approval hash required')
    import modal
    from coppertop.reload_launch import hard_deadline,stop_and_audit_owned_app,cli_json
    p=prepared['plan']
    validate(prepared)
    output=Path(output);output.mkdir(parents=True,exist_ok=True)
    with (output/'consumed.json').open('xb') as stream:
        stream.write(encoded({'plan_sha256':prepared['plan_sha256'],'reservation_usd':'0.20'}))
        stream.flush();os.fsync(stream.fileno())
    save(output/'prepared.json',prepared)
    state={'status':'failed','plan_sha256':prepared['plan_sha256'],'reservation_usd':'0.20','holds_released':False}
    app=context=None;entered=False;began=time.monotonic()
    try:
        with hard_deadline(1200):
            app,function=define(modal,p)
            context=app.run(name=p['app_name'],environment_name='main')
            with hard_deadline(300):context.__enter__()
            entered=True;state['app_id']=app.app_id;save(output/'state.json',state)
            with hard_deadline(max(1,1200-(time.monotonic()-began))):
                result=collect(function,p)
            if result['plan_sha256']!=prepared['plan_sha256'] or len(encoded(result))>262144:raise ValueError('Receipt identity/size differs')
            save(output/'inventory.json',result);state['status']='passed';state['inventory_bytes']=len(encoded(result))
    except BaseException as error:
        state['error_type']=type(error).__name__
        host=getattr(error,'host',None)
        if isinstance(host,str) and re.fullmatch('[A-Za-z0-9.-]+',host):state['failed_dns_host']=host
    finally:
        cleanup_began=time.monotonic()
        if app and app.app_id:
            state['app_id']=app.app_id;save(output/'state.json',state)
            try:
                with hard_deadline(180):
                    if entered:
                        try:
                            with hard_deadline(30):context.__exit__(None,None,None)
                        except BaseException as error:state['context_exit_error_type']=type(error).__name__
                    while True:
                        try:
                            state['cleanup']=stop_and_audit_owned_app(app.app_id);break
                        except RuntimeError:
                            if time.monotonic()-cleanup_began>=177:raise
                            time.sleep(2)
                    apps=cli_json([sys.executable,'-m','modal','app','list','--env','main','--json'])
                    rows=[row for row in apps if row.get('App ID',row.get('app_id'))==app.app_id]
                    state['app_snapshot']=rows
            except BaseException as error:state['cleanup_error_type']=type(error).__name__
        state['elapsed_seconds']=time.monotonic()-began;state['cleanup_seconds']=time.monotonic()-cleanup_began
        state['compute_provisional_upper_usd']=str((Decimal('0.0000131')*2+Decimal('0.00000222')*16)*Decimal('1.75')*Decimal(str(state['elapsed_seconds'])))
        save(output/'state.json',state)
    if state.get('cleanup',{}).get('app_stopped') is not True:state['status']='failed'
    # Read only this day's metered report. Never rewrite the sibling budget ledger.
    try:
        today=datetime.now(timezone.utc).date()
        billing=cli_json([sys.executable,'-m','modal','billing','report','--start',today.isoformat(),
            '--end',(today+timedelta(days=1)).isoformat(),'--resolution','h','--tz','UTC','--json'])
        rows=[row for row in billing if row['object_id']==state.get('app_id')]
        reported=sum((Decimal(str(row['cost'])) for row in rows),Decimal(0))
        state['reported_metered_usd']=str(reported);state['billing_final']=False
        save(output/'billing.json',{'checked_utc':datetime.now(timezone.utc).isoformat(),'app_id':state.get('app_id'),
              'rows':rows,'reported_usd':str(reported),'full_reservation_retained_usd':'0.20'})
    except Exception as error:state['billing_error_type']=type(error).__name__
    save(output/'state.json',state);return state


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--prepare',action='store_true');parser.add_argument('--execute',type=Path)
    parser.add_argument('--approved-plan-sha256')
    parser.add_argument('--output',type=Path,required=True);args=parser.parse_args()
    if args.prepare:save(args.output,prepare());return
    if not args.execute:parser.error('Choose --prepare or --execute')
    def terminated(signum,frame):raise KeyboardInterrupt('Inventory termination')
    signal.signal(signal.SIGTERM,terminated)
    result=execute(json.loads(args.execute.read_bytes()),args.output,args.approved_plan_sha256)
    print(encoded({key:result.get(key) for key in ('status','app_id','reported_metered_usd','compute_provisional_upper_usd','cleanup')}).decode())
    if result['status']!='passed':raise SystemExit(1)


if __name__=='__main__':main()
