import json
import os
import subprocess
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from coppertop.chords import LocalStore, canonical, digest, object_path
from coppertop.cloud_stage import StageManifest
from coppertop.gpu_smoke import RELEASE, MODEL_REPO, MODEL_REVISION
from coppertop.streams import ObjectRef
from coppertop import modal_reload as module


class Volume:
    def __init__(self, events, options=None):
        self.events, self.options = events, options or {}
    def with_mount_options(self, **options):
        return Volume(self.events, dict(self.options, **options))
    def commit(self):
        self.events.append('commit')


class App:
    def __init__(self, *args, **options):
        self.options, self.definitions = options, {}
    def function(self, **options):
        def define(function):
            self.definitions[function.__name__] = options
            return function
        return define


class ReloadOnlyTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.root = Path(temp.name).resolve()
        self.store = LocalStore(self.root/'r2')
        self.events = []
        self.roles = []
        self.volume = Volume(self.events)
        source_ref = ObjectRef(self.store.put(b'source'), 6)
        self.source = StageManifest(module.CHECKPOINT_RUN_ID, RELEASE, {object_path(RELEASE):source_ref})
        refs = {name:ObjectRef(self.store.put(name.encode()),len(name)) for name in (
            'head.safetensors','validation.safetensors','head-manifest.json','result.json')}
        self.checkpoint = StageManifest(module.CHECKPOINT_RUN_ID, RELEASE, refs)
        receipt = {'schema_version':1,'kind':'native_head_weights','run_id':module.CHECKPOINT_RUN_ID,
            'release':RELEASE,'base_model':MODEL_REPO,'revision':MODEL_REVISION,
            'scope':'head_weights_only','optimizer_state':False,'rng_state':False,
            'artifacts':{name:ref.to_dict() for name,ref in refs.items()}}
        self.key = self.store.put(canonical(receipt))
        self.patch = patch.object(module, 'INPUT_MOUNT', str(self.root/'volume'))
        self.patch.start(); self.addCleanup(self.patch.stop)
        def runner(argv, **options):
            self.events.append('native')
            self.assertEqual(options, {'check':True,'timeout':120})
            self.assertIn('coppertop.smoke_reload',argv)
            self.assertNotIn('coppertop.smoke_runtime',argv)
            target = Path(argv[argv.index('--output')+1])
            self.assertFalse(target.exists())
            target.mkdir()
            target.joinpath('result.json').write_bytes(canonical(self.native_result()))
        self.options = dict(volume=self.volume, image=object(),
            store_factory=lambda role:(self.roles.append(role) or self.store),
            transfer_secrets={role:[object()] for role in ('source','checkpoint','report')},
            source=self.source,checkpoint=self.checkpoint,checkpoint_key=self.key,
            original_container_id=module.ORIGINAL_CONTAINER_ID,attempt_id='reload-attempt',region='us-east',runner=runner)
        self.functions = module.create_reload_functions(SimpleNamespace(App=App,__version__='1.6.1'),**self.options)

    def native_result(self):
        head=self.checkpoint.objects['head.safetensors']; case=self.checkpoint.objects['validation.safetensors']
        return {'status':'passed','release':RELEASE,'kind':'native_head_fresh_process_reload',
            'model_repo':MODEL_REPO,'model_revision':MODEL_REVISION,'cutoff':6,
            'fresh_process_reload_verified':True,'velocity_exact':True,'velocity_max_abs_difference':0.0,
            'checkpoint_sha256':head.sha256,'checkpoint_bytes':head.size,
            'validation_sha256':case.sha256,'validation_bytes':case.size}

    def invoke(self, function, args, timeout):
        self.events.append(function.__name__)
        with patch.dict(os.environ, MODAL_TASK_ID='ta-'+function.__name__.replace('_','')):
            return function(*args)

    def cleanup(self, success):
        self.events.append(('cleanup',success))
        return {'active_containers':0,'active_sandboxes':0,'volume_deleted':True,'app_stopped':True}

    def execute(self, **changes):
        def stop(identity):
            self.events.append('stopped-'+identity)
            return True
        options=dict(admission=lambda:self.events.append('admission'),
            image_gate=lambda:(self.events.append('image_gate') or {'status':'passed','container_id':'ta-imagegate'}),
            stop_verify=stop,cleanup=self.cleanup,invoke=self.invoke)
        options.update(changes)
        return module.execute_reload(self.functions,self.root/'attempt',**options)

    def test_reload_only_end_to_end_and_stop_barriers(self):
        result=self.execute()
        self.assertEqual(result['status'],'passed')
        self.assertEqual(self.roles,['source','checkpoint','report'])
        report=result['phases']['publish']['report_key']
        self.assertEqual(json.loads(self.store.get(report))['training_container_id'],module.ORIGINAL_CONTAINER_ID)
        self.assertLess(self.events.index('stopped-ta-stagesource'),self.events.index('image_gate'))
        self.assertLess(self.events.index('stopped-ta-imagegate'),self.events.index('restage_checkpoint'))
        self.assertLess(self.events.index('stopped-ta-restagecheckpoint'),self.events.index('verify'))
        self.assertLess(self.events.index('stopped-ta-verify'),self.events.index('publish_report'))
        self.assertEqual(self.events[-1],('cleanup',True))
        with self.assertRaises(FileExistsError):self.execute()

    def test_reserved_authorization_cannot_replay_controller(self):
        directory=self.root/'attempt'
        directory.mkdir()
        reservation={'run_id':self.checkpoint.run_id,'checkpoint_key':self.key,'automatic_retries':0,
                     'plan_sha256':'a'*64,'recipe_sha256':'b'*64}
        (directory/'consumed.json').write_text(json.dumps(reservation))
        self.assertEqual(self.execute(reservation=reservation)['status'],'passed')
        before=list(self.events)
        with self.assertRaises(FileExistsError):self.execute(reservation=reservation)
        self.assertEqual(self.events,before)

    def test_definitions_bound_compute_and_readonly_no_gpu_secrets(self):
        definitions=self.functions.app.definitions
        self.assertEqual(self.functions.app.options['secrets'],[])
        self.assertEqual(set(definitions),{'stage_source','restage_checkpoint','verify','publish_report'})
        self.assertEqual(definitions['stage_source']['timeout'],600)
        self.assertEqual(definitions['restage_checkpoint']['timeout'],1800)
        self.assertEqual(definitions['publish_report']['volumes'],{})
        gpu=definitions['verify']
        self.assertEqual(gpu['timeout'],300)
        self.assertEqual(gpu['startup_timeout'],300)
        self.assertEqual(gpu['gpu'],'L40S')
        self.assertEqual(gpu['secrets'],[])
        self.assertTrue(gpu['block_network'])
        self.assertTrue(gpu['volumes'][module.INPUT_MOUNT].options['read_only'])
        for definition in definitions.values():
            self.assertEqual(definition['retries'],0)
            self.assertTrue(definition['single_use_containers'])

    def test_allocation_error_consumes_attempt_and_cleans_owner(self):
        def invoke(function,args,timeout):
            raise RuntimeError('allocation failed without task id')
        with self.assertRaises(RuntimeError):self.execute(invoke=invoke)
        record=json.loads((self.root/'attempt/outcome.json').read_bytes())
        self.assertEqual(record['status'],'failed')
        self.assertTrue(record['cleanup']['volume_deleted'])
        self.assertEqual(self.events[-1],('cleanup',False))
        self.assertTrue((self.root/'attempt/consumed.json').is_file())

    def test_returned_identity_saved_before_validation_failure(self):
        def invoke(function,args,timeout):
            return {'container_id':'ta-owned','receipt':{'wrong':True}}
        with self.assertRaises(ValueError):self.execute(invoke=invoke)
        record=json.loads((self.root/'attempt/outcome.json').read_bytes())
        self.assertEqual(record['phases']['source']['container_id'],'ta-owned')
        self.assertEqual(self.events[-1],('cleanup',False))

    def test_stop_failure_blocks_gpu_and_report(self):
        def stop(identity):return identity!='ta-restagecheckpoint'
        with self.assertRaises(RuntimeError):self.execute(stop_verify=stop)
        self.assertNotIn('verify',self.events)
        self.assertNotIn('report',self.roles)

    def test_native_timeout_and_cleanup_failure_recorded(self):
        def runner(*args,**kwargs):raise subprocess.TimeoutExpired('native',120)
        self.functions=module.create_reload_functions(SimpleNamespace(App=App,__version__='1.6.1'),**dict(self.options,runner=runner))
        def cleanup(success):raise RuntimeError('audit unconfirmed')
        with self.assertRaises(subprocess.TimeoutExpired):self.execute(cleanup=cleanup)
        record=json.loads((self.root/'attempt/outcome.json').read_bytes())
        self.assertEqual(record['error_type'],'TimeoutExpired')
        self.assertEqual(record['cleanup_error_type'],'RuntimeError')
        self.assertNotIn('report',self.roles)

    def test_foreign_original_or_run_rejected_without_sdk_definition(self):
        for change in ({'original_container_id':'ta-other'}, {'source':StageManifest('other',RELEASE,self.source.objects)}):
            with self.subTest(change=change),self.assertRaises(ValueError):
                module.create_reload_functions(SimpleNamespace(App=App,__version__='1.6.1'),**dict(self.options,**change))

    def test_report_tampering_rejected_before_cpu_write(self):
        result=self.execute()
        native=result['phases']['verify']['result']
        record={'container_id':native['container_id'],'result':dict(native, checkpoint_receipt_key='a'*64),
                'report_sha256':result['phases']['verify']['report_sha256']}
        before=list(self.roles)
        with patch.dict(os.environ, MODAL_TASK_ID='ta-publisher'),self.assertRaises(ValueError):
            self.functions.publish_report(record)
        self.assertEqual(self.roles,before)

    def test_unknown_cleanup_proof_fails_success(self):
        with self.assertRaises(RuntimeError):
            self.execute(cleanup=lambda success:{'active_containers':False,'active_sandboxes':0,'volume_deleted':True,'app_stopped':True})
        record=json.loads((self.root/'attempt/outcome.json').read_bytes())
        self.assertEqual(record['status'],'failed')
        self.assertEqual(record['cleanup_error_type'],'RuntimeError')

    def test_gpu_identity_saved_when_comparison_fails(self):
        def invoke(function,args,timeout):
            if function is self.functions.verify:
                return {'container_id':'ta-ownedGPU','result':{'velocity_exact':False}}
            return self.invoke(function,args,timeout)
        with self.assertRaises(ValueError):self.execute(invoke=invoke)
        record=json.loads((self.root/'attempt/outcome.json').read_bytes())
        self.assertEqual(record['phases']['verify']['container_id'],'ta-ownedGPU')
        self.assertNotIn('report',self.roles)
        self.assertEqual(self.events[-1],('cleanup',False))

    def test_oversized_metadata_keeps_actual_id_and_cleans(self):
        def invoke(function,args,timeout):return {'container_id':'ta-owned','receipt':{},'extra':[0]*257}
        with self.assertRaises(ValueError):self.execute(invoke=invoke)
        record=json.loads((self.root/'attempt/outcome.json').read_bytes())
        self.assertEqual(record['phases']['source']['container_id'],'ta-owned')
        self.assertTrue(record['cleanup']['app_stopped'])
        self.assertNotIn('image_gate',self.events)
