import tempfile
import unittest
from pathlib import Path
from coppertop.cloud_stage import StageManifest, stage_receipt
from coppertop.cloud_roundtrip import PhaseCalls, run_roundtrip
from coppertop.streams import ObjectRef
from coppertop.gpu_smoke import RELEASE, MODEL_REPO, MODEL_REVISION


class CloudPhasesTests(unittest.TestCase):
    def fixture(self, *, stage_fails=False, commit_fails=False, reuse=False, stop_fails=False):
        self.events=[]
        inputs=StageManifest('run',RELEASE,{'signal':ObjectRef('b'*64,1)})
        output=StageManifest('run',RELEASE,{'head.safetensors':ObjectRef('c'*64,2),
            'validation.safetensors':ObjectRef('d'*64,1),'head-manifest.json':ObjectRef('1'*64,1),'result.json':ObjectRef('2'*64,1)})
        def stage(raw):
            self.events.append('stage')
            if stage_fails:raise ValueError('bad input')
            return stage_receipt(inputs)
        def train(raw):
            self.events.append('train')
            return {**stage_receipt(output),'container_id':'training','output_manifest':output.to_dict(),
                    'status':'invalid' if commit_fails else 'committed'}
        def publish(raw):
            self.events.append('publish')
            return {'run_id':'run','release':RELEASE,'checkpoint_key':'e'*64}
        def restage(key,raw):
            self.events.append('restage');return stage_receipt(output)
        report=StageManifest('run',RELEASE,{'result.json':ObjectRef('f'*64,1)})
        def reload(raw_input,raw_output,prior):
            self.events.append('reload')
            return {'container_id':'training' if reuse else 'verifier','run_id':'run','release':RELEASE,
                    'report_manifest':report.to_dict(),'commit_receipt':stage_receipt(report),
                    'result':{'status':'passed','release':RELEASE,'checkpoint_sha256':'c'*64,
                        'validation_sha256':'d'*64,'checkpoint_bytes':2,'validation_bytes':1,
                        'kind':'native_head_fresh_process_reload','model_repo':MODEL_REPO,'model_revision':MODEL_REVISION,'cutoff':6,
                        'fresh_process_reload_verified':True,
                        'velocity_exact':True,'velocity_max_abs_difference':0}}
        def publish_reload(raw,identity):
            self.events.append('publish_reload')
            return {'run_id':'run','release':RELEASE,'container_id':identity,'report_key':'f'*64}
        def stop(identity):
            self.events.append('stop-'+identity);return not stop_fails
        return inputs,PhaseCalls('modal',stage,train,publish,restage,reload,publish_reload,stop)

    def test_no_gpu_until_stage_then_gpu_stopped_before_cpu_publish(self):
        with tempfile.TemporaryDirectory() as d:
            inputs,calls=self.fixture();record=run_roundtrip(calls,inputs,Path(d)/'receipts')
            self.assertEqual(record['status'],'passed')
            self.assertEqual(self.events,['stage','train','stop-training','publish','restage','reload','stop-verifier','publish_reload'])
            with self.assertRaises(FileExistsError):run_roundtrip(calls,inputs,Path(d)/'receipts')

    def test_bad_staging_never_allocates_gpu(self):
        with tempfile.TemporaryDirectory() as d:
            inputs,calls=self.fixture(stage_fails=True)
            with self.assertRaises(ValueError):run_roundtrip(calls,inputs,Path(d)/'receipts')
            self.assertEqual(self.events,['stage'])

    def test_commit_failure_stops_gpu_and_never_publishes(self):
        with tempfile.TemporaryDirectory() as d:
            inputs,calls=self.fixture(commit_fails=True)
            with self.assertRaises(ValueError):run_roundtrip(calls,inputs,Path(d)/'receipts')
            self.assertEqual(self.events,['stage','train','stop-training'])

    def test_shutdown_failure_blocks_cpu_and_next_gpu(self):
        with tempfile.TemporaryDirectory() as d:
            inputs,calls=self.fixture(stop_fails=True)
            with self.assertRaises(RuntimeError):run_roundtrip(calls,inputs,Path(d)/'receipts')
            self.assertNotIn('publish',self.events);self.assertNotIn('reload',self.events)

    def test_fresh_container_identity_required_even_when_prediction_matches(self):
        with tempfile.TemporaryDirectory() as d:
            inputs,calls=self.fixture(reuse=True)
            with self.assertRaises(ValueError):run_roundtrip(calls,inputs,Path(d)/'receipts')
            self.assertEqual(self.events[-1],'stop-training')

    def test_cleanup_query_exception_preserves_primary_and_records_unverified_shutdown(self):
        import json
        from dataclasses import replace
        with tempfile.TemporaryDirectory() as d:
            inputs,calls=self.fixture(commit_fails=True)
            def fail(_):raise TimeoutError('provider unavailable')
            calls=replace(calls,stop_verify=fail)
            with self.assertRaises(ValueError):run_roundtrip(calls,inputs,Path(d)/'receipts')
            record=json.loads((Path(d)/'receipts/run.json').read_text())
            self.assertEqual(record['status'],'shutdown_unverified')
            self.assertEqual(record['error_type'],'ValueError')
            self.assertEqual(record['cleanup_error_type'],'TimeoutError')
            self.assertEqual(record['active_container_id'],'training')

    def test_large_or_recursive_remote_metadata_rejected_before_next_phase(self):
        from dataclasses import replace
        for value in ({'extra':'x'*65537}, {'extra':list(range(257))}):
            with tempfile.TemporaryDirectory() as d:
                inputs,calls=self.fixture();calls=replace(calls,stage=lambda raw:value)
                with self.assertRaises(ValueError):run_roundtrip(calls,inputs,Path(d)/'receipts')
                self.assertNotIn('train',self.events)
        cycle={};cycle['cycle']=cycle
        with tempfile.TemporaryDirectory() as d:
            inputs,calls=self.fixture();calls=replace(calls,stage=lambda raw:cycle)
            with self.assertRaises(ValueError):run_roundtrip(calls,inputs,Path(d)/'receipts')

    def test_published_report_must_match_committed_hash(self):
        from dataclasses import replace
        with tempfile.TemporaryDirectory() as d:
            inputs,calls=self.fixture()
            calls=replace(calls,publish_reload=lambda raw,identity:{'run_id':'run','release':RELEASE,
                'container_id':identity,'report_key':'0'*64})
            with self.assertRaises(ValueError):run_roundtrip(calls,inputs,Path(d)/'receipts')
            self.assertEqual(self.events[-1],'stop-verifier')

    def test_bad_gpu_metadata_still_verifies_owned_shutdown(self):
        from dataclasses import replace
        for phase in ('train','reload'):
            with tempfile.TemporaryDirectory() as d:
                inputs,calls=self.fixture();original=getattr(calls,phase)
                def bad(*args):
                    result=original(*args);result['extra']='x'*65537;return result
                calls=replace(calls,**{phase:bad})
                with self.assertRaises(ValueError):run_roundtrip(calls,inputs,Path(d)/'receipts')
                expected='stop-training' if phase=='train' else 'stop-verifier'
                self.assertEqual(self.events[-1],expected)
