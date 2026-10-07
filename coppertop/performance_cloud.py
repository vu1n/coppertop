"""One approval-bound Modal evaluation; CPU transfers/cache gates precede GPU."""
import argparse
import hashlib
import json
import os
import re
import subprocess
import socket
import sys
import tempfile
import time
from decimal import Decimal
from pathlib import Path
from types import SimpleNamespace

from .chords import canonical, digest, object_path
from .cloud_stage import StageManifest, DeadlineStore, stage_objects, stage_receipt, verify_stage, commit_directory
from .cloud_roundtrip import _metadata
from .gpu_launch import historical_total
from .gpu_smoke import RELEASE
from .image_check import validate as validate_native, require_native_interpreter, require_dependencies, pinned_dependencies
from .modal_reload import CHECKPOINT_RUN_ID, ORIGINAL_CONTAINER_ID, INPUT_MOUNT, _container
from .modal_staging import stop_verifier
from .performance_eval import plan as evaluation_plan, validate_plan, summarize, runtime_image, runtime_sources, CACHED_IMAGE
from .performance_runtime import MODULE, SIMULATOR, GATE_REASONS
from .r2_session import SessionScope, R2StoreFactory, mint
from .reload_launch import (read_json, save, cli_json, hard_deadline, delete_owned_volume,
                            selected_env, parent_credentials, refresh_billing, stop_and_audit_owned_app, CHECKPOINT_PREFIX, RECEIPT)
from .smoke_artifacts import fetch_smoke_checkpoint
from .streams import ObjectRef, read_small

INPUTS_SHA256 = 'efdace4feef64de66d12eca2d4c159d06672115f749109b9e11f9523ebf91578'
LIMITS = {'controller_seconds': 5400, 'app_startup_seconds': 300,
          'source_seconds': 600, 'checkpoint_seconds': 1800, 'cache_gate_seconds': 1200,
          'gpu_seconds': 1800, 'publisher_seconds': 300, 'startup_seconds_each': 300,
          'audit_seconds_each': 60, 'volume_bytes': 3*1024**3, 'report_bytes': 65536,
          'r2_requests': 2000, 'modal_metadata_egress_bytes': 1024**3, 'automatic_retries': 0}


