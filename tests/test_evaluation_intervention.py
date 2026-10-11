"""Actual migrated gate/runner with CPU doubles; no model or simulator claims."""
import asyncio
from types import SimpleNamespace
import unittest
from unittest.mock import patch

import numpy as np

from coppertop.decision_gate import GatePolicy, validate_answers
from coppertop.decision_runner import candidate_admission, gated_rollout
from coppertop.mechanism import native_actions


def answers(replan=.9, continue_probability=.1, uncertain=0):
    timing = {'replan': replan, 'continue': continue_probability, 'uncertain': uncertain}
    return {'timing': {'type': 'choice', 'choice': max(timing, key=timing.get),
                      'probabilities': timing, 'confidence': max(timing.values())},
            'data_usefulness': {'type': 'choice', 'choice': 'uncertain', 'confidence': 1,
                               'probabilities': {'useful': 0, 'not_useful': 0, 'uncertain': 1}}}


class Clock:
    def __init__(self):
        self.now = 1_000_000_000

    def monotonic_ns(self):
        return self.now

    def advance(self, milliseconds):
        self.now += milliseconds * 1_000_000

    def sleep(self, seconds):
        self.now += int(seconds * 1e9)


class InlineThread:
    """Complete owned work before controls; suitable only for paused-mode tests."""
    def __init__(self, *, target, **kwargs):
        self.target = target

    def start(self):
        self.target()

    def is_alive(self):
        return False

    def join(self, timeout):
        pass


class CleanupThread(InlineThread):
    """Completed inline work with a synthetic owned lifecycle pending join."""
    def __init__(self, **kwargs):
        super().__init__(**kwargs)
        self.alive = True

    def is_alive(self):
        return self.alive

    def join(self, timeout):
        self.alive = False


class Environment:
    def __init__(self, clock, stop_after=None, step_ms=1):
        self.clock, self.actions, self.stop_after = clock, [], stop_after
        self.step_ms = step_ms

    def step(self, action):
        self.actions.append(action)
        self.clock.advance(self.step_ms)
        return SimpleNamespace(stopped=len(self.actions) == self.stop_after)


