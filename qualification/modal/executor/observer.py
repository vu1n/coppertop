"""Independent exact-App watchdog. No allocation or inference API is available here."""
import argparse
import asyncio
import math
import os
from pathlib import Path
import re
import time
from common import (APP_ID, IDENTITY, RUN_ID, SANDBOX_ID, STAGES, durable,
                    read_json, verify_bundle)


def controller_alive(record):
    try:
        fields = Path('/proc')/str(record['controller_pid'])/'stat'
        stat = fields.read_text().rsplit(')',1)[1].split()
        return stat[19] == record['controller_start_ticks']
    except (OSError,KeyError,IndexError):
        return False


def validate_record(record, plan_sha, run_id):
    if (record.get('plan_sha256')!=plan_sha or record.get('run_id')!=run_id
            or not re.fullmatch(APP_ID,record.get('app_id',''))
            or {key:record.get(key) for key in IDENTITY} != IDENTITY
            or type(record.get('generation')) is not int or record['generation']<1
            or record.get('stage') not in STAGES
            or record.get('command') not in ('running','cpu_cleanup','final_cleanup')
            or type(record.get('execution_deadline')) not in (int,float)
            or type(record.get('outer_deadline')) not in (int,float)
            or not math.isfinite(record['execution_deadline'])
            or not math.isfinite(record['outer_deadline'])
            or type(record.get('controller_pulse')) not in (int,float)
            or not math.isfinite(record['controller_pulse'])
            or (record['command'] in ('cpu_cleanup','final_cleanup') and
                (type(record.get('cleanup_deadline')) not in (int,float) or
                 not math.isfinite(record['cleanup_deadline'])))
            or record['execution_deadline']>record['outer_deadline']
            or (record.get('sandbox_id') is not None and
                not re.fullmatch(SANDBOX_ID,record['sandbox_id']))):
        raise ValueError('Exact owned watchdog record required')


async def cleanup_owned(backend, record, *, final, seconds=120, clock=time, pause=asyncio.sleep):
    end=clock.monotonic()+seconds
    errors=[]
    async def call(kind,function):
        try:
            await asyncio.wait_for(function(),max(.001,min(12,end-clock.monotonic())))
        except BaseException as error:
            errors.append({'operation':kind,'error_type':type(error).__name__})
    # AppStop is attempted even if Sandbox termination fails or times out.
    if record.get('sandbox_id'):
        await call('SandboxTerminate',lambda:backend.stop_sandbox(record['sandbox_id']))
    if final:
        await call('AppStop',lambda:backend.stop_app(record['app_id']))
    receipt={'confirmed':False,'app_id':record['app_id'],'workspace':IDENTITY['workspace'],
             'environment':'main','active_containers':None,'active_sandboxes':None,
             'app_stopped':None,'cleanup_errors':errors}
    while clock.monotonic()<end:
        try:
            if not final:
                await asyncio.wait_for(backend.heartbeat(record['app_id']),max(.001,min(6,end-clock.monotonic())))
            audit=await asyncio.wait_for(backend.audit(record['app_id'],stopped=final),
                                         max(.001,end-clock.monotonic()))
            if (audit.get('app_id')!=record['app_id'] or audit.get('workspace')!=IDENTITY['workspace']
                    or audit.get('environment')!='main'):
                raise ValueError('Independent cleanup identity differs')
            receipt.update(audit)
            if audit['confirmed']:
                break
        except BaseException as error:
            errors.append({'operation':'independent_audit','error_type':type(error).__name__})
        if clock.monotonic()<end:
            await pause(min(1,max(0,end-clock.monotonic())))
    return {**receipt,'cleanup_errors':errors[:64],'plan_sha256':record['plan_sha256'],
            'run_id':record['run_id'],'generation':record['generation'],
            'observer_pid':os.getpid(),'final':final,'finished_at':clock.time()}


