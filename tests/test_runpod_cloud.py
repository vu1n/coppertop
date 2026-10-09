"""Local contract tests. Fake capacity/processes/S3 never establish a live runtime."""
from copy import deepcopy
from datetime import datetime,timezone,timedelta
import io
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import Mock,patch

from coppertop.chords import LocalStore,canonical,digest,object_path
from coppertop import runpod_cloud as cloud, runpod_worker as worker, runpod_runtime as runtime
from coppertop.runpod_evidence import Evidence
from coppertop.runpod_staging import RunpodVolumeConfig
from coppertop.reload_launch import read_json
from coppertop.record_archive import seal_directory,read_completion
from coppertop.s3 import S3Store
from test_record_archive import DiskS3

NOW=datetime(2026,10,8,18,0,tzinfo=timezone.utc)


def inputs():
    from coppertop.image_check import pinned_dependencies
    root=Path(cloud.__file__).resolve().parents[1]
    config={'run_id':'mechanism-1234567890abcdef','image':'example/coppertop@sha256:'+'a'*64,
            'runtime_export_sha256':'b'*64,'cpu_admission_sha256':'c'*64,
            'datacenter':'US-WA-1','gpu':'NVIDIA RTX 6000 Ada Generation','r2_bucket':'coppertop',
            'retention':{'max_bytes':2*1024**3,'accept_ongoing_storage_charges':True,'cleanup_owner':'test operator','release_condition':'verified_publication_or_explicit_recovery_or_deletion'},
            'additional_cap_usd':'25', 'prices':{'checked_utc':NOW.isoformat(),'gpu_hour_usd':'0.84',
                'container_gb_month_usd':'0.1','volume_gb_month_usd':'0.07','r2_gb_month_usd':'0.015',
                'r2_request_reservation_usd':'4.86','transfer_reservation_usd':'1'}}
    p=cloud.prepare(config)
    cpu={'status':'passed','cuda_executed':False,'render_reset_executed':False,
         'runtime_export_sha256':config['runtime_export_sha256'],'worker_sha256':p['plan']['worker_sha256'],
         'simulator':{'status':'passed'},'model_checksums_verified':True,'full_sources_verified':True,
         'system_libraries_loaded':True,'native':dict.fromkeys(('active_closure_checked','pip_check','imports_verified','cuda_abi_verified'),True)}
    cpu['native'].update(status='passed',versions=dict(pinned_dependencies(root/'requirements-gpu-smoke.lock',root/'requirements-r2.txt'),
        torch='2.8.0+cu128',torchvision='0.23.0+cu128',**{'flash-attn':'2.8.3'}))
    config['cpu_admission_sha256']=digest(canonical(cpu))
    return cloud.prepare(config),cpu


class API:
    def __init__(self):
        self.values={'pods':{},'networkvolumes':{}};self.calls=[];self.lose=None;self.refuse_delete=False
    def request(self,method,resource,payload=None):
        self.calls.append((method,resource,deepcopy(payload)))
        kind,_,identity=resource.partition('/')
        if method=='GET': return deepcopy(self.values[kind].get(identity)) if identity else list(deepcopy(self.values[kind]).values())
        if method=='POST':
            value=dict(payload,id='pod-1' if kind=='pods' else 'volume-1')
            if kind=='pods': value['costPerHr']='0.84'
            self.values[kind][value['id']]=value
            if self.lose==kind: raise ConnectionError('Simulated lost create response')
            return value
        if method=='DELETE':
            if self.refuse_delete and kind=='pods': raise ConnectionError('Unavailable deletion')
            self.values[kind].pop(identity,None)

    def quote(self,gpu,datacenter):
        self.calls.append(('QUOTE','catalog',None))
        return {'gpu':gpu,'datacenter':datacenter,'gpu_hour_usd':'0.84','checked_utc':NOW.isoformat()}


