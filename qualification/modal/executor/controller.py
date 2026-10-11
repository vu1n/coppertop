"""One consumed CPU -> independent absence -> GPU attempt, never an automatic retry."""
import argparse
import asyncio
import io
import json
import math
import os
from pathlib import Path
import re
import shutil
import subprocess
import sys
import tarfile
import time
from uuid import uuid4
from diagnostics import error_evidence

sys.path.insert(0,str(Path(__file__).resolve().parent))
from common import (APP_ID, IDENTITY, IMAGE, RUN_ID, STAGES, canonical, durable,
                    read_json, sha, verify_bundle)

# Reviewable literal executed only in a new owned Sandbox; no extractall, symlinks or shell.
INSTALLER = '''import hashlib,io,json,pathlib,sys,tarfile
root=pathlib.Path(sys.argv[1]);size=int(sys.argv[2]);expected=sys.argv[3]
assert root.parent==pathlib.Path('/tmp') and root.name.startswith('coppertop-qualification-')
assert 0<size<=10485760
raw=sys.stdin.buffer.read(size+1)
assert len(raw)==size and hashlib.sha256(raw).hexdigest()==expected
root.mkdir(mode=0o700,exist_ok=False)
seen=set();total=0
with tarfile.open(fileobj=io.BytesIO(raw),mode='r:') as archive:
 for entry in archive:
  parts=entry.name.split('/')
  assert entry.isfile() and not entry.issym() and not entry.islnk()
  assert entry.name not in seen and not entry.name.startswith('/') and '\\\\' not in entry.name
  assert all(x not in ('','.','..') for x in parts) and entry.size<=8388608
  seen.add(entry.name);total+=entry.size
  assert len(seen)<=128 and total<=10485760
  path=root/entry.name;path.parent.mkdir(parents=True,exist_ok=True,mode=0o700)
  data=archive.extractfile(entry).read(entry.size+1)
  assert len(data)==entry.size
  with path.open('xb') as target:target.write(data)
print(json.dumps({'installed_files':len(seen),'archive_sha256':expected,'payload_bytes':total}))
'''


def fixed_scope(plan):
    if (plan['identity']['workspace']!=IDENTITY['workspace'] or plan['identity']['environment']!='main'
            or plan['identity']['environment_id']!=IDENTITY['environment_id']
            or plan['cached_images']['selected_combined_image']!=IMAGE
            or plan['proposal_budget_usd']!='2.00' or plan['pricing']['total_estimated_reserve_usd']!='1.78093731'
            or plan['pricing']['checked_date']!=time.strftime('%Y-%m-%d',time.gmtime())
            or plan['resources']['max_active_containers']!=1 or not plan['resources']['stages_serial']
            or plan['resources']['apps']!=1 or plan['resources']['sandboxes']!=2
            or plan['resources']['functions']!=0 or plan['resources']['non_preemptible']
            or plan['transfers_and_writes']['r2_calls']!=0 or plan['cached_images']['new_image_builds']!=0
            or plan['workload']['actor_calls']!=2 or plan['workload']['clef_calls']!=1
            or plan['workload']['warmup_calls']!=0 or plan['workload']['training_steps']!=0
            or plan['workload']['simulator_controls']!=0 or plan['workload']['full_episodes']!=0
            or plan['deadlines']['outer_absolute_deadline_seconds']!=1066
            or plan['deadlines']['cpu']['reserved_billable_seconds']!=406
            or plan['deadlines']['gpu']['reserved_billable_seconds']!=660
            or plan['retry_link']['aggregate_total_reserve_usd']!='1.82899735'
            or plan['cleanup']['cleanup_budget_seconds']!=120):
        raise ValueError('Fixed identity, quote, resource, time and call scope required')
    for stage, options in STAGES.items():
        actual=plan['resources'][stage+'_stage']
        if (actual['cpu_request_cores']!=options['cpu'][0] or actual['cpu_limit_cores']!=options['cpu'][1]
                or actual['memory_request_mib']!=options['memory'][0]
                or actual['memory_limit_mib']!=options['memory'][1]
                or actual['provider_timeout_seconds']!=options['timeout']
                or actual['idle_timeout_seconds']!=20 or actual['gpu_count']!=int(stage=='gpu')):
            raise ValueError('Fixed stage resource request and limit required')


