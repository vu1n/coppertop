import tempfile
import unittest
import inspect
import json
import hashlib
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import coppertop
from coppertop import modal_image as module
from coppertop.chords import LocalStore
from coppertop.cloud_stage import StageManifest, stage_objects
from coppertop.streams import ObjectRef
from coppertop.modal_image import (prepare_image, create_image_gate, RECIPE_FILES,
    SEED_IMAGE_ID)


class Image:
    def __init__(self):
        self.calls = []
    def add_local_file(self, source, target, **options):
        self.calls.append(('copy', str(source), target, options))
        return self
    def env(self, values):
        self.calls.append(('env', values))
        return self
    def workdir(self, path):
        self.calls.append(('workdir', path))
        return self
    def run_function(self, function, **options):
        self.calls.append(('build', function, options))
        return self


class App:
    def function(self, **options):
        self.options = options
        return lambda function: function


class Volume:
    def with_mount_options(self, **options):
        self.options = options
        return self


class ModalImageTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        for name in RECIPE_FILES:
            path = self.root/name
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text('reviewed fixture\n')
        self.sdk_lock = ('boto3==1.43.108\nbotocore==1.43.108\ns3transfer==0.19.2\njmespath==1.1.0\n'
            'python-dateutil==2.9.0.post0\nurllib3==2.8.0\nsix==1.17.0\n')
        (self.root/'requirements-r2.txt').write_text(self.sdk_lock)
        (self.root/'requirements-gpu-smoke.lock').write_text('accelerate==1.11.0\nurllib3==2.8.0\n')
        (self.root/'containers/gpu-smoke-seed-runtime.json').write_text(json.dumps({
            'schema_version': 1, 'seed_image_id': SEED_IMAGE_ID,
            'evidence_kind': 'historical_seed_install_log',
            'versions': {'accelerate': '1.15.0', 'urllib3': '2.5.0'}}))
        evaluator = self.root/module.EVALUATOR_FILE
        evaluator.parent.mkdir(parents=True)
        evaluator.write_bytes(b'pinned evaluator fixture')
        raw = evaluator.read_bytes()
        (self.root/'containers/gpu-smoke-pins.json').write_text(json.dumps({
            'evaluator_git_blob': hashlib.sha1(b'blob '+str(len(raw)).encode()+b'\0'+raw).hexdigest()}))
        self.seed = Image()
        self.sdk = SimpleNamespace(__version__='1.6.1')

    def prepare(self):
        return prepare_image(self.sdk, seed=self.seed, seed_id=SEED_IMAGE_ID,
                             root=self.root, region='us-east')

    def test_lazy_recipe_copies_only_allowlist_and_has_bounded_cpu_build(self):
        (self.root/'.env').write_text('excluded')
        (self.root/'coppertop/unreviewed.py').write_text('excluded')
        prepared = self.prepare()
        copies = [call for call in self.seed.calls if call[0] == 'copy']
        self.assertEqual(len(copies), len(RECIPE_FILES) + 2)
        self.assertTrue(all(call[-1] == {'copy': True} for call in copies))
        self.assertFalse(any('unreviewed.py' in call[1] or '.env' in call[1] for call in copies))
        build = self.seed.calls[-1]
        self.assertEqual(build[2], {'cpu': (4, 4), 'memory': (16384, 16384), 'timeout': 600,
            'gpu': None, 'secrets': [], 'region': 'us-east', 'include_source': False})
        self.assertIs(prepared.image, self.seed)

    def test_new_source_r2_pins_and_tool_bytes_change_recipe_identity(self):
        for name in ('coppertop/modal_staging.py', 'requirements-r2.txt', 'tools/run_reload_only.py'):
            before = self.prepare().recipe_sha256
            (self.root/name).write_text((self.root/name).read_text() + '\n# different reviewed bytes\n')
            self.assertNotEqual(before, self.prepare().recipe_sha256)

    def test_build_aligns_both_locks_without_reinstalling_cuda_or_model(self):
        stale = self.root/'coppertop/old_job.py'
        stale.write_text('stale')
        outside = self.root/'model.safetensors'
        outside.write_text('native seed data')
        cache = self.root/'coppertop/__pycache__'
        cache.mkdir()
        (cache/'modal_staging.cpython-311.pyc').write_bytes(b'stale bytecode')
        (cache/'outside-link').symlink_to(outside)
        real_path = Path
        def mapped_path(value):
            return self.root if value == '/app' else real_path(value)
        with patch.object(module, 'Path', side_effect=mapped_path), \
             patch.dict(module.os.environ, {'PIP_EXTRA_INDEX_URL': 'https://fixture.invalid',
                 'PIP_USER': '1', 'PYTHONHOME': '/fixture', 'PYTHONPATH': '/fixture'}), \
             patch.object(module, 'installed_dependencies', return_value={}), \
             patch.object(module, 'require_dependencies') as require, \
             patch.object(module, 'require_native_interpreter'), \
             patch.object(module, 'prepare_cached_layout') as layout, \
             patch.object(module.subprocess, 'run') as run:
            module._install_runtime()
        self.assertFalse(stale.exists())
        self.assertTrue(outside.exists())
        self.assertFalse(cache.exists())
        layout.assert_called_once_with()
        self.assertEqual(len(run.call_args_list), 2)
        install = run.call_args_list[0]
        self.assertIn('--no-deps', install.args[0])
        self.assertIn('--isolated', install.args[0])
        self.assertEqual(install.kwargs['env']['PIP_CONFIG_FILE'], module.os.devnull)
        self.assertNotIn('PYTHONHOME', install.kwargs['env'])
        self.assertNotIn('PYTHONPATH', install.kwargs['env'])
        self.assertNotIn('PIP_EXTRA_INDEX_URL', install.kwargs['env'])
        self.assertNotIn('PIP_USER', install.kwargs['env'])
        self.assertIn('--only-binary=:all:', install.args[0])
        self.assertEqual(install.kwargs['timeout'], 480)
        self.assertIn(str(self.root/'requirements-gpu-smoke.lock'), install.args[0])
        self.assertIn(str(self.root/'requirements-r2.txt'), install.args[0])
        self.assertEqual(require.call_args.args[0]['accelerate'], '1.11.0')
        self.assertFalse({'torch', 'torchvision', 'flash-attn'} & require.call_args.args[0].keys())

    def test_pinned_sdk_signatures_accept_recipe_and_gate_options_offline(self):
        try:
            import modal
        except ImportError:
            self.skipTest('Pinned optional controller SDK unavailable')
        if modal.__version__ != '1.6.1':
            self.skipTest('Pinned optional controller SDK unavailable')
        prepared = self.prepare()
        build = self.seed.calls[-1]
        inspect.signature(modal.Image.run_function).bind(None, build[1], **build[2])
        from modal._functions import _Function
        with patch.object(_Function, '_from_local', wraps=_Function._from_local) as define:
            modal.Image.debian_slim().run_function(build[1], **build[2])
        definition = define.call_args.kwargs
        self.assertTrue(definition['is_builder_function'])
        self.assertEqual(definition['cpu'], (4, 4))
        self.assertEqual(definition['memory'], (16384, 16384))
        from modal._resources import convert_fn_config_to_resources_config
        resources = convert_fn_config_to_resources_config(cpu=definition['cpu'], memory=definition['memory'], gpu=definition['gpu'])
        self.assertEqual(resources.milli_cpu, 4000)
        self.assertEqual(resources.milli_cpu_max, 4000)
        self.assertEqual(resources.memory_mb, 16384)
        self.assertEqual(resources.memory_mb_max, 16384)
        app = App()
        create_image_gate(app, prepared=prepared, input_volume=Volume(), run_id='run', attempt_id='attempt', region='us-east')
        inspect.signature(modal.App.function).bind(None, **app.options)

    def test_wrong_sdk_seed_missing_file_and_symlink_stop_before_definition(self):
        for sdk, seed_id in ((SimpleNamespace(__version__='1.4.3'), SEED_IMAGE_ID),
                             (self.sdk, 'im-other')):
            with self.assertRaises(ValueError):
                prepare_image(sdk, seed=self.seed, seed_id=seed_id, root=self.root, region='us-east')
        path = self.root/RECIPE_FILES[0]
        path.unlink()
        with self.assertRaises(ValueError):
            self.prepare()
        path.symlink_to(self.root/RECIPE_FILES[1])
        with self.assertRaises(ValueError):
            self.prepare()
        self.assertEqual(self.seed.calls, [])

    def test_gate_limits_and_readonly_run_mount(self):
        app, volume = App(), Volume()
        gate = create_image_gate(app, prepared=self.prepare(), input_volume=volume,
                                run_id='run-one', attempt_id='attempt-two', region='us-east')
        self.assertTrue(callable(gate))
        self.assertEqual(volume.options, {'sub_path': '/attempt-two', 'read_only': True})
        self.assertEqual(app.options['cpu'], (4, 4))
        self.assertEqual(app.options['memory'], (16384, 16384))
        for key, expected in {'gpu': None, 'timeout': 1200, 'startup_timeout': 300,
            'retries': 0, 'secrets': [], 'block_network': True, 'single_use_containers': True,
            'min_containers': 0, 'max_containers': 1, 'buffer_containers': 0}.items():
            self.assertEqual(app.options[key], expected)

    def test_recipe_mutation_and_foreign_mount_run_rejected(self):
        prepared = self.prepare()
        prepared.recipe['region'] = 'elsewhere'
        with self.assertRaises(ValueError):
            create_image_gate(App(), prepared=prepared, input_volume=Volume(), run_id='run', attempt_id='attempt', region='us-east')
        with self.assertRaises(ValueError):
            create_image_gate(App(), prepared=self.prepare(), input_volume=Volume(), run_id='../escape', attempt_id='attempt', region='us-east')

    def test_gate_verifies_source_before_processor_and_returns_bounded_provenance(self):
        prepared = self.prepare()
        store = LocalStore(self.root/'store')
        key = store.put(b'signal')
        manifest = StageManifest('run-one', 'a'*64, {'signal': ObjectRef(key, 6)})
        mount = self.root/'inputs'
        stage_objects(store, manifest, mount/manifest.key, max_seconds=10, commit=lambda: None)
        calls = []
        def validator(args):
            calls.append(args)
            return {'status': 'passed', 'image_id': args.image_id,
                'recipe_sha256': prepared.recipe['native_recipe_sha256'],
                'snapshot_verified': True, 'imports_verified': True,
                'processor_parity_verified': True, 'entrypoint_verified': True,
                'full_model_instantiated': False, 'cuda_executed': False}
        gate = create_image_gate(App(), prepared=prepared, input_volume=Volume(),
            run_id='run-one', attempt_id='attempt-two', region='us-east', validator=validator)
        real_path = Path
        def mapped_path(value):
            if value == '/app':
                return self.root
            if value == '/opt/gpu-smoke-pins.json':
                return self.root/'containers/gpu-smoke-pins.json'
            if value == '/opt/evaluator.py':
                return self.root/module.EVALUATOR_FILE
            return real_path(value)
        with patch.object(module, 'Path', side_effect=mapped_path), \
             patch.object(module, 'INPUT_MOUNT', str(mount)), \
             patch.dict(module.os.environ, MODAL_TASK_ID='ta-gate'), \
             patch.object(module, 'require_native_interpreter'), \
             patch('coppertop.image_check.importlib.metadata.version',
                 side_effect=lambda name: prepared.recipe['dependency_alignment']['expected'][name]), \
             patch.object(coppertop, 'reload_launch', SimpleNamespace(), create=True), \
             patch.object(coppertop, 'modal_reload', SimpleNamespace(), create=True), \
             patch.object(coppertop, 'r2_session', SimpleNamespace(), create=True), \
             patch.dict('sys.modules', {name: SimpleNamespace() for name in ('boto3', 'botocore', 's3transfer')}):
            result = gate(manifest.to_dict(), 'im-reviewed')
            self.assertEqual(result['container_id'], 'ta-gate')
            self.assertEqual(result['attempt_id'], 'attempt-two')
            self.assertEqual(result['recipe_sha256'], prepared.recipe_sha256)
            self.assertEqual(calls[0].store, mount/manifest.key)
            self.assertEqual(calls[0].model, Path('/opt/model'))
            (mount/manifest.key/'signal').write_bytes(b'broken')
            with self.assertRaises(ValueError):
                gate(manifest.to_dict(), 'im-reviewed')
            self.assertEqual(len(calls), 1)

    def test_offline_recipe_reports_all_historical_differences_and_unknowns(self):
        report = self.prepare().recipe['dependency_alignment']['historical_seed_report']
        rows = {row['package']: row for row in report['differences']}
        self.assertEqual(rows['accelerate']['observed'], '1.15.0')
        self.assertEqual(rows['urllib3']['observed'], '2.5.0')
        self.assertEqual(rows['boto3']['status'], 'unknown')
        self.assertEqual(report['package_count'], 8)
        self.assertEqual(len(rows), 8)

    def test_conflicting_pins_and_cuda_install_targets_fail_before_sdk_definition(self):
        (self.root/'requirements-gpu-smoke.lock').write_text('urllib3==2.5.0\n')
        with self.assertRaisesRegex(ValueError, 'Conflicting dependency pins'):
            self.prepare()
        self.assertEqual(self.seed.calls, [])
        (self.root/'requirements-gpu-smoke.lock').write_text('torch==2.8.0\n')
        with self.assertRaisesRegex(ValueError, 'Cached CUDA stack'):
            self.prepare()
        self.assertEqual(self.seed.calls, [])

    def test_inventory_must_describe_the_pinned_seed(self):
        path = self.root/'containers/gpu-smoke-seed-runtime.json'
        value = json.loads(path.read_text())
        value['seed_image_id'] = 'im-other'
        path.write_text(json.dumps(value))
        with self.assertRaisesRegex(ValueError, 'exact seed'):
            self.prepare()
        self.assertEqual(self.seed.calls, [])

    def test_foreign_interpreter_stops_before_install_or_package_cleanup(self):
        with patch.object(module, 'Path', side_effect=lambda value: self.root if value == '/app' else Path(value)), \
             patch.object(module, 'require_native_interpreter', side_effect=ValueError('different interpreter')), \
             patch.object(module.subprocess, 'run') as run, \
             patch.object(module, 'installed_dependencies') as versions:
            with self.assertRaisesRegex(ValueError, 'different interpreter'):
                module._install_runtime()
        run.assert_not_called()
        versions.assert_not_called()

    def test_missing_or_changed_evaluator_stops_before_provider_definition(self):
        path = self.root/module.EVALUATOR_FILE
        path.write_bytes(b'changed')
        with self.assertRaisesRegex(ValueError, 'evaluator bytes differ'):
            self.prepare()
        path.unlink()
        with self.assertRaisesRegex(ValueError, 'evaluator required'):
            self.prepare()
        self.assertEqual(self.seed.calls, [])

    def test_cached_model_alias_and_wheel_integrity_have_no_download_fallback(self):
        work, opt = self.root/'work', self.root/'opt'
        (work/'model').mkdir(parents=True)
        opt.mkdir()
        index = work/'model/model.safetensors.index.json'
        index.write_text('{}')
        wheel = work/'pinned.whl'
        wheel.write_bytes(b'cached wheel')
        (opt/'gpu-smoke-pins.json').write_text(json.dumps({'flash_attention_wheel': {
            'name': wheel.name, 'size': wheel.stat().st_size,
            'digest': 'sha256:'+hashlib.sha256(wheel.read_bytes()).hexdigest()}}))
        real_path = Path
        def mapped(value):
            if str(value).startswith(('/work','/opt')):
                return self.root/str(value).lstrip('/')
            return real_path(value)
        with patch.object(module, 'Path', side_effect=mapped):
            module.prepare_cached_layout()
            self.assertEqual((opt/'model').resolve(), (work/'model').resolve())
            module.prepare_cached_layout()  # Reusing the exact alias is safe.
            wheel.write_bytes(b'bad bytes!!!')
            with self.assertRaisesRegex(ValueError, 'Cached wheel checksum'):
                module.prepare_cached_layout()
            index.unlink()
            with self.assertRaisesRegex(ValueError, 'Warm checkpoint cache absent'):
                module.prepare_cached_layout()
            index.write_text('{}')
            (opt/'model').unlink()
            (opt/'model').mkdir()
            with self.assertRaisesRegex(ValueError, 'model path differs'):
                module.prepare_cached_layout()


if __name__ == '__main__':
    unittest.main()
