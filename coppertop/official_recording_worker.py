"""Pinned official rollout recording on an allocated worker; no cloud allocation."""
import argparse
import io
import json
import math
import os
import re
import signal
from pathlib import Path
import subprocess
import time
from types import SimpleNamespace

from .chords import LocalStore, canonical, digest
from .diagnostics import failure
from .performance_eval import recording_acceptance_plan
from .performance_runtime import NATIVE, MODULE, stop_owned, rollout
from .reload_launch import hard_deadline, read_json, save
from .streams import read_small


def plan(*, full=False):
    from .mechanism_runtime import plan as runtime_plan
    acceptance=recording_acceptance_plan(full=full)
    return {'kind':'official_recording_worker', 'image':acceptance['cached_image'],
        'acceptance':acceptance, 'runtime_sha256':runtime_plan()['runtime_sha256'],
        'limits':acceptance['compute_proposal'], 'allocates_cloud_resources':False}


def validate_recording(output, *, entry=None, full=False):
    """Read back native values, query attribution and every encoded frame locally."""
    import av
    import numpy as np
    import pyarrow.parquet as pq
    from .official_recording import STATE_FIELDS
    from .publication import release_objects
    episode=read_json(output/'episode.json')
    limits=recording_acceptance_plan(full=full)['limits']
    steps=episode.get('steps',0)
    calls=(steps+15)//16
    if (episode.get('status')!='completed' or episode.get('model_queries')!=calls
            or episode.get('seed')!=1150 or not 1<=steps<=limits['horizon']):
        raise ValueError('Official acceptance episode bounds differ')
    if entry is None:
        from .performance_runtime import load_official
        entry=load_official('entry')
    store=LocalStore(output/'store'); key=episode['chords_release']
    closure=release_objects(store,key,max_bytes=limits['record_bytes'])
    if len(closure)>limits['record_objects']: raise ValueError('Official acceptance object bound differs')
    manifest=json.loads(store.get(key))
    if len(manifest['episodes'])!=1: raise ValueError('Official acceptance episode count differs')
    record=json.loads(store.get(manifest['episodes'][0]))
    rows=pq.read_table(io.BytesIO(store.get(record['signals']))).to_pylist()
    queries=json.loads(store.get(record['queries']))['queries']
    if len(queries)!=calls or any(q.get('status')!='completed' for q in queries):
        raise ValueError('Official acceptance query count differs')
    def array(ref):
        value=np.load(io.BytesIO(store.get(ref['object'])),allow_pickle=False)
        if str(value.dtype)!=ref['dtype'] or list(value.shape)!=ref['shape']:
            raise ValueError('Official acceptance array descriptor differs')
        return value
    def same_bytes(left,right):
        return left.shape==right.shape and left.dtype==right.dtype and left.tobytes()==right.tobytes()
    if len(rows)!=steps+1: raise ValueError('Official acceptance frame geometry differs')
    chunks=[]
    from .chords import window_indices
    for number,query in enumerate(queries):
        at=number*16
        indices,valid=window_indices(at,len(rows),(-6,-4,-2,0))
        executed=min(16,steps-at)
        if (query['query_id']!=number or query['at_frame']!=at
                or query['history_frames']!=[rows[i]['frame_id'] for i in indices]
                or query['history_valid']!=list(valid)
                or query['executed_offsets']!=list(range(executed))
                or query['discarded_offsets']!=list(range(executed,16))):
            raise ValueError('Official acceptance query history/offsets differ')
        inputs=json.loads(store.get(query['input']))
        history=[json.loads(store.get(rows[i]['observation'])) for i in indices]
        native=[{name:array(ref) for name,ref in obs['native'].items()} for obs in history]
        expected_state=np.stack([entry.observation_to_state(obs) for obs in native])
        if (any(inputs['instruction']!=obs['instruction'] for obs in history)
                or not same_bytes(array(inputs['state_view']),expected_state)):
            raise ValueError('Official acceptance state history bytes differ')
        if set(inputs['camera_views'])!=set(entry.CAMERA_KEYS):
            raise ValueError('Official acceptance camera history set differs')
        for name,ref in inputs['camera_views'].items():
            if not same_bytes(array(ref),np.stack([obs[name] for obs in native])):
                raise ValueError('Official acceptance camera history bytes differ')
        chunk=array(query['chunk'])
        if chunk.shape!=(16,12) or chunk.dtype!=np.float32 or not np.isfinite(chunk).all():
            raise ValueError('Official acceptance chunk geometry differs')
        chunks.append(chunk)
    for index,row in enumerate(rows):
        raw=json.loads(store.get(row['observation']))
        state=np.concatenate([array(raw['native'][name]) for name,_ in STATE_FIELDS])
        if (row['step']!=index or row['time_ns']!=index*50_000_000
                or not same_bytes(state.astype(np.float64),np.asarray(row['state'],dtype=np.float64))):
            raise ValueError('Official acceptance native state/frame timing differs')
        if index<len(rows)-1:
            if row['query_id']!=index//16 or row['chunk_offset']!=index%16 or not same_bytes(np.asarray(row['action'],dtype=np.float32),chunks[index//16][index%16]):
                raise ValueError('Official acceptance action attribution differs')
        elif row['action'] is not None: raise ValueError('Official acceptance terminal action differs')
    if full and (bool(episode.get('success'))!=rows[-1]['success'] or not
            (rows[-1]['success'] or rows[-1]['terminated'] or rows[-1]['truncated'] or steps==600)):
        raise ValueError('Full official episode outcome/boundary differs')
    if len(record['videos'])!=3: raise ValueError('Official acceptance camera count differs')
    for video in record['videos'].values():
        with av.open(io.BytesIO(read_small(store,video['sha256'],64*1024**2 if full else 4*1024**2))) as container:
            count=0
            for index,frame in enumerate(container.decode(video=0)):
                if index>=len(rows) or frame.width!=256 or frame.height!=256 or frame.pts is None or abs(float(frame.pts*frame.time_base)-index/20)>1e-6:
                    raise ValueError('Official acceptance video frame timing differs')
                count+=1
            if count!=len(rows): raise ValueError('Official acceptance video frame count differs')
    result = {'status':'recording_acceptance','query_calls':calls,'controls':episode['steps'],
        'source_frames':len(rows),'release':key,'objects':len(closure),'real_simulation':True,
        'claim':'bounded official evaluator recording; not replay parity or throughput'}

    if full:
        if (type(episode.get('success')) is not bool
                or not re.fullmatch('[0-9a-f]{64}',episode.get('initial_observation_sha256',''))
                or any(type(episode.get(name)) not in (int,float) or not math.isfinite(episode[name]) or episode[name]<0
                    for name in ('wall_seconds','inference_seconds','model_load_seconds'))):
            raise ValueError('Full official outcome/timing fields differ')
        result.update(success=episode['success'],terminated=rows[-1]['terminated'],
            truncated=rows[-1]['truncated'],horizon_reached=steps==600,
            initial_observation_sha256=episode['initial_observation_sha256'],
            wall_seconds=episode['wall_seconds'],inference_seconds=episode['inference_seconds'],
            model_load_seconds=episode['model_load_seconds'],cadence=16,horizon=600,split='development',
            claim='full frozen official recording baseline; not a data utility improvement')
    return result


