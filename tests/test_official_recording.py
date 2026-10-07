"""CPU fixtures exercise recording; optional pinned evaluator still uses fake physics/model."""
from collections import deque
import hashlib
import importlib.util
import io
import json
import os
from pathlib import Path
import tempfile
from types import ModuleType, SimpleNamespace
import unittest
from unittest.mock import patch
from unittest.mock import Mock

import numpy as np

from coppertop.chords import LocalStore, canonical, digest, fork
from coppertop.diagnostics import failure
from coppertop.official_recording import Recorder, STATE_FIELDS, acceptance_plan, evaluate_recorded

CAMERAS=('video.robot0_agentview_left','video.robot0_agentview_right','video.robot0_eye_in_hand')


def observation(index):
    obs={name:np.arange(width,dtype=np.float64)+index*100+i*10 for i,(name,width) in enumerate(STATE_FIELDS)}
    obs.update({key:np.full((256,256,3),index*3+i,dtype=np.uint8) for i,key in enumerate(CAMERAS)})
    obs['annotation.human.task_description']='Put mug under machine'
    return obs


def history(queue,length,interval):
    values=list(queue)
    return np.stack([values[max(0,len(values)-1-(length-1-i)*interval)] for i in range(length)])


def model_state(obs):
    return np.concatenate([obs['state.end_effector_position_relative'],np.zeros(3),
        obs['state.gripper_qpos'],obs['state.base_position'],np.zeros(3)]).astype(np.float32)


class Env:
    def __init__(self,stop=16): self.stop=stop; self.closed=False; self.index=0; self.actions=[]
    @property
    def unwrapped(self): return SimpleNamespace(env=SimpleNamespace(control_timestep=.05))
    def reset(self,seed): self.seed=seed; return observation(0),{}
    def step(self,action):
        self.actions.append(action); self.index+=1
        return observation(self.index),float(self.index),self.index==self.stop,False,{'success':False}
    def close(self): self.closed=True


class Client:
    def __init__(self,fail=False): self.calls=[]; self.fail=fail
    def infer(self,states,images,instruction):
        self.calls.append(states.copy())
        if self.fail: raise RuntimeError('SECRET=do-not-export https://host/?token=private')
        return np.arange(192,dtype=np.float32).reshape(16,12)+100*len(self.calls)


def settings(cadence=16,horizon=16):
    return SimpleNamespace(obs_history=4,obs_interval=2,video_stride=1,video_fps=20,
        replan_steps=cadence,horizon=horizon,seed=1000,num_trials=50,split='pretrain',
        save_videos=True,save_failure_videos=False)


class FixtureEntry:
    CAMERA_KEYS=CAMERAS
    observation_to_state=staticmethod(model_state)
    collect_images=staticmethod(lambda obs:{key:obs[key] for key in CAMERAS})

    @staticmethod
    def evaluate_task(task,task_index,args,client,gym,get_horizon,convert,output,**kwargs):
        env=gym.make('robocasa/'+task,split=args.split,seed=args.seed)
        frames=[]
        try:
            obs,_=env.reset(seed=args.seed+task_index*args.num_trials+kwargs['episode_indices'][0])
            states,images=deque(maxlen=7),{key:deque(maxlen=7) for key in CAMERAS}
            states.append(model_state(obs))
            for key in CAMERAS: images[key].append(obs[key])
            queue=deque(); done=truncated=False; success=False; steps=0
            while steps<args.horizon:
                if not queue:
                    chunk=client.infer(history(states,4,2),{key:history(values,4,2) for key,values in images.items()},obs['annotation.human.task_description'])
                    queue.extend(chunk[:args.replan_steps])
                obs,_,done,truncated,info=env.step(convert(queue.popleft()))
                states.append(model_state(obs))
                for key in CAMERAS: images[key].append(obs[key])
                steps+=1; success=bool(info.get('success',False))
                if success or done or truncated: break
            return {'horizon':args.horizon,'episodes':[{'episode':0,'seed':1150,'steps':steps,'success':success}]}
        finally: env.close()


