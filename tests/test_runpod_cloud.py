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

NOW=datetime(2026,10,8,18,0,tzinfo=timezone.utc)


def inputs():
    from coppertop.image_check import pinned_dependencies
    root=Path(cloud.__file__).resolve().parents[1]
    config={'run_id':'mechanism-1234567890abcdef','image':'example/coppertop@sha256:'+'a'*64,
            'runtime_export_sha256':'b'*64,'cpu_admission_sha256':'c'*64,
            'datacenter':'US-WA-1','gpu':'NVIDIA RTX 6000 Ada Generation','r2_bucket':'coppertop',
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
        evidence=Mock();evidence.wait_receipt.return_value={'status':'passed','pod_id':'pod-1'}
        evidence.publish.return_value='d'*64
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
                self.assertFalse(api.values['pods']);self.assertTrue(api.values['networkvolumes']);return 'd'*64
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
                self.assertEqual(result['status'],'failed');self.assertTrue(result['cleanup']['volume_absent'])
                self.assertFalse(fixture[2].values['pods']);self.assertFalse(fixture[2].values['networkvolumes'])
                self.assertIn('recovery_report_key',result)

    def test_cleanup_failure_preserves_volume_and_can_recover(self):
        with tempfile.TemporaryDirectory() as root:
            fixture=self.fixture(root);fixture[2].refuse_delete=True
            result=self.execute(root,fixture)
            self.assertEqual(result['status'],'failed');self.assertIn('pod_error',result['cleanup'])
            self.assertTrue(fixture[2].values['networkvolumes']);fixture[3].publish.assert_not_called()
            fixture[2].refuse_delete=False
            outcome=cloud.cleanup(fixture[2],fixture[4],result,fixture[0],Path(root)/'controller/state.json')
            self.assertTrue(outcome['volume_absent'])
            self.assertEqual(sum(call[:2]==('POST','pods') for call in fixture[2].calls),1)

    def test_publication_and_upload_abort_failure_still_delete_volume(self):
        with tempfile.TemporaryDirectory() as root:
            fixture=self.fixture(root)
            fixture[3].publish.side_effect=ValueError('Bad evidence')
            fixture[3].abort_incomplete.side_effect=ConnectionError('Abort unavailable')
            result=self.execute(root,fixture)
            self.assertEqual(result['status'],'failed');self.assertIn('multipart_error',result['cleanup'])
            self.assertTrue(result['cleanup']['volume_absent']);self.assertIn('recovery_report_key',result)

    def test_timeout_recovers_sealed_evidence_before_volume_delete(self):
        with tempfile.TemporaryDirectory() as root:
            fixture=self.fixture(root);p,cpu,api,e,factory,publisher=fixture
            e.wait_receipt.side_effect=TimeoutError('Ready marker unavailable')
            e.recovery_receipt.side_effect=None;e.recovery_receipt.return_value={'status':'failed'}
            def publish(*args):
                self.assertFalse(api.values['pods']);self.assertTrue(api.values['networkvolumes']);return 'd'*64
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

    def test_paid_cli_is_blocked_before_credentials_until_shared_publication_is_validated(self):
        with tempfile.TemporaryDirectory() as root:
            p,_=inputs();config=Path(root)/'config.json';config.write_bytes(canonical(p['plan']['config']))
            with patch.object(sys,'argv',['runpod','--execute','--config',str(config),'--cpu-receipt',str(Path(root)/'none.json'),
                '--output',str(Path(root)/'out')]),patch('coppertop.runpod_evidence.make_transports',side_effect=AssertionError('Credentials read')),patch('sys.stderr',io.StringIO()):
                with self.assertRaises(SystemExit) as error:cloud.main()
                self.assertEqual(error.exception.code,2)


class MemoryS3:
    def __init__(self):
        self.data={};self.aborted=[]
        self.meta=SimpleNamespace(endpoint_url='https://s3api-us-wa-1.runpod.io/',region_name='US-WA-1',
            config=SimpleNamespace(connect_timeout=10,read_timeout=20,retries={'total_max_attempts':1}))
    def get_object(self,Bucket,Key):
        raw=self.data[Key];return {'Body':io.BytesIO(raw),'ContentLength':len(raw)}
    def put_object(self,Bucket,Key,Body):self.data[Key]=Body
    def list_multipart_uploads(self,**kwargs):return {'Uploads':[]}
    def abort_multipart_upload(self,**kwargs):self.aborted.append(kwargs)


class EvidenceTests(unittest.TestCase):
    def fixture(self):
        p,cpu=inputs();s3=MemoryS3();transport=Evidence(RunpodVolumeConfig('volume-1','US-WA-1',s3.meta.endpoint_url),s3)
        base=transport.base(p)
        data=b'raw native observation';key=digest(data);objects={key:len(data)}
        inv=canonical(objects);inventory=digest(inv)
        record={'provider':'RunPod','run_id':p['plan']['config']['run_id'],'pod_id':'pod-1',
            'plan_sha256':p['plan_sha256'],'status':'failed','inventory':inventory,
            'object_count':2,'object_bytes':len(data)+len(inv),'result':{'status':'failed'}}
        s3.data[base+'/record.json']=canonical(record)
        s3.data[base+'/outputs/store/'+object_path(key)]=data
        s3.data[base+'/outputs/store/'+object_path(inventory)]=inv
        receipt={'pod_id':'pod-1','plan_sha256':p['plan_sha256'],'status':'failed','record_sha256':digest(canonical(record))}
        s3.data[base+'/ready.json']=canonical(receipt)
        return p,s3,transport,record,receipt,key

    def test_checksum_fetch_and_existing_publisher_readback(self):
        with tempfile.TemporaryDirectory() as root:
            p,s3,t,record,receipt,key=self.fixture();store=LocalStore(Path(root)/'r2')
            t.stage_plan(p);self.assertEqual(t.wait_receipt(p,'pod-1',1),receipt)
            report=t.publish(p,receipt,Path(root)/'fetch',store)
            self.assertEqual(store.get(report),canonical(record));self.assertEqual(store.get(key),b'raw native observation')

    def test_corrupt_object_inventory_and_receipt_rejected(self):
        for mutation in ('object','inventory','identity','outcome'):
            with self.subTest(mutation=mutation),tempfile.TemporaryDirectory() as root:
                p,s3,t,record,receipt,key=self.fixture();store=Mock()
                if mutation=='object':s3.data[t.base(p)+'/outputs/store/'+object_path(key)]=b'corrupt'
                if mutation=='inventory':s3.data[t.base(p)+'/outputs/store/'+object_path(record['inventory'])]=b'{}'
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
            with patch('coppertop.runpod_evidence.publish_directory',side_effect=TimeoutError('Shared publisher deadline')):
                with self.assertRaises(TimeoutError):t.publish(p,receipt,directory,Mock())
            self.assertEqual(read_json(directory/'source-record.json'),record)
            marker=read_json(directory/'fetch-complete.json')
            self.assertTrue(marker['checksums_verified']);self.assertFalse(marker['full_publication_verified'])
            self.assertEqual(LocalStore(directory/'store').get(key),b'raw native observation')

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
            destination.put.assert_not_called();self.assertFalse((Path(root)/'fetch').exists())


class RuntimeTests(unittest.TestCase):
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
            with self.assertRaises(ValueError):worker.run(p,mount=root/'mount',scratch=root,runner=runner,admit=lambda:cpu)


if __name__=='__main__':unittest.main()