def require_approval(root, approval):
    if not isinstance(approval,dict) or not isinstance(approval.get('plan_sha256'),str):
        raise ValueError('Explicit exact-byte approval required')
    plan,manifest=verify_bundle(root,approval['plan_sha256'],require_ready=True)
    fixed_scope(plan)
    expected={'approved':True,'plan_sha256':approval['plan_sha256'],**IDENTITY,
              'modal_allowance_usd':'2.00','one_attempt':True,'bounded_exposure_accepted':True,
              'cleanup_authorized':True,'rate_ceiling_confirmed':True,'r2_actions':False,
              'expires_at':approval.get('expires_at')}
    expiry=approval.get('expires_at')
    if (type(expiry) not in (int,float) or not math.isfinite(expiry)
            or not time.time()<expiry<=time.time()+1800 or approval!=expected):
        raise ValueError('Fresh explicit one-attempt budget/risk/cleanup approval required')
    return plan,manifest


def payload(root, manifest):
    names=['plan.json','execution-manifest.json']+[row['path'] for row in manifest['files']]
    buffer=io.BytesIO();total=0;source_bytes=0;input_bytes=0
    with tarfile.open(fileobj=buffer,mode='w',format=tarfile.USTAR_FORMAT) as archive:
        for name in sorted(names):
            path=root/name
            if path.is_symlink():raise ValueError('No symlink payload')
            raw=path.read_bytes();total+=len(raw)
            if name.startswith('input/'):input_bytes+=len(raw)
            else:source_bytes+=len(raw)
            info=tarfile.TarInfo(name);info.size=len(raw);info.mode=0o600;info.mtime=0
            archive.addfile(info,io.BytesIO(raw))
    raw=buffer.getvalue()
    if source_bytes>2097152 or input_bytes>8388608 or total>10485760 or len(raw)>10485760:
        raise ValueError('Source/input/archive transfer caps exceeded')
    return raw,names


async def wait_file(path, observer, seconds):
    end=time.monotonic()+seconds
    while time.monotonic()<end:
        if path.exists():return read_json(path)
        if observer.poll() is not None:raise ValueError('Independent observer exited without receipt')
        await asyncio.sleep(.1)
    raise TimeoutError('Independent observer handshake/cleanup deadline expired')


def positive_remaining(deadline_at, cap=None):
    remaining=deadline_at-time.time()
    if not math.isfinite(remaining) or remaining<=0:
        raise TimeoutError('Absolute execution authority expired before new work')
    return min(remaining,cap) if cap is not None else remaining


def check_ack(ack, record, observer):
    if (not ack.get('armed') or any(ack.get(k)!=record[k] for k in (
            'plan_sha256','run_id','app_id','generation','stage','execution_deadline',*IDENTITY))
            or ack.get('observer_pid')!=observer.pid or ack.get('observer_pgid')==os.getpgrp()
            or observer.poll() is not None):
        raise ValueError('Independent authenticated observer acknowledgement required')


def check_result(result, record, stage):
    if (not isinstance(result,dict) or result.get('status')!='passed'
            or result.get('plan_sha256')!=record['plan_sha256'] or result.get('run_id')!=record['run_id']
            or result.get('stage')!=stage or result.get('execution_manifest_sha256')!=record['execution_manifest_sha256']
            or not result.get('owned_direct_children_reaped') or result.get('child_cleanup_errors')
            or result.get('training_steps')!=0 or result.get('simulator_controls')!=0):
        raise ValueError('Exact completed stage and child cleanup receipt required')
    phases=result['phases']
    expected=['fresh-restored-hash','three-interpreter-dependencies','actual-native-processors'] if stage=='cpu' else [
        'fresh-restored-hash','actor-load','clef-load','stock-actor-call','disabled-clef-actor-call',
        'official-clef-joint-head-call','exact-decoded-action-parity']
    if [row['phase'] for row in phases]!=expected or phases[0]['result']['files']!=37:
        raise ValueError('Exact integrity and native phase closure required')
    if stage=='gpu' and (result.get('actor_calls')!=2 or result.get('clef_calls')!=1
            or phases[-1]['result'].get('finite') is not True):
        raise ValueError('Exact finite action parity and bounded call count required')