class ControllerTests(unittest.TestCase):
    def fixture(self,directory):
        prepared,cpu=inputs();api=API()
        evidence=Mock();evidence.wait_receipt.return_value={'status':'passed','pod_id':'pod-1','archive':{'sha256':'f'*64,'size':10240},'durable_archive_verified':True}
        evidence.publish.return_value={'report_key':'d'*64,'archive':evidence.wait_receipt.return_value['archive'],'archive_verified':True}
        evidence.recovery_receipt.side_effect=FileNotFoundError('No sealed record')
        factory=Mock(return_value=evidence)
        publisher=LocalStore(Path(directory)/'r2');publisher.bucket='coppertop';publisher.prefix=prepared['plan']['r2_prefix']
        publisher.session_expires_utc=NOW+timedelta(hours=1)
        return prepared,cpu,api,evidence,factory,publisher

    def execute(self,root,fixture):
        p,cpu,api,e,factory,publisher=fixture
        return cloud.execute(p,p['plan_sha256'],Path(root)/'controller',api=api,evidence=factory,
                             publisher=publisher,cpu_receipt=cpu,now=NOW)

    def test_success_terminates_before_publish_and_removes_volume(self):
        with tempfile.TemporaryDirectory() as root:
            fixture=self.fixture(root);p,cpu,api,e,factory,publisher=fixture
            def publish(*args):
                self.assertFalse(api.values['pods']);self.assertTrue(api.values['networkvolumes']);return e.publish.return_value
            e.publish.side_effect=publish
            result=self.execute(root,fixture)
            self.assertEqual(result['status'],'passed');self.assertEqual(result['cleanup'],{'pod_absent':True,'volume_absent':True})
            self.assertFalse(api.values['pods']);self.assertFalse(api.values['networkvolumes'])
            e.abort_incomplete.assert_called_once();self.assertFalse(result['holds_released'])
            pod=next(payload for method,resource,payload in api.calls if method=='POST' and resource=='pods')
            self.assertFalse(pod['interruptible']);self.assertEqual(pod['ports'],[])
            self.assertEqual(set(pod['env']),{'COPPERTOP_RUN_ID','COPPERTOP_PLAN_SHA256','COPPERTOP_RUNTIME_IMAGE'})
            self.assertEqual(p['plan']['worker']['acceptance']['limits']['query_calls'],38)
            with self.assertRaises(FileExistsError):self.execute(root,fixture)
            self.assertEqual(sum(call[:2]==('POST','pods') for call in api.calls),1)

    def test_lost_create_response_reconciles_without_relaunch(self):
        for phase in ('pods','networkvolumes'):
            with self.subTest(phase=phase),tempfile.TemporaryDirectory() as root:
                fixture=self.fixture(root);fixture[2].lose=phase
                result=self.execute(root,fixture)
                self.assertEqual(result['status'],'failed');self.assertFalse(fixture[2].values['pods'])
                self.assertEqual(bool(fixture[2].values['networkvolumes']),phase=='pods')
                self.assertTrue(result['cleanup'].get('stage_retained') if phase=='pods' else result['cleanup'].get('volume_absent'))
                self.assertIn('recovery_report_key',result)

    def test_cleanup_failure_preserves_volume_and_can_recover(self):
        with tempfile.TemporaryDirectory() as root:
            fixture=self.fixture(root);fixture[2].refuse_delete=True
            result=self.execute(root,fixture)
            self.assertEqual(result['status'],'failed');self.assertIn('pod_error',result['cleanup'])
            self.assertTrue(fixture[2].values['networkvolumes']);fixture[3].publish.assert_not_called()
            fixture[2].refuse_delete=False
            outcome=cloud.cleanup(fixture[2],fixture[4],result,fixture[0],Path(root)/'controller/state.json')
            self.assertTrue(outcome['stage_retained'])
            outcome=cloud.cleanup(fixture[2],fixture[4],result,fixture[0],Path(root)/'controller/state.json',delete_unpublished=True)
            self.assertTrue(outcome['volume_absent'])
            self.assertEqual(sum(call[:2]==('POST','pods') for call in fixture[2].calls),1)

    def test_publication_failure_retains_source_and_only_explicit_deletion_aborts(self):
        with tempfile.TemporaryDirectory() as root:
            fixture=self.fixture(root)
            fixture[3].publish.side_effect=ValueError('Bad evidence')
            fixture[3].abort_incomplete.side_effect=ConnectionError('Abort unavailable')
            result=self.execute(root,fixture)
            self.assertEqual(result['status'],'failed');self.assertTrue(result['cleanup']['stage_retained'])
            self.assertEqual(result['runtime_status'],'passed');self.assertFalse(result['publication_verified'])
            self.assertEqual(result['cleanup']['monthly_allocated_volume_usd'],'0.21')
            self.assertTrue(result['cleanup']['ongoing_storage_charges_accepted'])
            fixture[3].abort_incomplete.assert_not_called();self.assertIn('recovery_report_key',result)
            cleanup=cloud.cleanup(fixture[2],fixture[4],result,fixture[0],Path(root)/'controller/state.json',delete_unpublished=True)
            self.assertIn('multipart_error',cleanup);self.assertTrue(cleanup['volume_absent'])

    def test_timeout_recovers_sealed_evidence_before_volume_delete(self):
        with tempfile.TemporaryDirectory() as root:
            fixture=self.fixture(root);p,cpu,api,e,factory,publisher=fixture
            e.wait_receipt.side_effect=TimeoutError('Ready marker unavailable')
            e.recovery_receipt.side_effect=None;e.recovery_receipt.return_value=dict(e.wait_receipt.return_value,status='failed')
            def publish(*args):
                self.assertFalse(api.values['pods']);self.assertTrue(api.values['networkvolumes']);return e.publish.return_value
            e.publish.side_effect=publish
            result=self.execute(root,fixture)
            self.assertEqual(result['status'],'failed');self.assertTrue(result['evidence_recovered'])
            self.assertTrue(result['cleanup']['volume_absent']);e.publish.assert_called_once()

    def test_quote_hash_receipt_and_destination_reject_before_network(self):
        for mutation in ('stale','hash','receipt','prefix','code','session'):
            with self.subTest(mutation=mutation),tempfile.TemporaryDirectory() as root:
                p,cpu,api,e,factory,publisher=self.fixture(root)
                if mutation=='receipt':cpu['cuda_executed']=True
                if mutation=='prefix':publisher.prefix='wrong'
                if mutation=='session':publisher.session_expires_utc=NOW+timedelta(minutes=20)
                if mutation=='code':p['plan']['runtime_sha256']['coppertop/runpod_worker.py']='e'*64;p['plan_sha256']=digest(canonical(p['plan']))
                approved='0'*64 if mutation=='hash' else p['plan_sha256']
                now=NOW+timedelta(hours=1) if mutation=='stale' else NOW
                with self.assertRaises(ValueError):
                    cloud.execute(p,approved,Path(root)/'out',api=api,evidence=factory,publisher=publisher,cpu_receipt=cpu,now=now)
                self.assertFalse(api.calls)

    def test_ownership_mismatch_never_deletes_unknown_resource(self):
        with tempfile.TemporaryDirectory() as root:
            fixture=self.fixture(root);p=fixture[0];api=fixture[2]
            api.values['pods']['pod-1']={'id':'pod-1','name':'unrelated'}
            api.values['networkvolumes']['volume-1']={'id':'volume-1','name':'unrelated'}
            state={'pod_id':'pod-1','volume_id':'volume-1'}
            result=cloud.cleanup(api,fixture[4],state,p,Path(root)/'state.json')
            self.assertIn('pod_error',result);self.assertFalse(any(method=='DELETE' for method,_,_ in api.calls))

    def test_unknown_runtime_and_unbudgeted_plan_are_rejected(self):
        p,cpu=inputs()
        for field,value in (('image','runpod/pytorch:latest'),('additional_cap_usd','1'),('datacenter','US-UNKNOWN')):
            config=deepcopy(p['plan']['config']);config[field]=value
            with self.assertRaises(ValueError):cloud.prepare(config)
        self.assertEqual(p['plan']['cost_bound_usd'],'16.940')

    def test_live_quote_price_increase_blocks_allocation(self):
        with tempfile.TemporaryDirectory() as root:
            fixture=self.fixture(root)
            fixture[2].quote=lambda gpu,datacenter:{'gpu':gpu,'datacenter':datacenter,'gpu_hour_usd':'1.00'}
            result=self.execute(root,fixture)
            self.assertEqual(result['status'],'failed')
            self.assertFalse(any(method=='POST' for method,_,_ in fixture[2].calls))

    def test_preparation_has_no_scientific_or_network_import(self):
        script="""import builtins
original=builtins.__import__
def guarded(name,*args,**kwargs):
    if name.split('.')[0] in ('torch','av','pyarrow','numpy','boto3'): raise AssertionError(name)
    return original(name,*args,**kwargs)
builtins.__import__=guarded
import coppertop.runpod_cloud, coppertop.runpod_runtime
"""
        process=subprocess.run([sys.executable,'-c',script],capture_output=True,timeout=30)
        self.assertEqual(process.returncode,0,process.stderr.decode())

    def test_paid_cli_is_blocked_before_credentials_without_fresh_rollout_approval(self):
        with tempfile.TemporaryDirectory() as root:
            p,_=inputs();config=Path(root)/'config.json';config.write_bytes(canonical(p['plan']['config']))
            with patch.object(sys,'argv',['runpod','--execute','--config',str(config),'--cpu-receipt',str(Path(root)/'none.json'),
                '--output',str(Path(root)/'out')]),patch('coppertop.runpod_evidence.make_transports',side_effect=AssertionError('Credentials read')),patch('sys.stderr',io.StringIO()):
                with self.assertRaises(SystemExit) as error:cloud.main()
                self.assertEqual(error.exception.code,2)

    def test_missing_retention_blocks_allocation_and_metadata_is_not_archive_completion(self):
        with tempfile.TemporaryDirectory() as root:
            p,cpu,api,e,factory,publisher=self.fixture(root)
            p['plan']['archive_retention']=None;p['plan_sha256']=digest(canonical(p['plan']))
            with self.assertRaises(ValueError):cloud.execute(p,p['plan_sha256'],Path(root)/'controller',api=api,evidence=factory,publisher=publisher,cpu_receipt=cpu,now=NOW)
            self.assertFalse(api.calls)
        with tempfile.TemporaryDirectory() as root:
            fixture=self.fixture(root);fixture[3].publish.return_value={'report_key':'d'*64}
            state=self.execute(root,fixture)
            self.assertFalse(state['publication_verified']);self.assertTrue(state['cleanup']['stage_retained'])
            self.assertIn('recovery_report_key',state)
            self.assertFalse(any(method=='DELETE' and resource.startswith('networkvolumes/') for method,resource,_ in fixture[2].calls))

    def test_lost_multipart_creation_response_reports_unresolved_upload_and_retains_source(self):
        with tempfile.TemporaryDirectory() as root:
            fixture=self.fixture(root)
            def lost(prepared,receipt,directory,publisher):
                cloud.save(Path(directory)/'r2-attempt.json',{'publication_verified':False})
                raise ConnectionError('creation response lost before upload ID was saved')
            fixture[3].publish.side_effect=lost
            state=self.execute(root,fixture)
            self.assertTrue(state['r2_upload_recovery_required']);self.assertTrue(state['r2_upload_state_unresolved'])
            self.assertTrue(state['cleanup']['stage_retained']);self.assertFalse(fixture[2].values['pods'])
            result=cloud.cleanup(fixture[2],fixture[4],state,fixture[0],Path(root)/'controller/state.json',delete_unpublished=True)
            self.assertTrue(result['stage_retained']);self.assertTrue(fixture[2].values['networkvolumes'])


