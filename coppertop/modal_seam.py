"""Offline preparation and a fixed two-call Modal seam, not an HTTP gateway.

Provider imports and client authentication belong only to an approved worker.
The separate supervisor retains exact-App cleanup authority on worker failure.
"""
import base64
from contextlib import contextmanager
import hashlib
import json
import os
from pathlib import Path
import re
import signal
import subprocess
import sys
import time

from . import modal_seam_probe as body

SDK_VERSION = '1.6.1'
APP_ID = r'ap-[A-Za-z0-9]{22}'
SOURCE_FILES = ('coppertop/__init__.py', 'coppertop/modal_seam_probe.py',
                'coppertop/modal_seam.py', 'coppertop/modal_seam_tls.py',
                'tools/run_modal_seam.py', 'requirements-gpu-controller.txt',
                'requirements-modal-seam.txt')
LEGACY_SOURCE_FILES = tuple(name for name in SOURCE_FILES if name not in
                           ('coppertop/modal_seam_tls.py', 'requirements-modal-seam.txt'))
OPTIONS = dict(cpu=(0.125, 0.125), memory=(256, 256), gpu=None,
               min_containers=0, max_containers=1, buffer_containers=0,
               single_use_containers=False, scaledown_window=180, retries=0,
               timeout=30, startup_timeout=60, secrets=[], volumes={},
               block_network=True, restrict_modal_access=True, include_source=False)
LEGACY_OPTIONS = {**OPTIONS, 'single_use_containers': True, 'scaledown_window': 2}


def canonical(value):
    return (json.dumps(value, sort_keys=True, separators=(',', ':'), allow_nan=False)+'\n').encode()


def sha(value):
    return hashlib.sha256(value).hexdigest()


def sources(root):
    result = {}
    for name in SOURCE_FILES:
        path = Path(root)/name
        if path.is_symlink() or not path.is_file() or path.stat().st_size > 131072:
            raise ValueError('Bounded regular reviewed source required')
        result[name] = sha(path.read_bytes())
    return result


def fixed_plan(suite, workspace, image_id, source_hashes, *, now):
    if not isinstance(suite, str) or not re.fullmatch(r'ct-modal-[a-z0-9-]{1,32}', suite):
        raise ValueError('Fresh bounded suite identity required')
    if workspace is not None and (not isinstance(workspace, str) or not re.fullmatch(r'[a-z0-9][a-z0-9_-]{0,63}', workspace)):
        raise ValueError('Exact existing workspace name required')
    if image_id is not None and (not isinstance(image_id, str) or not re.fullmatch(r'im-[A-Za-z0-9]+', image_id)):
        raise ValueError('Existing immutable Modal image ID required')
    if type(now) is not int:
        raise ValueError('Integer plan clock required')
    return {'schemaVersion': 1, 'suiteId': suite, 'provider': 'modal',
            'workspace': workspace, 'environment': 'main', 'imageId': image_id,
            'sdkVersion': SDK_VERSION, 'createdAt': now, 'expiresAt': now+1800,
            'sourceSha256': source_hashes, 'functionOptions': json.loads(canonical(OPTIONS)),
            'executionSeconds': 180, 'cleanupSeconds': 60,
            'apps': 1, 'functions': 1, 'spawnCalls': 2,
            'payloadSha256': body.PAYLOAD_HASH, 'payloadBytes': 32,
            'operationalAllowanceUsd': '0.25', 'invoiceCapVerified': False,
            'imageBuildAuthorized': False, 'persistentDeploymentAuthorized': False,
            'realOrchestratorIntegration': False}


def prepare(root, suite, workspace=None, image_id=None, *, now=None):
    now = int(time.time()) if now is None else now
    return fixed_plan(suite, workspace, image_id, sources(root), now=now)


