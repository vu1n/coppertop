"""Controller-only durable handoff through the existing ObjectStore boundary."""
import json
from pathlib import Path
from .chords import canonical
from .gpu_smoke import RELEASE, MODEL_REPO, MODEL_REVISION
from .smoke_runtime import MAX_DELTA_BYTES, MAX_VALIDATION_BYTES
from .streams import ObjectRef, checked, file_ref, file_chunks, read_small, verify

CASE_BYTES = MAX_VALIDATION_BYTES
REPORT_BYTES = 65536


def publish_smoke_checkpoint(store, directory: Path, result: dict, manifest: dict, run_id: str):
    """Commit the receipt last, after read-back verification of every object."""
    if not run_id or result.get('status') != 'passed' or result.get('release') != RELEASE:
        raise ValueError('Passed pinned runtime result required')
    if (manifest.get('release'), manifest.get('base_model'), manifest.get('revision')) != (RELEASE, MODEL_REPO, MODEL_REVISION):
        raise ValueError('Native head manifest identity mismatch')
    refs = {}
    for name, cap, field in (
        ('head.safetensors', MAX_DELTA_BYTES, 'checkpoint'),
        ('validation.safetensors', CASE_BYTES, 'validation'),
        ('head-manifest.json', REPORT_BYTES, None),
        ('result.json', REPORT_BYTES, None),
    ):
        path = directory/name
        if path.is_symlink() or not path.is_file() or not 0 < path.stat().st_size <= cap:
            raise ValueError('Artifact path/size exceeds handoff bound')
        ref = file_ref(path)
        if field:
            if any(value.get(field+'_sha256') != ref.sha256 or value.get(field+'_bytes') != ref.size for value in (result, manifest)):
                raise ValueError('Artifact differs from runtime receipt')
        refs[name] = ref
    # Validate all local inputs before writing even the first remote object.
    for name, ref in refs.items():
        if store.put_stream(ref, file_chunks(directory/name)) != ref:
            raise ValueError('Durable object identity mismatch')
        verify(store, ref)
    receipt = {'schema_version': 1, 'kind': 'native_head_weights', 'run_id': run_id,
        'release': RELEASE, 'base_model': MODEL_REPO, 'revision': MODEL_REVISION,
        'scope': 'head_weights_only', 'optimizer_state': False, 'rng_state': False,
        'artifacts': {name: ref.to_dict() for name, ref in refs.items()}}
    raw = canonical(receipt)
    key = store.put(raw)
    if read_small(store, key, REPORT_BYTES) != raw:
        raise ValueError('Durable receipt read-back mismatch')
    return key


def fetch_smoke_checkpoint(store, key: str, directory: Path, run_id: str):
    receipt = json.loads(read_small(store, key, REPORT_BYTES))
    expected = {'schema_version': 1, 'kind': 'native_head_weights', 'run_id': run_id,
        'release': RELEASE, 'base_model': MODEL_REPO, 'revision': MODEL_REVISION,
        'scope': 'head_weights_only', 'optimizer_state': False, 'rng_state': False}
    if any(receipt.get(k) != v for k, v in expected.items()):
        raise ValueError('Durable receipt identity/scope mismatch')
    names = {'head.safetensors': MAX_DELTA_BYTES, 'validation.safetensors': CASE_BYTES,
             'head-manifest.json': REPORT_BYTES, 'result.json': REPORT_BYTES}
    if set(receipt['artifacts']) != set(names):
        raise ValueError('Durable receipt artifact set differs')
    refs = {name: ObjectRef(**receipt['artifacts'][name]) for name in names}
    if any(not 0 < ref.size <= names[name] for name, ref in refs.items()):
        raise ValueError('Durable artifact exceeds fetch cap')
    directory.mkdir(parents=True, exist_ok=False)
    for name, ref in refs.items():
        if store.stat(ref.sha256) != ref:
            raise ValueError('Durable object length mismatch')
        # Only the atomically renamed file is a checksum-verified input.
        temp = directory/(name+'.partial')
        with temp.open('xb') as target:
            for chunk in checked(store.iter_bytes(ref.sha256), ref):
                target.write(chunk)
        temp.replace(directory/name)
    return receipt
