"""Actual on-disk Arrow and content-addressed evidence paths, entirely offline."""
from contextlib import redirect_stdout
import io
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

import pyarrow.compute as pc
import pyarrow.parquet as pq

from coppertop.chord_effects import from_effect_release
from coppertop.chords import LocalStore, canonical, digest
from coppertop.effect_cli import demo, main
from coppertop.intervention_value import evaluate_plan


class LocalWorkflowTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.directory = self.root / 'example'
        self.example = demo(self.directory)
        self.store = LocalStore(self.directory / 'store')

    def test_saved_plan_recomputes_values_and_preserves_all_scheduled_cases(self):
        report = evaluate_plan(self.store, self.example['evaluation_plan'])
        self.assertEqual(report, json.loads((self.directory / 'report.json').read_bytes()))
        self.assertEqual((report['scheduled_pairs'], report['evaluated_pairs'], report['valid_fixture_pairs']), (6, 5, 4))
        self.assertEqual(report['initialization_groups'], 1)
        self.assertEqual(report['labels'], {'continue': 2, 'replan': 1, 'uncertain': 1})
        self.assertEqual(report['qualified_robotics_pairs'], 0)
        self.assertFalse(report['robotics_benefit_established'])
        self.assertEqual(report['exclusions'], {'missing_replay_verification': 1})
        self.assertEqual(report['missing_comparisons'], [dict(comparison_id='missing-branch', reason='missing_branch', arms=['replan'])])
        second = demo(self.root / 'second')
        self.assertEqual(self.example, second)
        self.assertEqual((self.directory / 'report.json').read_bytes(), (self.root / 'second/report.json').read_bytes())

    def test_arrow_filter_and_native_join_use_existing_frame_ids(self):
        data, effects = from_effect_release(self.store, self.example['effect_release'])
        self.assertEqual(effects.to_pylist(), pq.read_table(self.directory / 'effects.parquet').to_pylist())
        moved = effects.filter(pc.equal(effects['after'], 'gripper'))
        self.assertEqual(moved.num_rows, 1)
        joined = moved.join(data.table.select(['frame_id', 'time_ns']),
                            keys='end_frame_id', right_keys='frame_id', join_type='inner')
        self.assertEqual(joined.num_rows, 1)
        native = data.table.filter(pc.is_in(data.table['frame_id'], value_set=moved['end_frame_id']))
        self.assertEqual(native['state'][0].as_py(), [0.0]*16)
        self.assertEqual(joined['time_ns'][0].as_py(), 100_000_000)

    def test_cli_index_and_evaluate_refuse_overwrites(self):
        cases = [('index', '--release', self.example['effect_release'], 'new.parquet'),
                 ('evaluate', '--plan', self.example['evaluation_plan'], 'new.json')]
        for command, flag, identity, output in cases:
            path = self.root / output
            args = [command, '--store', str(self.directory / 'store'), flag, identity, '--output', str(path)]
            with redirect_stdout(io.StringIO()):
                main(args)
            before = path.read_bytes()
            with redirect_stdout(io.StringIO()), self.assertRaises(FileExistsError):
                main(args)
            self.assertEqual(path.read_bytes(), before)
        with self.assertRaises(FileExistsError):
            demo(self.directory)

    def test_missing_objects_are_visible_but_corruption_is_not_an_outcome(self):
        plan = json.loads(self.store.get(self.example['evaluation_plan']))
        missing = dict(plan['comparisons'][0], replan=digest(b'unavailable object'))
        value = dict(plan, comparisons=[missing])
        report = evaluate_plan(self.store, self.store.put(canonical(value)))
        self.assertEqual((report['scheduled_pairs'], report['evaluated_pairs']), (1, 0))
        self.assertEqual(report['missing_comparisons'][0]['reason'], 'missing_object')
        self.assertEqual(report['labels'], {})
        self.store.path(plan['comparisons'][0]['replan']).write_bytes(b'corrupt')
        with self.assertRaisesRegex(ValueError, 'mismatch'):
            evaluate_plan(self.store, self.example['evaluation_plan'])

    def test_every_reloaded_frame_and_protocol_binding_is_checked(self):
        original = json.loads(self.store.get(self.example['evaluation_plan']))
        spec = original['comparisons'][0]
        evidence = json.loads(self.store.get(spec['replan']))
        for mutation in ('frame', 'protocol', 'success', 'extra_field'):
            value = json.loads(canonical(evidence))
            if mutation == 'frame': value['tape']['frames'][2]['frame_id'] = digest(b'foreign frame')
            elif mutation == 'protocol': value['protocol_sha256'] = digest(b'different actor')
            elif mutation == 'success': value['tape']['frames'][-1]['success'] = False
            else: value['future_reward'] = 1.0
            spec2 = dict(spec, replan=self.store.put(canonical(value)))
            key = self.store.put(canonical(dict(original, comparisons=[spec2])))
            with self.subTest(mutation=mutation), self.assertRaises((ValueError, TypeError)):
                evaluate_plan(self.store, key)

    def test_missing_branch_cannot_hide_corrupt_present_artifacts(self):
        plan = json.loads(self.store.get(self.example['evaluation_plan']))
        spec = plan['comparisons'][0]
        for missing_arm in ('continue', 'replan'):
            for missing_ref in (None, digest(b'unavailable object')):
                for present in ('continue', 'replan', 'replay'):
                    if present == missing_arm:
                        continue
                    path = self.store.path(spec[present])
                    original = path.read_bytes()
                    key = self.store.put(canonical(dict(plan, comparisons=[dict(spec, **{missing_arm: missing_ref})])))
                    try:
                        path.write_bytes(b'corrupt')
                        with self.subTest(missing_arm=missing_arm, missing_ref=missing_ref, present=present):
                            with self.assertRaisesRegex(ValueError, 'mismatch'):
                                evaluate_plan(self.store, key)
                    finally:
                        path.write_bytes(original)

    def test_missing_branch_cannot_hide_malformed_present_artifacts(self):
        plan = json.loads(self.store.get(self.example['evaluation_plan']))
        spec = plan['comparisons'][0]
        for missing_arm in ('continue', 'replan'):
            for missing_ref in (None, digest(b'unavailable object')):
                for present in ('continue', 'replan', 'replay'):
                    if present == missing_arm:
                        continue
                    value = json.loads(self.store.get(spec[present]))
                    tape = value['uninterrupted'] if present == 'replay' else value['tape']
                    tape['frames'][0]['frame_id'] = digest(b'foreign frame')
                    changed = dict(spec, **{missing_arm: missing_ref, present: self.store.put(canonical(value))})
                    key = self.store.put(canonical(dict(plan, comparisons=[changed])))
                    with self.subTest(missing_arm=missing_arm, missing_ref=missing_ref, present=present):
                        with self.assertRaisesRegex(ValueError, 'normalization|binding'):
                            evaluate_plan(self.store, key)

    def test_missing_branch_cannot_hide_invalid_accounting_or_replay_scope(self):
        plan = json.loads(self.store.get(self.example['evaluation_plan']))
        spec = plan['comparisons'][0]
        for missing_ref in (None, digest(b'unavailable object')):
            for present, mutation in (('replan', 'protocol'), ('replan', 'wall'), ('replay', 'scope')):
                value = json.loads(self.store.get(spec[present]))
                if mutation == 'protocol':
                    value['protocol_sha256'] = digest(b'different protocol')
                elif mutation == 'wall':
                    value['wall_ns'] = -1
                else:
                    value['scope'] = 'unqualified_override'
                changed = dict(spec, **{'continue': missing_ref, present: self.store.put(canonical(value))})
                key = self.store.put(canonical(dict(plan, comparisons=[changed])))
                with self.subTest(missing_ref=missing_ref, mutation=mutation), self.assertRaises(ValueError):
                    evaluate_plan(self.store, key)

    def test_missing_comparisons_are_still_grouped_and_all_metadata_is_bounded(self):
        plan = json.loads(self.store.get(self.example['evaluation_plan']))
        spec = plan['comparisons'][-1]
        other = dict(spec, comparison_id='sibling', protocol=dict(spec['protocol'], split='sealed_test'))
        key = self.store.put(canonical(dict(plan, comparisons=[spec, other])))
        with self.assertRaisesRegex(ValueError, 'cross splits'):
            evaluate_plan(self.store, key)
        with patch('coppertop.intervention_value.MAX_EVIDENCE_BYTES', 1), self.assertRaises(ValueError):
            evaluate_plan(self.store, self.example['evaluation_plan'])

    def test_missing_comparisons_cannot_hide_invalid_eligibility(self):
        plan = json.loads(self.store.get(self.example['evaluation_plan']))
        spec = plan['comparisons'][0]
        for absent in (None, digest(b'unavailable object')):
            for eligible, reason in ((True, 'stale'), (1, None), (0, 'stale'),
                                     (False, None), (False, ''), (False, 'x'*257)):
                changed = dict(spec, **{'continue': absent, 'eligible': eligible, 'ineligible_reason': reason})
                key = self.store.put(canonical(dict(plan, comparisons=[changed])))
                with self.subTest(absent=absent, eligible=eligible, reason=reason):
                    with self.assertRaisesRegex(ValueError, 'eligibility'):
                        evaluate_plan(self.store, key)
            changed = dict(spec, **{'continue': absent, 'eligible': False, 'ineligible_reason': 'stale'})
            key = self.store.put(canonical(dict(plan, comparisons=[changed])))
            report = evaluate_plan(self.store, key)
            self.assertEqual(report['evaluated_pairs'], 0)
            self.assertEqual(len(report['missing_comparisons']), 1)
            self.assertEqual(report['labels'], {})

    def test_oversized_utf8_identifiers_fail_before_tape_construction(self):
        plan = json.loads(self.store.get(self.example['evaluation_plan']))
        spec = plan['comparisons'][0]
        for field, value in [('episode_id', 'x'*1025), ('parent_episode_id', 'x'*1025),
                             ('episode_id', 'ø'*513), ('parent_episode_id', 'ø'*513)]:
            evidence = json.loads(self.store.get(spec['continue']))
            evidence['tape'][field] = value
            key = self.store.put(canonical(evidence))
            modified = self.store.put(canonical(dict(plan, comparisons=[dict(spec, **{'continue': key})])))
            with self.subTest(field=field), patch('coppertop.intervention_value.Frame', side_effect=AssertionError('constructed early')):
                with self.assertRaisesRegex(ValueError, 'identifier'):
                    evaluate_plan(self.store, modified)

    def test_cached_evidence_still_consumes_cumulative_processing_budget(self):
        plan = json.loads(self.store.get(self.example['evaluation_plan']))
        first = plan['comparisons'][0]
        second = dict(first, comparison_id='same-evidence-ineligible', eligible=False, ineligible_reason='fixture_exclusion')
        key = self.store.put(canonical(dict(plan, comparisons=[first, second])))
        first_pass_bytes = len(self.store.get(key)) + sum(len(self.store.get(first[field])) for field in ('continue', 'replan', 'replay'))
        self.assertEqual(evaluate_plan(self.store, key)['evaluated_pairs'], 2)
        with patch('coppertop.intervention_value.MAX_PROCESSING_BYTES', first_pass_bytes):
            with self.assertRaisesRegex(ValueError, 'processing byte bound'):
                evaluate_plan(self.store, key)