def validate_plan(root, plan, *, check_current_sources=True):
    if not isinstance(plan, dict):
        raise ValueError('Exact plan required')
    hashes = plan.get('sourceSha256')
    source_names = (set(SOURCE_FILES),) if check_current_sources else (set(SOURCE_FILES), set(LEGACY_SOURCE_FILES))
    if (not isinstance(hashes, dict) or set(hashes) not in source_names
        or any(not isinstance(value, str) or not re.fullmatch(r'[a-f0-9]{64}', value) for value in hashes.values())):
        raise ValueError('Exact reviewed source manifest required')
    expected = fixed_plan(plan.get('suiteId'), plan.get('workspace'), plan.get('imageId'),
                          sources(root) if check_current_sources else hashes, now=plan.get('createdAt'))
    if not check_current_sources and plan.get('functionOptions') == json.loads(canonical(LEGACY_OPTIONS)):
        expected['functionOptions'] = json.loads(canonical(LEGACY_OPTIONS))
    if canonical(plan) != canonical(expected):
        raise ValueError('Plan differs from current source or fixed scope')


def require_approval(root, plan, approval, *, now=None, recovery=False):
    validate_plan(root, plan, check_current_sources=not recovery)
    now = time.time() if now is None else now
    if plan['workspace'] is None or plan['imageId'] is None:
        raise ValueError('Workspace and existing image prerequisites unresolved')
    expected = {'approved': True, 'planSha256': sha(canonical(plan)),
                'workspace': plan['workspace'], 'environment': 'main',
                'operationalAllowanceUsd': '0.25', 'invoiceCapVerified': False,
                'boundedExposureAccepted': True, 'cleanupAuthorized': True,
                'imageBuildAuthorized': False, 'persistentDeploymentAuthorized': False}
    if (not isinstance(approval, dict) or canonical(approval) != canonical(expected)
        or (not recovery and not plan['createdAt'] <= now < plan['expiresAt'])):
        raise ValueError('Fresh exact plan, workspace, budget/risk and cleanup approval required')


def save(path, value):
    path = Path(path)
    temporary = path.with_name(path.name+'.tmp')
    with temporary.open('wb') as handle:
        handle.write(canonical(value))
        handle.flush()
        os.fsync(handle.fileno())
    os.replace(temporary, path)


@contextmanager
def deadline(seconds):
    if seconds <= 0:
        raise TimeoutError('Phase authority expired')
    started = time.monotonic()
    old_handler, old_timer = signal.getsignal(signal.SIGALRM), signal.getitimer(signal.ITIMER_REAL)
    def expired(_signal, _frame):
        raise TimeoutError('Bounded phase deadline exceeded')
    signal.signal(signal.SIGALRM, expired)
    signal.setitimer(signal.ITIMER_REAL, min(seconds, old_timer[0]) if old_timer[0] else seconds)
    try:
        yield
    finally:
        signal.setitimer(signal.ITIMER_REAL, 0)
        signal.signal(signal.SIGALRM, old_handler)
        if old_timer[0]:
            remaining = old_timer[0]-(time.monotonic()-started)
            if remaining > 0:
                signal.setitimer(signal.ITIMER_REAL, remaining, old_timer[1])


def diagnostic(error, stage):
    known = {'ValueError', 'TimeoutError', 'PermissionError', 'OSError',
             'CalledProcessError', 'TimeoutExpired', 'Error', 'AuthError',
             'ExecutionError', 'FunctionTimeoutError', 'RemoteError'}
    name = type(error).__name__
    return {'stage': stage, 'errorClass': name if name in known else 'Error',
            'message': 'Bounded Modal seam operation failed'}


def cli_json(argv, runner=subprocess.run, *, timeout=10):
    response = runner(argv, capture_output=True, timeout=timeout, check=False)
    if response.returncode != 0 or not isinstance(response.stdout, bytes) or len(response.stdout) > 65536:
        raise ValueError('Bounded metadata command failed')
    value = json.loads(response.stdout)
    if not isinstance(value, list) or len(value) > 64:
        raise ValueError('Bounded exact-App metadata list required')
    return value


