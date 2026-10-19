"""Derived analysis contracts; synthetic fixtures never establish robot performance."""
import copy
import importlib.util
import io
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

import numpy as np
import pyarrow as pa
import pyarrow.compute as pc
import pyarrow.parquet as pq

from coppertop.analysis import FRAME_SCHEMA, FrameData, Snapshot, from_release, write_snapshot
from coppertop.chords import LocalStore, canonical, digest, frame_id, release
from coppertop.official_recording import ACTION_LAYOUT, STATE_LAYOUT

HAS_ANALYSIS = all(importlib.util.find_spec(name) for name in ('lance', 'datafusion'))


def fixture(store, *, generated=True, horizon=4, success=False):
    episode_id = 'fixture/episode'
    rows = []
    for index in range(4):
        values = [index + x / 16 for x in range(16)]
        action = None if index == 3 and generated else [index + x / 12 for x in range(12)]
        common = {'frame_id': frame_id(episode_id, index), 'action': action}
        if generated:
            rows.append(dict(common, step=index, time_ns=index * 50_000_000, state=values,
                             observation=digest(b'raw observation'), success=success and index == 3,
                             terminated=False, truncated=False))
        else:
            rows.append(dict(common, frame_index=index, timestamp=index / 20,
                             **{'observation.state': values, 'next.done': index == 3}))
    out = io.BytesIO()
    pq.write_table(pa.Table.from_pylist(rows), out)
    signals = store.put(out.getvalue())
    result = {'status': 'completed', 'horizon': horizon, 'success': success,
              'outcome_scope': 'official episode outcome' if horizon == 600 else 'bounded prefix'}
    record = {'schema_version': 1, 'episode_id': episode_id, 'initialization_group': 'fixture/init',
              'state_layout': STATE_LAYOUT, 'action_layout': ACTION_LAYOUT, 'signals': signals,
              'length': 4, 'videos': {'left': {'object': digest(b'original MP4')}},
              'provenance': {'real_simulation': False, 'real_data': False, 'split': 'development'}}
    if generated:
        record.update(kind='generated_official_rollout', official_result=result)
    episode = store.put(canonical(record))
    overlay = store.put(canonical({'episode_id': episode_id, 'split': 'development'}))
    return release(store, [episode], overlays=(overlay,)), rows, record


class FixtureStore:
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.store = LocalStore(self.root / 'canonical')