class RecordingTests(unittest.TestCase):
    def test_capture_failure_retains_executed_control_receipt(self):
        class BadView(FixtureEntry):
            @staticmethod
            def observation_to_state(obs):
                return np.zeros(13) if obs['state.base_position'][0]>0 else model_state(obs)
        with tempfile.TemporaryDirectory() as root:
            env=Env();store=LocalStore(Path(root)/'store')
            with self.assertRaisesRegex(ValueError,'Official14D state view differs'):
                evaluate_recorded(BadView,SimpleNamespace(make=lambda *a,**kw:env),Client(),settings(),
                    lambda task:600,tuple,root,task='CoffeeSetupMug',task_index=3,episode_index=0,seed=1150,
                    run_id='capture-failure',store=store,provenance={'real_simulation':False})
            result=json.loads((Path(root)/'chords-recording/failure.json').read_text())
            self.assertEqual(result['unrecorded_transition']['action'],list(env.actions[0]))
            release=json.loads(store.get(result['partial_release']))
            record=json.loads(store.get(release['episodes'][0]))
            executions=json.loads(store.get(record['executions']))['executions']
            self.assertEqual(executions[0]['action'],list(env.actions[0]))
            self.assertFalse(executions[0]['observation_captured']);self.assertEqual(record['length'],1)
            self.assertEqual(record['videos'],{})

    def test_flush_error_closes_every_video_and_cannot_be_retried_implicitly(self):
        with tempfile.TemporaryDirectory() as root:
            r=Recorder(FixtureEntry,LocalStore(Path(root)/'store'),Path(root)/'record',run_id='flush',
                task='CoffeeSetupMug',seed=1150,cadence=16,horizon=16,provenance={})
            containers=[Mock(),Mock()]
            r.writers={'first':(containers[0],SimpleNamespace(encode=Mock(side_effect=RuntimeError('codec error')))),
                'second':(containers[1],SimpleNamespace(encode=lambda:[]))}
            with self.assertRaisesRegex(RuntimeError,'codec error'):r.close_videos()
            self.assertTrue(all(container.close.call_count==1 for container in containers))
            self.assertEqual(r.writers,{})
            r.close_videos()
            self.assertEqual(containers[0].close.call_count,1)

    def test_artifacts_reject_file_and_directory_symlinks(self):
        with tempfile.TemporaryDirectory() as root:
            output=Path(root)/'owned';output.mkdir()
            r=Recorder(FixtureEntry,LocalStore(output/'store'),output/'record',run_id='links',
                task='CoffeeSetupMug',seed=1150,cadence=16,horizon=16,provenance={})
            outside=Path(root)/'outside';outside.mkdir();(outside/'secret').write_bytes(b'private-value')
            (output/'linked').symlink_to(outside/'secret')
            (output/'linked-directory').symlink_to(outside,target_is_directory=True)
            for path in (output/'linked',output/'linked-directory'/'secret'):
                with self.assertRaises(OSError):r.artifact(path)
            os.mkfifo(output/'fifo')
            with self.assertRaisesRegex(ValueError,'regular artifact'):r.artifact(output/'fifo')
            self.assertNotIn(digest(b'private-value'),r.seen)

    def collect(self,root,*,entry=FixtureEntry,cadence=16,stop=16,client=None,store=None):
        env=Env(stop); client=client or Client(); store=store or LocalStore(Path(root)/'store')
        original=client.infer
        stats,key=evaluate_recorded(entry,SimpleNamespace(make=lambda *a,**kw:env),client,settings(cadence),
            lambda task:600,lambda action:tuple(float(v) for v in action),root,
            task='CoffeeSetupMug',task_index=3,episode_index=0,seed=1150,run_id='fixture'+str(cadence),
            store=store,provenance={'source_pins':{'fixture':True},'real_simulation':False})
        self.assertEqual(client.infer,original); self.assertTrue(env.closed)
        release=json.loads(store.get(key)); record=json.loads(store.get(release['episodes'][0]))
        return env,client,store,key,record

    def test_native_roundtrip_histories_video_and_thin_fork(self):
        import av
        import pyarrow.parquet as pq
        with tempfile.TemporaryDirectory() as root:
            env,client,store,key,record=self.collect(root,cadence=8)
            rows=pq.read_table(io.BytesIO(store.get(record['signals']))).to_pylist()
            self.assertEqual(len(rows),17);self.assertIsNone(rows[-1]['action'])
            self.assertEqual([tuple(row['action']) for row in rows[:-1]],env.actions)
            self.assertFalse(record['official_result']['success']);self.assertTrue(rows[-1]['terminated'])
            self.assertEqual(len({row['frame_id'] for row in rows}),17)
            self.assertEqual(rows[-1]['time_ns'],800_000_000)
            queries=json.loads(store.get(record['queries']))['queries']
            self.assertEqual(len(queries),2);self.assertEqual(queries[1]['at_frame'],8)
            self.assertEqual(queries[0]['discarded_offsets'],list(range(8,16)))
            self.assertEqual(queries[1]['history_frames'],[rows[i]['frame_id'] for i in (2,4,6,8)])
            self.assertEqual(queries[0]['history_valid'],[False,False,False,True])
            self.assertEqual([row['chunk_offset'] for row in rows[:-1]],list(range(8))*2)
            raw=json.loads(store.get(rows[8]['observation']))['native']
            for name,width in STATE_FIELDS:
                np.testing.assert_array_equal(np.load(io.BytesIO(store.get(raw[name]['object'])),allow_pickle=False),observation(8)[name])
            for camera,video in record['videos'].items():
                with av.open(io.BytesIO(store.get(video['sha256']))) as container:
                    frames=list(container.decode(video=0))
                    self.assertEqual(len(frames),17)
                    self.assertEqual([float(f.time) for f in frames],[i/20 for i in range(17)])
            branch=fork(store,key)
            self.assertEqual(json.loads(store.get(branch))['episodes'],json.loads(store.get(key))['episodes'])
            from coppertop.publication import publish_release,release_objects
            destination=LocalStore(Path(root)/'roundtrip')
            self.assertEqual(publish_release(store,destination,branch),branch)
            closure=release_objects(store,branch,max_bytes=24*1024**2)
            self.assertTrue(set(record['raw']).issubset(closure))
            self.assertEqual(destination.get(record['queries']),store.get(record['queries']))

    def test_cadence_is_owned_by_evaluator_and_groups_stay_together(self):
        with tempfile.TemporaryDirectory() as root:
            records=[]
            for cadence,count in ((16,1),(8,2)):
                directory=Path(root)/str(cadence);directory.mkdir()
                _,client,_,_,record=self.collect(directory,cadence=cadence)
                self.assertEqual(len(client.calls),count);records.append(record)
            self.assertEqual(records[0]['initialization_group'],records[1]['initialization_group'])
            self.assertNotEqual(records[0]['episode_id'],records[1]['episode_id'])

    def test_premature_official_end_is_preserved_not_invented_success(self):
        with tempfile.TemporaryDirectory() as root:
            env,_,_,_,record=self.collect(root,stop=4)
            self.assertEqual(record['length'],5);self.assertFalse(record['official_result']['success'])
            self.assertEqual(len(env.actions),4)

    def test_query_failure_preserves_partial_release_and_sanitizes_values(self):
        with tempfile.TemporaryDirectory() as root:
            env=Env();client=Client(fail=True);store=LocalStore(Path(root)/'store');original=client.infer
            with self.assertRaises(RuntimeError):
                evaluate_recorded(FixtureEntry,SimpleNamespace(make=lambda *a,**kw:env),client,settings(),lambda task:600,
                    tuple,root,task='CoffeeSetupMug',task_index=3,episode_index=0,seed=1150,run_id='failure',store=store,
                    provenance={'real_simulation':False})
            raw=(Path(root)/'chords-recording/failure.json').read_bytes()
            self.assertNotIn(b'do-not-export',raw);self.assertNotIn(b'token=private',raw)
            result=json.loads(raw);self.assertEqual(result['failure']['component'],'official.evaluate_task')
            self.assertEqual(result['failure']['message'],'exception_text_omitted')
            release=json.loads(store.get(result['partial_release']))
            episode=json.loads(store.get(release['episodes'][0]))
            query=json.loads(store.get(episode['queries']))['queries'][0]
            self.assertEqual(query['status'],'failed');self.assertGreater(query['finished_ns'],query['submitted_ns'])
            self.assertEqual(client.infer,original);self.assertTrue(env.closed)

    def test_bounds_bad_history_and_unaligned_native_action_rejected(self):
        with tempfile.TemporaryDirectory() as root:
            r=Recorder(FixtureEntry,LocalStore(Path(root)/'store'),Path(root)/'record',run_id='test',
                task='CoffeeSetupMug',seed=1150,cadence=16,horizon=16,provenance={})
            try:
                r.capture(observation(0))
                bad=np.zeros((4,14),dtype=np.float32)
                with self.assertRaisesRegex(ValueError,'history differs'):
                    r.infer(Client().infer,bad,{key:np.stack([observation(0)[key]]*4) for key in CAMERAS},r.instruction)
                states=np.stack([model_state(observation(0))]*4)
                r.infer(Client().infer,states,{key:np.stack([observation(0)[key]]*4) for key in CAMERAS},r.instruction)
                with self.assertRaisesRegex(ValueError,'cadence/chunk'):
                    r.convert(tuple,np.zeros(12,dtype=np.float32))
                with self.assertRaisesRegex(ValueError,'byte bound'):
                    r.reserve('a'*64,24*1024**2)
            finally:r.close_videos()

    def test_pure_acceptance_plan_is_small_and_separate_from_comparison(self):
        p=acceptance_plan();self.assertFalse(p['cloud_actions'])
        self.assertEqual(p['limits']['query_calls'],1);self.assertEqual(p['limits']['horizon'],4)
        self.assertIn('Separate',p['later_comparison'])

    @unittest.skipUnless(os.environ.get('COPPERTOP_OFFICIAL_ENTRY'),'Pinned official evaluator source not provided')
    def test_pinned_official_evaluator_with_fake_model_and_environment(self):
        import av
        from coppertop.performance_eval import SOURCE_BLOBS
        path=Path(os.environ['COPPERTOP_OFFICIAL_ENTRY']);raw=path.read_bytes()
        self.assertEqual(hashlib.sha1(b'blob '+str(len(raw)).encode()+b'\0'+raw).hexdigest(),SOURCE_BLOBS['eval_robocasa365/entry.py'])
        def save_video(path,frames,fps):
            with av.open(str(path),'w') as container:
                stream=container.add_stream('libx264',rate=fps)
                stream.height,stream.width=frames[0].shape[:2];stream.pix_fmt='yuv420p'
                for frame in frames:
                    for packet in stream.encode(av.VideoFrame.from_ndarray(frame,format='rgb24')):container.mux(packet)
                for packet in stream.encode():container.mux(packet)
        imageio=ModuleType('imageio');imageio.__path__=[]
        imageio.v2=SimpleNamespace(mimsave=save_video)
        with patch.dict('sys.modules',{'client':SimpleNamespace(Client=object),'imageio':imageio,'imageio.v2':imageio.v2}):
            spec=importlib.util.spec_from_file_location('fixture_official',path);entry=importlib.util.module_from_spec(spec);spec.loader.exec_module(entry)
        # Use the actual pinned state/history/evaluator code, still fake physics/model.
        for horizon in (4,16):
            with self.subTest(horizon=horizon),tempfile.TemporaryDirectory() as root:
                env=Env(horizon);client=Client();store=LocalStore(Path(root)/'store')
                stats,key=evaluate_recorded(entry,SimpleNamespace(make=lambda *a,**kw:env),client,settings(horizon=horizon),lambda task:600,
                    tuple,root,task='CoffeeSetupMug',task_index=3,episode_index=0,seed=1150,run_id='official-code-fixture',
                    store=store,provenance={'real_simulation':False})
                record=json.loads(store.get(json.loads(store.get(key))['episodes'][0]))
                video=Path(root)/'CoffeeSetupMug/episode_000_seed_1150_failure.mp4'
                self.assertEqual(store.get(record['official_video']['sha256']),video.read_bytes())
                self.assertFalse(record['official_video']['transcoded']);self.assertEqual(len(client.calls),1)
                self.assertEqual(stats['episodes'][0]['steps'],horizon);self.assertTrue(env.closed)
                if horizon==4:
                    from coppertop.publication import release_objects
                    self.assertLessEqual(len(release_objects(store,key,max_bytes=24*1024**2)),64)