def prepare(inputs, ledger, attempt, root):
    """Pure local binding: no environment, provider lookup, signing or resources."""
    if not re.fullmatch(r'reload-[0-9a-f]{16}', attempt):
        raise ValueError('One exact bounded attempt identity required')
    bundle = {name: inputs[name] for name in ('exact_input_objects', 'checkpoint_artifacts')}
    if digest(canonical(bundle)) != INPUTS_SHA256:
        raise ValueError('Exact previously verified source/checkpoint object bundle required')
    if (Decimal(str(ledger.get('cumulative_cap', '0'))) != 48
        or historical_total(ledger)+10 > 48):
        raise ValueError('Retained holds plus full $10 must fit proposed $48 cap')
    source_refs = {item['path'].removeprefix('chords/'): ObjectRef(item['sha256'], item['size'])
                   for item in bundle['exact_input_objects'] if item['path'].startswith('chords/objects/')}
    checkpoint_refs = {name: ObjectRef(value['sha256'], value['size'])
                       for name, value in bundle['checkpoint_artifacts'].items()}
    source = StageManifest(CHECKPOINT_RUN_ID, RELEASE, source_refs)
    checkpoint = StageManifest(CHECKPOINT_RUN_ID, RELEASE, checkpoint_refs)
    source.validate(); checkpoint.validate()
    scopes = (SessionScope('source', 'coppertop', 'chords', tuple(item['path'] for item in bundle['exact_input_objects'] if item['path'].startswith('chords/objects/'))),
        SessionScope('checkpoint', 'coppertop', CHECKPOINT_PREFIX, tuple(item['path'] for item in bundle['exact_input_objects'] if item['path'].startswith(CHECKPOINT_PREFIX+'/'))),
        SessionScope('report', 'coppertop', 'chords/gpu-reload/'+attempt))
    root = Path(root)
    local_files = ('coppertop/performance_eval.py', 'coppertop/performance_runtime.py',
                   'coppertop/performance_cloud.py', 'coppertop/reload_launch.py', 'tools/run_performance_cloud.py')
    code = {name: hashlib.sha256((root/name).read_bytes()).hexdigest() for name in local_files}
    p = {'schema_version': 1, 'kind': 'approved_paired_performance_cloud', 'provider': 'Modal',
         'region': 'us', 'environment': 'main', 'attempt_id': attempt,
         'app_name': 'coppertop-performance-'+attempt,
         'volume_name': 'coppertop-'+attempt+'-inputs', 'cached_image': CACHED_IMAGE,
         'evaluation': evaluation_plan(), 'input_bundle_sha256': INPUTS_SHA256,
         'input_bundle': bundle,
         'source_manifest': source.to_dict(), 'checkpoint_manifest': checkpoint.to_dict(),
         'checkpoint_receipt': RECEIPT, 'training_container_id': ORIGINAL_CONTAINER_ID,
         'limits': dict(LIMITS), 'additional_cap_usd': '10', 'cumulative_cap_usd': '48',
         'prior_conservative_holds_usd': str(historical_total(ledger)), 'holds_released_usd': '0',
         'ledger_sha256': digest(canonical(ledger)), 'operator_sources_sha256': code,
         'auth': {'ttl_seconds': 7200, 'session_count': 3, 'bucket': 'coppertop',
                  'scopes': [{'role': s.role, 'actions': list(s.actions), **s.paths} for s in scopes]},
         'retention': 'Delete only new owned Volume after exact-App zero-active audit; preserve original R2 checkpoint and immutable report',
         'new_downloads': False, 'new_image_builds': False, 'paid_authorized': False}
    return {'cloud_plan': p, 'cloud_plan_sha256': digest(canonical(p)),
            'source': source, 'checkpoint': checkpoint, 'scopes': scopes}


def validate_report(record, p):
    _metadata(record)
    if set(record) != {'container_id', 'result', 'report_sha256'}:
        raise ValueError('Exact evaluation metadata envelope required')
    identity = _container(record['container_id'])
    result = record['result']
    expected = {'evaluation_plan_sha256': validate_plan(p['evaluation']),
        'attempt_id': p['attempt_id'], 'container_id': identity,
        'training_container_id': ORIGINAL_CONTAINER_ID, 'checkpoint_receipt': RECEIPT}
    if identity == ORIGINAL_CONTAINER_ID or any(result.get(k) != v for k, v in expected.items()):
        raise ValueError('Fresh evaluation identity/provenance differs')
    if result.get('provenance') != p['evaluation']:
        raise ValueError('Evaluation recipe provenance differs')
    if result.get('status') == 'completed':
        derived = summarize(p['evaluation'], result['records'])
        if any(result.get(k) != v for k, v in derived.items()):
            raise ValueError('Evaluation metrics are not derived from the matched completed pairs')
    elif (result.get('status') != 'error' or result.get('result_comparison_valid') is not False
          or result.get('automatic_retries') != 0):
        raise ValueError('Evaluation error must remain an invalid comparison')
    raw = canonical(result)
    if len(raw) > LIMITS['report_bytes'] or digest(raw) != record['report_sha256']:
        raise ValueError('Evaluation report content reference differs')
    return raw


def loopback_gate():
    """Prove the blocked-network job can use its stock local transport."""
    with socket.socket() as listener, socket.socket() as client:
        listener.settimeout(2); client.settimeout(2)
        listener.bind(('127.0.0.1', 0)); listener.listen(1)
        client.connect(listener.getsockname())
        connection, _ = listener.accept()
        with connection:
            connection.settimeout(2); client.sendall(b'probe')
            if connection.recv(5) != b'probe': raise ValueError('Local model transport probe failed')
    return True


