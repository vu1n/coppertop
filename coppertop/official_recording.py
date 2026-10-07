"""Passive Chords adapter around pinned evaluate_task; the official code owns cadence."""
import io
import hashlib
import math
import os
from pathlib import Path
import re
import stat
import time
from types import SimpleNamespace

import numpy as np

from .chords import canonical, digest, frame_id, release, window_indices
from .diagnostics import failure
from .streams import ObjectRef, CHUNK_BYTES

STATE_FIELDS = (('state.base_position',3), ('state.base_rotation',4),
    ('state.end_effector_position_relative',3), ('state.end_effector_rotation_relative',4),
    ('state.gripper_qpos',2))
STATE_LAYOUT = 'base_position3/base_quaternion_xyzw4/EE_relative_position3/EE_relative_quaternion_xyzw4/gripper2'
ACTION_LAYOUT = 'EEtranslation3/EErotation3/gripper1/base4/mode1'


from .performance_eval import recording_acceptance_plan as acceptance_plan


class Recorder:
    def __init__(self, entry, store, directory, *, run_id, task, seed, cadence, horizon, provenance):
        if not re.fullmatch(r'[A-Za-z0-9_-]{1,64}',run_id) or cadence not in (8,16) or not 1<=horizon<=600:
            raise ValueError('Bounded run identity, cadence and horizon required')
        self.directory=Path(directory); self.directory.mkdir(exist_ok=False)
        self.entry,self.store=entry,store
        self.task,self.seed,self.cadence,self.horizon=task,seed,cadence,horizon
        self.object_limit=64 if horizon<=4 else 1024
        self.group=f'RoboCasa/{task}/pretrain/constructor1000/episode{seed}'
        self.provenance=dict(provenance,run_id=run_id,task=task,simulator_seed=seed,
                             cadence=cadence,split='development')
        self.episode_id=self.group+'/'+digest(canonical(self.provenance))
        self.rows,self.queries,self.model_states,self.images=[],[],[],[]
        self.seen={}; self.writers={}; self.video_errors={}; self.executions=[]
        self.pending=None; self.active_query=None; self.instruction=None

    def put(self, raw):
        key=digest(raw)
        self.reserve(key,len(raw))
        if self.store.put(raw)!=key: raise ValueError('Immutable recording checksum differs')
        return key

    def reserve(self, key, size):
        if key not in self.seen:
            if len(self.seen)>=self.object_limit or sum(self.seen.values())+size>24*1024**2:
                raise ValueError('Recording object/byte bound exceeded')
            self.seen[key]=size

    def array(self, value):
        array=np.array(value,copy=True)
        if array.dtype.hasobject or not np.isfinite(array).all(): raise ValueError('Finite primitive arrays required')
        out=io.BytesIO(); np.save(out,array,allow_pickle=False)
        return {'object':self.put(out.getvalue()),'dtype':str(array.dtype),'shape':list(array.shape)}

    def capture(self, observation, *, success=False, terminated=False, truncated=False):
        import av
        if len(self.rows)>self.horizon: raise ValueError('Frame bound exceeded')
        instruction=observation['annotation.human.task_description']
        if not isinstance(instruction,str) or not instruction or len(instruction)>4096:
            raise ValueError('Bounded task instruction required')
        if self.instruction is not None and instruction!=self.instruction: raise ValueError('Instruction changed within episode')
        self.instruction=instruction
        model_state=np.array(self.entry.observation_to_state(observation),copy=True)
        if model_state.shape!=(14,) or not np.isfinite(model_state).all(): raise ValueError('Official14D state view differs')
        state,raw={},{}
        for name,width in STATE_FIELDS:
            array=np.asarray(observation[name])
            if array.shape!=(width,) or array.dtype.kind!='f': raise ValueError('Native named state geometry differs')
            raw[name]=self.array(array); state[name]=array.tolist()
        images={key:np.array(image,copy=True) for key,image in self.entry.collect_images(observation).items()}
        if set(images)!=set(self.entry.CAMERA_KEYS): raise ValueError('Camera set differs')
        for key,image in images.items():
            if not re.fullmatch(r'[A-Za-z0-9_.-]{1,100}',key): raise ValueError('Safe camera identifier required')
            if image.shape!=(256,256,3) or image.dtype!=np.uint8: raise ValueError('Pinned camera geometry differs')
            source=np.asarray(observation[key])
            if source.dtype!=np.uint8 or not np.array_equal(source,image):
                raise ValueError('Source camera bytes differ from official view')
            raw[key]=self.array(image)
            if key not in self.writers:
                path=self.directory/(key+'.mp4')
                container=av.open(str(path),'w'); stream=container.add_stream('libx264',rate=20)
                stream.width=stream.height=256; stream.pix_fmt='yuv420p'
                self.writers[key]=(container,stream)
            container,stream=self.writers[key]
            for packet in stream.encode(av.VideoFrame.from_ndarray(image,format='rgb24')): container.mux(packet)
        index=len(self.rows)
        ref=self.put(canonical({'native':raw,'instruction':instruction}))
        self.rows.append({'step':index,'time_ns':index*50_000_000,'frame_id':frame_id(self.episode_id,index),
            'state':sum((state[name] for name,_ in STATE_FIELDS),[]),'observation':ref,
            'action':None,'query_id':None,'chunk_offset':None,'reward':None,
            'success':bool(success),'terminated':bool(terminated),'truncated':bool(truncated)})
        # Query validation needs only the official seven-observation history.
        self.model_states.append(model_state)
        self.images.append(images)
        if len(self.model_states)>7: self.model_states.pop(0); self.images.pop(0)
        return ref

    def infer(self, native_infer, states, images, instruction):
        if not self.rows or self.pending is not None or len(self.queries)>=math.ceil(self.horizon/self.cadence):
            raise ValueError('Query outside bounded official schedule')
        frame=len(self.rows)-1
        if frame!=len(self.queries)*self.cadence or any(self.rows[-1][k] for k in ('success','terminated','truncated')):
            raise ValueError('Query outside official cadence or episode boundary')
        indices,valid=window_indices(frame,len(self.rows),(-6,-4,-2,0))
        base=max(0,len(self.rows)-7)
        expected=np.stack([self.model_states[i-base] for i in indices])
        if instruction!=self.instruction or not np.array_equal(states,expected) or set(images)!=set(self.entry.CAMERA_KEYS):
            raise ValueError('Official query history differs from recorded frames')
        for key,array in images.items():
            if not np.array_equal(array,np.stack([self.images[i-base][key] for i in indices])):
                raise ValueError('Official camera history differs from recorded frames')
        query={'query_id':len(self.queries),'at_frame':frame,
            'history_frames':[self.rows[i]['frame_id'] for i in indices], 'history_valid':list(valid),
            'input':self.put(canonical({'state_view':self.array(states),
                'camera_views':{key:self.array(value) for key,value in images.items()},'instruction':instruction})),
            'submitted_ns':time.monotonic_ns()}
        self.queries.append(query)
        try:
            chunk=np.asarray(native_infer(states,images,instruction))
            if chunk.shape!=(16,12) or chunk.dtype!=np.float32 or not np.isfinite(chunk).all():
                raise ValueError('Native decoded action chunk geometry differs')
            query.update(finished_ns=time.monotonic_ns(),chunk=self.array(chunk),status='completed')
            self.active_query=(query['query_id'],np.array(chunk,copy=True),0)
            return chunk
        except Exception as error:
            query.update(finished_ns=time.monotonic_ns(),status='failed',failure=failure(error,'official.infer'))
            raise

    def convert(self, native_convert, action):
        if self.active_query is None or self.pending is not None: raise ValueError('Executed action has no query')
        query_id,chunk,offset=self.active_query
        if offset>=self.cadence or not np.array_equal(np.asarray(action),chunk[offset]):
            raise ValueError('Executed action differs from official cadence/chunk')
        converted=native_convert(action)
        self.pending=(query_id,offset,np.array(action,copy=True))
        return converted

    def step(self, native_step, action):
        if self.pending is None or self.rows[-1]['terminated'] or self.rows[-1]['truncated'] or self.rows[-1]['success']:
            raise ValueError('Step crosses recording boundary or lacks native action')
        query_id,offset,native=self.pending
        started=time.monotonic_ns()
        value=native_step(action)
        finished=time.monotonic_ns()
        observation,reward,done,truncated,info=value
        execution={'source_frame':self.rows[-1]['frame_id'],'query_id':query_id,'chunk_offset':offset,
            'action':native.tolist(),'observation_captured':False,
            'native_step_started_ns':started,'native_step_finished_ns':finished}
        self.executions.append(execution)
        if not np.isfinite(reward): raise ValueError('Finite reward required')
        prior=self.rows[-1]
        self.capture(observation,success=info.get('success',False),terminated=done,truncated=truncated)
        prior.update(action=native.tolist(),query_id=query_id,chunk_offset=offset,reward=float(reward))
        execution['observation_captured']=True
        self.active_query=(query_id,self.active_query[1],offset+1); self.pending=None
        return value

    def close_videos(self):
        writers,self.writers=self.writers,{}
        errors=[]
        for key,(container,stream) in writers.items():
            try:
                try:
                    for packet in stream.encode(): container.mux(packet)
                finally: container.close()
            except Exception as error:
                self.video_errors[key]=failure(error,'chords.video_flush');errors.append(error)
        if errors: raise errors[0]

    def artifact(self, path):
        # Resolve each component through directory descriptors: no symlink can
        # redirect an evaluator artifact outside the owned output tree.
        relative=Path(path).absolute().relative_to(self.directory.parent.absolute())
        if any(part in ('.','..') for part in relative.parts): raise ValueError('Artifact escapes output')
        descriptor=os.open(self.directory.parent,os.O_RDONLY|os.O_DIRECTORY|os.O_NOFOLLOW)
        try:
            for part in relative.parts[:-1]:
                child=os.open(part,os.O_RDONLY|os.O_DIRECTORY|os.O_NOFOLLOW,dir_fd=descriptor)
                os.close(descriptor);descriptor=child
            handle=os.open(relative.name,os.O_RDONLY|os.O_NOFOLLOW|os.O_NONBLOCK,dir_fd=descriptor)
        finally: os.close(descriptor)
        with os.fdopen(handle,'rb') as source:
            metadata=os.fstat(source.fileno())
            if not stat.S_ISREG(metadata.st_mode) or metadata.st_size>24*1024**2:
                raise ValueError('Bounded regular artifact required')
            checksum=hashlib.sha256()
            count=0
            for chunk in iter(lambda:source.read(CHUNK_BYTES),b''):
                count+=len(chunk)
                if count>metadata.st_size: raise ValueError('Artifact changed during read')
                checksum.update(chunk)
            if count!=metadata.st_size: raise ValueError('Artifact changed during read')
            ref=ObjectRef(checksum.hexdigest(),metadata.st_size);self.reserve(ref.sha256,ref.size)
            source.seek(0)
            if self.store.put_stream(ref,iter(lambda:source.read(CHUNK_BYTES),b''))!=ref:
                raise ValueError('Artifact checksum differs')
        return dict(ref.to_dict(),object=ref.sha256)

    def finish(self, result, official_video=None):
        import pyarrow as pa
        import pyarrow.parquet as pq
        self.close_videos()
        if not self.rows: raise ValueError('Cannot release an empty recording')
        for query in self.queries:
            offsets=[item['chunk_offset'] for item in self.executions if item['query_id']==query['query_id']]
            query['executed_offsets']=offsets
            query['discarded_offsets']=[i for i in range(16) if i not in offsets] if 'chunk' in query else []
        schema=pa.schema([('step',pa.int64()),('time_ns',pa.int64()),('frame_id',pa.string()),
            ('state',pa.list_(pa.float64(),16)),('observation',pa.string()),
            ('action',pa.list_(pa.float32())),('query_id',pa.int64()),('chunk_offset',pa.int64()),
            ('reward',pa.float64()),('success',pa.bool_()),('terminated',pa.bool_()),('truncated',pa.bool_())])
        path=self.directory/'signals.parquet'; pq.write_table(pa.Table.from_pylist(self.rows,schema=schema),path,compression='zstd')
        videos={key.removeprefix('video.'):{**self.artifact(self.directory/(key+'.mp4')),'source_key':key,'fps':20,'frame_count':len(self.rows),
                'pixel_origin':'raw_rgb','encoding':'h264_yuv420p','loss':'lossy; immutable raw arrays retained'}
                for key in self.entry.CAMERA_KEYS if key not in self.video_errors
                and all(item['observation_captured'] for item in self.executions)}
        record={'schema_version':1,'kind':'generated_official_rollout','episode_id':self.episode_id,
            'initialization_group':self.group,'instruction':self.instruction,'length':len(self.rows),
            'signals':self.artifact(path)['sha256'],'state_layout':STATE_LAYOUT,'action_layout':ACTION_LAYOUT,
            'coordinate_conventions':{'state':'Native named source poses; quaternion xyzw, no normalization',
                'action':'Source controller commands; no absolute pose interpretation or rescaling',
                'clock':'episode-relative20Hz control index; not a real-time throughput guarantee'},
            'videos':videos,'queries':self.put(canonical({'queries':self.queries})),
            'official_result':result,'provenance':self.provenance,
            'executed_control_count':len(self.executions),
            'segment':{'start_control':0,'stop_control':len(self.rows)-1,
                'environment_ended':self.rows[-1]['terminated'] or self.rows[-1]['truncated'],
                'bounded_horizon_cut':len(self.rows)-1==self.horizon and not any(self.rows[-1][k] for k in ('success','terminated','truncated'))},
            'timing_conventions':'worker monotonic_ns for queries/native steps; recording overhead included only in evaluator wall time',
            'executions':self.put(canonical({'executions':self.executions})), 'video_failures':self.video_errors,
            'raw_source':'Lossless immutable .npy arrays for every observation and exact query inputs/chunks',
            'license':'Generated run; source code/model/asset licenses retained by pinned provenance; redistribution permission not inferred'}
        if official_video is not None:
            record['official_video']={**self.artifact(official_video),'video_stride':1,'fps':20,'transcoded':False}
        # Existing Chords publication traverses raw object references. Include the
        # complete immutable input/query/array closure, not only derived signals.
        record['raw']={key:{'object':key,'sha256':key,'bytes':size}
                       for key,size in sorted(self.seen.items())}
        overlay=self.put(canonical({'schema_version':1,'episode_id':self.episode_id,'split':'development',
            'success':result.get('success'),'quality':{'status':result['status'],
                'outcome_scope':result.get('outcome_scope','failed recording; outcome undefined'),
                'real_simulation':self.provenance.get('real_simulation',False)}}))
        episode=self.put(canonical(record))
        manifest=canonical({'schema_version':1,'episodes':[episode],'parent':None,'overlays':[overlay]})
        self.reserve(digest(manifest),len(manifest))
        return release(self.store,[episode],overlays=(overlay,))


