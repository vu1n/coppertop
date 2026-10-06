import hashlib
import io
import tempfile
import tracemalloc
import unittest
from pathlib import Path
from unittest.mock import patch
from coppertop.chords import LocalStore, digest
from coppertop.streams import ObjectRef, CHUNK_BYTES, checked
from coppertop.multipart import UploadState, UploadInterrupted, MIN_PART_BYTES
from coppertop.s3 import S3Store
from coppertop.checkpoint import save_checkpoint_stream, checkpoint_reference
from coppertop.worker import Job, validate_job
from coppertop.chords import release, canonical
from coppertop.publication import publish_release
from test_storage import FakeS3, S3Error


def generated(size):
    chunk = b'a' * CHUNK_BYTES
    while size:
        count = min(size, len(chunk))
        yield chunk[:count]
        size -= count


def reference(size):
    h = hashlib.sha256()
    for chunk in generated(size):
        h.update(chunk)
    return ObjectRef(h.hexdigest(), size)


class GeneratedBody:
    def __init__(self, size):
        self.remaining = size
        self.closed = False

    def read(self, count):
        assert 0 < count <= CHUNK_BYTES
        n = min(self.remaining, count)
        self.remaining -= n
        return b'a' * n

    def close(self):
        self.closed = True


class MultipartS3(FakeS3):
    def __init__(self):
        super().__init__()
        self.sessions = {}
        self.complete = {}
        self.sent = []
        self.fail_part = None
        self.completions = 0
        self.last_body = None

    def create_multipart_upload(self, *, Bucket, Key):
        upload_id = str(len(self.sessions) + 1)
        self.sessions[upload_id] = {'destination': (Bucket, Key), 'parts': {}}
        return {'UploadId': upload_id}

    def upload_part(self, *, Bucket, Key, UploadId, PartNumber, Body):
        if PartNumber == self.fail_part:
            raise S3Error('ServiceUnavailable')
        assert len(Body) <= MIN_PART_BYTES
        assert Body == b'a' * len(Body)
        sha = digest(Body)
        self.sessions[UploadId]['parts'][PartNumber] = {'PartNumber': PartNumber,
            'Size': len(Body), 'ETag': sha}
        self.sent.append(PartNumber)
        return {'ETag': sha}

    def list_parts(self, *, Bucket, Key, UploadId, PartNumberMarker):
        return {'Parts': list(self.sessions[UploadId]['parts'].values()), 'IsTruncated': False}

    def complete_multipart_upload(self, *, Bucket, Key, UploadId, MultipartUpload):
        session = self.sessions[UploadId]
        numbers = [p['PartNumber'] for p in MultipartUpload['Parts']]
        assert numbers == sorted(session['parts'])
        self.complete[Bucket, Key] = sum(session['parts'][n]['Size'] for n in numbers)
        self.completions += 1
        return {}

    def head_object(self, *, Bucket, Key):
        if (Bucket, Key) in self.complete:
            return {'ContentLength': self.complete[Bucket, Key]}
        return super().head_object(Bucket=Bucket, Key=Key)

    def get_object(self, *, Bucket, Key, Range=None):
        if (Bucket, Key) not in self.complete:
            return super().get_object(Bucket=Bucket, Key=Key, Range=Range)
        size = self.complete[Bucket, Key]
        result = {}
        count = size
        if Range:
            start, end = map(int, Range.removeprefix('bytes=').split('-'))
            count = end-start+1
            result['ContentRange'] = f'bytes {start}-{end}/{size}'
        self.last_body = GeneratedBody(count)
        return {**result, 'Body': self.last_body, 'ContentLength': count}

    def abort_multipart_upload(self, *, Bucket, Key, UploadId):
        del self.sessions[UploadId]


