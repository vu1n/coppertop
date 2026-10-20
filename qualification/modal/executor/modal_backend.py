"""Pinned SDK 1.6.1 adapter. Instantiation is local; open() authenticates explicitly.

V1 allocating RPCs and exec starts are sent once. Read/stop RPCs are bounded,
explicitly scoped and never use the default credentials or config environment.
"""
import asyncio
import io
import json
import os
import re
import time
import urllib.parse
from diagnostics import error_evidence
from decimal import Decimal, InvalidOperation
from common import APP_ID, IDENTITY, IMAGE, SANDBOX_ID, STAGES


class ModalBackend:
    def __init__(self):
        self.client = None
        self.sandboxes = {}
        self.app_creates = 0
        self.created_stages = set()
        self.exec_counts = {}
        self.creation_started = {}
        self.progress_callback = None
        self.transient_secrets = []

    def emit(self, phase, **fields):
        if self.progress_callback:
            self.progress_callback({'phase':phase,'time':time.time(),**fields})

    async def open(self):
        if self.client is not None:
            raise ValueError('One explicit client per process')
        from tls import bootstrap
        bootstrap()
        # This application-only server selection does not write credentials or profiles.
        os.environ['MODAL_SERVER_URL'] = 'https://api.modal.com'
        from modal.client import _Client
        from modal._utils.async_utils import synchronizer
        from modal_proto import api_pb2
        from google.protobuf.empty_pb2 import Empty
        if asyncio.get_running_loop() is not synchronizer._get_loop(start=False):
            raise ValueError('Whole provider lifecycle must use the pinned SDK loop before authentication')
        if not all(os.environ.get(name) for name in ('MODAL_TOKEN_ID_BACKUP','MODAL_TOKEN_SECRET_BACKUP')):
            raise ValueError('Already-injected backup pair absent')
        self.client = await asyncio.wait_for(_Client.from_credentials(
            os.environ['MODAL_TOKEN_ID_BACKUP'], os.environ['MODAL_TOKEN_SECRET_BACKUP']),15)
        self.p = api_pb2
        if self.client._is_localhost:
            raise ValueError('Official verified remote TLS endpoint required')
        workspace = await self.rpc('WorkspaceNameLookup',Empty())
        environments = await self.rpc('EnvironmentList',Empty())
        found = [row for row in environments.items if row.name == IDENTITY['environment']]
        if (workspace.username != IDENTITY['workspace'] or len(found)!=1
                or found[0].environment_id != IDENTITY['environment_id']):
            raise ValueError('Authenticated existing workspace/environment differs')
        return dict(IDENTITY)

    async def close(self):
        for sandbox in self.sandboxes.values():
            router=getattr(sandbox,'_command_router_client',None)
            if router is not None:
                try:await asyncio.wait_for(router.close(),2)
                except Exception:pass
        if self.client is not None:
            await asyncio.wait_for(self.client._close(),5)

    async def rpc(self, name, request, timeout=10):
        if self.client is None:
            raise ValueError('Explicit authenticated client required')
        return await asyncio.wait_for(getattr(self.client._stub,name)(
            request,retry=None,timeout=timeout),timeout+.5)

    async def image_lookup(self):
        response = await self.rpc('ImageFromId',self.p.ImageFromIdRequest(image_id=IMAGE))
        if response.image_id != IMAGE or not re.match(r'^Python 3\.11\.13(?:\s|$)',response.metadata.python_version_info):
            raise ValueError('Selected immutable image identity/Python metadata differs')
        return {'image_id':IMAGE,'builds':0}

    async def billing_rates(self):
        response=await self.rpc('WorkspaceBillingRates',self.p.WorkspaceBillingRatesRequest())
        ceilings={'cpu_hour_cost_sandbox':'0.141912','mem_gib_hour_cost_sandbox':'0.024012',
                  'gpu_hour_cost_a100_80gb':'2.50002','egress_gib_cost':'0.04'}
        rates={}
        for key,ceiling in ceilings.items():
            if key not in response.rates:raise ValueError('Required authenticated account rate absent')
            try:value=Decimal(response.rates[key])
            except InvalidOperation:raise ValueError('Malformed authenticated account rate') from None
            if not value.is_finite() or value<0 or value>Decimal(ceiling):
                raise ValueError('Authenticated account rate exceeds approved reserve ceiling')
            rates[key]=str(value)
        return {**IDENTITY,'rates':rates,'ceilings':ceilings,'checked_at':time.time(),
                'source':'official WorkspaceBillingRates RPC','verified_before_allocation':True}

    async def create_app(self, run_id):
        self.app_creates += 1
        if self.app_creates != 1:
            raise ValueError('No App allocation retry')
        response = await self.rpc('AppCreate',self.p.AppCreateRequest(description=run_id,
            environment_name='main',app_state=self.p.APP_STATE_EPHEMERAL))
        if not re.fullmatch(APP_ID,response.app_id):
            raise ValueError('New exact observed App ID required')
        return response.app_id

    async def heartbeat(self, app_id):
        if not re.fullmatch(APP_ID,app_id):raise ValueError('Owned App ID required')
        await self.rpc('AppHeartbeat',self.p.AppHeartbeatRequest(app_id=app_id),timeout=5)

    async def create_sandbox(self, app_id, stage):
        if not re.fullmatch(APP_ID,app_id) or stage not in STAGES or stage in self.created_stages:
            raise ValueError('One exact owned allocation per fixed stage')
        self.created_stages.add(stage)  # Consumed before the allocating RPC, including lost responses.
        started=time.time()
        self.emit('sandbox-create-attempt',stage=stage,app_id=app_id)
        from modal._resources import convert_fn_config_to_resources_config
        from modal.sandbox import _Sandbox
        options = STAGES[stage]
        network = _SandboxNetwork(self.p)
        definition = self.p.Sandbox(entrypoint_args=['/bin/sleep',str(options['timeout'])],
            image_id=IMAGE,secret_ids=[],mount_ids=[],resources=convert_fn_config_to_resources_config(
                cpu=options['cpu'],memory=options['memory'],gpu=options['gpu']),
            timeout_secs=options['timeout'],idle_timeout_secs=options['idle_timeout'],
            network_access=network,block_network=True,enable_snapshot=False,
            include_oidc_identity_token=False,runtime_debug=False,
            name=None,workdir='/tmp')
        response = await self.rpc('SandboxCreate',self.p.SandboxCreateRequest(
            app_id=app_id,definition=definition),timeout=10)
        if not re.fullmatch(SANDBOX_ID,response.sandbox_id):
            raise ValueError('Pinned V1 Sandbox ID required; no V2 fallback')
        sandbox = _Sandbox._new_hydrated(response.sandbox_id,self.client,response.metadata)
        if sandbox._is_v2:
            raise ValueError('Pinned V1 Sandbox backend required')
        self.sandboxes[response.sandbox_id] = sandbox
        self.creation_started[response.sandbox_id] = started
        self.emit('sandbox-create-ack',stage=stage,sandbox_id=response.sandbox_id,
                  preexec_deadline=started+15)
        return response.sandbox_id

    async def exec(self, sandbox_id, argv, seconds, *, stdin=b''):
        if sandbox_id not in self.sandboxes or seconds<=0 or len(stdin)>10485760:
            raise ValueError('Owned bounded execution required')
        count = self.exec_counts.get(sandbox_id,0)+1
        if count>2:
            raise ValueError('One upload and one stage exec only')
        self.exec_counts[sandbox_id]=count
        sandbox = self.sandboxes[sandbox_id]
        kind='upload' if stdin else 'worker'
        self.emit('exec-path-start',sandbox_id=sandbox_id,kind=kind,ordinal=count,
                  stdin_bytes_expected=len(stdin))
        async def bounded():
            from modal._utils import task_command_router_client as transport
            from modal._utils.grpc_utils import ModalChannel
            first=count==1
            preexec_deadline=(self.creation_started[sandbox_id]+15 if first else time.time()+15)
            def remaining(cap):
                value=min(cap,preexec_deadline-time.time())
                if value<=0:raise TimeoutError('Cumulative pre-exec15s authority expired')
                return value
            self.emit('task-readiness-start',sandbox_id=sandbox_id)
            readiness_seconds=remaining(5)
            task_id = await asyncio.wait_for(sandbox._get_task_id(raise_if_task_complete=True),readiness_seconds)
            self.emit('task-readiness-ack',sandbox_id=sandbox_id,task_id=task_id)
            router=getattr(sandbox,'_command_router_client',None)
            if router is None:
                self.emit('router-metadata-start',task_id=task_id)
                response=await self.rpc('TaskGetCommandRouterAccess',self.p.TaskGetCommandRouterAccessRequest(
                    task_id=task_id),timeout=remaining(5))
                parsed=urllib.parse.urlsplit(response.url)
                if parsed.scheme!='https' or not parsed.hostname or parsed.username or parsed.password:
                    raise ValueError('Official returned verified HTTPS worker endpoint required')
                self.transient_secrets.append(response.jwt)
                endpoint={'scheme':'https','host':parsed.hostname,'port':parsed.port or 443}
                self.emit('router-metadata-ack',task_id=task_id,endpoint=endpoint)
                self.emit('router-connect-start',endpoint=endpoint)
                original_open=ModalChannel._open_connection
                async def observed_open(channel):
                    try:return await original_open(channel)
                    except BaseException as error:
                        self.emit('router-transport-exception',endpoint=endpoint,
                                  error=error_evidence(error,self.transient_secrets))
                        raise
                # Observe only the official channel's own TLS/proxy path. No endpoint,
                # DNS, proxy, certificate, auth or SDK file is changed.
                ModalChannel._open_connection=observed_open
                try:
                    connection_seconds=remaining(8)
                    router=await asyncio.wait_for(transport.TaskCommandRouterClient._connect(
                        self.client,task_id,response.url,response.jwt),connection_seconds)
                finally:ModalChannel._open_connection=original_open
                sandbox._command_router_client=router
                self.emit('router-connect-ack',endpoint=endpoint)
            starts = 0
            async def one_start(request):
                nonlocal starts
                starts+=1
                if starts!=1:raise ValueError('No exec-start replay')
                # Raw grpclib unary method has no retry wrapper. Use normal router auth/TLS.
                async with router._lease():
                    start_seconds=remaining(5)
                    metadata=router._get_metadata()
                    self.emit('exec-start-attempt',sandbox_id=sandbox_id,kind=kind,exec_id=request.exec_id)
                    result=await router._stub.TaskExecStart(request,
                        metadata=metadata,timeout=start_seconds)
                    self.emit('exec-start-ack',sandbox_id=sandbox_id,kind=kind,exec_id=request.exec_id)
                    return result
            original = router.exec_start
            router.exec_start = one_start
            try:
                process = await sandbox.exec(*argv,timeout=max(1,int(seconds)),text=False,secrets=[])
            finally:
                router.exec_start = original
            async def stream(reader,cap):
                result=bytearray()
                async for chunk in reader:
                    if not isinstance(chunk,bytes) or len(result)+len(chunk)>cap:
                        raise ValueError('Bounded exec output exceeded')
                    result.extend(chunk)
                return bytes(result)
            async def write():
                self.emit('stdin-start',sandbox_id=sandbox_id,kind=kind,bytes_expected=len(stdin))
                written=await process._stdin_write_stream(io.BytesIO(stdin))
                self.emit('stdin-ack',sandbox_id=sandbox_id,kind=kind,bytes_written=written)
                return written
            out,err,code,written = await asyncio.gather(stream(process.stdout,1048576),
                stream(process.stderr,65536),process.wait(),write())
            # Never persist remote stderr text. Class-only failures avoid credential/error echoes.
            if written!=len(stdin):
                raise ValueError('Bounded remote exec failed')
            if code!=0:
                try:failure=json.loads(out)
                except Exception:raise ValueError('Bounded remote exec failed') from None
                if not isinstance(failure,dict) or failure.get('status')!='failed':
                    raise ValueError('Bounded remote exec failed')
            self.emit('exec-readback-ack',sandbox_id=sandbox_id,kind=kind,
                      stdout_bytes=len(out),stderr_bytes=len(err),exit_code=code)
            return out
        try:return await asyncio.wait_for(bounded(),seconds)
        except BaseException as error:
            self.emit('exec-path-failed',sandbox_id=sandbox_id,kind=kind,
                      error=error_evidence(error,self.transient_secrets))
            raise

    async def stop_sandbox(self,sandbox_id):
        if not re.fullmatch(SANDBOX_ID,sandbox_id):raise ValueError('Owned V1 Sandbox ID required')
        await self.rpc('SandboxTerminate',self.p.SandboxTerminateRequest(sandbox_id=sandbox_id))

    async def stop_app(self,app_id):
        if not re.fullmatch(APP_ID,app_id):raise ValueError('Owned App ID required')
        await self.rpc('AppStop',self.p.AppStopRequest(app_id=app_id,source=self.p.APP_STOP_SOURCE_CLI))

    async def audit(self,app_id,*,stopped):
        if not re.fullmatch(APP_ID,app_id):raise ValueError('Exact owned App ID required')
        info = await self.rpc('AppGetInfo',self.p.AppGetInfoRequest(app_id=app_id))
        if info.info.app_id!=app_id or info.info.environment_name!='main':
            raise ValueError('App ownership/environment audit differs')
        lifecycle = await self.rpc('AppGetLifecycle',self.p.AppGetLifecycleRequest(app_id=app_id))
        tasks = await self.rpc('TaskList',self.p.TaskListRequest(app_id=app_id,environment_name='main'))
        task_ids=set()
        for task in tasks.tasks:
            if (task.app_id!=app_id or not re.fullmatch(r'ta-[A-Za-z0-9]+',task.task_id)
                    or task.task_id in task_ids or len(task_ids)>=16):
                raise ValueError('Owned container metadata malformed or foreign')
            task_ids.add(task.task_id)
        sandbox_ids=set(); before=None
        for _ in range(4):
            response = await self.rpc('SandboxList',self.p.SandboxListRequest(app_id=app_id,
                environment_name='main',include_finished=False,before_timestamp=before))
            if not response.sandboxes:break
            for row in response.sandboxes:
                if (row.app_id!=app_id or not re.fullmatch(SANDBOX_ID,row.id)
                        or row.id in sandbox_ids or len(sandbox_ids)>=4):
                    raise ValueError('Owned V1 Sandbox list malformed or foreign')
                sandbox_ids.add(row.id)
            cursor=response.sandboxes[-1].created_at
            if cursor<=0 or (before is not None and cursor>=before):
                raise ValueError('Sandbox listing cursor lacks strict progress')
            before=cursor
        else:raise ValueError('Sandbox listing pagination exceeds bound')
        app_stopped=lifecycle.lifecycle.app_state==self.p.APP_STATE_STOPPED
        return {'confirmed':not task_ids and not sandbox_ids and (app_stopped or not stopped),
                'workspace':IDENTITY['workspace'],'environment':'main','app_id':app_id,
                'app_stopped':app_stopped,'active_containers':len(task_ids),
                'active_sandboxes':len(sandbox_ids),'sandbox_backend':'V1',
                'audited_at':time.time()}


def _SandboxNetwork(p):
    # Same official builder used by SDK Sandbox._new(block_network=True).
    from modal.sandbox import _build_outbound_network_access
    return _build_outbound_network_access(True,None,None)