class MemoryS3:
    def __init__(self):
        self.data={};self.aborted=[]
        self.meta=SimpleNamespace(endpoint_url='https://s3api-us-wa-1.runpod.io/',region_name='US-WA-1',
            config=SimpleNamespace(connect_timeout=10,read_timeout=20,retries={'total_max_attempts':1}))
    def get_object(self,Bucket,Key):
        from test_storage import S3Error
        if Key not in self.data:raise S3Error('NoSuchKey')
        raw=self.data[Key];return {'Body':io.BytesIO(raw),'ContentLength':len(raw)}
    def put_object(self,Bucket,Key,Body):self.data[Key]=Body
    def list_objects_v2(self,Bucket,Prefix,MaxKeys):
        entries=[{'Key':key,'Size':len(raw)} for key,raw in self.data.items() if key.startswith(Prefix)]
        return {'Contents':entries[:MaxKeys],'IsTruncated':len(entries)>MaxKeys}
    def list_multipart_uploads(self,**kwargs):return {'Uploads':[]}
    def abort_multipart_upload(self,**kwargs):self.aborted.append(kwargs)


class EvidenceTests(unittest.TestCase):
    def fixture(self):
        p,cpu=inputs();s3=MemoryS3();transport=Evidence(RunpodVolumeConfig('volume-1','US-WA-1',s3.meta.endpoint_url),s3)
        base=transport.base(p)
        data=b'raw native observation';key=digest(data);objects={key:len(data)}
        inv=canonical(objects);inventory=digest(inv)
        with tempfile.TemporaryDirectory() as temp:
            source=Path(temp)/'original';local=LocalStore(source/'store')
            local.put(data);local.put(inv)
            descriptor=seal_directory(source,Path(temp)/'episode.tar')
            s3.data[base+'/archive/episode.tar']=(Path(temp)/'episode.tar').read_bytes()
        record={'provider':'RunPod','run_id':p['plan']['config']['run_id'],'pod_id':'pod-1',
            'plan_sha256':p['plan_sha256'],'status':'failed','inventory':inventory,
            'object_count':2,'object_bytes':len(data)+len(inv),'result':{'status':'failed'},
            'archive':descriptor,'durable_archive_verified':True}
        s3.data[base+'/record.json']=canonical(record)
        receipt={'pod_id':'pod-1','plan_sha256':p['plan_sha256'],'status':'failed','record_sha256':digest(canonical(record)),
            'archive':descriptor['archive'],'durable_archive_verified':True}
        s3.data[base+'/ready.json']=canonical(receipt)
        return p,s3,transport,record,receipt,key

    def test_checksum_fetch_and_existing_publisher_readback(self):
        with tempfile.TemporaryDirectory() as root:
            p,s3,t,record,receipt,key=self.fixture();store=S3Store(DiskS3(Path(root)/'r2'),'coppertop',prefix=p['plan']['r2_prefix'])
            t.stage_plan(p);self.assertEqual(t.wait_receipt(p,'pod-1',1),receipt)
            report=t.publish(p,receipt,Path(root)/'fetch',store)
            self.assertEqual(read_completion(store,report['report_key'])['record'],record)
            self.assertEqual(LocalStore(Path(root)/'fetch/outputs/store').get(key),b'raw native observation')

    def test_corrupt_object_inventory_and_receipt_rejected(self):
        for mutation in ('object','descriptor','identity','outcome'):
            with self.subTest(mutation=mutation),tempfile.TemporaryDirectory() as root:
                p,s3,t,record,receipt,key=self.fixture();store=Mock()
                if mutation=='object':s3.data[t.base(p)+'/archive/episode.tar']=b'corrupt'
                if mutation=='descriptor':
                    record['archive']['manifest_sha256']='0'*64
                    s3.data[t.base(p)+'/record.json']=canonical(record);receipt['record_sha256']=digest(canonical(record))
                if mutation=='identity':receipt['pod_id']='unknown'
                if mutation=='outcome':
                    record['status']='passed';receipt['status']='passed'
                    s3.data[t.base(p)+'/record.json']=canonical(record);receipt['record_sha256']=digest(canonical(record))
                with self.assertRaises((ValueError,KeyError)):t.publish(p,receipt,Path(root)/'fetch',store)
                store.put.assert_not_called();store.put_bounded.assert_not_called()

    def test_incomplete_upload_abort_is_explicit_and_audited(self):
        _,s3,t,_,_,_=self.fixture()
        s3.list_multipart_uploads=Mock(side_effect=[{'Uploads':[{'Key':'partial','UploadId':'upload-1'}]}, {'Uploads':[]}])
        t.abort_incomplete();self.assertEqual(s3.aborted[0]['UploadId'],'upload-1')

    def test_publication_timeout_keeps_complete_cpu_evidence_and_sealed_record(self):
        with tempfile.TemporaryDirectory() as root:
            p,s3,t,record,receipt,key=self.fixture();directory=Path(root)/'fetch'
            with patch('coppertop.runpod_evidence.publish_archive',side_effect=TimeoutError('Shared publisher deadline')):
                with self.assertRaises(TimeoutError):t.publish(p,receipt,directory,Mock())
            self.assertEqual(read_json(directory/'source-record.json'),record)
            marker=read_json(directory/'fetch-complete.json')
            self.assertTrue(marker['checksums_verified']);self.assertFalse(marker['full_publication_verified'])
            self.assertEqual(LocalStore(directory/'outputs/store').get(key),b'raw native observation')

    def test_aggregate_rejects_before_publication(self):
        with tempfile.TemporaryDirectory() as root:
            p,s3,t,record,receipt,key=self.fixture();destination=Mock()
            inventory={digest(str(i).encode()):64*1024**2 for i in range(20)}
            raw=canonical(inventory);record['inventory']=digest(raw)
            record['object_count']=21;record['object_bytes']=sum(inventory.values())+len(raw)
            s3.data[t.base(p)+'/record.json']=canonical(record)
            s3.data[t.base(p)+'/outputs/store/'+object_path(record['inventory'])]=raw
            receipt['record_sha256']=digest(canonical(record))
            with self.assertRaisesRegex(ValueError,'aggregate'):t.publish(p,receipt,Path(root)/'fetch',destination)
            destination.put.assert_not_called();destination.multipart_file.assert_not_called()

    def test_explicit_resume_reuses_journal_and_publishes_only_archive_and_receipt(self):
        from coppertop.multipart import UploadInterrupted
        with tempfile.TemporaryDirectory() as root:
            p,s3,t,record,receipt,key=self.fixture();client=DiskS3(Path(root)/'r2');client.fail_part=1
            store=S3Store(client,'coppertop',prefix=p['plan']['r2_prefix']);fetch=Path(root)/'fetch'
            with patch('coppertop.s3.STREAMED_PUT_BYTES',0),self.assertRaises(UploadInterrupted):t.publish(p,receipt,fetch,store)
            journal=read_json(fetch/'upload-state.json');self.assertEqual(journal['upload_id'],'opaque-session')
            self.assertEqual(t.upload_state(p).to_dict(),journal)
            self.assertTrue((fetch/'episode.tar').is_file());self.assertTrue(read_json(fetch/'fetch-complete.json')['checksums_verified'])
            with self.assertRaisesRegex(ValueError,'Explicit'):t.publish(p,receipt,fetch,store)
            (fetch/'upload-state.json').unlink() # New CPU process can use durable volume journal.
            client.fail_part=None;result=t.publish(p,receipt,fetch,store,resume=True)
            self.assertTrue(result['archive_verified']);self.assertEqual(client.calls.count('create'),1)
            self.assertEqual(read_completion(store,result['report_key'])['record'],record)
            self.assertFalse(client.object(store.key(key)).exists())
            self.assertEqual(LocalStore(fetch/'outputs/store').get(key),b'raw native observation')

    def test_retained_size_is_bounded_and_abort_requires_absence_verification(self):
        from coppertop.runpod_evidence import abort_publication
        from coppertop.multipart import UploadState
        from coppertop.streams import ObjectRef
        from test_storage import S3Error
        p,s3,t,_,_,_=self.fixture()
        measured=t.stage_size(p);self.assertTrue(measured['within_size_authorization'])
        self.assertEqual(measured['size_bytes'],sum(len(raw) for raw in s3.data.values()))
        s3.list_objects_v2=Mock(return_value={'IsTruncated':True,'Contents':[]})
        with self.assertRaises(ValueError):t.stage_size(p)
        store=Mock();store.bucket='coppertop';store.key.return_value='owned/archive'
        upload=UploadState('coppertop/owned/archive','upload-1',ObjectRef('a'*64,10))
        store.client.list_parts.side_effect=S3Error('NoSuchUpload');abort_publication(store,upload)
        store.abort.assert_called_once_with(upload)
        store.client.list_parts.side_effect=None;store.client.list_parts.return_value={'Parts':[]}
        with self.assertRaises(ValueError):abort_publication(store,upload)


