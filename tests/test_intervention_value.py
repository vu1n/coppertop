"""Synthetic matched outcomes exercise the reducer; they are no robotics evidence."""
from dataclasses import replace
import unittest
from unittest.mock import patch

from coppertop.chords import canonical, digest
from coppertop.intervention_value import (Continuation, PairProtocol, ReplayEvidence,
                                         UtilityRecipe, continuation_record, evaluate_pair, summarize)
from coppertop.mechanism import COMPONENTS, Frame, Tape, native_actions


def protocol(group='synthetic/init', split='development', ceiling=2):
    return PairProtocol(group, split, 1, 4, ceiling, digest(b'actor'), digest(b'continuation'),
                        digest(b'paused synchronous fixture clock'), digest(b'causal input schema'),
                        tuple([17] * ceiling))


def recipe(**changes):
    args = dict(success=1.0, wall_second=0.01, actor_query=0.1, scorer_query=0.02,
                cost_unit=1.0, task_failure=0.2, margin=0.001, cost_unit_name='synthetic_cost_unit',
                task_failure_definition_sha256=digest(b'scripted task failure event'))
    args.update(changes)
    return UtilityRecipe(**args)


def tape(name, *, changed=False, success=False, end=4, terminated=False, truncated=False):
    actions, frames = [], []
    for index in range(end + 1):
        value = str(index) + ('/changed' if changed and index > 1 else '')
        frames.append(Frame(index, index * 50_000_000,
                            tuple((key, digest((key+value).encode())) for key in COMPONENTS),
                            success=success and index == end,
                            terminated=terminated and index == end,
                            truncated=truncated and index == end))
        if index < end:
            actions.append(native_actions([1.0 if changed and index >= 1 else 0.0] * 12))
    return Tape(name, 1152, tuple(actions), tuple(frames),
                parent_episode_id='synthetic/decision', inherited_frames=min(2, len(frames)))


def call(kind='actor', *, control=1, started=0, status='completed', cost=0.01, seed=17):
    return dict(kind=kind, control=control, started_ns=started,
                finished_ns=None if status == 'submitted' else started + 100,
                status=status, cost_units=cost, seed=seed if kind == 'actor' else None)


def branch(proto, name, *, changed=False, success=False, calls=None, **changes):
    value = tape(name, changed=changed, success=success)
    args = dict(tape=value, initialization_group=proto.initialization_group, split=proto.split,
                protocol_sha256=proto.identity, calls=(call(),) if calls is None else calls,
                wall_ns=1_000_000_000, pause_ns=1000, success_wall_ns=900_000_000 if success else None,
                task_failures=0, infrastructure_valid=True)
    args.update(changes)
    return Continuation(**args)


def replay():
    return ReplayEvidence(tape('uninterrupted'), tape('restored'), digest(b'CPU coverage fixture'), 'synthetic_fixture')


