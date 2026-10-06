"""Define (never provision) single-use Modal Functions for cloud staging.

The caller supplies existing dedicated Volumes, a reviewed image and scoped
transfer Secret handles. No payload or parent credential enters this factory.
"""
import json
import os
import re
import subprocess
from dataclasses import dataclass
from pathlib import Path

from .cloud_stage import StageManifest, stage_objects, verify_stage, commit_directory, DeadlineStore, manifest_for_directory
from .chords import canonical
from .streams import read_small
from .cloud_smoke import validate_reload_result, validate_checkpoint_manifest
from .smoke_artifacts import fetch_smoke_checkpoint, publish_smoke_checkpoint

INPUT_MOUNT = '/coppertop-inputs'
OUTPUT_MOUNT = '/coppertop-outputs'
REPORT_LIMIT = 65536


@dataclass(frozen=True)
class ModalFunctions:
    app: object
    stage: object
    train: object
    publish: object
    restage: object
    reload: object
    publish_reload: object


def _identity():
    identity = os.environ.get('MODAL_TASK_ID', '')
    if not identity.startswith('ta-'):
        raise RuntimeError('Modal container identity unavailable')
    return identity


def _report(path):
    if path.is_symlink():
        raise ValueError('Report symlink rejected')
    with path.open('rb') as source:
        raw = source.read(REPORT_LIMIT + 1)
    if len(raw) > REPORT_LIMIT:
        raise ValueError('Report exceeds bound')
    return json.loads(raw)


def _run_command(builder, manifest, source, target, seconds, runner):
    command = builder(manifest, source, target)
    if not isinstance(command, (list, tuple)) or not command or any(not isinstance(arg, str) for arg in command):
        raise ValueError('Reviewed command builder must return argv')
    # Deliberately inherit only the image/runtime environment; GPU functions have
    # no transfer secrets or app-level inherited secrets.
    runner(command, check=True, timeout=seconds)


