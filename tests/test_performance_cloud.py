import copy
import json
import tempfile
import time
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

from coppertop import performance_cloud as module
from coppertop.chords import canonical, digest
from coppertop.cloud_stage import stage_receipt
from coppertop.reload_launch import reconcile, read_json
from test_performance_eval import records
from test_modal_reload import App, Volume


ROOT = Path(module.__file__).resolve().parents[1]
INPUTS = read_json(ROOT/'containers/performance-inputs.json')
ATTEMPT = 'reload-abcdef0123456789'


def ledger():
    return {'schema_version': 2, 'billing_start_utc': '2026-10-01', 'cumulative_cap': '58.82',
            'unbounded_spend': [], 'apps': {'ap-old': {'state': 'unsettled_bounded',
             'known_reported_usd': '1', 'reservation_usd': '48.81459248', 'description': 'coppertop-old'}}}


def envelope(prepared):
    p = prepared['cloud_plan']
    result = module.summarize(p['evaluation'], records())
    result.update(provenance=p['evaluation'], evaluation_plan_sha256=module.validate_plan(p['evaluation']),
        attempt_id=ATTEMPT, container_id='ta-fresh', training_container_id=module.ORIGINAL_CONTAINER_ID,
        checkpoint_receipt=module.RECEIPT)
    return {'container_id': 'ta-fresh', 'result': result, 'report_sha256': digest(canonical(result))}