def evaluate_recorded(entry, gym, client, settings, get_task_horizon, convert_action, output, *,
                      task, task_index, episode_index, seed, run_id, store, provenance):
    """No control loop here: wrapping reset/step/infer leaves evaluation upstream."""
    if (settings.obs_history,settings.obs_interval,settings.video_stride,settings.video_fps)!=(4,2,1,20):
        raise ValueError('Pinned history and official video timing required')
    horizon=settings.horizon or get_task_horizon(task)
    if (settings.seed!=1000 or settings.num_trials!=50 or settings.split!='pretrain'
            or seed!=settings.seed+task_index*settings.num_trials+episode_index):
        raise ValueError('Pinned constructor/split/episode seed schedule required')
    task_output=Path(output)/task
    if task_output.exists() or task_output.is_symlink(): raise ValueError('Official task output already exists')
    recorder=Recorder(entry,store,Path(output)/'chords-recording',run_id=run_id,task=task,seed=seed,
        cadence=settings.replan_steps,horizon=horizon,provenance=provenance)
    native_infer=client.infer
    class WrappedEnvironment:
        def __init__(self,env): self.env=env
        def reset(self,**kwargs):
            if kwargs.get('seed')!=seed or recorder.rows: raise ValueError('Single exact seeded episode required')
            clock=float(self.env.unwrapped.env.control_timestep)
            if not math.isfinite(clock) or round(clock*1e9)!=50_000_000:
                raise ValueError('Pinned official20Hz control clock required')
            value=self.env.reset(**kwargs); recorder.capture(value[0]); return value
        def step(self,action): return recorder.step(self.env.step,action)
        def close(self): self.env.close()
    wrapped_gym=SimpleNamespace(make=lambda *args,**kwargs:WrappedEnvironment(gym.make(*args,**kwargs)))
    client.infer=lambda states,images,instruction:recorder.infer(native_infer,states,images,instruction)
    component='official.evaluate_task'
    try:
        stats=entry.evaluate_task(task,task_index,settings,client,wrapped_gym,get_task_horizon,
            lambda action:recorder.convert(convert_action,action),Path(output),
            episode_indices=[episode_index],show_progress=False,write_task_stats=True)
        episode=stats['episodes'][0]
        if (len(stats['episodes'])!=1 or episode['seed']!=seed or episode['steps']!=len(recorder.rows)-1
                or bool(episode['success'])!=recorder.rows[-1]['success']):
            raise ValueError('Official result differs from recording')
        status='success' if episode['success'] else 'failure'
        video=Path(output)/task/f'episode_{episode_index:03d}_seed_{seed}_{status}.mp4'
        result=dict(episode,status='completed',horizon=horizon,
                    outcome_scope='bounded prefix; success=False does not establish full-episode failure')
        component='chords.finish'
        key=recorder.finish(result,video if video.is_file() else None)
        return stats,key
    except Exception as error:
        result={'status':'failed','comparison_valid':False,'failure':failure(error,component)}
        if recorder.executions and not recorder.executions[-1]['observation_captured']:
            result['unrecorded_transition']=recorder.executions[-1]
        (recorder.directory/'failure.json').write_bytes(canonical(result))
        try: recorder.close_videos()
        except Exception as video_error: result['video_failure']=failure(video_error,'chords.video_flush')
        if recorder.rows:
            try: result['partial_release']=recorder.finish(result)
            except Exception as capture_error: result['partial_release_failure']=failure(capture_error,'chords.partial_finish')
        (recorder.directory/'failure.json').write_bytes(canonical(result))
        raise
    finally:
        client.infer=native_infer
        recorder.close_videos()
