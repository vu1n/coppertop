"""Executable, approval-gated reload-only Modal definitions; bulk bytes stay cloud-side."""
import json
import os
import re
import subprocess
import tempfile
import time
from dataclasses import dataclass
from pathlib import Path

from .chords import canonical, digest, object_path
from .cloud_stage import StageManifest, DeadlineStore, stage_objects, stage_receipt, verify_stage, commit_directory
from .cloud_smoke import reload_command, validate_checkpoint_manifest, validate_reload_result
from .gpu_smoke import RELEASE
from .cloud_roundtrip import _metadata
from .smoke_artifacts import fetch_smoke_checkpoint
from .streams import read_small

INPUT_MOUNT = '/coppertop-reload-inputs'
REPORT_LIMIT = 65536
CHECKPOINT_RUN_ID = 'b766e3e0-d9da-4429-b6b2-c55efb14f574'
ORIGINAL_CONTAINER_ID = 'ta-01M47X1ZKX3YA3K9YK9A0YZWBS'


def _container(value):
    if not isinstance(value, str) or len(value) > 128 or not re.fullmatch(r'ta-[A-Za-z0-9]+', value):
        raise ValueError('Actual Modal Function container identity required')
    return value


def _validate_pair(source, checkpoint, original):
    source.validate()
    validate_checkpoint_manifest(checkpoint, CHECKPOINT_RUN_ID)
    if source.run_id != checkpoint.run_id or source.release != RELEASE or object_path(RELEASE) not in source.objects:
        raise ValueError('Preserved pinned source/checkpoint identity required')
    if _container(original) != ORIGINAL_CONTAINER_ID:
        raise ValueError('Verified original training identity required')


@dataclass(frozen=True)
class ReloadFunctions:
    app: object
    stage_source: object
    restage_checkpoint: object
    verify: object
    publish_report: object
    source: StageManifest
    checkpoint: StageManifest
    checkpoint_key: str
    original_container_id: str


