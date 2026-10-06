import copy
import hashlib
import json
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch, Mock

from coppertop import reload_launch as module
from coppertop.chords import canonical, object_path
from coppertop.gpu_smoke import MODEL_REPO, MODEL_REVISION


def fixture():
    artifacts={name:{'sha256':hashlib.sha256(name.encode()).hexdigest(),'size':100} for name in (
        'head.safetensors','validation.safetensors','head-manifest.json','result.json')}
    artifacts['head.safetensors']={'sha256':module.HEAD,'size':1230711168}
    source=[{'path':'chords/'+object_path(hashlib.sha256(str(i).encode()).hexdigest()),
             'sha256':hashlib.sha256(str(i).encode()).hexdigest(),'size':100} for i in range(16)]
    source.append({'path':'chords/'+object_path(module.RELEASE),'sha256':module.RELEASE,'size':195})
    checkpoint=[{'path':module.CHECKPOINT_PREFIX+'/'+object_path(ref['sha256']),**ref} for ref in artifacts.values()]
    checkpoint.append({'path':module.CHECKPOINT_PREFIX+'/'+object_path(module.RECEIPT),'sha256':module.RECEIPT,'size':818})
    attempt='reload-643845c8f57341d6'
    plan={'provider':'Modal','region':'us','environment':'main','additional_cap':'8','cumulative_cap':'30',
        'holds_released':0,'checkpoint_run_id':module.CHECKPOINT_RUN_ID,'source_release':module.RELEASE,
        'checkpoint_receipt_sha256':module.RECEIPT,'original_training_task_id':module.ORIGINAL_CONTAINER_ID,
        'original_training_identity_verified':True,'attempt_id':attempt,'app_name':'coppertop-reload-only-'+attempt,'prior_conservative_holds':'21',
        'image':{'cache_seed':module.SEED_IMAGE_ID,'model':MODEL_REPO,'model_revision':MODEL_REVISION},
        'storage':{'input_volume':'coppertop-'+attempt+'-inputs','total_bytes_ceiling':3*1024**3},
        'exact_input_objects':source+checkpoint,'checkpoint_artifacts':artifacts,
        'limits':{'updated_image_build_cpu_seconds':600,'image_build_startup_seconds':300,
            'image_gate_cpu_seconds':1200,'cpu_stage_seconds':600,'cpu_checkpoint_restage_seconds':1800,
            'cpu_report_publish_seconds':300,'function_startup_seconds_each':300,'gpu_function_seconds':300,
            'native_reload_subprocess_seconds':120,'scale_down_seconds':2,'shutdown_audit_budget_seconds_each':60,
            'controller_wall_seconds':7200,'r2_total_requests':2000,'r2_new_write_bytes':65536,
            'modal_outbound_bytes':1073741824,'max_gpu_containers':1,'automatic_retries':0},
        'authentication':{'ttl_seconds':7200,'bucket':'coppertop','session_count':3,
            'source_session':{'actions':['GetObject','HeadObject'],'objectPaths':[r['path'] for r in source]},
            'checkpoint_session':{'actions':['GetObject','HeadObject'],'objectPaths':[r['path'] for r in checkpoint]},
            'write_session':{'actions':['GetObject','HeadObject','PutObject'],
                             'prefixPaths':['chords/gpu-reload/'+attempt+'/objects/']}}}
    ledger={'schema_version':2,'billing_start_utc':'2026-10-01','unbounded_spend':[],
        'apps':{'ap-old':{'description':'coppertop-old','state':'unsettled_bounded',
                         'known_reported_usd':'1','reservation_usd':'21'}}}
    return plan,ledger


