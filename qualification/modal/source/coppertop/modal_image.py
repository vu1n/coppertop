"""Offline definitions for a warm-image update and its bounded CPU gate.

Constructing these definitions performs no build or provider lookup. The caller
must supply the separately verified seed handle and authorize its later use.
"""
import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path
from types import SimpleNamespace

from .chords import canonical
from .cloud_stage import StageManifest, verify_stage, _file_reference
from .streams import ObjectRef
from .image_check import (validate, pinned_dependencies, dependency_report,
                          installed_dependencies, require_dependencies,
                          require_native_interpreter, NATIVE_PYTHON)

SEED_IMAGE_ID = 'im-MGjcv5j3hs0KKoxdxCexhs'
INPUT_MOUNT = '/coppertop-inputs'
EVALUATOR_FILE = '.local/native/evaluator.py'
PACKAGE_FILES = tuple('coppertop/' + name + '.py' for name in (
    '__init__', '__main__', 'adapter', 'checkpoint', 'chords', 'cloud_roundtrip',
    'cloud_smoke', 'cloud_stage', 'dependency_gate', 'episode', 'gpu_launch', 'gpu_smoke', 'image_check',
    'ingest', 'lerobot', 'modal_image', 'modal_reload', 'modal_staging', 'multipart', 'publication',
    'reload_launch', 'r2_session', 'runpod_staging', 's3', 'smoke_artifacts', 'smoke_reload',
    'smoke_runtime', 'streams', 'verify_loader', 'worker', 'xiaomi'))
RECIPE_FILES = PACKAGE_FILES + (
    'containers/Dockerfile.gpu-smoke', 'containers/gpu-smoke-pins.json',
    'containers/gpu-smoke-seed-runtime.json',
    'requirements-gpu-smoke.lock', 'requirements-r2.txt',
    'tools/prepare_gpu_fixture.py', 'tools/run_reload_only.py')
OFFLINE_ENV = {'HF_HUB_OFFLINE': '1', 'TRANSFORMERS_OFFLINE': '1',
    'HF_HUB_DISABLE_TELEMETRY': '1', 'HF_HOME': '/tmp/coppertop-hf',
    'HF_MODULES_CACHE': '/tmp/coppertop-hf/modules', 'PYTHONPATH': '/app',
    'PYTHONDONTWRITEBYTECODE': '1', 'TOKENIZERS_PARALLELISM': 'false'}


def _hash_files(root, names):
    hashes = {}
    for name in names:
        path = root / name
        if path.is_symlink() or not path.is_file() or path.stat().st_size > 1024 * 1024:
            raise ValueError('Bounded regular reviewed image input required: ' + name)
        hashes[name] = hashlib.sha256(path.read_bytes()).hexdigest()
    return hashes


def runtime_dependencies(root):
    sdk_pins = pinned_dependencies(root/'requirements-r2.txt')
    expected = {'boto3': '1.43.108', 'botocore': '1.43.108', 's3transfer': '0.19.2',
        'jmespath': '1.1.0', 'python-dateutil': '2.9.0.post0', 'urllib3': '2.8.0', 'six': '1.17.0'}
    if sdk_pins != expected:
        raise ValueError('Reviewed R2 SDK pins required')
    pins = pinned_dependencies(root/'requirements-gpu-smoke.lock', root/'requirements-r2.txt')
    if {'torch', 'torchvision', 'flash-attn'} & pins.keys():
        raise ValueError('Cached CUDA stack must not be reinstalled')
    return pins


def _install_runtime():
    """Align both exact locks while retaining cached Torch, Flash Attention and model."""
    root = Path('/app')
    pins = runtime_dependencies(root)
    require_native_interpreter()
    prepare_cached_layout()
    print(canonical({'dependency_alignment_before':
        dependency_report(pins, installed_dependencies(pins))}).decode())
    # The disposable seed may contain earlier project modules. Remove only stale
    # Python sources/bytecode in this exact package; keep native model/cache files.
    package = root/'coppertop'
    allowed = {Path(name).name for name in PACKAGE_FILES}
    if package.is_symlink() or not package.is_dir():
        raise ValueError('Regular project package required')
    if not allowed <= {path.name for path in package.glob('*.py')}:
        raise ValueError('Reviewed package source missing')
    for path in (*package.glob('*.py'), *package.glob('*.pyc')):
        if path.name not in allowed:
            path.unlink()
    cache = package/'__pycache__'
    if cache.is_symlink():
        cache.unlink()
    elif cache.is_dir():
        # shutil.rmtree unlinks nested symlinks rather than following them.
        if cache.parent.resolve() != package.resolve():
            raise ValueError('Package bytecode cache escapes owned package')
        shutil.rmtree(cache)
    env = {key: value for key, value in os.environ.items()
           if not key.startswith('PIP_') and key not in ('PYTHONHOME', 'PYTHONPATH')}
    env['PIP_CONFIG_FILE'] = os.devnull
    subprocess.run([sys.executable, '-m', 'pip', '--isolated', 'install',
        '--no-input', '--disable-pip-version-check', '--no-deps', '--no-cache-dir',
        '--only-binary=:all:', '--retries', '0', '--timeout', '15',
        '--index-url', 'https://pypi.org/simple', '-r', str(root/'requirements-gpu-smoke.lock'),
        '-r', str(root/'requirements-r2.txt')], check=True, timeout=480, env=env)
    require_dependencies(pins)
    subprocess.run([sys.executable, '-m', 'pip', '--isolated', 'check'], check=True, timeout=30, env=env)


