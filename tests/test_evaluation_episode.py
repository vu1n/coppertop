"""Full-horizon injected CPU checks; no model, simulator, provider or real sleeps."""
import asyncio
from dataclasses import FrozenInstanceError, replace
import json
import unittest
from unittest.mock import patch

from coppertop import evaluation_episode as episode
from coppertop.mechanism import Frame, Tape


class EpisodeFixtureTests(unittest.TestCase):
    def setUp(self):
        self.bounds = episode.EpisodeBounds(120, .2, .2, .2, .2, .2, .5, 3)
        self.clock = episode._FixtureClock()
        self.env = episode._FixtureEnvironment(self.clock)
        self.actor = episode._FixtureActor(self.clock)
        self.scorer = episode._FixtureScorer(self.clock)

    def run_episode(self, *, enabled=True, bounds=None, budget=1):
        with patch('coppertop.decision_runner.time', self.clock):
            return episode.run_episode(self.env, self.actor, self.scorer if enabled else None,
                episode_id='test/clef' if enabled else 'test/stock', seed=1152,
                clef_enabled=enabled, bounds=bounds or self.bounds, clock=self.clock,
                extra_actor_budget=budget)

    def assert_closed(self, enabled=True):
        self.assertTrue(self.env.closed)
        self.assertTrue(self.actor.closed)
        if enabled:
            self.assertTrue(self.scorer.closed)

    def test_four_full_episodes_match_resets_stock_parity_and_native_changes(self):
        result = episode.run_fixture_pilot()
        self.assertEqual(result['accounting']['status'], 'fixture_passed')
        self.assertEqual(result['accounting']['episode_order'], [
            'provisional-dev1152/xiaomi_only', 'provisional-dev1152/xiaomi_clef',
            'provisional-dev1153/xiaomi_clef', 'provisional-dev1153/xiaomi_only'])
        self.assertEqual(result['accounting']['controls'], 2400)
        self.assertTrue(result['accounting']['matching_reset_component_bytes'])
        self.assertTrue(result['accounting']['stock_fixture_parity'])
        self.assertTrue(result['accounting']['attributable_fixture_changes'])
        self.assertFalse(result['accounting']['unused_seed_verified'])
        self.assertEqual(result['accounting']['totals']['actor'], 152)
        self.assertEqual(result['accounting']['totals']['actor_extra'], 2)
        self.assertEqual(result['accounting']['totals']['scorer'], 2)
        for item, tape in zip(result['episodes'], result['tapes']):
            self.assertIs(item['tape'], tape)
            self.assertIsInstance(tape, Tape)
            self.assertEqual((len(tape.actions), len(tape.frames)), (600, 601))
            self.assertTrue(all(len(action) == 12 for action in tape.actions))
            self.assertFalse(item['accounting']['success'])
            self.assertTrue(item['accounting']['horizon_reached'])
            self.assertTrue(item['accounting']['infrastructure_valid'])
            self.assertEqual(item['accounting']['counts']['cleanup']['completed'],
                             3 if item['accounting']['arm'] == 'xiaomi_clef' else 2)
            self.assertTrue(all(row['seed'] == episode.ACTOR_SEED
                                for row in item['accounting']['journal'] if row['kind'] == 'actor'))
            with self.assertRaises(FrozenInstanceError):
                tape.seed = 1
            json.dumps(item['accounting'], allow_nan=False)
            json.dumps(item['provenance'], allow_nan=False)
        json.dumps(result['accounting'], allow_nan=False)
        json.dumps(result['provenance'], allow_nan=False)

    def test_candidate_remainder_runs_to_control23_then_normal_query(self):
        result = self.run_episode()
        accounting, tape = result['accounting'], result['tape']
        actor_rows = [row for row in accounting['journal'] if row['kind'] == 'actor']
        self.assertEqual([(row['phase'], row['completed_control']) for row in actor_rows[:3]],
                         [('initial', 0), ('extra', 8), ('normal', 24)])
        self.assertEqual(tape.actions[8:24], episode._fixture_chunk(1152, 8))
        self.assertEqual([row['chunk_offset'] for row in accounting['action_sources'][16:24]], list(range(8, 16)))
        self.assertEqual(accounting['counts']['actor_normal']['submitted'], 36)
        self.assertEqual(accounting['counts']['actor']['submitted'], 38)
        self.assertEqual(accounting['counts']['input']['submitted'], 38)
        self.assertEqual(accounting['changed_controls_at_handoff'], 8)
        self.assert_closed()

    def test_zero_extra_budget_scores_once_but_keeps_stock_schedule(self):
        result = self.run_episode(budget=0)
        accounting = result['accounting']
        self.assertEqual(accounting['opportunity'], 'query_budget_exhausted')
        self.assertEqual(accounting['counts']['actor_extra']['submitted'], 0)
        self.assertEqual(accounting['counts']['actor']['submitted'], 38)
        self.assertEqual(accounting['counts']['scorer']['submitted'], 1)
        self.assertFalse(accounting['attributable_action_change'])
        self.assertTrue(accounting['infrastructure_valid'])
        self.assert_closed()

    def test_clef_off_has_no_score_extra_or_worker_and_matches_stock(self):
        with patch('coppertop.decision_runner.threading.Thread', side_effect=AssertionError('No worker')):
            result = self.run_episode(enabled=False)
        expected = tuple(episode._fixture_chunk(1152, start)[offset]
                         for start in range(0, 600, 16) for offset in range(min(16, 600-start)))
        self.assertEqual(result['tape'].actions, expected)
        self.assertEqual(result['accounting']['counts']['scorer']['submitted'], 0)
        self.assertEqual(result['accounting']['counts']['actor_extra']['submitted'], 0)
        self.assertEqual(result['accounting']['counts']['input']['submitted'], 38)
        self.assert_closed(enabled=False)

    def test_terminated_at_control8_is_not_success_or_opportunity(self):
        self.env = episode._FixtureEnvironment(self.clock, stop_after=8)
        result = self.run_episode()
        accounting = result['accounting']
        self.assertEqual(accounting['controls'], 8)
        self.assertEqual(accounting['counts']['scorer']['submitted'], 0)
        self.assertTrue(accounting['terminated'])
        self.assertTrue(accounting['policy_stopped'])
        self.assertFalse(accounting['success'])
        self.assertFalse(accounting['horizon_reached'])
        self.assertTrue(accounting['infrastructure_valid'])
        self.assert_closed()

    def test_actual_success_and_truncation_flags_are_preserved(self):
        for kind in ('success', 'truncated'):
            with self.subTest(kind=kind):
                self.setUp()
                self.env = episode._FixtureEnvironment(self.clock, stop_after=14, stop_kind=kind)
                result = self.run_episode()
                self.assertEqual(len(result['tape'].actions), 14)
                self.assertEqual(result['accounting']['success'], kind == 'success')
                self.assertEqual(result['accounting']['truncated'], kind == 'truncated')
                self.assertTrue(result['accounting']['policy_stopped'])
                self.assertFalse(result['accounting']['horizon_reached'])
                self.assert_closed()

    def test_initial_actor_failure_counts_submission_without_retry(self):
        with patch.object(type(self.actor), '__call__', side_effect=ValueError('initial failed')):
            result = self.run_episode()
        self.assertEqual(len(result['tape'].actions), 0)
        self.assertEqual(result['accounting']['counts']['actor_initial']['failed'], 1)
        self.assertEqual(result['accounting']['counts']['actor']['submitted'], 1)
        self.assertFalse(result['accounting']['infrastructure_valid'])
        self.assert_closed()

    def test_normal_actor_failure_stops_at_expired_queue_without_replay(self):
        original = type(self.actor).__call__
        def fail_at_boundary(actor, inputs, seed):
            if int(inputs[0][0, 1]) == 16:
                raise ValueError('normal failed')
            return original(actor, inputs, seed)
        with patch.object(type(self.actor), '__call__', fail_at_boundary):
            result = self.run_episode(enabled=False)
        self.assertEqual(len(result['tape'].actions), 16)
        self.assertEqual(result['accounting']['counts']['actor_normal']['failed'], 1)
        self.assertEqual(result['accounting']['counts']['actor']['submitted'], 2)
        self.assert_closed(enabled=False)

    def test_extra_actor_failure_keeps_audited_old_queue_and_is_infrastructure_invalid(self):
        original = type(self.actor).__call__
        def fail_extra(actor, inputs, seed):
            if int(inputs[0][0, 1]) == 8:
                raise ValueError('extra failed')
            return original(actor, inputs, seed)
        with patch.object(type(self.actor), '__call__', fail_extra):
            result = self.run_episode()
        self.assertEqual(result['tape'].actions, episode._fixture_chunk(1152, 0))
        self.assertEqual(result['accounting']['counts']['actor_extra']['failed'], 1)
        self.assertEqual(result['accounting']['counts']['actor']['submitted'], 2)
        self.assertEqual(result['accounting']['counts']['scorer']['completed'], 1)
        self.assertFalse(result['accounting']['infrastructure_valid'])
        self.assert_closed()

    def test_scorer_failure_is_counted_and_does_not_query_extra(self):
        with patch.object(type(self.scorer), '__call__', side_effect=ValueError('score failed')):
            result = self.run_episode()
        self.assertEqual(len(result['tape'].actions), 16)
        self.assertEqual(result['accounting']['counts']['scorer']['failed'], 1)
        self.assertEqual(result['accounting']['counts']['actor_extra']['submitted'], 0)
        self.assertFalse(result['accounting']['infrastructure_valid'])
        self.assert_closed()

    def test_actor_timeout_and_cancel_failure_do_not_mask_primary_error(self):
        original = type(self.actor).__call__
        def late(actor, inputs, seed):
            self.clock.advance(201)
            return original(actor, inputs, seed)
        with patch.object(type(self.actor), '__call__', late), \
             patch.object(self.actor, 'cancel', side_effect=RuntimeError('cancel failed')):
            result = self.run_episode()
        counts = result['accounting']['counts']
        self.assertEqual(counts['actor']['timeout'], 1)
        self.assertEqual(counts['cancel']['failed'], 1)
        self.assertEqual([row['error_type'] for row in result['accounting']['errors'][:2]],
                         ['TimeoutError', 'RuntimeError'])
        self.assertEqual(len(result['tape'].actions), 0)
        self.assert_closed()

    def test_scorer_timeout_has_separate_status_and_no_extra_actor(self):
        original = type(self.scorer).__call__
        def late(scorer, inputs):
            self.clock.advance(201)
            return original(scorer, inputs)
        with patch.object(type(self.scorer), '__call__', late):
            result = self.run_episode()
        self.assertEqual(result['accounting']['counts']['scorer']['timeout'], 1)
        self.assertEqual(result['accounting']['counts']['actor_extra']['submitted'], 0)
        self.assertEqual(len(result['tape'].actions), 16)
        self.assert_closed()

    def test_reset_timeout_keeps_reset_frame_and_closes_every_owner(self):
        original = self.env.reset
        def late(seed):
            self.clock.advance(201)
            return original(seed)
        with patch.object(self.env, 'reset', late):
            result = self.run_episode()
        self.assertEqual(result['accounting']['counts']['reset']['timeout'], 1)
        self.assertEqual(len(result['tape'].frames), 1)
        self.assertEqual(result['accounting']['counts']['actor']['submitted'], 0)
        self.assert_closed()

    def test_input_timeout_is_counted_before_actor_submission(self):
        original = self.env.inputs
        def late():
            self.clock.advance(201)
            return original()
        with patch.object(self.env, 'inputs', late):
            result = self.run_episode()
        self.assertEqual(result['accounting']['counts']['input']['timeout'], 1)
        self.assertEqual(result['accounting']['counts']['actor']['submitted'], 0)
        self.assert_closed()

    def test_late_step_retains_actual_audited_control_in_invalid_partial_tape(self):
        result = self.run_episode(bounds=replace(self.bounds, step_seconds=.05))
        self.assertEqual(len(result['tape'].actions), 1)
        self.assertEqual(result['accounting']['counts']['step']['timeout'], 1)
        row = next(row for row in result['accounting']['journal'] if row['kind'] == 'step')
        self.assertTrue(row['retained_in_tape'])
        self.assertFalse(result['accounting']['infrastructure_valid'])
        self.assert_closed()

    def test_late_handoff_frames_retain_selected_source_offset_and_only_actual_changes(self):
        for mode, late_control, source, offset, changed in (
                ('candidate', 8, 'candidate', 0, 1),
                ('candidate', 9, 'candidate', 1, 2),
                ('old', 8, 'old_queue', 8, 0),
                ('identical', 8, 'candidate', 0, 0)):
            with self.subTest(mode=mode, late_control=late_control):
                self.setUp()
                original_step = self.env.step
                original_actor = type(self.actor).__call__
                def late(action):
                    if self.env.control == late_control:
                        self.clock.advance(121)
                    return original_step(action)
                def ask(actor, inputs, seed):
                    if mode == 'identical' and int(inputs[0][0, 1]) == 8:
                        self.clock.advance(3)
                        return episode._fixture_chunk(1152, 0)[8:] + episode._fixture_chunk(1152, 16)[:8]
                    return original_actor(actor, inputs, seed)
                with patch.object(self.env, 'step', late), patch.object(type(self.actor), '__call__', ask):
                    result = self.run_episode(budget=0 if mode == 'old' else 1)
                accounting = result['accounting']
                self.assertEqual(len(result['tape'].actions), late_control + 1)
                self.assertEqual(accounting['action_sources'][-1],
                                 {'control': late_control, 'source': source, 'chunk_offset': offset})
                self.assertEqual(accounting['changed_controls_at_handoff'], changed)
                self.assertEqual(accounting['attributable_action_change'], changed > 0)
                self.assertEqual(accounting['counts']['step']['timeout'], 1)
                self.assertFalse(accounting['infrastructure_valid'])
                row = next(row for row in accounting['journal']
                           if row['kind'] == 'step' and row['control'] == late_control)
                self.assertTrue(row['retained_in_tape'])
                self.assertEqual((row['source'], row['chunk_offset']), (source, offset))
                self.assertEqual(row['changed_action'], mode == 'candidate')
                self.assert_closed()

    def test_invalid_handoff_frame_does_not_retain_selected_candidate_or_change(self):
        original = self.env.step
        def invalid(action):
            frame = original(action)
            return Frame(99, frame.time_ns, frame.components) if frame.step == 9 else frame
        with patch.object(self.env, 'step', invalid):
            result = self.run_episode()
        accounting = result['accounting']
        self.assertEqual(len(result['tape'].actions), 8)
        self.assertEqual(len(accounting['action_sources']), 8)
        self.assertEqual(accounting['changed_controls_at_handoff'], 0)
        row = next(row for row in accounting['journal'] if row['kind'] == 'step' and row['control'] == 8)
        self.assertEqual(row['source'], 'candidate')
        self.assertTrue(row['changed_action'])  # Selected change was never validly retained.
        self.assertFalse(row['retained_in_tape'])
        self.assertEqual(accounting['counts']['step']['failed'], 1)
        self.assert_closed()

    def test_runner_branch_deadline_keeps_completed_late_frame_attribution(self):
        # Score+actor consume8ms, then a valid80ms step crosses this branch's
        # 80ms deadline while remaining within the separate200ms step bound.
        result = self.run_episode(bounds=replace(self.bounds, handoff_seconds=.08))
        accounting = result['accounting']
        self.assertEqual(len(result['tape'].actions), 9)
        self.assertEqual(accounting['action_sources'][-1],
                         {'control': 8, 'source': 'candidate', 'chunk_offset': 0})
        self.assertEqual(accounting['changed_controls_at_handoff'], 1)
        self.assertEqual(accounting['counts']['step']['completed'], 9)
        self.assertEqual(accounting['counts']['step']['timeout'], 0)
        self.assertEqual(accounting['errors'][0]['error_type'], 'TimeoutError')
        self.assertIn('Control/audit branch deadline', accounting['errors'][0]['message'])
        self.assertFalse(accounting['infrastructure_valid'])
        self.assert_closed()

    def test_handoff_timeout_keeps_primary_journal_and_secondary_worker_cleanup_errors(self):
        class HeldThread:
            """Inline scorer/actor with a synthetic unclosed worker lifecycle."""
            def __init__(self, *, target, **kwargs):
                self.target = target
            def start(self):
                self.target()
            def is_alive(self):
                return True
            def join(self, timeout):
                pass
        original = self.env.step
        def late(action):
            if self.env.control == 8:
                self.clock.advance(121)
            return original(action)
        with patch.object(self.env, 'step', late), \
             patch.object(self.scorer, 'cancel', side_effect=asyncio.CancelledError('score cancel failed')), \
             patch.object(self.actor, 'cancel', side_effect=RuntimeError('actor cancel failed')), \
             patch('coppertop.decision_runner.threading.Thread', HeldThread):
            result = self.run_episode()
        accounting = result['accounting']
        self.assertEqual(accounting['errors'][0]['error_type'], 'TimeoutError')
        self.assertEqual(accounting['errors'][0]['operation'], 'step')
        self.assertEqual(accounting['counts']['step']['timeout'], 1)
        self.assertEqual(accounting['counts']['cancel']['failed'], 2)
        self.assertTrue(any(row['operation'] == 'cancel' and row['error_type'] == 'CancelledError'
                            for row in accounting['errors']))
        secondary = [row for row in accounting['errors'] if row['operation'] == 'runner_cleanup']
        self.assertEqual([row['phase'] for row in secondary], ['cancel_scorer', 'cancel_actor', 'worker_alive'])
        self.assertEqual(secondary[-1]['message'], 'Owned worker still alive')
        self.assertEqual(accounting['action_sources'][-1]['source'], 'candidate')
        self.assertEqual(accounting['changed_controls_at_handoff'], 1)
        self.assertFalse(accounting['infrastructure_valid'])
        json.dumps(accounting, allow_nan=False)
        self.assert_closed()

    def test_episode_deadline_includes_controls_and_cleanup(self):
        result = self.run_episode(enabled=False, bounds=replace(self.bounds, episode_seconds=.5))
        self.assertLess(result['accounting']['controls'], 600)
        self.assertFalse(result['accounting']['infrastructure_valid'])
        self.assertTrue(any(row['error_type'] == 'TimeoutError' for row in result['accounting']['errors']))
        self.assert_closed(enabled=False)

    def test_cleanup_failure_attempts_all_closes(self):
        def bad_close():
            self.actor.closed = True
            raise ValueError('actor close failed')
        with patch.object(self.actor, 'close', bad_close):
            result = self.run_episode()
        self.assertEqual(result['accounting']['counts']['cleanup']['submitted'], 3)
        self.assertEqual(result['accounting']['counts']['cleanup']['failed'], 1)
        self.assertFalse(result['accounting']['infrastructure_valid'])
        self.assert_closed()

    def test_total_cleanup_budget_still_attempts_every_close(self):
        def slow_actor_close():
            self.actor.closed = True
            self.clock.advance(10)
        def slow_env_close():
            self.env.closed = True
            self.clock.advance(10)
        with patch.object(self.actor, 'close', slow_actor_close), patch.object(self.env, 'close', slow_env_close):
            result = self.run_episode(enabled=False, bounds=replace(self.bounds, cleanup_seconds=.015))
        self.assertEqual(result['accounting']['counts']['cleanup']['submitted'], 2)
        self.assertEqual(result['accounting']['counts']['cleanup']['timeout'], 1)
        self.assertEqual(result['accounting']['cleanup_wall_ns'], 20_000_000)
        self.assert_closed(enabled=False)

    def test_already_exhausted_cleanup_marks_even_zero_time_close_late(self):
        def slow_actor_close():
            self.actor.closed = True
            self.clock.advance(16)
        def zero_time_env_close():
            self.env.closed = True
        with patch.object(self.actor, 'close', slow_actor_close), patch.object(self.env, 'close', zero_time_env_close):
            result = self.run_episode(enabled=False, bounds=replace(self.bounds, cleanup_seconds=.015))
        self.assertEqual(result['accounting']['counts']['cleanup']['submitted'], 2)
        self.assertEqual(result['accounting']['counts']['cleanup']['timeout'], 2)
        self.assert_closed(enabled=False)

    def test_continue_timing_and_usefulness_do_not_add_actor_calls(self):
        def continue_answer(scorer, inputs):
            self.clock.advance(5)
            return {'timing': {'type': 'choice', 'choice': 'continue', 'confidence': .9,
                              'probabilities': {'replan': .1, 'continue': .9, 'uncertain': 0}},
                    'data_usefulness': {'type': 'choice', 'choice': 'useful', 'confidence': 1,
                                       'probabilities': {'useful': 1, 'not_useful': 0, 'uncertain': 0}}}
        with patch.object(type(self.scorer), '__call__', continue_answer):
            result = self.run_episode()
        self.assertEqual(result['accounting']['controls'], 600)
        self.assertEqual(result['accounting']['opportunity'], 'no_early_replan')
        self.assertEqual(result['accounting']['counts']['actor_extra']['submitted'], 0)
        self.assertFalse(result['accounting']['attributable_action_change'])
        self.assert_closed()

    def test_admitted_identical_candidate_is_not_an_attributable_change(self):
        original = type(self.actor).__call__
        def identical(actor, inputs, seed):
            if int(inputs[0][0, 1]) == 8:
                self.clock.advance(3)
                return episode._fixture_chunk(1152, 0)[8:] + episode._fixture_chunk(1152, 16)[:8]
            return original(actor, inputs, seed)
        with patch.object(type(self.actor), '__call__', identical):
            result = self.run_episode()
        self.assertEqual(result['accounting']['controls'], 600)
        self.assertEqual(result['accounting']['counts']['actor_extra']['completed'], 1)
        self.assertEqual(result['accounting']['changed_controls_at_handoff'], 0)
        self.assertFalse(result['accounting']['attributable_action_change'])
        self.assertTrue(result['accounting']['infrastructure_valid'])
        self.assert_closed()

    def test_zero_change_valid_policy_does_not_pass_fixture_mechanism_acceptance(self):
        original = episode._FixtureActor.__call__
        def identical(actor, inputs, seed):
            reset_seed, control = int(inputs[0][0, 0]), int(inputs[0][0, 1])
            if control == 8:
                actor.clock.advance(3)
                return (episode._fixture_chunk(reset_seed, 0)[8:] +
                        episode._fixture_chunk(reset_seed, 16)[:8])
            return original(actor, inputs, seed)
        with patch.object(episode._FixtureActor, '__call__', identical):
            result = episode.run_fixture_pilot()
        self.assertEqual(result['accounting']['status'], 'fixture_failed')
        self.assertFalse(result['accounting']['attributable_fixture_changes'])
        self.assertEqual(result['accounting']['controls'], 2400)
        self.assertTrue(all(item['accounting']['infrastructure_valid'] for item in result['episodes']))
        self.assertTrue(result['accounting']['stock_fixture_parity'])

    def test_missing_reset_component_bytes_never_make_a_tape(self):
        with patch.object(self.env, 'reset_components', return_value={}):
            result = self.run_episode()
        self.assertIsNone(result['tape'])
        self.assertEqual(result['accounting']['counts']['reset']['failed'], 1)
        self.assertFalse(result['accounting']['success'])
        self.assert_closed()

    def test_missing_reset_bytes_cannot_pass_pilot_pair_parity(self):
        with patch.object(episode._FixtureEnvironment, 'reset_components', return_value={}):
            result = episode.run_fixture_pilot()
        self.assertEqual(result['accounting']['status'], 'fixture_failed')
        self.assertFalse(result['accounting']['matching_reset_component_bytes'])
        self.assertFalse(result['accounting']['stock_fixture_parity'])
        self.assertTrue(all(tape is None for tape in result['tapes']))

    def test_invalid_post_control_frame_does_not_fabricate_retained_control(self):
        original = self.env.step
        def invalid(action):
            value = original(action)
            return Frame(9, value.time_ns, value.components)
        with patch.object(self.env, 'step', invalid):
            result = self.run_episode()
        self.assertEqual(len(result['tape'].actions), 0)
        self.assertEqual(result['accounting']['counts']['step']['failed'], 1)
        self.assertFalse(next(row for row in result['accounting']['journal']
                              if row['kind'] == 'step')['retained_in_tape'])
        self.assert_closed()

    def test_bounds_and_actor_seed_are_explicit(self):
        for value in (0, -1, float('nan'), float('inf'), True):
            with self.assertRaises(ValueError):
                replace(self.bounds, actor_seconds=value)
        with self.assertRaises(ValueError):
            replace(self.bounds, handoff_seconds=31)
        with self.assertRaises(ValueError):
            episode.run_episode(self.env, self.actor, self.scorer, episode_id='x', seed=1152,
                clef_enabled=True, bounds=self.bounds, actor_seed=7)


if __name__ == '__main__':
    unittest.main()