class ReloadLauncherTests(unittest.TestCase):
    def test_pinned_bucket_does_not_require_an_extra_local_assignment(self):
        account = 'a' * 32
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / '.env.local'
            config = ('CLOUDFLARE_API_TOKEN=fixture-token\n'
                f'CLOUDFLARE_ACCOUNT_ID={account}\n'
                f'CLOUDFLARE_R2_URL=https://{account}.r2.cloudflarestorage.com/coppertop\n')
            path.write_text(config)
            values = module.selected_env(path)
            self.assertNotIn('R2_BUCKET', values)
            self.assertEqual(values['CLOUDFLARE_R2_URL'],
                f'https://{account}.r2.cloudflarestorage.com')
            path.write_text(config + 'R2_BUCKET=coppertop\n')
            self.assertEqual(module.selected_env(path)['R2_BUCKET'], 'coppertop')
            path.write_text(config + 'R2_BUCKET=unrelated\n')
            with self.assertRaisesRegex(ValueError, 'approved coppertop bucket'):
                module.selected_env(path)

    def prepare(self,plan,ledger):
        with patch.object(module,'build_recipe',return_value={'reviewed':'recipe'}):
            return module.prepare(plan,ledger,Path('/does-not-exist'))

    def test_preparation_uses_only_metadata_and_binds_three_narrow_scopes(self):
        plan,ledger=fixture()
        with patch.object(module,'selected_env',side_effect=AssertionError('credential read')), \
             patch.object(module,'parent_credentials',side_effect=AssertionError('API call')):
            prepared=self.prepare(plan,ledger)
        self.assertEqual(len(prepared['source'].objects),17)
        self.assertEqual(len(prepared['checkpoint'].objects),4)
        self.assertEqual([len(s.object_paths) for s in prepared['scopes']],[17,5,0])
        self.assertEqual(prepared['conservative_after_hold_usd'],'29')

    def test_changed_review_recipe_or_missing_approval_blocks(self):
        plan,ledger=fixture()
        prepared=self.prepare(plan,ledger)
        for approval,recipe in ((None,prepared['recipe_sha256']),('0'*64,prepared['recipe_sha256']),
                                 (prepared['plan_sha256'],'0'*64)):
            plan['image']['recipe_sha256']=recipe
            with self.assertRaises(ValueError):
                module.require_approval(prepared,plan,approval)

    def test_plan_bounds_and_namespace_changes_rejected(self):
        plan,ledger=fixture()
        mutations=[lambda p:p['storage'].update(output_volume='other'),
            lambda p:p['limits'].update(automatic_retries=1),
            lambda p:p.update(original_training_task_id='ta-other'),
            lambda p:p['exact_input_objects'][0].update(path='other/object'),
            lambda p:p['exact_input_objects'][0].update(size=-1),
            lambda p:p['checkpoint_artifacts']['head.safetensors'].update(size=10)]
        for mutate in mutations:
            changed=copy.deepcopy(plan);mutate(changed)
            with self.assertRaises(ValueError):
                self.prepare(changed,ledger)

    def test_stopped_billing_never_releases_hold_and_unknown_spend_blocks(self):
        _,ledger=fixture()
        row={'object_id':'ap-old','description':'coppertop-old','environment':'main','interval_start':'2026-10-06T01:00:00','cost':'2'}
        updated,cost=module.reconcile(ledger,[row],app_id='ap-new')
        self.assertEqual(module.historical_total(updated),21)
        self.assertEqual(updated['apps']['ap-old']['reservation_usd'],'21')
        for changed in (dict(row,cost='22'),dict(row,**{'object_id':'ap-unknown'}),dict(row,cost='NaN')):
            with self.assertRaises(ValueError):module.reconcile(ledger,[changed],app_id='ap-new')
        with self.assertRaises(ValueError):
            module.reconcile(ledger,[dict(row,**{'object_id':'ap-new','cost':'8.01'})],app_id='ap-new')

    def test_current_full_hold_is_retained_in_returned_ledger(self):
        _,ledger=fixture()
        ledger['apps']['ap-new']={'description':'coppertop-reload-only','state':'unsettled_bounded',
                                 'known_reported_usd':'0','reservation_usd':'8'}
        row={'object_id':'ap-new','description':'coppertop-reload-only','environment':'main',
             'interval_start':'2026-10-06T01:00:00','cost':'0.1'}
        updated,cost=module.reconcile(ledger,[row],app_id='ap-new',reservation_key='ap-new')
        self.assertEqual(module.historical_total(updated),29)
        self.assertEqual(updated['apps']['ap-new']['reservation_usd'],'8')
        with self.assertRaises(ValueError):module.reconcile(updated,[row])

    def test_volume_delete_requires_captured_identity(self):
        deleted=[]
        current=SimpleNamespace(object_id='vo-different',hydrate=lambda:None)
        sdk=SimpleNamespace(Volume=SimpleNamespace(from_name=lambda *a,**k:current,
            objects=SimpleNamespace(delete=lambda *a,**k:deleted.append(a))))
        with self.assertRaises(ValueError):module.delete_owned_volume(sdk,'dedicated','vo-owned')
        self.assertEqual(deleted,[])
        current.object_id='vo-owned'
        module.delete_owned_volume(sdk,'dedicated','vo-owned')
        self.assertEqual(deleted,[('dedicated',)])

    def test_selected_env_rejects_duplicates_and_expansion_without_eval(self):
        with tempfile.TemporaryDirectory() as directory:
            path=Path(directory)/'.env'
            for text in ('CLOUDFLARE_API_TOKEN=x\nCLOUDFLARE_API_TOKEN=y',
                         'CLOUDFLARE_API_TOKEN=$(touch /tmp/never)'):
                path.write_text(text)
                with self.assertRaises(ValueError):module.selected_env(path)

    def test_default_prepare_with_missing_env_file_has_no_execution(self):
        plan,ledger=fixture()
        with tempfile.TemporaryDirectory() as directory:
            root=Path(directory)
            (root/'plan.json').write_bytes(canonical(plan));(root/'ledger.json').write_bytes(canonical(ledger))
            with patch.object(module,'build_recipe',return_value={'reviewed':'recipe'}), \
                 patch.object(module,'run_approved',side_effect=AssertionError('execution')), \
                 patch('builtins.print'):
                result=module.main(['--plan',str(root/'plan.json'),'--ledger',str(root/'ledger.json'),
                                    '--env-file',str(root/'absent')])
            self.assertEqual(result,0)

    def test_attempt_consumed_before_auth_and_not_reusable(self):
        plan,ledger=fixture()
        prepared=self.prepare(plan,ledger)
        with tempfile.TemporaryDirectory() as directory:
            root=Path(directory)
            output=root/'.local'/'reload-only'/plan['attempt_id']
            def blocked(*args,**options):
                self.assertTrue((output/'consumed.json').is_file())
                retained=json.loads((output/'ledger.json').read_text())
                self.assertEqual(retained['apps']['pending-'+plan['attempt_id']]['reservation_usd'],'8')
                raise ValueError('parent unavailable')
            with patch.object(module,'_run_approved',side_effect=blocked) as live, \
                 patch.object(module,'refresh_billing',return_value=copy.deepcopy(ledger)):
                with self.assertRaises(ValueError):
                    module.run_approved(plan,prepared,ledger,root/'absent',output,root)
                with self.assertRaises(FileExistsError):
                    module.run_approved(plan,prepared,ledger,root/'absent',output,root)
                self.assertEqual(live.call_count,1)
            self.assertEqual(json.loads((output/'launch-failure.json').read_text())['error_type'],'ValueError')

    def test_volume_collision_is_never_adopted_or_deleted(self):
        self.live_admission_failure(collision=True)

    def test_partial_app_start_failure_stops_owned_app_and_deletes_owned_volume(self):
        self.live_admission_failure(collision=False)

    def live_admission_failure(self, *, collision):
        plan,ledger=fixture()
        prepared=self.prepare(plan,ledger)
        with tempfile.TemporaryDirectory() as directory:
            root=Path(directory);output=root/'.local'/'reload-only'/plan['attempt_id']
            volume=SimpleNamespace(object_id='vo-created',hydrate=Mock())
            create=Mock(side_effect=ValueError('existing') if collision else None)
            delete=Mock()
            sdk=SimpleNamespace(__version__='1.6.1',
                Secret=SimpleNamespace(from_dict=Mock(return_value='ephemeral')),
                Image=SimpleNamespace(from_id=Mock(return_value='seed')),
                Volume=SimpleNamespace(from_name=Mock(return_value=volume),
                    objects=SimpleNamespace(create=create,delete=delete)))
            app=SimpleNamespace(app_id=None)
            def enter():
                app.app_id='ap-owned'
                raise TimeoutError('partial app start')
            context=SimpleNamespace(__enter__=enter,__exit__=Mock())
            app.run=Mock(return_value=context)
            functions=SimpleNamespace(app=app,source=prepared['source'],checkpoint=prepared['checkpoint'],
                checkpoint_key=module.RECEIPT,original_container_id=module.ORIGINAL_CONTAINER_ID,
                stage_source=Mock(),restage_checkpoint=Mock(),verify=Mock(),publish_report=Mock())
            image=SimpleNamespace(image=SimpleNamespace(object_id='im-fake'),recipe_sha256=prepared['recipe_sha256'])
            values={'CLOUDFLARE_R2_URL':'https://'+'a'*32+'.r2.cloudflarestorage.com'}
            calls=[]
            def runner(argv,**options):
                calls.append(argv)
                return SimpleNamespace(returncode=0,stdout=b'[]',stderr=b'')
            with patch.object(module,'refresh_billing',side_effect=lambda value,*a,**kw:value), \
                 patch.object(module,'selected_env',return_value=values), \
                 patch.object(module,'parent_credentials',return_value=('dummy-access','dummy-secret')), \
                 patch.object(module,'prepare_image',return_value=image), \
                 patch.object(module,'create_reload_functions',return_value=functions), \
                 patch.object(module,'create_image_gate',return_value=Mock()):
                with self.assertRaises(ValueError if collision else TimeoutError):
                    module.run_approved(plan,prepared,ledger,root/'absent',output,root,sdk=sdk,runner=runner)
            create.assert_called_once_with(plan['storage']['input_volume'],environment_name='main',allow_existing=False)
            functions.stage_source.spawn.assert_not_called()
            retained=json.loads((output/'ledger.json').read_text())
            outcome=json.loads((output/'outcome.json').read_text())
            self.assertEqual(module.historical_total(retained),29)
            if collision:
                delete.assert_not_called();volume.hydrate.assert_not_called();app.run.assert_not_called()
                self.assertEqual(outcome['cleanup_error_type'],'RuntimeError')
                self.assertTrue(json.loads((output/'resource-ownership.json').read_text())['orphan_possible'])
            else:
                delete.assert_called_once_with(plan['storage']['input_volume'],environment_name='main',allow_missing=False)
                self.assertIn('ap-owned',retained['apps'])
                self.assertEqual(retained['apps']['ap-owned']['reservation_usd'],'8')
                self.assertTrue(outcome['cleanup']['volume_deleted'])
                self.assertTrue(any('stop' in argv and 'ap-owned' in argv for argv in calls))

    def test_nested_phase_timer_cannot_extend_whole_attempt(self):
        with patch.object(module.signal,'getsignal',return_value='outer-handler'), \
             patch.object(module.signal,'getitimer',return_value=(10,0)), \
             patch.object(module.signal,'signal'), \
             patch.object(module.signal,'setitimer') as timer, \
             patch.object(module.time,'monotonic',side_effect=(100,101)):
            with module.hard_deadline(60):pass
        self.assertEqual(timer.call_args_list[0].args,(module.signal.ITIMER_REAL,10))
        self.assertEqual(timer.call_args_list[-1].args,(module.signal.ITIMER_REAL,9,0))

    def test_provider_metadata_nonzero_or_oversized_fails_closed(self):
        for response in (SimpleNamespace(returncode=1,stdout=b'[]'),
                         SimpleNamespace(returncode=0,stdout=b'x'*65)):
            with self.assertRaises(RuntimeError):
                module.cli_json(['fake'],lambda *a,**k:response,64)


if __name__=='__main__':unittest.main()