def simulator_preflight(runner=subprocess.run):
    """Persist the structured child receipt, including failure, without raw stderr."""
    with tempfile.TemporaryFile() as stdout, tempfile.TemporaryFile() as stderr:
        try:
            process = runner([SIMULATOR, '-m', MODULE, '--simulator-gate'], stdout=stdout, stderr=stderr, timeout=600,
                env={'PATH': '/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin',
                     'HOME': '/root', 'PYTHONPATH': '/app', 'HF_HUB_OFFLINE': '1', 'TRANSFORMERS_OFFLINE': '1'})
        except (OSError, subprocess.TimeoutExpired) as error:
            return {'status': 'failed', 'render_reset_executed': False, 'diagnostic': {
                'check': 'process_bootstrap', 'error_type': type(error).__name__, 'reason': 'exception_text_omitted'}}
        stdout_size = stdout.tell(); stderr_size = stderr.tell()
        stdout.seek(max(0, stdout_size-65536)); lines = stdout.read(65536).splitlines()
    fallback = {'status': 'failed', 'render_reset_executed': False,
        'diagnostic': {'check': 'process_bootstrap', 'error_type': 'SimulatorProcessFailure',
            'reason': 'structured_receipt_unavailable', 'exit_code': process.returncode,
            'stdout_bytes': stdout_size, 'stderr_bytes': stderr_size}}
    try:
        receipt = json.loads(lines[-1])
        _metadata(receipt)
        if (stdout_size > 65536 or receipt.get('render_reset_executed') is not False or
            (process.returncode == 0 and receipt.get('status') != 'passed') or
            (process.returncode != 0 and receipt.get('status') != 'failed')):
            return fallback
        if receipt['status'] == 'failed':
            if set(receipt) != {'status', 'render_reset_executed', 'diagnostic'}: return fallback
            diagnostic = receipt['diagnostic']
            if (not isinstance(diagnostic, dict) or not {'check', 'error_type', 'reason', 'trace'} <= set(diagnostic)
                or set(diagnostic)-{'check', 'error_type', 'reason', 'trace', 'missing_module', 'exit_code', 'errno', 'dependency_differences'}
                or diagnostic['reason'] not in GATE_REASONS | {'exception_text_omitted'}): return fallback
            for key in ('check', 'error_type', 'missing_module'):
                if key in diagnostic and (not isinstance(diagnostic[key], str)
                    or not re.fullmatch(r'[A-Za-z0-9_.]{1,128}', diagnostic[key])): return fallback
            for key in ('exit_code', 'errno'):
                if key in diagnostic and type(diagnostic[key]) is not int: return fallback
            if 'dependency_differences' in diagnostic:
                differences = diagnostic['dependency_differences']
                if not isinstance(differences, list) or not 1 <= len(differences) <= 256: return fallback
                for item in differences:
                    if (not isinstance(item, dict) or set(item) != {'package', 'expected', 'observed', 'status'}
                        or not isinstance(item['package'], str) or not re.fullmatch(r'[a-z0-9][a-z0-9-]{0,127}', item['package'])
                        or not isinstance(item['expected'], str) or not re.fullmatch(r'[0-9][A-Za-z0-9.+-]{0,127}', item['expected'])
                        or (item['observed'] is not None and item['observed'] != 'omitted' and
                            (not isinstance(item['observed'], str) or not re.fullmatch(r'[0-9][A-Za-z0-9.+-]{0,127}', item['observed'])))
                        or item['status'] not in ('unknown', 'missing', 'different')): return fallback
            if not isinstance(diagnostic['trace'], list) or len(diagnostic['trace']) > 16: return fallback
            for frame in diagnostic['trace']:
                if (not isinstance(frame, dict) or set(frame) != {'file', 'line', 'function'}
                    or type(frame['line']) is not int or not 1 <= frame['line'] <= 1000000
                    or any(not isinstance(frame[key], str) or not re.fullmatch(r'[A-Za-z0-9_.<>-]{1,128}', frame[key])
                           for key in ('file', 'function'))): return fallback
        elif (set(receipt) != {'status', 'task', 'horizon', 'render_reset_executed', 'assets'}
              or receipt['task'] != evaluation_plan()['task'] or receipt['horizon'] != evaluation_plan()['horizon']
              or receipt['assets'] != {'files': 123236, 'extracted_bytes': 24063148172,
                  'archive_bytes': 10986648838,
                  'verification': 'immutable image provenance plus completed CRC extraction receipt and every cached file length'}):
            return fallback
        return receipt
    except (ValueError, IndexError, AttributeError, TypeError, KeyError):
        return fallback


