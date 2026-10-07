import importlib.metadata
import json
import subprocess
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch
from packaging.requirements import Requirement
from packaging.utils import canonicalize_name

from coppertop import dependency_gate, performance_runtime as runtime
from coppertop.performance_cloud import simulator_preflight
from coppertop.chords import canonical
from coppertop.image_check import pinned_dependencies

ROOT = Path(__file__).resolve().parents[1]


class DependencyGateTests(unittest.TestCase):
    def test_complete_simulator_baseline_includes_transitives_and_inherited_cpu_stack(self):
        pins = pinned_dependencies(ROOT/'requirements-simulator.lock')
        self.assertEqual(len(pins), 129)
        self.assertEqual(pins['idna'], '3.10')
        self.assertEqual(pins['setuptools'], '65.5.0')
        self.assertEqual(pins['torch'], '2.7.1+cpu')
        self.assertEqual(pins['torchvision'], '0.22.1+cpu')
        self.assertEqual(pins['transformers'], '4.57.1')
        self.assertEqual(pins['gym'], '0.26.2')
        self.assertEqual(pins['evdev'], '2.0.0')

    def test_pip_conflict_stops_admission_without_leaking_manager_output(self):
        runner = Mock(return_value=SimpleNamespace(returncode=1))
        with patch.dict(dependency_gate.os.environ,
                        {'PIP_EXTRA_INDEX_URL': 'sensitive', 'PYTHONHOME': 'different'}):
            with self.assertRaisesRegex(ValueError, 'closure failed pip check'):
                dependency_gate.require_pip_check(runner)
        options = runner.call_args.kwargs
        self.assertEqual(options['stdout'], subprocess.DEVNULL)
        self.assertEqual(options['stderr'], subprocess.DEVNULL)
        self.assertNotIn('PIP_EXTRA_INDEX_URL', options['env'])
        self.assertNotIn('PYTHONHOME', options['env'])
        self.assertEqual(options['timeout'], 60)

    def test_official_metadata_active_closure_is_satisfied_by_the_entire_lock(self):
        audit = json.loads((ROOT/'containers/simulator-dependency-audit.json').read_text())
        pins = pinned_dependencies(ROOT/'requirements-simulator.lock')
        packages = audit['packages']
        self.assertEqual(set(packages), set(pins))
        for name, record in packages.items():
            self.assertEqual(record['version'], pins[name])
        for record in [*packages.values(), *audit['editable_sources']]:
            for line in record['active_requirements']:
                requirement = Requirement(line)
                name = canonicalize_name(requirement.name)
                self.assertIn(name, pins, line)
                self.assertIn(pins[name], requirement.specifier, line)
        self.assertEqual(audit['missing_or_conflicting_active_requirements'], [])
        self.assertEqual(audit['binary_only_blockers'], ['evdev', 'gym'])
        self.assertEqual(sum(record['wheel']['bytes'] for record in packages.values()
                             if record.get('wheel') is not None),
                         audit['minimum_selected_pypi_wheel_bytes'])

    def checkout_mocks(self):
        return [runtime.SOURCE_PINS['robocasa'], b'', runtime.SOURCE_PINS['robosuite'], b'']

    def test_multiple_missing_pins_stop_before_any_simulator_import(self):
        pins = pinned_dependencies(ROOT/'requirements-simulator.lock')
        observed = dict(pins)
        for name in ('idna', 'certifi', 'urllib3'):
            observed[name] = None
        with patch.object(runtime.importlib.util, 'find_spec',
                          return_value=SimpleNamespace(origin='/cache/pkg/__init__.py')), \
             patch.object(runtime.subprocess, 'check_output', side_effect=self.checkout_mocks()), \
             patch('coppertop.image_check.pinned_dependencies', return_value=pins), \
             patch('coppertop.image_check.installed_dependencies', return_value=observed), \
             patch.object(runtime, 'require_imports') as imports:
            receipt = runtime.simulator_gate_receipt()
        self.assertEqual(receipt['diagnostic']['reason'], 'Pinned simulator lock differs')
        self.assertEqual(receipt['diagnostic']['check'], 'dependencies')
        self.assertEqual({item['package'] for item in receipt['diagnostic']['dependency_differences']},
                         {'idna', 'certifi', 'urllib3'})
        imports.assert_not_called()

    def test_versions_alone_cannot_admit_conflicting_installed_dependencies(self):
        pins = pinned_dependencies(ROOT/'requirements-simulator.lock')
        with patch.object(runtime.importlib.util, 'find_spec',
                          return_value=SimpleNamespace(origin='/cache/pkg/__init__.py')), \
             patch.object(runtime.subprocess, 'check_output', side_effect=self.checkout_mocks()), \
             patch('coppertop.image_check.pinned_dependencies', return_value=pins), \
             patch('coppertop.image_check.installed_dependencies', return_value=pins), \
             patch.object(runtime, 'require_pip_check',
                          side_effect=ValueError('Installed dependency closure failed pip check')), \
             patch.object(runtime, 'require_imports') as imports:
            receipt = runtime.simulator_gate_receipt()
        self.assertEqual(receipt['status'], 'failed')
        self.assertEqual(receipt['diagnostic']['reason'], 'Installed dependency closure failed pip check')
        imports.assert_not_called()

    def test_module_files_missing_despite_distribution_metadata_are_rejected(self):
        # The live failure was an import, so version-only regression coverage
        # would reproduce the original false admission.
        with patch.object(dependency_gate.importlib, 'import_module',
                          side_effect=ModuleNotFoundError('omitted', name='idna')):
            with self.assertRaises(ModuleNotFoundError):
                dependency_gate.require_imports(('idna', 'requests'))

    def test_all_pin_differences_round_trip_and_arbitrary_versions_are_omitted(self):
        differences = [
            {'package': 'idna', 'expected': '3.10', 'observed': None, 'status': 'missing'},
            {'package': 'requests', 'expected': '2.34.2',
             'observed': 'https://dummy-secret.invalid/private', 'status': 'different'}]
        with patch.object(runtime, 'simulator_gate',
                          side_effect=dependency_gate.LockMismatch(differences)):
            receipt = runtime.simulator_gate_receipt()
        def runner(command, **options):
            options['stdout'].write(canonical(receipt))
            return SimpleNamespace(returncode=1)
        actual = simulator_preflight(runner)
        self.assertEqual(actual, receipt)
        items = actual['diagnostic']['dependency_differences']
        self.assertEqual(items[1]['observed'], 'omitted')
        self.assertNotIn('dummy-secret', json.dumps(actual))
        receipt['diagnostic']['dependency_differences'][1]['observed'] = ['invalid', 'dummy-secret']
        self.assertEqual(simulator_preflight(runner)['diagnostic']['reason'],
                         'structured_receipt_unavailable')

    def test_installed_requests_metadata_declares_the_live_missing_transitive(self):
        requires = importlib.metadata.requires('requests')
        self.assertTrue(any(line.startswith('idna') for line in requires))
