"""Deterministic fake integration; official simulator and CUDA are not executed."""
import json
from collections import deque
from dataclasses import replace
from pathlib import Path
import tempfile
import threading
import time
from types import SimpleNamespace
import unittest
from unittest.mock import patch

import numpy as np

from coppertop.mechanism import native_actions
from coppertop.mechanism_policy import SeededModel
from coppertop.mechanism_robocasa import RoboCasaReplay, evidence
from coppertop.mechanism_runner import delayed_rollout
from coppertop.mechanism_runtime import experiment, input_hash, plan, save_trace
from test_mechanism import LatentEnv


class FakeEnv(LatentEnv):
    def __init__(self, on_step=lambda index: None):
        super().__init__()
        self.on_step = on_step
        self.control_ns = 5_000_000
        self.coverage = {'complete_state_certified': False, 'fixture': True}
        self.entry = SimpleNamespace(CAMERA_KEYS=('camera',),
            observation_to_state=lambda obs: obs['state'])

    def reset(self, seed):
        result = super().reset(seed)
        self.observe()
        return result

    def observe(self):
        self.observation = {'state': np.full(14, self.physics, dtype=np.float32),
                            'camera': np.zeros((256, 256, 3), dtype=np.uint8)}

    def step(self, action):
        result = super().step(action)
        self.observe()
        self.on_step(self.index)
        return result

    def inputs(self):
        state = np.tile(self.observation['state'], (4, 1))
        images = {'camera': np.tile(self.observation['camera'], (4, 1, 1, 1))}
        for array in (state, *images.values()): array.setflags(write=False)
        return state, images, 'instruction at step '+str(self.index)


class FakePolicy:
    def __init__(self, release):
        self.release, self.cancelled = release, threading.Event()
        self.calls = []

    def __call__(self, inputs, seed):
        self.calls.append((input_hash(inputs), seed))
        if len(self.calls) == 5:
            if not self.release.wait(1): raise TimeoutError('Fake release missing')
        return np.arange(192, dtype=np.float32).reshape(16, 12) / 100 + seed % 100

    def cancel(self):
        self.cancelled.set()
        self.release.set()