def prepare_cached_layout():
    """Reuse existing assets at the same paths as the previous verified runtime."""
    model, cached = Path('/opt/model'), Path('/work/model')
    if not (cached/'model.safetensors.index.json').is_file():
        raise ValueError('Warm checkpoint cache absent; no download/fallback')
    if model.exists() or model.is_symlink():
        if not model.is_dir() or model.resolve() != cached.resolve():
            raise ValueError('Native model path differs from cached model')
    else:
        model.symlink_to(cached, target_is_directory=True)
    pin = json.loads(Path('/opt/gpu-smoke-pins.json').read_bytes())['flash_attention_wheel']
    name = pin['name']
    if Path(name).name != name:
        raise ValueError('Cached wheel filename must be a basename')
    ref = ObjectRef(pin['digest'].removeprefix('sha256:'), pin['size'])
    if _file_reference(Path('/work')/name, ref.size) != ref:
        raise ValueError('Cached wheel checksum differs')


def _native_digest(hashes):
    names = ['containers/Dockerfile.gpu-smoke', 'containers/gpu-smoke-pins.json',
             'requirements-gpu-smoke.lock', *sorted(PACKAGE_FILES)]
    digest = hashlib.sha256()
    for name in names:
        digest.update(name.encode() + b'\0' + bytes.fromhex(hashes[name]))
    return digest.hexdigest()


@dataclass(frozen=True)
class ImagePreparation:
    image: object
    recipe: dict
    recipe_sha256: str


def build_recipe(root: Path, *, seed_id=SEED_IMAGE_ID, region: str):
    """Pure local approval identity, requiring no SDK or provider handle."""
    if seed_id != SEED_IMAGE_ID or not isinstance(region, str) or not region:
        raise ValueError('Reviewed seed identity and region required')
    root = Path(root)
    hashes = _hash_files(root, RECIPE_FILES)
    pins = runtime_dependencies(root)
    inventory = json.loads((root/'containers/gpu-smoke-seed-runtime.json').read_bytes())
    if (inventory.get('schema_version') != 1 or inventory.get('seed_image_id') != seed_id
        or inventory.get('evidence_kind') != 'historical_seed_install_log'
        or not isinstance(inventory.get('versions'), dict)):
        raise ValueError('Historical inventory must identify the exact seed')
    evaluator = root/EVALUATOR_FILE
    if evaluator.is_symlink() or not evaluator.is_file():
        raise ValueError('Pinned local evaluator required before provider access')
    with evaluator.open('rb') as source:
        raw = source.read(32769)
    blob = hashlib.sha1(b'blob '+str(len(raw)).encode()+b'\0'+raw).hexdigest()
    expected_blob = json.loads((root/'containers/gpu-smoke-pins.json').read_bytes())['evaluator_git_blob']
    if len(raw) > 32768 or blob != expected_blob:
        raise ValueError('Bounded pinned evaluator bytes differ')
    return {'schema_version': 1, 'seed_image_id': seed_id, 'files': hashes,
        'evaluator': {'git_blob': blob, 'sha256': hashlib.sha256(raw).hexdigest(),
                      'size': len(raw), 'image_path': '/opt/evaluator.py'},
        'native_recipe_sha256': _native_digest(hashes),
        'dependency_alignment': {'expected': pins,
            'historical_seed_report': dependency_report(pins, inventory['versions']),
            'inventory_is_fresh': False,
            'installer_lock_files': ['requirements-gpu-smoke.lock', 'requirements-r2.txt'],
            'live_gate': 'All installed versions, CUDA ABI, model hashes and processor parity before GPU'},
        'environment': OFFLINE_ENV, 'region': region,
        'build': {'cpu': (4, 4), 'memory': (16384, 16384), 'timeout': 600, 'gpu': None,
                  'native_python': NATIVE_PYTHON, 'pip_isolated': True, 'pip_config_file': '/dev/null',
                  'cached_model': '/work/model', 'native_model_alias': '/opt/model',
                  'cached_wheel': 'verify pinned length/hash without download or reinstall',
                  'secrets': [], 'include_source': False, 'installer': 'coppertop.modal_image._install_runtime',
                  'cleanup': 'remove unexpected /app/coppertop/*.py and *.pyc plus owned __pycache__'}}


