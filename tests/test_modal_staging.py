import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch
from types import SimpleNamespace

from coppertop.chords import LocalStore
from coppertop.cloud_stage import StageManifest, manifest_for_directory
from coppertop.streams import ObjectRef
from coppertop import modal_staging as module


class Volume:
    def __init__(self, calls=None, options=None):
        self.calls = calls if calls is not None else []
        self.options = options or {}
    def with_mount_options(self, **options):
        return Volume(self.calls, dict(self.options, **options))
    def reload(self):
        self.calls.append('reload')
    def commit(self):
        self.calls.append('commit')


class App:
    def __init__(self, name, **options):
        self.options = options
        self.definitions = {}
    def function(self, **options):
        def register(function):
            self.definitions[options['name']] = options
            return function
        return register


class ModalStagingTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name).resolve()
        self.inputs, self.outputs = Volume(), Volume()
        self.store = LocalStore(self.root/'r2')
        self.patch = patch.multiple(module, INPUT_MOUNT=str(self.root/'input'), OUTPUT_MOUNT=str(self.root/'output'))
        self.patch.start()
        self.addCleanup(self.patch.stop)
        self.env = patch.dict(os.environ, MODAL_TASK_ID='ta-training')
        self.env.start()
        self.addCleanup(self.env.stop)
        key = self.store.put(b'input')
        from coppertop.gpu_smoke import RELEASE
        self.manifest = StageManifest('test-run', RELEASE, {'objects/input': ObjectRef(key, 5)})
        self.invocations = []
        self.roles = []
        def runner(argv, **options):
            self.invocations.append((argv, options))
            target = Path(argv[-1])
            target.mkdir()
            target.joinpath('result.json').write_text(json.dumps({'status':'passed', 'fresh_process_reload_verified':True, 'velocity_exact':True}))
        self.options = dict(input_volume=self.inputs, output_volume=self.outputs, image=object(),
            store_factory=lambda role:(self.roles.append(role) or self.store), transfer_secrets={'input':[object()], 'output':[object()]}, region='us-east', run_id='test-run',
            train_command=lambda manifest, source, target:['python','-m','native_train',str(target)],
            reload_command=lambda original, checkpoint, source, delta, target:['python','-m','native_reload',str(target)],
            output_manifest_builder=lambda manifest, target:manifest_for_directory(target, manifest.run_id, manifest.release, ['result.json']),
            runner=runner)
        self.functions = module.create_functions(SimpleNamespace(App=App), **self.options)

    def test_definitions_have_no_gpu_secrets_retries_or_shared_container(self):
        self.assertEqual(self.functions.app.options['secrets'], [])
        for name, options in self.functions.app.definitions.items():
            self.assertTrue(options['single_use_containers'])
            self.assertEqual(options['retries'], 0)
            for volume in options['volumes'].values():
                self.assertEqual(volume.options['sub_path'], '/test-run')
            if name in ('coppertop-train', 'coppertop-reload'):
                self.assertTrue(options['block_network'])
                self.assertEqual(options['secrets'], [])
                self.assertTrue(options['volumes'][module.INPUT_MOUNT].options['read_only'])
            else:
                self.assertIsNone(options['gpu'])
                role = 'input' if name == 'coppertop-stage' else 'output'
                self.assertEqual(options['secrets'], self.options['transfer_secrets'][role])

    def test_stage_commits_before_train_and_generated_commit_has_no_bulk_copy(self):
        receipt = self.functions.stage(self.manifest.to_dict())
        self.assertEqual(self.inputs.calls, ['reload','commit'])
        self.assertEqual(receipt['status'], 'committed')
        result = self.functions.train(self.manifest.to_dict())
        self.assertEqual(self.outputs.calls, ['commit'])
        self.assertEqual(result['identity'], 'ta-training')
        output = StageManifest.from_dict(result['output_manifest'])
        self.assertTrue((self.root/'output'/output.key/'stage.json').is_file())
        self.assertFalse((self.root/'output'/'train').exists())
        self.assertEqual(self.invocations[0][1], {'check':True, 'timeout':480})

    def test_corrupt_stage_prevents_gpu_command(self):
        self.functions.stage(self.manifest.to_dict())
        (self.root/'input'/self.manifest.key/'objects/input').write_bytes(b'wrong')
        with self.assertRaises(ValueError):
            self.functions.train(self.manifest.to_dict())
        self.assertEqual(self.invocations, [])
        self.assertNotIn('commit', self.outputs.calls)

    def test_same_container_verifier_rejected_before_execution(self):
        with self.assertRaises(ValueError):
            self.functions.reload(self.manifest.to_dict(), self.manifest.to_dict(), 'ta-training')
        self.assertEqual(self.invocations, [])

    def test_foreign_run_rejected_before_transfer(self):
        foreign = StageManifest('another', self.manifest.release, self.manifest.objects)
        with self.assertRaises(ValueError):
            self.functions.stage(foreign.to_dict())
        self.assertEqual(self.inputs.calls, [])

    def test_invalid_limits_and_shared_volumes_rejected(self):
        for change in ({'process_seconds':600}, {'cpu_seconds':3601}, {'output_volume':self.inputs}, {'transfer_secrets':[]}, {'gpu_seconds':901}, {'startup_seconds':3601}):
            with self.subTest(change=change), self.assertRaises(ValueError):
                module.create_functions(SimpleNamespace(App=App), **dict(self.options, **change))

    def test_failed_commit_does_not_return_readiness(self):
        self.inputs.commit = lambda: (_ for _ in ()).throw(RuntimeError('commit failed'))
        with self.assertRaises(RuntimeError):
            self.functions.stage(self.manifest.to_dict())
        self.assertFalse((self.root/'input'/self.manifest.key/'stage.json').exists())
        self.assertEqual(self.invocations, [])

    def test_fresh_container_commits_then_cpu_publishes_report(self):
        self.functions.stage(self.manifest.to_dict())
        head = self.store.put(b'head'); case = self.store.put(b'case')
        checkpoint = StageManifest('test-run', self.manifest.release, {
            'head.safetensors': ObjectRef(head, 4), 'validation.safetensors': ObjectRef(case, 4),
            'head-manifest.json':ObjectRef(self.store.put(b'metadata'),8),
            'result.json':ObjectRef(self.store.put(b'result'),6)})
        self.functions.stage(checkpoint.to_dict())
        from coppertop.gpu_smoke import MODEL_REPO, MODEL_REVISION
        def runner(argv, **options):
            target = Path(argv[-1]); target.mkdir()
            target.joinpath('result.json').write_text(json.dumps({
                'status':'passed', 'release':checkpoint.release, 'cutoff':6,
                'kind':'native_head_fresh_process_reload', 'model_repo':MODEL_REPO, 'model_revision':MODEL_REVISION,
                'fresh_process_reload_verified':True, 'velocity_exact':True, 'velocity_max_abs_difference':0.0,
                'checkpoint_sha256':head, 'checkpoint_bytes':4, 'validation_sha256':case, 'validation_bytes':4}))
        functions = module.create_functions(SimpleNamespace(App=App), **dict(self.options, runner=runner))
        with patch.dict(os.environ, MODAL_TASK_ID='ta-verifier'):
            result = functions.reload(self.manifest.to_dict(), checkpoint.to_dict(), 'ta-training')
        self.assertEqual(result['identity'], 'ta-verifier')
        self.assertTrue(result['result']['velocity_exact'])
        self.assertEqual(self.outputs.calls, ['commit'])
        published = functions.publish_reload(result['report_manifest'], 'ta-verifier')
        self.assertEqual(published['report_key'], result['report_manifest']['objects']['result.json']['sha256'])
        self.assertEqual(self.roles, ['input', 'input', 'output'])
        with self.assertRaises(ValueError):
            functions.publish_reload(result['report_manifest'], 'ta-other')

    def test_process_timeout_prevents_output_commit(self):
        self.functions.stage(self.manifest.to_dict())
        import subprocess
        self.options['runner'] = lambda *args, **kwargs: (_ for _ in ()).throw(subprocess.TimeoutExpired('native', 480))
        functions = module.create_functions(SimpleNamespace(App=App), **self.options)
        with self.assertRaises(subprocess.TimeoutExpired):
            functions.train(self.manifest.to_dict())
        self.assertEqual(self.outputs.calls, [])

    def test_invalid_checkpoint_rejected_before_output_access(self):
        with self.assertRaises(ValueError):
            self.functions.publish(self.manifest.to_dict())
        with self.assertRaises(ValueError):
            self.functions.restage('a'*64, self.manifest.to_dict())
        self.assertEqual(self.roles, [])