class FrameTests(FixtureStore, unittest.TestCase):
    def test_generated_prefix_preserves_values_and_unknown_final_outcome(self):
        key, rows, _ = fixture(self.store)
        before = {p: p.read_bytes() for p in self.store.root.rglob('*') if p.is_file()}
        data = from_release(self.store, key)
        actual = data.table.to_pylist()
        self.assertEqual([r['state'] for r in actual], [r['state'] for r in rows])
        self.assertEqual([r['action'] for r in actual], [r['action'] for r in rows])
        self.assertTrue(all(r['episode_success'] is None for r in actual))
        self.assertEqual({r['data_kind'] for r in actual}, {'synthetic_fixture'})
        self.assertEqual(before, {p: p.read_bytes() for p in self.store.root.rglob('*') if p.is_file()})

    def test_imported_done_is_not_success_or_training_eligibility(self):
        key, rows, _ = fixture(self.store, generated=False)
        actual = from_release(self.store, key).table.to_pylist()
        self.assertEqual([r['state'] for r in actual], [r['observation.state'] for r in rows])
        self.assertTrue(actual[-1]['terminated'])
        self.assertIsNone(actual[-1]['success'])
        self.assertIsNone(actual[-1]['episode_success'])
        self.assertEqual({r['split'] for r in actual}, {'development'})

    def test_development_annotation_cannot_be_promoted_to_holdout(self):
        key, _, record = fixture(self.store)
        changed = self.store.put(canonical({'episode_id': record['episode_id'], 'split': 'sealed_test'}))
        old = json.loads(self.store.get(key))
        key = release(self.store, old['episodes'], overlays=(changed,))
        with self.assertRaisesRegex(ValueError, 'development data'):
            from_release(self.store, key)

    def test_full_official_result_must_match_final_observation(self):
        key, _, record = fixture(self.store, horizon=600, success=True)
        self.assertTrue(from_release(self.store, key).table['episode_success'][0].as_py())
        record['official_result']['success'] = False
        bad = release(self.store, [self.store.put(canonical(record))])
        with self.assertRaisesRegex(ValueError, 'outcome differs'):
            from_release(self.store, bad)

    def test_windows_are_past_only_and_retain_padding_mask(self):
        key, _, _ = fixture(self.store)
        data = from_release(self.store, key)
        rows = data.windows((-2, 0)).to_pylist()
        self.assertEqual(rows[0]['history_frame_ids'], [rows[0]['frame_id']] * 2)
        self.assertEqual(rows[0]['history_valid'], [False, True])
        self.assertEqual(rows[2]['history_valid'], [True, True])
        for row in rows:
            self.assertTrue(all(t <= row['time_ns'] for t in row['history_time_ns']))
        for offsets in [(0, 1), (-1, -1, 0), (-1,), (), tuple(range(-65, 1))]:
            with self.assertRaises(ValueError):
                data.windows(offsets)

    def test_duplicate_frames_and_cross_split_siblings_rejected(self):
        key, _, _ = fixture(self.store)
        data = from_release(self.store, key)
        rows = data.table.to_pylist()
        bad = copy.deepcopy(rows)
        bad[1]['frame_id'] = bad[0]['frame_id']
        with self.assertRaises(ValueError):
            FrameData(pa.Table.from_pylist(bad, schema=FRAME_SCHEMA), data.sources).validate()
        bad = copy.deepcopy(rows)
        for row in bad:
            row['episode_id'] = 'sibling'; row['frame_id'] = frame_id('sibling', row['frame_index']); row['split'] = 'train'
        with self.assertRaisesRegex(ValueError, 'cross splits'):
            FrameData(pa.Table.from_pylist(rows + bad, schema=FRAME_SCHEMA), data.sources).validate()

    def test_timestamp_boundary_and_corrupt_signal_fail_closed(self):
        key, _, record = fixture(self.store)
        data = from_release(self.store, key)
        for mutation in ('timestamp', 'boundary'):
            rows = data.table.to_pylist()
            if mutation == 'timestamp': rows[1]['time_ns'] = rows[0]['time_ns']
            else: rows[1]['terminated'] = True
            with self.assertRaises(ValueError):
                FrameData(pa.Table.from_pylist(rows, schema=FRAME_SCHEMA), data.sources).validate()
        self.store.path(record['signals']).write_bytes(b'corrupt')
        with self.assertRaises(ValueError):
            from_release(self.store, key)

    def test_integer_outcome_flags_are_rejected_before_arrow_coercion(self):
        _, rows, record = fixture(self.store)
        for row in rows:
            row['success'] = 0
        out = io.BytesIO()
        pq.write_table(pa.Table.from_pylist(rows), out)
        record['signals'] = self.store.put(out.getvalue())
        key = release(self.store, [self.store.put(canonical(record))])
        with self.assertRaisesRegex(ValueError, 'boolean or unknown'):
            from_release(self.store, key)

    def test_metadata_budget_applies_before_signal_decode(self):
        key, _, _ = fixture(self.store)
        with patch('coppertop.analysis.MAX_METADATA_BYTES', 1):
            with self.assertRaisesRegex(ValueError, 'metadata byte bound'):
                from_release(self.store, key)


