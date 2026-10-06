"""Publish an immutable dependency closure; the selected release is the last object."""
import json
from .chords import ObjectStore, object_path
from .streams import read_small, verify

MAX_OBJECTS = 100_000
MAX_MANIFEST_BYTES = 4 * 1024 * 1024


def _json(store: ObjectStore, key: str) -> dict:
    data = read_small(store, key, MAX_MANIFEST_BYTES)
    if len(data) > MAX_MANIFEST_BYTES:
        raise ValueError('Oversized manifest')
    result = json.loads(data)
    if not isinstance(result, dict) or result.get('schema_version') != 1:
        raise ValueError('Unsupported manifest schema')
    return result


def release_objects(store: ObjectStore, release_key: str, *,
                    max_objects: int = MAX_OBJECTS, max_bytes: int | None = None) -> tuple[str, ...]:
    """Postorder, deduplicated closure; validates every byte before any publication."""
    ordered = []
    visited = {}
    total_bytes = 0
    pending = [(release_key, 'release', False)]
    while pending:
        key, kind, exiting = pending.pop()
        object_path(key)
        if exiting:
            ordered.append(key)
            continue
        if key in visited:
            if visited[key] != kind:
                raise ValueError('Object referenced with conflicting roles')
            continue
        if len(visited) >= max_objects:
            raise ValueError('Release object bound exceeded')
        ref = store.stat(key)
        total_bytes += ref.size
        if max_bytes is not None and total_bytes > max_bytes:
            raise ValueError('Release byte bound exceeded')
        visited[key] = kind
        if kind == 'blob':
            verify(store, ref)
            ordered.append(key)
            continue
        document = _json(store, key)
        pending.append((key, kind, True))
        if kind == 'release':
            episodes = document['episodes']
            overlays = document['overlays']
            if not isinstance(episodes, list) or not isinstance(overlays, list):
                raise ValueError('Invalid release references')
            children = [(ref, 'episode') for ref in episodes] + [(ref, 'blob') for ref in overlays]
            if document['parent']:
                children.append((document['parent'], 'release'))
        else:
            children = [(document['signals'], 'blob')]
            children += [(entry['object'], 'blob') for entry in document['raw'].values()]
            children += [(entry['object'], 'blob') for entry in document['videos'].values()]
        pending.extend((ref, child_kind, False) for ref, child_kind in reversed(children))
    # Content-addressed manifests cannot form cycles without a hash collision.
    if not ordered or ordered[-1] != release_key:
        raise ValueError('Invalid release dependency ordering')
    return tuple(ordered)


def publish_release(source: ObjectStore, target: ObjectStore, release_key: str) -> str:
    keys = release_objects(source, release_key)
    for key in keys:
        ref = source.stat(key)
        try:
            existing = target.stat(key)
        except FileNotFoundError:
            if target.put_stream(ref, source.iter_bytes(key)) != ref:
                raise ValueError('Target returned a different object identity')
        else:
            if existing != ref:
                raise ValueError('Destination size mismatch')
        verify(target, ref)
    return release_key
