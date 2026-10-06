"""Bounded transfer primitives. References, not payloads, cross job boundaries."""
import hashlib
from dataclasses import dataclass
from typing import Iterable

CHUNK_BYTES = 256 * 1024
SMALL_BYTES = 4 * 1024 * 1024


@dataclass(frozen=True)
class ObjectRef:
    sha256: str
    size: int

    def __post_init__(self):
        from .chords import object_path
        object_path(self.sha256)
        if type(self.size) is not int or self.size < 0:
            raise ValueError('Object size must be nonnegative')

    def to_dict(self):
        return {'sha256': self.sha256, 'size': self.size}


def checked(chunks: Iterable[bytes], ref: ObjectRef):
    """Integrity is established only after exhaustion; consumers must commit last."""
    total = 0
    hasher = hashlib.sha256()
    for chunk in chunks:
        if not isinstance(chunk, bytes) or len(chunk) > CHUNK_BYTES:
            raise ValueError('Stream chunk exceeds boundary')
        total += len(chunk)
        if total > ref.size:
            raise ValueError('Stream exceeds declared size')
        hasher.update(chunk)
        yield chunk
    if total != ref.size or hasher.hexdigest() != ref.sha256:
        raise ValueError('Stream size/checksum mismatch')


def verify(store, ref: ObjectRef):
    for _ in checked(store.iter_bytes(ref.sha256), ref):
        pass
    return ref


def read_small(store, key: str, limit: int = SMALL_BYTES) -> bytes:
    ref = store.stat(key)
    if ref.size > limit:
        raise ValueError('Object exceeds buffered-read bound')
    return b''.join(checked(store.iter_bytes(key), ref))


def file_ref(path):
    hasher = hashlib.sha256()
    size = 0
    with path.open('rb') as source:
        while chunk := source.read(CHUNK_BYTES):
            size += len(chunk)
            hasher.update(chunk)
    return ObjectRef(hasher.hexdigest(), size)


def file_chunks(path):
    with path.open('rb') as source:
        while chunk := source.read(CHUNK_BYTES):
            yield chunk