def create_functions(sdk, prepared, *, image, volume, store_factory, sessions, runner=subprocess.run):
    """Define five single-use phases. This function performs no allocation."""
    if sdk.__version__ != '1.6.1' or set(sessions) != {'source', 'checkpoint', 'report'}:
        raise ValueError('Reviewed SDK and three scoped CPU session handles required')
    p = prepared['cloud_plan']; source = prepared['source']; checkpoint = prepared['checkpoint']
    expected_sources = dict(p['evaluation']['runtime_sources_sha256'])
    mount = volume.with_mount_options(sub_path='/'+p['attempt_id'])
    read_only = mount.with_mount_options(read_only=True)
    app = sdk.App('coppertop-performance', secrets=[], volumes={}, include_source=False)
    common = dict(image=image, region='us', serialized=True, retries=0,
        single_use_containers=True, min_containers=0, buffer_containers=0,
        max_containers=1, scaledown_window=2, startup_timeout=300)
    cpu = dict(common, cpu=(2, 2), memory=(4096, 4096), gpu=None)

    @app.function(**dict(cpu, name='performance-source-stage', timeout=600,
        secrets=list(sessions['source']), volumes={INPUT_MOUNT: mount}))
    def stage_source():
        receipt = stage_objects(DeadlineStore(store_factory('source'), 600), source,
            Path(INPUT_MOUNT)/source.key, max_seconds=600, commit=volume.commit)
        return {'container_id': _container(os.environ.get('MODAL_TASK_ID')), 'receipt': receipt}

    @app.function(**dict(cpu, name='performance-checkpoint-stage', timeout=1800,
        secrets=list(sessions['checkpoint']), volumes={INPUT_MOUNT: mount}))
    def stage_checkpoint():
        directory = Path(INPUT_MOUNT)/checkpoint.key
        partial = Path(INPUT_MOUNT)/(checkpoint.key+'-fetch')
        receipt = fetch_smoke_checkpoint(DeadlineStore(store_factory('checkpoint'), 1800), RECEIPT, partial, CHECKPOINT_RUN_ID)
        if receipt['artifacts'] != {name: ref.to_dict() for name, ref in checkpoint.objects.items()}:
            raise ValueError('Pinned checkpoint manifest differs')
        if directory.exists():
            raise ValueError('Checkpoint stage already exists')
        partial.rename(directory)
        committed = commit_directory(checkpoint, directory, volume.commit)
        return {'container_id': _container(os.environ.get('MODAL_TASK_ID')), 'receipt': committed}

    @app.function(**dict(common, name='performance-cache-gate', timeout=1200,
        cpu=(4, 4), memory=(16384, 16384), gpu=None, secrets=[], block_network=True,
        volumes={INPUT_MOUNT: read_only}))
    def cache_gate():
        if runtime_sources() != expected_sources:
            raise ValueError('Mounted evaluation source differs')
        require_native_interpreter()
        loopback_gate()
        verify_stage(source, Path(INPUT_MOUNT)/source.key)
        verify_stage(checkpoint, Path(INPUT_MOUNT)/checkpoint.key)
        root = Path('/app')
        require_dependencies(pinned_dependencies(root/'requirements-gpu-smoke.lock', root/'requirements-r2.txt'))
        native = validate_native(SimpleNamespace(root=root, store=Path(INPUT_MOUNT)/source.key,
            model=Path('/opt/model'), pins=Path('/opt/gpu-smoke-pins.json'),
            evaluator=Path('/opt/evaluator.py'), image_id=CACHED_IMAGE))
        if (native.get('status') != 'passed' or native.get('full_model_instantiated') is not False
            or native.get('cuda_executed') is not False
            or any(native.get(k) is not True for k in ('snapshot_verified', 'imports_verified', 'processor_parity_verified', 'entrypoint_verified'))):
            raise ValueError('Native CPU cache gate failed')
        simulation = simulator_preflight(runner)
        return {'container_id': _container(os.environ.get('MODAL_TASK_ID')), 'status': simulation['status'],
                'image_id': CACHED_IMAGE, 'native': native, 'simulator': simulation,
                'evaluation_plan_sha256': validate_plan(p['evaluation'])}

    @app.function(**dict(common, name='paired-performance-evaluator', timeout=1800,
        gpu='L40S', cpu=(4, 4), memory=(65536, 65536), secrets=[], block_network=True,
        volumes={INPUT_MOUNT: read_only}))
    def evaluate():
        identity = _container(os.environ.get('MODAL_TASK_ID'))
        if identity == ORIGINAL_CONTAINER_ID or runtime_sources() != expected_sources:
            raise ValueError('Fresh verified evaluation container required')
        verify_stage(checkpoint, Path(INPUT_MOUNT)/checkpoint.key)
        with tempfile.TemporaryDirectory(prefix='coppertop-performance-') as scratch:
            plan_path = Path(scratch)/'plan.json'; save(plan_path, p['evaluation'])
            target = Path(scratch)/'episodes'
            command = ['/opt/conda/bin/python', '-m', MODULE, '--execute-cuda', '--plan', str(plan_path),
                '--approved-plan-sha256', validate_plan(p['evaluation']),
                '--head', str(Path(INPUT_MOUNT)/checkpoint.key/'head.safetensors'), '--output', str(target)]
            process = runner(command, timeout=1800)
            if process.returncode == 0:
                result = read_json(target/'result.json', LIMITS['report_bytes'])
            else:
                failure = read_json(target/'failure.json', LIMITS['report_bytes']) if (target/'failure.json').is_file() else {'error_type': 'NativeProcessFailure'}
                result = {'kind': p['evaluation']['kind'], 'status': 'error',
                    'result_comparison_valid': False, 'automatic_retries': 0,
                    'error_type': failure.get('error_type', 'NativeProcessFailure'), 'provenance': p['evaluation']}
                if (target/'progress.json').is_file():
                    result['partial_records'] = read_json(target/'progress.json')['records']
        result.update(evaluation_plan_sha256=validate_plan(p['evaluation']), attempt_id=p['attempt_id'],
            container_id=identity, training_container_id=ORIGINAL_CONTAINER_ID, checkpoint_receipt=RECEIPT)
        record = {'container_id': identity, 'result': result, 'report_sha256': digest(canonical(result))}
        validate_report(record, p)
        return record

    @app.function(**dict(cpu, name='performance-report-publisher', timeout=300,
        secrets=list(sessions['report']), volumes={}))
    def publish(record):
        raw = validate_report(record, p)
        store = DeadlineStore(store_factory('report'), 300)
        key = store.put(raw)
        if key != digest(raw) or read_small(store, key, LIMITS['report_bytes']) != raw:
            raise ValueError('Immutable evaluation report read-back differs')
        return {'container_id': _container(os.environ.get('MODAL_TASK_ID')), 'report_key': key,
                'verifier_container_id': record['container_id'], 'attempt_id': p['attempt_id']}

    return SimpleNamespace(app=app, source=stage_source, checkpoint=stage_checkpoint,
                           gate=cache_gate, evaluate=evaluate, publish=publish)


