"""Local fake provider/publication tests, never allocation or credential access."""
import copy
import json
from pathlib import Path
import subprocess
import sys
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

from coppertop import mechanism_cloud as module
from coppertop.chords import LocalStore, canonical, digest
from coppertop.r2_session import SessionScope, BoundedClient, LIMITS
from coppertop.reload_launch import reconcile
from test_modal_reload import App, Volume

ATTEMPT = 'mechanism-abcdef0123456789'


def ledger():
    return {'schema_version':2,'billing_start_utc':'2026-10-01','cumulative_cap':'64.82',
        'unbounded_spend':[], 'apps':{'ap-old':{'state':'unsettled_bounded',
        'known_reported_usd':'6.09975749','reservation_usd':'58.81459248','description':'coppertop-old'}}}


class CloudTests(unittest.TestCase):
    def test_cpu_mount_preflight_reads_committed_view_and_rejects_corruption(self):
        for corrupt in (False,True):
            with self.subTest(corrupt=corrupt),tempfile.TemporaryDirectory() as root:
                physical=Path(root)/'physical';physical.mkdir()
                alias=Path(root)/'alias';alias.symlink_to(physical,target_is_directory=True)
                events=[]
                def reload():
                    events.append('reload')
                    if corrupt:(physical/'mount-preflight'/'episode.tar').write_bytes(b'corrupt')
                action=lambda:module.archive_mount_preflight(alias.resolve(strict=True),ATTEMPT,
                    commit=lambda:events.append('commit'),reload=reload)
                if corrupt:
                    with self.assertRaisesRegex(ValueError,'Archive checksum'):action()
                else:
                    receipt=action();self.assertTrue(receipt['verified'])
                    self.assertEqual(receipt['source_bytes'],4096);self.assertLessEqual(receipt['archive_bytes'],32768)
                self.assertEqual(events,['commit','reload'])
                self.assertTrue((physical/'mount-preflight'/'episode.tar').exists())

    def test_full_baseline_refuses_gpu_without_verified_mount_preflight(self):
        from test_record_archive import POLICY
        value=ledger();value['cumulative_cap']='70.82'
        prepared=module.prepare(value,ATTEMPT,'official_recording_full','A100-80GB',POLICY)
        functions=SimpleNamespace(gate='gate',evaluate='evaluate',publish='publish')
        specifications=module.create_functions(SimpleNamespace(__version__='1.6.1',App=App),
            prepared,object(),Volume([]),Mock(),[]).app.definitions
        self.assertIsNone(specifications['gate']['gpu'])
        self.assertEqual(specifications['gate']['volumes'],specifications['evaluate']['volumes'])
        for probe in ({},{'verified':False},{'verified':True,'source_bytes':4096,
                'archive_bytes':10240,'provider_commit_and_reload':False}):
            with self.subTest(probe=probe),tempfile.TemporaryDirectory() as root:
                invoked=[]
                def invoke(function,args,bound):
                    invoked.append(function)
                    return {'status':'passed','container_id':'ta-gate',
                        'plan_sha256':prepared['cloud_plan_sha256'],'archive_preflight':probe}
                with self.assertRaisesRegex(ValueError,'real-mount archive preflight'):
                    module.execute_phases(functions,prepared,root,admission=lambda:None,invoke=invoke,
                        stopped=lambda identity:True,cleanup=lambda:{'app_stopped':True,'volume_deleted':True,
                            'active_containers':0,'active_sandboxes':0})
                self.assertEqual(invoked,['gate'])
                self.assertFalse(json.loads((Path(root)/'outcome.json').read_bytes()).get('gpu_started',False))

    def test_owned_stage_release_requires_compute_audit_and_verified_data_or_empty_stage(self):
        from test_record_archive import POLICY
        audit={'app_stopped':True,'active_containers':0,'active_sandboxes':0}
        cases=[('complete',100,False,False),('unknown',None,False,False),
            ('over_limit',2*1024**3+1,False,False),('empty',0,False,True),('published',100,True,True)]
        for label,size,published,deleted in cases:
            with self.subTest(label=label),patch.object(module,'delete_owned_volume') as delete:
                volume=SimpleNamespace(iterdir=Mock(return_value=iter([SimpleNamespace(type=1,size=size)])))
                if size is None:volume.iterdir.side_effect=OSError('unknown size')
                result=module.cleanup_owned_stage(object(),volume,name='owned',identity='vo-owned',audit=audit,
                    gpu_started=True,publication_verified=published,policy=POLICY,rate='0.09')
                self.assertEqual(result['volume_deleted'],deleted)
                if deleted:delete.assert_called_once()
                else:
                    delete.assert_not_called();self.assertTrue(result['volume_retained_for_recovery'])
                    self.assertEqual(result['retention']['size_bytes'],size)
        with patch.object(module,'delete_owned_volume') as delete:
            for invalid in (None,dict(POLICY,accept_ongoing_storage_charges=False)):
                with self.assertRaises(ValueError):module.cleanup_owned_stage(object(),Mock(),name='owned',identity='vo-owned',audit=audit,
                    gpu_started=True,publication_verified=False,policy=invalid,rate='0.09')
            with self.assertRaises(ValueError):module.cleanup_owned_stage(object(),Mock(),name='owned',identity='vo-owned',
                audit=dict(audit,active_containers=1),gpu_started=True,publication_verified=True,policy=POLICY,rate='0.09')
            delete.assert_not_called()

    def test_retained_stage_metadata_measurement_and_unknown_or_over_limit_reporting(self):
        from test_record_archive import POLICY
        resource={'provider':'modal','id':'vo-owned','name':'owned'}
        volume=SimpleNamespace(iterdir=Mock(return_value=iter([
            SimpleNamespace(type=2,size=4096),SimpleNamespace(type=1,size=1024**3)])))
        report=module.retained_stage_report(volume,resource,POLICY,'0.09')
        self.assertEqual(report['size_bytes'],1024**3);self.assertEqual(report['estimated_monthly_usd'],'0.09')
        self.assertEqual(report['resource'],resource)
        volume.iterdir.side_effect=OSError('provider metadata unavailable')
        report=module.retained_stage_report(volume,resource,POLICY,'0.09')
        self.assertIsNone(report['size_bytes']);self.assertEqual(report['size_error_type'],'OSError')
        volume.iterdir.side_effect=None;volume.iterdir.return_value=iter([SimpleNamespace(type=1,size=2*1024**3+1)])
        report=module.retained_stage_report(volume,resource,POLICY,'0.09')
        self.assertFalse(report['within_size_authorization'])

    def test_full_launch_requires_explicit_storage_authorization_before_keys(self):
        from test_record_archive import POLICY
        value=ledger();value['cumulative_cap']='70.82'
        reviewed_at=module.datetime.fromisoformat(module.prepare(value,ATTEMPT,'official_recording_full')['cloud_plan']['pricing']['checked_utc'])
        with patch.object(module,'datetime',wraps=module.datetime) as clock,patch.object(module,'selected_env',side_effect=AssertionError('keys forbidden')):
            clock.now.return_value=reviewed_at
            for retention,reason in ((None,'retention policy'),(dict(POLICY,accept_ongoing_storage_charges=False),'explicitly accepted')):
                with self.assertRaisesRegex(ValueError,reason):
                    prepared=module.prepare(value,ATTEMPT,'official_recording_full',retention=retention)
                    module.execute(prepared,value,Path.cwd(),Path('unused'),prepared['cloud_plan_sha256'],
                        sdk=SimpleNamespace(__version__='1.6.1'))
            prepared=module.prepare(value,ATTEMPT,'official_recording_full',retention=POLICY)
            self.assertEqual(prepared['cloud_plan']['archive_retention'],POLICY)
            self.assertEqual(prepared['cloud_plan']['pricing']['volume_storage_usd_per_gib_month'],'0.09')
            self.assertNotIn('retention_backstop',prepared['cloud_plan'])

    def test_archive_checkpoint_and_verified_completion_gate_cleanup(self):
        from test_record_archive import POLICY
        value=ledger();value['cumulative_cap']='70.82'
        prepared=module.prepare(value,ATTEMPT,'official_recording_full',retention=POLICY)
        functions=SimpleNamespace(gate='gate',evaluate='evaluate',publish='publish')
        archive={'sha256':'a'*64,'size':10240}
        for fail in (None,'publish','staging'):
            with self.subTest(fail=fail),tempfile.TemporaryDirectory() as root:
                events=[]
                evaluation={'container_id':'ta-gpu','status':'passed','plan_sha256':prepared['cloud_plan_sha256'],
                    'archive':archive,'durable_archive_verified':fail!='staging','record_sha256':'b'*64}
                def invoke(function,args,bound):
                    events.append(function)
                    if function=='gate':return {'container_id':'ta-gate','status':'passed','plan_sha256':prepared['cloud_plan_sha256'],
                        'archive_preflight':{'verified':True,'source_bytes':4096,'archive_bytes':10240,'provider_commit_and_reload':True}}
                    if function=='evaluate':return evaluation
                    if fail=='publish':raise OSError('publication interrupted')
                    return {'container_id':'ta-pub','plan_sha256':prepared['cloud_plan_sha256'],
                        'archive':archive,'archive_verified':True,'report_key':'c'*64,
                        'source_record_sha256':digest(canonical(evaluation)),'published_status':'passed'}
                def cleanup():
                    events.append('cleanup')
                    outcome=json.loads((Path(root)/'outcome.json').read_bytes())
                    if fail:
                        self.assertFalse(outcome.get('durable_report_verified',False))
                        return {'app_stopped':True,'active_containers':0,'active_sandboxes':0,'volume_deleted':False,
                            'volume_retained_for_recovery':True,'retention':{'ongoing_charges_accepted':True}}
                    self.assertTrue(outcome['durable_report_verified'])
                    return {'app_stopped':True,'active_containers':0,'active_sandboxes':0,'volume_deleted':True}
                action=lambda:module.execute_phases(functions,prepared,root,admission=lambda:None,
                    invoke=invoke,stopped=lambda task:events.append('stopped-'+task) or True,cleanup=cleanup)
                if fail:
                    with self.assertRaises(Exception):action()
                else:self.assertTrue(action()['durable_report_verified'])
                self.assertEqual(events[-1],'cleanup')
                if fail=='staging':self.assertNotIn('publish',events)
                else:self.assertLess(events.index('stopped-ta-gpu'),events.index('publish'))

    def test_explicit_a100_is_bound_to_plan_and_provider_without_fallback(self):
        value=ledger();value['cumulative_cap']='70.82'
        original=module.prepare(value,ATTEMPT,'official_recording_full')
        selected=module.prepare(value,ATTEMPT,'official_recording_full','A100-80GB')
        self.assertNotEqual(original['cloud_plan_sha256'],selected['cloud_plan_sha256'])
        self.assertEqual(original['cloud_plan']['worker'],selected['cloud_plan']['worker'])
        self.assertEqual(selected['cloud_plan']['pricing']['a100_80gb_per_second'],'.000694')
        functions=module.create_functions(SimpleNamespace(__version__='1.6.1',App=App),
            selected,object(),Volume([]),Mock(),[])
        self.assertEqual(functions.app.definitions['evaluate']['gpu'],'A100-80GB')
        self.assertEqual(functions.app.definitions['evaluate']['retries'],0)
        self.assertEqual(functions.app.definitions['publish']['timeout'],2100)
        self.assertEqual(selected['cloud_plan']['auth']['ttl_seconds'],7200)
        self.assertGreater(selected['cloud_plan']['auth']['ttl_seconds'],selected['cloud_plan']['resources']['controller_seconds'])
        reviewed_at=module.datetime.fromisoformat(selected['cloud_plan']['pricing']['checked_utc'])
        with patch.object(module,'datetime',wraps=module.datetime) as clock:
            clock.now.return_value=reviewed_at
            with self.assertRaisesRegex(ValueError,'Modal1.6.1'):
                module.execute(selected,value,Path.cwd(),Path('unused'),selected['cloud_plan_sha256'],
                    sdk=SimpleNamespace(__version__='unsupported'))
        changed=copy.deepcopy(selected);changed['cloud_plan']['resources']['gpu']='L40S'
        with self.assertRaisesRegex(ValueError,'Exact proposed plan'):
            module.execute(changed,value,Path.cwd(),Path('unused'),selected['cloud_plan_sha256'])
        with self.assertRaises(ValueError):module.prepare(value,ATTEMPT,'official_recording_full','any')

    def test_cli_exit_reflects_terminal_status_after_publication_cleanup(self):
        # Exercise process exit as well as main's returned code; no provider path runs.
        script = """
import sys
from unittest.mock import patch
from coppertop import mechanism_cloud as module
status=sys.argv[1]
sys.argv=['mechanism_cloud','--ledger','unused','--attempt','unused','--execute']
result={'status':status,'durable_report_verified':True,
        'cleanup':{'app_stopped':True,'volume_deleted':True}}
with patch.object(module,'read_json',return_value={}), patch.object(module,'prepare',return_value={}), patch.object(module,'execute',return_value=result):
    raise SystemExit(module.main())
"""
        for status, expected in (('passed',0),('failed',1),('started',1)):
            with self.subTest(status=status):
                process=subprocess.run([sys.executable,'-c',script,status],capture_output=True,timeout=30)
                self.assertEqual(process.returncode,expected,process.stderr.decode())

    def test_preparation_never_reads_keys_or_allocates(self):
        with patch.object(module,'selected_env',side_effect=AssertionError), patch.object(module,'mint',side_effect=AssertionError):
            prepared=module.prepare(ledger(),ATTEMPT)
        self.assertFalse(prepared['cloud_plan']['paid_authorized'])
        self.assertEqual(prepared['cloud_plan']['auth']['session_count'],1)
        self.assertEqual(prepared['cloud_plan']['auth']['ttl_seconds'],3600)
        self.assertEqual(prepared['scope'].actions,('GetObject','HeadObject','PutObject'))
        self.assertEqual(prepared['cloud_plan']['additional_cap_usd'],'6')
        self.assertEqual(prepared['cloud_plan']['retained_holds_usd'],'58.81459248')
        self.assertEqual(module.prepare(ledger(),ATTEMPT)['cloud_plan_sha256'],prepared['cloud_plan_sha256'])
        with self.assertRaises(ValueError): module.prepare(ledger(),'reload-abcdef0123456789')

    def test_publisher_only_scoped_cpu_has_secret_and_gpu_has_write_volume(self):
        sdk=SimpleNamespace(__version__='1.6.1',App=App)
        functions=module.create_functions(sdk,module.prepare(ledger(),ATTEMPT),object(),Volume([]),Mock(),['ephemeral-placeholder'])
        specs=functions.app.definitions
        self.assertEqual(specs['evaluate']['gpu'],'L40S')
        self.assertEqual(specs['evaluate']['timeout'],270)
        self.assertEqual(specs['evaluate']['secrets'],[])
        self.assertTrue(specs['evaluate']['block_network'])
        self.assertEqual(specs['gate']['secrets'],[])
        self.assertIsNone(specs['publish']['gpu'])
        self.assertEqual(specs['publish']['secrets'],['ephemeral-placeholder'])
        self.assertTrue(specs['publish']['volumes'][module.MOUNT].options['read_only'])
        self.assertFalse(functions.app.options['include_source'])
        self.assertTrue(all(s['retries']==0 and s['max_containers']==1 for s in specs.values()))

    def test_cpu_publication_roundtrip_corruption_inventory_and_symlink_guards(self):
        with tempfile.TemporaryDirectory() as root:
            source=Path(root)/'output'; local=LocalStore(source/'store')
            key=local.put(b'canonical-camera-or-signal-object')
            record={'objects':{key:33},'result':{'status':'bounded_feasibility'}}
            record['objects']=module.output_inventory(source)
            destination=LocalStore(Path(root)/'r2')
            receipt=module.publish_directory(source,record,destination)
            self.assertEqual(destination.get(receipt),canonical(record))
            self.assertEqual(destination.get(key),local.get(key))
            bad=copy.deepcopy(record); bad['objects'][key]+=1
            with self.assertRaises(ValueError): module.publish_directory(source,bad,destination)
            (source/'link').symlink_to(Path(root))
            with self.assertRaises(ValueError): module.output_inventory(source)
            (source/'link').unlink()
            local.path(key).write_bytes(b'corrupt')
            with self.assertRaises(ValueError): module.output_inventory(source)

    def test_recovery_publishes_metadata_when_artifact_inventory_is_corrupt(self):
        with tempfile.TemporaryDirectory() as root:
            mount=Path(root)/'mount'; local=LocalStore(mount/ATTEMPT/'outputs'/'store')
            key=local.put(b'original'); local.path(key).write_bytes(b'corrupted')
            destination=LocalStore(Path(root)/'r2')
            prepared=module.prepare(ledger(),ATTEMPT)
            sdk=SimpleNamespace(__version__='1.6.1',App=App)
            functions=module.create_functions(sdk,prepared,object(),Volume([]),lambda role:destination,[])
            record={'recovery':True,'status':'failed','objects':{},
                'plan_sha256':prepared['cloud_plan_sha256']}
            with patch.object(module,'MOUNT',str(mount)), patch.object(module,'_container',return_value='ta-publisher'):
                receipt=functions.publish(record)
            durable=json.loads(destination.get(receipt['report_key']))
            self.assertEqual(durable['status'],'failed')
            self.assertEqual(durable['artifact_error_type'],'ValueError')
            self.assertEqual(durable['objects'],{})
            self.assertFalse(destination.path(key).exists())
            self.assertEqual(receipt['source_record_sha256'],digest(canonical(record)))

    def test_recovery_before_gpu_mount_needs_no_volume_writes(self):
        for workload in ('mechanism','official_recording_full'):
            with self.subTest(workload=workload),tempfile.TemporaryDirectory() as root:
                mount=Path(root)/'mount';mount.mkdir()
                value=ledger()
                if workload=='official_recording_full':value['cumulative_cap']='70.82'
                prepared=module.prepare(value,ATTEMPT,workload)
                destination=LocalStore(Path(root)/'r2')
                functions=module.create_functions(SimpleNamespace(__version__='1.6.1',App=App),
                    prepared,object(),Volume([]),lambda role:destination,[])
                options=functions.app.definitions['publish']['volumes'][module.MOUNT].options
                self.assertEqual(options['read_only'],workload!='official_recording_full');self.assertNotIn('sub_path',options)
                record={'recovery':True,'status':'failed','objects':{},'plan_sha256':prepared['cloud_plan_sha256']}
                native=LocalStore.__init__
                def reject_volume_writes(store,path):
                    if Path(path).is_relative_to(mount):raise PermissionError('readonly Volume')
                    native(store,path)
                with patch.object(module,'MOUNT',str(mount)),patch.object(module,'_container',return_value='ta-publisher'),patch.object(LocalStore,'__init__',reject_volume_writes):
                    receipt=functions.publish(record)
                result=json.loads(destination.get(receipt['report_key']))
                self.assertEqual(result['status'],'failed');self.assertEqual(list(mount.iterdir()),[])
                if workload=='official_recording_full':self.assertEqual(result['object_count'],1)

    def test_publication_failure_and_cpu_gate_failure_always_cleanup(self):
        prepared=module.prepare(ledger(),ATTEMPT)
        for failure in ('gate','publish',None):
            with self.subTest(failure=failure), tempfile.TemporaryDirectory() as root:
                functions=SimpleNamespace(gate='gate',evaluate='evaluate',publish='publish')
                events=[]
                evaluation={'container_id':'ta-gpu','plan_sha256':prepared['cloud_plan_sha256'],'status':'passed'}
                def invoke(function,args,bound):
                    events.append(function)
                    if function=='gate': return {'container_id':'ta-cpu','plan_sha256':prepared['cloud_plan_sha256'],'status':'failed' if failure=='gate' else 'passed'}
                    if function=='evaluate': return evaluation
                    if failure=='publish': raise RuntimeError('readback failed')
                    return {'container_id':'ta-publisher','plan_sha256':prepared['cloud_plan_sha256'],
                        'report_key':digest(canonical(evaluation)), 'source_record_sha256':digest(canonical(evaluation)),
                        'published_status':'passed'}
                def cleanup():
                    events.append('cleanup'); return {'app_stopped':True,'volume_deleted':True,'active_containers':0,'active_sandboxes':0}
                call=lambda:module.execute_phases(functions,prepared,Path(root),admission=lambda:events.append('admit'),
                    invoke=invoke,stopped=lambda identity:True,cleanup=cleanup)
                if failure: 
                    with self.assertRaises(Exception): call()
                else: self.assertTrue(call()['durable_report_verified'])
                self.assertEqual(events[-1],'cleanup')
                if failure=='gate': self.assertNotIn('evaluate',events)

    def test_compact_receipt_binds_publication_to_committed_record_hash(self):
        prepared=module.prepare(ledger(),ATTEMPT)
        functions=SimpleNamespace(gate='gate',evaluate='evaluate',publish='publish')
        committed={'status':'passed','objects':{},'result':{'status':'recording_acceptance'}}
        receipt={'container_id':'ta-gpu','plan_sha256':prepared['cloud_plan_sha256'],
            'attempt_id':ATTEMPT,'status':'passed','record_sha256':digest(canonical(committed))}
        for corrupt in (False,True):
            with self.subTest(corrupt=corrupt),tempfile.TemporaryDirectory() as root:
                events=[]
                def invoke(function,args,bound):
                    events.append(function)
                    if function=='gate':return {'container_id':'ta-cpu','status':'passed','plan_sha256':prepared['cloud_plan_sha256']}
                    if function=='evaluate':return receipt
                    self.assertEqual(args,(receipt,))
                    return {'container_id':'ta-pub','plan_sha256':prepared['cloud_plan_sha256'],
                        'published_status':'passed','source_record_sha256':digest(canonical(receipt)),
                        'report_key':'0'*64 if corrupt else receipt['record_sha256']}
                def cleanup():
                    events.append('cleanup')
                    return {'app_stopped':True,'volume_deleted':True,'active_containers':0,'active_sandboxes':0}
                def execute():return module.execute_phases(functions,prepared,Path(root),admission=lambda:None,
                    invoke=invoke,stopped=lambda _:True,cleanup=cleanup)
                if corrupt:
                    with self.assertRaisesRegex(ValueError,'Publication receipt'):execute()
                else:self.assertEqual(execute()['status'],'passed')
                self.assertEqual(events,['gate','evaluate','publish','cleanup'])

    def test_cleanup_failure_cannot_be_reported_as_pass(self):
        prepared=module.prepare(ledger(),ATTEMPT)
        with tempfile.TemporaryDirectory() as root:
            functions=SimpleNamespace(gate='gate',evaluate='evaluate',publish='publish')
            with self.assertRaises(ValueError):
                module.execute_phases(functions,prepared,Path(root),admission=lambda:None,
                    invoke=lambda *args:{'container_id':'ta-cpu','status':'failed','plan_sha256':prepared['cloud_plan_sha256']},
                    stopped=lambda _:True,cleanup=lambda:{'app_stopped':True,'volume_deleted':True,'active_containers':1,'active_sandboxes':0})
            self.assertEqual(json.loads((Path(root)/'outcome.json').read_text())['status'],'failed')

    def test_exact_approval_rejected_before_credential_access(self):
        prepared=module.prepare(ledger(),ATTEMPT)
        with patch.object(module,'selected_env',side_effect=AssertionError):
            with self.assertRaises(ValueError): module.execute(prepared,ledger(),Path.cwd(),Path('unused'),'wrong')
            prepared['cloud_plan']['resources']['gpu']='H100'
            with self.assertRaises(ValueError): module.execute(prepared,ledger(),Path.cwd(),Path('unused'),prepared['cloud_plan_sha256'])

    def test_failed_gpu_rpc_publishes_failure_once_before_cleanup(self):
        prepared=module.prepare(ledger(),ATTEMPT)
        functions=SimpleNamespace(gate='gate',evaluate='evaluate',publish='publish')
        events=[]
        def invoke(function,args,bound):
            events.append(function)
            if function=='evaluate': raise TimeoutError('bounded provider timeout')
            if function=='gate': return {'container_id':'ta-cpu','status':'passed','plan_sha256':prepared['cloud_plan_sha256']}
            record=args[0]
            self.assertTrue(record['recovery']); self.assertEqual(record['status'],'failed')
            return {'container_id':'ta-pub','plan_sha256':prepared['cloud_plan_sha256'],
                'source_record_sha256':digest(canonical(record)), 'published_status':'failed', 'report_key':'a'*64}
        def cleanup():
            events.append('cleanup')
            return {'app_stopped':True,'volume_deleted':True,'active_containers':0,'active_sandboxes':0}
        with tempfile.TemporaryDirectory() as root:
            result=module.execute_phases(functions,prepared,Path(root),admission=lambda:None,
                invoke=invoke,stopped=lambda _:True,cleanup=cleanup)
        self.assertEqual(result['status'],'failed'); self.assertTrue(result['durable_report_verified'])
        self.assertEqual(events,['gate','evaluate','publish','cleanup'])

    def test_reconciliation_preserves_hold_and_never_treats_charge_as_release(self):
        updated,cost=reconcile(ledger(),[],additional_cap=6)
        self.assertEqual(updated['apps']['ap-old']['reservation_usd'],'58.81459248')
        self.assertEqual(cost,'0')
        over=ledger(); over['apps']['ap-old']['reservation_usd']='59'
        with self.assertRaises(ValueError): reconcile(over,[],additional_cap=6)

    def test_new_session_cannot_escape_prefix_or_delete_or_exceed_bytes(self):
        scope=SessionScope('mechanism','coppertop','chords/mechanism/'+ATTEMPT)
        client=Mock(); endpoint='https://'+'a'*32+'.r2.cloudflarestorage.com'
        wrapped=BoundedClient(client,scope,endpoint,100,clock=lambda:1)
        key=scope.prefix+'/objects/'+digest(b'payload')[:2]+'/'+digest(b'payload')
        wrapped.put_object(Bucket='coppertop',Key=key,Body=b'payload',IfNoneMatch='*')
        self.assertEqual(client.put_object.call_args.kwargs['StorageClass'],'STANDARD')
        with self.assertRaises(ValueError): wrapped.get_object(Bucket='coppertop',Key='chords/objects/aa/'+'a'*64)
        with self.assertRaises(ValueError): wrapped._call('DeleteObject',{'Bucket':'coppertop','Key':key})
        wrapped.write_bytes=LIMITS['mechanism'][1]
        with self.assertRaises(ValueError): wrapped.put_object(Bucket='coppertop',Key=key,Body=b'x',IfNoneMatch='*')
        with self.assertRaises(ValueError): SessionScope('mechanism','coppertop','chords/mechanism/other')


if __name__=='__main__':unittest.main()
