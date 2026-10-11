"""Sequential cloud phases; only bounded metadata returns to the controller."""
import math
from dataclasses import dataclass
from pathlib import Path
from typing import Callable
from .cloud_stage import StageManifest, stage_receipt
from .cloud_smoke import validate_checkpoint_manifest, validate_reload_result
from .chords import canonical, object_path


@dataclass(frozen=True)
class PhaseCalls:
    provider: str
    stage: Callable
    train: Callable
    publish: Callable
    restage: Callable
    reload: Callable
    publish_reload: Callable
    stop_verify: Callable[[str], bool]

    def __post_init__(self):
        if self.provider not in ('modal', 'runpod'):
            raise ValueError('Supported provider required')


def _committed(value, manifest):
    expected = stage_receipt(manifest)
    if not isinstance(value, dict) or any(value.get(k) != v for k, v in expected.items()):
        raise ValueError('Provider commit receipt differs')


def _identity(value):
    if not isinstance(value, dict):
        raise ValueError('Provider container metadata required')
    identity = value.get('container_id')
    if not isinstance(identity, str) or not 0 < len(identity) <= 128:
        raise ValueError('Provider container identity required')
    return identity


def run_roundtrip(calls: PhaseCalls, inputs: StageManifest, receipts: Path):
    """Caller separately binds approved credentials, fresh billing and resource caps.

    A consumed run directory prevents accidental replay. GPU completion is not
    shutdown: an independent provider stop verification gates the next phase.
    """
    inputs.validate()
    receipts.mkdir(parents=True, exist_ok=False)
    record = {'schema_version': 1, 'provider': calls.provider, 'run_id': inputs.run_id,
              'release': inputs.release, 'status': 'running', 'automatic_retries': 0, 'phases': {}}
    def save():
        raw = canonical(record)
        if len(raw) > 65536:
            raise ValueError('Controller receipt exceeds metadata bound')
        temp = receipts/'run.json.partial'
        temp.write_bytes(raw)
        temp.replace(receipts/'run.json')
    def accept(phase, result):
        _metadata(result)
        record['phases'][phase] = result
        save()
    save()
    active = None
    try:
        record['phase'] = 'stage'; save()
        staged = _metadata(calls.stage(inputs.to_dict()))
        _committed(staged, inputs); accept('stage', staged)
        record['phase'] = 'train'; save()
        trained = calls.train(inputs.to_dict())
        active = _identity(trained)
        trained = _metadata(trained)
        output = StageManifest.from_dict(trained['output_manifest'])
        if (output.run_id, output.release) != (inputs.run_id, inputs.release):
            raise ValueError('GPU output identity differs')
        validate_checkpoint_manifest(output, inputs.run_id)
        _committed(trained, output); accept('train', trained)
        record['phase'] = 'train_shutdown'; save()
        if calls.stop_verify(active) is not True:
            raise RuntimeError('Training container shutdown unverified')
        accept('train_shutdown', {'container_id': active, 'stopped': True})
        training_identity = active
        active = None
        record['phase'] = 'publish'; save()
        published = _metadata(calls.publish(output.to_dict()))
        if (published.get('run_id'), published.get('release')) != (inputs.run_id, inputs.release):
            raise ValueError('Durable receipt identity differs')
        key = published['checkpoint_key']; object_path(key)
        accept('publish', published)
        record['phase'] = 'restage'; save()
        restaged = _metadata(calls.restage(key, output.to_dict()))
        _committed(restaged, output); accept('restage', restaged)
        record['phase'] = 'reload'; save()
        reloaded = calls.reload(inputs.to_dict(), output.to_dict(), training_identity)
        active = _identity(reloaded)
        reloaded = _metadata(reloaded)
        if active == training_identity:
            raise ValueError('Verifier reused the training container')
        result = reloaded['result']
        if (reloaded.get('run_id'), reloaded.get('release')) != (inputs.run_id, inputs.release):
            raise ValueError('Verifier identity differs')
        validate_reload_result(result, output)
        accept('reload', reloaded)
        record['phase'] = 'reload_shutdown'; save()
        if calls.stop_verify(active) is not True:
            raise RuntimeError('Verifier container shutdown unverified')
        accept('reload_shutdown', {'container_id': active, 'stopped': True})
        active = None
        record['phase'] = 'publish_reload'; save()
        report_manifest = StageManifest.from_dict(reloaded['report_manifest'])
        if (report_manifest.run_id, report_manifest.release) != (inputs.run_id, inputs.release):
            raise ValueError('Reload report manifest identity differs')
        _committed(reloaded['commit_receipt'], report_manifest)
        published_report = _metadata(calls.publish_reload(report_manifest.to_dict(), reloaded['container_id']))
        if (published_report.get('run_id'), published_report.get('release'), published_report.get('container_id')) != (inputs.run_id, inputs.release, reloaded['container_id']):
            raise ValueError('Published verifier report identity differs')
        object_path(published_report['report_key'])
        if set(report_manifest.objects) != {'result.json'} or published_report['report_key'] != report_manifest.objects['result.json'].sha256:
            raise ValueError('Published verifier report checksum differs')
        accept('publish_reload', published_report)
        record.update(status='passed', phase='complete')
        save()
        return record
    except BaseException as error:
        record.update(status='failed', error_type=type(error).__name__)
        save()
        raise
    finally:
        # Provider call adapters must clean up on allocation/return failures where
        # the controller never received a container ID. Never discover unrelated jobs.
        if active is not None:
            try:
                record['cleanup_verified'] = calls.stop_verify(active) is True
            except BaseException as cleanup_error:
                record['cleanup_verified'] = False
                record['cleanup_error_type'] = type(cleanup_error).__name__
            if not record['cleanup_verified']:
                record.update(status='shutdown_unverified', active_container_id=active)
            save()


def _metadata(value):
    if not isinstance(value, dict):
        raise ValueError('Phase must return metadata object')
    stack = [(value, 0)]
    nodes = size = 0
    while stack:
        item, depth = stack.pop()
        nodes += 1
        if nodes > 4096 or depth > 16:
            raise ValueError('Phase metadata structure exceeds bound')
        if isinstance(item, dict):
            if len(item) > 256:
                raise ValueError('Phase metadata field count exceeds bound')
            for key, child in item.items():
                if not isinstance(key, str) or len(key) > 256:
                    raise ValueError('Bounded metadata string keys required')
                size += len(key.encode('utf-8'))
                stack.append((child, depth + 1))
        elif isinstance(item, list):
            if len(item) > 256:
                raise ValueError('Phase metadata list exceeds bound')
            stack.extend((child, depth + 1) for child in item)
        elif isinstance(item, str):
            if len(item) > 65536:
                raise ValueError('Phase metadata string exceeds bound')
            size += len(item.encode('utf-8'))
        elif item is None or isinstance(item, bool):
            size += 5
        elif type(item) is int:
            if item.bit_length() > 256:
                raise ValueError('Phase metadata integer exceeds bound')
            size += 80
        elif type(item) is float and math.isfinite(item):
            size += 32
        else:
            raise ValueError('Phase metadata is not finite JSON')
        if size > 65536:
            raise ValueError('Phase metadata exceeds byte bound')
    if len(canonical(value)) > 65536:
        raise ValueError('Phase metadata serialization exceeds bound')
    return value