class InterventionTests(unittest.TestCase):
    def decide(self, value, *, policy=None, **kwargs):
        return (policy or GatePolicy(probability_decimals=4)).decide(value, observed_frame=8,
            **dict({'current_frame': 8, 'queued_until': 16, 'remaining_queries': 1}, **kwargs))

    def run_paused(self, value=None, *, enabled=True, query_error=None, score_error=None, score_ms=895,
                   actor_ms=399, stop_after=None, remaining_queries=1, timeout_seconds=5,
                   step_ms=1):
        clock = Clock()
        env = Environment(clock, stop_after, step_ms)
        old = np.arange(16*12, dtype=np.float32).reshape(16, 12)
        candidate = np.full((16, 12), -1, dtype=np.float32)
        calls = []

        def scorer():
            calls.append(('score', len(env.actions)))
            clock.advance(score_ms)
            if score_error:
                raise score_error
            return value if value is not None else answers(.8, .1, .0999)

        def query(inputs, seed):
            calls.append(('actor', inputs, seed, len(env.actions)))
            clock.advance(actor_ms)
            if query_error:
                raise query_error
            return candidate

        with patch('coppertop.decision_runner.time', clock), \
             patch('coppertop.decision_runner.threading.Thread', InlineThread):
            result = gated_rollout(env, scorer, query, {'causal': 'prefix'}, old,
                observed_frame=8, boundary=8, seed=20261006, steps=4,
                remaining_queries=remaining_queries, policy=GatePolicy(probability_decimals=4),
                clock_mode='paused_synchronous', enabled=enabled, timeout_seconds=timeout_seconds)
        return old, candidate, env, calls, result

    def test_four_decimal_benchmark_and_gate_agree_without_normalizing(self):
        for value in (answers(.8, .1, .0999), answers(.8, .1, .1001)):
            checked = validate_answers(value, probability_decimals=4)
            self.assertEqual(checked['timing']['probabilities'], value['timing']['probabilities'])
            self.assertTrue(self.decide(value)['request_early_replan'])
            with self.assertRaises(ValueError):
                self.decide(value, policy=GatePolicy())

    def test_grid_bad_mass_nonfinite_range_and_wrong_choice_remain_rejected(self):
        for mutation in (
            lambda x: x['timing']['probabilities'].update(replan=.80001),
            lambda x: x['timing']['probabilities'].update(replan=.81),
            lambda x: x['timing']['probabilities'].update(replan=float('nan')),
            lambda x: x['timing']['probabilities'].update(replan=float('inf')),
            lambda x: x['timing']['probabilities'].update(replan=True),
            lambda x: x['timing']['probabilities'].update(replan=-.1),
            lambda x: x['timing'].update(choice='continue'),
        ):
            value = answers(.8, .1, .0999)
            mutation(value)
            with self.assertRaises(ValueError):
                self.decide(value)

    def test_threshold_margin_age_and_budget_boundaries(self):
        for replan, expected in ((.7999, False), (.8, True), (.8001, True)):
            self.assertEqual(self.decide(answers(replan, round(1-replan, 4)))['request_early_replan'], expected)
        # The default .8 threshold implies margin >=.6; use a valid lower
        # threshold to test .2 margin independently without invalid mass.
        for replan, expected in ((.5999, False), (.6, True), (.6001, True)):
            self.assertEqual(self.decide(answers(replan, .4),
                policy=GatePolicy(threshold=.5, probability_decimals=4))['request_early_replan'], expected)
        for age in (0, 1, 2, 3):
            self.assertEqual(self.decide(answers(), current_frame=8+age)['request_early_replan'], age <= 2)
        self.assertEqual(self.decide(answers(), remaining_queries=0)['reason'], 'query_budget_exhausted')

    def test_usefulness_alone_never_queries_actor_or_changes_actions(self):
        value = answers(.1, .9)
        value['data_usefulness'] = {'type': 'choice', 'choice': 'useful', 'confidence': 1,
                                   'probabilities': {'useful': 1, 'not_useful': 0, 'uncertain': 0}}
        old, _, env, calls, result = self.run_paused(value)
        self.assertEqual([row[0] for row in calls], ['score'])
        self.assertEqual(result['query_calls'], 0)
        self.assertEqual(env.actions, list(native_actions(old, chunk=True)[8:12]))

    def test_clef_off_preserves_supplied_stock_queue_without_extra_queries(self):
        old, _, env, calls, result = self.run_paused(enabled=False)
        self.assertEqual(calls, [])
        self.assertEqual(result['scorer_calls'], 0)
        self.assertEqual(result['query_calls'], 0)
        self.assertEqual(result['query_seed'], 20261006)
        self.assertEqual(env.actions, list(native_actions(old, chunk=True)[8:12]))
        self.assertEqual(result['pause_ns'], 0)
        self.assertFalse(result['attributable_action_change'])

    def test_clef_off_requires_no_scorer_transport_or_worker(self):
        clock = Clock()
        env = Environment(clock)
        old = np.arange(16*12, dtype=np.float32).reshape(16, 12)
        with patch('coppertop.decision_runner.time', clock), \
             patch('coppertop.decision_runner.threading.Thread', side_effect=AssertionError('worker forbidden')):
            result = gated_rollout(env, None, None, {}, old, observed_frame=8,
                boundary=8, seed=20261006, steps=4, remaining_queries=0, enabled=False)
        self.assertEqual(result['actions'], native_actions(old, chunk=True)[8:12])
        self.assertEqual((result['scorer_calls'], result['query_calls']), (0, 0))

    def test_actual_runner_applies_actor_actions_and_freezes_controls_during_waits(self):
        old, candidate, env, calls, result = self.run_paused()
        self.assertEqual(calls, [('score', 0), ('actor', {'causal': 'prefix'}, 20261006, 0)])
        self.assertEqual(env.actions, list(native_actions(candidate, chunk=True)[:4]))
        self.assertNotEqual(env.actions, list(native_actions(old, chunk=True)[8:12]))
        self.assertEqual(result['committed_steps'], 0)
        self.assertEqual(result['candidate_admission']['age_controls'], 0)
        self.assertEqual(result['gate']['current_frame'], 8)
        self.assertEqual(result['pause_ns'], 1_294_000_000)
        self.assertEqual(result['gate_latency_ns'], 895_000_000)
        self.assertEqual(result['actor_latency_ns'], 399_000_000)
        self.assertEqual(result['wall_ns'], 1_298_000_000)
        self.assertEqual((result['scorer_calls'], result['query_calls']), (1, 1))
        self.assertEqual(result['changed_controls'], 4)
        self.assertTrue(result['attributable_action_change'])
        self.assertEqual(result['action_sources'], [
            {'control': 8+i, 'source': 'candidate', 'chunk_offset': i} for i in range(4)])

    def test_invalid_or_failed_or_budget_exhausted_keeps_old_queue(self):
        bad = answers(.8, .1, .0999)
        bad['timing']['probabilities']['replan'] = float('nan')
        for kwargs, calls, error in (({'value': bad}, 0, 'ValueError'),
                                     ({'query_error': TimeoutError()}, 1, 'TimeoutError'),
                                     ({'remaining_queries': 0}, 0, None)):
            old, _, env, _, result = self.run_paused(**kwargs)
            self.assertEqual(result['query_calls'], calls)
            self.assertEqual(result['error_type'], error)
            self.assertIsNone(result['committed_steps'])
            self.assertEqual(env.actions, list(native_actions(old, chunk=True)[8:12]))

    def test_paused_deadline_is_infrastructure_failure_without_controls(self):
        with self.assertRaises(TimeoutError):
            self.run_paused(timeout_seconds=1)

    def test_canceled_worker_score_or_actor_is_recorded_and_keeps_old_queue(self):
        for phase in ('score', 'actor'):
            with self.subTest(phase=phase):
                key = 'score_error' if phase == 'score' else 'query_error'
                old, _, env, calls, result = self.run_paused(**{key: asyncio.CancelledError(phase + ' canceled')})
                self.assertEqual(result['error_type'], 'CancelledError')
                self.assertEqual(result['scorer_calls'], 1)
                self.assertEqual(result['query_calls'], 0 if phase == 'score' else 1)
                self.assertEqual(len(calls), 1 if phase == 'score' else 2)
                self.assertEqual(env.actions, list(native_actions(old, chunk=True)[8:12]))
                self.assertIsNone(result['committed_steps'])
                self.assertFalse(result['attributable_action_change'])

    def test_paused_functional_steps_need_no_fifty_ms_wall_pacing_but_keep_deadline(self):
        _, candidate, env, _, result = self.run_paused(step_ms=80)
        self.assertEqual(env.actions, list(native_actions(candidate, chunk=True)[:4]))
        self.assertEqual(result['wall_ns'], 1_614_000_000)
        self.assertEqual(result['candidate_admission']['age_controls'], 0)
        with self.assertRaisesRegex(TimeoutError, 'Control/audit branch deadline'):
            self.run_paused(step_ms=2000)
        clock = Clock()
        env = Environment(clock, step_ms=80)
        old = np.zeros((16, 12), dtype=np.float32)
        with patch('coppertop.decision_runner.time', clock):
            with self.assertRaisesRegex(RuntimeError, 'Control/audit exceeded branch pacing'):
                gated_rollout(env, None, None, {}, old, observed_frame=8, boundary=8,
                    seed=20261006, steps=4, remaining_queries=0, enabled=False)

    def test_episode_stop_prevents_extra_controls(self):
        _, _, env, _, result = self.run_paused(stop_after=1)
        self.assertEqual(len(env.actions), 1)
        self.assertEqual(len(result['frames']), 1)

    def test_cleanup_failures_preserve_active_control_exception_identity(self):
        for primary, secondary in ((TimeoutError('native control timeout'), RuntimeError),
                (ValueError('invalid control'), RuntimeError),
                (TimeoutError('native control timeout'), asyncio.CancelledError)):
            with self.subTest(primary=type(primary).__name__, secondary=secondary.__name__):
                clock = Clock()
                env = Environment(clock)
                cancellations = []
                def scorer():
                    return answers()
                def query(inputs, seed):
                    return np.ones((16, 12), dtype=np.float32)
                def cancel(name):
                    cancellations.append(name)
                    raise secondary(name + ' cancellation failed')
                scorer.cancel = lambda: cancel('scorer')
                query.cancel = lambda: cancel('actor')
                with patch('coppertop.decision_runner.time', clock), \
                     patch('coppertop.decision_runner.threading.Thread', CleanupThread), \
                     patch.object(env, 'step', side_effect=primary):
                    with self.assertRaises(type(primary)) as caught:
                        gated_rollout(env, scorer, query, {}, np.zeros((16, 12)),
                            observed_frame=8, boundary=8, seed=20261006, steps=4,
                            remaining_queries=1, clock_mode='paused_synchronous')
                self.assertIs(caught.exception, primary)
                self.assertEqual(cancellations, ['scorer', 'actor'])
                self.assertEqual([row['operation'] for row in primary.runner_cleanup_errors],
                                 ['cancel_scorer', 'cancel_actor'])
                self.assertTrue(any('scorer cancellation failed' in note for note in primary.__notes__))

    def test_runner_deadline_and_pacing_errors_survive_cancellation_failure(self):
        for mode, step_ms, error_type, message in (
                ('paused_synchronous', 2000, TimeoutError, 'Control/audit branch deadline'),
                ('continuous_async', 80, RuntimeError, 'Control/audit exceeded branch pacing')):
            with self.subTest(mode=mode):
                clock = Clock()
                env = Environment(clock, step_ms=step_ms)
                def scorer():
                    return answers()
                def query(inputs, seed):
                    return np.ones((16, 12), dtype=np.float32)
                def cancel():
                    raise ValueError('secondary cancel error')
                scorer.cancel = query.cancel = cancel
                with patch('coppertop.decision_runner.time', clock), \
                     patch('coppertop.decision_runner.threading.Thread', CleanupThread):
                    with self.assertRaisesRegex(error_type, message) as caught:
                        gated_rollout(env, scorer, query, {}, np.zeros((16, 12)),
                            observed_frame=8, boundary=8, seed=20261006, steps=4,
                            remaining_queries=1, clock_mode=mode, timeout_seconds=1)
                self.assertEqual(len(env.actions), 1)
                self.assertEqual(len(caught.exception.runner_cleanup_errors), 2)

    def test_cancellation_failure_after_valid_controls_still_fails(self):
        clock = Clock()
        env = Environment(clock)
        def scorer():
            return answers()
        def query(inputs, seed):
            return np.ones((16, 12), dtype=np.float32)
        def cancel():
            raise ValueError('secondary cancel error')
        scorer.cancel = query.cancel = cancel
        with patch('coppertop.decision_runner.time', clock), \
             patch('coppertop.decision_runner.threading.Thread', CleanupThread):
            with self.assertRaisesRegex(RuntimeError, 'Owned scorer/actor cancellation failed') as caught:
                gated_rollout(env, scorer, query, {}, np.zeros((16, 12)),
                    observed_frame=8, boundary=8, seed=20261006, steps=4,
                    remaining_queries=1, clock_mode='paused_synchronous')
        self.assertEqual(len(env.actions), 4)
        self.assertIsInstance(caught.exception.__cause__, ValueError)
        self.assertEqual(len(caught.exception.runner_cleanup_errors), 2)

    def test_worker_join_failure_does_not_mask_active_error(self):
        primary = ValueError('invalid control')
        class BrokenJoinThread(InlineThread):
            def join(self, timeout):
                raise RuntimeError('join failed')
        clock = Clock()
        env = Environment(clock)
        with patch('coppertop.decision_runner.time', clock), \
             patch('coppertop.decision_runner.threading.Thread', BrokenJoinThread), \
             patch.object(env, 'step', side_effect=primary):
            with self.assertRaises(ValueError) as caught:
                gated_rollout(env, lambda: answers(), lambda inputs, seed: np.ones((16, 12)),
                    {}, np.zeros((16, 12)), observed_frame=8, boundary=8, seed=20261006,
                    steps=4, remaining_queries=1, clock_mode='paused_synchronous')
        self.assertIs(caught.exception, primary)
        self.assertEqual(primary.runner_cleanup_errors[0]['operation'], 'worker_join')

    def test_actual_control_index_not_wall_time_defines_candidate_freshness(self):
        for elapsed in (0, 1, 2, 3, 15, 16, 19, 23):
            result = candidate_admission(observed_frame=8, current_frame=8+elapsed)
            self.assertEqual(result['apply'], elapsed <= 2)
            self.assertEqual(result['discarded_offsets'], min(elapsed, 16))
            if elapsed >= 16:
                self.assertEqual(result['reason'], 'exhausted_candidate_suffix')
            elif elapsed > 2:
                self.assertEqual(result['reason'], 'stale_candidate')
        for kwargs in ({'current_frame': 7}, {'current_frame': True}, {'candidate_controls': 32}):
            with self.assertRaises(ValueError):
                candidate_admission(**dict({'observed_frame': 8, 'current_frame': 8}, **kwargs))

    def test_candidate_identical_to_old_actions_is_not_intervention_evidence(self):
        old, _, _, _, _ = self.run_paused()
        identical = np.vstack((old[8:], old[:8]))
        with patch('coppertop.decision_runner.native_actions', side_effect=(
                native_actions(old, chunk=True), native_actions(identical, chunk=True))):
            _, _, env, _, result = self.run_paused()
        self.assertEqual(result['committed_steps'], 0)
        self.assertEqual(env.actions, list(native_actions(old, chunk=True)[8:12]))
        # Admission/commit alone is insufficient; acceptance compares executed actions.
        self.assertFalse(any(a != b for a, b in zip(env.actions, native_actions(old, chunk=True)[8:12])))
        self.assertFalse(result['attributable_action_change'])

    def test_cpu_gate_rejects_unauthenticated_reference_before_model_import(self):
        from pathlib import Path
        import tempfile
        from coppertop.colocation_stage import cpu_gate
        with tempfile.TemporaryDirectory() as folder:
            Path(folder, 'joint_schema_model.py').write_bytes(b'x'*23263)
            with patch('coppertop.colocation_stage.CLEF_MODEL', Path(folder)):
                with self.assertRaisesRegex(ValueError, 'reference'):
                    cpu_gate()


if __name__ == '__main__':
    unittest.main()