def create_reload_functions(sdk, *, volume, image, store_factory, transfer_secrets,
                            source: StageManifest, checkpoint: StageManifest,
                            checkpoint_key: str, original_container_id: str,
                            attempt_id: str, region: str, runner=subprocess.run):
    """Define only. Existing objects/session handles are bound by approved caller.

    store_factory(role) reads only role-scoped temporary CPU session environment;
    callbacks/images must contain no parent credentials. No training is exposed.
    """
    _validate_pair(source, checkpoint, original_container_id)
    object_path(checkpoint_key)
    if getattr(sdk, '__version__', None) != '1.6.1':
        raise ValueError('Modal SDK 1.6.1 required')
    if not isinstance(attempt_id, str) or not re.fullmatch(r'[A-Za-z0-9_-]{1,64}', attempt_id) or not region:
        raise ValueError('Explicit attempt directory and compute region required')
    roles = {'source', 'checkpoint', 'report'}
    if not isinstance(transfer_secrets, dict) or set(transfer_secrets) != roles or any(not values for values in transfer_secrets.values()):
        raise ValueError('Scoped source/checkpoint/report CPU session handles required')
    mount = volume.with_mount_options(sub_path='/'+attempt_id)
    app = sdk.App('coppertop-reload-only', secrets=[], volumes={}, include_source=False)
    common = dict(image=image, region=region, serialized=True, retries=0,
                  single_use_containers=True, min_containers=0, buffer_containers=0,
                  max_containers=1, scaledown_window=2, startup_timeout=300)
    cpu = dict(common, cpu=(2, 2), memory=(4096, 4096), gpu=None)

    @app.function(**dict(cpu, name='reload-source-stage', timeout=600,
                        secrets=list(transfer_secrets['source']), volumes={INPUT_MOUNT:mount}))
    def stage_source():
        identity = _container(os.environ.get('MODAL_TASK_ID'))
        receipt = stage_objects(DeadlineStore(store_factory('source'), 600), source,
            Path(INPUT_MOUNT)/source.key, max_seconds=600, commit=volume.commit)
        return {'container_id':identity, 'receipt':receipt}

    @app.function(**dict(cpu, name='reload-checkpoint-stage', timeout=1800,
                        secrets=list(transfer_secrets['checkpoint']), volumes={INPUT_MOUNT:mount}))
    def restage_checkpoint():
        identity = _container(os.environ.get('MODAL_TASK_ID'))
        directory = Path(INPUT_MOUNT)/checkpoint.key
        partial = Path(INPUT_MOUNT)/(checkpoint.key+'-fetch')
        receipt = fetch_smoke_checkpoint(DeadlineStore(store_factory('checkpoint'), 1800),
                                        checkpoint_key, partial, checkpoint.run_id)
        if receipt['artifacts'] != {name:ref.to_dict() for name,ref in checkpoint.objects.items()}:
            raise ValueError('Existing R2 checkpoint differs from pinned manifest')
        if directory.exists():
            raise ValueError('Checkpoint stage already exists')
        partial.rename(directory)
        committed = commit_directory(checkpoint, directory, volume.commit)
        return {'container_id':identity, 'receipt':committed}

    @app.function(**dict(common, name='reload-native-verifier', timeout=300,
        gpu='L40S', cpu=(4,4), memory=(65536,65536), secrets=[], block_network=True,
        volumes={INPUT_MOUNT:mount.with_mount_options(read_only=True)}))
    def verify():
        identity = _container(os.environ.get('MODAL_TASK_ID'))
        if identity == original_container_id:
            raise ValueError('Fresh container required')
        original = Path(INPUT_MOUNT)/source.key
        weights = Path(INPUT_MOUNT)/checkpoint.key
        verify_stage(source, original)
        verify_stage(checkpoint, weights)
        with tempfile.TemporaryDirectory(prefix='coppertop-reload-') as scratch:
            target = Path(scratch)/'result'  # Native CLI owns creation of output.
            command = reload_command(source, checkpoint, original, weights, target)
            runner(command, check=True, timeout=120)
            path = target/'result.json'
            if path.is_symlink():
                raise ValueError('Verifier result symlink rejected')
            with path.open('rb') as stream:
                raw = stream.read(REPORT_LIMIT+1)
            if len(raw) > REPORT_LIMIT:
                raise ValueError('Verifier result exceeds metadata cap')
            result = json.loads(raw)
        _metadata(result)
        validate_reload_result(result, checkpoint)
        result.update(run_id=checkpoint.run_id, container_id=identity,
            training_container_id=original_container_id, source_manifest_key=source.key,
            checkpoint_manifest_key=checkpoint.key, checkpoint_receipt_key=checkpoint_key)
        raw = canonical(result)
        if len(raw) > REPORT_LIMIT:
            raise ValueError('Bound verifier result exceeds metadata cap')
        return {'container_id':identity, 'result':result, 'report_sha256':digest(raw)}

    def validate_report(record):
        if not isinstance(record, dict) or set(record) != {'container_id','result','report_sha256'}:
            raise ValueError('Exact verifier metadata envelope required')
        _metadata(record)
        identity = _container(record['container_id'])
        result = record['result']
        validate_reload_result(result, checkpoint)
        expected = {'run_id':checkpoint.run_id, 'container_id':identity,
            'training_container_id':original_container_id, 'source_manifest_key':source.key,
            'checkpoint_manifest_key':checkpoint.key, 'checkpoint_receipt_key':checkpoint_key}
        if identity == original_container_id or any(result.get(name) != value for name,value in expected.items()):
            raise ValueError('Verifier report binding differs')
        raw = canonical(result)
        if len(raw) > REPORT_LIMIT or digest(raw) != record['report_sha256']:
            raise ValueError('Verifier report content reference differs')
        return raw

    @app.function(**dict(cpu, name='reload-report-publisher', timeout=300,
                        secrets=list(transfer_secrets['report']), volumes={}))
    def publish_report(record):
        identity = _container(os.environ.get('MODAL_TASK_ID'))
        raw = validate_report(record)
        store = DeadlineStore(store_factory('report'), 300)
        key = store.put(raw)
        if key != digest(raw) or read_small(store, key, REPORT_LIMIT) != raw:
            raise ValueError('R2 verifier report read-back differs')
        return {'container_id':identity, 'report_key':key, 'run_id':checkpoint.run_id,
                'release':checkpoint.release, 'verifier_container_id':record['container_id']}

    return ReloadFunctions(app, stage_source, restage_checkpoint, verify, publish_report,
                           source, checkpoint, checkpoint_key, original_container_id)


def _invoke(function, args, timeout):
    return function.spawn(*args).get(timeout=timeout)


