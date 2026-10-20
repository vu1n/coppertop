"""Immutable staging contracts; provider commit barriers establish visibility."""
import json
import hashlib
import re
import time
from dataclasses import dataclass
from pathlib import Path
from types import MappingProxyType
from typing import Callable, Mapping
from .chords import ObjectStore, canonical, digest, object_path
from .streams import ObjectRef, CHUNK_BYTES, checked

MAX_OBJECTS = 64
MAX_STAGE_BYTES = 3 * 1024**3
MAX_MANIFEST_BYTES = 65536
READY_FILE = 'stage.json'


def safe_name(name: str) -> str:
    if not isinstance(name, str) or len(name) > 256 or not re.fullmatch(r'[A-Za-z0-9._/-]+', name):
        raise ValueError('Invalid staged object name')
    if name.startswith('/') or any(p in ('', '.', '..') for p in name.split('/')) or name.split('/')[0] in (READY_FILE, '.transfer-tmp'):
        raise ValueError('Staged path must be relative and distinct from the receipt')
    return name


@dataclass(frozen=True)
class StageManifest:
    run_id: str
    release: str
    objects: Mapping[str, ObjectRef]

    def __post_init__(self):
        object.__setattr__(self, 'objects', MappingProxyType(dict(self.objects)))
        self.validate()

    def validate(self):
        if not isinstance(self.run_id, str) or not re.fullmatch(r'[A-Za-z0-9_-]{1,64}', self.run_id):
            raise ValueError('Bounded run identity required')
        object_path(self.release)
        if not 0 < len(self.objects) <= MAX_OBJECTS:
            raise ValueError('Staged object count exceeds bound')
        for name, ref in self.objects.items():
            safe_name(name)
            if not isinstance(ref, ObjectRef) or not 0 < ref.size <= MAX_STAGE_BYTES:
                raise ValueError('Invalid staged object reference')
            if any(name.startswith(other + '/') for other in self.objects if other != name):
                raise ValueError('Staged file and directory paths overlap')
        if sum(ref.size for ref in self.objects.values()) > MAX_STAGE_BYTES:
            raise ValueError('Stage exceeds total byte bound')
        if len(canonical(self.to_dict())) > MAX_MANIFEST_BYTES:
            raise ValueError('Stage manifest exceeds metadata bound')

    def to_dict(self):
        return {'schema_version': 1, 'kind': 'cloud_stage_manifest', 'run_id': self.run_id,
                'release': self.release, 'objects': {name: ref.to_dict() for name, ref in self.objects.items()}}

    @classmethod
    def from_dict(cls, value: dict):
        if not isinstance(value, dict) or set(value) != {'schema_version','kind','run_id','release','objects'}:
            raise ValueError('Unexpected stage manifest fields')
        if value['schema_version'] != 1 or value['kind'] != 'cloud_stage_manifest' or not isinstance(value['objects'], dict):
            raise ValueError('Unsupported stage manifest')
        if len(value['objects']) > MAX_OBJECTS:
            raise ValueError('Stage object count exceeds bound')
        return cls(value['run_id'], value['release'], {name: ObjectRef(**ref) for name, ref in value['objects'].items()})

    @property
    def key(self):
        return digest(canonical(self.to_dict()))


def stage_receipt(manifest: StageManifest):
    return {'schema_version': 1, 'kind': 'cloud_stage', 'status': 'committed',
            'run_id': manifest.run_id, 'release': manifest.release, 'manifest_key': manifest.key}


def _path(directory: Path, name: str):
    safe_name(name)
    path = directory/name
    if directory.is_symlink():
        raise ValueError('Staging root cannot be a symlink')
    current = directory
    for part in Path(name).parts:
        current = current/part
        if current.is_symlink():
            raise ValueError('Staging paths cannot traverse symlinks')
    return path


def verify_stage(manifest: StageManifest, directory: Path):
    manifest.validate()
    marker = directory/READY_FILE
    if marker.is_symlink() or not marker.is_file() or marker.stat().st_size > MAX_MANIFEST_BYTES:
        raise ValueError('Bounded stage receipt required')
    with marker.open('rb') as source:
        data = source.read(MAX_MANIFEST_BYTES + 1)
    if len(data) > MAX_MANIFEST_BYTES or json.loads(data) != stage_receipt(manifest):
        raise ValueError('Stage receipt identity mismatch')
    for name, ref in manifest.objects.items():
        path = _path(directory, name)
        if not path.is_file() or path.stat().st_size != ref.size or _file_reference(path, ref.size) != ref:
            raise ValueError('Staged payload differs from immutable reference')