class PerformanceCloudTests(unittest.TestCase):
    def prepare(self, root=ROOT): return module.prepare(INPUTS, ledger(), ATTEMPT, root)

    def test_nonstandard_storage_stops_before_bulk_get(self):
        client = SimpleNamespace(head_object=Mock(return_value={'StorageClass': 'STANDARD'}), get_object=Mock())
        store = SimpleNamespace(client=client, bucket='coppertop', key=lambda key: 'chords/objects/'+key)
        factory = Mock(return_value=store)
        self.assertIs(module.standard_input_store(factory, 'source', ['public-digest']), store)
        client.head_object.assert_called_once_with(Bucket='coppertop', Key='chords/objects/public-digest')
        client.head_object.return_value = {'StorageClass': 'INFREQUENT_ACCESS'}
        with self.assertRaises(ValueError): module.standard_input_store(factory, 'source', ['public-digest'])
        client.get_object.assert_not_called()

    def test_subprocess_failure_receipt_survives_but_raw_output_and_credentials_do_not(self):
        diagnostic = {'check': 'task_registry', 'error_type': 'ModuleNotFoundError',
            'reason': 'exception_text_omitted', 'missing_module': 'missing_sim',
            'trace': [{'file': 'performance_runtime.py', 'line': 73, 'function': 'simulator_gate'}]}
        receipt = {'status': 'failed', 'render_reset_executed': False, 'diagnostic': diagnostic}
        def runner(args, **options):
            self.assertEqual(options['timeout'], 600)
            self.assertNotIn('CLOUDFLARE_API_TOKEN', options['env'])
            options['stdout'].write(b'harmless warning\n'+canonical(receipt))
            options['stderr'].write(b'dummy-secret-must-not-persist')
            return SimpleNamespace(returncode=1)
        returned = module.simulator_preflight(runner)
        self.assertEqual(returned, receipt)
        self.assertNotIn('dummy-secret', json.dumps(returned))

    def test_real_import_trace_with_frozen_frames_survives_receipt_validation(self):
        import importlib
        from coppertop import performance_runtime as runtime
        def missing(progress):
            progress('dependencies')
            importlib.import_module('coppertop_missing_simulator_module_for_test')
        with patch.object(runtime, 'simulator_gate', side_effect=missing):
            receipt = runtime.simulator_gate_receipt()
        self.assertTrue(any('frozen' in frame['file'] for frame in receipt['diagnostic']['trace']))
        def runner(args, **options):
            options['stdout'].write(canonical(receipt))
            return SimpleNamespace(returncode=1)
        self.assertEqual(module.simulator_preflight(runner), receipt)

    def test_invalid_or_oversized_child_output_retains_only_exit_and_size(self):
        for raw in (b'dummy-secret', b'x'*65537, canonical({'status': 'failed', 'render_reset_executed': False,
                'diagnostic': {'check': 'bad', 'raw_secret': 'dummy-secret'}}),
                canonical({'status': 'failed', 'render_reset_executed': False, 'diagnostic': {
                    'check': 'dependencies', 'error_type': 'ValueError', 'reason': [], 'trace': []}})):
            def runner(args, **options):
                options['stdout'].write(raw); options['stderr'].write(b'dummy-secret')
                return SimpleNamespace(returncode=2)
            returned = module.simulator_preflight(runner)
            self.assertEqual(returned['status'], 'failed')
            self.assertEqual(returned['diagnostic']['exit_code'], 2)
            self.assertNotIn('dummy-secret', json.dumps(returned))

    def test_timeout_and_missing_interpreter_return_safe_failure_receipts(self):
        for error in (FileNotFoundError('dummy-secret'), module.subprocess.TimeoutExpired(['private'], 600)):
            receipt = module.simulator_preflight(Mock(side_effect=error))
            self.assertEqual(receipt['status'], 'failed')
            self.assertEqual(receipt['diagnostic']['error_type'], type(error).__name__)
            self.assertNotIn('dummy-secret', json.dumps(receipt))

    def test_prepare_binds_fixed_inputs_budget_code_and_cpu_only_scopes_without_access(self):
        with patch.object(module, 'selected_env', side_effect=AssertionError('secret read')), \
             patch.object(module, 'parent_credentials', side_effect=AssertionError('API call')):
            prepared = self.prepare()
        p = prepared['cloud_plan']
        self.assertEqual(p['additional_cap_usd'], '10')
        self.assertEqual(p['cumulative_cap_usd'], '58.82')
        self.assertEqual([len(s.object_paths) for s in prepared['scopes']], [17, 5, 0])
        self.assertEqual(p['auth']['ttl_seconds'], 7200)
        self.assertFalse(p['new_image_builds'])
        self.assertFalse(p['new_downloads'])
        self.assertEqual(p['limits']['automatic_retries'], 0)
        self.assertEqual(p['limits']['volume_bytes'], 3*1024**3)

    def test_wrong_inputs_holds_and_cap_are_rejected(self):
        changed = copy.deepcopy(INPUTS); changed['exact_input_objects'][0]['path'] = 'unrelated/object'
        with self.assertRaises(ValueError): module.prepare(changed, ledger(), ATTEMPT, ROOT)
        for cap, hold in (('48.82', '48.81459248'), ('58.83', '48.81459248'), ('58.82', '48.83')):
            value = ledger(); value['cumulative_cap'] = cap; value['apps']['ap-old']['reservation_usd'] = hold
            with self.assertRaises(ValueError): module.prepare(INPUTS, value, ATTEMPT, ROOT)

    def test_fresh_billing_preserves_all_holds_and_unsupported_envelopes_fail(self):
        value = ledger()
        row = {'object_id': 'ap-old', 'description': 'coppertop-old', 'environment': 'main',
               'interval_start': '2026-10-06T01:00:00Z', 'cost': '.25'}
        updated, _ = reconcile(value, [row], additional_cap=10)
        self.assertEqual(updated['apps'], value['apps'])
        with self.assertRaises(ValueError): reconcile(value, [], additional_cap=8)
        value['apps']['ap-new'] = {'state': 'unsettled_bounded', 'known_reported_usd': '0',
                                  'reservation_usd': '10', 'description': 'coppertop-performance'}
        updated, _ = reconcile(value, [], app_id='ap-new', reservation_key='ap-new', additional_cap=10)
        self.assertEqual(module.historical_total(updated), module.Decimal('58.81459248'))
        with self.assertRaises(ValueError): reconcile(value, [], additional_cap=10)

    def test_gpu_has_no_secrets_network_or_writable_mount_and_functions_have_hard_limits(self):
        prepared = self.prepare()
        sdk = SimpleNamespace(__version__='1.6.1', App=App)
        calls = module.create_functions(sdk, prepared, image='reviewed', volume=Volume([]),
            store_factory=Mock(), sessions={k: ['scoped-'+k] for k in ('source', 'checkpoint', 'report')})
        definitions = calls.app.definitions
        self.assertEqual(len(definitions), 5)
        for name, options in definitions.items():
            self.assertEqual(options['region'], 'us')
            self.assertEqual(options['max_containers'], 1)
            self.assertEqual(options['retries'], 0)
            self.assertTrue(options['single_use_containers'])
        gpu = definitions['evaluate']
        self.assertEqual(gpu['gpu'], 'L40S'); self.assertEqual(gpu['timeout'], 1800)
        self.assertEqual(gpu['cpu'], (4, 4)); self.assertEqual(gpu['memory'], (65536, 65536))
        self.assertEqual(gpu['secrets'], []); self.assertTrue(gpu['block_network'])
        self.assertTrue(gpu['volumes'][module.INPUT_MOUNT].options['read_only'])
        self.assertEqual(definitions['cache_gate']['secrets'], [])
        self.assertIsNone(definitions['cache_gate']['gpu'])
        self.assertEqual(definitions['cache_gate']['timeout'], 1200)
        self.assertEqual(definitions['publish']['volumes'], {})

    def run_phases(self, root, *, failure=None):
        prepared = self.prepare()
        calls = SimpleNamespace(source='source', checkpoint='checkpoint', gate='gate', evaluate='evaluate', publish='publish')
        evaluated = envelope(prepared); events = []
        def invoke(function, args, timeout):
            events.append('invoke-'+function)
            if function == failure: raise RuntimeError('fixture phase failure')
            if function in ('source', 'checkpoint'):
                return {'container_id': 'ta-'+function, 'receipt': stage_receipt(prepared[function])}
            if function == 'gate':
                return {'container_id': 'ta-gate', 'status': 'failed' if failure == 'bad_gate' else 'passed', 'image_id': module.CACHED_IMAGE,
                        'evaluation_plan_sha256': module.validate_plan(prepared['cloud_plan']['evaluation']),
                        'simulator': {'status': 'failed' if failure == 'bad_gate' else 'passed',
                            'diagnostic': {'check': 'asset_inventory', 'error_type': 'ValueError'}}}
            if function == 'evaluate': return evaluated
            return {'container_id': 'ta-publisher', 'report_key': '0'*64 if failure == 'bad_publish' else evaluated['report_sha256'],
                    'attempt_id': ATTEMPT, 'verifier_container_id': 'ta-fresh'}
        def stopped(identity): events.append('stopped-'+identity); return True
        def cleanup():
            events.append('cleanup')
            return {'app_stopped': True, 'active_containers': 0, 'active_sandboxes': 0, 'volume_deleted': True}
        output = root/'attempt'; output.mkdir()
        try:
            result = module.execute_phases(calls, prepared, output, admission=lambda: events.append('admit'),
                invoke=invoke, stopped=stopped, cleanup=cleanup)
        except (RuntimeError, ValueError):
            result = read_json(output/'outcome.json')
        return result, events, output, calls, prepared, invoke, stopped, cleanup

    def test_stage_commit_and_stop_barriers_precede_gpu_and_report_publication(self):
        with tempfile.TemporaryDirectory() as directory:
            result, events, output, calls, prepared, invoke, stopped, cleanup = self.run_phases(Path(directory))
            self.assertEqual(result['status'], 'passed'); self.assertTrue(result['durable_report_verified'])
            self.assertLess(events.index('stopped-ta-checkpoint'), events.index('invoke-gate'))
            self.assertLess(events.index('stopped-ta-gate'), events.index('invoke-evaluate'))
            self.assertLess(events.index('stopped-ta-fresh'), events.index('invoke-publish'))
            self.assertEqual(events[-1], 'cleanup')
            with self.assertRaises(FileExistsError):
                module.execute_phases(calls, prepared, output, admission=Mock(), invoke=invoke, stopped=stopped, cleanup=cleanup)

    def test_cpu_failures_never_invoke_gpu_and_every_phase_failure_cleans_up(self):
        for failure in ('source', 'checkpoint', 'gate', 'evaluate', 'publish', 'bad_gate', 'bad_publish'):
            with tempfile.TemporaryDirectory() as directory:
                result, events, *_ = self.run_phases(Path(directory), failure=failure)
                self.assertEqual(result['status'], 'failed'); self.assertEqual(events[-1], 'cleanup')
                if failure in ('source', 'checkpoint', 'gate', 'bad_gate'):
                    self.assertNotIn('invoke-evaluate', events)
                if failure == 'bad_gate':
                    self.assertEqual(result['phases']['cache_gate']['simulator']['diagnostic']['check'], 'asset_inventory')

    def test_publication_rejects_forged_metrics_hash_and_training_identity(self):
        prepared = self.prepare(); record = envelope(prepared)
        module.validate_report(record, prepared['cloud_plan'])
        changed = copy.deepcopy(record); changed['result']['arms']['official']['successes'] = 99
        changed['report_sha256'] = digest(canonical(changed['result']))
        with self.assertRaises(ValueError): module.validate_report(changed, prepared['cloud_plan'])
        changed = copy.deepcopy(record); changed['report_sha256'] = 'a'*64
        with self.assertRaises(ValueError): module.validate_report(changed, prepared['cloud_plan'])
        changed = copy.deepcopy(record); changed['container_id'] = module.ORIGINAL_CONTAINER_ID
        with self.assertRaises(ValueError): module.validate_report(changed, prepared['cloud_plan'])

    def test_approval_consumed_and_full_hold_saved_before_credential_gate_failure(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            for name in ('coppertop/performance_eval.py', 'coppertop/performance_runtime.py', 'coppertop/performance_cloud.py',
                         'coppertop/reload_launch.py', 'tools/run_performance_cloud.py'):
                target = root/name; target.parent.mkdir(parents=True, exist_ok=True); target.write_bytes((ROOT/name).read_bytes())
            prepared = self.prepare(root)
            def refresh(value, output, **kwargs): module.save(output/'ledger.json', value); return copy.deepcopy(value)
            def live(*args, **kwargs):
                output = args[3]
                self.assertTrue((output/'consumed.json').is_file())
                held = read_json(output/'ledger.json')
                self.assertEqual(held['apps']['pending-'+ATTEMPT]['reservation_usd'], '10')
                raise ValueError('fixture credential failure')
            with patch.object(module, 'refresh_billing', side_effect=refresh), patch.object(module, '_live', side_effect=live) as live_call:
                with self.assertRaises(ValueError): module.execute(prepared, ledger(), root, root/'missing.env', None)
                self.assertEqual(live_call.call_count, 0)
                with self.assertRaises(ValueError): module.execute(prepared, ledger(), root, root/'missing.env', prepared['cloud_plan_sha256'])
                with self.assertRaises(FileExistsError): module.execute(prepared, ledger(), root, root/'missing.env', prepared['cloud_plan_sha256'])
                self.assertEqual(live_call.call_count, 1)
                failure = read_json(root/'.local/performance-eval'/ATTEMPT/'launch-failure.json')
                self.assertEqual(failure['automatic_retries'], 0)

    def test_approval_does_not_survive_changed_code_or_ledger(self):
        prepared = self.prepare(); changed = ledger(); changed['apps']['ap-old']['known_reported_usd'] = '2'
        with patch.object(module, '_live', side_effect=AssertionError('cloud access')):
            with self.assertRaises(ValueError):
                module.execute(prepared, changed, ROOT, ROOT/'missing.env', prepared['cloud_plan_sha256'])

    def test_partial_start_and_volume_collision_preserve_ownership_boundaries(self):
        for failure in ('collision', 'partial_start', 'active_audit', 'changed_identity'):
            with self.subTest(failure=failure), tempfile.TemporaryDirectory() as directory:
                output = Path(directory)
                prepared = self.prepare()
                held = ledger()
                held['apps']['pending-'+ATTEMPT] = {'state': 'unsettled_bounded',
                    'known_reported_usd': '0', 'reservation_usd': '10', 'description': 'coppertop-performance'}
                volume = SimpleNamespace(object_id='vo-owned', hydrate=Mock())
                other = SimpleNamespace(object_id='vo-unrelated', hydrate=Mock())
                create = Mock(side_effect=ValueError('exists') if failure == 'collision' else None)
                delete = Mock()
                sdk = SimpleNamespace(__version__='1.6.1',
                    Secret=SimpleNamespace(from_dict=Mock(return_value='scoped')),
                    Image=SimpleNamespace(from_id=Mock(return_value='cached')),
                    Volume=SimpleNamespace(from_name=Mock(side_effect=[volume, other] if failure == 'changed_identity' else None,
                                                         return_value=volume),
                        objects=SimpleNamespace(create=create, delete=delete)))
                app = SimpleNamespace(app_id=None)
                def enter():
                    app.app_id = 'ap-owned'
                    raise TimeoutError('partial start')
                app.run = Mock(return_value=SimpleNamespace(__enter__=enter, __exit__=Mock()))
                calls = SimpleNamespace(app=app, source=Mock(), checkpoint=Mock(), gate=Mock(), evaluate=Mock(), publish=Mock())
                audit = Mock(side_effect=RuntimeError('active') if failure == 'active_audit' else None)
                with patch.object(module, 'cli_json', return_value=[]), \
                     patch.object(module, 'selected_env', return_value={'CLOUDFLARE_R2_URL': 'https://'+'a'*32+'.r2.cloudflarestorage.com'}), \
                     patch.object(module, 'parent_credentials', return_value=('fixture', 'fixture')), \
                     patch.object(module, 'mint', return_value=SimpleNamespace(environment=lambda: {})), \
                     patch.object(module, 'runtime_image', return_value='cached'), \
                     patch.object(module, 'create_functions', return_value=calls), \
                     patch.object(module, 'refresh_billing', side_effect=lambda value, *a, **k: value), \
                     patch.object(module, 'stop_and_audit_owned_app', audit):
                    with self.assertRaises(ValueError if failure == 'collision' else TimeoutError):
                        module._live(prepared, held, output/'missing.env', output, ROOT,
                                     deadline=time.monotonic()+5400, sdk=sdk)
                calls.evaluate.spawn.assert_not_called()
                outcome = read_json(output/'outcome.json')
                if failure == 'partial_start':
                    audit.assert_called_once_with('ap-owned', runner=module.subprocess.run)
                    delete.assert_called_once_with(prepared['cloud_plan']['volume_name'], environment_name='main', allow_missing=False)
                    self.assertTrue(outcome['cleanup']['volume_deleted'])
                    self.assertEqual(read_json(output/'ledger.json')['apps']['ap-owned']['reservation_usd'], '10')
                else:
                    delete.assert_not_called()
                    self.assertIn('cleanup_error_type', outcome)
                if failure == 'collision': app.run.assert_not_called()
