"""Import only the bounded, pinned human episode; never copy experiment artifacts."""
import io
import json
from pathlib import Path
import pyarrow as pa
import pyarrow.parquet as pq
from .adapter import load_official
from .chords import ObjectStore, canonical, digest, frame_id, release

LICENSE = 'CC-BY-4.0'


def ingest(source: Path, store: ObjectStore) -> str:
    episode = load_official(source)
    acquisition = json.loads((source / 'sources/official-files.json').read_text())
    raw = {}
    for entry in acquisition['files']:
        data = (source / entry['path']).read_bytes()
        if len(data) != entry['bytes'] or digest(data) != entry['sha256']:
            raise ValueError('Acquisition manifest mismatch')
        raw[entry['path']] = {**entry, 'object': store.put(data), 'license': LICENSE}
    table = pq.read_table(io.BytesIO(store.get(raw['official-episode/data/chunk-000/episode_000000.parquet']['object'])))
    # Canonical actions are native control commands, never normalized model targets.
    action = pa.array(episode.executed_native, type=table.schema.field('action').type)
    table = table.set_column(table.schema.get_field_index('action'), 'action', action)
    frame_ids = [frame_id(episode.episode_id, i) for i in range(len(table))]
    table = table.append_column('frame_id', pa.array(frame_ids))
    buffer = io.BytesIO()
    pq.write_table(table, buffer, compression='zstd')
    record = {
        'schema_version': 1, 'episode_id': episode.episode_id,
        'initialization_group': episode.initialization_group,
        'instruction': episode.instruction, 'length': len(table),
        'signals': store.put(buffer.getvalue()), 'raw': raw,
        'provenance': episode.provenance,
        'action_layout': 'EEtranslation3/EErotation3/gripper1/base4/mode1',
        'state_layout': 'base_position3/base_quaternion_xyzw4/EE_relative_position3/EE_relative_quaternion_xyzw4/gripper2',
        'coordinate_conventions': {'state': 'Source RoboCasa base and relative EE pose; quaternion xyzw',
                                  'action': 'Source controller commands, not absolute target poses; source controller metadata retained',
                                  'units': 'Source conventions; no inferred rescaling',
                                  'clock': 'episode-relative seconds; 20Hz camera/state/action index alignment'},
        'videos': {name: {**video, 'object': raw[video['path']]['object']} for name, video in episode.videos.items()},
    }
    annotation = store.put(canonical({'schema_version': 1, 'episode_id': episode.episode_id,
                                     'split': episode.split, 'quality': episode.quality}))
    return release(store, [store.put(canonical(record))], overlays=(annotation,))