def sdk_command(script, *args):
    # Runtime SSL state cannot cross a process boundary. Every independent
    # observer/stop application bootstraps before its own first SDK import.
    bootstrap = ('import sys; sys.path.insert(0,sys.argv.pop(1)); '
                 'from coppertop.modal_seam_tls import bootstrap; bootstrap(); ')
    return [sys.executable, '-c', bootstrap+script, str(Path(__file__).resolve().parents[1]), *args]


def modal_cli(*args):
    return sdk_command("import runpy; runpy.run_module('modal',run_name='__main__',alter_sys=True)", *args)


def active_containers(app_id, runner=subprocess.run):
    if not isinstance(app_id, str) or not re.fullmatch(APP_ID, app_id):
        raise ValueError('Exact owned App ID required')
    rows = cli_json(modal_cli('container', 'list', '--app-id', app_id, '--env', 'main', '--json'), runner)
    found = set()
    for row in rows:
        if (not isinstance(row, dict) or set(row) != {'container_id', 'app_id', 'app_name', 'start_time'}
            or row['app_id'] != app_id or not isinstance(row['container_id'], str)
            or not re.fullmatch(r'ta-[A-Za-z0-9]+', row['container_id'])
            or row['container_id'] in found):
            raise ValueError('Container metadata malformed or outside owned App')
        found.add(row['container_id'])
    return found


def contain(app_id, runner=subprocess.run):
    # One outer deadline includes stop and both independent absence queries.
    with deadline(60):
        if not isinstance(app_id, str) or not re.fullmatch(APP_ID, app_id):
            raise ValueError('Exact owned App ID required')
        stopped = runner(modal_cli('app', 'stop', app_id, '--env', 'main', '--yes'), capture_output=True, timeout=10, check=False)
        if not isinstance(stopped.stdout, bytes) or not isinstance(stopped.stderr, bytes) or len(stopped.stdout)+len(stopped.stderr) > 65536:
            raise ValueError('Stop reply exceeds metadata bound')
        acknowledged = stopped.returncode == 0 or any(
            stream.strip().startswith(b'App is already stopped.') for stream in (stopped.stdout, stopped.stderr))
        for _ in range(30):
            containers = active_containers(app_id, runner)
            script = 'import modal,json,sys; print(json.dumps([s.object_id for s in modal.Sandbox.list(app_id=sys.argv[1])]))'
            sandboxes = cli_json(sdk_command(script, app_id), runner)
            if not acknowledged:
                raise ValueError('Owned App stop not acknowledged')
            if not containers and sandboxes == []:
                return {'confirmed': True, 'appId': app_id, 'activeContainers': 0, 'activeSandboxes': 0}
            time.sleep(1)
        raise TimeoutError('Owned App absence unconfirmed')


def create_probe(sdk, image, root, suite):
    if sdk.__version__ != SDK_VERSION:
        raise ValueError('Pinned Modal SDK required')
    # Mount exactly two reviewed files at startup. No build/copy layer, package
    # auto-discovery, dotenv, credentials or project tree enters compute.
    for name in ('coppertop/__init__.py', 'coppertop/modal_seam_probe.py'):
        image = image.add_local_file(Path(root)/name, '/root/'+name, copy=False)
    app = sdk.App(suite, secrets=[], volumes={}, include_source=False)
    function = app.function(name='coppertop-modal-cpu-probe', image=image, **OPTIONS)(body.probe)
    return app, function


def verify_image(image_id):
    # SDK 1.6.1 Image.from_id is lazy and cannot hydrate on demand. Use only
    # its read-only lookup RPC here, never ImageGetOrCreate or a build path.
    from modal.client import _Client
    from modal._utils.async_utils import synchronizer
    from modal_proto import api_pb2
    async def lookup():
        client = await _Client.from_env()
        return await client._stub.ImageFromId(api_pb2.ImageFromIdRequest(image_id=image_id), retry=None, timeout=10)
    with deadline(15):
        response = synchronizer.create_blocking(lookup)()
    if (response.image_id != image_id
        or not re.match(r'^Python 3\.12\.[0-9]+(?:\s|$)', response.metadata.python_version_info)):
        raise ValueError('Existing image identity or Python 3.12 metadata differs')
    return {'imageId': image_id, 'pythonVersion': '3.12', 'buildRequested': False}


