import copy
import importlib.util
import io
import json
import sys
import tempfile
import unittest
from decimal import Decimal
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

from coppertop.reload_launch import reconcile, read_json

ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location('cpu_repair_tool', ROOT/'tools/run_cpu_image_repair.py')
module = importlib.util.module_from_spec(spec)
sys.modules[spec.name] = module
spec.loader.exec_module(module)
ATTEMPT = 'repair-61d8ca278e0543bf'


def ledger():
    return {'schema_version': 2, 'cumulative_cap': '48.07', 'billing_start_utc': '2026-10-01',
        'unbounded_spend': [], 'apps': {'ap-old': {'state': 'unsettled_bounded',
            'known_reported_usd': '0', 'reservation_usd': '48.06459248'}}}


def receipt(p):
    versions = {**p['native_pins'], 'torch': '2.8.0+cu128',
                'torchvision': '0.23.0+cu128', 'flash-attn': '2.8.3'}
    return {'status': 'passed', 'plan_sha256': module.sha(p), 'gpu_started': False,
        'simulator': copy.deepcopy(p['simulator_success']), 'native': {
        'status': 'passed', 'versions': versions, 'active_closure_checked': True,
        'pip_check': True, 'imports_verified': True, 'cuda_abi_verified': True,
        'full_model_instantiated': False, 'cuda_executed': False}}


