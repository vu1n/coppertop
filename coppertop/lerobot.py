"""Export a single Chords episode to LeRobot v2.1, pinned to loader 0.3.3."""
import io
import json
import os
import tempfile
from pathlib import Path
import numpy as np
import pyarrow as pa
import pyarrow.parquet as pq
from .chords import ObjectStore, canonical
from .streams import checked, file_ref
from .episode import CAMERAS, reorder_action

LOADER_VERSION = '0.3.3'


def _stats(array):
    array = np.asarray(array)
    return {name: value.tolist() for name, value in {
        'min': array.min(axis=0), 'max': array.max(axis=0),
        'mean': array.mean(axis=0), 'std': array.std(axis=0),
        'count': np.array([len(array)]),
    }.items()}


def export(store: ObjectStore, release_key: str, destination: Path) -> None:
    manifest = json.loads(store.get(release_key))
    if len(manifest['episodes']) != 1:
        raise ValueError('Milestone exporter requires one episode')
    record = json.loads(store.get(manifest['episodes'][0]))
    if set(record['videos']) != set(CAMERAS):
        raise ValueError('Unsupported camera names')
    parquet = pq.ParquetFile(io.BytesIO(store.get(record['signals'])))
    metadata = parquet.metadata
    if not 0 < metadata.num_rows <= 10000 or sum(
        metadata.row_group(i).total_byte_size for i in range(metadata.num_row_groups)
    ) > 64 * 1024 * 1024:
        raise ValueError('Signal object exceeds milestone decode bounds')
    table = parquet.read()
    actions = [reorder_action(a, 'native', 'lerobot') for a in table['action'].to_pylist()]
    table = table.set_column(table.schema.get_field_index('action'), 'action',
                             pa.array(actions, type=table.schema.field('action').type))
    table = table.drop(['frame_id'])
    info = json.loads(store.get(record['raw']['official-episode/meta/info.json']['object']))
    n = len(table)
    if not 0 < n <= 10000:
        raise ValueError('Episode row bound exceeded')
    info['data_path'] = 'data/chunk-{episode_chunk:03d}/episode_{episode_index:06d}.parquet'
    info['video_path'] = 'videos/chunk-{episode_chunk:03d}/{video_key}/episode_{episode_index:06d}.mp4'
    info.update(total_episodes=1, total_frames=n, total_videos=3, total_tasks=1,
                total_chunks=1, splits={'train': '0:1'})
    # LeRobot's train range is structural. The sidecar retains development-only eligibility.
    destination = Path(destination)
    destination.mkdir(parents=True, exist_ok=False)
    (destination / 'meta').mkdir()
    for name in ('modality.json', 'embodiment.json', 'tasks.jsonl'):
        key = record['raw'][f'official-episode/meta/{name}']['object']
        (destination / 'meta' / name).write_bytes(store.get(key))
    (destination / 'meta/info.json').write_bytes(canonical(info))
    (destination / 'meta/episodes.jsonl').write_bytes(canonical({
        'episode_index': 0, 'tasks': [record['instruction']], 'length': n}))
    data_path = destination / 'data/chunk-000/episode_000000.parquet'
    data_path.parent.mkdir(parents=True)
    pq.write_table(table, data_path, compression='zstd')
    stats = {}
    for name, feature in info['features'].items():
        if feature['dtype'] != 'video':
            values = np.asarray(table[name].to_pylist())
            if values.ndim == 1:
                values = values[:, None]
            stats[name] = _stats(values)
    import av
    for name, video in record['videos'].items():
        video_key = 'observation.images.' + name
        target = destination / f'videos/chunk-000/{video_key}/episode_000000.mp4'
        target.parent.mkdir(parents=True)
        with tempfile.NamedTemporaryFile(dir=target.parent, delete=False) as output:
            staging = Path(output.name)
            try:
                for chunk in checked(store.iter_bytes(video['object']), store.stat(video['object'])):
                    output.write(chunk)
                output.flush()
                os.fsync(output.fileno())
                os.link(staging, target)
            finally:
                staging.unlink(missing_ok=True)
        count = 0
        total = np.zeros(3)
        squares = np.zeros(3)
        minimum = np.ones(3)
        maximum = np.zeros(3)
        with av.open(str(target)) as container:
            stream = container.streams.video[0]
            if (stream.width, stream.height) != (256, 256):
                raise ValueError('Unsupported encoded video dimensions')
            for frame in container.decode(video=0):
                if count >= n or (frame.width, frame.height) != (256, 256):
                    raise ValueError('Video exceeds the pinned frame/dimension bounds')
                pixels = frame.to_ndarray(format='rgb24').astype(np.float64) / 255
                total += pixels.sum(axis=(0, 1))
                squares += np.square(pixels).sum(axis=(0, 1))
                minimum = np.minimum(minimum, pixels.min(axis=(0, 1)))
                maximum = np.maximum(maximum, pixels.max(axis=(0, 1)))
                count += 1
        if count != n:
            raise ValueError('Video frame count differs from signals')
        mean = total / (n * 256 * 256)
        std = np.sqrt(np.maximum(0, squares / (n * 256 * 256) - mean ** 2))
        stats[video_key] = {name: value[:, None, None].tolist() for name, value in {
            'min': minimum, 'max': maximum, 'mean': mean, 'std': std,
        }.items()}
        stats[video_key]['count'] = [n]
    (destination / 'meta/episodes_stats.jsonl').write_bytes(canonical({'episode_index': 0, 'stats': stats}))
    sidecars = destination / 'sidecars'
    sidecars.mkdir()
    (sidecars / 'episode.json').write_bytes(canonical(record))
    (sidecars / 'release.json').write_bytes(store.get(release_key))
    for ref in manifest['overlays']:
        (sidecars / f'annotation-{ref}.json').write_bytes(store.get(ref))
    outputs = {str(p.relative_to(destination)): file_ref(p).sha256
               for p in sorted(destination.rglob('*')) if p.is_file()}
    (sidecars / 'export.json').write_bytes(canonical({'loader_version': LOADER_VERSION,
        'format': 'v2.1', 'release': release_key, 'files': outputs}))