def request(plan, mode, deadline_ms):
    run = plan['suiteId']
    return body.validate_request({'schemaVersion': 1, 'runId': run, 'generation': run+'-e1',
        'bodyId': run+'-'+mode, 'mode': mode, 'payloadBase64': base64.b64encode(body.PAYLOAD).decode(),
        'payloadSha256': body.PAYLOAD_HASH, 'sourceSha256': plan['sourceSha256']['coppertop/modal_seam_probe.py'],
        'deadlineMs': deadline_ms})


def receipt(request, result, output):
    fields = {'schemaVersion', 'runId', 'generation', 'bodyId', 'sourceSha256',
              'containerId', 'payloadBase64', 'digest', 'bytes'}
    if (not isinstance(result, dict) or set(result) != fields
        or type(result['schemaVersion']) is not int or result['schemaVersion'] != 1
        or any(result[k] != request[k] for k in ('runId', 'generation', 'bodyId', 'sourceSha256', 'payloadBase64'))
        or type(result['bytes']) is not int or result['bytes'] != 32
        or result['digest'] != body.PAYLOAD_HASH
        or not isinstance(result['containerId'], str) or not re.fullmatch(r'ta-[A-Za-z0-9]+', result['containerId'])):
        raise ValueError('Exact bounded completion receipt required')
    raw = base64.b64decode(result['payloadBase64'], validate=True)
    if raw != body.PAYLOAD or sha(raw) != result['digest']:
        raise ValueError('Raw receipt bytes differ')
    path = Path(output)/('artifact-'+result['digest']+'.bin')
    with path.open('xb') as handle:
        handle.write(raw)
        handle.flush()
        os.fsync(handle.fileno())
    if path.read_bytes() != raw:
        raise ValueError('Local raw receipt read-back differs')
    return {**result, 'localArtifact': path.name, 'storage': 'local-only',
            'cost': None, 'billing': 'unverified'}


def start_marker(call, request):
    buffer = ''
    with deadline(15):
        for index, entry in enumerate(call.logs.stream(timeout=15)):
            if entry.source != 'stdout':
                continue
            if index >= 64 or not isinstance(entry.message, str) or len(buffer)+len(entry.message) > 4096:
                raise ValueError('Start observation exceeds metadata bound')
            buffer += entry.message
            while '\n' in buffer:
                line, buffer = buffer.split('\n', 1)
                if not line.startswith(body.START_PREFIX):
                    continue
                marker = json.loads(line[len(body.START_PREFIX):])
                expected = {key: request[key] for key in ('runId', 'generation', 'bodyId', 'sourceSha256')}
                expected.update(schemaVersion=1, event='started', payloadSha256=body.PAYLOAD_HASH)
                if (not isinstance(marker, dict) or set(marker) != set(expected)|{'containerId'}
                    or type(marker['schemaVersion']) is not int
                    or any(marker[k] != v for k, v in expected.items())
                    or not isinstance(marker['containerId'], str) or not re.fullmatch(r'ta-[A-Za-z0-9]+', marker['containerId'])):
                    raise ValueError('Start marker identity differs')
                return marker
    raise ValueError('Running probe start not observed')