def create_functions(sdk, *, input_volume, output_volume, image, store_factory,
                     transfer_secrets, region: str, run_id: str, train_command, reload_command,
                     output_manifest_builder, cpu_seconds=1800, gpu_seconds=600,
                     startup_seconds=300, process_seconds=480, reload_seconds=120, runner=subprocess.run):
    """Register functions locally; caller separately authorizes app.run/remote.

    store_factory(role) selects 'input' or 'output' CAS namespace using only a
    scoped temporary session from the
    CPU Function environment. Images and callback closures must contain no keys.
    Returned receipts are small; bulk bytes stay between R2 and mounted Volumes.
    """
    limits = (cpu_seconds, gpu_seconds, startup_seconds, process_seconds, reload_seconds)
    if any(type(value) is not int or value <= 0 for value in limits) or process_seconds >= gpu_seconds or any(value > 3600 for value in limits) or gpu_seconds > 900 or reload_seconds > 120 or reload_seconds >= gpu_seconds:
        raise ValueError('Positive separate process/Function/startup bounds required')
    if getattr(sdk, '__version__', '1.6.1') != '1.6.1':
        raise ValueError('Modal SDK 1.6.1 required')
    if not region or input_volume is output_volume or not isinstance(transfer_secrets, dict) or set(transfer_secrets) != {'input', 'output'} or any(not value for value in transfer_secrets.values()):
        raise ValueError('Distinct dedicated Volumes, scoped CPU secrets and region required')
    if not isinstance(run_id, str) or not re.fullmatch(r'[A-Za-z0-9_-]{1,64}', run_id):
        raise ValueError('Bounded run identity required')
    inputs = input_volume.with_mount_options(sub_path='/'+run_id)
    outputs = output_volume.with_mount_options(sub_path='/'+run_id)
    def decode(raw):
        manifest = StageManifest.from_dict(raw)
        if manifest.run_id != run_id:
            raise ValueError('Manifest run differs from mounted run')
        return manifest
    app = sdk.App('coppertop-cloud-stage', secrets=[], volumes={}, include_source=False)
    common = dict(image=image, region=region, retries=0, min_containers=0,
                  max_containers=1, buffer_containers=0, scaledown_window=2,
                  single_use_containers=True, startup_timeout=startup_seconds,
                  serialized=True)
    cpu = dict(common, cpu=(2, 2), memory=(4096, 4096), timeout=cpu_seconds,
               gpu=None)
    cpu_input = dict(cpu, secrets=list(transfer_secrets['input']))
    cpu_output = dict(cpu, secrets=list(transfer_secrets['output']))
    gpu = dict(common, cpu=(4, 4), memory=(65536, 65536), gpu='L40S',
               timeout=gpu_seconds, secrets=[], block_network=True)

    @app.function(**dict(cpu_input, name='coppertop-stage', volumes={INPUT_MOUNT: inputs}))
    def stage(raw_manifest):
        manifest = decode(raw_manifest)
        input_volume.reload()
        store = DeadlineStore(store_factory('input'), cpu_seconds)
        receipt = stage_objects(store, manifest, Path(INPUT_MOUNT)/manifest.key,
                                max_seconds=cpu_seconds, commit=input_volume.commit)
        return dict(receipt, directory=str(Path(INPUT_MOUNT)/manifest.key), manifest=manifest.to_dict(), container_id=_identity(), phase='stage')

    @app.function(**dict(gpu, name='coppertop-train', volumes={
        INPUT_MOUNT: inputs.with_mount_options(read_only=True), OUTPUT_MOUNT: outputs}))
    def train(raw_manifest):
        manifest = decode(raw_manifest)
        source = Path(INPUT_MOUNT)/manifest.key
        verify_stage(manifest, source)
        target = Path(OUTPUT_MOUNT)/'train'
        target.parent.mkdir(parents=True, exist_ok=True)
        if target.exists():
            raise ValueError('Native output directory already exists')
        _run_command(train_command, manifest, source, target, process_seconds, runner)
        output = output_manifest_builder(manifest, target)
        if (output.run_id, output.release) != (manifest.run_id, manifest.release):
            raise ValueError('Generated manifest changed run/release')
        # Readiness marker and all data become visible through the Function's
        # mounted Volume commit, never a controller-side Sandbox commit.
        committed = Path(OUTPUT_MOUNT)/output.key
        if committed.exists():
            raise ValueError('Generated stage already exists')
        target.rename(committed)
        stage_receipt = commit_directory(output, committed, output_volume.commit)
        return dict(stage_receipt, output_manifest=output.to_dict(), commit_receipt=stage_receipt,
                    result=_report(committed/'result.json'), identity=_identity(), container_id=_identity(), phase='train')

    @app.function(**dict(cpu_output, name='coppertop-publish', volumes={
        OUTPUT_MOUNT: outputs.with_mount_options(read_only=True)}))
    def publish(raw_manifest):
        manifest = decode(raw_manifest)
        validate_checkpoint_manifest(manifest, run_id)
        output_volume.reload()
        directory = Path(OUTPUT_MOUNT)/manifest.key
        verify_stage(manifest, directory)
        receipt_key = publish_smoke_checkpoint(DeadlineStore(store_factory('output'), cpu_seconds), directory,
            _report(directory/'result.json'), _report(directory/'head-manifest.json'), manifest.run_id)
        return {'run_id': manifest.run_id, 'release': manifest.release,
                'checkpoint_key': receipt_key, 'container_id': _identity(), 'phase': 'publish'}

    @app.function(**dict(cpu_output, name='coppertop-restage', volumes={INPUT_MOUNT: inputs}))
    def restage(receipt_key, raw_manifest):
        manifest = decode(raw_manifest)
        validate_checkpoint_manifest(manifest, run_id)
        input_volume.reload()
        download = Path(INPUT_MOUNT)/(manifest.key+'-fetch')
        fetched = fetch_smoke_checkpoint(DeadlineStore(store_factory('output'), cpu_seconds), receipt_key, download, manifest.run_id)
        if fetched['artifacts'] != {name: ref.to_dict() for name, ref in manifest.objects.items()}:
            raise ValueError('R2 checkpoint differs from requested immutable manifest')
        committed = Path(INPUT_MOUNT)/manifest.key
        if committed.exists():
            raise ValueError('Restaged checkpoint already exists')
        download.rename(committed)
        receipt = commit_directory(manifest, committed, input_volume.commit)
        return dict(receipt, directory=str(committed), manifest=manifest.to_dict(), receipt=receipt, container_id=_identity(), phase='restage')

    @app.function(**dict(gpu, name='coppertop-reload', volumes={
        INPUT_MOUNT: inputs.with_mount_options(read_only=True), OUTPUT_MOUNT: outputs}))
    def reload(raw_input_manifest, raw_checkpoint_manifest, training_container_id):
        identity = _identity()
        if not training_container_id or identity == training_container_id:
            raise ValueError('Separate training and verifier containers required')
        manifest = decode(raw_checkpoint_manifest)
        input_manifest = decode(raw_input_manifest)
        if input_manifest.release != manifest.release:
            raise ValueError('Reload source release differs')
        source = Path(INPUT_MOUNT)/manifest.key
        original = Path(INPUT_MOUNT)/input_manifest.key
        verify_stage(manifest, source)
        verify_stage(input_manifest, original)
        target = Path(OUTPUT_MOUNT)/'reload'
        target.parent.mkdir(parents=True, exist_ok=True)
        if target.exists():
            raise ValueError('Native output directory already exists')
        builder = lambda _manifest, _source, _target: reload_command(input_manifest, manifest, original, source, target)
        _run_command(builder, manifest, source, target, reload_seconds, runner)
        result = _report(target/'result.json')
        validate_reload_result(result, manifest)
        result.update(run_id=manifest.run_id, container_id=identity, training_container_id=training_container_id)
        raw = canonical(result)
        if len(raw) > REPORT_LIMIT:
            raise ValueError('Verifier result exceeds bound')
        (target/'result.json').write_bytes(raw)
        report_manifest = manifest_for_directory(target, manifest.run_id, manifest.release, ['result.json'])
        committed = Path(OUTPUT_MOUNT)/report_manifest.key
        if committed.exists():
            raise ValueError('Verifier report stage already exists')
        target.rename(committed)
        receipt = commit_directory(report_manifest, committed, output_volume.commit)
        return {'run_id': manifest.run_id, 'release': manifest.release, 'phase': 'reload',
                'identity': identity, 'container_id': identity, 'training_container_id': training_container_id,
                'result': result, 'report_manifest': report_manifest.to_dict(), 'commit_receipt': receipt}

    @app.function(**dict(cpu_output, name='coppertop-publish-reload', volumes={
        OUTPUT_MOUNT: outputs.with_mount_options(read_only=True)}))
    def publish_reload(raw_report_manifest, verifier_container_id):
        manifest = decode(raw_report_manifest)
        if set(manifest.objects) != {'result.json'} or manifest.objects['result.json'].size > REPORT_LIMIT:
            raise ValueError('Exact bounded verifier report required')
        directory = Path(OUTPUT_MOUNT)/manifest.key
        verify_stage(manifest, directory)
        result = _report(directory/'result.json')
        if (result.get('container_id') != verifier_container_id
            or not verifier_container_id.startswith('ta-')
            or result.get('training_container_id') == verifier_container_id
            or result.get('status') != 'passed' or result.get('release') != manifest.release
            or result.get('fresh_process_reload_verified') is not True
            or result.get('velocity_exact') is not True):
            raise ValueError('Verifier report identity/status differs')
        raw = (directory/'result.json').read_bytes()
        store = DeadlineStore(store_factory('output'), cpu_seconds)
        report_key = store.put(raw)
        if report_key != manifest.objects['result.json'].sha256 or read_small(store, report_key, REPORT_LIMIT) != raw:
            raise ValueError('Durable verifier report read-back differs')
        return {'report_key': report_key, 'run_id': manifest.run_id, 'release': manifest.release,
                'container_id': verifier_container_id, 'phase': 'publish_reload'}

    return ModalFunctions(app, stage, train, publish, restage, reload, publish_reload)



