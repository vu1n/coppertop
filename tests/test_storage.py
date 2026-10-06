import io
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch
from coppertop.chords import LocalStore, canonical, digest, fork, release
from coppertop.checkpoint import save_checkpoint, resume_checkpoint
from coppertop.publication import publish_release, release_objects
from coppertop.s3 import R2Config, S3Store


class S3Error(Exception):
    def __init__(self, code):
        self.response = {'Error': {'Code': code}}


class FakeS3:
    def __init__(self):
        self.objects = {}
        self.writes = []
        self.fail_key = None

    def put_object(self, *, Bucket, Key, Body, IfNoneMatch, ContentType):
        assert IfNoneMatch == '*'
        if Key == self.fail_key:
            raise S3Error('ServiceUnavailable')
        if (Bucket, Key) in self.objects:
            raise S3Error('PreconditionFailed')
        self.objects[Bucket, Key] = Body
        self.writes.append(Key)
        return {}

    def head_object(self, *, Bucket, Key):
        if (Bucket, Key) not in self.objects:
            raise S3Error('NoSuchKey')
        return {'ContentLength': len(self.objects[Bucket, Key])}

    def get_object(self, *, Bucket, Key, Range=None):
        if (Bucket, Key) not in self.objects:
            raise S3Error('NoSuchKey')
        data = self.objects[Bucket, Key]
        result = {}
        if Range:
            start, end = map(int, Range.removeprefix('bytes=').split('-'))
            result['ContentRange'] = f'bytes {start}-{end}/{len(data)}'
            data = data[start:end+1]
        return {**result, 'Body': io.BytesIO(data), 'ContentLength': len(data)}


class StorageTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.store = LocalStore(Path(self.tmp.name) / 'source')
        self.raw = self.store.put(b'original video bytes')
        self.signals = self.store.put(b'parquet placeholder')
        self.episode = self.store.put(canonical({'schema_version': 1, 'signals': self.signals,
            'raw': {'video': {'object': self.raw}}, 'videos': {'camera': {'object': self.raw}}}))
        self.base = release(self.store, [self.episode])
        self.client = FakeS3()
        self.target = S3Store(self.client, 'test')

    def test_atomic_failure_cleanup_and_retry(self):
        data = b'new bytes'
        with patch('coppertop.chords.os.link', side_effect=OSError('interrupted')):
            with self.assertRaises(OSError):
                self.store.put(data)
        self.assertFalse(self.store.path(digest(data)).exists())
        self.assertEqual(list(self.store.root.rglob('.staging-*')), [])
        self.assertEqual(self.store.get(self.store.put(data)), data)

    def test_readers_see_complete_object_and_idempotent_retry(self):
        data = b'new bytes'
        real_link = os.link
        def link(source, destination):
            self.assertFalse(destination.exists())
            self.assertEqual(Path(source).read_bytes(), data)
            real_link(source, destination)
            self.assertEqual(destination.read_bytes(), data)
        with patch('coppertop.chords.os.link', side_effect=link):
            key = self.store.put(data)
        self.assertEqual(key, self.store.put(data))

    def test_manifest_last_and_thin_fork(self):
        publish_release(self.store, self.target, self.base)
        self.assertEqual(self.client.writes[-1], self.target.key(self.base))
        self.assertEqual(len(self.client.writes), 4)
        overlay = self.store.put(canonical({'success': None}))
        child = fork(self.store, self.base, overlays=(overlay,))
        publish_release(self.store, self.target, child)
        self.assertEqual(self.client.writes[-2:], [self.target.key(overlay), self.target.key(child)])
        count = len(self.client.writes)
        publish_release(self.store, self.target, child)
        self.assertEqual(len(self.client.writes), count)

    def test_missing_source_preflight_writes_nothing(self):
        self.store.path(self.raw).unlink()
        with self.assertRaises(FileNotFoundError):
            publish_release(self.store, self.target, self.base)
        self.assertEqual(self.client.writes, [])

    def test_partial_publication_retry(self):
        self.client.fail_key = self.target.key(self.episode)
        with self.assertRaises(S3Error):
            publish_release(self.store, self.target, self.base)
        with self.assertRaises(FileNotFoundError):
            self.target.get(self.base)
        written = list(self.client.writes)
        self.client.fail_key = None
        publish_release(self.store, self.target, self.base)
        self.assertEqual(self.client.writes[:len(written)], written)
        self.assertEqual(len(self.client.writes), len(set(self.client.writes)))
        self.assertEqual(release_objects(self.target, self.base)[-1], self.base)

    def test_corrupt_destination_fails_closed(self):
        self.client.objects['test', self.target.key(self.raw)] = b'corrupt'
        with self.assertRaises(ValueError):
            publish_release(self.store, self.target, self.base)
        with self.assertRaises(FileNotFoundError):
            self.target.get(self.base)

    def test_conditional_put_and_errors(self):
        key = self.target.put(b'bytes')
        self.assertEqual(self.target.put(b'bytes'), key)
        self.client.objects['test', self.target.key(key)] = b'corrupt'
        with self.assertRaises(ValueError):
            self.target.put(b'bytes')
        self.client.fail_key = self.target.key(digest(b'other'))
        with self.assertRaises(S3Error):
            self.target.put(b'other')

    def test_config_is_explicit_and_credential_free(self):
        config = R2Config.from_mapping({'R2_ENDPOINT': 'https://example.r2.cloudflarestorage.com',
            'R2_BUCKET': 'bucket', 'AWS_SECRET_ACCESS_KEY': 'unused sentinel'})
        self.assertEqual(config.bind(self.client).bucket, 'bucket')
        for endpoint in ('http://example.com', 'https://user:password@example.com', 'https://example.com/path'):
            with self.assertRaises(ValueError):
                R2Config(endpoint, 'bucket')
        for prefix in ('../objects', '/root', 'a//b'):
            with self.assertRaises(ValueError):
                S3Store(self.client, 'bucket', prefix=prefix)

    def test_checkpoint_resume_identity_and_corruption(self):
        first = save_checkpoint(self.store, self.base, 'run-1', 1, b'opaque weights')
        second = save_checkpoint(self.store, self.base, 'run-1', 2, b'next weights', previous=first)
        manifest, payload = resume_checkpoint(self.store, second, self.base, 'run-1')
        self.assertEqual((manifest['step'], payload, manifest['previous']), (2, b'next weights', first))
        self.assertEqual(second, save_checkpoint(self.store, self.base, 'run-1', 2, b'next weights', previous=first))
        with self.assertRaises(ValueError):
            resume_checkpoint(self.store, second, self.base, 'other-run')
        with self.assertRaises(ValueError):
            save_checkpoint(self.store, self.base, 'run-1', 1, b'bad', previous=first)
        self.store.path(manifest['payload']).write_bytes(b'bad')
        with self.assertRaises(ValueError):
            resume_checkpoint(self.store, second, self.base, 'run-1')

    def test_worker_bootstrap_and_checkpoint_subprocess(self):
        key = save_checkpoint(self.store, self.base, 'run', 0, b'opaque')
        command = [sys.executable, '-m', 'coppertop.worker', '--store', str(self.store.root),
                   '--release', self.base, '--checkpoint', key, '--run-id', 'run']
        result = subprocess.run(command, capture_output=True, text=True, check=True)
        self.assertIn('"checkpoint_bytes": 6', result.stdout)
        self.assertIn('"step": 0', result.stdout)
        mismatch = release(self.store, [], parent=None)
        with self.assertRaises(ValueError):
            resume_checkpoint(self.store, key, mismatch, 'run')
        command[-1] = 'other-run'
        self.assertNotEqual(subprocess.run(command, capture_output=True).returncode, 0)

    def test_conflicting_manifest_role_rejected(self):
        key = release(self.store, [self.episode], overlays=(self.episode,))
        with self.assertRaises(ValueError):
            release_objects(self.store, key)


if __name__ == '__main__':
    unittest.main()