def execute_calls(function, plan, app_id, output, *, runner=subprocess.run,
                  monotonic=time.monotonic, wall=time.time, sleep=time.sleep, execution_deadline=None):
    result = {'completion': None, 'cancellation': None, 'callIds': [], 'stage': 'complete-spawn'}
    def record():
        save(Path(output)/'observations.json', result)
    absolute = min(plan['expiresAt'], wall()+180) if execution_deadline is None else execution_deadline
    for mode in ('complete', 'hold'):
        if mode == 'hold' and absolute-wall() <= 20:
            raise ValueError('Insufficient authority before hold spawn')
        req = request(plan, mode, int(absolute*1000))
        result['stage'] = mode+'-spawn'
        record()  # Save attempted scope before the one spawn attempt.
        started = monotonic()
        call = function.spawn(req)
        if not isinstance(call.object_id, str) or not re.fullmatch(r'fc-[A-Za-z0-9]+', call.object_id) or call.object_id in result['callIds']:
            raise ValueError('Unique provider call identity required')
        result['callIds'].append(call.object_id)
        record()
        if mode == 'complete':
            result['stage'] = 'complete-receipt'
            with deadline(min(90, absolute-wall())):
                raw = call.get(timeout=min(90, absolute-wall()))
            result['completion'] = receipt(req, raw, output)
        else:
            result['stage'] = 'hold-start'
            marker = start_marker(call, req)
            container = marker['containerId']
            if container != result['completion']['containerId']:
                raise ValueError('Both calls must reuse the same owned container')
            if container not in active_containers(app_id, runner):
                raise ValueError('Running container not observed in owned App')
            # The hold cannot finish normally before 20 seconds from spawn's
            # start. Require observed containment inside that interval; a slow
            # cold start fails conservatively rather than counting natural exit.
            result['stage'] = 'cancel-and-confirm'
            with deadline(min(20-(monotonic()-started), absolute-wall())):
                call.cancel(terminate_containers=True)
                while container in active_containers(app_id, runner):
                    sleep(0.25)
                if monotonic()-started >= 20:
                    raise ValueError('Cancellation absence arrived after natural hold bound')
            result['cancellation'] = {'callId': call.object_id, 'containerId': container,
                'startObserved': True, 'cancelAcknowledged': True, 'containerAbsentBeforeAppStop': True,
                'sameContainerAsCompletion': True, 'withinHoldSeconds': 20, 'nativeTerminalState': 'not-asserted'}
        record()
    result['stage'] = 'calls-verified'
    record()
    return result


