import io
import tempfile
import unittest
from types import SimpleNamespace
from pathlib import Path
from unittest.mock import Mock, patch

from coppertop.chords import LocalStore, canonical, digest
from coppertop.streams import ObjectRef, file_ref
from coppertop.runpod_staging import PART_BYTES, RunpodStager, RunpodVolumeConfig, seal_runpod_output, seal_runpod_stage


class Client:
    def __init__(self):
        self.meta = SimpleNamespace(endpoint_url='https://s3api-us-ks-2.runpod.io/', region_name='US-KS-2',
                                    config=SimpleNamespace(connect_timeout=10, read_timeout=30,
                                                           retries={'total_max_attempts': 1}))
        self.objects = {}
        self.parts = {}
        self.writes = []
        self.aborted = False

    def put_object(self, *, Bucket, Key, Body):
        self.objects[Key] = Body
        self.writes.append(Key)

    def get_object(self, *, Bucket, Key):
        return {'Body': io.BytesIO(self.objects[Key])}

    def head_object(self, *, Bucket, Key):
        return {'ContentLength': len(self.objects[Key])}

    def create_multipart_upload(self, **kwargs):
        return {'UploadId': 'one'}

    def upload_part(self, *, PartNumber, Body, **kwargs):
        self.parts[PartNumber] = Body
        return {'ETag': str(PartNumber)}

    def complete_multipart_upload(self, *, Key, **kwargs):
        self.objects[Key] = b''.join(self.parts.values())
        self.writes.append(Key)

    def abort_multipart_upload(self, **kwargs):
        self.aborted = True


class RunpodStagingTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name).resolve()
        self.source = LocalStore(self.root / 'source')
        self.client = Client()
        self.config = RunpodVolumeConfig('volume-1', 'US-KS-2', 'https://s3api-us-ks-2.runpod.io/')
        self.stager = RunpodStager(self.config, self.client)

    def manifest(self):
        from coppertop.cloud_stage import StageManifest
        data = b'canonical checkpoint'
        key = self.source.put(data)
        return StageManifest('run-one', 'a' * 64, {'head.safetensors': ObjectRef(key, len(data))})

    def test_upload_marker_last_and_same_datacenter_readiness(self):
        manifest = self.manifest()
        receipt = self.stager.stage(self.source, manifest, self.root / 'scratch', max_seconds=30)
        self.assertTrue(self.client.writes[-1].endswith('/stage.json'))
        path = self.stager.require_ready(manifest, receipt, pod_datacenter='US-KS-2', max_seconds=30)
        self.assertEqual(path, f'/workspace/coppertop/stages/{manifest.key}')
        with self.assertRaises(ValueError):
            self.stager.require_ready(manifest, receipt, pod_datacenter='US-CA-2', max_seconds=30)

    def test_corruption_blocks_gpu_readiness(self):
        manifest = self.manifest()
        receipt = self.stager.stage(self.source, manifest, self.root / 'scratch', max_seconds=30)
        self.client.objects[f'coppertop/stages/{manifest.key}/head.safetensors'] = b'bad'
        with self.assertRaises(ValueError):
            self.stager.require_ready(manifest, receipt, pod_datacenter='US-KS-2', max_seconds=30)

    def test_failed_upload_never_publishes_ready_marker(self):
        manifest = self.manifest()
        with patch.object(self.client, 'put_object', side_effect=RuntimeError('upload failure')):
            with self.assertRaises(RuntimeError):
                self.stager.stage(self.source, manifest, self.root / 'scratch', max_seconds=30)
        self.assertFalse(any(key.endswith('/stage.json') for key in self.client.objects))

    def test_multipart_stream_verified_and_aborts_corruption(self):
        data = b'x' * (PART_BYTES + 1)
        ref = ObjectRef(digest(data), len(data))
        chunks = (data[i:i + 262144] for i in range(0, len(data), 262144))
        self.stager._upload('payload', ref, chunks, self.stager._deadline(30))
        self.assertEqual(self.client.objects['payload'], data)
        self.assertEqual(len(self.client.parts), 2)
        with self.assertRaises(ValueError):
            self.stager._upload('bad', ref, (b'bad',), self.stager._deadline(30))
        self.assertTrue(self.client.aborted)
        self.assertNotIn('bad', self.client.objects)

    def test_expired_transfer_and_nonfinite_deadline(self):
        with self.assertRaises(TimeoutError):
            self.stager._verify('anything', ObjectRef('a' * 64, 0), 0)
        for limit in (0, -1, float('nan'), float('inf'), True, 3601):
            with self.assertRaises(ValueError):
                self.stager._deadline(limit)

    def test_client_binding_blocks_ambient_region_or_retries(self):
        self.client.meta.region_name = 'US-CA-2'
        with self.assertRaises(ValueError):
            RunpodStager(self.config, self.client)
        self.client.meta.region_name = 'US-KS-2'
        self.client.meta.config.retries = {'total_max_attempts': 3}
        with self.assertRaises(ValueError):
            RunpodStager(self.config, self.client)

    def test_endpoint_and_mount_are_explicit(self):
        for dc, endpoint, mount in (('OTHER', self.config.endpoint, '/workspace'),
                                    ('US-KS-2', 'https://evil.invalid/', '/workspace'),
                                    ('US-KS-2', self.config.endpoint, '/tmp')):
            with self.assertRaises(ValueError):
                RunpodVolumeConfig('volume', dc, endpoint, mount)


    def output_fixture(self):
        from coppertop.cloud_stage import manifest_for_directory
        from coppertop.gpu_smoke import RELEASE, MODEL_REPO, MODEL_REVISION
        directory = self.root / 'gpu-output'
        directory.mkdir()
        (directory/'head.safetensors').write_bytes(b'weights')
        (directory/'validation.safetensors').write_bytes(b'case')
        head = file_ref(directory/'head.safetensors')
        case = file_ref(directory/'validation.safetensors')
        fields = {'release': RELEASE, 'checkpoint_sha256': head.sha256, 'checkpoint_bytes': head.size,
                  'validation_sha256': case.sha256, 'validation_bytes': case.size}
        (directory/'result.json').write_bytes(canonical(dict(fields, status='passed')))
        (directory/'head-manifest.json').write_bytes(canonical(dict(fields, base_model=MODEL_REPO,
                                                                    revision=MODEL_REVISION)))
        manifest = manifest_for_directory(directory, 'run-one', RELEASE,
            ('head.safetensors', 'validation.safetensors', 'head-manifest.json', 'result.json'))
        seal_runpod_output(manifest, directory)
        for path in directory.iterdir():
            self.client.objects[f'coppertop/runs/run-one/train/{path.name}'] = path.read_bytes()
        return manifest

    def test_cpu_publish_r2_and_separate_reload_stage(self):
        manifest = self.output_fixture()
        r2 = LocalStore(self.root/'r2')
        published = self.stager.publish_output(r2, manifest, self.root/'publish', run_id='run-one', max_seconds=30)
        counted = Mock(wraps=r2)
        receipt = self.stager.restage_checkpoint(counted, published['checkpoint_key'], manifest,
            self.root/'reload', run_id='run-one', max_seconds=30)
        for name in ('head.safetensors', 'validation.safetensors'):
            key = manifest.objects[name].sha256
            self.assertEqual(sum(call.args[0] == key for call in counted.iter_bytes.call_args_list), 1)
        self.assertEqual(receipt['manifest_key'], manifest.key)
        self.assertNotEqual(receipt['directory'], 'coppertop/runs/run-one/train')
        self.assertTrue(receipt['directory'].startswith('coppertop/stages/'))
        import json
        durable = json.loads(r2.get(published['checkpoint_key']))
        self.assertEqual(durable['kind'], 'native_head_weights')
        self.assertEqual(durable['run_id'], 'run-one')
        self.assertFalse(durable['optimizer_state'])
        self.stager.require_ready(manifest, receipt, pod_datacenter='US-KS-2', max_seconds=30)

    def test_output_run_id_size_and_corruption_block_publication(self):
        manifest = self.output_fixture()
        r2 = LocalStore(self.root/'r2')
        with self.assertRaises(ValueError):
            self.stager.publish_output(r2, manifest, self.root/'wrong-run', run_id='other', max_seconds=30)
        key = 'coppertop/runs/run-one/train/head.safetensors'
        self.client.objects[key] = b'wrong length'
        with self.assertRaises(ValueError):
            self.stager.publish_output(r2, manifest, self.root/'wrong-size', run_id='run-one', max_seconds=30)
        self.assertFalse(r2.root.exists())
        self.client.objects[key] = b'xxxxxxx'
        with self.assertRaises(ValueError):
            self.stager.publish_output(r2, manifest, self.root/'bad-hash', run_id='run-one', max_seconds=30)
        self.assertFalse(r2.root.exists())

    def test_wrong_gpu_marker_blocks_publication(self):
        manifest = self.output_fixture()
        self.client.objects['coppertop/runs/run-one/train/stage.json'] = b'bad'
        r2 = LocalStore(self.root/'r2')
        with self.assertRaises(ValueError):
            self.stager.publish_output(r2, manifest, self.root/'publish', run_id='run-one', max_seconds=30)
        self.assertFalse((self.root/'publish').exists())
        self.assertFalse(r2.root.exists())


    def report_fixture(self, checkpoint, overrides=None):
        from coppertop.cloud_stage import manifest_for_directory
        from coppertop.gpu_smoke import MODEL_REPO, MODEL_REVISION
        directory = self.root/'gpu-reload'
        directory.mkdir()
        head = checkpoint.objects['head.safetensors']
        case = checkpoint.objects['validation.safetensors']
        result = {'status': 'passed', 'release': checkpoint.release, 'run_id': checkpoint.run_id,
                  'kind': 'native_head_fresh_process_reload', 'model_repo': MODEL_REPO,
                  'model_revision': MODEL_REVISION, 'cutoff': 6,
                  'container_id': 'fresh-pod', 'training_container_id': 'train-pod',
                  'checkpoint_sha256': head.sha256, 'checkpoint_bytes': head.size,
                  'validation_sha256': case.sha256, 'validation_bytes': case.size,
                  'fresh_process_reload_verified': True, 'velocity_exact': True,
                  'velocity_max_abs_difference': 0.0}
        result.update(overrides or {})
        (directory/'result.json').write_bytes(canonical(result))
        manifest = manifest_for_directory(directory, checkpoint.run_id, checkpoint.release, ('result.json',))
        seal_runpod_stage(manifest, directory)
        for path in directory.iterdir():
            self.client.objects[f'coppertop/runs/run-one/reload/{path.name}'] = path.read_bytes()
        return manifest

    def test_publish_fresh_report_exact_r2_object(self):
        checkpoint = self.output_fixture()
        report = self.report_fixture(checkpoint)
        r2 = LocalStore(self.root/'r2')
        published = self.stager.publish_reload(r2, report, checkpoint, self.root/'report-cpu',
            container_id='fresh-pod', training_container_id='train-pod', max_seconds=30)
        self.assertEqual(published['report_key'], report.objects['result.json'].sha256)
        self.assertEqual(r2.get(published['report_key']),
            self.client.objects['coppertop/runs/run-one/reload/result.json'])

    def test_report_corruption_and_reused_pod_block_r2_write(self):
        checkpoint = self.output_fixture()
        report = self.report_fixture(checkpoint)
        r2 = LocalStore(self.root/'r2')
        with self.assertRaises(ValueError):
            self.stager.publish_reload(r2, report, checkpoint, self.root/'reused',
                container_id='train-pod', training_container_id='train-pod', max_seconds=30)
        self.client.objects['coppertop/runs/run-one/reload/result.json'] = b'bad'
        with self.assertRaises(ValueError):
            self.stager.publish_reload(r2, report, checkpoint, self.root/'corrupt',
                container_id='fresh-pod', training_container_id='train-pod', max_seconds=30)
        self.assertFalse(r2.root.exists())


    def test_valid_checksum_failed_native_comparison_blocks_report_publish(self):
        checkpoint = self.output_fixture()
        report = self.report_fixture(checkpoint, {'fresh_process_reload_verified': False})
        r2 = LocalStore(self.root/'r2')
        with self.assertRaises(ValueError):
            self.stager.publish_reload(r2, report, checkpoint, self.root/'failed-comparison',
                container_id='fresh-pod', training_container_id='train-pod', max_seconds=30)
        self.assertFalse(r2.root.exists())


if __name__ == '__main__':
    unittest.main()