def execute(output, approved_sha256, *, run_id, popen=subprocess.Popen, perform=rollout, validate=validate_recording, full=False):
    if not re.fullmatch(r'mechanism-[0-9a-f]{16}',run_id): raise ValueError('Dedicated recording run identity required')
    worker=plan(full=full)
    if digest(canonical(worker))!=approved_sha256: raise ValueError('Exact recording worker plan required')
    output=Path(output); output.mkdir(parents=True,exist_ok=False)
    began=time.monotonic()
    server=None; component='official.server_start'
    try:
        with (output/'server.log').open('wb') as log:
            try:
                with hard_deadline(worker['limits']['session_seconds']):
                    server=popen([NATIVE,'-m',MODULE,'--serve','--arm','official','--output',str(output),
                        *(['--full-recording'] if full else [])],
                        stdout=log,stderr=log,env=dict(os.environ),start_new_session=True)
                    started=time.monotonic()
                    while not (output/'ready.json').is_file():
                        if server.poll() is not None: raise RuntimeError('Official server exited before readiness')
                        if time.monotonic()-started>=90: raise TimeoutError('Official server readiness deadline')
                        time.sleep(.1)
                    component='official.evaluate_task'
                    perform(SimpleNamespace(arm='official',seed=1150,output=output,
                        record_chords=True,record_run_id=run_id,
                        record_horizon=600 if full else 4,replan_steps=16))
                    component='chords.acceptance_readback'
                    result=validate(output,full=True) if full else validate(output)
            finally:
                if server is not None: stop_owned(server)
        if full: result['worker_elapsed_seconds']=time.monotonic()-began
        save(output/'result.json',result)
        return result
    except BaseException as error:
        save(output/'failure.json',{'status':'failed','failure':failure(error,component),
            'automatic_retries':0,'comparison_valid':False})
        raise


def main():
    def terminate(signum,frame):
        raise SystemExit('Owned worker terminated')
    # The Pod controller's timeout must unwind execute's server cleanup.
    signal.signal(signal.SIGTERM,terminate)
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--execute-local',action='store_true',required=True)
    parser.add_argument('--plan-sha256',required=True)
    parser.add_argument('--output',type=Path,required=True)
    parser.add_argument('--run-id',required=True)
    parser.add_argument('--full',action='store_true')
    args=parser.parse_args(); execute(args.output,args.plan_sha256,run_id=args.run_id,full=args.full)


if __name__=='__main__': main()