def prepare_image(sdk, *, seed, seed_id: str, root: Path, region: str):
    """Return a lazy image recipe; never call from_id, hydrate, build or download."""
    if sdk.__version__ != '1.6.1':
        raise ValueError('Modal SDK 1.6.1 required')
    root = Path(root)
    recipe = build_recipe(root, seed_id=seed_id, region=region)
    image = seed
    for name in RECIPE_FILES:
        image = image.add_local_file(root / name, '/app/' + name, copy=True)
    image = image.add_local_file(root/'containers/gpu-smoke-pins.json', '/opt/gpu-smoke-pins.json', copy=True)
    image = image.add_local_file(root/EVALUATOR_FILE, '/opt/evaluator.py', copy=True)
    image = image.env(OFFLINE_ENV).workdir('/app')
    # SDK 1.6.1 forwards tuple limits to its Function resource protobuf even
    # though Image.run_function's public annotations list only scalar requests.
    image = image.run_function(_install_runtime, cpu=(4, 4), memory=(16384, 16384), timeout=600,
        gpu=None, secrets=[], region=region, include_source=False)
    return ImagePreparation(image, recipe, hashlib.sha256(canonical(recipe)).hexdigest())


def create_image_gate(app, *, prepared: ImagePreparation, input_volume, run_id: str, attempt_id: str,
                      region: str, validator=validate):
    """Register the CPU gate in the existing app; inputs are already committed."""
    if any(not isinstance(value, str) or not re.fullmatch(r'[A-Za-z0-9_-]{1,64}', value)
           for value in (run_id, attempt_id)):
        raise ValueError('Bounded run identity required')
    if region != prepared.recipe['region'] or hashlib.sha256(canonical(prepared.recipe)).hexdigest() != prepared.recipe_sha256:
        raise ValueError('Reviewed image recipe/region differs')
    volume = input_volume.with_mount_options(sub_path='/' + attempt_id, read_only=True)
    recipe = prepared.recipe
    expected_recipe = prepared.recipe_sha256

    @app.function(name='coppertop-image-gate', image=prepared.image,
        cpu=(4, 4), memory=(16384, 16384), gpu=None, region=region,
        timeout=1200, startup_timeout=300, retries=0, secrets=[], block_network=True,
        min_containers=0, max_containers=1, buffer_containers=0, scaledown_window=2,
        single_use_containers=True, serialized=True, volumes={INPUT_MOUNT: volume})
    def gate(raw_manifest, image_id):
        manifest = StageManifest.from_dict(raw_manifest)
        if manifest.run_id != run_id or not isinstance(image_id, str) or not re.fullmatch(r'im-[A-Za-z0-9]+', image_id):
            raise ValueError('Mounted run and image identity required')
        identity = os.environ.get('MODAL_TASK_ID', '')
        if not re.fullmatch(r'ta-[A-Za-z0-9]+', identity):
            raise ValueError('Actual Modal container identity required')
        require_native_interpreter()
        root = Path('/app')
        if {path.name for path in (root/'coppertop').glob('*.py')} != {Path(name).name for name in PACKAGE_FILES}:
            raise ValueError('Image contains an unexpected project module')
        if _hash_files(root, recipe['files']) != recipe['files']:
            raise ValueError('Image code/recipe inputs differ')
        if hashlib.sha256(Path('/opt/gpu-smoke-pins.json').read_bytes()).hexdigest() != recipe['files']['containers/gpu-smoke-pins.json']:
            raise ValueError('Runtime pins differ from reviewed recipe')
        evaluator = recipe['evaluator']
        if _file_reference(Path('/opt/evaluator.py'), evaluator['size']) != ObjectRef(evaluator['sha256'], evaluator['size']):
            raise ValueError('Runtime evaluator bytes differ')
        source = Path(INPUT_MOUNT) / manifest.key
        verify_stage(manifest, source)
        require_dependencies(pinned_dependencies(root/'requirements-gpu-smoke.lock',
                                                root/'requirements-r2.txt'))
        # Verify the new entrypoints and the CPU transfer client's imports offline.
        import boto3, botocore, s3transfer
        from . import modal_staging, modal_reload, runpod_staging, reload_launch, r2_session
        runtime = validator(SimpleNamespace(root=root, store=source, model=Path('/opt/model'),
            pins=Path('/opt/gpu-smoke-pins.json'), evaluator=Path('/opt/evaluator.py'), image_id=image_id))
        required = ('snapshot_verified', 'imports_verified', 'processor_parity_verified', 'entrypoint_verified')
        if (runtime.get('status') != 'passed' or runtime.get('image_id') != image_id
            or runtime.get('recipe_sha256') != recipe['native_recipe_sha256']
            or any(runtime.get(name) is not True for name in required)
            or runtime.get('full_model_instantiated') is not False or runtime.get('cuda_executed') is not False):
            raise ValueError('CPU native image gate did not pass')
        result = {'schema_version': 1, 'status': 'passed', 'run_id': run_id,
            'release': manifest.release, 'attempt_id': attempt_id, 'container_id': identity, 'image_id': image_id,
            'recipe_sha256': expected_recipe, 'runtime_receipt': runtime}
        if len(canonical(result)) > 65536:
            raise ValueError('CPU image gate receipt exceeds bound')
        return result

    return gate