def execute_phases(functions, prepared, output, *, admission, invoke, stopped, cleanup):
    """An exclusive controller marker makes the consumed authorization unreplayable."""
    p = prepared['cloud_plan']; output = Path(output)
    with (output/'controller-started.json').open('x') as marker:
        marker.write(json.dumps({'cloud_plan_sha256': prepared['cloud_plan_sha256']}))
    state = {'status': 'started', 'phases': {}, 'checkpoint_preserved': True}
    error = None
    def phase(name, function, bound, args=()):
        state['phase'] = name; save(output/'outcome.json', state)
        response = invoke(function, args, bound)
        identity = _container(response.get('container_id') if isinstance(response, dict) else None)
        state['phases'][name] = response; _metadata(response); save(output/'outcome.json', state)
        if stopped(identity) is not True:
            raise RuntimeError('Owned phase container stop unconfirmed')
        return response
    try:
        save(output/'outcome.json', state); admission()
        for name, manifest, function, bound in (('source', prepared['source'], functions.source, 900),
                ('checkpoint', prepared['checkpoint'], functions.checkpoint, 2100)):
            response = phase(name, function, bound)
            if response.get('receipt') != stage_receipt(manifest):
                raise ValueError('Mounted committed stage receipt differs')
        gate = phase('cache_gate', functions.gate, 1500)
        if (gate.get('status') != 'passed' or gate.get('image_id') != CACHED_IMAGE
            or gate.get('evaluation_plan_sha256') != validate_plan(p['evaluation'])):
            raise ValueError('CPU cache gate did not pass; GPU forbidden')
        evaluated = phase('evaluate', functions.evaluate, 2100)
        raw = validate_report(evaluated, p)
        published = phase('publish', functions.publish, 600, (evaluated,))
        if (published.get('report_key') != digest(raw) or published.get('attempt_id') != p['attempt_id']
            or published.get('verifier_container_id') != evaluated['container_id']):
            raise ValueError('Durable evaluation report receipt differs')
        state['status'] = 'passed' if evaluated['result']['status'] == 'completed' else 'failed'
        state['durable_report_verified'] = True
    except BaseException as caught:
        error = caught; state.update(status='failed', error_type=type(caught).__name__)
    finally:
        try:
            receipt = cleanup(); state['cleanup'] = _metadata(receipt)
            if (any(type(receipt.get(k)) is not int or receipt[k] != 0 for k in ('active_containers', 'active_sandboxes'))
                or receipt.get('app_stopped') is not True or receipt.get('volume_deleted') is not True):
                raise RuntimeError('Owned teardown verification incomplete')
        except BaseException as caught:
            state.update(status='failed', cleanup_error_type=type(caught).__name__)
            if error is None: error = caught
        save(output/'outcome.json', state)
    if error is not None: raise error
    return state


