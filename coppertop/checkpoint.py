"""Immutable opaque checkpoints; only the worker interprets the payload bytes."""
import json
from .chords import ObjectStore, canonical, digest, object_path
from .publication import release_objects
from .streams import ObjectRef, CHUNK_BYTES, SMALL_BYTES, read_small, verify
from typing import Iterable


def save_checkpoint(store: ObjectStore, release: str, run_id: str, step: int,
                    payload: bytes, *, previous: str | None = None,
                    release_store: ObjectStore | None = None) -> str:
    if len(payload) > SMALL_BYTES:
        raise ValueError('Use save_checkpoint_stream for large checkpoints')
    ref = ObjectRef(digest(payload), len(payload))
    return save_checkpoint_stream(store, release, run_id, step, ref,
        (payload[i:i+CHUNK_BYTES] for i in range(0, len(payload), CHUNK_BYTES)),
        previous=previous, release_store=release_store)


def save_checkpoint_stream(store: ObjectStore, release: str, run_id: str, step: int,
                           payload: ObjectRef, chunks: Iterable[bytes], *, previous: str | None = None,
                           release_store: ObjectStore | None = None) -> str:
    if not run_id or type(step) is not int or step < 0:
        raise ValueError('Checkpoint requires a run identity and nonnegative step')
    release_objects(release_store if release_store is not None else store, release)
    if previous is not None:
        prior, _ = checkpoint_reference(store, previous, release, run_id, release_store=release_store)
        if step <= prior['step']:
            raise ValueError('Checkpoint step must advance')
    if store.put_stream(payload, chunks) != payload:
        raise ValueError('Checkpoint payload identity mismatch')
    verify(store, payload)
    payload_key = payload.sha256
    manifest = canonical({'schema_version': 1, 'kind': 'checkpoint',
                               'release': release, 'run_id': run_id, 'step': step,
                               'payload': payload_key, 'payload_ref': payload.to_dict(), 'previous': previous})
    if len(manifest) > 4096:
        raise ValueError('Oversized checkpoint manifest')
    return store.put(manifest)


def resume_checkpoint(store: ObjectStore, key: str, release: str, run_id: str, *,
                      release_store: ObjectStore | None = None) -> tuple[dict, bytes]:
    manifest, ref = checkpoint_reference(store, key, release, run_id,
        release_store=release_store, max_payload_bytes=SMALL_BYTES)
    return manifest, read_small(store, ref.sha256)


def checkpoint_reference(store: ObjectStore, key: str, release: str, run_id: str, *,
                         release_store: ObjectStore | None = None,
                         max_payload_bytes: int | None = None) -> tuple[dict, ObjectRef]:
    data = read_small(store, key, 4096)
    if len(data) > 4096:
        raise ValueError('Oversized checkpoint manifest')
    manifest = json.loads(data)
    if not isinstance(manifest, dict) or manifest.get('schema_version') != 1 or manifest.get('kind') != 'checkpoint':
        raise ValueError('Unsupported checkpoint manifest')
    if manifest.get('release') != release or manifest.get('run_id') != run_id:
        raise ValueError('Checkpoint belongs to a different release or run')
    if type(manifest.get('step')) is not int or manifest['step'] < 0:
        raise ValueError('Invalid checkpoint step')
    object_path(manifest['payload'])
    if manifest.get('previous') is not None:
        object_path(manifest['previous'])
    release_objects(release_store if release_store is not None else store, release)
    ref = ObjectRef(**manifest['payload_ref']) if 'payload_ref' in manifest else store.stat(manifest['payload'])
    if ref.sha256 != manifest['payload']:
        raise ValueError('Checkpoint payload reference mismatch')
    if max_payload_bytes is not None and ref.size > max_payload_bytes:
        raise ValueError('Checkpoint payload exceeds buffered resume bound')
    verify(store, ref)
    return manifest, ref
