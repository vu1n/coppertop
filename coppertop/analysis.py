"""Bounded, derived frame/window snapshots; canonical Chords objects stay read-only."""
from dataclasses import dataclass
from importlib.metadata import version
import io
import json
import platform
from pathlib import Path

import numpy as np
import pyarrow as pa
import pyarrow.parquet as pq

from .chords import canonical, digest, frame_id, object_path, window_indices
from .episode import EXPLORED_DEV_SEEDS, vector
from .official_recording import ACTION_LAYOUT, STATE_LAYOUT
from .streams import file_ref, read_small

MAX_ROWS = 10000
MAX_METADATA_BYTES = 8 * 1024**2
MAX_TABLE_BYTES = 64 * 1024**2
MAX_SNAPSHOT_BYTES = 256 * 1024**2
FRAME_SCHEMA = pa.schema([
    ('release_id', pa.string()), ('episode_ref', pa.string()),
    ('episode_id', pa.string()), ('initialization_group', pa.string()),
    ('split', pa.string()), ('data_kind', pa.string()),
    ('provenance_sha256', pa.string()), ('source_sha256', pa.string()),
    ('frame_id', pa.string()), ('frame_index', pa.int64()), ('time_ns', pa.int64()),
    ('state', pa.list_(pa.float64(), 16)), ('action', pa.list_(pa.float64())),
    ('observation_ref', pa.string()), ('camera_refs', pa.string()),
    ('success', pa.bool_()), ('terminated', pa.bool_()), ('truncated', pa.bool_()),
    ('episode_success', pa.bool_()), ('outcome_scope', pa.string()),
])


def _flag(value):
    if value is not None and type(value) is not bool:
        raise ValueError('Outcome flags must be boolean or unknown')
    return value


def _table_hash(table):
    sink = pa.BufferOutputStream()
    with pa.ipc.new_stream(sink, table.schema) as writer:
        writer.write_table(table.combine_chunks())
    return digest(sink.getvalue().to_pybytes())


def _bounded_batches(source, schema):
    batches, total, rows = [], 0, 0
    for batch in source:
        total += batch.get_total_buffer_size()
        rows += batch.num_rows
        if total > MAX_TABLE_BYTES or rows > MAX_ROWS:
            raise ValueError('Decoded analysis table byte bound exceeded')
        batches.append(batch)
    return pa.Table.from_batches(batches, schema=None if batches else schema)


def _bounded_table(dataset):
    return _bounded_batches(dataset.to_batches(batch_size=128), dataset.schema)


