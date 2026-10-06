import hashlib
import json
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch
from coppertop.chords import canonical
from coppertop.gpu_launch import launch, authorize, RELIABILITY_APPROVAL
from coppertop.gpu_smoke import RELEASE, MODEL_REPO, MODEL_REVISION
from coppertop.s3 import S3Store
import test_gpu_launch as controller_test_support
from test_gpu_launch import RECEIPT, BILLING, NOW
from test_storage import FakeS3


class ReliabilityControllerTests(unittest.TestCase):
    fixture = controller_test_support.LaunchTests.fixture

    def setup_run(self, failure=None):
        sdk,app,sb=self.fixture();client=FakeS3()
        durable=S3Store(client,'existing-test-bucket',prefix='chords/gpu-reliability/run')
        head=b'weights';case=b'case'
        fields={'release':RELEASE,'checkpoint_sha256':hashlib.sha256(head).hexdigest(),
            'checkpoint_bytes':len(head),'validation_sha256':hashlib.sha256(case).hexdigest(),'validation_bytes':len(case)}
        result={**fields,'status':'passed','process_pid':123}
        manifest={**fields,'base_model':MODEL_REPO,'revision':MODEL_REVISION}
        fresh={**fields,'status':'passed','process_pid':456,'fresh_process_reload_verified':True,
            'velocity_exact':True,'velocity_max_abs_difference':0.0}
        if failure=='same_pid':fresh['process_pid']=123
        files={'/tmp/coppertop-output/'+name:data for name,data in {
            'head.safetensors':head,'validation.safetensors':case,
            'result.json':canonical(result),'head-manifest.json':canonical(manifest)}.items()}
        files['/tmp/coppertop-reloaded/verification/result.json']=canonical(fresh)
        if failure=='bad_case':files['/tmp/coppertop-output/validation.safetensors']=b'CASE'
        sb.filesystem.stat.side_effect=lambda name:SimpleNamespace(size=len(files[name]))
        sb.filesystem.copy_to_local.side_effect=lambda remote,dest:Path(dest).write_bytes(files[remote])
        def upload(local,remote):
            # Reload inputs cannot be uploaded until a committed durable receipt exists.
            receipts=[json.loads(v) for v in client.objects.values() if v.startswith(b'{')]
            self.assertTrue(any(v.get('kind')=='native_head_weights' for v in receipts))
            files[remote]=Path(local).read_bytes()
        sb.filesystem.copy_from_local.side_effect=upload
        processes=[Mock(returncode=0),Mock(returncode=1 if failure=='reload_exit' else 0),Mock(returncode=0)]
        processes[2].stdout.read.return_value=canonical(fresh).decode('utf-8')
        sb.exec.side_effect=processes
        if failure=='storage':client.put_object=Mock(side_effect=OSError('storage unavailable'))
        return sdk,app,sb,durable,files

    def run_launch(self,root,sdk,app,durable):
        with patch('coppertop.gpu_launch.release_objects',return_value=()):
            return launch(sdk,app,RECEIPT,RELIABILITY_APPROVAL,BILLING,'recipe',Mock(),
                root/'out',root/'hold',now=NOW,sleep=lambda _:None,durable_store=durable)

    def test_durable_commit_fetch_and_distinct_process_before_shutdown(self):
        sdk,app,sb,durable,files=self.setup_run()
        with tempfile.TemporaryDirectory() as d:
            root=Path(d);record=self.run_launch(root,sdk,app,durable)
            self.assertEqual(record['status'],'passed');self.assertEqual(record['durable_status'],'verified')
            self.assertTrue(record['fresh_process_reload_verified']);self.assertTrue(record['shutdown']['stopped'])
            self.assertEqual(record['reservation_usd'],'6.5');self.assertEqual(sb.exec.call_count,3)
            args=sb.exec.call_args_list[1].args;options=sb.exec.call_args_list[1].kwargs
            self.assertEqual(args[args.index('--head')+1],'/tmp/coppertop-reloaded/head.safetensors')
            self.assertEqual(args[args.index('--output')+1],'/tmp/coppertop-reloaded/verification')
            self.assertEqual(options['timeout'],120);self.assertNotIn('secrets',options)
            self.assertEqual(sb.exec.call_args_list[2].kwargs['timeout'],30)
            compile(sb.exec.call_args_list[2].args[2],'<bounded-report-reader>','exec')
            self.assertEqual(files['/tmp/coppertop-reloaded/head.safetensors'],b'weights')
            self.assertEqual((root/'out/durable-fetched/head.safetensors').read_bytes(),b'weights')
            self.assertTrue(durable.get(record['durable_reload_report_key']))
            self.assertEqual(sdk.Sandbox.create.call_count,1)

    def test_storage_failure_never_starts_reload_and_stops(self):
        sdk,app,sb,durable,_=self.setup_run('storage')
        with tempfile.TemporaryDirectory() as d:
            root=Path(d)
            with self.assertRaises(OSError):self.run_launch(root,sdk,app,durable)
            record=json.loads((root/'out/launch.json').read_text())
            self.assertEqual(record['failed_phase'],'durable_publish');self.assertEqual(sb.exec.call_count,1)
            self.assertTrue(record['shutdown']['stopped']);self.assertNotIn('durable_checkpoint_key',record)

    def test_failed_fresh_process_preserves_durable_checkpoint_without_retry(self):
        for failure in ('reload_exit','same_pid'):
            with self.subTest(failure=failure), tempfile.TemporaryDirectory() as d:
                sdk,app,sb,durable,_=self.setup_run(failure);root=Path(d)
                with self.assertRaises((RuntimeError,ValueError)):self.run_launch(root,sdk,app,durable)
                record=json.loads((root/'out/launch.json').read_text())
                self.assertEqual(record['status'],'failed');self.assertEqual(record['durable_status'],'verified')
                self.assertTrue(durable.get(record['durable_checkpoint_key']));self.assertTrue(record['shutdown']['stopped'])
                self.assertEqual(sdk.Sandbox.create.call_count,1);self.assertEqual(sb.exec.call_count,2 if failure=='reload_exit' else 3)
                self.assertNotIn('fresh_process_reload_verified',record)

    def test_reliability_requires_exact_new_approval_and_scoped_r2_before_allocation(self):
        sdk,app,sb,durable,_=self.setup_run()
        with self.assertRaises(ValueError):authorize({**RELIABILITY_APPROVAL,'durable_output':False},BILLING,NOW)
        for store in (None,Mock(),S3Store(FakeS3(),'test',prefix='chords')):
            with self.subTest(store=store), tempfile.TemporaryDirectory() as d:
                with self.assertRaises(ValueError):self.run_launch(Path(d),sdk,app,store)
        sdk.Sandbox.create.assert_not_called()

    def test_retained_old_holds_can_block_new_approval(self):
        ledger={'schema_version':2,'unbounded_spend':[],'apps':{'old':{
            'state':'unsettled_bounded','known_reported_usd':'.155','reservation_usd':'16'}}}
        with self.assertRaises(ValueError):authorize(RELIABILITY_APPROVAL,{**BILLING,'ledger':ledger},NOW)

    def test_corrupt_case_does_not_claim_verified_retrieval(self):
        sdk,app,sb,durable,_=self.setup_run('bad_case')
        with tempfile.TemporaryDirectory() as d:
            root=Path(d)
            with self.assertRaises(ValueError):self.run_launch(root,sdk,app,durable)
            record=json.loads((root/'out/launch.json').read_text())
            self.assertEqual(record['retrieval_status'],'incomplete')
            self.assertEqual(record['durable_status'],'incomplete')
            self.assertEqual(record['failed_phase'],'durable_publish')
            self.assertEqual(sb.exec.call_count,1);self.assertTrue(record['shutdown']['stopped'])

    def test_report_reader_rejects_excess_at_source_before_output(self):
        import subprocess,sys
        sdk,app,sb,durable,files=self.setup_run()
        with tempfile.TemporaryDirectory() as d:
            root=Path(d);self.run_launch(root,sdk,app,durable)
            script=sb.exec.call_args_list[2].args[2]
            report=root/'reader-input.json';report.write_bytes(b'x'*65537)
            failed=subprocess.run([sys.executable,'-c',script,str(report),'65536'],capture_output=True,timeout=5)
            self.assertNotEqual(failed.returncode,0);self.assertEqual(failed.stdout,b'')
            report.write_bytes(b'{"status":"passed"}')
            passed=subprocess.run([sys.executable,'-c',script,str(report),str(report.stat().st_size)],capture_output=True,timeout=5)
            self.assertEqual(passed.returncode,0);self.assertEqual(passed.stdout,report.read_bytes())
