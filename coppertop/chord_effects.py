"""Observed interval effects are retrospective sidecars, never actor inputs.

An interval between observations i and j describes outgoing controls [i, j).
Labels describe evidence, not verified geometry or counterfactual causation.
"""
import json

import pyarrow as pa

from .analysis import MAX_METADATA_BYTES, MAX_ROWS, MAX_TABLE_BYTES, FrameData
from .chords import canonical, digest, object_path
from .streams import read_small

EVIDENCE_KINDS = frozenset(('human_observation', 'telemetry_rule', 'synthetic_fixture'))
EFFECT_SCHEMA = pa.schema([
    ('annotation_id', pa.string()), ('episode_id', pa.string()),
    ('episode_ref', pa.string()), ('source_sha256', pa.string()),
    ('initialization_group', pa.string()), ('split', pa.string()), ('data_kind', pa.string()),
    ('start', pa.int64()), ('end', pa.int64()), ('available_at', pa.int64()),
    ('start_frame_id', pa.string()), ('end_frame_id', pa.string()),
    ('available_frame_id', pa.string()),
    ('start_time_ns', pa.int64()), ('end_time_ns', pa.int64()), ('available_time_ns', pa.int64()),
    ('entity', pa.string()), ('predicate', pa.string()),
    ('before', pa.string()), ('after', pa.string()),
    ('evidence_kind', pa.string()), ('provenance', pa.string()),
])


def _episodes(data):
    if not isinstance(data, FrameData):
        raise ValueError('Validated native FrameData required')
    data.validate()
    episodes = {}
    for row in data.table.to_pylist():
        episodes.setdefault(row['episode_id'], []).append(row)
    return episodes


def _text(value, *, unknown=False):
    if unknown and value is None:
        return
    if not isinstance(value, str) or not value.strip() or len(value.encode()) > 256:
        raise ValueError('Nonempty bounded effect text required')


def _annotation(episodes, *, episode_id, start, end, entity, predicate, before, after,
                evidence_kind, provenance, available_at=None):
    if not isinstance(episode_id, str) or episode_id not in episodes:
        raise ValueError('Effect episode is absent from native frames')
    rows = episodes[episode_id]
    available_at = end if available_at is None else available_at
    if (any(type(x) is not int for x in (start, end, available_at))
            or not 0 <= start < end <= available_at < len(rows)):
        raise ValueError('Observed interval and availability at or after its end required')
    if any(row['action'] is None for row in rows[start:end]):
        raise ValueError('Interval must have every outgoing native control')
    for value in (entity, predicate):
        _text(value)
    for value in (before, after):
        _text(value, unknown=True)
    if evidence_kind not in EVIDENCE_KINDS:
        raise ValueError('Explicit observed effect evidence kind required')
    object_path(provenance)
    anchor = rows[start]
    return {
        'kind': 'interval_effect', 'schema_version': 1,
        **{key: anchor[key] for key in ('episode_id', 'episode_ref', 'source_sha256',
                                       'initialization_group', 'split', 'data_kind')},
        'start': start, 'end': end, 'available_at': available_at,
        'start_frame_id': anchor['frame_id'], 'end_frame_id': rows[end]['frame_id'],
        'available_frame_id': rows[available_at]['frame_id'],
        'start_time_ns': anchor['time_ns'], 'end_time_ns': rows[end]['time_ns'],
        'available_time_ns': rows[available_at]['time_ns'],
        'entity': entity, 'predicate': predicate, 'before': before, 'after': after,
        'evidence_kind': evidence_kind, 'provenance': provenance,
    }


def effect_annotation(data, *, episode_id, start, end, entity, predicate, before, after,
                      evidence_kind, provenance, available_at=None):
    """Create a versioned label bound to existing observations; no source writes.

    provenance identifies the annotator/rubric or deterministic rule artifact.
    It is an identity, not a claim that an external annotator was correct.
    """
    return _annotation(_episodes(data), episode_id=episode_id, start=start, end=end,
                       entity=entity, predicate=predicate, before=before, after=after,
                       evidence_kind=evidence_kind, provenance=provenance, available_at=available_at)


def effect_table(data, overlays, *, available_at=None):
    """Build a separate bounded, deterministic effect table, retaining disagreement.

    available_at is an observation-index cutoff, applied within each episode.
    It never changes what FrameData.windows exports to a model.
    """
    episodes = _episodes(data)
    if (not isinstance(overlays, (list, tuple)) or len(overlays) > MAX_ROWS
            or available_at is not None and (type(available_at) is not int or available_at < 0)):
        raise ValueError('Bounded effect list and nonnegative availability cutoff required')
    rows, seen, total = [], set(), 0
    for overlay in overlays:
        if not isinstance(overlay, dict):
            raise ValueError('Versioned effect object required')
        raw = canonical(overlay)
        total += len(raw)
        if total > MAX_METADATA_BYTES:
            raise ValueError('Effect metadata byte bound exceeded')
        if overlay.get('kind') != 'interval_effect' or type(overlay.get('schema_version')) is not int or overlay['schema_version'] != 1:
            raise ValueError('Unsupported effect kind/version')
        try:
            expected = _annotation(episodes, **{key: overlay[key] for key in (
                'episode_id', 'start', 'end', 'entity', 'predicate', 'before', 'after',
                'evidence_kind', 'provenance', 'available_at')})
        except KeyError as error:
            raise ValueError('Incomplete effect binding') from error
        if raw != canonical(expected):
            raise ValueError('Effect source/frame binding differs from native data')
        identity = digest(raw)
        if identity in seen:
            raise ValueError('Duplicate effect annotation')
        seen.add(identity)
        if available_at is None or overlay['available_at'] <= available_at:
            rows.append(dict(overlay, annotation_id=identity))
    table = pa.Table.from_pylist(sorted(rows, key=lambda row: row['annotation_id']), schema=EFFECT_SCHEMA)
    if table.nbytes > MAX_TABLE_BYTES:
        raise ValueError('Effect table byte bound exceeded')
    return table


def from_effect_release(store, release_id):
    """Read typed effect sidecars alongside one bounded native Chords release."""
    from .analysis import from_release
    raw = read_small(store, release_id, limit=MAX_METADATA_BYTES)
    manifest = json.loads(raw)
    data = from_release(store, release_id)
    overlays, total = [], len(raw)
    for key in manifest['overlays']:
        item = read_small(store, key, limit=MAX_METADATA_BYTES)
        total += len(item)
        if total > MAX_METADATA_BYTES:
            raise ValueError('Effect release metadata byte bound exceeded')
        value = json.loads(item)
        if value.get('kind') == 'interval_effect':
            overlays.append(value)
    return data, effect_table(data, overlays)