class DiagnosticTests(unittest.TestCase):
    def test_entrypoint_captures_failures_before_recording_setup(self):
        from coppertop import performance_runtime
        with tempfile.TemporaryDirectory() as root:
            error=ValueError('SECRET=never-copy-this')
            with patch.object(performance_runtime,'rollout',side_effect=error):
                with self.assertRaises(ValueError):
                    performance_runtime.main(['--rollout','--arm','official','--seed','1150','--output',root,
                        '--record-chords','--record-run-id','failed-setup'])
            raw=(Path(root)/'recording-failure.json').read_bytes()
            self.assertNotIn(b'never-copy-this',raw)
            self.assertEqual(json.loads(raw)['failure']['component'],'official.rollout_entrypoint')

    def test_owned_replay_guard_preserves_safe_message_and_component(self):
        from coppertop.mechanism import replay
        from test_mechanism import LatentEnv,tape
        class BadReset(LatentEnv):
            def reset(self,seed):super().reset(seed);self.physics=1;return self.frame()
        try:replay(BadReset,tape(),prefix_steps=4,continuation_steps=2)
        except ValueError as error:result=failure(error,'mechanism.replay')
        self.assertEqual(result['message'],'Reset parity failed')
        self.assertEqual(result['traceback'][-1]['file'],'mechanism.py')
        self.assertEqual(result['traceback'][-1]['function'],'replay')

    def test_message_paths_locals_and_traceback_are_bounded(self):
        try: raise ValueError('AWS_SECRET_ACCESS_KEY=private-value /Users/private/account')
        except ValueError as error: result=failure(error,'official.evaluate_task')
        raw=canonical(result)
        self.assertNotIn(b'private-value',raw);self.assertNotIn(b'/Users/private',raw)
        self.assertLess(len(raw),4096);self.assertLessEqual(len(result['traceback']),12)
        self.assertEqual(result['message'],'exception_text_omitted')
