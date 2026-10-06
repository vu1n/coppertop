import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock
from coppertop.chords import LocalStore, canonical
from coppertop.cloud_stage import (StageManifest, stage_objects, verify_stage,
    commit_directory, manifest_for_directory, READY_FILE)
from coppertop.streams import ObjectRef


class StageContractTests(unittest.TestCase):
    def fixture(self, root):
        source = LocalStore(root/'source')
        payload = b'camera-and-native-signals'
        key = source.put(payload)
        manifest = StageManifest('run', 'a'*64, {'objects/'+key[:2]+'/'+key: ObjectRef(key,len(payload))})
        return source, manifest

    def test_stage_commits_after_complete_hash_verification(self):
        with tempfile.TemporaryDirectory() as d:
            root=Path(d);store,manifest=self.fixture(root);directory=root/'stage'
            def commit():verify_stage(manifest,directory)
            receipt=stage_objects(store,manifest,directory,max_seconds=10,commit=commit)
            self.assertEqual(receipt['manifest_key'],manifest.key)
            self.assertEqual(StageManifest.from_dict(manifest.to_dict()),manifest)
            verify_stage(manifest,directory)
            self.assertEqual(list(root.glob('**/*.partial')),[])

    def test_corrupt_source_cannot_produce_ready_marker_or_commit(self):
        with tempfile.TemporaryDirectory() as d:
            root=Path(d);store,manifest=self.fixture(root);proxy=Mock(wraps=store)
            proxy.iter_bytes.return_value=iter([b'wrong'])
            commit=Mock()
            with self.assertRaises(ValueError):stage_objects(proxy,manifest,root/'stage',max_seconds=10,commit=commit)
            commit.assert_not_called();self.assertFalse((root/'stage'/READY_FILE).exists())

    def test_failed_barrier_never_returns_ready_and_marker_removed(self):
        with tempfile.TemporaryDirectory() as d:
            root=Path(d);store,manifest=self.fixture(root)
            with self.assertRaises(RuntimeError):stage_objects(store,manifest,root/'stage',max_seconds=10,commit=Mock(side_effect=RuntimeError('commit failed')))
            self.assertFalse((root/'stage'/READY_FILE).exists())

    def test_path_and_metadata_bounds_precede_any_io(self):
        ref=ObjectRef('b'*64,1)
        for name in ('../escape','/escape','x//y','stage.json'):
            with self.subTest(name=name),self.assertRaises(ValueError):StageManifest('run','a'*64,{name:ref})
        with self.assertRaises(ValueError):StageManifest('../run','a'*64,{'x':ref})
        with self.assertRaises(ValueError):StageManifest('run','a'*64,{'x':ref,'x/y':ref})
        with self.assertRaises(ValueError):StageManifest('run','a'*64,{str(i):ref for i in range(65)})

    def test_manifest_is_immutable_and_order_independent(self):
        refs={'a':ObjectRef('a'*64,1),'b':ObjectRef('b'*64,2)}
        manifest=StageManifest('run','c'*64,refs);refs.clear()
        self.assertEqual(len(manifest.objects),2)
        with self.assertRaises(TypeError):manifest.objects['x']=ObjectRef('d'*64,1)
        self.assertEqual(manifest.key,StageManifest('run','c'*64,dict(reversed(list(manifest.objects.items())))).key)

    def test_tamper_after_commit_rejected(self):
        with tempfile.TemporaryDirectory() as d:
            root=Path(d);store,manifest=self.fixture(root)
            stage_objects(store,manifest,root/'stage',max_seconds=10,commit=lambda:None)
            name=next(iter(manifest.objects));(root/'stage'/name).write_bytes(b'corrupt')
            with self.assertRaises(ValueError):verify_stage(manifest,root/'stage')

    def test_symlink_escape_and_reusing_destination_rejected(self):
        with tempfile.TemporaryDirectory() as d:
            root=Path(d);store,manifest=self.fixture(root)
            (root/'outside').mkdir();(root/'alias').symlink_to(root/'outside',target_is_directory=True)
            with self.assertRaises(ValueError):manifest_for_directory(root/'alias','run','a'*64,['x'])
            (root/'stage').mkdir()
            with self.assertRaises(FileExistsError):stage_objects(store,manifest,root/'stage',max_seconds=10,commit=lambda:None)

    def test_seal_generated_payload_without_copy(self):
        with tempfile.TemporaryDirectory() as d:
            directory=Path(d);(directory/'head.safetensors').write_bytes(b'weights')
            manifest=manifest_for_directory(directory,'run','a'*64,['head.safetensors'])
            commit=Mock();receipt=commit_directory(manifest,directory,commit)
            commit.assert_called_once();verify_stage(manifest,directory)
            self.assertEqual(receipt['manifest_key'],manifest.key)

    def test_payload_partial_suffix_is_order_independent_and_receipt_subtree_reserved(self):
        with tempfile.TemporaryDirectory() as d:
            root=Path(d);source=LocalStore(root/'source');ka=source.put(b'a');kb=source.put(b'b')
            refs={'foo.partial':ObjectRef(ka,1),'foo':ObjectRef(kb,1)}
            first=StageManifest('run','a'*64,refs);second=StageManifest('run','a'*64,dict(reversed(list(refs.items()))))
            self.assertEqual(first.key,second.key)
            stage_objects(source,first,root/'first',max_seconds=10,commit=lambda:None)
            stage_objects(source,second,root/'second',max_seconds=10,commit=lambda:None)
            verify_stage(first,root/'first');verify_stage(second,root/'second')
        with self.assertRaises(ValueError):StageManifest('run','a'*64,{'stage.json/file':ObjectRef('b'*64,1)})

    def test_special_file_cannot_block_commit(self):
        import os
        with tempfile.TemporaryDirectory() as d:
            root=Path(d);os.mkfifo(root/'head')
            manifest=StageManifest('run','a'*64,{'head':ObjectRef('b'*64,1)})
            commit=Mock()
            with self.assertRaises(ValueError):commit_directory(manifest,root,commit)
            commit.assert_not_called()

    def test_deadline_store_blocks_stale_io_before_request(self):
        from unittest.mock import patch
        from coppertop.cloud_stage import DeadlineStore
        store=Mock()
        with patch('coppertop.cloud_stage.time.monotonic',side_effect=[0,11]):
            bounded=DeadlineStore(store,10)
            with self.assertRaises(TimeoutError):bounded.stat('a'*64)
        store.stat.assert_not_called()
