"""Interval-sidecar contracts; scripted annotations do not measure robot semantics."""
import copy
import json
from pathlib import Path
import tempfile
import unittest

import pyarrow as pa
import pyarrow.compute as pc

from coppertop.analysis import FrameData, FRAME_SCHEMA, from_release
from coppertop.chord_effects import effect_annotation, effect_table, from_effect_release
from coppertop.chords import LocalStore, canonical, digest, fork, frame_id
from test_analysis import fixture


class EffectTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.store = LocalStore(Path(self.temp.name))
        self.release, _, self.record = fixture(self.store)
        self.data = from_release(self.store, self.release)

    def annotation(self, **changes):
        args = dict(episode_id=self.record['episode_id'], start=0, end=2,
                    entity='mug', predicate='support_relation', before='table', after='gripper',
                    evidence_kind='synthetic_fixture', provenance=digest(b'fixture rubric'))
        args.update(changes)
        return effect_annotation(self.data, **args)

    def test_contract_effect_query_preserves_native_source_and_conflicting_labels(self):
        native = self.data.table.to_pylist()
        windows = self.data.windows().to_pylist()
        before = {p: p.read_bytes() for p in self.store.root.rglob('*') if p.is_file()}
        first = self.annotation()
        second = self.annotation(after='unknown', available_at=3)
        table = effect_table(self.data, [first, second])
        self.assertEqual(table.to_pylist(), effect_table(self.data, [second, first]).to_pylist())
        self.assertEqual(table.num_rows, 2)
        row = table.filter(pc.equal(table['after'], 'gripper')).to_pylist()[0]
        self.assertEqual((row['start'], row['end'], row['available_at']), (0, 2, 2))
        self.assertEqual((row['start_frame_id'], row['end_frame_id']),
                         (native[0]['frame_id'], native[2]['frame_id']))
        self.assertEqual(row['end_time_ns'], native[2]['time_ns'])
        self.assertEqual(row['source_sha256'], self.record['signals'])
        self.assertEqual(effect_table(self.data, [first, second], available_at=1).num_rows, 0)
        self.assertEqual(effect_table(self.data, [first, second], available_at=2).num_rows, 1)
        self.assertEqual(windows, self.data.windows().to_pylist())
        self.assertEqual(native, self.data.table.to_pylist())
        self.assertEqual(before, {p: p.read_bytes() for p in self.store.root.rglob('*') if p.is_file()})
        with self.assertRaisesRegex(ValueError, 'binding'):
            effect_table(self.data, [dict(first, source_sha256=digest(b'foreign'))])
        terminal = self.annotation(start=2, end=3)
        self.assertEqual(effect_table(self.data, [terminal])['end'][0].as_py(), 3)

    def test_contract_effect_query_typed_sidecars_cannot_reassign_split(self):
        effect = self.annotation()
        child = fork(self.store, self.release, overlays=(self.store.put(canonical(effect)),))
        data, table = from_effect_release(self.store, child)
        self.assertEqual(table.num_rows, 1)
        self.assertEqual(data.table['source_sha256'].to_pylist(), self.data.table['source_sha256'].to_pylist())
        self.assertEqual(json.loads(self.store.get(child))['episodes'],
                         json.loads(self.store.get(self.release))['episodes'])
        foreign = dict(effect, split='sealed_test')
        bad = fork(self.store, self.release, overlays=(self.store.put(canonical(foreign)),))
        self.assertEqual(set(from_release(self.store, bad).table['split'].to_pylist()), {'development'})
        with self.assertRaisesRegex(ValueError, 'binding'):
            from_effect_release(self.store, bad)

    def test_every_source_and_endpoint_binding_is_checked(self):
        annotation = self.annotation()
        for field, value in [('episode_id', 'foreign'), ('episode_ref', digest(b'foreign')),
                             ('initialization_group', 'other'),
                             ('split', 'train'), ('data_kind', 'real_simulation'),
                             ('start_frame_id', digest(b'foreign')), ('end_frame_id', digest(b'foreign')),
                             ('available_frame_id', digest(b'foreign')), ('start_time_ns', 4),
                             ('end_time_ns', 4), ('available_time_ns', 4),
                             ('start_time_ns', False), ('start_time_ns', 0.0),
                             ('schema_version', 2), ('schema_version', True), ('kind', 'prediction')]:
            with self.subTest(field=field, value=value), self.assertRaises(ValueError):
                effect_table(self.data, [dict(annotation, **{field: value})])
        with self.assertRaisesRegex(ValueError, 'Duplicate'):
            effect_table(self.data, [annotation, annotation])
        with self.assertRaisesRegex(ValueError, 'binding'):
            effect_table(self.data, [dict(annotation, future_outcome=True)])

    def test_intervals_unknown_labels_and_bounds(self):
        for args in [dict(start=True), dict(start=-1), dict(start=2), dict(end=4),
                     dict(available_at=1), dict(available_at=4), dict(available_at=True),
                     dict(evidence_kind='model_prediction'), dict(entity=''), dict(predicate='x'*257),
                     dict(before=4), dict(provenance='not-a-hash')]:
            with self.subTest(args=args), self.assertRaises(ValueError):
                self.annotation(**args)
        self.assertIsNone(self.annotation(before=None, after=None)['after'])
        self.assertEqual(effect_table(self.data, []).num_rows, 0)
        for cutoff in (-1, True, 1.5):
            with self.assertRaises(ValueError):
                effect_table(self.data, [], available_at=cutoff)
        rows = copy.deepcopy(self.data.table.to_pylist())
        rows[1]['action'] = None
        data = FrameData(pa.Table.from_pylist(rows, schema=FRAME_SCHEMA), self.data.sources)
        with self.assertRaisesRegex(ValueError, 'outgoing'):
            effect_annotation(data, **{key: value for key, value in self.annotation().items()
                                      if key in ('episode_id', 'start', 'end', 'entity', 'predicate',
                                                 'before', 'after', 'evidence_kind', 'provenance')})

    def test_initialization_siblings_cannot_cross_split(self):
        rows = self.data.table.to_pylist()
        siblings = copy.deepcopy(rows)
        for row in siblings:
            row.update(episode_id='sibling', split='train', frame_id=frame_id('sibling', row['frame_index']))
        bad = FrameData(pa.Table.from_pylist(rows+siblings, schema=FRAME_SCHEMA), self.data.sources)
        with self.assertRaisesRegex(ValueError, 'cross splits'):
            effect_table(bad, [])

    def test_overlapping_annotations_do_not_rescan_native_actions(self):
        rows = []
        original = self.data.table.to_pylist()[0]
        for index in range(128):
            row = copy.deepcopy(original)
            row.update(frame_index=index, time_ns=index*50_000_000,
                       frame_id=frame_id(row['episode_id'], index))
            if index == 127:
                row['action'] = None
            rows.append(row)
        table = pa.Table.from_pylist(rows, schema=FRAME_SCHEMA)
        data = FrameData(table, self.data.sources)
        fields = dict(episode_id=original['episode_id'], start=0, end=127,
                      entity='mug', predicate='support_relation', before='table', after='gripper',
                      evidence_kind='synthetic_fixture', provenance=digest(b'fixture rubric'))
        annotation = effect_annotation(data, **fields)
        overlays = [dict(annotation, entity=f'entity-{index}') for index in range(128)]
        reads = [0]

        class CountedRow(dict):
            def __getitem__(self, key):
                if key == 'action':
                    reads[0] += 1
                return super().__getitem__(key)

        class CountedTable:
            def __getattr__(self, key):
                return getattr(table, key)

            def to_pylist(self):
                return [CountedRow(row) for row in table.to_pylist()]

        counted = FrameData(CountedTable(), self.data.sources)
        self.assertEqual(effect_table(counted, overlays).num_rows, len(overlays))
        # Count work rather than wall time: each frame plus bounded per-label work.
        self.assertLessEqual(reads[0], 3*len(rows) + len(overlays))

    def test_missing_action_is_checked_only_within_each_half_open_interval(self):
        rows = self.data.table.to_pylist()
        rows[1]['action'] = None
        data = FrameData(pa.Table.from_pylist(rows, schema=FRAME_SCHEMA), self.data.sources)
        fields = {key: value for key, value in self.annotation().items()
                  if key in ('episode_id', 'entity', 'predicate', 'before', 'after', 'evidence_kind', 'provenance')}
        for start, end in ((0, 1), (2, 3)):
            self.assertEqual(effect_annotation(data, start=start, end=end, **fields)['end'], end)
        for start, end in ((0, 2), (1, 2), (1, 3)):
            with self.subTest(start=start, end=end), self.assertRaisesRegex(ValueError, 'outgoing'):
                effect_annotation(data, start=start, end=end, **fields)
