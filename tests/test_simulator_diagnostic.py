import copy
import importlib.util
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

from coppertop.reload_launch import read_json, reconcile
from coppertop.gpu_launch import historical_total
from test_modal_reload import App

ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location('simulator_diagnostic_tool', ROOT/'tools/run_simulator_diagnostic.py')
module = importlib.util.module_from_spec(spec); spec.loader.exec_module(module)
ATTEMPT = 'diag-abcdef0123456789'


def ledger():
    return {'schema_version': 2, 'billing_start_utc': '2026-10-01', 'cumulative_cap': '48',
        'unbounded_spend': [], 'apps': {'ap-old': {'state': 'unsettled_bounded',
            'known_reported_usd': '0.00233051', 'reservation_usd': '47.86459248'}}}


class DiagnosticTests(unittest.TestCase):
    def test_offline_plan_fits_headroom_and_contains_no_gpu_secrets_volume_or_build(self):
        with patch.object(module, 'refresh_billing', side_effect=AssertionError('provider access')):
            p = module.prepare(ledger(), ATTEMPT, ROOT)['plan']
        self.assertEqual(p['additional_cap_usd'], '0.10')
        self.assertEqual(p['prior_holds_usd'], '47.86459248')
        self.assertFalse(p['image_builds']); self.assertFalse(p['r2_access'])
        self.assertEqual(p['secrets'], []); self.assertEqual(p['volumes'], {})
        self.assertIsNone(p['limits']['gpu'])
        self.assertEqual(p['limits']['cpu'], [2, 2])
        self.assertEqual(p['limits']['function_seconds'], 900)
        self.assertEqual(p['limits']['startup_seconds'], 300)
        updated, _ = reconcile(ledger(), [], additional_cap=module.CAP)
        self.assertEqual(updated, ledger())
        self.assertEqual(historical_total(updated)+module.CAP, module.Decimal('47.96459248'))

    def test_definition_is_one_bounded_secret_free_cpu_function(self):
        app, _ = module.define(SimpleNamespace(__version__='1.6.1', App=App), 'cached')
        self.assertEqual(len(app.definitions), 1)
        options = app.definitions['diagnostic']
        self.assertEqual(options['cpu'], (2, 2)); self.assertEqual(options['memory'], (4096, 4096))
        self.assertEqual(options['timeout'], 900); self.assertEqual(options['startup_timeout'], 300)
        self.assertIsNone(options['gpu']); self.assertTrue(options['block_network'])
        self.assertEqual(options['secrets'], []); self.assertEqual(options['volumes'], {})
        self.assertEqual(options['retries'], 0); self.assertEqual(options['max_containers'], 1)

    def test_changed_ledger_and_overbudget_cannot_consume_or_access(self):
        prepared = module.prepare(ledger(), ATTEMPT, ROOT)
        changed = ledger(); changed['apps']['ap-old']['known_reported_usd'] = '1'
        with patch.object(module, 'refresh_billing', side_effect=AssertionError('provider access')):
            with self.assertRaises(ValueError): module.execute(prepared, changed, ROOT, prepared['plan_sha256'])
        changed = ledger(); changed['apps']['ap-old']['reservation_usd'] = '47.91'
        with self.assertRaises(ValueError): module.prepare(changed, ATTEMPT, ROOT)
        with self.assertRaises(ValueError): reconcile(ledger(), [], additional_cap='0.11')

    def test_existing_pending_hold_cannot_be_replaced_even_with_ample_headroom(self):
        held = ledger(); held['apps']['ap-old']['reservation_usd'] = '40'
        held['apps']['pending-'+ATTEMPT] = {'state': 'unsettled_bounded',
            'known_reported_usd': '0', 'reservation_usd': '0.10'}
        original = copy.deepcopy(held)
        with patch.object(module, 'refresh_billing', side_effect=AssertionError('provider access')):
            with self.assertRaisesRegex(ValueError, 'already reserved'):
                module.prepare(held, ATTEMPT, ROOT)
        self.assertEqual(held, original)

    def test_revised_ceiling_applies_only_to_the_single_ten_cent_cpu_diagnostic(self):
        held = ledger(); held['apps']['ap-old']['reservation_usd'] = '47.96459248'
        with self.assertRaises(ValueError): module.prepare(held, ATTEMPT, ROOT)
        held['cumulative_cap'] = '48.07'
        prepared = module.prepare(held, ATTEMPT, ROOT)
        self.assertEqual(prepared['plan']['cumulative_cap_usd'], '48.07')
        updated, _ = reconcile(held, [], additional_cap=module.CAP)
        self.assertEqual(updated, held)
        with self.assertRaises(ValueError): reconcile(held, [], additional_cap=10)
        with self.assertRaises(ValueError): reconcile(held, [], additional_cap=8)
        held['cumulative_cap'] = '48.08'
        with self.assertRaises(ValueError): module.prepare(held, ATTEMPT, ROOT)

    def test_partial_start_audits_owned_app_and_preserves_full_tiny_hold(self):
        self.partial_start(0)

    def test_shutdown_audit_waits_for_drain_without_retrying_diagnostic(self):
        self.partial_start(1)

    def test_persistently_active_resources_are_not_reported_clean(self):
        self.partial_start(3)

    def partial_start(self, failures):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            for name in ('tools/run_simulator_diagnostic.py',):
                target = root/name; target.parent.mkdir(parents=True); target.write_bytes((ROOT/name).read_bytes())
            prepared = module.prepare(ledger(), ATTEMPT, root)
            app = SimpleNamespace(app_id=None)
            def enter():
                app.app_id = 'ap-owned'
                raise TimeoutError('partial start')
            app.run = Mock(return_value=SimpleNamespace(__enter__=enter, __exit__=Mock()))
            function = Mock()
            sdk = SimpleNamespace(__version__='1.6.1', Image=SimpleNamespace(from_id=Mock(return_value='cached')))
            audit = Mock(side_effect=[RuntimeError('draining')]*failures +
                [{'app_stopped': True, 'active_containers': 0, 'active_sandboxes': 0}])
            with patch.object(module, 'refresh_billing', side_effect=lambda value,*a,**k:copy.deepcopy(value)), \
                 patch.object(module, 'cli_json', return_value=[]), \
                 patch.object(module, 'runtime_image', return_value='cached'), \
                 patch.object(module, 'define', return_value=(app, function)), \
                 patch.object(module, 'stop_and_audit_owned_app', audit), patch.object(module.time, 'sleep'):
                with self.assertRaises(TimeoutError): module.execute(prepared, ledger(), root, prepared['plan_sha256'], sdk=sdk)
                with self.assertRaises(FileExistsError): module.execute(prepared, ledger(), root, prepared['plan_sha256'], sdk=sdk)
            function.spawn.assert_not_called()
            self.assertEqual(audit.call_count, min(failures+1, 3))
            self.assertTrue(all(call.args == ('ap-owned',) for call in audit.call_args_list))
            out = root/'.local/simulator-diagnostic'/ATTEMPT
            held = read_json(out/'ledger.json')
            self.assertEqual(held['apps']['ap-owned']['reservation_usd'], '0.10')
            self.assertEqual(historical_total(held), module.Decimal('47.96459248'))
            outcome = read_json(out/'outcome.json')
            if failures == 3:
                self.assertEqual(outcome['cleanup_error_type'], 'RuntimeError')
                self.assertNotIn('cleanup', outcome)
            else: self.assertEqual(outcome['cleanup']['active_containers'], 0)