def stop_verifier(app_id: str, *, environment='main', command=('modal',),
                  runner=subprocess.run, max_seconds=60, clock=None, sleep=None):
    """Bind a read-only, app-scoped stopped-container confirmation callback.

    Only pass container IDs returned by this App's phase receipts. Absence from
    TaskList proves no active container in this App; it is not a kill operation.
    """
    import time
    if not isinstance(app_id, str) or not re.fullmatch(r'ap-[A-Za-z0-9]+', app_id):
        raise ValueError('Exact Modal App ID required')
    if not isinstance(environment, str) or not re.fullmatch(r'[A-Za-z0-9_-]{1,64}', environment):
        raise ValueError('Explicit Modal environment required')
    if not isinstance(command, tuple) or not command or any(not isinstance(value, str) or not value for value in command):
        raise ValueError('Reviewed CLI command tuple required')
    if type(max_seconds) is not int or not 1 <= max_seconds <= 60:
        raise ValueError('Stop confirmation must fit within sixty seconds')
    now = clock or time.monotonic
    pause = sleep or time.sleep
    argv = [*command, 'container', 'list', '--app-id', app_id, '--env', environment, '--json']

    def confirm(container_id: str):
        if not isinstance(container_id, str) or not re.fullmatch(r'ta-[A-Za-z0-9]+', container_id):
            raise ValueError('Modal Function container ID required')
        deadline = now() + max_seconds
        while True:
            remaining = deadline - now()
            if remaining <= 0:
                raise TimeoutError('Modal container stop was not confirmed')
            response = runner(argv, check=True, capture_output=True, text=True,
                              timeout=min(10, remaining))
            raw = response.stdout
            if not isinstance(raw, str) or len(raw.encode('utf-8')) > REPORT_LIMIT:
                raise ValueError('Container list exceeds metadata bound')
            rows = json.loads(raw)
            if not isinstance(rows, list) or len(rows) > 64:
                raise ValueError('Bounded container list required')
            active = set()
            for row in rows:
                if (not isinstance(row, dict) or set(row) != {'container_id', 'app_id', 'app_name', 'start_time'}
                    or row['app_id'] != app_id or not isinstance(row['container_id'], str)
                    or not re.fullmatch(r'ta-[A-Za-z0-9]+', row['container_id'])
                    or row['container_id'] in active):
                    raise ValueError('Malformed or differently scoped container list')
                active.add(row['container_id'])
            if now() >= deadline:
                raise TimeoutError('Modal stop query exceeded deadline')
            if container_id not in active:
                return True
            pause(min(1, deadline - now()))
    return confirm
