import json
import tempfile
import unittest
from pathlib import Path
from coppertop.adapter import load_official
from coppertop.chords import LocalStore, canonical, fork, frame_id, release, window_indices
from coppertop.episode import initialization_group, reorder_action


class ChordsTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.store = LocalStore(Path(self.tmp.name))

    def test_objects_are_content_addressed(self):
        key = self.store.put(b'raw')
        self.assertEqual(key, self.store.put(b'raw'))
        self.assertEqual(self.store.get(key), b'raw')
        self.store.path(key).write_bytes(b'corrupt')
        with self.assertRaises(ValueError):
            self.store.get(key)

    def test_path_keys_are_bounded(self):
        for key in ('../secrets', '/etc/passwd', 'a'*63):
            with self.assertRaises(ValueError):
                self.store.get(key)

    def test_forks_share_base_objects(self):
        episode = self.store.put(canonical({'episode_id': 'e'}))
        base = release(self.store, [episode])
        overlay = self.store.put(canonical({'episode_id': 'e', 'success': None}))
        child = fork(self.store, base, overlays=(overlay,))
        manifest = json.loads(self.store.get(child))
        self.assertEqual(manifest['episodes'], [episode])
        self.assertEqual(manifest['parent'], base)
        self.assertEqual(manifest['overlays'], [overlay])
        self.assertEqual(release(self.store, [episode]), base)

    def test_missing_and_duplicate_references_rejected(self):
        key = self.store.put(b'episode')
        with self.assertRaises(ValueError):
            release(self.store, [key, key])
        with self.assertRaises(FileNotFoundError):
            release(self.store, ['0'*64])

    def test_stable_frame_identity(self):
        self.assertEqual(frame_id('group@revision', 0), frame_id('group@revision', 0))
        self.assertNotEqual(frame_id('group@revision', 0), frame_id('group@revision', 1))
        self.assertNotEqual(frame_id('group@revision', 0), frame_id('group@other', 0))
        with self.assertRaises(ValueError):
            frame_id('group@revision', -1)

    def test_group_identity_excludes_revision(self):
        self.assertEqual(initialization_group('repo', 'dataset', 0), initialization_group('repo', 'dataset', 0))
        self.assertNotEqual(initialization_group('repo', 'dataset', 0), initialization_group('repo', 'dataset', 1))

    def test_unpinned_source_rejected_before_decoding(self):
        source = Path(self.tmp.name) / 'input'
        (source / 'sources').mkdir(parents=True)
        (source / 'sources/official-files.json').write_text('{"files": []}')
        with self.assertRaisesRegex(ValueError, 'pinned official source'):
            load_official(source)

    def test_action_semantics(self):
        source = tuple(range(12))
        native = reorder_action(source, 'lerobot', 'native')
        self.assertEqual(native, (5., 6., 7., 8., 9., 10., 11., 0., 1., 2., 3., 4.))
        self.assertEqual(reorder_action(native, 'native', 'lerobot'), source)
        with self.assertRaises(ValueError):
            reorder_action(source, 'unknown', 'native')

    def test_boundary_masks(self):
        self.assertEqual(window_indices(0, 3, (-2, 0, 1, 3)), ((0, 0, 1, 2), (False, True, True, False)))
        self.assertEqual(window_indices(2, 3, (0, 1)), ((2, 2), (True, False)))
        with self.assertRaises(ValueError):
            window_indices(3, 3, (0,))


if __name__ == '__main__':
    unittest.main()
