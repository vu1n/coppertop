"""Toy latent-state tests are not official RoboCasa execution."""
from dataclasses import replace
import json
from pathlib import Path
import tempfile
import unittest

import numpy as np

from coppertop.chords import LocalStore, canonical, digest
from coppertop.mechanism import (COMPONENTS, Frame, Tape, candidate_diagnostics,
                                delayed_branches, native_actions, replay)


class LatentEnv:
    def __init__(self):
        self.closed = False

    def reset(self, seed):
        self.rng = np.random.default_rng(seed)
        self.index = 0
        self.physics = 0.
        self.controller = 0.
        self.task = False
        self.history = []
        return self.frame()

    def frame(self):
        values = {'physics': self.physics, 'model': {'light': self.task},
                  'task': self.task, 'controller': self.controller,
                  'rng': self.rng.bit_generator.state,
                  'observables': self.index % 2, 'history': self.history,
                  'observation': self.physics}
        return Frame(self.index, self.index * 50_000_000,
                     tuple(sorted((k, digest(canonical({'value': v}))) for k, v in values.items())))

    def step(self, action):
        self.controller += action[0]
        self.physics += self.controller + float(self.rng.uniform())
        self.task = self.physics > 2
        self.history = (self.history + [self.physics])[-7:]
        self.index += 1
        return self.frame()

    def close(self):
        self.closed = True


def tape():
    env = LatentEnv()
    frames = [env.reset(1150)]
    actions = tuple(native_actions([.25] + [0.] * 11) for _ in range(10))
    frames.extend(env.step(a) for a in actions)
    return Tape('episode-a', 1150, actions, tuple(frames))


class MechanismTests(unittest.TestCase):
    def test_component_map_order_does_not_change_parity(self):
        original = tape()
        reverse = replace(original, frames=tuple(replace(f, components=f.components[::-1]) for f in original.frames))
        self.assertEqual(original, reverse)
        self.assertEqual(replay(LatentEnv, reverse, prefix_steps=4, continuation_steps=2)['status'], 'bounded_parity')

    def test_fresh_prefix_and_suffix_parity(self):
        env = LatentEnv()
        result = replay(lambda: env, tape(), prefix_steps=4, continuation_steps=6)
        self.assertEqual(result['status'], 'bounded_parity')
        self.assertTrue(env.closed)

    def test_physics_only_restore_does_not_restore_continuation(self):
        source = LatentEnv()
        source.reset(1150)
        action = native_actions([.25] + [0.] * 11)
        for _ in range(4): source.step(action)
        target = LatentEnv()
        target.reset(1150)
        target.physics, target.index = source.physics, source.index
        self.assertNotEqual(source.step(action), target.step(action))

    def test_each_missing_or_changed_component_fails(self):
        original = tape()
        for component in COMPONENTS:
            with self.subTest(component=component):
                frame = original.frames[4]
                parts = dict(frame.components)
                parts[component] = '0' * 64
                changed = replace(frame, components=tuple(sorted(parts.items())))
                bad = replace(original, frames=original.frames[:4] + (changed,) + original.frames[5:])
                env = LatentEnv()
                with self.assertRaisesRegex(ValueError, 'control 4'):
                    replay(lambda: env, bad, prefix_steps=4, continuation_steps=2)
                self.assertTrue(env.closed)
                with self.assertRaises(ValueError):
                    replace(frame, components=tuple(p for p in frame.components if p[0] != component))

    def test_reset_mismatch_and_invalid_continuation(self):
        original = tape()
        with self.assertRaisesRegex(ValueError, 'Reset parity'):
            replay(LatentEnv, replace(original, seed=1151), prefix_steps=4, continuation_steps=2)
        for prefix, suffix in ((4, 0), (9, 2), (-1, 1), (True, 1)):
            with self.assertRaises(ValueError):
                replay(LatentEnv, original, prefix_steps=prefix, continuation_steps=suffix)

    def test_tape_identity_immutability_and_boundary_flags(self):
        original = tape()
        with self.assertRaises(ValueError): replace(original, episode_id=7)
        with self.assertRaises(ValueError): replace(original, frames=(None,) + original.frames[1:])
        with self.assertRaises(ValueError): replace(original, actions=list(original.actions))
        with self.assertRaises(ValueError):
            replace(original, actions=(tuple(np.zeros(12, dtype=np.float32)),) + original.actions[1:])
        with self.assertRaises(ValueError): replace(original, frames=original.frames[1:])
        for flag in ('success', 'terminated', 'truncated'):
            stopped = replace(original.frames[4], **{flag: True})
            with self.assertRaises(ValueError):
                replace(original, frames=original.frames[:4] + (stopped,) + original.frames[5:])
        end = replace(original.frames[-1], terminated=True)
        final = replace(original, frames=original.frames[:-1] + (end,))
        self.assertFalse(final.frames[-1].success)

    def test_immutable_sidecar_reuses_existing_store(self):
        with tempfile.TemporaryDirectory() as root:
            store = LocalStore(Path(root))
            original = tape()
            key = original.publish(store, {'source_revision': 'pinned', 'coverage': 'toy'})
            self.assertEqual(key, original.publish(store, {'source_revision': 'pinned', 'coverage': 'toy'}))
            data = json.loads(store.get(key))
            self.assertEqual(len({f['frame_id'] for f in data['frames']}), 11)
            self.assertEqual(data['action_order'][-1], 'mode')

    def test_latency_commits_same_controls_then_discards_stale_candidate(self):
        old = np.arange(192, dtype=np.float32).reshape(16, 12)
        new = old + 1000
        for boundary in (4, 8, 12):
            branches = delayed_branches(old, new, boundary=boundary,
                latency_ns=50_000_001, control_ns=50_000_000, steps=4)
            self.assertEqual(branches['committed_steps'], 2)
            self.assertEqual(branches['continue'][:2], branches['replan'][:2])
            self.assertEqual(branches['replan'][2:], native_actions(new, chunk=True)[2:4])
            self.assertEqual(len(branches['continue']), len(branches['replan']))
        zero = delayed_branches(old, new, boundary=4, latency_ns=0, control_ns=1, steps=4)
        self.assertEqual(zero['replan'], native_actions(new, chunk=True)[:4])

    def test_delay_exhaustion_and_midstep_interrupt_rejected(self):
        chunk = np.zeros((16, 12), dtype=np.float32)
        for kwargs in ({'boundary': 4.5}, {'boundary': 0}, {'latency_ns': 4},
                       {'boundary': 15}, {'control_ns': 0}, {'latency_ns': -1}):
            params = dict(boundary=4, latency_ns=1, control_ns=1, steps=4)
            params.update(kwargs)
            with self.assertRaises(ValueError): delayed_branches(chunk, chunk, **params)

    def test_native_semantics_finiteness_and_diversity_accounting(self):
        chunk = np.zeros((16, 12), dtype=np.float32)
        other = chunk.copy()
        other[0, 11] = 1
        report = candidate_diagnostics(chunk, chunk.copy(), other)
        self.assertTrue(report['same_seed_exact'])
        self.assertTrue(report['different_seed_distinct'])
        self.assertEqual(report['query_calls'], 3)
        self.assertEqual(report['discarded_calls'], 2)
        self.assertFalse(candidate_diagnostics(chunk, other, chunk)['same_seed_exact'])
        for bad in (np.zeros((16, 4)), np.full((16, 12), np.nan)):
            with self.assertRaises(ValueError): native_actions(bad, chunk=True)


if __name__ == '__main__':
    unittest.main()
