"""Immutable objects and release manifests; storage is independent of compute."""
import hashlib
import json
import os
import re
import tempfile
from pathlib import Path
from typing import Protocol, Iterable
from .streams import ObjectRef, CHUNK_BYTES, SMALL_BYTES, checked, read_small, verify


def canonical(value: dict) -> bytes:
    return (json.dumps(value, sort_keys=True, separators=(',', ':'), allow_nan=False) + '\n').encode()


def digest(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def frame_id(episode_id: str, frame: int) -> str:
    if not episode_id or type(frame) is not int or frame < 0:
        raise ValueError('Invalid frame identity')
    return digest(f'{episode_id}/frame/{frame}'.encode())


def object_path(key: str) -> str:
    if not isinstance(key, str) or not re.fullmatch(r'[0-9a-f]{64}', key):
        raise ValueError('Object key must be a SHA256 digest')
    return f'objects/{key[:2]}/{key}'


class ObjectStore(Protocol):
    def put(self, data: bytes) -> str: ...
    def get(self, key: str) -> bytes: ...
    def stat(self, key: str) -> ObjectRef: ...
    def iter_bytes(self, key: str, *, offset: int = 0, length: int | None = None) -> Iterable[bytes]: ...
    def put_stream(self, ref: ObjectRef, chunks: Iterable[bytes]) -> ObjectRef: ...


class LocalStore:
    def __init__(self, root: Path):
        self.root = Path(root)

    def path(self, key: str) -> Path:
        path = self.root / object_path(key)
        if not path.resolve().is_relative_to(self.root.resolve()):
            raise ValueError('Object path escapes store')
        return path

    def put(self, data: bytes) -> str:
        if len(data) > SMALL_BYTES:
            raise ValueError('Use put_stream for large objects')
        return self.put_stream(ObjectRef(digest(data), len(data)), (data[i:i+CHUNK_BYTES] for i in range(0, len(data), CHUNK_BYTES))).sha256

    def put_stream(self, ref: ObjectRef, chunks: Iterable[bytes]) -> ObjectRef:
        key = ref.sha256
        path = self.path(key)
        path.parent.mkdir(parents=True, exist_ok=True)
        # Link a fully flushed staging file atomically; readers never see partial bytes.
        with tempfile.NamedTemporaryFile(dir=path.parent, prefix='.staging-', delete=False) as out:
            staging = Path(out.name)
            try:
                for chunk in checked(chunks, ref):
                    out.write(chunk)
                out.flush()
                os.fsync(out.fileno())
                try:
                    os.link(staging, path)
                except FileExistsError:
                    if self.stat(key) != ref:
                        raise ValueError('Existing object size mismatch')
                    verify(self, ref)
            finally:
                staging.unlink(missing_ok=True)
        return ref

    def get(self, key: str) -> bytes:
        return read_small(self, key)

    def stat(self, key: str) -> ObjectRef:
        return ObjectRef(key, self.path(key).stat().st_size)

    def iter_bytes(self, key: str, *, offset: int = 0, length: int | None = None):
        size = self.stat(key).size
        if type(offset) is not int or offset < 0 or offset > size or (length is not None and (type(length) is not int or length < 0 or offset + length > size)):
            raise ValueError('Invalid object range')
        remaining = size - offset if length is None else length
        with self.path(key).open('rb') as source:
            source.seek(offset)
            while remaining:
                chunk = source.read(min(CHUNK_BYTES, remaining))
                if not chunk:
                    raise ValueError('Truncated object stream')
                remaining -= len(chunk)
                yield chunk


def release(store: ObjectStore, episodes: list[str], *, parent: str | None = None,
            overlays: tuple[str, ...] = ()) -> str:
    if len(set(episodes)) != len(episodes):
        raise ValueError('Duplicate episode reference')
    for ref in [*episodes, *overlays, *([parent] if parent else [])]:
        verify(store, store.stat(ref))
    return store.put(canonical({'schema_version': 1, 'episodes': episodes,
                                'parent': parent, 'overlays': list(overlays)}))


def fork(store: ObjectStore, base: str, *, overlays: tuple[str, ...] = ()) -> str:
    manifest = json.loads(store.get(base))
    return release(store, manifest['episodes'], parent=base,
                   overlays=tuple(manifest['overlays']) + overlays)


def window_indices(frame: int, length: int, offsets: tuple[int, ...]):
    if not 0 <= frame < length:
        raise ValueError('Frame outside episode')
    indices = tuple(min(length - 1, max(0, frame + o)) for o in offsets)
    valid = tuple(0 <= frame + o < length for o in offsets)
    return indices, valid