def execute_reload(functions: ReloadFunctions, attempt_dir: Path, *, admission,
                   image_gate, stop_verify, cleanup, invoke=_invoke, clock=time.monotonic, reservation=None):
    """One consumed attempt. Caller owns approved app.run and billing/auth gates.

    cleanup(success) must stop the owned App, independently audit all containers
    and Sandboxes, and delete only the new owned Volume after zero-active proof.
    image_gate must be bounded and return its actual container ID and passed status.
    """
    _validate_pair(functions.source, functions.checkpoint, functions.original_container_id)
    attempt_dir.mkdir(parents=True, exist_ok=True)
    required = {'run_id':functions.checkpoint.run_id, 'checkpoint_key':functions.checkpoint_key, 'automatic_retries':0}
    if reservation is None:
        with (attempt_dir/'consumed.json').open('x') as marker:
            marker.write(json.dumps(required))
    else:
        if (not isinstance(reservation,dict) or any(reservation.get(k) != v for k,v in required.items())
            or json.loads((attempt_dir/'consumed.json').read_text()) != reservation):
            raise ValueError('Exact consumed authorization reservation required')
    # A launcher may reserve before credential signing; its marker cannot replay
    # this controller because the second phase start is also exclusive.
    with (attempt_dir/'controller-started.json').open('x') as marker:
        marker.write(json.dumps(required))
    state = {'status':'started', 'phases':{}, 'checkpoint_preserved':True}
    deadline = clock()+7200
    def save():
        temporary = attempt_dir/'outcome.json.partial'
        temporary.write_bytes(canonical(state))
        temporary.replace(attempt_dir/'outcome.json')
    def remaining(bound):
        left = deadline-clock()
        if left <= 0:
            raise TimeoutError('Reload-only whole-attempt deadline reached')
        return min(bound, left)
    def record(phase, response):
        # Save actual owned IDs before checking bounded metadata/status bindings.
        identity = _container(response.get('container_id') if isinstance(response,dict) else None)
        state['phases'][phase] = {'container_id':identity}
        save()
        _metadata(response)
        return identity
    def stopped(identity):
        if remaining(60) < 60:
            raise TimeoutError('Insufficient whole-attempt time for bounded stop confirmation')
        if stop_verify(identity) is not True:
            raise RuntimeError('Owned phase container stop unconfirmed')
    primary = None
    succeeded = False
    try:
        save()
        admission()
        remaining(1)
        def stage(phase, function, manifest, bound):
            response = invoke(function, (), remaining(bound))
            identity = record(phase, response)
            if response.get('receipt') != stage_receipt(manifest):
                raise ValueError('CPU committed stage receipt differs')
            stopped(identity)
            state['phases'][phase]['receipt'] = response['receipt']
            save()
        stage('source', functions.stage_source, functions.source, 900)
        gate = image_gate()
        identity = record('image_gate', gate)
        if gate.get('status') != 'passed':
            raise ValueError('CPU image compatibility gate did not pass')
        stopped(identity)
        stage('checkpoint', functions.restage_checkpoint, functions.checkpoint, 2100)
        verified = invoke(functions.verify, (), remaining(600))
        identity = record('verify', verified)
        validate_reload_result(verified.get('result'), functions.checkpoint)
        expected = {'run_id':functions.checkpoint.run_id, 'container_id':identity,
            'training_container_id':functions.original_container_id,
            'source_manifest_key':functions.source.key, 'checkpoint_manifest_key':functions.checkpoint.key,
            'checkpoint_receipt_key':functions.checkpoint_key}
        if identity == functions.original_container_id or any(verified['result'].get(name)!=value for name,value in expected.items()):
            raise ValueError('Fresh verifier identity/provenance mismatch')
        raw = canonical(verified['result'])
        if len(raw)>REPORT_LIMIT or digest(raw)!=verified.get('report_sha256'):
            raise ValueError('Bounded verifier report hash differs')
        state['phases']['verify'].update(result=verified['result'],report_sha256=digest(raw))
        save()
        stopped(identity)
        published = invoke(functions.publish_report, (verified,), remaining(600))
        publisher = record('publish', published)
        if (published.get('report_key') != digest(raw) or published.get('run_id') != functions.checkpoint.run_id
            or published.get('release') != functions.checkpoint.release or published.get('verifier_container_id') != identity):
            raise ValueError('Durable report receipt differs')
        stopped(publisher)
        state['phases']['publish'].update(published)
        state['status'] = 'passed'
        succeeded = True
    except BaseException as error:
        primary = error
        state.update(status='failed', error_type=type(error).__name__)
    finally:
        try:
            receipt = cleanup(succeeded)
            _metadata(receipt)
            state['cleanup'] = receipt
            if (not isinstance(receipt,dict)
                or any(type(receipt.get(name)) is not int or receipt[name] != 0 for name in ('active_containers','active_sandboxes'))
                or receipt.get('volume_deleted') is not True or receipt.get('app_stopped') is not True):
                raise RuntimeError('Owned cleanup verification incomplete')
        except BaseException as error:
            state.update(status='failed',cleanup_error_type=type(error).__name__)
            if primary is None:
                primary = error
        save()
    if primary is not None:
        raise primary
    return state