def _live(prepared, ledger, env_file, output, root, *, deadline, sdk=None, runner=subprocess.run):
    if sdk is None: import modal as sdk
    if sdk.__version__ != '1.6.1': raise ValueError('Modal SDK1.6.1 required')
    p = prepared['cloud_plan']; pending = 'pending-'+p['attempt_id']
    for name, expected in p['operator_sources_sha256'].items():
        path = Path(root)/name
        if path.is_symlink() or hashlib.sha256(path.read_bytes()).hexdigest() != expected:
            raise ValueError('Approved source changed before access/allocation')
    with hard_deadline(60):
        apps = cli_json([sys.executable, '-m', 'modal', 'app', 'list', '--env', 'main', '--json'], runner)
        if any(a.get('description') == p['app_name'] for a in apps):
            raise ValueError('Dedicated App name already exists')
        volumes = cli_json([sys.executable, '-m', 'modal', 'volume', 'list', '--env', 'main', '--json'], runner)
        if any(v.get('name') == p['volume_name'] for v in volumes):
            raise ValueError('Dedicated Volume name already exists')
    values = selected_env(env_file); access, secret = parent_credentials(values)
    endpoint = values['CLOUDFLARE_R2_URL']
    sessions = {scope.role: [sdk.Secret.from_dict(mint(access, secret, endpoint=endpoint, scope=scope).environment())]
                for scope in prepared['scopes']}
    del values, access, secret
    with hard_deadline(60): image = sdk.Image.from_id(CACHED_IMAGE)
    image = runtime_image(sdk, image, root)
    volume = sdk.Volume.from_name(p['volume_name'], environment_name='main', create_if_missing=False)
    functions = create_functions(sdk, prepared, image=image, volume=volume,
        store_factory=R2StoreFactory(endpoint, prepared['scopes']), sessions=sessions)
    owned = {'attempted': False, 'created': False, 'volume_id': None, 'context': None, 'entered': False}
    current = ledger
    def ownership():
        save(output/'resource-ownership.json', {**{k: owned[k] for k in ('attempted', 'created', 'volume_id')},
            'volume_name': p['volume_name'], 'app_id': functions.app.app_id, 'environment': 'main'})
    def key(): return functions.app.app_id if functions.app.app_id in current['apps'] else pending
    def bind():
        identity = functions.app.app_id
        if identity and pending in current['apps']:
            if not re.fullmatch(r'ap-[A-Za-z0-9]+', identity) or identity in current['apps']:
                raise ValueError('New App identity collides with historical ledger')
            current['apps'][identity] = current['apps'].pop(pending)
            save(output/'ledger.json', current); ownership()
    def admission():
        nonlocal current
        current = refresh_billing(current, output, reservation_key=pending, runner=runner, additional_cap=10)
        with hard_deadline(60):
            owned['attempted'] = True; ownership()
            sdk.Volume.objects.create(p['volume_name'], environment_name='main', allow_existing=False)
            owned['created'] = True; ownership()
            volume.hydrate(); owned['volume_id'] = volume.object_id; ownership()
        owned['context'] = functions.app.run(name=p['app_name'], environment_name='main')
        with hard_deadline(300): owned['context'].__enter__()
        owned['entered'] = True; bind()
    def invoke(function, args, timeout):
        nonlocal current
        if function is functions.evaluate:
            if deadline-time.monotonic() < 3000:
                raise TimeoutError('Insufficient whole-attempt time for GPU, publication and cleanup; GPU forbidden')
            current = refresh_billing(current, output, app_id=functions.app.app_id,
                reservation_key=key(), runner=runner, additional_cap=10)
            if Decimal(current['apps'][functions.app.app_id]['known_reported_usd']) > Decimal('.75'):
                raise ValueError('CPU cost exceeds bounded pre-GPU envelope')
        remaining = deadline-time.monotonic()
        if remaining <= 0: raise TimeoutError('Whole-attempt deadline reached')
        return function.spawn(*args).get(timeout=min(timeout, remaining))
    def stopped(identity):
        return stop_verifier(functions.app.app_id, environment='main', command=(sys.executable, '-m', 'modal'), runner=runner)(identity)
    def cleanup():
        nonlocal current
        bind(); app_id = functions.app.app_id; context_error = None
        if owned['entered']:
            try:
                with hard_deadline(60): owned['context'].__exit__(None, None, None)
            except BaseException as caught: context_error = type(caught).__name__
            owned['entered'] = False
        if app_id:
            stop_and_audit_owned_app(app_id, runner=runner)
        if owned['created']:
            if not owned['volume_id']:
                with hard_deadline(60): volume.hydrate(); owned['volume_id'] = volume.object_id; ownership()
            delete_owned_volume(sdk, p['volume_name'], owned['volume_id'])
        elif owned['attempted']:
            raise RuntimeError('Unconfirmed Volume creation requires ownership audit')
        current = refresh_billing(current, output, app_id=app_id, reservation_key=key(), runner=runner, additional_cap=10)
        return {'app_stopped': True, 'active_containers': 0, 'active_sandboxes': 0,
                'volume_deleted': True, 'created_volume_deleted': owned['created'],
                'holds_released': 0, 'context_exit_error_type': context_error}
    return execute_phases(functions, prepared, output, admission=admission, invoke=invoke, stopped=stopped, cleanup=cleanup)