@unittest.skipUnless(HAS_ANALYSIS, 'Install requirements-analysis.txt for Lance/DataFusion integration')
class SnapshotTests(FixtureStore, unittest.TestCase):
    def snapshot(self):
        key, _, _ = fixture(self.store)
        data = from_release(self.store, key)
        checksum = write_snapshot(data, self.root / 'analysis', offsets=(-2, 0))
        return data, checksum, Snapshot(self.root / 'analysis', checksum)

    def test_lance_and_datafusion_match_arrow_filter_and_group_baseline(self):
        data, _, snapshot = self.snapshot()
        self.assertEqual(snapshot.table().to_pylist(), data.table.to_pylist())
        baseline = data.table.filter(pc.greater_equal(data.table['time_ns'], 100_000_000))
        result = snapshot.query('SELECT frame_id, state FROM frames WHERE time_ns >= 100000000 ORDER BY frame_index')
        self.assertEqual(result.to_pylist(), baseline.select(['frame_id', 'state']).to_pylist())
        counts = snapshot.query('SELECT split, COUNT(*) AS n, COUNT(episode_success) AS known FROM frames GROUP BY split').to_pylist()
        self.assertEqual(counts, [{'split': 'development', 'n': 4, 'known': 0}])

    def test_lance_exact_neighbors_match_numpy_and_preserve_dev_filter(self):
        data, _, snapshot = self.snapshot()
        query = np.asarray(data.table['state'][1].as_py(), dtype=np.float32)
        expected = sorted(float(np.square(np.asarray(r['state'], dtype=np.float32) - query).sum())
                          for r in data.table.to_pylist())
        result = snapshot.nearest(query, k=4, filter="split = 'development'")
        np.testing.assert_allclose(result['_distance'].to_pylist(), expected, rtol=1e-6, atol=1e-6)
        self.assertEqual(snapshot.nearest(query, filter="split = 'sealed_test'").num_rows, 0)
        for k in (0, True, 10001):
            with self.assertRaises(ValueError): snapshot.nearest(query, k=k)

    def test_sql_cannot_write_or_create_tables(self):
        _, _, snapshot = self.snapshot()
        for statement in ['DELETE FROM frames', 'CREATE TABLE bad AS SELECT * FROM frames',
                          f"COPY frames TO '{self.root}/forbidden.parquet' STORED AS PARQUET"]:
            with self.assertRaises(Exception): snapshot.query(statement)
        self.assertFalse((self.root / 'forbidden.parquet').exists())

    def test_snapshot_identity_tampering_and_overwrite_refused(self):
        data, checksum, snapshot = self.snapshot()
        with self.assertRaises(FileExistsError): write_snapshot(data, self.root / 'analysis')
        with self.assertRaisesRegex(ValueError, 'manifest hash'): Snapshot(self.root / 'analysis', '0' * 64)
        relative = next(iter(snapshot.manifest['files']))
        path = self.root / 'analysis' / relative
        path.write_bytes(b'corrupt')
        with self.assertRaisesRegex(ValueError, 'bytes differ'): Snapshot(self.root / 'analysis', checksum)

    def test_reader_remains_pinned_when_lance_has_a_newer_version(self):
        import lance
        data, checksum, snapshot = self.snapshot()
        changed = data.table.set_column(data.table.schema.get_field_index('outcome_scope'), 'outcome_scope', pa.array(['newer'] * 4))
        newer = lance.write_dataset(changed, self.root / 'analysis/frames.lance', mode='overwrite')
        self.assertGreater(newer.version, snapshot.manifest['lance_versions']['frames'])
        self.assertEqual(Snapshot(self.root / 'analysis', checksum).table().to_pylist(), data.table.to_pylist())

    def test_logical_content_identity_reproduces_despite_physical_lance_ids(self):
        data, _, a = self.snapshot()
        second = write_snapshot(data, self.root / 'second', offsets=(-2, 0))
        b = Snapshot(self.root / 'second', second)
        self.assertEqual(a.manifest['content_id'], b.manifest['content_id'])
        self.assertEqual(a.manifest['arrow_table_sha256'], b.manifest['arrow_table_sha256'])


if __name__ == '__main__':
    unittest.main()
