"""Deterministic CPU handoffs through the recovered runner and gate."""
import threading
from types import SimpleNamespace
import unittest
from unittest.mock import patch

import numpy as np

from coppertop.decision_gate import GatePolicy
from coppertop.decision_runner import gated_rollout
from coppertop.mechanism import native_actions


REAL_THREAD = threading.Thread


def wait_for(event):
    if not event.wait(2):
        raise AssertionError('Owned fixture handshake did not complete')


def rounded_answers():
    return {
        'timing': {'type': 'choice', 'choice': 'replan', 'confidence': .8,
                   'probabilities': {'replan': .8, 'continue': .1, 'uncertain': .0999}},
        'data_usefulness': {'type': 'choice', 'choice': 'uncertain', 'confidence': 1,
                            'probabilities': {'useful': 0, 'not_useful': 0, 'uncertain': 1}},
    }


class VirtualClock:
    def __init__(self):
        self.now = 1_000_000_000
        self.lock = threading.Lock()
        self.after_sleep = lambda: None

    def monotonic_ns(self):
        with self.lock:
            return self.now

    def advance(self, nanoseconds):
        with self.lock:
            self.now += nanoseconds

    def sleep(self, seconds):
        self.advance(round(seconds * 1e9))
        self.after_sleep()


class Environment:
    def __init__(self, clock):
        self.clock = clock
        self.actions = []
        self.before_return = lambda: None

    def step(self, action):
        self.actions.append(action)
        self.clock.advance(1_000_000)
        self.before_return()
        return SimpleNamespace(stopped=False)


class AsyncFixture:
    """Owned real worker plus event barriers; only virtual time advances."""
    def __init__(self, *, blocked_score=False):
        self.clock = VirtualClock()
        self.env = Environment(self.clock)
        self.old = np.arange(16 * 12, dtype=np.float32).reshape(16, 12)
        self.candidate = np.full((16, 12), -1, dtype=np.float32)
        self.score_started = threading.Event()
        self.score_release = threading.Event()
        self.actor_started = threading.Event()
        self.actor_release = threading.Event()
        self.worker_done = threading.Event()
        self.blocked_score = blocked_score
        self.calls = []
        self.worker = None

    def scorer(self):
        self.calls.append(('score', len(self.env.actions)))
        self.score_started.set()
        if self.blocked_score:
            wait_for(self.score_release)
        return rounded_answers()

    def query(self, inputs, seed):
        self.calls.append(('actor', inputs, seed))
        self.actor_started.set()
        wait_for(self.actor_release)
        return self.candidate

    def cancel(self):
        self.score_release.set()
        self.actor_release.set()

    def thread_factory(self, *, target, **kwargs):
        fixture = self

        def owned_work():
            try:
                target()
            finally:
                fixture.worker_done.set()

        class OwnedThread:
            def __init__(self):
                self.thread = REAL_THREAD(target=owned_work, **kwargs)

            def start(self):
                self.thread.start()
                wait_for(fixture.score_started if fixture.blocked_score else fixture.actor_started)

            def is_alive(self):
                return self.thread.is_alive()

            def join(self, timeout):
                self.thread.join(timeout)

        self.worker = OwnedThread()
        return self.worker

    def finish_actor(self):
        self.actor_release.set()
        wait_for(self.worker_done)

    def run(self, *, max_age_controls=2):
        scorer = SimpleNamespace(__call__=self.scorer, cancel=self.cancel)
        query = SimpleNamespace(__call__=self.query, cancel=self.cancel)

        class Call:
            def __init__(self, owned):
                self.owned = owned

            def __call__(self, *args):
                return self.owned.__call__(*args)

            def cancel(self):
                self.owned.cancel()

        with patch('coppertop.decision_runner.time', self.clock), \
             patch('coppertop.decision_runner.threading.Thread', self.thread_factory):
            result = gated_rollout(self.env, Call(scorer), Call(query), {'causal': 'prefix'}, self.old,
                observed_frame=8, boundary=8, seed=20261006, steps=6, remaining_queries=1,
                policy=GatePolicy(max_age_controls=max_age_controls, probability_decimals=4))
        self.worker.join(2)
        if self.worker.is_alive():
            raise AssertionError('Owned fixture worker survived rollout')
        return result


class AsyncRunnerTests(unittest.TestCase):
    def run_arrival(self, completed_controls):
        fixture = AsyncFixture()

        def release_at_boundary():
            if len(fixture.env.actions) == completed_controls:
                fixture.finish_actor()

        fixture.clock.after_sleep = release_at_boundary
        return fixture, fixture.run()

    def test_admitted_actor_arriving_at_age_three_keeps_old_nonempty_queue(self):
        fixture, result = self.run_arrival(3)
        self.assertTrue(result['gate']['request_early_replan'])
        self.assertEqual(result['gate']['current_frame'], 8)
        self.assertEqual((result['scorer_calls'], result['query_calls']), (1, 1))
        self.assertEqual(result['candidate_admission'], {
            'reason': 'stale_candidate', 'apply': False, 'age_controls': 3, 'discarded_offsets': 3})
        self.assertLess(result['candidate_admission']['discarded_offsets'], len(fixture.candidate))
        self.assertIsNone(result['committed_steps'])
        self.assertTrue(result['late_candidate_discarded'])
        self.assertIsNone(result['error_type'])
        self.assertEqual(result['changed_controls'], 0)
        self.assertFalse(result['attributable_action_change'])
        self.assertEqual([row['source'] for row in result['action_sources']], ['old_queue'] * 6)
        expected = native_actions(fixture.old, chunk=True)[8:14]
        self.assertEqual(result['actions'], expected)
        self.assertEqual(fixture.env.actions, list(expected))

    def test_admitted_actor_arriving_at_age_two_applies_only_unexpired_offsets(self):
        fixture, result = self.run_arrival(2)
        self.assertEqual(result['candidate_admission']['reason'], 'candidate_ready')
        self.assertEqual(result['candidate_admission']['age_controls'], 2)
        self.assertEqual(result['committed_steps'], 2)
        self.assertIsNone(result['error_type'])
        self.assertEqual(result['changed_controls'], 4)
        self.assertTrue(result['attributable_action_change'])
        self.assertEqual([row['chunk_offset'] for row in result['action_sources']], [8, 9, 2, 3, 4, 5])
        expected = (native_actions(fixture.old, chunk=True)[8:10]
                    + native_actions(fixture.candidate, chunk=True)[2:6])
        self.assertEqual(result['actions'], expected)
        self.assertEqual(fixture.env.actions, list(expected))

    def test_score_during_in_progress_step_uses_completed_index_not_wall_ceiling(self):
        fixture = AsyncFixture(blocked_score=True)

        def finish_score_before_first_control_completes():
            if len(fixture.env.actions) == 1:
                fixture.score_release.set()
                wait_for(fixture.actor_started)
                fixture.finish_actor()

        fixture.env.before_return = finish_score_before_first_control_completes
        result = fixture.run(max_age_controls=0)
        self.assertEqual(result['gate_latency_ns'], 1_000_000)
        self.assertEqual(result['gate']['current_frame'], result['gate']['observed_frame'])
        self.assertTrue(result['gate']['request_early_replan'])
        self.assertEqual(result['query_calls'], 1)
        self.assertEqual(result['candidate_admission']['reason'], 'stale_candidate')
        self.assertEqual(result['candidate_admission']['age_controls'], 1)
        self.assertIsNone(result['error_type'])
        self.assertEqual(result['actions'], native_actions(fixture.old, chunk=True)[8:14])


if __name__ == '__main__':
    unittest.main()