def execute(prepared, ledger, root, env_file, approved_sha256, *, sdk=None, runner=subprocess.run):
    """Consume one exact approval before secrets/access; never silently retry."""
    if approved_sha256 != prepared['cloud_plan_sha256']:
        raise ValueError('Exact cloud-plan approval required')
    p = prepared['cloud_plan']; root = Path(root).resolve()
    checked = prepare(p['input_bundle'], ledger, p['attempt_id'], root)
    if checked['cloud_plan_sha256'] != approved_sha256:
        raise ValueError('Approved code, scope, inputs or ledger changed')
    prepared = checked; p = prepared['cloud_plan']
    output = root/'.local'/'performance-eval'/p['attempt_id']
    if any(path.is_symlink() for path in (root/'.local', output.parent, output)):
        raise ValueError('Dedicated ignored attempt path required')
    output.mkdir(parents=True, exist_ok=True)
    with (output/'consumed.json').open('x') as marker:
        marker.write(json.dumps({'cloud_plan_sha256': approved_sha256, 'automatic_retries': 0}))
    try:
        deadline = time.monotonic()+LIMITS['controller_seconds']
        with hard_deadline(LIMITS['controller_seconds']):
            current = refresh_billing(ledger, output, runner=runner, additional_cap=10)
            pending = 'pending-'+p['attempt_id']
            if pending in current['apps']: raise ValueError('Attempt already reserved')
            current['apps'][pending] = {'state': 'unsettled_bounded', 'known_reported_usd': '0',
                'reservation_usd': '10', 'description': 'coppertop-performance',
                'acceptance_basis': 'Full approved paired evaluation hold; preserve every historical hold'}
            save(output/'ledger.json', current)
            save(output/'reservation.json', {'additional_usd': '10', 'overall_usd': '48',
                'prior_holds_usd': p['prior_conservative_holds_usd'], 'released_usd': '0', 'cloud_plan_sha256': approved_sha256})
            return _live(prepared, current, env_file, output, root, deadline=deadline, sdk=sdk, runner=runner)
    except BaseException as caught:
        save(output/'launch-failure.json', {'status': 'failed', 'error_type': type(caught).__name__,
             'automatic_retries': 0, 'consumed': True})
        raise


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--inputs', type=Path, required=True)
    parser.add_argument('--ledger', type=Path, required=True)
    parser.add_argument('--attempt', required=True)
    parser.add_argument('--approved-cloud-plan-sha256')
    parser.add_argument('--env-file', type=Path, default=Path.home()/'code/coppertop/.env.local')
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument('--prepare', action='store_true'); mode.add_argument('--execute', action='store_true')
    args = parser.parse_args(argv); root = Path(__file__).resolve().parents[1]
    try:
        ledger = read_json(args.ledger)
        prepared = prepare(read_json(args.inputs), ledger, args.attempt, root)
        if not args.execute:
            print(json.dumps({'cloud_plan': prepared['cloud_plan'], 'cloud_plan_sha256': prepared['cloud_plan_sha256'], 'cloud_actions': False}))
            return 0
        outcome = execute(prepared, ledger, root, args.env_file, args.approved_cloud_plan_sha256)
        print(json.dumps({'status': outcome['status'], 'output': str(root/'.local/performance-eval'/args.attempt)}))
        return 0 if outcome['status'] == 'passed' else 1
    except Exception as caught:
        detail = type(caught).__name__ if args.execute else type(caught).__name__+': '+str(caught)
        print('Performance evaluation blocked: '+detail, file=sys.stderr); return 1


if __name__ == '__main__':
    raise SystemExit(main())