def worker(root, plan, approval, output):
    """Private subprocess entry; public supervisor already consumed approval."""
    require_approval(root, plan, approval)
    claim = json.loads((Path(root)/'.local/modal-seam-executions'/ (plan['suiteId']+'.json')).read_bytes())
    if claim != {'planSha256': sha(canonical(plan)), 'output': str(Path(output).resolve())}:
        raise ValueError('Supervisor execution claim differs')
    authority = json.loads((Path(output)/'run-authority.json').read_bytes())
    if (set(authority) != {'planSha256', 'startedAt', 'deadline'}
        or authority['planSha256'] != claim['planSha256']
        or not isinstance(authority['startedAt'], (float, int)) or isinstance(authority['startedAt'], bool)
        or not isinstance(authority['deadline'], (float, int)) or isinstance(authority['deadline'], bool)
        or not plan['createdAt'] <= authority['startedAt'] <= time.time() < authority['deadline']
        or authority['deadline'] != min(authority['startedAt']+180, plan['expiresAt'])):
        raise ValueError('Original bounded supervisor deadline required')
    with (Path(output)/'worker-started.json').open('xb') as started:
        started.write(canonical({'planSha256': claim['planSha256']}))
    ownership = {'planSha256': sha(canonical(plan)), 'suiteId': plan['suiteId'],
                 'workspace': plan['workspace'], 'environment': 'main',
                 'cleanupAuthorized': True, 'creationAttempted': False, 'appId': None}
    stage = 'preflight'
    app = None
    try:
        if not all(name in os.environ for name in ('MODAL_TOKEN_ID', 'MODAL_TOKEN_SECRET')):
            raise ValueError('Injected Modal API credential pair absent')
        from .modal_seam_tls import bootstrap
        bootstrap()
        import modal as sdk
        if sdk.__version__ != SDK_VERSION:
            raise ValueError('Pinned Modal SDK required')
        with deadline(authority['deadline']-time.time()):
            workspace = sdk.Workspace.from_context()
            workspace.hydrate()
            if workspace.name != plan['workspace']:
                raise ValueError('Authenticated workspace differs from approval')
            environment = sdk.Environment.from_name('main', create_if_missing=False)
            environment.hydrate()
            apps = environment.apps.list()
            if len(apps) > 64 or any(row.name == plan['suiteId'] or row.description == plan['suiteId'] for row in apps):
                raise ValueError('Dedicated App name conflict or unsupported listing')
            verified_image = verify_image(plan['imageId'])
            save(Path(output)/'preflight.json', {'workspace': workspace.name, 'environment': 'main', 'image': verified_image})
            image = sdk.Image.from_id(plan['imageId'])
            app, function = create_probe(sdk, image, root, plan['suiteId'])
            ownership['creationAttempted'] = True
            save(Path(output)/'ownership.json', ownership)
            stage = 'app-create'
            with app.run(detach=False, environment_name='main'):
                if not isinstance(app.app_id, str) or not re.fullmatch(APP_ID, app.app_id):
                    raise ValueError('Owned App identity missing')
                ownership['appId'] = app.app_id
                save(Path(output)/'ownership.json', ownership)
                stage = 'two-call-probe'
                execute_calls(function, plan, app.app_id, output, execution_deadline=authority['deadline'])
        save(Path(output)/'worker-result.json', {'passed': True})
        return 0
    except BaseException as error:
        # An App ID can become available while entering the SDK context fails.
        if app is not None and isinstance(app.app_id, str) and re.fullmatch(APP_ID, app.app_id):
            ownership['appId'] = app.app_id
        save(Path(output)/'ownership.json', ownership)
        save(Path(output)/'worker-result.json', {'passed': False, 'failure': diagnostic(error, stage)})
        return 1


def claim_execution(root, plan, output):
    registry = Path(root)/'.local/modal-seam-executions'
    registry.mkdir(parents=True, exist_ok=True)
    with (registry/(plan['suiteId']+'.json')).open('xb') as handle:
        handle.write(canonical({'planSha256': sha(canonical(plan)), 'output': str(Path(output).resolve())}))
        handle.flush()
        os.fsync(handle.fileno())


def cleanup_receipt(root, plan, output, *, cleanup=contain):
    # Cleanup uses the original plan/approval/claim, even after expiry or a
    # source repair. Current source integrity is a launch-only condition.
    if canonical(json.loads((Path(output)/'plan.json').read_bytes())) != canonical(plan):
        raise ValueError('Original saved cleanup plan required')
    approval = json.loads((Path(output)/'approval.json').read_bytes())
    require_approval(root, plan, approval, recovery=True)
    claim = json.loads((Path(root)/'.local/modal-seam-executions'/(plan['suiteId']+'.json')).read_bytes())
    if claim != {'planSha256': sha(canonical(plan)), 'output': str(Path(output).resolve())}:
        raise ValueError('Original supervisor cleanup claim required')
    ownership = json.loads((Path(output)/'ownership.json').read_bytes())
    expected = {'planSha256': sha(canonical(plan)), 'suiteId': plan['suiteId'],
                'workspace': plan['workspace'], 'environment': 'main', 'cleanupAuthorized': True}
    if (not isinstance(ownership, dict) or set(ownership) != set(expected)|{'creationAttempted', 'appId'}
        or any(ownership[k] != v for k, v in expected.items())
        or type(ownership['creationAttempted']) is not bool):
        raise ValueError('Original exact-scope cleanup authority required')
    result = None
    try:
        if not ownership['creationAttempted']:
            if ownership['appId'] is not None:
                raise ValueError('Unattempted allocation has an unexpected App ID')
            result = {'confirmed': True, 'allocationNotAttempted': True}
        else:
            identity = ownership['appId']
            if not isinstance(identity, str) or not re.fullmatch(APP_ID, identity):
                raise ValueError('Allocation attempted with unknown ownership; no name adoption')
            result = cleanup(identity)
            if canonical(result) != canonical({'confirmed': True, 'appId': identity, 'activeContainers': 0, 'activeSandboxes': 0}):
                raise ValueError('Exact owned App absence unconfirmed')
    except BaseException as error:
        result = {'confirmed': False, 'failure': diagnostic(error, 'independent-cleanup')}
        raise
    finally:
        # Keep failures as well as successes when an operator retries only containment.
        with (Path(output)/'cleanup-history.jsonl').open('ab') as handle:
            handle.write(canonical({'at': int(time.time()), 'scope': expected, 'cleanup': result}))
            handle.flush()
            os.fsync(handle.fileno())
    return result


