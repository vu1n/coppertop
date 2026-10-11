import ast
import builtins
from dataclasses import fields
import hashlib
import importlib.util
import inspect
import io
import json
import os
from pathlib import Path
import py_compile
import sys
import tempfile
from types import ModuleType, SimpleNamespace
import unittest
from unittest.mock import patch

from coppertop import clef_cache, colocation_stage as stage, colocation_worker as worker


class ClefReferenceIdentityTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.model = Path(self.directory.name)
        self.source = self.model/'joint_schema_model.py'
        self.fixture = b'#' + b'x'*(worker.REFERENCE_BYTES-2) + b'\n'

    def reference_fixture(self, source):
        raw = source.encode()
        self.assertLess(len(raw), worker.REFERENCE_BYTES-1)
        return raw + b'#'*(worker.REFERENCE_BYTES-len(raw)-1) + b'\n'

    def load_fixture(self, raw):
        self.source.write_bytes(raw)
        with patch.object(worker, 'REFERENCE_SHA256', hashlib.sha256(raw).hexdigest()):
            module = worker.load_reference_code(self.model)
        self.addCleanup(sys.modules.pop, module.__name__, None)
        return module

    def test_reference_pins_match_immutable_recovery_metadata(self):
        root = Path(__file__).resolve().parents[1]
        release = json.loads((root/'manifests/clef-recovery/model-release.json').read_bytes())
        reference = release['reference_implementation']
        self.assertEqual(reference['path'], self.source.name)
        self.assertEqual(reference['sha256'], worker.REFERENCE_SHA256)
        row = next(item for item in release['files'] if item['path'] == reference['path'])
        self.assertEqual(row['bytes'], worker.REFERENCE_BYTES)

    def test_exact_fixture_hash_is_required_even_at_correct_size(self):
        self.source.write_bytes(self.fixture)
        with self.assertRaisesRegex(ValueError, 'Pinned Clef reference bytes required'):
            worker.require_reference_code(self.model)
        # A harmless synthetic source stands in for authenticated public bytes;
        # the real model/reference implementation is never present or executed.
        with patch.object(worker, 'REFERENCE_SHA256', hashlib.sha256(self.fixture).hexdigest()):
            self.assertEqual(worker.require_reference_code(self.model), self.fixture)
            self.source.write_bytes(b'!' + self.fixture[1:])
            with self.assertRaises(ValueError):
                worker.require_reference_code(self.model)

    def test_read_is_bounded_and_wrong_sizes_fail_before_hashing(self):
        class BoundedSource(io.BytesIO):
            def read(inner, size=-1):
                self.assertEqual(size, worker.REFERENCE_BYTES+1)
                return super().read(size)

        for raw in (b'', self.fixture[:-1], self.fixture+b'x'*10000):
            with self.subTest(size=len(raw)), patch.object(Path, 'open', return_value=BoundedSource(raw)), \
                    patch.object(worker.hashlib, 'sha256') as hasher:
                with self.assertRaises(ValueError):
                    worker.require_reference_code(self.model)
                hasher.assert_not_called()

    def forbid_runtime_imports(self):
        original = builtins.__import__
        attempted = []

        def checked(name, *args, **kwargs):
            if name.split('.')[0] in {'numpy', 'torch', 'transformers', 'PIL', 'joint_schema_model'}:
                attempted.append(name)
                raise AssertionError('Model runtime import attempted')
            return original(name, *args, **kwargs)

        return attempted, checked

    def test_worker_rejects_reference_before_runtime_or_model_import(self):
        self.source.write_bytes(self.fixture)
        attempted, checked = self.forbid_runtime_imports()
        with patch.object(worker, 'CLEF_MODEL', self.model), patch('builtins.__import__', checked):
            with self.assertRaisesRegex(ValueError, 'Pinned Clef reference bytes required'):
                worker.serve_worker('clef')
        self.assertEqual(attempted, [])

    def test_actual_builder_script_rejects_reference_before_runtime_import(self):
        # Extract the actual generated validation script without invoking build,
        # subprocesses, installation, download, or the model runtime.
        tree = ast.parse(inspect.getsource(clef_cache.build))
        script = next(node.value.value for node in ast.walk(tree)
                      if isinstance(node, ast.Assign) and isinstance(node.value, ast.Constant)
                      and any(isinstance(target, ast.Name) and target.id == 'check' for target in node.targets))
        self.source.write_bytes(self.fixture)
        load = worker.load_reference_code
        attempted, checked = self.forbid_runtime_imports()
        with patch.object(worker, 'load_reference_code', side_effect=lambda _path: load(self.model)) as guard, \
                patch('builtins.__import__', checked):
            with self.assertRaisesRegex(ValueError, 'Pinned Clef reference bytes required'):
                exec(compile(script, '<cache-validation-fixture>', 'exec'), {})
        guard.assert_called_once_with('/opt/coppertop-clef/clef-model')
        self.assertEqual(attempted, [])

    def test_authenticated_bytes_ignore_sibling_package_and_cached_module(self):
        package = self.model/'joint_schema_model'
        package.mkdir()
        (package/'__init__.py').write_text("raise AssertionError('unauthenticated package executed')\n")
        cached = ModuleType('joint_schema_model')
        cached.marker = 'unauthenticated cached module'
        original_path = list(sys.path)
        with patch.dict(sys.modules, {'joint_schema_model': cached}):
            module = self.load_fixture(self.reference_fixture("marker = 'authenticated source'\n"))
            self.assertEqual(module.marker, 'authenticated source')
            self.assertIs(sys.modules['joint_schema_model'], cached)
        self.assertEqual(sys.path, original_path)

    def test_authenticated_source_ignores_valid_timestamp_bytecode(self):
        authenticated = self.reference_fixture("marker = 'authenticated source'\n")
        self.source.write_bytes(self.reference_fixture("marker = 'unauthenticated bytecode'\n"))
        timestamp = int(self.source.stat().st_mtime)
        py_compile.compile(str(self.source), doraise=True,
            invalidation_mode=py_compile.PycInvalidationMode.TIMESTAMP)
        self.source.write_bytes(authenticated)
        os.utime(self.source, (timestamp, timestamp))
        # Confirm the harmless cache is usable by a normal Python source loader.
        spec = importlib.util.spec_from_file_location('fixture_cached_reference', self.source)
        cached = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(cached)
        self.assertEqual(cached.marker, 'unauthenticated bytecode')
        with patch.object(worker, 'REFERENCE_SHA256', hashlib.sha256(authenticated).hexdigest()):
            module = worker.load_reference_code(self.model)
        self.addCleanup(sys.modules.pop, module.__name__, None)
        self.assertEqual(module.marker, 'authenticated source')

    def test_loader_uses_the_verified_read_even_if_path_is_replaced_after_authentication(self):
        authenticated = self.reference_fixture("marker = 'authenticated source'\n")
        self.source.write_bytes(authenticated)
        verify = worker.require_reference_code

        def replace_after_read(directory):
            raw = verify(directory)
            self.source.write_bytes(self.reference_fixture("marker = 'replacement source'\n"))
            return raw

        with patch.object(worker, 'REFERENCE_SHA256', hashlib.sha256(authenticated).hexdigest()), \
             patch.object(worker, 'require_reference_code', side_effect=replace_after_read):
            module = worker.load_reference_code(self.model)
        self.addCleanup(sys.modules.pop, module.__name__, None)
        self.assertEqual(module.marker, 'authenticated source')

    def test_fresh_module_identity_supports_dataclass_annotations(self):
        raw = self.reference_fixture("from __future__ import annotations\n"
            "from dataclasses import dataclass\n@dataclass\nclass Record:\n    marker: str = 'authenticated'\n")
        first, second = self.load_fixture(raw), self.load_fixture(raw)
        self.assertNotEqual(first.__name__, second.__name__)
        for module in (first, second):
            self.assertIs(sys.modules[module.__name__], module)
            self.assertEqual(module.Record().marker, 'authenticated')
            self.assertEqual(fields(module.Record)[0].name, 'marker')

    def test_failed_execution_removes_only_its_own_module_identity(self):
        unrelated_name = '_coppertop_clef_reference_fixture_other_owner'
        unrelated = ModuleType(unrelated_name)
        with patch.dict(sys.modules, {unrelated_name: unrelated}):
            before = {name for name in sys.modules if name.startswith('_coppertop_clef_reference_')}
            with self.assertRaisesRegex(RuntimeError, 'authenticated fixture failure'):
                self.load_fixture(self.reference_fixture("raise RuntimeError('authenticated fixture failure')\n"))
            after = {name for name in sys.modules if name.startswith('_coppertop_clef_reference_')}
            self.assertEqual(after, before)
            self.assertIs(sys.modules[unrelated_name], unrelated)

    def test_actual_worker_executes_authenticated_source_with_stdout_on_stderr(self):
        raw = self.reference_fixture("print('authenticated reference fixture initialized')\n"
            "def load_release_model(*args, **kwargs):\n"
            "    raise RuntimeError('authenticated fixture model loader reached')\n"
            "def systemone(*args, **kwargs):\n    return None\n")
        self.source.write_bytes(raw)
        package = self.model/'joint_schema_model'
        package.mkdir()
        (package/'__init__.py').write_text("raise AssertionError('unauthenticated package executed')\n")
        fake_torch = ModuleType('torch')
        fake_torch.set_grad_enabled = lambda _enabled: None
        fake_torch.__version__ = 'fixture'
        fake_torch.version = SimpleNamespace(cuda='fixture')
        fake_torch.cuda = SimpleNamespace(get_device_name=lambda: 'fixture', mem_get_info=lambda: (0, 0))
        fake_pil = ModuleType('PIL')
        fake_pil.Image = object()
        stdout, stderr = io.StringIO(), io.StringIO()
        load = worker.load_reference_code

        def owned_load(directory):
            module = load(directory)
            self.addCleanup(sys.modules.pop, module.__name__, None)
            return module

        with patch.object(worker, 'CLEF_MODEL', self.model), patch.object(worker, 'ROOT', self.model), \
             patch.object(worker, 'REFERENCE_SHA256', hashlib.sha256(raw).hexdigest()), \
             patch.object(worker, 'load_reference_code', side_effect=owned_load) as loader, \
             patch.dict(sys.modules, {'torch': fake_torch, 'numpy': ModuleType('numpy'), 'PIL': fake_pil}), \
             patch('sys.stdout', stdout), patch('sys.stderr', stderr):
            with self.assertRaisesRegex(RuntimeError, 'authenticated fixture model loader reached'):
                worker.serve_worker('clef')
        loader.assert_called_once_with(self.model)
        self.assertEqual(stdout.getvalue(), '')
        self.assertEqual(stderr.getvalue(), 'authenticated reference fixture initialized\n')

    def test_cpu_gate_uses_shared_loader_before_runtime_imports(self):
        self.source.write_bytes(self.fixture)
        attempted, checked = self.forbid_runtime_imports()
        with patch.object(stage, 'CLEF_MODEL', self.model), \
             patch.object(stage, 'load_reference_code', wraps=worker.load_reference_code) as loader, \
             patch('builtins.__import__', checked):
            with self.assertRaisesRegex(ValueError, 'Pinned Clef reference bytes required'):
                stage.cpu_gate()
        loader.assert_called_once_with(self.model)
        self.assertEqual(attempted, [])


if __name__ == '__main__':
    unittest.main()