async def execute(root, approval, *, backend=None, observer_factory=None):
    root=root.resolve(strict=True)
    plan,manifest=require_approval(root,approval)  # Before SDK imports or authentication.
    archive,names=payload(root,manifest)
    run_id='ct-qual-'+uuid4().hex[:12]
    output=root/('attempt-'+run_id);output.mkdir(mode=0o700,exist_ok=False)
    sealed=output/'payload';sealed.mkdir(mode=0o700)
    for name in names:
        target=sealed/name;target.parent.mkdir(parents=True,exist_ok=True,mode=0o700)
        shutil.copyfile(root/name,target)
    verify_bundle(sealed,approval['plan_sha256'],require_ready=True)
    if backend is None:
        from modal_backend import ModalBackend
        backend=ModalBackend()
    progress=[]
    def observe(event):
        progress.append(event)
        if len(progress)>128:raise ValueError('Bounded phase counter limit exceeded')
        durable(output/'provider-progress.json',{'events':progress})
    backend.progress_callback=observe
    observer=None; record=None;primary=None;cleanup=None;closed=False
    state={'run_id':run_id,'plan_sha256':approval['plan_sha256'],'execution_manifest_sha256':plan['execution_manifest_sha256'],
           'status':'failed','provider_creates_attempted':0,'stages':{}}
    pulse_task=None
    try:
        if await backend.open()!=IDENTITY:raise ValueError('Explicit backup identity differs')
        await backend.image_lookup()
        state['account_rates']=await backend.billing_rates()
        durable(root/'consumed-attempt.json',{'run_id':run_id,'plan_sha256':approval['plan_sha256'],
                'execution_manifest_sha256':plan['execution_manifest_sha256'],'consumed_at':time.time()},exclusive=True)
        state['provider_creates_attempted']=1
        app_id=await backend.create_app(run_id)
        ticks=Path('/proc/self/stat').read_text().rsplit(')',1)[1].split()[19]
        record={'run_id':run_id,'plan_sha256':approval['plan_sha256'],**IDENTITY,'app_id':app_id,
                'execution_manifest_sha256':plan['execution_manifest_sha256'],'stage':'cpu','generation':1,
                'command':'running','execution_deadline':time.time()+STAGES['cpu']['timeout'],'outer_deadline':time.time()+1066,
                'sandbox_id':None,'controller_pid':os.getpid(),'controller_start_ticks':ticks,
                'controller_pgid':os.getpgrp(),'controller_pulse':time.time()}
        durable(output/'ownership.json',record)
        if observer_factory is None:
            observer=subprocess.Popen([sys.executable,str(sealed/'executor/observer.py'),'--root',str(sealed),
                '--output',str(output),'--plan-sha256',approval['plan_sha256'],'--run-id',run_id],
                stdin=subprocess.DEVNULL,stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL,start_new_session=True)
        else:observer=observer_factory(sealed,output,record)
        async def pulse():
            while True:
                record['controller_pulse']=time.time();durable(output/'ownership.json',record)
                await asyncio.sleep(.5)
        pulse_task=asyncio.create_task(pulse())
        ack=await wait_file(output/'ack-1.json',observer,20);check_ack(ack,record,observer)
        for stage in ('cpu','gpu'):
            if stage=='gpu':
                absence=state['cpu_cleanup']
                if (not absence['confirmed'] or absence['app_stopped']
                        or absence['active_containers']!=0 or absence['active_sandboxes']!=0):
                    raise ValueError('Independent CPU zero-active audit required before GPU')
                record.update(stage='gpu',generation=3,command='running',sandbox_id=None,
                              execution_deadline=min(time.time()+540,record['outer_deadline']-120))
                record.pop('cleanup_deadline',None)
                durable(output/'ownership.json',record)
                ack=await wait_file(output/'ack-3.json',observer,10);check_ack(ack,record,observer)
            stage_start=time.time()
            # The acknowledged deadline starts before the allocating request and bounds startup, uploads and exec.
            remaining=positive_remaining(record['execution_deadline'],15)
            state['provider_creates_attempted']+=1
            sandbox_id=await asyncio.wait_for(backend.create_sandbox(app_id,stage),remaining)
            record['sandbox_id']=sandbox_id;durable(output/'ownership.json',record)
            remote='/tmp/coppertop-qualification-'+run_id
            remaining=positive_remaining(min(stage_start+60,record['execution_deadline']))
            installed=json.loads(await backend.exec(sandbox_id,
                ['/opt/conda/bin/python','-I','-c',INSTALLER,remote,str(len(archive)),sha(archive)],remaining,stdin=archive))
            if installed['archive_sha256']!=sha(archive) or installed['installed_files']!=len(names):
                raise ValueError('Exact upload read-back differs')
            remaining=positive_remaining(record['execution_deadline'])
            command=['/opt/conda/bin/python','-I',remote+'/executor/worker.py','--root',remote,
                '--plan-sha256',approval['plan_sha256'],'--run-id',run_id,'--mode','stage','--kind',stage]
            raw=await backend.exec(sandbox_id,command,remaining)
            result=json.loads(raw);durable(output/(stage+'-result.json'),result)
            check_result(result,record,stage)
            if time.time()>=record['execution_deadline']:raise TimeoutError('Stage completed after absolute authority')
            state['stages'][stage]=result
            if stage=='cpu':
                record.update(command='cpu_cleanup',generation=2,cleanup_deadline=time.time()+120)
                # CPU cleanup has its independent 120-second window, not a renewed execution allowance.
                durable(output/'ownership.json',record)
                absence=await wait_file(output/'cpu-cleanup.json',observer,120)
                if (absence.get('app_id')!=app_id or absence.get('plan_sha256')!=approval['plan_sha256']
                        or absence.get('run_id')!=run_id or absence.get('generation')!=2
                        or absence.get('observer_pid')!=observer.pid):
                    raise ValueError('Independent CPU cleanup receipt binding differs')
                state['cpu_cleanup']=absence
        state['status']='passed'
    except BaseException as error:
        primary=type(error).__name__
        state['primary_error']=error_evidence(error,getattr(backend,'transient_secrets',()))
    finally:
        if record is not None:
            cleanup_end=min(record.get('cleanup_deadline',time.time()+120),record['outer_deadline'])
            record.update(command='final_cleanup',generation=4,cleanup_deadline=cleanup_end)
            durable(output/'ownership.json',record)
            if observer is not None:
                try:
                    cleanup=await wait_file(output/'cleanup.json',observer,max(.001,cleanup_end-time.time()-24))
                except BaseException as error:
                    state['independent_cleanup_error_type']=type(error).__name__
            # Independent receipt can already prove cleanup; otherwise controller still attempts both stops.
            if not cleanup or not cleanup.get('confirmed'):
                try:
                    if record.get('sandbox_id'):await asyncio.wait_for(backend.stop_sandbox(record['sandbox_id']),
                        max(.001,min(12,cleanup_end-time.time())))
                except BaseException:pass
                try:await asyncio.wait_for(backend.stop_app(record['app_id']),max(.001,min(12,cleanup_end-time.time())))
                except BaseException:pass
            if (not cleanup or not cleanup.get('confirmed') or cleanup.get('app_id')!=record['app_id']
                    or cleanup.get('plan_sha256')!=record['plan_sha256'] or cleanup.get('run_id')!=run_id
                    or not cleanup.get('final') or not cleanup.get('app_stopped')
                    or cleanup.get('active_containers')!=0 or cleanup.get('active_sandboxes')!=0):
                state['status']='cleanup_unconfirmed'
        if pulse_task:
            pulse_task.cancel()
            try:await pulse_task
            except asyncio.CancelledError:pass
        try:await backend.close();closed=True
        except BaseException:pass
        if observer is not None and isinstance(observer, subprocess.Popen):
            # It has already supplied cleanup or exhausted its bounded window.
            # Reap the exited child without holding up provider cleanup.
            try:await asyncio.to_thread(observer.wait, timeout=5)
            except subprocess.TimeoutExpired:pass
        state.update(primary_error_type=primary,cleanup=cleanup,client_closed=closed)
        durable(output/'result.json',state)
    return state,output


