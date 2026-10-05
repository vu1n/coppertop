"""Immutable objects and release manifests; storage is independent of compute."""
import hashlib
import json
import re
from pathlib import Path
from typing import Protocol


def canonical(value: dict) -> bytes:
    return (json.dumps(value, sort_keys=True, separators=(',', ':'), allow_nan=False) + '\n').encode()


def digest(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def frame_id(episode_id: str, frame: int) -> str:
    if not episode_id or type(frame) is not int or frame < 0:
        raise ValueError('Invalid frame identity')
    return digest(f'{episode_id}/frame/{frame}'.encode())


class ObjectStore(Protocol):
    def put(self, data: bytes) -> str: ...
    def get(self, key: str) -> bytes: ...


class LocalStore:
    def __init__(self, root: Path):
        self.root = Path(root)

    def path(self, key: str) -> Path:
        if not re.fullmatch(r'[0-9a-f]{64}', key):
            raise ValueError('Object key must be a SHA256 digest')
        path = self.root / 'objects' / key[:2] / key
        if not path.resolve().is_relative_to(self.root.resolve()):
            raise ValueError('Object path escapes store')
        return path

    def put(self, data: bytes) -> str:
        key = digest(data)
        path = self.path(key)
        path.parent.mkdir(parents=True, exist_ok=True)
        try:
            with path.open('xb') as out:
                out.write(data)
        except FileExistsError:
            if self.get(key) != data:
                raise ValueError('Existing object corrupted')
        return key

    def get(self, key: str) -> bytes:
        data = self.path(key).read_bytes()
        if digest(data) != key:
            raise ValueError('Object checksum mismatch')
        return data


def release(store: ObjectStore, episodes: list[str], *, parent: str | None = None,
            overlays: tuple[str, ...] = ()) -> str:
    if len(set(episodes)) != len(episodes):
        raise ValueError('Duplicate episode reference')
    for ref in [*episodes, *overlays, *([parent] if parent else [])]:
        store.get(ref)
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
