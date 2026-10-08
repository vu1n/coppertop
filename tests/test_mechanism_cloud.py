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
                self.assertTrue(options['read_only']);self.assertNotIn('sub_path',options)
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