class RunnerTests(unittest.TestCase):
    def test_collector_accepts_final_stop_but_rejects_premature_stop(self):
        for flag in ('terminated','truncated'):
            for end_step in (15,16):
                with self.subTest(flag=flag,end_step=end_step):
                    release, instances = threading.Event(), []
                    class EndingEnv(FakeEnv):
                        def step(self, action):
                            frame=super().step(action)
                            return replace(frame,**{flag:self.index==end_step})
                    def factory():
                        release.clear()
                        env=EndingEnv(lambda index: release.set() if index>=6 else None)
                        env.control_ns=20_000_000
                        instances.append(env)
                        return env
                    policy=FakePolicy(release)
                    if end_step==15:
                        with self.assertRaisesRegex(ValueError,'Baseline ended before required'):
                            experiment(factory,policy)
                        self.assertEqual(len(policy.calls),1)
                    else:
                        result,_,tape,_,branch_tape=experiment(factory,policy)
                        self.assertEqual(result['query_calls'],5)
                        self.assertEqual(result['parity']['status'],'bounded_parity')
                        self.assertEqual(len(tape.actions),16)
                        self.assertTrue(getattr(tape.frames[-1],flag))
                        self.assertTrue(getattr(branch_tape.frames[-1],flag))
                    self.assertTrue(all(env.closed for env in instances))

    def test_adapter_forwards_native_order_updates_history_and_detaches_inputs(self):
        seen = []
        def convert(action):
            seen.append(action.copy())
            return {'ee_position': action[:3], 'ee_rotation': action[3:6],
                    'gripper': action[6:7], 'base': action[7:11], 'mode': action[11:]}
        observation = {'state': np.zeros(14, dtype=np.float32),
                       'camera': np.zeros((256, 256, 3), dtype=np.uint8),
                       'annotation.human.task_description': 'fixed instruction'}
        def step(action):
            self.assertEqual(action['base'].tolist(), [7., 8., 9., 10.])
            self.assertEqual(action['mode'].tolist(), [11.])
            return observation, 0, False, False, {'success': False}
        adapter = RoboCasaReplay.__new__(RoboCasaReplay)
        adapter.entry = SimpleNamespace(CAMERA_KEYS=('camera',),
            observation_to_state=lambda obs: obs['state'],
            collect_images=lambda obs: {'camera': obs['camera']},
            sample_history=lambda queue, length, interval: np.stack(
                [list(queue)[max(0, len(queue)-1-(length-1-i)*interval)] for i in range(length)]))
        adapter.env = SimpleNamespace(step=step)
        adapter.observation, adapter.index, adapter.flags = observation, 0, (False, False, False)
        adapter.states, adapter.images = deque(maxlen=7), {'camera': deque(maxlen=7)}
        adapter.frame = lambda: adapter.index
        adapter._append()
        snapshot = adapter.inputs()
        with patch.dict('sys.modules', {'robocasa.utils.env_utils': SimpleNamespace(convert_action=convert)}):
            self.assertEqual(adapter.step(tuple(float(i) for i in range(12))), 1)
        np.testing.assert_array_equal(seen[0], np.arange(12, dtype=np.float32))
        self.assertEqual(len(adapter.states), 2)
        observation['state'][:] = 99
        self.assertTrue((snapshot[0] == 0).all())
        self.assertFalse(snapshot[0].flags.writeable)

    def test_fast_query_waits_for_first_control_boundary(self):
        class Immediate:
            def __call__(self, inputs, seed): return np.ones((16, 12))
            def cancel(self): pass
        env = FakeEnv()
        env.reset(1150)
        result = delayed_rollout(env, Immediate(), env.inputs(), np.zeros((16, 12)),
            boundary=4, seed=7, control_ns=20_000_000, steps=3, timeout_seconds=.2)
        self.assertEqual(result['committed_steps'], 1)
        self.assertEqual(result['actions'][0], (0.,)*12)
        self.assertEqual(result['actions'][1], (1.,)*12)

    def test_slow_final_control_cannot_pass_deadline_or_pacing_gate(self):
        release = threading.Event()
        class Query:
            def __call__(self, inputs, seed): release.wait(.2); return np.ones((16, 12))
            def cancel(self): release.set()
        class Slow(FakeEnv):
            def step(self, action):
                result = super().step(action)
                release.set()
                if self.index == 2: time.sleep(.04)
                return result
        for deadline, exception in ((.03, TimeoutError), (.2, RuntimeError)):
            release.clear()
            env = Slow()
            env.reset(1150)
            with self.assertRaises(exception):
                delayed_rollout(env, Query(), env.inputs(), np.zeros((16, 12)),
                    boundary=4, seed=7, control_ns=10_000_000, steps=2, timeout_seconds=deadline)

    def test_complete_collector_replay_diversity_async_handoff_and_publication(self):
        release, instances = threading.Event(), []
        def factory():
            release.clear()
            instance = FakeEnv(lambda index: release.set() if index >= 6 else None)
            instances.append(instance)
            return instance
        policy = FakePolicy(release)
        journal = []
        result, baseline, tape, branch, branch_tape = experiment(factory, policy,
            log_sink=lambda records: journal.append(json.loads(json.dumps(records))))
        self.assertEqual(len(policy.calls), 5)
        self.assertEqual(len(journal[-1]), 5)
        self.assertEqual(result['query_calls'], 5)
        self.assertEqual(result['discarded_query_calls'], 3)
        self.assertGreaterEqual(result['committed_steps'], 2)
        self.assertLess(result['committed_steps'], 12)
        self.assertTrue(all(env.closed for env in instances))
        self.assertEqual(policy.calls[1][0], policy.calls[4][0])
        delay = result['committed_steps']
        self.assertEqual(branch_tape.actions[:4+delay], tape.actions[:4+delay])
        self.assertNotEqual(branch_tape.actions[4+delay:], tape.actions[4+delay:])
        # Every control coordinate survives; no base/EE/gripper reorder is introduced.
        candidate = native_actions(np.arange(192, dtype=np.float32).reshape(16, 12)/100+8, chunk=True)
        self.assertEqual(branch_tape.actions[4+delay], candidate[delay])
        with tempfile.TemporaryDirectory() as root:
            from coppertop.chords import LocalStore
            store = LocalStore(Path(root)/'store')
            prefix = save_trace(baseline, tape, Path(root)/'prefix', {'fixture': True}, stop=4+delay, store=store)
            save_trace(baseline, tape, Path(root)/'continue', {'fixture': True}, start=4+delay, store=store, parent=prefix)
            save_trace(branch, branch_tape, Path(root)/'replan', {'fixture': True}, start=4+delay, store=store, parent=prefix)
            manifest = json.loads((Path(root)/'continue/manifest.json').read_text())
            self.assertIn('signals.parquet', manifest['artifacts'])
            self.assertIn('camera.mp4', manifest['artifacts'])
            self.assertEqual(manifest['parent'], prefix)
            self.assertEqual(json.loads((Path(root)/'replan/manifest.json').read_text())['parent'], prefix)
            fork_manifest = json.loads((Path(root)/'replan/manifest.json').read_text())
            branch_audit = json.loads(store.get(fork_manifest['tape']))
            import pyarrow.parquet as pq
            prefix_ids = pq.read_table(Path(root)/'prefix/signals.parquet')['frame_id'].to_pylist()
            suffix = pq.read_table(Path(root)/'replan/signals.parquet')
            self.assertIsNone(suffix['action'].to_pylist()[-1])
            self.assertEqual(prefix_ids, [f['frame_id'] for f in branch_audit['frames'][:4+delay]])
            self.assertEqual(branch_tape.identity(4+delay), tape.identity(4+delay))
            self.assertNotEqual(branch_tape.identity(5+delay), tape.identity(5+delay))

    def test_timeout_cancels_query_no_late_handoff_or_hold(self):
        cancelled = threading.Event()
        class Blocked:
            def __call__(self, inputs, seed):
                cancelled.wait(1)
                return np.zeros((16, 12), dtype=np.float32)
            def cancel(self): cancelled.set()
        env = FakeEnv()
        env.reset(1150)
        with self.assertRaises(TimeoutError):
            delayed_rollout(env, Blocked(), env.inputs(), np.zeros((16, 12)),
                boundary=4, seed=7, control_ns=10_000_000, steps=12, timeout_seconds=.025)
        stopped_index = env.index
        self.assertTrue(cancelled.is_set())
        self.assertLessEqual(stopped_index, 3)
        self.assertEqual(env.index, stopped_index)

    def test_bad_query_fails_without_handoff(self):
        class Bad:
            def __call__(self, inputs, seed): return np.zeros((16, 4))
            def cancel(self): pass
        env = FakeEnv()
        env.reset(1150)
        with self.assertRaises(RuntimeError):
            delayed_rollout(env, Bad(), env.inputs(), np.zeros((16, 12)),
                boundary=4, seed=7, control_ns=1_000_000, steps=12, timeout_seconds=.1)

    def test_failed_call_remains_in_accounting_journal(self):
        class Failing:
            def __call__(self, inputs, seed): raise ValueError('failure')
        journal, instances = [], []
        def factory():
            env = FakeEnv()
            instances.append(env)
            return env
        with self.assertRaises(ValueError):
            experiment(factory, Failing(), log_sink=lambda records: journal.append(json.loads(json.dumps(records))))
        self.assertEqual(journal[-1][0]['error_type'], 'ValueError')
        self.assertTrue(instances[0].closed)

    def test_opaque_fields_are_omissions_and_array_changes_are_visible(self):
        missing = []
        result = evidence({'rng': np.random.default_rng(7), 'hidden': object(), 'array': np.zeros(3)}, missing)
        self.assertIsNone(result['hidden'])
        self.assertEqual(len(missing), 1)
        self.assertNotEqual(result['array'], evidence(np.ones(3), []))

    def test_seeded_wrapper_strips_seed_and_restores_rng_context(self):
        entered, seeds, forwarded = [], [], []
        class Context:
            def __enter__(self): entered.append('enter')
            def __exit__(self, *args): entered.append('exit')
        torch = SimpleNamespace(random=SimpleNamespace(fork_rng=lambda devices: Context()),
            manual_seed=seeds.append, cuda=SimpleNamespace(manual_seed_all=seeds.append))
        class Model:
            device, dtype = SimpleNamespace(index=0), 'dtype'
            def __call__(self, **data): forwarded.append(data); return 'actions'
        with patch.dict('sys.modules', {'torch': torch}):
            model = SeededModel(Model())
            self.assertEqual(model(_coppertop_seed=7, state='original'), 'actions')
            with self.assertRaises(ValueError): model(_coppertop_seed=True)
        self.assertEqual(seeds, [7, 7])
        self.assertEqual(forwarded, [{'state': 'original'}])
        self.assertEqual(entered, ['enter', 'exit'])

    def test_offline_plan_has_finite_workload_and_all_new_code_hashes(self):
        p = plan()
        self.assertEqual(p['limits']['queries'], 5)
        self.assertEqual(p['limits']['automatic_retries'], 0)
        self.assertFalse(p['allocates_cloud_resources'])
        self.assertFalse(p['live_verified'])


if __name__ == '__main__': unittest.main()
