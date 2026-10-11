import asyncio
import copy
import hashlib
import inspect
import json
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import time
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, patch

from coppertop import modal_seam as seam
from coppertop import modal_seam_probe as probe


APP = 'ap-'+'A'*22
FOREIGN_APP = 'ap-'+'B'*22


class SeamTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        repository = Path(__file__).resolve().parents[1]
        for name in seam.SOURCE_FILES:
            path = self.root/name
            path.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(repository/name, path)
        self.plan = seam.prepare(self.root, 'ct-modal-test', 'fixture-workspace', 'im-Fixture')
        self.output = self.root/'result'

    def approval(self, plan=None):
        plan = plan or self.plan
        return {'approved': True, 'planSha256': seam.sha(seam.canonical(plan)),
                'workspace': plan['workspace'], 'environment': 'main',
                'operationalAllowanceUsd': '0.25', 'invoiceCapVerified': False,
                'boundedExposureAccepted': True, 'cleanupAuthorized': True,
                'imageBuildAuthorized': False, 'persistentDeploymentAuthorized': False}

    def req(self, mode='complete'):
        return seam.request(self.plan, mode, int((time.time()+180)*1000))

    def completed(self, request, container='ta-Complete'):
        return {**{k: request[k] for k in ('schemaVersion', 'runId', 'generation', 'bodyId', 'sourceSha256', 'payloadBase64')},
                'containerId': container, 'digest': probe.PAYLOAD_HASH, 'bytes': 32}

    def marker(self, request, container='ta-Complete'):
        return {**{k: request[k] for k in ('runId', 'generation', 'bodyId', 'sourceSha256')},
                'schemaVersion': 1, 'event': 'started', 'payloadSha256': probe.PAYLOAD_HASH,
                'containerId': container}

    def owner(self, app_id=APP, attempted=True):
        return {'planSha256': seam.sha(seam.canonical(self.plan)), 'suiteId': self.plan['suiteId'],
                'workspace': self.plan['workspace'], 'environment': 'main', 'cleanupAuthorized': True,
                'creationAttempted': attempted, 'appId': app_id}

    def fake_calls(self, *, bad_receipt=False, bad_marker=False, cancel_error=False, slow=False, different_container=False):
        state = SimpleNamespace(calls=[], cancels=[], active=True, clock=0)
        test = self
        class Call:
            def __init__(self, request):
                self.request = request
                self.object_id = 'fc-Complete' if request['mode']=='complete' else 'fc-Hold'
                self.logs = self
            def get(self, timeout):
                result = test.completed(self.request)
                if bad_receipt:
                    result['digest'] = '0'*64
                return result
            def stream(self, timeout):
                marker = test.marker(self.request, 'ta-Hold' if different_container else 'ta-Complete')
                if bad_marker:
                    marker['generation'] = 'wrong'
                line = probe.START_PREFIX+json.dumps(marker)+'\n'
                if slow:
                    state.clock = 21
                yield SimpleNamespace(message=line[:12],source='stdout')
                yield SimpleNamespace(message=line[12:],source='stdout')
            def cancel(self, **options):
                state.cancels.append(options)
                if cancel_error:
                    raise PermissionError('Authorization: Bearer DO-NOT-RECORD')
                state.active = False
        class Function:
            def spawn(self, request):
                state.calls.append(request)
                return Call(request)
        def runner(argv, **options):
            rows = [{'container_id': 'ta-Complete', 'app_id': APP, 'app_name': 'ct-modal-test', 'start_time': 'fixture'}] if state.active else []
            return SimpleNamespace(returncode=0, stdout=json.dumps(rows).encode(), stderr=b'')
        return Function(), runner, state

    def test_preparation_is_offline_and_unknown_image_workspace_fail_before_process(self):
        incomplete = seam.prepare(self.root, 'ct-modal-unresolved')
        self.assertIsNone(incomplete['workspace'])
        self.assertIsNone(incomplete['imageId'])
        launched = []
        with self.assertRaisesRegex(ValueError, 'unresolved'):
            seam.supervise(self.root, incomplete, self.approval(incomplete), self.output,
                           process_factory=lambda *a, **k: launched.append(a))
        self.assertEqual(launched, [])
        self.assertFalse((self.root/'.local').exists())

    def test_fixed_prebuilt_plan_binds_recipe_and_rejects_changes_before_any_process(self):
        plan = seam.prepare(self.root, 'ct-modal-base', 'fixture-workspace', prebuilt_base=True)
        self.assertIsNone(plan['imageId'])
        self.assertEqual(plan['imageRecipe'], seam.PREBUILT_BASE)
        seam.require_approval(self.root, plan, self.approval(plan))
        for key, value in [('pythonVersion','3.11.12'),('builderVersion','PREVIEW'),
                           ('namespace','workspace'),('forceBuild',True),('ignoreCache',True),
                           ('allowGlobalDeployment',True),('customBuildAuthorized',True)]:
            changed = copy.deepcopy(plan)
            changed['imageRecipe'][key] = value
            with self.subTest(key=key), self.assertRaises(ValueError):
                seam.require_approval(self.root, changed, self.approval(changed))
        with self.assertRaises(ValueError):
            seam.prepare(self.root, 'ct-modal-base', 'fixture-workspace', 'im-Fixture', prebuilt_base=True)
        self.assertFalse((self.root/'.local').exists())

    def test_approval_rejections_and_expanded_scope_precede_claim_and_process(self):
        changes = {'approved': False, 'planSha256': '0'*64, 'workspace': 'other', 'environment': 'prod',
                   'operationalAllowanceUsd': '1', 'invoiceCapVerified': True, 'boundedExposureAccepted': False,
                   'cleanupAuthorized': False, 'imageBuildAuthorized': True, 'persistentDeploymentAuthorized': True}
        for key, value in changes.items():
            with self.subTest(key=key):
                approval = self.approval()
                approval[key] = value
                with self.assertRaises(ValueError):
                    seam.supervise(self.root, self.plan, approval, self.output)
        for key, value in [('spawnCalls',3), ('environment','prod'), ('executionSeconds',181), ('cleanupSeconds',61), ('apps',2)]:
            plan = copy.deepcopy(self.plan)
            plan[key] = value
            with self.assertRaises(ValueError):
                seam.require_approval(self.root, plan, self.approval(plan))
        plan = copy.deepcopy(self.plan)
        plan['functionOptions']['max_containers'] = 2
        with self.assertRaises(ValueError):
            seam.require_approval(self.root, plan, self.approval(plan))
        self.assertEqual(seam.OPTIONS['max_containers'], 1)
        self.assertFalse((self.root/'.local').exists())

    def test_expiry_future_clock_source_change_and_symlink_are_refused(self):
        for now in (self.plan['createdAt']-1, self.plan['expiresAt']):
            with self.assertRaises(ValueError):
                seam.require_approval(self.root, self.plan, self.approval(), now=now)
        source = self.root/'coppertop/modal_seam_probe.py'
        source.write_bytes(source.read_bytes()+b'\n# changed\n')
        with self.assertRaises(ValueError):
            seam.require_approval(self.root, self.plan, self.approval())
        source.unlink()
        source.symlink_to(self.root/'coppertop/modal_seam.py')
        with self.assertRaises(ValueError):
            seam.prepare(self.root, 'ct-modal-changed')

    def test_definition_mounts_only_reviewed_files_and_has_fixed_resource_bounds(self):
        calls = []
        class Image:
            def add_local_file(self, *args, **kwargs):
                calls.append((args, kwargs))
                return self
        class App:
            def __init__(self, *args, **kwargs):
                calls.append(('app', args, kwargs))
            def function(self, **options):
                calls.append(('function', options))
                return lambda function: function
        sdk = SimpleNamespace(__version__='1.6.1', App=App)
        _, function, _ = seam.create_probe(sdk, Image(), self.root, self.plan['suiteId'])
        self.assertIs(function, probe.probe)
        self.assertEqual([call[0][1] for call in calls[:2]], ['/root/coppertop/__init__.py', '/root/coppertop/modal_seam_probe.py'])
        self.assertTrue(all(call[1] == {'copy':False} for call in calls[:2]))
        options = calls[-1][1]
        self.assertEqual(options['cpu'], (0.125,0.125))
        self.assertEqual(options['memory'], (256,256))
        self.assertEqual(options['max_containers'], 1)
        self.assertEqual(options['secrets'], [])
        self.assertEqual(options['volumes'], {})
        self.assertFalse(options['single_use_containers'])
        self.assertEqual(options['scaledown_window'], 180)
        self.assertTrue(options['block_network'])
        self.assertFalse(options['include_source'])
        self.assertEqual(options['retries'], 0)

    def test_pinned_sdk_signatures_and_lazy_definition_do_not_authenticate(self):
        import modal
        self.assertEqual(modal.__version__, seam.SDK_VERSION)
        inspect.signature(modal.App.function).bind(None, **seam.OPTIONS)
        with patch.object(modal.Client, 'from_env', side_effect=AssertionError('No authentication in definition')):
            image = modal.Image.from_id('imFixture'.replace('im','im-',1))
            app, function, _ = seam.create_probe(modal, image, self.root, self.plan['suiteId'])
        self.assertIsNone(app.app_id)
        self.assertIsInstance(function, modal.Function)

    def global_response(self, *, status=None, version='Python 3.12.10 (fixture)', identity='im-Fixture'):
        from modal_proto import api_pb2
        return api_pb2.ImageGetOrCreateResponse(image_id=identity,
            result=api_pb2.GenericResult(status=api_pb2.GenericResult.GENERIC_STATUS_SUCCESS if status is None else status),
            metadata=api_pb2.ImageMetadata(python_version_info=version))

    def test_prebuilt_selector_refuses_builder_and_bypass_settings_without_authentication(self):
        import modal
        from modal.config import config
        from modal._utils.async_utils import synchronizer
        from modal_proto import api_pb2
        environment = modal.Environment.from_name('main',create_if_missing=False)
        raw_environment = synchronizer._translate_in(environment)
        metadata = api_pb2.EnvironmentMetadata(name='main')
        metadata.settings.image_builder_version = '2025.06'
        raw_environment._hydrate_metadata(metadata)
        self.assertFalse(hasattr(environment,'_settings'))
        def values(key):
            return False if key in ('force_build','ignore_cache') else None
        with patch.object(config,'get',side_effect=values), patch.dict(seam.os.environ,{},clear=True):
            image = seam.prebuilt_image(modal, environment)
            self.assertIsInstance(image,modal.Image)
            for key in ('force_build','ignore_cache'):
                with patch.object(config,'get',side_effect=lambda name: True if name==key else values(name)), self.assertRaises(ValueError):
                    seam.prebuilt_image(modal,environment)
            raw_environment._settings = SimpleNamespace(image_builder_version='2024.10')
            with self.assertRaises(ValueError):
                seam.prebuilt_image(modal,environment)
            raw_environment._settings = SimpleNamespace(image_builder_version='2025.06')
            with patch.dict(seam.os.environ,{'MODAL_IMAGE_ALLOW_GLOBAL_DEPLOYMENT':'0'}), self.assertRaises(ValueError):
                seam.prebuilt_image(modal,environment)

    def test_actual_pinned_sdk_loader_hits_only_exact_global_cache_after_app_journal(self):
        import modal
        from modal._utils.async_utils import synchronizer
        from modal.config import config
        self.output.mkdir()
        environment = SimpleNamespace(_settings=SimpleNamespace(image_builder_version='2025.06'))
        response = self.global_response()
        async def lookup(req, **options):
            self.assertEqual(req,seam.global_image_request(APP))
            self.assertEqual(options,{'retry':None,'timeout':10})
            self.assertEqual(json.loads((self.output/'ownership.json').read_bytes())['appId'],APP)
            return response
        original = AsyncMock(side_effect=lookup)
        client = SimpleNamespace(_stub=SimpleNamespace(ImageGetOrCreate=original))
        values = lambda key: False if key in ('force_build','ignore_cache','function_runtime_debug') else None
        with patch.object(config,'get',side_effect=values), patch.dict(seam.os.environ,{},clear=True):
            image = seam.prebuilt_image(modal,environment)
            app = modal.App('ct-modal-fixture')
            seam.journal_app(app,self.owner(),self.output,prebuilt=True)
            raw_app, raw_image = synchronizer._translate_in(app), synchronizer._translate_in(image)
            async def resolve():
                async with raw_app._set_local_app(client,SimpleNamespace(app_id=APP)):
                    context = SimpleNamespace(app_id=APP,environment_name='main',client=client)
                    with patch('modal._image._get_environment_cached',AsyncMock(return_value=environment)):
                        await raw_image._load(raw_image,SimpleNamespace(),context,None)
                    self.assertEqual(raw_image.object_id,'im-Fixture')
            asyncio.run(resolve())
        self.assertIs(client._stub.ImageGetOrCreate,original)
        self.assertEqual(original.await_count,1)
        resolved = json.loads((self.output/'resolved-image.json').read_bytes())
        self.assertEqual(resolved,{'appId':APP,'imageId':'im-Fixture','recipe':seam.PREBUILT_BASE,
                                   'cacheHit':True,'customBuildRequested':False})

    def test_global_request_guard_denies_custom_scope_and_pending_or_unverified_results(self):
        import modal
        from modal._utils.async_utils import synchronizer
        from modal_proto import api_pb2
        cases = [('namespace',api_pb2.DEPLOYMENT_NAMESPACE_WORKSPACE),('force_build',True),
                 ('ignore_cache',True),('allow_global_deployment',True),('app_id',FOREIGN_APP),
                 ('builder_version','PREVIEW'),('commands','RUN touch custom')]
        for field,value in cases:
            output = self.root/field;output.mkdir()
            original = AsyncMock(return_value=self.global_response())
            client = SimpleNamespace(_stub=SimpleNamespace(ImageGetOrCreate=original))
            app = modal.App('ct-modal-fixture');seam.journal_app(app,self.owner(),output,prebuilt=True)
            request = seam.global_image_request(APP)
            if field=='commands':
                request.image.dockerfile_commands.append(value)
            else:
                setattr(request,field,value)
            async def denied():
                async with synchronizer._translate_in(app)._set_local_app(client,SimpleNamespace(app_id=APP)):
                    with self.assertRaises(ValueError):
                        await client._stub.ImageGetOrCreate(request)
            asyncio.run(denied())
            original.assert_not_awaited()
            self.assertIs(client._stub.ImageGetOrCreate,original)
            self.assertFalse((output/'resolved-image.json').exists())
        for label,response in [('pending',self.global_response(status=0)),
                               ('failure',self.global_response(status=api_pb2.GenericResult.GENERIC_STATUS_FAILURE)),
                               ('wrong-version',self.global_response(version='Python 3.12.11')),
                               ('missing-id',self.global_response(identity=''))]:
            output = self.root/label;output.mkdir()
            original = AsyncMock(return_value=response)
            client = SimpleNamespace(_stub=SimpleNamespace(ImageGetOrCreate=original))
            app = modal.App('ct-modal-fixture');seam.journal_app(app,self.owner(),output,prebuilt=True)
            async def denied_reply():
                async with synchronizer._translate_in(app)._set_local_app(client,SimpleNamespace(app_id=APP)):
                    with self.assertRaises(ValueError):
                        await client._stub.ImageGetOrCreate(seam.global_image_request(APP))
            asyncio.run(denied_reply())
            self.assertEqual(original.await_count,1)
            self.assertFalse((output/'resolved-image.json').exists())

    def test_image_resolution_failure_or_hang_retains_original_exact_app_cleanup(self):
        import modal
        from modal._utils.async_utils import synchronizer
        for hangs in (False,True):
            self.plan = seam.prepare(self.root,'ct-modal-basehang' if hangs else 'ct-modal-basedeny',
                                     'fixture-workspace',prebuilt_base=True)
            self.output = self.root/('basehang' if hangs else 'basedeny');self.output.mkdir()
            seam.claim_execution(self.root,self.plan,self.output)
            seam.save(self.output/'plan.json',self.plan);seam.save(self.output/'approval.json',self.approval())
            async def broken(*args,**kwargs):
                if hangs:
                    await asyncio.Event().wait()
                raise PermissionError('DO-NOT-RECORD')
            original = AsyncMock(side_effect=broken)
            client = SimpleNamespace(_stub=SimpleNamespace(ImageGetOrCreate=original))
            app = modal.App(self.plan['suiteId']);seam.journal_app(app,self.owner(),self.output,prebuilt=True)
            async def resolve():
                async with synchronizer._translate_in(app)._set_local_app(client,SimpleNamespace(app_id=APP)):
                    await client._stub.ImageGetOrCreate(seam.global_image_request(APP))
            with self.assertRaises(asyncio.TimeoutError if hangs else PermissionError):
                asyncio.run(asyncio.wait_for(resolve(),timeout=0.01))
            _,cleanup,_,calls,_ = self.fake_process()
            result = seam.cleanup_receipt(self.root,self.plan,self.output,cleanup=cleanup)
            self.assertTrue(result['confirmed']);self.assertEqual(calls,[APP])
            self.assertIs(client._stub.ImageGetOrCreate,original)
            self.assertNotIn('DO-NOT-RECORD',(self.output/'ownership.json').read_text())

    def test_prebuilt_admission_requires_exact_cached_identity_and_independent_metadata(self):
        self.output.mkdir()
        image = SimpleNamespace(object_id='im-Fixture')
        original = {'appId':APP,'imageId':'im-Fixture','recipe':seam.PREBUILT_BASE,
                    'cacheHit':True,'customBuildRequested':False}
        for key,value in [('appId',FOREIGN_APP),('imageId','im-Other'),('recipe',{}),
                          ('cacheHit',False),('customBuildRequested',True)]:
            changed = {**original,key:value};seam.save(self.output/'resolved-image.json',changed)
            with patch.object(seam,'verify_image') as verify, self.assertRaises(ValueError):
                seam.admit_prebuilt(image,APP,self.output)
            verify.assert_not_called()
        seam.save(self.output/'resolved-image.json',original)
        with patch.object(seam,'verify_image',side_effect=ValueError('Metadata mismatch')) as verify, self.assertRaises(ValueError):
            seam.admit_prebuilt(image,APP,self.output)
        verify.assert_called_once_with('im-Fixture',python_version='3.12.10')
        with patch.object(seam,'verify_image',return_value={'imageId':'im-Fixture','pythonVersion':'3.12.10','buildRequested':False}) as verify:
            self.assertEqual(seam.admit_prebuilt(image,APP,self.output)['recipe'],seam.PREBUILT_BASE)
        verify.assert_called_once_with('im-Fixture',python_version='3.12.10')

    def test_tls_bootstrap_is_application_local_preserves_verification_and_precedes_sdk(self):
        script = '''
import ssl,sys
from coppertop.modal_seam_tls import bootstrap
assert 'modal' not in sys.modules
original_context = ssl.SSLContext
original_paths = ssl.get_default_verify_paths()
bootstrap()
import truststore
assert ssl.SSLContext is truststore.SSLContext
context = ssl.create_default_context()
assert context.verify_mode == ssl.CERT_REQUIRED and context.check_hostname
assert ssl.get_default_verify_paths() == original_paths
bootstrap()
import modal
from modal._utils.grpc_utils import ModalChannel
context = ModalChannel._get_default_ssl_context(None)
assert context.verify_mode == ssl.CERT_REQUIRED and context.check_hostname
print('verified')
'''
        repository = Path(__file__).resolve().parents[1]
        response = subprocess.run([sys.executable,'-c',script],cwd=repository,capture_output=True,timeout=10)
        self.assertEqual(response.returncode,0,response.stderr.decode())
        self.assertEqual(response.stdout,b'verified\n')

    def test_tls_bootstrap_rejects_late_or_missing_dependency_before_authentication(self):
        repository = Path(__file__).resolve().parents[1]
        for prelude in ("import modal", "from unittest.mock import patch; p=patch('importlib.metadata.version',return_value='wrong'); p.start()"):
            script = prelude+'''
from coppertop.modal_seam_tls import bootstrap
try:
    bootstrap()
except ValueError:
    print('refused')
else:
    raise AssertionError('Invalid bootstrap accepted')
'''
            response = subprocess.run([sys.executable,'-c',script],cwd=repository,capture_output=True,timeout=10)
            self.assertEqual(response.returncode,0,response.stderr.decode())
            self.assertEqual(response.stdout,b'refused\n')

    def test_cleanup_subprocess_bootstrap_runs_before_sdk_even_from_another_directory(self):
        script = "import ssl; from modal._utils.grpc_utils import ModalChannel; c=ModalChannel._get_default_ssl_context(None); assert c.verify_mode==ssl.CERT_REQUIRED and c.check_hostname; print('verified')"
        response = subprocess.run(seam.sdk_command(script),cwd=self.root,capture_output=True,timeout=10)
        self.assertEqual(response.returncode,0,response.stderr.decode())
        self.assertEqual(response.stdout,b'verified\n')

    def test_existing_image_lookup_uses_only_bounded_read_rpc_and_rejects_metadata_drift(self):
        from modal.client import _Client
        for image, python, accepted in [('im-Fixture','Python 3.12.11 (fixture)',True),
                                         ('im-Other','Python 3.12.11',False),
                                         ('im-Fixture','Python 3.11.9',False),
                                         ('im-Fixture','',False)]:
            response = SimpleNamespace(image_id=image, metadata=SimpleNamespace(python_version_info=python))
            lookup = AsyncMock(return_value=response)
            client = SimpleNamespace(_stub=SimpleNamespace(ImageFromId=lookup))
            with patch.object(_Client,'from_env',new=AsyncMock(return_value=client)):
                if accepted:
                    result = seam.verify_image('im-Fixture')
                    self.assertEqual(result,{'imageId':'im-Fixture','pythonVersion':'3.12','buildRequested':False})
                else:
                    with self.assertRaises(ValueError):
                        seam.verify_image('im-Fixture')
            self.assertEqual(lookup.await_count,1)
            self.assertEqual(lookup.call_args.args[0].image_id,'im-Fixture')
            self.assertEqual(lookup.call_args.kwargs,{'retry':None,'timeout':10})

    def test_remote_probe_validates_source_and_preserves_exact_raw_bytes(self):
        request = self.req()
        with patch.object(probe, 'runtime_container_id', return_value='ta-Complete'), patch.object(probe.sys,'version_info',(3,12,0)), patch('builtins.print') as emit:
            result = probe.probe(request)
        self.assertEqual(result, self.completed(request))
        marker = json.loads(emit.call_args.args[0].removeprefix(probe.START_PREFIX))
        self.assertEqual(marker['containerId'], 'ta-Complete')
        self.assertEqual(marker['payloadSha256'], hashlib.sha256(bytes(range(32))).hexdigest())
        self.assertNotIn('TOKEN', json.dumps(result))

    def test_remote_probe_denies_foreign_payload_source_expiry_and_expanded_authority(self):
        for key, value in [('payloadBase64','bad'), ('sourceSha256','0'*64), ('generation','other'),
                           ('deadlineMs',0), ('deadlineMs',int((time.time()+200)*1000)), ('schemaVersion',True)]:
            request = self.req()
            request[key] = value
            with self.subTest(key=key), patch.object(probe,'runtime_container_id',return_value='ta-Complete'), patch.object(probe.sys,'version_info',(3,12,0)), patch('builtins.print') as emit:
                with self.assertRaises(ValueError):
                    probe.probe(request)
                emit.assert_not_called()

    def test_remote_hold_is_twenty_seconds_and_does_not_return_after_expiry(self):
        request = self.req('hold')
        with patch.object(probe,'runtime_container_id',return_value='ta-Hold'), patch.object(probe.sys,'version_info',(3,12,0)), patch.object(probe.time,'sleep') as sleep, patch('builtins.print'):
            probe.probe(request)
        sleep.assert_called_once_with(20)
        with patch.object(probe,'runtime_container_id',return_value='ta-Hold'), patch.object(probe.sys,'version_info',(3,12,0)), patch.object(probe.time,'sleep'), patch.object(probe.time,'time',side_effect=[100,100,281]), patch('builtins.print'):
            request['deadlineMs'] = 280000
            with self.assertRaisesRegex(ValueError,'before receipt'):
                probe.probe(request)

    def test_hold_with_insufficient_authority_cannot_emit_start_or_spawn(self):
        request = self.req('hold')
        request['deadlineMs'] = int((time.time()+10)*1000)
        with patch.object(probe,'runtime_container_id',return_value='ta-Hold'), patch.object(probe.sys,'version_info',(3,12,0)), patch.object(probe.time,'sleep') as sleep, patch('builtins.print') as emit:
            with self.assertRaisesRegex(ValueError,'Insufficient hold'):
                probe.probe(request)
        emit.assert_not_called()
        sleep.assert_not_called()
        self.output.mkdir()
        function, runner, state = self.fake_calls()
        with self.assertRaisesRegex(ValueError,'before hold spawn'):
            seam.execute_calls(function,self.plan,APP,self.output,runner=runner,wall=lambda:100,execution_deadline=120)
        self.assertEqual(len(state.calls),1)
        self.assertEqual(state.cancels,[])

    def test_two_calls_verify_immutable_local_receipt_and_running_container_cancellation(self):
        self.output.mkdir()
        function, runner, state = self.fake_calls()
        result = seam.execute_calls(function, self.plan, APP, self.output, runner=runner, monotonic=lambda:state.clock)
        self.assertEqual(len(state.calls), 2)
        self.assertEqual(state.cancels, [{'terminate_containers':True}])
        self.assertTrue(result['cancellation']['containerAbsentBeforeAppStop'])
        self.assertTrue(result['cancellation']['sameContainerAsCompletion'])
        self.assertEqual(result['completion']['containerId'], result['cancellation']['containerId'])
        self.assertIsNone(result['completion']['cost'])
        path = self.output/result['completion']['localArtifact']
        self.assertEqual(path.read_bytes(), bytes(range(32)))
        with self.assertRaises(FileExistsError):
            seam.receipt(state.calls[0], self.completed(state.calls[0]), self.output)

    def test_bad_receipt_blocks_second_call_and_mismatched_marker_blocks_cancel(self):
        for bad in ('bad_receipt','bad_marker'):
            output = self.root/bad
            output.mkdir()
            function, runner, state = self.fake_calls(**{bad:True})
            with self.assertRaises(ValueError):
                seam.execute_calls(function, self.plan, APP, output, runner=runner)
            self.assertEqual(len(state.calls), 1 if bad=='bad_receipt' else 2)
            self.assertEqual(state.cancels, [])

    def test_replacement_container_fails_before_cancel_and_cannot_pass_one_container_scope(self):
        self.output.mkdir()
        function, runner, state = self.fake_calls(different_container=True)
        with self.assertRaisesRegex(ValueError, 'reuse the same owned container'):
            seam.execute_calls(function, self.plan, APP, self.output, runner=runner)
        self.assertEqual(len(state.calls), 2)
        self.assertEqual(state.cancels, [])

    def test_cancel_denial_is_not_retried_and_slow_natural_absence_cannot_pass(self):
        for option, error in [('cancel_error',PermissionError), ('slow',TimeoutError)]:
            output = self.root/option
            output.mkdir()
            function, runner, state = self.fake_calls(**{option:True})
            with self.assertRaises(error):
                seam.execute_calls(function, self.plan, APP, output, runner=runner, monotonic=lambda:state.clock)
            self.assertEqual(len(state.calls),2)
            self.assertLessEqual(len(state.cancels),1)

    def test_foreign_duplicate_or_malformed_metadata_cannot_prove_absence_or_broaden_stop(self):
        for rows in ([{'container_id':'ta-Foreign','app_id':FOREIGN_APP,'app_name':'x','start_time':'x'}],
                     [{'container_id':'ta-X','app_id':APP,'app_name':'x','start_time':'x'}]*2,
                     {'not':'a list'}, [{'container_id':'ta-X','app_id':APP,'app_name':'x','start_time':'x'}]*65):
            calls = []
            def runner(argv, **kwargs):
                calls.append(argv)
                if 'stop' in argv:
                    return SimpleNamespace(returncode=0,stdout=b'',stderr=b'')
                return SimpleNamespace(returncode=0, stdout=json.dumps(rows).encode(), stderr=b'')
            with self.assertRaises(ValueError):
                seam.contain(APP,runner)
            self.assertEqual(sum('stop' in call for call in calls),1)
            self.assertIn(APP,calls[0])

    def test_app_names_and_short_or_long_identifiers_never_reach_cleanup_cli(self):
        calls = []
        for identity in ('ap-Owned','ct-modal-test','ap-'+'A'*21,'ap-'+'A'*23,APP+'\n'):
            for operation in (seam.contain,seam.active_containers):
                with self.subTest(identity=identity,operation=operation.__name__), self.assertRaises(ValueError):
                    operation(identity,lambda *a,**k:calls.append(a))
        self.assertEqual(calls,[])

    def test_stop_acknowledgement_requires_both_exact_app_lists_empty(self):
        calls = []
        def runner(argv, **kwargs):
            calls.append(argv)
            if 'stop' in argv:
                return SimpleNamespace(returncode=0,stdout=b'',stderr=b'')
            return SimpleNamespace(returncode=0,stdout=b'[]',stderr=b'')
        result = seam.contain(APP,runner)
        self.assertTrue(result['confirmed'])
        self.assertEqual(sum('stop' in call for call in calls),1)
        self.assertIn(APP, calls[-1])
        def active(argv, **kwargs):
            if 'stop' in argv:
                return SimpleNamespace(returncode=0,stdout=b'',stderr=b'')
            if 'Sandbox.list' in argv[2]:
                return SimpleNamespace(returncode=0,stdout=b'["sbActive"]',stderr=b'')
            return SimpleNamespace(returncode=0,stdout=b'[]',stderr=b'')
        with patch.object(seam.time,'sleep'), self.assertRaises(TimeoutError):
            seam.contain(APP,active)

    def test_diagnostics_never_preserve_raw_api_auth_text(self):
        error = PermissionError('Authorization: Bearer DO-NOT-RECORD https://secret.invalid/?token=private')
        value = seam.diagnostic(error,'cancel')
        self.assertEqual(value['errorClass'],'PermissionError')
        self.assertNotIn('DO-NOT-RECORD',json.dumps(value))
        self.assertNotIn('secret.invalid',json.dumps(value))

    def fake_process(self, *, exit_code=0, hangs=False, unknown=False, cleanup_denied=False):
        test = self
        class Process:
            pid = 999999
            running = True
            def __init__(self, argv, **options):
                self.options = options
                seam.save(test.output/'ownership.json',test.owner(None if unknown else APP))
                seam.save(test.output/'worker-result.json',{'passed':exit_code==0})
                seam.save(test.output/'observations.json',{'stage':'calls-verified','callIds':['fc-Complete','fc-Hold'],'completion':{},'cancellation':{}})
            def wait(self, timeout):
                if hangs and self.running:
                    raise subprocess.TimeoutExpired('fixture',timeout)
                self.running = False
                return exit_code
            def poll(self):
                return None if self.running else exit_code
        cleanup_calls, terminations = [], []
        def cleanup(identity):
            cleanup_calls.append(identity)
            if cleanup_denied:
                raise PermissionError('DO-NOT-RECORD')
            return {'confirmed':True,'appId':identity,'activeContainers':0,'activeSandboxes':0}
        def terminate(process):
            terminations.append(process.pid)
            process.running = False
        return Process, cleanup, terminate, cleanup_calls, terminations

    def test_supervisor_proves_cleanup_after_success_and_blocks_suite_reuse_across_output_dirs(self):
        process, cleanup, terminate, calls, _ = self.fake_process()
        result = seam.supervise(self.root,self.plan,self.approval(),self.output,process_factory=process,cleanup=cleanup,terminate=terminate)
        self.assertTrue(result['passed'])
        self.assertEqual(calls,[APP])
        with self.assertRaises(FileExistsError):
            seam.supervise(self.root,self.plan,self.approval(),self.root/'other',process_factory=process,cleanup=cleanup)

    def test_independent_cleanup_survives_worker_failure_and_hang(self):
        for hangs in (False,True):
            self.plan = seam.prepare(self.root,'ct-modal-hang' if hangs else 'ct-modal-fail','fixture-workspace','im-Fixture')
            self.output = self.root/('hang' if hangs else 'fail')
            process, cleanup, terminate, calls, terminations = self.fake_process(exit_code=1,hangs=hangs)
            result = seam.supervise(self.root,self.plan,self.approval(),self.output,process_factory=process,cleanup=cleanup,terminate=terminate)
            self.assertFalse(result['passed'])
            self.assertTrue(result['cleanup']['confirmed'])
            self.assertEqual(calls,[APP])
            self.assertEqual(terminations,[999999] if hangs else [])

    def test_unknown_owner_and_cleanup_denial_remain_failures_without_name_adoption(self):
        for unknown in (False,True):
            self.plan = seam.prepare(self.root,'ct-modal-unknown' if unknown else 'ct-modal-denied','fixture-workspace','im-Fixture')
            self.output = self.root/('unknown' if unknown else 'denied')
            process, cleanup, terminate, calls, _ = self.fake_process(unknown=unknown,cleanup_denied=True)
            result = seam.supervise(self.root,self.plan,self.approval(),self.output,process_factory=process,cleanup=cleanup,terminate=terminate)
            self.assertFalse(result['passed'])
            self.assertFalse(result['cleanup']['confirmed'])
            self.assertEqual(calls,[] if unknown else [APP])
            self.assertNotIn('DO-NOT-RECORD',(self.output/'outcome.json').read_text())
            self.assertTrue((self.output/'ownership.json').exists())

    def test_cleanup_after_expiry_keeps_exact_original_scope_and_history(self):
        self.output.mkdir()
        seam.claim_execution(self.root,self.plan,self.output)
        seam.save(self.output/'plan.json',self.plan)
        seam.save(self.output/'approval.json',self.approval())
        seam.save(self.output/'ownership.json',self.owner())
        _, cleanup, _, calls, _ = self.fake_process()
        # Source repairs/removal cannot disable receipt-bound recovery cleanup.
        (self.root/'coppertop/modal_seam_probe.py').unlink()
        with patch.object(seam.time,'time',return_value=self.plan['expiresAt']+100):
            seam.cleanup_receipt(self.root,self.plan,self.output,cleanup=cleanup)
        self.assertEqual(len((self.output/'cleanup-history.jsonl').read_text().splitlines()),1)
        owner = self.owner()
        owner['suiteId'] = 'foreign'
        seam.save(self.output/'ownership.json',owner)
        with self.assertRaises(ValueError):
            seam.cleanup_receipt(self.root,self.plan,self.output,cleanup=cleanup)
        self.assertEqual(calls,[APP])

    def test_cleanup_requires_original_saved_approval_and_claim_without_source_reads(self):
        self.output.mkdir()
        seam.claim_execution(self.root,self.plan,self.output)
        seam.save(self.output/'plan.json',self.plan)
        approval = self.approval()
        seam.save(self.output/'approval.json',approval)
        seam.save(self.output/'ownership.json',self.owner())
        calls = []
        approval['cleanupAuthorized'] = False
        seam.save(self.output/'approval.json',approval)
        with self.assertRaises(ValueError):
            seam.cleanup_receipt(self.root,self.plan,self.output,cleanup=lambda identity:calls.append(identity))
        seam.save(self.output/'approval.json',self.approval())
        seam.save(self.root/'.local/modal-seam-executions'/ (self.plan['suiteId']+'.json'),
                  {'planSha256':seam.sha(seam.canonical(self.plan)),'output':'foreign-directory'})
        with self.assertRaises(ValueError):
            seam.cleanup_receipt(self.root,self.plan,self.output,cleanup=lambda identity:calls.append(identity))
        self.assertEqual(calls,[])

    def test_pre_tls_original_manifest_remains_cleanup_only_after_controller_upgrade(self):
        self.plan.pop('imageRecipe')
        self.plan['sourceSha256'] = {name:self.plan['sourceSha256'][name] for name in seam.LEGACY_SOURCE_FILES}
        self.plan['functionOptions'] = json.loads(seam.canonical(seam.LEGACY_OPTIONS))
        with self.assertRaises(ValueError):
            seam.require_approval(self.root,self.plan,self.approval())
        self.output.mkdir()
        seam.claim_execution(self.root,self.plan,self.output)
        seam.save(self.output/'plan.json',self.plan)
        seam.save(self.output/'approval.json',self.approval())
        seam.save(self.output/'ownership.json',self.owner())
        _, cleanup, _, calls, _ = self.fake_process()
        result = seam.cleanup_receipt(self.root,self.plan,self.output,cleanup=cleanup)
        self.assertTrue(result['confirmed'])
        self.assertEqual(calls,[APP])

    def test_direct_or_replayed_worker_is_refused_without_erasing_prior_ownership(self):
        self.output.mkdir()
        seam.save(self.output/'ownership.json',self.owner())
        original = (self.output/'ownership.json').read_bytes()
        with self.assertRaises(FileNotFoundError):
            seam.worker(self.root,self.plan,self.approval(),self.output)
        self.assertEqual((self.output/'ownership.json').read_bytes(),original)


if __name__ == '__main__':
    unittest.main()
