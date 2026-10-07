"""One official query on an already allocated worker; reuse the three-phase publisher."""
import argparse
import io
import json
import os
import re
from pathlib import Path
import subprocess
import time
from types import SimpleNamespace

from .chords import LocalStore, canonical, digest
from .diagnostics import failure
from .performance_eval import recording_acceptance_plan
from .performance_runtime import NATIVE, MODULE, stop_owned, rollout
from .reload_launch import hard_deadline, read_json, save


def plan():
    from .mechanism_runtime import plan as runtime_plan
    acceptance=recording_acceptance_plan()
    return {'kind':'official_recording_worker', 'image':acceptance['cached_image'],
        'acceptance':acceptance, 'runtime_sha256':runtime_plan()['runtime_sha256'],
        'limits':acceptance['compute_proposal'], 'allocates_cloud_resources':False}


def validate_recording(output, *, entry=None):
    """Read back native values, query attribution and every encoded frame locally."""
    import av
    import numpy as np
    import pyarrow.parquet as pq
    from .official_recording import STATE_FIELDS
    from .publication import release_objects
    episode=read_json(output/'episode.json')
    if (episode.get('status')!='completed' or episode.get('model_queries')!=1
            or episode.get('seed')!=1150 or not 1<=episode.get('steps',0)<=4):
        raise ValueError('Official acceptance episode bounds differ')
    if entry is None:
        from .performance_runtime import load_official
        entry=load_official('entry')
    store=LocalStore(output/'store'); key=episode['chords_release']
    closure=release_objects(store,key,max_bytes=24*1024**2)
    if len(closure)>64: raise ValueError('Official acceptance object bound differs')
    manifest=json.loads(store.get(key))
    if len(manifest['episodes'])!=1: raise ValueError('Official acceptance episode count differs')
    record=json.loads(store.get(manifest['episodes'][0]))
    rows=pq.read_table(io.BytesIO(store.get(record['signals']))).to_pylist()
    queries=json.loads(store.get(record['queries']))['queries']
    if len(queries)!=1 or queries[0].get('status')!='completed':
        raise ValueError('Official acceptance query count differs')
    query=queries[0]
    if (query['at_frame']!=0 or query['history_frames']!=[rows[0]['frame_id']]*4
            or query['history_valid']!=[False,False,False,True]
            or query['executed_offsets']!=list(range(episode['steps']))
            or query['discarded_offsets']!=list(range(episode['steps'],16))):
        raise ValueError('Official acceptance query history/offsets differ')
    def array(ref):
        value=np.load(io.BytesIO(store.get(ref['object'])),allow_pickle=False)
        if str(value.dtype)!=ref['dtype'] or list(value.shape)!=ref['shape']:
            raise ValueError('Official acceptance array descriptor differs')
        return value
    def same_bytes(left,right):
        return left.shape==right.shape and left.dtype==right.dtype and left.tobytes()==right.tobytes()
    inputs=json.loads(store.get(query['input']))
    initial=json.loads(store.get(rows[0]['observation']))
    raw_initial={name:array(ref) for name,ref in initial['native'].items()}
    expected_state=np.stack([entry.observation_to_state(raw_initial)]*4)
    if inputs['instruction']!=initial['instruction'] or not same_bytes(array(inputs['state_view']),expected_state):
        raise ValueError('Official acceptance state history bytes differ')
    if set(inputs['camera_views'])!=set(entry.CAMERA_KEYS):
        raise ValueError('Official acceptance camera history set differs')
    for name,ref in inputs['camera_views'].items():
        if not same_bytes(array(ref),np.stack([raw_initial[name]]*4)):
            raise ValueError('Official acceptance camera history bytes differ')
    chunk=array(query['chunk'])
    if chunk.shape!=(16,12) or chunk.dtype!=np.float32 or not np.isfinite(chunk).all() or len(rows)!=episode['steps']+1:
        raise ValueError('Official acceptance frame/chunk geometry differs')
    for index,row in enumerate(rows):
        raw=json.loads(store.get(row['observation']))
        state=np.concatenate([array(raw['native'][name]) for name,_ in STATE_FIELDS])
        if (row['step']!=index or row['time_ns']!=index*50_000_000
                or not same_bytes(state.astype(np.float64),np.asarray(row['state'],dtype=np.float64))):
            raise ValueError('Official acceptance native state/frame timing differs')
        if index<len(rows)-1:
            if row['query_id']!=query['query_id'] or row['chunk_offset']!=index or not same_bytes(np.asarray(row['action'],dtype=np.float32),chunk[index]):
                raise ValueError('Official acceptance action attribution differs')
        elif row['action'] is not None: raise ValueError('Official acceptance terminal action differs')
    if len(record['videos'])!=3: raise ValueError('Official acceptance camera count differs')
    for video in record['videos'].values():
        with av.open(io.BytesIO(store.get(video['sha256']))) as container:
            count=0
            for index,frame in enumerate(container.decode(video=0)):
                if index>=len(rows) or frame.width!=256 or frame.height!=256 or frame.pts is None or abs(float(frame.pts*frame.time_base)-index/20)>1e-6:
                    raise ValueError('Official acceptance video frame timing differs')
                count+=1
            if count!=len(rows): raise ValueError('Official acceptance video frame count differs')
    return {'status':'recording_acceptance','query_calls':1,'controls':episode['steps'],
        'source_frames':len(rows),'release':key,'objects':len(closure),'real_simulation':True,
        'claim':'bounded official evaluator recording; not replay parity or throughput'}


def execute(output, approved_sha256, *, run_id, popen=subprocess.Popen, perform=rollout, validate=validate_recording):
    if not re.fullmatch(r'mechanism-[0-9a-f]{16}',run_id): raise ValueError('Dedicated recording run identity required')
    worker=plan()
    if digest(canonical(worker))!=approved_sha256: raise ValueError('Exact recording worker plan required')
    output=Path(output); output.mkdir(parents=True,exist_ok=False)
    server=None; component='official.server_start'
    try:
        with (output/'server.log').open('wb') as log:
            try:
                with hard_deadline(240):
                    server=popen([NATIVE,'-m',MODULE,'--serve','--arm','official','--output',str(output)],
                        stdout=log,stderr=log,env=dict(os.environ),start_new_session=True)
                    started=time.monotonic()
                    while not (output/'ready.json').is_file():
                        if server.poll() is not None: raise RuntimeError('Official server exited before readiness')
                        if time.monotonic()-started>=90: raise TimeoutError('Official server readiness deadline')
                        time.sleep(.1)
                    component='official.evaluate_task'
                    perform(SimpleNamespace(arm='official',seed=1150,output=output,
                        record_chords=True,record_run_id=run_id,
                        record_horizon=4,replan_steps=16))
                    component='chords.acceptance_readback'
                    result=validate(output)
            finally:
                if server is not None: stop_owned(server)
        save(output/'result.json',result)
        return result
    except BaseException as error:
        save(output/'failure.json',{'status':'failed','failure':failure(error,component),
            'automatic_retries':0,'comparison_valid':False})
        raise


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--execute-local',action='store_true',required=True)
    parser.add_argument('--plan-sha256',required=True)
    parser.add_argument('--output',type=Path,required=True)
    parser.add_argument('--run-id',required=True)
    args=parser.parse_args(); execute(args.output,args.plan_sha256,run_id=args.run_id)


if __name__=='__main__': main()