def supervise(root, plan, approval, output, *, process_factory=subprocess.Popen,
              cleanup=contain, terminate=None):
    require_approval(root, plan, approval)  # Before claim, process, SDK or metadata access.
    output = Path(output).resolve()
    claim_execution(root, plan, output)
    output.mkdir(parents=True, exist_ok=False)
    save(output/'plan.json', plan)
    save(output/'approval.json', approval)
    started = time.time()
    authority = {'planSha256': sha(canonical(plan)), 'startedAt': started,
                 'deadline': min(started+180, plan['expiresAt'])}
    save(output/'run-authority.json', authority)
    save(output/'ownership.json', {'planSha256': sha(canonical(plan)), 'suiteId': plan['suiteId'],
         'workspace': plan['workspace'], 'environment': 'main', 'cleanupAuthorized': True,
         'creationAttempted': False, 'appId': None})
    process = None
    result = {'passed': False, 'workerExit': None, 'cleanup': None, 'failure': None}
    cleanup_called = False
    try:
        process = process_factory([sys.executable, str(Path(root)/'tools/run_modal_seam.py'),
             'internal-worker', '--plan', str(output/'plan.json'), '--approval', str(output/'approval.json'),
             '--output', str(output)], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
             start_new_session=True)
        result['workerExit'] = process.wait(timeout=authority['deadline']-time.time())
        worker_result = json.loads((output/'worker-result.json').read_bytes())
        observations = json.loads((output/'observations.json').read_bytes()) if worker_result.get('passed') is True else None
        if (result['workerExit'] != 0 or worker_result != {'passed': True}
            or observations is None or observations.get('stage') != 'calls-verified'
            or len(observations.get('callIds', [])) != 2
            or observations.get('completion') is None or observations.get('cancellation') is None):
            raise ValueError('Two-call worker did not verify its bounded gates')
        result['passed'] = True
    except BaseException as error:
        result['failure'] = diagnostic(error, 'worker')
    finally:
        try:
            with deadline(60):
                if process is not None and process.poll() is None:
                    if terminate is not None:
                        terminate(process)
                    else:
                        os.killpg(process.pid, signal.SIGTERM)
                        try:
                            process.wait(timeout=2)
                        except subprocess.TimeoutExpired:
                            os.killpg(process.pid, signal.SIGKILL)
                            process.wait(timeout=2)
                cleanup_called = True
                result['cleanup'] = cleanup_receipt(root, plan, output, cleanup=cleanup)
        except BaseException as error:
            result['passed'] = False
            result['cleanup'] = {'confirmed': False, 'failure': diagnostic(error, 'independent-cleanup')}
            if not cleanup_called:
                with (output/'cleanup-history.jsonl').open('ab') as handle:
                    handle.write(canonical({'at': int(time.time()), 'planSha256': sha(canonical(plan)), 'cleanup': result['cleanup']}))
                    handle.flush()
                    os.fsync(handle.fileno())
        save(output/'outcome.json', result)
    return result