class ModalStopVerifierTests(unittest.TestCase):
    def test_active_then_absent_with_scoped_read_only_command(self):
        now = [0]
        calls = []
        responses = [json.dumps([{'container_id':'ta-owned','app_id':'ap-owned','app_name':'test','start_time':'Pending'}]), '[]']
        def runner(argv, **options):
            calls.append((argv, options))
            return SimpleNamespace(stdout=responses.pop(0))
        verifier = module.stop_verifier('ap-owned', command=('python','-m','modal'), runner=runner,
            clock=lambda:now[0], sleep=lambda seconds:now.__setitem__(0, now[0]+seconds))
        self.assertIs(verifier('ta-owned'), True)
        self.assertEqual(len(calls), 2)
        self.assertEqual(calls[0][0], ['python','-m','modal','container','list','--app-id','ap-owned','--env','main','--json'])
        self.assertEqual(calls[0][1]['timeout'], 10)

    def test_malformed_out_of_scope_and_query_errors_fail_closed(self):
        cases = ['{}', json.dumps([{'container_id':'ta-owned','app_id':'ap-other','app_name':'test','start_time':'Pending'}]), 'x'*65537]
        for raw in cases:
            with self.subTest(raw=raw[:40]), self.assertRaises(ValueError):
                module.stop_verifier('ap-owned', runner=lambda *args, **kwargs:SimpleNamespace(stdout=raw))('ta-owned')
        import subprocess
        for error in (subprocess.CalledProcessError(1, ['modal']), subprocess.TimeoutExpired(['modal'], 10)):
            def failed(*args, **kwargs):
                raise error
            with self.subTest(error=type(error).__name__), self.assertRaises(type(error)):
                module.stop_verifier('ap-owned', runner=failed)('ta-owned')

    def test_active_deadline_is_bounded_and_invalid_ids_never_query(self):
        now = [0]
        raw = json.dumps([{'container_id':'ta-owned','app_id':'ap-owned','app_name':'test','start_time':'Pending'}])
        verifier = module.stop_verifier('ap-owned', max_seconds=2,
            runner=lambda *args, **kwargs:SimpleNamespace(stdout=raw), clock=lambda:now[0],
            sleep=lambda seconds:now.__setitem__(0, now[0]+seconds))
        with self.assertRaises(TimeoutError):
            verifier('ta-owned')
        with self.assertRaises(ValueError):
            verifier('sb-sandbox')