class ValueTests(unittest.TestCase):
    def test_identifiers_are_bounded_before_serializing_direct_library_evidence(self):
        proto = protocol()
        original = branch(proto, 'continue')
        for field in ('episode_id', 'parent_episode_id'):
            oversized = replace(original, tape=replace(original.tape, **{field: 'ø'*513}))
            with self.subTest(field=field), patch.object(Tape, 'record', side_effect=AssertionError('serialized early')):
                with self.assertRaisesRegex(ValueError, 'identifier'):
                    continuation_record(oversized)
        maximum = replace(original, tape=replace(original.tape, episode_id='x'*1024))
        self.assertEqual(continuation_record(maximum)['tape']['episode_id'], 'x'*1024)

    def test_contract_paired_value_keeps_vectors_costs_and_provisional_help_harm(self):
        proto = protocol()
        continuing = branch(proto, 'continue')
        replanning = branch(proto, 'replan', changed=True, success=True,
                            calls=(call('scorer'), call(started=100), call(control=3, started=200)))
        pair = evaluate_pair(continuing, replanning, protocol=proto, recipe=recipe(), replay=replay())
        self.assertTrue(pair['valid'])
        self.assertEqual(pair['timing_label'], 'replan')
        self.assertAlmostEqual(pair['delta'], 0.86)
        self.assertEqual(pair['branches']['replan']['actor_queries'], 2)
        self.assertEqual(pair['branches']['replan']['scorer_queries'], 1)
        self.assertEqual(pair['branches']['replan']['pause_ns'], 1000)
        self.assertEqual(pair['changed_controls'], 3)
        self.assertEqual(pair['label_status'], 'provisional_cpu_fixture')
        self.assertFalse(pair['robotics_benefit_established'])
        self.assertEqual(pair['branches']['continue']['capped_controls_to_success'], 3)
        self.assertEqual(pair['branches']['continue']['evidence_sha256'], digest(canonical(continuation_record(continuing))))
        harmful = evaluate_pair(branch(proto, 'continue-success', success=True),
                                branch(proto, 'replan-failure', changed=True, task_failures=1),
                                protocol=proto, recipe=recipe(), replay=replay())
        self.assertEqual(harmful['timing_label'], 'continue')
        self.assertAlmostEqual(harmful['delta'], -1.2)
        self.assertTrue(harmful['branches']['continue']['success'])
        self.assertFalse(harmful['branches']['replan']['success'])
        neutral = evaluate_pair(continuing, branch(proto, 'neutral', changed=True),
                                protocol=proto, recipe=recipe(), replay=replay())
        self.assertEqual(neutral['timing_label'], 'uncertain')
        no_handoff = evaluate_pair(continuing, branch(proto, 'same', calls=(call(), call(started=200))),
                                   protocol=proto, recipe=recipe(), replay=replay())
        self.assertEqual(no_handoff['timing_label'], 'continue')
        self.assertEqual(no_handoff['handoff'], 'no_handoff')

    def test_contract_missing_evidence_and_unknown_prefix_never_become_continue(self):
        proto = protocol()
        pair = evaluate_pair(branch(proto, 'continue'), branch(proto, 'replan'),
                             protocol=proto, recipe=recipe())
        self.assertIsNone(pair['delta'])
        self.assertIsNone(pair['timing_label'])
        self.assertIn('missing_replay_verification', pair['invalid_reasons'])
        report = summarize([pair])
        self.assertEqual(report['scheduled_pairs'], 1)
        self.assertEqual(report['valid_fixture_pairs'], 0)
        self.assertEqual(report['qualified_robotics_pairs'], 0)
        self.assertEqual(report['labels'], {})
        self.assertEqual(summarize([])['scheduled_pairs'], 0)
        prefix = branch(proto, 'prefix', tape=tape('prefix', end=2, truncated=True))
        pair = evaluate_pair(prefix, branch(proto, 'replan'), protocol=proto, recipe=recipe(), replay=replay())
        self.assertFalse(pair['branches']['continue']['outcome_known'])
        self.assertTrue(pair['branches']['continue']['truncated'])
        self.assertIsNone(pair['timing_label'])
        real = evaluate_pair(branch(proto, 'continue'), branch(proto, 'replan'),
                             protocol=proto, recipe=recipe(), replay=replace(replay(), scope='recorded_replay'))
        self.assertIn('simulator_restoration_coverage_unqualified', real['invalid_reasons'])
        self.assertIsNone(real['delta'])

    def test_contract_paired_value_checks_audited_prefix_and_no_intervention_suffix(self):
        proto = protocol()
        for key in COMPONENTS:
            base = branch(proto, 'replan', changed=True)
            frames = list(base.tape.frames)
            frames[1] = replace(frames[1], components=tuple((k, digest(b'wrong') if k == key else v)
                                                         for k, v in frames[1].components))
            modified = replace(base, tape=replace(base.tape, frames=tuple(frames)))
            pair = evaluate_pair(branch(proto, 'continue'), modified, protocol=proto, recipe=recipe(), replay=replay())
            self.assertIn('branch_prefix_mismatch', pair['invalid_reasons'])
        for evidence in [replace(replay(), restored=tape('restore-wrong', changed=True)),
                         replace(replay(), uninterrupted=tape('no-suffix', end=1), restored=tape('no-suffix-2', end=1))]:
            pair = evaluate_pair(branch(proto, 'continue'), branch(proto, 'replan'),
                                 protocol=proto, recipe=recipe(), replay=evidence)
            self.assertIn('no_intervention_replay_mismatch', pair['invalid_reasons'])

    def test_contract_paired_value_requires_shared_native_prefix_identities(self):
        proto = protocol()
        continuing = branch(proto, 'continue')
        replanning = branch(proto, 'replan', changed=True, success=True)
        evidence = replay()
        baseline = evaluate_pair(continuing, replanning, protocol=proto, recipe=recipe(), replay=evidence)
        self.assertTrue(baseline['valid'])
        for arm in ('continue', 'replan', 'uninterrupted', 'restored'):
            first, second, verification = continuing, replanning, evidence
            original = {'continue': continuing.tape, 'replan': replanning.tape,
                        'uninterrupted': evidence.uninterrupted, 'restored': evidence.restored}[arm]
            foreign = replace(original, episode_id='unrelated/episode', parent_episode_id=None, inherited_frames=0)
            if arm == 'continue':
                first = replace(continuing, tape=foreign)
            elif arm == 'replan':
                second = replace(replanning, tape=foreign)
            else:
                verification = replace(evidence, **{arm: foreign})
            with self.subTest(arm=arm):
                pair = evaluate_pair(first, second, protocol=proto, recipe=recipe(), replay=verification)
                self.assertFalse(pair['valid'])
                self.assertIsNone(pair['delta'])
                self.assertIsNone(pair['timing_label'])

    def test_matching_boundary_identity_does_not_hide_different_reset_lineage(self):
        proto = protocol()
        evidence = replay()
        source = evidence.uninterrupted.parent_episode_id or evidence.uninterrupted.episode_id
        replanning = branch(proto, 'replan', changed=True)
        different_reset = replace(replanning.tape, episode_id=source,
                                  parent_episode_id='unrelated/reset', inherited_frames=1)
        self.assertEqual(different_reset.identity(proto.boundary), evidence.uninterrupted.identity(proto.boundary))
        self.assertNotEqual(different_reset.identity(0), evidence.uninterrupted.identity(0))
        pair = evaluate_pair(branch(proto, 'continue'), replace(replanning, tape=different_reset),
                             protocol=proto, recipe=recipe(), replay=evidence)
        self.assertIn('branch_prefix_mismatch', pair['invalid_reasons'])
        self.assertIsNone(pair['timing_label'])

    def test_native_parent_and_inherited_children_keep_distinct_suffix_identities(self):
        proto = protocol()
        evidence = replay()
        parent = replace(evidence.uninterrupted, episode_id='synthetic/decision',
                         parent_episode_id=None, inherited_frames=0)
        evidence = replace(evidence, uninterrupted=parent)
        continuing = branch(proto, 'continue')
        replanning = branch(proto, 'replan', changed=True, success=True)
        for child in (continuing.tape, replanning.tape, evidence.restored):
            self.assertEqual(child.identity(proto.boundary), parent.identity(proto.boundary))
            self.assertNotEqual(child.identity(proto.boundary + 1), parent.identity(proto.boundary + 1))
        pair = evaluate_pair(continuing, replanning, protocol=proto, recipe=recipe(), replay=evidence)
        self.assertTrue(pair['valid'])
        self.assertEqual(pair['case_frame_id'], parent.identity(proto.boundary))
        self.assertEqual(pair['timing_label'], 'replan')

    def test_contract_paired_value_budget_protocol_seed_and_accounting_are_strict(self):
        proto = protocol(ceiling=1)
        for kwargs in [dict(protocol_sha256=digest(b'other')), dict(split='sealed_test'),
                       dict(calls=(call(), call(started=200))), dict(calls=(call(seed=18),)),
                       dict(calls=(call(cost=True),)), dict(wall_ns=True), dict(pause_ns=2_000_000_000),
                       dict(success_wall_ns=1), dict(calls=(dict(call(), control=0),)),
                       dict(calls=(dict(call(), finished_ns=-1),))]:
            with self.subTest(kwargs=kwargs), self.assertRaises(ValueError):
                evaluate_pair(branch(proto, 'continue'), branch(proto, 'bad', **kwargs),
                              protocol=proto, recipe=recipe(), replay=replay())
        for status in ('failed', 'timeout', 'submitted'):
            pair = evaluate_pair(branch(proto, 'continue'), branch(proto, 'failed', calls=(call(status=status),)),
                                 protocol=proto, recipe=recipe(), replay=replay())
            self.assertIn('replan_infrastructure_invalid', pair['invalid_reasons'])
            self.assertEqual(pair['branches']['replan']['actor_queries'], 1)
            self.assertIsNone(pair['timing_label'])

    def test_contract_missing_evidence_eligibility_and_action_attribution_are_independent(self):
        proto = protocol()
        for eligible, reason in ((False, 'stale_candidate'), (False, 'budget_exhausted')):
            pair = evaluate_pair(branch(proto, 'continue'), branch(proto, 'replan', changed=True, success=True),
                                 protocol=proto, recipe=recipe(), replay=replay(),
                                 eligible=eligible, ineligible_reason=reason)
            self.assertIsNone(pair['timing_label'])
            self.assertEqual(summarize([pair])['exclusions'], {'ineligible:'+reason: 1})
        pair = evaluate_pair(branch(proto, 'continue'), branch(proto, 'no-actor', changed=True, calls=()),
                             protocol=proto, recipe=recipe(), replay=replay())
        self.assertIn('changed_actions_without_actor_query', pair['invalid_reasons'])
        late = branch(proto, 'late-actor', changed=True, calls=(call(control=3),))
        pair = evaluate_pair(branch(proto, 'continue'), late, protocol=proto, recipe=recipe(), replay=replay())
        self.assertIn('changed_actions_without_actor_query', pair['invalid_reasons'])
        pair = evaluate_pair(branch(proto, 'continue'), branch(proto, 'different-outcome', success=True),
                             protocol=proto, recipe=recipe(), replay=replay())
        self.assertIn('outcomes_differ_without_changed_controls', pair['invalid_reasons'])

    def test_groups_duplicates_and_modified_pair_bytes_cannot_inflate_report(self):
        proto = protocol()
        pair = evaluate_pair(branch(proto, 'continue'), branch(proto, 'replan'),
                             protocol=proto, recipe=recipe(), replay=replay())
        with self.assertRaisesRegex(ValueError, 'Duplicate'):
            summarize([pair, pair])
        with self.assertRaisesRegex(ValueError, 'immutable'):
            summarize([dict(pair, timing_label='replan')])
        other = replace(proto, split='sealed_test')
        pair2 = evaluate_pair(branch(other, 'other-continue'), branch(other, 'other-replan'),
                              protocol=other, recipe=recipe(), replay=replay())
        with self.assertRaisesRegex(ValueError, 'cross splits'):
            summarize([pair, pair2])

    def test_recipe_and_protocol_identity_bind_cost_condition_and_margins(self):
        proto = protocol()
        for kwargs in (dict(boundary=True), dict(horizon=601), dict(query_ceiling=0), dict(actor_seeds=(17,)),
                       dict(actor_sha256='wrong')):
            with self.subTest(kwargs=kwargs), self.assertRaises(ValueError):
                replace(proto, **kwargs)
        for value in (float('nan'), float('inf'), -1, True):
            with self.assertRaises(ValueError):
                recipe(actor_query=value)
        baseline = branch(proto, 'continue')
        pair = evaluate_pair(baseline, branch(proto, 'replan', changed=True),
                             protocol=proto, recipe=recipe(margin=0), replay=replay())
        self.assertEqual(pair['timing_label'], 'uncertain')
        updated = evaluate_pair(baseline, branch(proto, 'replan', changed=True),
                                protocol=proto, recipe=recipe(actor_query=0.9), replay=replay())
        self.assertNotEqual(pair['recipe_sha256'], updated['recipe_sha256'])
        self.assertNotEqual(pair['pair_id'], updated['pair_id'])
        boundary = evaluate_pair(baseline, branch(proto, 'margin', changed=True, calls=(call(), call(started=200))),
                                 protocol=proto, recipe=recipe(actor_query=0.1, cost_unit=0, margin=0.1), replay=replay())
        self.assertEqual(boundary['timing_label'], 'uncertain')
        for kwargs in (dict(cost_unit_name=''), dict(task_failure_definition_sha256='unknown')):
            with self.assertRaises(ValueError):
                recipe(**kwargs)