async def watch(root, output, plan_sha, run_id):
    from modal_backend import ModalBackend
    verify_bundle(root,plan_sha,require_ready=True)
    backend=ModalBackend(); last_generation=0; cleaned_cpu=False; last_valid=None
    await backend.open()
    try:
        while True:
            record=read_json(output/'ownership.json',65536)
            validate_record(record,plan_sha,run_id)
            if last_valid is not None and record['app_id']!=last_valid['app_id']:
                raise ValueError('Observed owned App identity cannot change')
            last_valid=record.copy()
            if record['generation']<last_generation:
                raise ValueError('Watchdog generation moved backward')
            # Identifies the observer's own separate process/session, not a controller promise.
            ack={'plan_sha256':plan_sha,'run_id':run_id,'app_id':record['app_id'],
                 'generation':record['generation'],'stage':record['stage'],
                 'execution_deadline':record['execution_deadline'],**IDENTITY,
                 'observer_pid':os.getpid(),'observer_pgid':os.getpgrp(),
                 'controller_pgid':record['controller_pgid'],'armed':True}
            if os.getpgrp()==record['controller_pgid']:
                raise ValueError('Observer must live outside the controller kill group')
            if record['generation']>last_generation:
                if record['stage']=='gpu':
                    cpu=read_json(output/'cpu-cleanup.json')
                    if (not cleaned_cpu or not cpu['confirmed'] or cpu['app_id']!=record['app_id']
                            or cpu['active_containers']!=0 or cpu['active_sandboxes']!=0
                            or cpu['app_stopped']):
                        raise ValueError('Independent CPU absence required before GPU acknowledgement')
                durable(output/('ack-'+str(record['generation'])+'.json'),ack,exclusive=True)
                last_generation=record['generation']
            active_deadline=(record.get('cleanup_deadline',record['execution_deadline'])
                             if record['command']=='cpu_cleanup' else record['execution_deadline'])
            expired=(not controller_alive(record) or time.time()>=active_deadline
                     or time.time()-record['controller_pulse']>5 or time.time()>=record['outer_deadline'])
            if record['command']=='final_cleanup' or expired:
                limit=min(120,max(.001,record.get('cleanup_deadline',time.time()+120)-time.time()))
                result=await cleanup_owned(backend,record,final=True,seconds=limit)
                durable(output/'cleanup.json',result)
                return 0 if result['confirmed'] else 1
            if record['command']=='cpu_cleanup' and not cleaned_cpu:
                limit=min(90,max(.001,record['cleanup_deadline']-time.time()-30))
                result=await cleanup_owned(backend,record,final=False,seconds=limit)
                durable(output/'cpu-cleanup.json',result)
                if not result['confirmed'] or result['app_stopped']:
                    limit=max(.001,record['cleanup_deadline']-time.time())
                    result=await cleanup_owned(backend,record,final=True,seconds=limit)
                    durable(output/'cleanup.json',result)
                    return 1
                cleaned_cpu=True
            await backend.heartbeat(record['app_id'])
            await asyncio.sleep(.5)
    except BaseException as error:
        # Once ownership is valid, even a malformed later journal/observer failure triggers App cleanup.
        if last_valid is not None:
            limit=min(120,max(.001,last_valid.get('cleanup_deadline',time.time()+120)-time.time()))
            result=await cleanup_owned(backend,last_valid,final=True,seconds=limit)
            result['observer_error_type']=type(error).__name__
            durable(output/'cleanup.json',result)
        return 1
    finally:
        await backend.close()


def main():
    p=argparse.ArgumentParser();p.add_argument('--root',type=Path,required=True)
    p.add_argument('--output',type=Path,required=True);p.add_argument('--plan-sha256',required=True)
    p.add_argument('--run-id',required=True);args=p.parse_args()
    if not re.fullmatch(RUN_ID,args.run_id):raise ValueError('Fixed owned run ID required')
    verify_bundle(args.root,args.plan_sha256,require_ready=True)
    from controller import run_on_sdk_loop
    return run_on_sdk_loop(watch,args.root,args.output,args.plan_sha256,args.run_id)


if __name__=='__main__':
    try:raise SystemExit(main())
    except Exception:raise SystemExit(1)
