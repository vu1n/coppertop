"""Explicit local real-data integration test; no dataset acquisition."""
import io
import json
import os
import tempfile
import unittest
from pathlib import Path
import pyarrow.parquet as pq
from coppertop.chords import LocalStore, canonical, digest, release
from coppertop.ingest import ingest
from coppertop.lerobot import export


@unittest.skipUnless(os.environ.get('COPPERTOP_SOURCE'), 'Set COPPERTOP_SOURCE to the already acquired source')
class RoundTripTests(unittest.TestCase):
    def test_export_rejects_unknown_camera_before_writing(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            store = LocalStore(root / 'store')
            record = store.put(canonical({'videos': {'../escape': {}}}))
            key = release(store, [record])
            with self.assertRaisesRegex(ValueError, 'camera names'):
                export(store, key, root / 'out')
            self.assertFalse((root / 'out').exists())

    def test_real_import_and_reproducible_exports(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            store = LocalStore(root / 'store')
            key = ingest(Path(os.environ['COPPERTOP_SOURCE']), store)
            self.assertEqual(key, ingest(Path(os.environ['COPPERTOP_SOURCE']), store))
            manifest = json.loads(store.get(key))
            record = json.loads(store.get(manifest['episodes'][0]))
            table = pq.read_table(io.BytesIO(store.get(record['signals'])))
            self.assertEqual(len(set(table['frame_id'].to_pylist())), 347)
            self.assertEqual(len(record['raw']), 13)
            annotation = json.loads(store.get(manifest['overlays'][0]))
            self.assertEqual(annotation['split'], 'development')
            self.assertIsNone(annotation['quality']['success'])
            self.assertIsNone(annotation['quality']['recovery'])
            export(store, key, root / 'a')
            export(store, key, root / 'b')
            files = lambda path: {str(p.relative_to(path)): digest(p.read_bytes()) for p in path.rglob('*') if p.is_file()}
            self.assertEqual(files(root / 'a'), files(root / 'b'))
            original = pq.read_table(io.BytesIO(store.get(record['raw']['official-episode/data/chunk-000/episode_000000.parquet']['object'])))
            result = pq.read_table(root / 'a/data/chunk-000/episode_000000.parquet')
            self.assertEqual(original.to_pylist(), result.to_pylist())
            for camera, video in record['videos'].items():
                out = root / f'a/videos/chunk-000/observation.images.{camera}/episode_000000.mp4'
                self.assertEqual(digest(out.read_bytes()), video['object'])
            with self.assertRaises(FileExistsError):
                export(store, key, root / 'a')


if __name__ == '__main__':
    unittest.main()