def run_on_sdk_loop(function, *args, **kwargs):
    """The same bridge used by the pinned official Modal CLI entrypoints."""
    from tls import bootstrap
    bootstrap()  # Before first Modal import; repeated verified bootstrap is safe.
    from modal._utils.async_utils import synchronizer
    return synchronizer.create_blocking(function)(*args, **kwargs)


def launch(root, approval):
    # All local identity/budget/source gates run before even importing the SDK.
    require_approval(root, approval)
    return run_on_sdk_loop(execute, root, approval)


def main():
    p=argparse.ArgumentParser();p.add_argument('--root',type=Path,required=True)
    p.add_argument('--approval',type=Path);p.add_argument('--check-only',action='store_true')
    args=p.parse_args();root=args.root.resolve(strict=True)
    if args.check_only:
        plan_sha=sha((root/'plan.json').read_bytes())
        verify_bundle(root,plan_sha,require_ready=True);fixed_scope(read_json(root/'plan.json'))
        print(json.dumps({'launch_ready':True,'plan_sha256':plan_sha,'provider_calls':0,'paid_actions':0}));return 0
    if args.approval is None:raise ValueError('Explicit approval file required')
    state,output=launch(root,read_json(args.approval,65536))
    print(json.dumps({'status':state['status'],'output':str(output),'plan_sha256':state['plan_sha256']}))
    return 0 if state['status']=='passed' else 1


if __name__=='__main__':
    try:raise SystemExit(main())
    except Exception as error:
        print(json.dumps({'launch_ready':False,'error_type':type(error).__name__,
                          'message':'Exact pinned bytes, final review and explicit approval gates required'}))
        raise SystemExit(1)