@dataclass(frozen=True)
class FrameData:
    table: pa.Table
    sources: tuple[dict, ...]

    def validate(self):
        if (self.table.schema != FRAME_SCHEMA or not 0 < self.table.num_rows <= MAX_ROWS
                or self.table.nbytes > MAX_TABLE_BYTES):
            raise ValueError('Bounded frame schema required')
        groups, sequences, identities = {}, {}, set()
        for row in self.table.to_pylist():
            for key in ('episode_id', 'initialization_group', 'frame_id', 'data_kind', 'outcome_scope'):
                if not isinstance(row[key], str) or not row[key]:
                    raise ValueError('Frame identity and evidence scope required')
            for key in ('episode_ref', 'provenance_sha256', 'source_sha256'):
                object_path(row[key])
            if row['release_id'] is not None:
                object_path(row['release_id'])
            if row['frame_id'] != frame_id(row['episode_id'], row['frame_index']):
                raise ValueError('Stable frame identity differs')
            if row['frame_id'] in identities:
                raise ValueError('Duplicate frame identity')
            identities.add(row['frame_id'])
            if row['split'] not in {'train', 'development', 'sealed_test'}:
                raise ValueError('Explicit dataset split required')
            group = row['initialization_group']
            if groups.setdefault(group, row['split']) != row['split']:
                raise ValueError('Initialization siblings cannot cross splits')
            sequence = sequences.setdefault(row['episode_id'], [])
            if sequence and any(row[key] != sequence[0][key] for key in (
                    'release_id', 'episode_ref', 'initialization_group', 'split', 'data_kind',
                    'provenance_sha256', 'source_sha256', 'episode_success', 'outcome_scope')):
                raise ValueError('Episode provenance changes within a sequence')
            if row['frame_index'] != len(sequence) or type(row['time_ns']) is not int or row['time_ns'] < 0:
                raise ValueError('Contiguous frames and nonnegative integer timestamps required')
            if sequence and row['time_ns'] <= sequence[-1]['time_ns']:
                raise ValueError('Frame timestamps must increase')
            if sequence and any(sequence[-1][key] is True for key in ('success', 'terminated', 'truncated')):
                raise ValueError('Frame sequence crosses an episode boundary')
            vector(row['state'], 16)
            if row['action'] is not None:
                vector(row['action'], 12)
            for key in ('success', 'terminated', 'truncated', 'episode_success'):
                _flag(row[key])
            sequence.append(row)
        if not self.sources or len(canonical({'sources': self.sources})) > MAX_METADATA_BYTES:
            raise ValueError('Bounded source provenance required')
        return self

    def windows(self, offsets=(-6, -4, -2, 0)):
        self.validate()
        if (not offsets or len(offsets) > 64 or tuple(sorted(set(offsets))) != tuple(offsets)
                or any(type(x) is not int or x > 0 for x in offsets) or offsets[-1] != 0):
            raise ValueError('Unique ordered past-only offsets ending at zero required')
        groups = {}
        for row in self.table.to_pylist():
            groups.setdefault(row['episode_id'], []).append(row)
        rows = []
        for episode in groups.values():
            for row in episode:
                indices, valid = window_indices(row['frame_index'], len(episode), tuple(offsets))
                features = np.asarray(row['state'], dtype=np.float32)
                if not np.isfinite(features).all():
                    raise ValueError('State view exceeds finite float32 feature range')
                rows.append(dict(row, history_frame_ids=[episode[i]['frame_id'] for i in indices],
                                 history_valid=list(valid), history_time_ns=[episode[i]['time_ns'] for i in indices],
                                 state_vector=features.tolist()))
        schema = pa.schema([*FRAME_SCHEMA, ('history_frame_ids', pa.list_(pa.string())),
                            ('history_valid', pa.list_(pa.bool_())), ('history_time_ns', pa.list_(pa.int64())),
                            ('state_vector', pa.list_(pa.float32(), 16))])
        result = pa.Table.from_pylist(rows, schema=schema)
        if result.nbytes > MAX_TABLE_BYTES:
            raise ValueError('Window table byte bound exceeded')
        return result


