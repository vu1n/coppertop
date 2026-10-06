import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock
from coppertop.chords import LocalStore, canonical
from coppertop.gpu_smoke import RELEASE, MODEL_REPO, MODEL_REVISION
from coppertop.smoke_artifacts import publish_smoke_checkpoint, fetch_smoke_checkpoint
from coppertop.streams import file_ref


class DurableSmokeTests(unittest.TestCase):
    def fixture(self, root):
        directory=root/'artifacts';directory.mkdir()
        (directory/'head.safetensors').write_bytes(b'head-weights')
        (directory/'validation.safetensors').write_bytes(b'validation-case')
        head=file_ref(directory/'head.safetensors');case=file_ref(directory/'validation.safetensors')
        fields={'release':RELEASE,'checkpoint_sha256':head.sha256,'checkpoint_bytes':head.size,
                'validation_sha256':case.sha256,'validation_bytes':case.size}
        result={**fields,'status':'passed'}
        manifest={**fields,'base_model':MODEL_REPO,'revision':MODEL_REVISION}
        (directory/'result.json').write_bytes(canonical(result))
        (directory/'head-manifest.json').write_bytes(canonical(manifest))
        return directory,result,manifest

    def test_durable_receipt_survives_source_loss_and_fetch_is_exact(self):
        with tempfile.TemporaryDirectory() as d:
            root=Path(d);directory,result,manifest=self.fixture(root);store=LocalStore(root/'durable')
            key=publish_smoke_checkpoint(store,directory,result,manifest,'run')
            expected={p.name:p.read_bytes() for p in directory.iterdir()}
            for p in directory.iterdir():p.unlink()
            receipt=fetch_smoke_checkpoint(store,key,root/'fetched','run')
            self.assertFalse(receipt['optimizer_state']);self.assertFalse(receipt['rng_state'])
            self.assertEqual(expected,{p.name:p.read_bytes() for p in (root/'fetched').iterdir()})

    def test_changed_local_head_rejected_before_any_remote_write(self):
        with tempfile.TemporaryDirectory() as d:
            root=Path(d);directory,result,manifest=self.fixture(root);store=Mock()
            (directory/'head.safetensors').write_bytes(b'bad')
            with self.assertRaises(ValueError):publish_smoke_checkpoint(store,directory,result,manifest,'run')
            store.put_stream.assert_not_called();store.put.assert_not_called()

    def test_wrong_run_or_oversized_reference_rejected_before_destination(self):
        with tempfile.TemporaryDirectory() as d:
            root=Path(d);directory,result,manifest=self.fixture(root);store=LocalStore(root/'durable')
            key=publish_smoke_checkpoint(store,directory,result,manifest,'run')
            with self.assertRaises(ValueError):fetch_smoke_checkpoint(store,key,root/'wrong','other')
            self.assertFalse((root/'wrong').exists())
            receipt=json.loads(store.get(key));receipt['artifacts']['validation.safetensors']['size']=65537
            bad=store.put(canonical(receipt))
            with self.assertRaises(ValueError):fetch_smoke_checkpoint(store,bad,root/'oversize','run')
            self.assertFalse((root/'oversize').exists())

    def test_corrupt_remote_head_never_becomes_validated_input(self):
        with tempfile.TemporaryDirectory() as d:
            root=Path(d);directory,result,manifest=self.fixture(root);store=LocalStore(root/'durable')
            key=publish_smoke_checkpoint(store,directory,result,manifest,'run')
            proxy=Mock(wraps=store)
            proxy.iter_bytes.side_effect=lambda digest,**kwargs: iter([b'x'*result['checkpoint_bytes']]) if digest==result['checkpoint_sha256'] else store.iter_bytes(digest,**kwargs)
            with self.assertRaises(ValueError):fetch_smoke_checkpoint(proxy,key,root/'fetched','run')
            self.assertFalse((root/'fetched/head.safetensors').exists())

    def test_failed_upload_never_commits_receipt(self):
        with tempfile.TemporaryDirectory() as d:
            directory,result,manifest=self.fixture(Path(d));store=Mock()
            store.put_stream.side_effect=OSError('storage unavailable')
            with self.assertRaises(OSError):publish_smoke_checkpoint(store,directory,result,manifest,'run')
            store.put.assert_not_called();self.assertTrue((directory/'head.safetensors').exists())


class MultipartPaginationBoundTests(unittest.TestCase):
    def test_invalid_remote_markers_stop_before_more_requests(self):
        from unittest.mock import patch
        from coppertop.s3 import S3Store
        from coppertop.streams import ObjectRef
        from coppertop.multipart import UploadInterrupted, MAX_PARTS
        for marker in (0, True, MAX_PARTS+1):
            with self.subTest(marker=marker):
                client=Mock();client.create_multipart_upload.return_value={'UploadId':'owned'}
                client.list_parts.return_value={'Parts':[],'IsTruncated':True,'NextPartNumberMarker':marker}
                store=S3Store(client,'test',prefix='chords/gpu-reliability/test')
                with patch.object(store,'stat',side_effect=FileNotFoundError):
                    with self.assertRaises(UploadInterrupted):store.multipart(ObjectRef('0'*64,5*1024**2),iter(()))
                self.assertEqual(client.list_parts.call_count,1);client.upload_part.assert_not_called()
                client.complete_multipart_upload.assert_not_called()

    def test_empty_progressing_pages_cannot_escape_marker_bound(self):
        from unittest.mock import patch
        from coppertop.s3 import S3Store
        from coppertop.streams import ObjectRef
        from coppertop.multipart import UploadInterrupted, MAX_PARTS
        client=Mock();client.create_multipart_upload.return_value={'UploadId':'owned'}
        client.list_parts.side_effect=[{'Parts':[],'IsTruncated':True,'NextPartNumberMarker':MAX_PARTS},
            {'Parts':[],'IsTruncated':True,'NextPartNumberMarker':MAX_PARTS+1}]
        store=S3Store(client,'test',prefix='chords/gpu-reliability/test')
        with patch.object(store,'stat',side_effect=FileNotFoundError):
            with self.assertRaises(UploadInterrupted):store.multipart(ObjectRef('0'*64,5*1024**2),iter(()))
        self.assertEqual(client.list_parts.call_count,2);client.upload_part.assert_not_called()