class StreamingTests(unittest.TestCase):
    def setUp(self):
        self.client = MultipartS3()
        self.store = S3Store(self.client, 'test')
        self.ref = reference(2 * MIN_PART_BYTES + 23)

    def test_interrupt_resume_reuses_receipts(self):
        saved = []
        self.client.fail_part = 2
        with self.assertRaises(UploadInterrupted) as caught:
            self.store.multipart(self.ref, generated(self.ref.size), persist=saved.append)
        self.assertEqual(self.client.completions, 0)
        with self.assertRaises(FileNotFoundError):
            self.store.stat(self.ref.sha256)
        state = UploadState.from_dict(saved[-1])
        self.assertEqual(len(state.parts), 1)
        self.client.fail_part = None
        self.store.multipart(self.ref, generated(self.ref.size), state=state)
        self.assertEqual(self.client.sent, [1, 2, 3])
        self.assertEqual(self.store.multipart(self.ref, generated(self.ref.size), state=state), self.ref)
        self.assertEqual(self.client.completions, 1)

    def test_hash_and_truncation_fail_before_completion(self):
        for ref, chunks in ((ObjectRef('0'*64, self.ref.size), generated(self.ref.size)),
                            (self.ref, generated(self.ref.size - 1))):
            with self.assertRaises(UploadInterrupted):
                self.store.multipart(ref, chunks)
        self.assertEqual(self.client.completions, 0)
        self.assertEqual(self.client.complete, {})

    def test_missing_remote_part_and_state_binding_fail(self):
        self.client.fail_part = 2
        with self.assertRaises(UploadInterrupted) as caught:
            self.store.multipart(self.ref, generated(self.ref.size))
        state = caught.exception.state
        self.client.sessions[state.upload_id]['parts'].clear()
        self.client.fail_part = None
        with self.assertRaises(UploadInterrupted):
            self.store.multipart(self.ref, generated(self.ref.size), state=state)
        with self.assertRaises(ValueError):
            S3Store(self.client, 'other').multipart(self.ref, generated(self.ref.size), state=state)
        self.assertEqual(self.client.completions, 0)
        self.store.abort(state)
        self.assertNotIn(state.upload_id, self.client.sessions)

    def test_bounded_generated_large_transfer_and_range(self):
        ref = reference(64 * 1024 * 1024)
        tracemalloc.start()
        try:
            self.store.put_stream(ref, generated(ref.size))
            _, peak = tracemalloc.get_traced_memory()
        finally:
            tracemalloc.stop()
        self.assertLess(peak, 24 * 1024 * 1024)
        self.assertEqual(b''.join(self.store.iter_bytes(ref.sha256, offset=17, length=29)), b'a'*29)
        self.assertTrue(self.client.last_body.closed)
        with self.assertRaises(ValueError):
            self.store.get(ref.sha256)

    def test_stream_bounds_and_range_response(self):
        with self.assertRaises(ValueError):
            list(checked((b'a'*(CHUNK_BYTES+1),), reference(CHUNK_BYTES+1)))
        key = self.store.put(b'small')
        with patch.object(self.client, 'get_object', return_value={'Body': io.BytesIO(b'sm'), 'ContentLength': 5}):
            with self.assertRaises(ValueError):
                self.store.get(key)
        with patch.object(self.client, 'get_object', return_value={'Body': io.BytesIO(b'm'), 'ContentLength': 1, 'ContentRange': 'wrong'}):
            with self.assertRaises(ValueError):
                list(self.store.iter_bytes(key, offset=1, length=1))

    def test_local_failed_stream_never_visible(self):
        with tempfile.TemporaryDirectory() as directory:
            store = LocalStore(Path(directory))
            with self.assertRaises(ValueError):
                store.put_stream(self.ref, generated(self.ref.size-1))
            self.assertFalse(store.path(self.ref.sha256).exists())
            self.assertEqual(list(store.root.rglob('.staging-*')), [])
            small = reference(29)
            store.put_stream(small, generated(small.size))
            self.assertEqual(b''.join(store.iter_bytes(small.sha256, offset=3, length=4)), b'aaaa')

    def test_receipt_persistence_loss_reuploads_same_number(self):
        saved = []
        def persist(snapshot):
            if snapshot['parts']:
                raise OSError('receipt store unavailable')
            saved.append(snapshot)
        with self.assertRaises(UploadInterrupted):
            self.store.multipart(self.ref, generated(self.ref.size), persist=persist)
        state = UploadState.from_dict(saved[-1])
        self.store.multipart(self.ref, generated(self.ref.size), state=state)
        self.assertEqual(self.client.sent, [1, 1, 2, 3])

    def test_lost_completion_response_retry_verifies_existing(self):
        complete = self.client.complete_multipart_upload
        def lost(**options):
            complete(**options)
            raise OSError('response lost')
        with patch.object(self.client, 'complete_multipart_upload', side_effect=lost):
            with self.assertRaises(UploadInterrupted) as caught:
                self.store.multipart(self.ref, generated(self.ref.size))
        self.assertEqual(self.store.multipart(self.ref, generated(self.ref.size),
                         state=caught.exception.state), self.ref)
        self.assertEqual(self.client.completions, 1)

    def test_release_streams_cloud_to_cloud_and_commits_last(self):
        self.store.put_stream(self.ref, generated(self.ref.size))
        episode = self.store.put(canonical({'schema_version': 1, 'signals': self.ref.sha256,
            'raw': {}, 'videos': {'camera': {'object': self.ref.sha256}}}))
        base = release(self.store, [episode])
        target_client = MultipartS3()
        target = S3Store(target_client, 'replica')
        target_client.fail_part = 2
        with self.assertRaises(UploadInterrupted):
            publish_release(self.store, target, base)
        with self.assertRaises(FileNotFoundError):
            target.stat(base)
        target_client.fail_part = None
        with patch.object(self.store, 'get', side_effect=AssertionError('no whole-object reads')):
            publish_release(self.store, target, base)
        self.assertEqual(target_client.writes[-1], target.key(base))

    def test_cloud_job_checkpoint_is_reference_only(self):
        base = release(self.store, [])
        checkpoint = save_checkpoint_stream(self.store, base, 'run', 1, self.ref, generated(self.ref.size))
        manifest, ref = checkpoint_reference(self.store, checkpoint, base, 'run')
        self.assertEqual(ref, self.ref)
        result = validate_job(self.store, Job(base, checkpoint, 'run'))
        self.assertEqual(result['payload_ref'], self.ref.to_dict())
        self.client.complete['test', self.store.key(ref.sha256)] -= 1
        with self.assertRaises(ValueError):
            checkpoint_reference(self.store, checkpoint, base, 'run')


if __name__ == '__main__':
    unittest.main()