def from_release(store, release_id):
    """Read one immutable release via ObjectStore; never mutate or publish it."""
    metadata_bytes = 0
    def metadata(reference):
        nonlocal metadata_bytes
        raw = read_small(store, reference)
        metadata_bytes += len(raw)
        if metadata_bytes > MAX_METADATA_BYTES:
            raise ValueError('Release metadata byte bound exceeded')
        return json.loads(raw)
    manifest = metadata(release_id)
    if manifest.get('schema_version') != 1 or not 1 <= len(manifest['episodes']) <= 100:
        raise ValueError('Bounded Chords release required')
    overlays = [metadata(key) for key in manifest['overlays']]
    rows, signal_bytes, repeated_metadata_bytes = [], 0, 0
    for reference in manifest['episodes']:
        record = metadata(reference)
        if record['state_layout'] != STATE_LAYOUT or record['action_layout'] != ACTION_LAYOUT:
            raise ValueError('Native state16/action12 semantics required')
        generated = record.get('kind') == 'generated_official_rollout'
        source = pq.ParquetFile(io.BytesIO(read_small(store, record['signals'], 64 * 1024**2)))
        signal_bytes += sum(source.metadata.row_group(i).total_byte_size for i in range(source.num_row_groups))
        if (source.metadata.num_rows != record['length'] or not 0 < record['length'] <= MAX_ROWS - len(rows)
                or signal_bytes > MAX_TABLE_BYTES):
            raise ValueError('Signal decode/row bound exceeded')
        annotation = {}
        for item in overlays:
            if item.get('episode_id') == record['episode_id']:
                annotation.update(item)
        split = annotation.get('split', record['provenance'].get('split'))
        if split != 'development' and (record['provenance'].get('split') == 'development'
                                       or record['provenance'].get('simulator_seed') in EXPLORED_DEV_SEEDS):
            raise ValueError('Explored development data cannot become held-out data')
        result = record.get('official_result', {})
        scope = result.get('outcome_scope', 'unknown; no completed outcome supplied')
        # A completed bounded-prefix recorder can still have success=False.
        outcome = result.get('success') if result.get('status') == 'completed' and result.get('horizon') == 600 else None
        videos = {camera: value['object'] for camera, value in record['videos'].items()}
        real = record['provenance'].get('real_simulation') if generated else record['provenance'].get('real_data')
        common = dict(release_id=release_id, episode_ref=reference, episode_id=record['episode_id'],
                      initialization_group=record['initialization_group'], split=split,
                      data_kind=record.get('kind', 'imported_demonstration') if real is True else 'synthetic_fixture',
                      provenance_sha256=digest(canonical(record['provenance'])), source_sha256=record['signals'],
                      episode_success=_flag(outcome), outcome_scope=scope, camera_refs=canonical(videos).decode())
        repeated_metadata_bytes += len(canonical(common)) * record['length']
        if repeated_metadata_bytes > MAX_TABLE_BYTES:
            raise ValueError('Repeated episode metadata exceeds analysis table byte bound')
        for index, signal in enumerate(source.read().to_pylist()):
            if generated:
                if signal['step'] != index:
                    raise ValueError('Signal frame index differs')
                state, time_ns = signal['state'], signal['time_ns']
                flags = {key: _flag(signal[key]) for key in ('success', 'terminated', 'truncated')}
            else:
                if signal['frame_index'] != index:
                    raise ValueError('Signal frame index differs')
                state, time_ns = signal['observation.state'], round(signal['timestamp'] * 1e9)
                flags = {'success': None, 'terminated': _flag(signal.get('next.done')), 'truncated': None}
            rows.append(dict(common, frame_id=signal['frame_id'], frame_index=index, time_ns=time_ns,
                             state=state, action=signal['action'], observation_ref=signal.get('observation'), **flags))
        if outcome is not None and rows[-1]['success'] != outcome:
            raise ValueError('Completed outcome differs from final observation')
    return FrameData(pa.Table.from_pylist(rows, schema=FRAME_SCHEMA), ({'kind': 'chords_release', 'sha256': release_id},)).validate()


def write_snapshot(data, destination, *, offsets=(-6, -4, -2, 0)):
    """Create a fresh local derivative. No append, overwrite, cleanup, or source writes."""
    import lance
    data.validate()
    windows = data.windows(offsets)
    destination = Path(destination)
    destination.mkdir(parents=True, exist_ok=False)
    tables = {'frames': data.table, 'windows': windows}
    versions = {}
    for name, table in tables.items():
        dataset = lance.write_dataset(table, destination / (name + '.lance'), mode='create', data_storage_version='2.0')
        versions[name] = dataset.version
    # Lance's advisory latest-version hint changes on later commits. Readers use
    # the exact pinned manifest, never this hint, so it is outside snapshot identity.
    paths = [p for p in sorted(destination.rglob('*')) if p.is_file() and p.name != 'latest_version_hint.json']
    if sum(p.stat().st_size for p in paths) > MAX_SNAPSHOT_BYTES:
        raise ValueError('Physical snapshot byte bound exceeded')
    files = {p.relative_to(destination).as_posix(): {'sha256': file_ref(p).sha256, 'bytes': p.stat().st_size} for p in paths}
    definition = {'schema_version': 1, 'sources': data.sources, 'offsets': list(offsets),
                  'arrow_table_sha256': {name: _table_hash(table) for name, table in tables.items()},
                  'rows': len(data.table), 'state_layout': STATE_LAYOUT, 'action_layout': ACTION_LAYOUT,
                  'feature_view': 'anchor native state16 cast to float32; unscaled squared L2; no learned embedding',
                  'query_backend': 'datafusion_ffi_lance_table_provider',
                  'implementation_sha256': {name: digest(Path(__file__).with_name(name).read_bytes())
                                            for name in ('analysis.py', 'chords.py', 'episode.py', 'streams.py', 'official_recording.py')},
                  'python': platform.python_version(),
                  'packages': {name: version(name) for name in ('pylance', 'datafusion', 'pyarrow', 'numpy')}}
    manifest = dict(definition, content_id=digest(canonical(definition)), lance_versions=versions, files=files)
    raw = canonical(manifest)
    if len(raw) > MAX_METADATA_BYTES:
        raise ValueError('Snapshot manifest byte bound exceeded')
    (destination / 'snapshot.json').write_bytes(raw)
    return digest(raw)


