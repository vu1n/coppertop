"""Fixed pinned smoke commands for Volume-mounted cloud stages."""
import json
from pathlib import Path
from .cloud_stage import StageManifest, manifest_for_directory, MAX_MANIFEST_BYTES
from .gpu_smoke import RELEASE, MODEL_REPO, MODEL_REVISION
from .smoke_runtime import MAX_DELTA_BYTES, MAX_VALIDATION_BYTES
from .chords import object_path

ARTIFACT_LIMITS = {'head.safetensors': MAX_DELTA_BYTES, 'validation.safetensors': MAX_VALIDATION_BYTES,
                   'head-manifest.json': MAX_MANIFEST_BYTES, 'result.json': MAX_MANIFEST_BYTES}
ARTIFACTS = tuple(ARTIFACT_LIMITS)


def _common(source: Path, target: Path):
    return ['--execute-cuda', '--store', str(source), '--model', '/opt/model',
            '--pins', '/opt/gpu-smoke-pins.json', '--evaluator', '/opt/evaluator.py',
            '--output', str(target)]


def train_command(manifest: StageManifest, source: Path, target: Path):
    if manifest.release != RELEASE or object_path(RELEASE) not in manifest.objects:
        raise ValueError('Pinned native input release required')
    return ['/opt/conda/bin/python','-m','coppertop.smoke_runtime', *_common(source,target),
            '--max-seconds','480']


def reload_command(inputs: StageManifest, checkpoint: StageManifest, source: Path,
                   weights: Path, target: Path):
    if (inputs.release, checkpoint.release, inputs.run_id) != (RELEASE, RELEASE, checkpoint.run_id):
        raise ValueError('Pinned input/checkpoint identities required')
    validate_checkpoint_manifest(checkpoint)
    head = checkpoint.objects['head.safetensors']; case = checkpoint.objects['validation.safetensors']
    return ['/opt/conda/bin/python','-m','coppertop.smoke_reload', *_common(source,target),
        '--head',str(weights/'head.safetensors'),'--validation',str(weights/'validation.safetensors'),
        '--checkpoint-sha256',head.sha256,'--checkpoint-bytes',str(head.size),
        '--validation-sha256',case.sha256,'--validation-bytes',str(case.size), '--max-seconds','120']


def output_manifest(inputs: StageManifest, directory: Path):
    for name, cap in ARTIFACT_LIMITS.items():
        path = directory/name
        if path.is_symlink() or not path.is_file() or not 0 < path.stat().st_size <= cap:
            raise ValueError('Native artifact exceeds bound')
    result = json.loads((directory/'result.json').read_bytes())
    metadata = json.loads((directory/'head-manifest.json').read_bytes())
    if (result.get('status') != 'passed' or result.get('release') != inputs.release
        or metadata.get('release') != inputs.release or inputs.release != RELEASE
        or metadata.get('base_model') != MODEL_REPO or metadata.get('revision') != MODEL_REVISION):
        raise ValueError('Native output identity differs')
    manifest = manifest_for_directory(directory, inputs.run_id, inputs.release, ARTIFACTS)
    for name, prefix in [('head.safetensors','checkpoint'),('validation.safetensors','validation')]:
        ref = manifest.objects[name]
        if any(value.get(prefix+'_sha256') != ref.sha256 or value.get(prefix+'_bytes') != ref.size
               for value in (result,metadata)):
            raise ValueError('Native output payload differs from receipt')
    return manifest


def validate_checkpoint_manifest(manifest: StageManifest, run_id=None):
    manifest.validate()
    if (manifest.release != RELEASE or (run_id is not None and manifest.run_id != run_id)
        or set(manifest.objects) != set(ARTIFACT_LIMITS)
        or any(ref.size > ARTIFACT_LIMITS[name] for name, ref in manifest.objects.items())):
        raise ValueError('Pinned bounded checkpoint output manifest required')


def validate_reload_result(result: dict, manifest: StageManifest):
    validate_checkpoint_manifest(manifest)
    expected = {'status': 'passed', 'release': manifest.release,
        'kind': 'native_head_fresh_process_reload', 'model_repo': MODEL_REPO,
        'model_revision': MODEL_REVISION, 'cutoff': 6}
    if not isinstance(result, dict) or any(result.get(key) != value for key, value in expected.items()):
        raise ValueError('Verifier pinned identity differs')
    if (result.get('fresh_process_reload_verified') is not True or result.get('velocity_exact') is not True
        or type(result.get('velocity_max_abs_difference')) not in (int, float)
        or result['velocity_max_abs_difference'] != 0):
        raise ValueError('Fresh verifier did not pass exact comparison')
    for name, prefix in [('head.safetensors','checkpoint'),('validation.safetensors','validation')]:
        ref = manifest.objects[name]
        if result.get(prefix+'_sha256') != ref.sha256 or type(result.get(prefix+'_bytes')) is not int or result[prefix+'_bytes'] != ref.size:
            raise ValueError('Verifier artifacts differ from immutable manifest')
