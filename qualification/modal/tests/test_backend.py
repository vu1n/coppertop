"""Pinned SDK protobuf checks with mock stubs; no auth or provider RPCs."""
import asyncio
import json
import time
from pathlib import Path
from types import SimpleNamespace
import sys
import unittest
from unittest.mock import patch
sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'executor'))
from common import IMAGE,STAGES
from modal_backend import ModalBackend
try:
    from tls import bootstrap
    bootstrap()
    from modal_proto import api_pb2 as p
except ImportError:p=None


@unittest.skipUnless(p is not None,'Run this file in the pinned Modal SDK venv')
class BackendTests(unittest.TestCase):
    def backend(self,responses):
        b=ModalBackend();b.p=p;calls=[]
        class Stub:
            def __getattr__(self,name):
                async def call(request,**kwargs):
                    calls.append((name,request,kwargs))
                    value=responses[name]
                    if callable(value):return value(request)
                    if isinstance(value,Exception):raise value
                    return value
                return call
        b.client=SimpleNamespace(_stub=Stub());return b,calls

    def test_allocating_requests_are_single_attempt_fixed_v1_and_no_extra_resources(self):
        async def run():
            b,calls=self.backend({'AppCreate':p.AppCreateResponse(app_id='ap-'+'A'*22),
                'SandboxCreate':p.SandboxCreateResponse(sandbox_id='sb-'+'C'*22)})
            with patch('modal.sandbox._Sandbox._new_hydrated',return_value=SimpleNamespace(_is_v2=False)):
                app=await b.create_app('ct-qual-'+'a'*12)
                for stage in STAGES:await b.create_sandbox(app,stage)
            with self.assertRaises(ValueError):await b.create_app('ct-qual-'+'a'*12)
            with self.assertRaises(ValueError):await b.create_sandbox(app,'cpu')
            self.assertEqual([x[0]for x in calls],['AppCreate','SandboxCreate','SandboxCreate'])
            self.assertEqual(calls[0][1].app_state,p.APP_STATE_EPHEMERAL)
            for (_,req,kw),stage in zip(calls[1:],STAGES):
                self.assertIsNone(kw['retry']);self.assertEqual(req.app_id,app)
                d=req.definition;options=STAGES[stage]
                self.assertEqual(d.image_id,IMAGE);self.assertTrue(d.block_network)
                self.assertFalse(d.enable_snapshot);self.assertFalse(d.include_oidc_identity_token)
                for name in ('secret_ids','mount_ids','volume_mounts','cloud_bucket_mounts','nfs_mounts','s3_mounts'):
                    self.assertEqual(len(getattr(d,name)),0)
                self.assertEqual((d.resources.milli_cpu,d.resources.milli_cpu_max),(4000,4000))
                self.assertEqual((d.resources.memory_mb,d.resources.memory_mb_max),options['memory'])
                self.assertEqual(d.timeout_secs,options['timeout']);self.assertEqual(d.idle_timeout_secs,20)
                self.assertEqual(d.resources.gpu_config.count,int(stage=='gpu'))
        asyncio.run(run())

    def test_lost_allocation_is_consumed_without_retry(self):
        async def run():
            b,calls=self.backend({'SandboxCreate':TimeoutError('synthetic lost response')})
            with self.assertRaises(TimeoutError):await b.create_sandbox('ap-'+'A'*22,'cpu')
            with self.assertRaises(ValueError):await b.create_sandbox('ap-'+'A'*22,'cpu')
            self.assertEqual(len(calls),1);self.assertIsNone(calls[0][2]['retry'])
        asyncio.run(run())

    def test_actual_account_rate_rpc_requires_all_finite_bounded_rates(self):
        async def run(rates):
            b,calls=self.backend({'WorkspaceBillingRates':p.WorkspaceBillingRatesResponse(rates=rates)})
            result=await b.billing_rates();self.assertEqual(len(calls),1);return result
        good={'cpu_hour_cost_sandbox':'0.141900','mem_gib_hour_cost_sandbox':'0.024000',
              'gpu_hour_cost_a100_80gb':'2.50000','egress_gib_cost':'0.04000'}
        self.assertTrue(asyncio.run(run(good))['verified_before_allocation'])
        for bad in ({}, {**good,'gpu_hour_cost_a100_80gb':'2.6'}, {**good,'egress_gib_cost':'NaN'},
                    {**good,'mem_gib_hour_cost_sandbox':'-1'}):
            with self.assertRaises(ValueError):asyncio.run(run(bad))

    def test_audit_rejects_foreign_task_sandbox_and_nonprogress_pagination(self):
        async def run(mode):
            app='ap-'+'A'*22
            tasks=p.TaskListResponse()
            if mode=='foreign-task':tasks.tasks.add(app_id='ap-'+'F'*22,task_id='ta-TEST')
            pages=[]
            if mode in ('foreign-sandbox','cursor'):
                page=p.SandboxListResponse();page.sandboxes.add(id='sb-'+'C'*22,
                    app_id=('ap-'+'F'*22 if mode=='foreign-sandbox' else app),created_at=1);pages=[page,page]
            def listing(_):return pages.pop(0) if pages else p.SandboxListResponse()
            b,calls=self.backend({'AppGetInfo':p.AppGetInfoResponse(info=p.AppHandleMetadata(app_id=app,environment_name='main')),
                'AppGetLifecycle':p.AppGetLifecycleResponse(lifecycle=p.AppLifecycle(app_state=p.APP_STATE_STOPPED)),
                'TaskList':tasks,'SandboxList':listing})
            result=await b.audit(app,stopped=True)
            self.assertTrue(result['confirmed']);self.assertEqual(result['active_containers'],0)
            for name,req,kwargs in calls:
                self.assertIsNone(kwargs['retry']);self.assertEqual(req.app_id,app)
                if name in ('TaskList','SandboxList'):self.assertEqual(req.environment_name,'main')
                if name=='SandboxList':self.assertFalse(req.include_finished)
        asyncio.run(run('empty'))
        for mode in ('foreign-task','foreign-sandbox','cursor'):
            with self.assertRaises(ValueError):asyncio.run(run(mode))

    def failed_connection(self, mode):
        from controller import run_on_sdk_loop
        from modal._utils.grpc_utils import ModalChannel
        from python_socks import ProxyError
        events=[];opened=[]
        sid='sb-'+'C'*22
        async def case():
            b,calls=self.backend({'TaskGetCommandRouterAccess':p.TaskGetCommandRouterAccessResponse(
                url='https://worker.fixture.modal.run',jwt='opaque-ephemeral-fixture')})
            b.client._is_localhost=False;b.progress_callback=events.append
            async def task(**kwargs):
                if mode=='readiness-stall':await asyncio.sleep(2)
                return 'ta-FIXTURE'
            async def forbidden_exec(*args,**kwargs):raise AssertionError('must not start an exec')
            b.sandboxes[sid]=SimpleNamespace(_get_task_id=task,exec=forbidden_exec)
            b.creation_started[sid]=time.time()-14.8
            async def fake_socket_boundary(channel):
                opened.append(channel)
                if mode=='denial':raise ProxyError('opaque-secret https://user:auth@host/private?jwt=secret',error_code=403)
                await asyncio.sleep(2)
            with patch.object(ModalChannel,'_open_connection',fake_socket_boundary):
                with self.assertRaises(TimeoutError):await b.exec(sid,['fixture.py'],2,stdin=b'fixture')
                self.assertIs(ModalChannel._open_connection,fake_socket_boundary)
            self.assertEqual(b.exec_counts[sid],1)
            self.assertFalse(any(e['phase']=='exec-start-attempt'for e in events))
            self.assertFalse(any(e['phase']=='stdin-start'for e in events))
            if mode=='readiness-stall':self.assertEqual(calls,[])
        run_on_sdk_loop(case)
        self.assertNotIn('opaque-secret',json.dumps(events))
        self.assertNotIn('opaque-ephemeral-fixture',json.dumps(events))
        return events,opened

    def test_actual_sdk_router_denial_code_is_captured_before_retry_timeout(self):
        events,opened=self.failed_connection('denial')
        self.assertEqual(len(opened),1)
        error=next(e['error']for e in events if e['phase']=='router-transport-exception')
        self.assertEqual(error['exception_chain'][0]['error_code'],403)
        endpoint=next(e['endpoint']for e in events if e['phase']=='router-metadata-ack')
        self.assertEqual(endpoint,{'scheme':'https','host':'worker.fixture.modal.run','port':443})

    def test_actual_sdk_router_stall_is_cancelled_and_hook_restored(self):
        events,opened=self.failed_connection('connection-stall')
        self.assertEqual(len(opened),1)
        self.assertEqual(next(e['error']['type']for e in events if e['phase']=='router-transport-exception'),'CancelledError')

    def test_cumulative_deadline_cancels_readiness_before_router_or_exec(self):
        events,opened=self.failed_connection('readiness-stall')
        self.assertEqual(opened,[])
        self.assertFalse(any(e['phase']=='router-metadata-start'for e in events))

if __name__=='__main__':unittest.main()