class Snapshot:
    """Pinned local Lance readers and read-only native DataFusion provider queries."""
    def __init__(self, directory, expected_sha256):
        import lance
        self.directory = Path(directory).resolve(strict=True)
        path = self.directory / 'snapshot.json'
        if path.is_symlink() or path.stat().st_size > MAX_METADATA_BYTES:
            raise ValueError('Bounded snapshot manifest required')
        raw = path.read_bytes()
        if digest(raw) != expected_sha256:
            raise ValueError('Snapshot manifest hash differs')
        self.manifest = json.loads(raw)
        if self.manifest['schema_version'] != 1 or not 0 < self.manifest['rows'] <= MAX_ROWS:
            raise ValueError('Snapshot schema/row limit differs')
        if sum(ref['bytes'] for ref in self.manifest['files'].values()) > MAX_SNAPSHOT_BYTES:
            raise ValueError('Physical snapshot byte bound exceeded')
        for name, ref in self.manifest['files'].items():
            p = self.directory / name
            if not p.is_file() or p.is_symlink() or not p.resolve(strict=True).is_relative_to(self.directory):
                raise ValueError('Snapshot file escapes directory')
            if p.stat().st_size != ref['bytes'] or file_ref(p).sha256 != ref['sha256']:
                raise ValueError('Snapshot file bytes differ')
        self._datasets = {name: lance.dataset(self.directory / (name + '.lance'), version=self.manifest['lance_versions'][name])
                          for name in ('frames', 'windows')}
        for name, dataset in self._datasets.items():
            if dataset.count_rows() != self.manifest['rows'] or _table_hash(_bounded_table(dataset)) != self.manifest['arrow_table_sha256'][name]:
                raise ValueError('Pinned snapshot table differs')

    def table(self, name='frames'):
        return _bounded_table(self._datasets[name])

    def query(self, sql):
        from datafusion import SessionConfig, SessionContext, SQLOptions
        from lance import FFILanceTableProvider
        context = SessionContext(SessionConfig().with_target_partitions(1))
        for name, dataset in self._datasets.items():
            context.register_table(name, FFILanceTableProvider(dataset, with_row_id=False, with_row_addr=False))
        options = SQLOptions().with_allow_ddl(False).with_allow_dml(False).with_allow_statements(False)
        result = context.sql(sql, options=options).limit(MAX_ROWS)
        return _bounded_batches((batch.to_pyarrow() for batch in result.execute_stream()), result.schema())

    def nearest(self, state, *, k=5, filter=None):
        query = np.asarray(vector(state, 16), dtype=np.float32)
        if not np.isfinite(query).all() or type(k) is not int or not 1 <= k <= MAX_ROWS:
            raise ValueError('Finite state and bounded neighbor count required')
        return self._datasets['windows'].to_table(
            nearest={'column': 'state_vector', 'q': query, 'k': k, 'metric': 'l2', 'use_index': False},
            filter=filter, prefilter=True)