def stage_objects(store: ObjectStore, manifest: StageManifest, directory: Path, *,
                  max_seconds: int, commit: Callable[[], None]):
    manifest.validate()
    if type(max_seconds) is not int or not 0 < max_seconds <= 3600:
        raise ValueError('Bounded CPU transfer duration required')
    deadline = time.monotonic() + max_seconds
    def check_time():
        if time.monotonic() >= deadline:
            raise TimeoutError('CPU staging deadline reached')
    directory.mkdir(parents=True, exist_ok=False)
    temporary = directory/'.transfer-tmp'
    temporary.mkdir()
    for index, (name, ref) in enumerate(manifest.objects.items()):
        check_time()
        if store.stat(ref.sha256) != ref:
            raise ValueError('Source object length differs')
        path = _path(directory, name)
        path.parent.mkdir(parents=True, exist_ok=True)
        temp = temporary/str(index)
        with temp.open('xb') as target:
            for chunk in checked(store.iter_bytes(ref.sha256), ref):
                check_time()
                target.write(chunk)
        temp.replace(path)
    temporary.rmdir()
    receipt = stage_receipt(manifest)
    (directory/READY_FILE).write_bytes(canonical(receipt))
    verify_stage(manifest, directory)
    check_time()
    # The marker alone is not a commit acknowledgement. Consumers are gated on
    # this return value only after the provider's mounted-storage barrier passes.
    try:
        commit()
        check_time()
    except BaseException:
        (directory/READY_FILE).unlink(missing_ok=True)
        raise
    return receipt


def manifest_for_directory(directory: Path, run_id: str, release: str, names):
    refs = {}
    for name in names:
        path = _path(directory, name)
        if not path.is_file():
            raise ValueError('Expected staged file missing')
        refs[name] = _file_reference(path)
    return StageManifest(run_id, release, refs)


def commit_directory(manifest: StageManifest, directory: Path, commit: Callable[[], None]):
    """Commit generated files with the same boundary as CPU-fetched inputs."""
    manifest.validate()
    for name, ref in manifest.objects.items():
        if _file_reference(_path(directory, name), ref.size) != ref:
            raise ValueError('Generated payload reference differs')
    marker = directory/READY_FILE
    with marker.open('xb') as target:
        target.write(canonical(stage_receipt(manifest)))
    try:
        commit()
    except BaseException:
        marker.unlink(missing_ok=True)
        raise
    return stage_receipt(manifest)


class DeadlineStore:
    """CPU transfer deadline between chunks; injected client's I/O must also time out."""
    def __init__(self, store: ObjectStore, max_seconds: int):
        if type(max_seconds) is not int or not 0 < max_seconds <= 3600:
            raise ValueError('Bounded CPU transfer duration required')
        self.store = store
        self.deadline = time.monotonic() + max_seconds

    def check(self):
        if time.monotonic() >= self.deadline:
            raise TimeoutError('CPU transfer deadline reached')

    def stat(self, key):
        self.check()
        result = self.store.stat(key)
        self.check()
        return result

    def iter_bytes(self, key, **kwargs):
        self.check()
        for chunk in self.store.iter_bytes(key, **kwargs):
            self.check()
            yield chunk

    def put(self, data):
        self.check()
        result = self.store.put(data)
        self.check()
        return result

    def put_stream(self, ref, chunks):
        def bounded():
            for chunk in chunks:
                self.check()
                yield chunk
        self.check()
        result = self.store.put_stream(ref, bounded())
        self.check()
        return result


def _file_reference(path: Path, expected_size=None):
    if path.is_symlink() or not path.is_file():
        raise ValueError('Regular staged file required')
    size = path.stat().st_size
    if not 0 < size <= MAX_STAGE_BYTES or (expected_size is not None and size != expected_size):
        raise ValueError('Staged file size differs or exceeds bound')
    deadline = time.monotonic() + 300
    hasher = hashlib.sha256()
    remaining = size
    with path.open('rb') as source:
        while remaining:
            if time.monotonic() >= deadline:
                raise TimeoutError('Staged file hashing deadline reached')
            chunk = source.read(min(CHUNK_BYTES, remaining))
            if not chunk:
                raise ValueError('Staged file truncated during hashing')
            hasher.update(chunk)
            remaining -= len(chunk)
        if source.read(1):
            raise ValueError('Staged file grew during hashing')
    return ObjectRef(hasher.hexdigest(), size)