class CpuRepairTests(unittest.TestCase):
    def prepare(self):
        return module.prepare(ledger(), ATTEMPT, ROOT)

    def test_offline_plan_preserves_holds_and_binds_only_small_official_repair(self):
        with patch.object(module, 'define', side_effect=AssertionError('provider')):
            prepared = self.prepare()
        p = prepared['plan']
        self.assertEqual(prepared['plan_sha256'], module.sha(p))
        self.assertEqual(p['prior_holds_usd'], '48.06459248')
        self.assertEqual(p['additional_cap_usd'], '0.25')
        self.assertEqual(p['cumulative_cap_usd'], '48.32')
        self.assertFalse(p['paid_authorized']); self.assertFalse(p['static_audit_is_runtime_proof'])
        self.assertEqual(p['secrets'], []); self.assertEqual(p['volumes'], {})
        self.assertEqual(p['maximum_wheel_download_bytes'], 32*1024**2)
        self.assertFalse(module.PROTECTED.intersection(p['wheel_selection']))
        self.assertFalse({'cbor2', 'grpclib', 'h2', 'hpack', 'hyperframe'}.intersection(p['repair_allowlist']))
        self.assertEqual(len(p['source_payload']), 7)
        self.assertLess(len(module.encoded(p)), 512*1024)
        rate = (Decimal('0.0000131')*2+Decimal('0.00000222')*4)*Decimal('1.75')
        bound = rate*3300+Decimal('0.04')
        self.assertEqual(bound, Decimal(p['estimated_all_in_bound_usd']))
        self.assertLess(bound, module.CAP)
        self.assertLess(Decimal(p['prior_holds_usd'])+module.CAP, module.CEILING)

    def test_new_ceiling_does_not_allow_old_gpu_bundles_or_other_reservations(self):
        held = ledger(); held['cumulative_cap'] = '48.32'
        reconcile(held, [], additional_cap='0.25')
        for cap in ('0.10', '0.20', '0.26', '8', '10'):
            with self.assertRaises(ValueError): reconcile(held, [], additional_cap=cap)
        held['cumulative_cap'] = '48.33'
        with self.assertRaises(ValueError): reconcile(held, [], additional_cap='0.25')

    def test_missing_only_repair_rejects_differences_incomplete_and_large_inventory(self):
        p = self.prepare()['plan']; versions = dict(p['simulator_pins'])
        self.assertEqual(module.select_repair(p, versions), ['idna'])
        versions['requests'] = None; versions['urllib3'] = None
        self.assertEqual(module.select_repair(p, versions), ['idna', 'requests', 'urllib3'])
        for name, value in (('torch', None), ('gym', None), ('h2', None), ('pyarrow', None), ('numpy', '1.0')):
            changed = dict(p['simulator_pins']); changed[name] = value
            with self.assertRaises(ValueError): module.select_repair(p, changed)
        with self.assertRaises(ValueError): module.select_repair(p, {'idna': None})

    def test_editable_source_failure_stops_before_distribution_inventory(self):
        p = self.prepare()['plan']
        with patch.object(module.os.path, 'samefile', return_value=True), \
             patch('importlib.util.find_spec', return_value=None), \
             patch.object(module.importlib.metadata, 'version') as version:
            with self.assertRaisesRegex(ValueError, 'editable source absent'):
                module.simulator_inventory(p)
        version.assert_not_called()
        with patch.object(module.os.path, 'samefile', return_value=True), \
             patch('importlib.util.find_spec', return_value=SimpleNamespace(origin='/cache/robocasa/robocasa/__init__.py')), \
             patch.object(module.subprocess, 'check_output', side_effect=['wrong', b'']), \
             patch.object(module.importlib.metadata, 'version') as version:
            with self.assertRaisesRegex(ValueError, 'editable source differs'):
                module.simulator_inventory(p)
        version.assert_not_called()

    def test_native_failure_cannot_reach_download_or_install(self):
        p = self.prepare()['plan']; opener = Mock()
        with patch.object(module, 'network_tx', return_value=0), \
             patch.object(module, 'child', side_effect=RuntimeError('native failed')), \
             patch('urllib.request.build_opener', return_value=opener), \
             patch.object(module.subprocess, 'run') as install:
            with self.assertRaises(RuntimeError): module.repair_image_worker(p)
        opener.open.assert_not_called(); install.assert_not_called()

    def test_corrupt_bound_wheel_is_rejected_before_install(self):
        p = self.prepare()['plan']; wheel = p['wheel_selection']['idna']
        metadata = {'urls': [{'filename': wheel['filename'], 'digests': {'sha256': wheel['sha256']},
                     'size': wheel['bytes'], 'url': 'https://files.pythonhosted.org/packages/example.whl'}]}
        opener = Mock(); opener.open.side_effect = [io.BytesIO(module.encoded(metadata)), io.BytesIO(b'bad-wheel')]
        with patch.object(module, 'network_tx', return_value=0), \
             patch.object(module, 'child', side_effect=[{}, p['simulator_pins']]), \
             patch('urllib.request.build_opener', return_value=opener), \
             patch.object(module.subprocess, 'run') as install, patch('builtins.print'):
            with self.assertRaisesRegex(ValueError, 'checksum differs'):
                module.repair_image_worker(p)
        install.assert_not_called()

    def test_network_threshold_stops_before_child_work(self):
        with patch.object(module, 'network_tx', side_effect=[0, 769*1024**2]), \
             patch.object(module, 'child') as child:
            with self.assertRaisesRegex(ValueError, 'network envelope exceeded'):
                module.repair_image_worker(self.prepare()['plan'])
        child.assert_not_called()

    def test_report_read_is_bounded_before_allocation(self):
        stream = io.BytesIO(b'x'*131072)
        opened = Mock(); opened.__enter__ = Mock(return_value=stream); opened.__exit__ = Mock(return_value=False)
        with patch.object(Path, 'open', return_value=opened):
            with self.assertRaisesRegex(ValueError, 'exceeds bound'):
                module.bounded_json(Path('unused'), 65536)
        self.assertEqual(stream.tell(), 65537)

    def test_receipt_requires_native_versions_and_complete_simulator_evidence(self):
        p = self.prepare()['plan']; result = receipt(p)
        module.validate_result(result, p)
        for mutate in (lambda r:r['native']['versions'].pop('idna'),
                       lambda r:r['native'].update(pip_check=False),
                       lambda r:r['simulator']['assets'].update(files=1),
                       lambda r:r.update(gpu_started=True)):
            changed = copy.deepcopy(result); mutate(changed)
            with self.assertRaises(ValueError): module.validate_result(changed, p)

    def test_context_exit_failure_still_audits_and_retains_full_hold_and_replay_guard(self):
        prepared = self.prepare(); p = prepared['plan']
        app = SimpleNamespace(app_id=None)
        def enter(): app.app_id = 'ap-owned'
        context = Mock(); context.__enter__ = Mock(side_effect=enter)
        context.__exit__ = Mock(side_effect=RuntimeError('exit failed'))
        app.run = Mock(return_value=context)
        gate = Mock(); gate.spawn.return_value.get.return_value = receipt(p)
        audit = Mock(return_value={'app_stopped': True, 'active_containers': 0, 'active_sandboxes': 0})
        def billing(current, *args, **kwargs):
            return reconcile(current, [], additional_cap=kwargs['additional_cap'],
                             reservation_key=kwargs.get('reservation_key'))[0]
        with tempfile.TemporaryDirectory() as directory, \
             patch.object(module, 'prepare', return_value=prepared), \
             patch.object(module, 'define', return_value=(app, gate, SimpleNamespace(object_id='im-repaired'))), \
             patch('coppertop.reload_launch.refresh_billing', side_effect=billing) as refresh, \
             patch('coppertop.reload_launch.stop_and_audit_owned_app', audit):
            root = Path(directory)
            result = module.execute(prepared, ledger(), root, prepared['plan_sha256'], sdk=object())
            self.assertEqual(result['context_exit_error_type'], 'RuntimeError')
            self.assertTrue(result['cleanup']['app_stopped'])
            audit.assert_called_once_with('ap-owned')
            held = read_json(root/'.local/cpu-image-repair'/ATTEMPT/'ledger.json')
            self.assertEqual(held['apps']['ap-owned']['reservation_usd'], '0.25')
            with self.assertRaises(FileExistsError):
                module.execute(prepared, ledger(), root, prepared['plan_sha256'], sdk=object())
            self.assertEqual(refresh.call_count, 2); gate.spawn.assert_called_once()

    def test_partial_start_retains_ownership_for_audit(self):
        prepared = self.prepare(); app = SimpleNamespace(app_id=None)
        def enter():
            app.app_id = 'ap-partial'; raise TimeoutError('partial start')
        context = Mock(); context.__enter__ = Mock(side_effect=enter)
        app.run = Mock(return_value=context); gate = Mock()
        audit = Mock(return_value={'app_stopped': True, 'active_containers': 0, 'active_sandboxes': 0})
        with tempfile.TemporaryDirectory() as directory, \
             patch.object(module, 'prepare', return_value=prepared), \
             patch.object(module, 'define', return_value=(app, gate, object())), \
             patch('coppertop.reload_launch.refresh_billing', side_effect=lambda current,*a,**k:copy.deepcopy(current)), \
             patch('coppertop.reload_launch.stop_and_audit_owned_app', audit):
            result = module.execute(prepared, ledger(), Path(directory), prepared['plan_sha256'], sdk=object())
        self.assertEqual(result['status'], 'failed')
        audit.assert_called_once_with('ap-partial'); gate.spawn.assert_not_called()

    def test_pinned_sdk_definition_and_entrypoint_mount_are_offline(self):
        try:
            import modal
            from modal._functions import _Function
            from modal._resources import convert_fn_config_to_resources_config
            from modal._utils.function_utils import FunctionSourceInfo, FunctionSourceInfoType
        except ImportError:
            self.skipTest('Optional pinned controller SDK unavailable')
        p = self.prepare()['plan']
        with patch.object(_Function, '_from_local', wraps=_Function._from_local) as definitions:
            app, gate, image = module.define(modal, p)
        self.assertIsNone(app.app_id)
        self.assertEqual(gate._spec_.cpu, (2, 2)); self.assertEqual(gate._spec_.memory, (4096, 4096))
        self.assertEqual(len(definitions.call_args_list), 2)
        builder, validation = [call.kwargs for call in definitions.call_args_list]
        self.assertTrue(builder['is_builder_function'])
        self.assertEqual(builder['timeout'], 600)
        self.assertEqual(validation['timeout'], 900); self.assertEqual(validation['startup_timeout'], 300)
        self.assertTrue(validation['block_network']); self.assertEqual(validation['retries'], 0)
        self.assertEqual(validation['max_containers'], 1)
        for definition in (builder, validation):
            self.assertTrue(definition['include_source'])
            resources = convert_fn_config_to_resources_config(cpu=definition['cpu'],
                        memory=definition['memory'], gpu=definition['gpu'])
            self.assertEqual(resources.milli_cpu_max, 2000)
            self.assertEqual(resources.memory_mb_max, 4096)
        info = FunctionSourceInfo(module.repair_image_worker)
        self.assertEqual(info._type, FunctionSourceInfoType.FILE)
        self.assertEqual(list(info.get_entrypoint_mount()), ['run_cpu_image_repair'])


if __name__ == '__main__': unittest.main()
