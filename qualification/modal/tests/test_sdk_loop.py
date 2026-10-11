"""Real pinned SDK lifecycle, hydration and exec streams; fake transport only."""
import asyncio
import io
import json
import shutil
import tarfile
import tempfile
import time
from contextlib import asynccontextmanager
from pathlib import Path
from types import SimpleNamespace
import sys
import unittest
from unittest.mock import patch

sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'executor'))
import controller
from common import IDENTITY
import common
from modal_backend import ModalBackend
try:
    from tls import bootstrap
    bootstrap()
    from modal.client import _Client
    from modal.sandbox import _Sandbox
    from modal._utils.task_command_router_client import TaskCommandRouterClient
    from modal._utils.async_utils import synchronizer
    from modal_proto import api_pb2 as p
    from modal_proto import task_command_router_pb2 as sr
except ImportError:
    p = None


@unittest.skipUnless(p is not None,'Pinned SDK environment required')
class SDKLoopTests(unittest.TestCase):
    def test_wrong_loop_fails_before_credentials_or_transport(self):
        async def case():
            b=ModalBackend()
            with patch.object(_Client,'from_credentials',side_effect=AssertionError('auth must not start')) as auth:
                with self.assertRaisesRegex(ValueError,'SDK loop'):
                    await b.open()
                auth.assert_not_called()
        asyncio.run(case())

    def test_real_client_hydration_and_worker_exec_use_one_sdk_loop(self):
        events=[];outputs={};stdin_payloads=[]
        app='ap-'+'A'*22;sid='sb-'+'C'*22
        class Stub:
            def __getattr__(self,name):
                async def call(request,**kwargs):
                    events.append(name)
                    if name=='WorkspaceNameLookup':return p.WorkspaceNameLookupResponse(username=IDENTITY['workspace'])
                    if name=='EnvironmentList':
                        response=p.EnvironmentListResponse()
                        response.items.add(name='main',environment_id=IDENTITY['environment_id'])
                        return response
                    if name=='TaskGetCommandRouterAccess':return p.TaskGetCommandRouterAccessResponse(url='https://worker.fixture.modal.run',jwt='fixture-jwt')
                    if name=='AppCreate':return p.AppCreateResponse(app_id=app)
                    if name=='SandboxCreate':return p.SandboxCreateResponse(sandbox_id=sid,metadata=p.SandboxHandleMetadata(app_id=app))
                    raise AssertionError('unexpected mocked RPC '+name)
                return call
        class Router:
            def __init__(self):self._stub=self
            @asynccontextmanager
            async def _lease(self):yield
            def _get_metadata(self):return []
            async def exec_start(self,request):raise AssertionError('adapter must use its single raw start')
            async def TaskExecStart(self,request,**kwargs):
                events.append('TaskExecStart')
                outputs[request.exec_id]=b'{"status":"passed","fixture_worker":true}\n'
                return sr.TaskExecStartResponse()
            async def exec_stdio_read(self,task_id,exec_id,file_descriptor,deadline,start_offset=0):
                if file_descriptor==p.FILE_DESCRIPTOR_STDOUT:
                    yield SimpleNamespace(data=outputs[exec_id])
            async def exec_stdin_write_stream(self,*,task_id,exec_id,source):
                raw=source.read();stdin_payloads.append(raw);return len(raw)
            async def exec_wait(self,*args):return sr.TaskExecWaitResponse(code=0)
        router=Router()
        async def get_stub(client,url):return Stub()
        async def get_task(sandbox,**kwargs):return 'ta-FIXTURE'
        async def get_router(sandbox,task):return router
        async def connect(cls,*args,**kwargs):return router
        async def case():
            b=ModalBackend()
            await b.open()
            self.assertIs(b.client._cancellation_context_event_loop,asyncio.get_running_loop())
            self.assertIs(asyncio.get_running_loop(),synchronizer._get_loop(start=False))
            created=await b.create_app('ct-qual-'+'a'*12)
            sandbox=await b.create_sandbox(created,'cpu')
            self.assertIsInstance(b.sandboxes[sandbox],_Sandbox)
            self.assertFalse(b.sandboxes[sandbox]._is_v2)
            # Real Sandbox.exec and ContainerProcess stream/write/wait handling.
            # The worker's CPU/GPU payload is deliberately a fixture, never model execution.
            for raw in (b'fixture-upload',b''):
                output=await b.exec(sandbox,['/opt/conda/bin/python','fixture_worker.py'],5,stdin=raw)
                self.assertEqual(output,b'{"status":"passed","fixture_worker":true}\n')
            await b.close();self.assertTrue(b.client.is_closed())
        with patch.object(_Client,'_get_stub',get_stub),patch.object(_Sandbox,'_get_task_id',get_task),\
             patch.object(_Sandbox,'_get_command_router_client',get_router),patch.object(TaskCommandRouterClient,'_connect',classmethod(connect)),\
             patch.dict('os.environ',{'MODAL_TOKEN_ID_BACKUP':'fixture','MODAL_TOKEN_SECRET_BACKUP':'fixture'}):
            controller.run_on_sdk_loop(case)
        self.assertEqual(events,['WorkspaceNameLookup','EnvironmentList','AppCreate','SandboxCreate','TaskGetCommandRouterAccess','TaskExecStart','TaskExecStart'])
        self.assertEqual(stdin_payloads,[b'fixture-upload',b''])

    def test_invalid_plan_prevents_sdk_bridge(self):
        with patch.object(controller,'require_approval',side_effect=ValueError('unready')) as gate,\
             patch.object(controller,'run_on_sdk_loop',side_effect=AssertionError('SDK bridge must not start')) as bridge:
            with self.assertRaises(ValueError):controller.launch(Path('/tmp/fixture'),{})
            gate.assert_called_once();bridge.assert_not_called()

    def test_full_controller_upload_worker_commands_handoff_and_cleanup_real_sdk(self):
        # Real local gates, payload, client, hydration, exec and receipt validation.
        # Provider/worker transport remains a fixture: zero native model execution.
        app='ap-'+'A'*22;events=[];processes={};stopped=[False]
        class Stub:
            def __getattr__(self,name):
                async def call(request,**kwargs):
                    events.append(name)
                    if name=='WorkspaceNameLookup':return p.WorkspaceNameLookupResponse(username=IDENTITY['workspace'])
                    if name=='EnvironmentList':
                        response=p.EnvironmentListResponse();response.items.add(name='main',environment_id=IDENTITY['environment_id']);return response
                    if name=='ImageFromId':
                        response=p.ImageFromIdResponse(image_id=common.IMAGE);response.metadata.python_version_info='Python 3.11.13 fixture';return response
                    if name=='WorkspaceBillingRates':return p.WorkspaceBillingRatesResponse(rates={
                        'cpu_hour_cost_sandbox':'0.141900','mem_gib_hour_cost_sandbox':'0.024000',
                        'gpu_hour_cost_a100_80gb':'2.50000','egress_gib_cost':'0.04000'})
                    if name=='TaskGetCommandRouterAccess':return p.TaskGetCommandRouterAccessResponse(url='https://worker.fixture.modal.run',jwt='fixture-jwt')
                    if name=='AppCreate':return p.AppCreateResponse(app_id=app)
                    if name=='SandboxCreate':
                        stage='gpu' if request.definition.resources.gpu_config.count else 'cpu'
                        return p.SandboxCreateResponse(sandbox_id='sb-'+('C' if stage=='cpu' else 'G')*22,metadata=p.SandboxHandleMetadata(app_id=app))
                    if name=='AppGetInfo':return p.AppGetInfoResponse(info=p.AppHandleMetadata(app_id=app,environment_name='main'))
                    if name=='AppGetLifecycle':return p.AppGetLifecycleResponse(lifecycle=p.AppLifecycle(app_state=p.APP_STATE_STOPPED if stopped[0] else p.APP_STATE_EPHEMERAL))
                    if name=='TaskList':return p.TaskListResponse()
                    if name=='SandboxList':return p.SandboxListResponse()
                    if name=='AppStop':stopped[0]=True;return p.AppStopResponse()
                    if name=='SandboxTerminate':return p.SandboxTerminateResponse()
                    if name=='AppHeartbeat':return p.AppHeartbeatResponse()
                    raise AssertionError('unexpected fake RPC '+name)
                return call
        class Router:
            def __init__(self):self._stub=self
            @asynccontextmanager
            async def _lease(self):yield
            def _get_metadata(self):return []
            async def exec_start(self,request):raise AssertionError('must use single raw start')
            async def TaskExecStart(self,request,**kwargs):
                args=list(request.command_args);event=asyncio.Event()
                processes[request.exec_id]={'args':args,'event':event,'output':b''}
                events.append('upload' if '-c' in args else 'worker-'+args[args.index('--kind')+1])
                return sr.TaskExecStartResponse()
            async def exec_stdin_write_stream(self,*,task_id,exec_id,source):
                item=processes[exec_id];args=item['args'];raw=source.read()
                if raw:
                    with tarfile.open(fileobj=io.BytesIO(raw),mode='r:') as archive:count=len(archive.getmembers())
                    output={'installed_files':count,'archive_sha256':common.sha(raw)}
                else:
                    stage=args[args.index('--kind')+1]
                    phases=['fresh-restored-hash','three-interpreter-dependencies','actual-native-processors'] if stage=='cpu' else [
                        'fresh-restored-hash','actor-load','clef-load','stock-actor-call','disabled-clef-actor-call',
                        'official-clef-joint-head-call','exact-decoded-action-parity']
                    output={'status':'passed','plan_sha256':args[args.index('--plan-sha256')+1],
                        'execution_manifest_sha256':plan['execution_manifest_sha256'],'run_id':args[args.index('--run-id')+1],
                        'stage':stage,'owned_direct_children_reaped':True,'child_cleanup_errors':[],
                        'training_steps':0,'simulator_controls':0,'actor_calls':2,'clef_calls':1,
                        'phases':[{'phase':name,'result':{'files':37,'finite':True},'seconds':0}for name in phases]}
                item['output']=common.canonical(output);item['event'].set();return len(raw)
            async def exec_stdio_read(self,task_id,exec_id,file_descriptor,deadline,start_offset=0):
                item=processes[exec_id];await item['event'].wait()
                if file_descriptor==p.FILE_DESCRIPTOR_STDOUT:yield SimpleNamespace(data=item['output'])
            async def exec_wait(self,task_id,exec_id,deadline):
                await processes[exec_id]['event'].wait();return sr.TaskExecWaitResponse(code=0)
        router=Router()
        async def get_stub(client,url):return Stub()
        async def get_task(sandbox,**kwargs):return 'ta-FIXTURE'
        async def get_router(sandbox,task):return router
        async def connect(cls,*args,**kwargs):return router
        async def case(root,approval):
            # The observer remains a fixture, while all provider SDK operations are real.
            from test_executor import ObserverDouble
            backend=ModalBackend();backend.events=events;backend.failure=None;backend.active=False
            return await controller.execute(root,approval,backend=backend,
                observer_factory=lambda sealed,output,record:ObserverDouble(backend,output))
        original=Path(__file__).resolve().parents[1]
        with tempfile.TemporaryDirectory() as temp:
            root=Path(temp)
            names=['plan.json','execution-manifest.json']+[x['path']for x in common.read_json(original/'execution-manifest.json')['files']]
            for name in names:
                target=root/name;target.parent.mkdir(parents=True,exist_ok=True);shutil.copyfile(original/name,target)
            plan=common.read_json(root/'plan.json');plan['executable_ready']=True;common.durable(root/'plan.json',plan)
            approval={'approved':True,'plan_sha256':common.sha((root/'plan.json').read_bytes()),**IDENTITY,
                'modal_allowance_usd':'2.00','one_attempt':True,'bounded_exposure_accepted':True,
                'cleanup_authorized':True,'rate_ceiling_confirmed':True,'r2_actions':False,'expires_at':time.time()+300}
            with patch.object(_Client,'_get_stub',get_stub),patch.object(_Sandbox,'_get_task_id',get_task),\
                 patch.object(_Sandbox,'_get_command_router_client',get_router),patch.object(TaskCommandRouterClient,'_connect',classmethod(connect)),\
                 patch.dict('os.environ',{'MODAL_TOKEN_ID_BACKUP':'fixture','MODAL_TOKEN_SECRET_BACKUP':'fixture'}):
                state,output=controller.run_on_sdk_loop(case,root,approval)
            self.assertEqual(state['status'],'passed');self.assertTrue(state['cleanup']['confirmed'])
            self.assertEqual(state['provider_creates_attempted'],3)
            self.assertEqual(events.count('AppCreate'),1);self.assertEqual(events.count('SandboxCreate'),2)
            self.assertEqual(events.count('upload'),2);self.assertEqual(events.count('worker-cpu'),1);self.assertEqual(events.count('worker-gpu'),1)
            self.assertLess(events.index('independent-cpu-audit'),events.index('worker-gpu'))
            self.assertTrue(state['client_closed']);self.assertTrue(stopped[0])


if __name__=='__main__':unittest.main()