class RuntimeTests(unittest.TestCase):
    def test_failed_stage_commit_keeps_producer_and_partial_archive_without_ack(self):
        p,cpu=inputs();c=p['plan']['config']
        env={'COPPERTOP_PLAN_SHA256':p['plan_sha256'],'COPPERTOP_RUN_ID':c['run_id'],
             'COPPERTOP_RUNTIME_IMAGE':c['image'],'RUNPOD_POD_ID':'pod-1'}
        with tempfile.TemporaryDirectory() as temp,patch.dict(os.environ,env),patch.object(worker,'read_json',side_effect=lambda path,*args:cpu if str(path)=='/opt/runpod-cpu-admission.json' else json.loads(Path(path).read_bytes())):
            root=Path(temp)
            (root/'mount').mkdir()
            def runner(argv,**kwargs):
                target=Path(argv[argv.index('--output')+1]);target.mkdir()
                LocalStore(target/'store').put(b'partial native state')
                (target/'failure.json').write_bytes(canonical({'status':'failed'}))
                return SimpleNamespace(returncode=1)
            with patch.object(worker,'commit_stage',side_effect=OSError('commit failed')):
                with self.assertRaises(OSError):worker.run(p,mount=root/'mount',scratch=root,runner=runner,admit=lambda:cpu)
            base=root/'mount/coppertop/runs'/c['run_id']
            self.assertFalse((base/'ready.json').exists());self.assertTrue((base/'archive/episode.tar').is_file())
            self.assertTrue((root/(c['run_id']+'-episode.tar')).is_file())
            self.assertTrue((root/(c['run_id']+'-outputs')/'failure.json').is_file())
    def test_contract_keeps_full_checkouts_two_environments_and_no_archives(self):
        spec=runtime.contract()
        self.assertFalse(spec['archive_required']);self.assertEqual(spec['simulator']['pin_count'],129)
        self.assertEqual(spec['model_bytes']+spec['extracted_asset_bytes'],34185733733)
        self.assertEqual(set(spec['source_pins']),{'xiaomi','robocasa','robosuite'})

    def test_offline_complete_inventory_rejects_missing_changed_and_escape(self):
        with tempfile.TemporaryDirectory() as root:
            root=Path(root)
            with self.assertRaises(ValueError):runtime.inventory(root)
            spec=runtime.contract()
            for name in [*spec['required_trees'],'debs']:(root/name).mkdir(parents=True,exist_ok=True)
            for name in spec['required_files']:
                path=root/name;path.parent.mkdir(parents=True,exist_ok=True);path.write_bytes(b'fixture')
            (root/'debs/system.deb').write_bytes(b'fixture')
            inventory=runtime.inventory(root);runtime.inventory(root,verify=inventory)
            (root/'work/sim/bin/python').write_bytes(b'changed')
            with self.assertRaises(ValueError):runtime.inventory(root,verify=inventory)
            (root/'work/sim/escape').symlink_to('/etc/passwd')
            with self.assertRaisesRegex(ValueError,'escapes'):runtime.inventory(root)

    def test_worker_seals_compact_full_inventory_and_strips_credentials(self):
        p,cpu=inputs()
        env={'COPPERTOP_PLAN_SHA256':p['plan_sha256'],'COPPERTOP_RUN_ID':p['plan']['config']['run_id'],
             'COPPERTOP_RUNTIME_IMAGE':p['plan']['config']['image'],'RUNPOD_POD_ID':'pod-1',
             'RUNPOD_API_KEY':'must-never-reach-gpu-child','R2_TEMP_SECRET_ACCESS_KEY':'must-never-reach-gpu-child'}
        with tempfile.TemporaryDirectory() as root,patch.dict(os.environ,env),patch.object(worker,'read_json',side_effect=lambda path,*args:cpu if str(path)=='/opt/runpod-cpu-admission.json' else json.loads(Path(path).read_bytes())):
            root=Path(root)
            physical=root/'physical';physical.mkdir()
            (root/'mount').symlink_to(physical,target_is_directory=True)
            def runner(argv,**kwargs):
                self.assertNotIn('RUNPOD_API_KEY',kwargs['env']);self.assertNotIn('R2_TEMP_SECRET_ACCESS_KEY',kwargs['env'])
                self.assertIn('--full',argv);self.assertEqual(kwargs['timeout'],560)
                target=Path(argv[argv.index('--output')+1]);target.mkdir()
                local=LocalStore(target/'store')
                for i in range(1100):local.put(str(i).encode())
                (target/'result.json').write_bytes(canonical({'status':'recording_acceptance'}))
                return SimpleNamespace(returncode=0)
            receipt=worker.run(p,mount=root/'mount',scratch=root,runner=runner,admit=lambda:cpu)
            base=root/'mount/coppertop/runs'/p['plan']['config']['run_id']
            record=json.loads((base/'record.json').read_bytes())
            self.assertNotIn('objects',record);self.assertEqual(record['object_count'],1101)
            self.assertLess(len(canonical(receipt)),1024);self.assertEqual(receipt['record_sha256'],digest(canonical(record)))
            from coppertop.record_archive import unpack_archive
            with self.assertRaisesRegex(ValueError,'Symlink path rejected'):
                unpack_archive(base/'archive/episode.tar',record['archive'],root/'rejected')
            restored=unpack_archive(physical/'coppertop/runs'/p['plan']['config']['run_id']/'archive/episode.tar',record['archive'],root/'restored')
            self.assertTrue((restored/'result.json').is_file())
            with self.assertRaises(ValueError):worker.run(p,mount=root/'mount',scratch=root,runner=runner,admit=lambda:cpu)

    def test_worker_still_rejects_symlink_below_trusted_mount_root(self):
        p,cpu=inputs();c=p['plan']['config']
        env={'COPPERTOP_PLAN_SHA256':p['plan_sha256'],'COPPERTOP_RUN_ID':c['run_id'],
             'COPPERTOP_RUNTIME_IMAGE':c['image'],'RUNPOD_POD_ID':'pod-1'}
        with tempfile.TemporaryDirectory() as temp,patch.dict(os.environ,env),patch.object(worker,'read_json',side_effect=lambda path,*args:cpu if str(path)=='/opt/runpod-cpu-admission.json' else json.loads(Path(path).read_bytes())):
            root=Path(temp);physical=root/'physical';physical.mkdir();mount=root/'mount';mount.symlink_to(physical)
            base=physical/'coppertop/runs'/c['run_id'];base.mkdir(parents=True)
            outside=root/'outside';outside.mkdir();(base/'archive').symlink_to(outside)
            def runner(argv,**kwargs):
                target=Path(argv[argv.index('--output')+1]);target.mkdir()
                (target/'result.json').write_bytes(canonical({'status':'recording_acceptance'}))
                return SimpleNamespace(returncode=0)
            with self.assertRaisesRegex(ValueError,'Symlink path rejected'):
                worker.run(p,mount=mount,scratch=root,runner=runner,admit=lambda:cpu)
            self.assertFalse(list(outside.iterdir()));self.assertFalse((base/'ready.json').exists())
            self.assertTrue((root/(c['run_id']+'-episode.tar')).is_file())


if __name__=='__main__':unittest.main()
