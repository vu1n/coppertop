"""Pinned LeRobot episode import. No inferred seed, success, or native proposals."""
import copy
import hashlib
import json
from pathlib import Path
from .episode import CAMERAS, Episode, initialization_group, reorder_action

LOGICAL_DATASET = 'pretrain/atomic/CloseBlenderLid/20250822/lerobot'
STATE_RANGES = {'base_position': (0,3), 'base_rotation': (3,7),
    'end_effector_position_relative': (7,10), 'end_effector_rotation_relative': (10,14), 'gripper_qpos': (14,16)}
ACTION_RANGES = {'base_motion': (0,4), 'control_mode': (4,5),
    'end_effector_position': (5,8), 'end_effector_rotation': (8,11), 'gripper_close': (11,12)}

def verify_modality(modality):
    for kind, ranges, key in [('state', STATE_RANGES, 'observation.state'), ('action', ACTION_RANGES, 'action')]:
        for name, bounds in ranges.items():
            item = modality[kind][name]
            if (item['start'], item['end']) != bounds or item['original_key'] != key:
                raise ValueError('Unsupported source modality layout')
    for camera in CAMERAS:
        if modality['video'][camera]['original_key'] != 'observation.images.' + camera:
            raise ValueError('Unsupported camera mapping')

def from_lerobot_rows(rows, *, modality, instruction, videos, provenance, episode_index=0, split='development', demonstration_evidence=None):
    verify_modality(modality)
    if not rows or len(rows)>10000: raise ValueError('Episode row bound exceeded')
    fps = provenance['fps']
    if fps<=0: raise ValueError('Invalid source fps')
    for i, row in enumerate(rows):
        if row['episode_index'] != episode_index or row['frame_index'] != i:
            raise ValueError('Mixed or noncontiguous episode')
        if abs(row['timestamp'] - i/fps)>1e-5:
            raise ValueError('Frame/timestamp mismatch')
        if row['task_index'] != rows[0]['task_index']:
            raise ValueError('Mixed task indices')
    if provenance['real_data'] and (provenance.get('family')!='human_pretrain' or not demonstration_evidence):
        raise ValueError('Real actor import requires explicit human-demonstration evidence')
    group = initialization_group(provenance['repo'], provenance['logical_dataset'], episode_index)
    return Episode(episode_id=f'{group}@{provenance["revision"]}', initialization_group=group,
        split=split, instruction=instruction,
        source_state16=tuple(tuple(r['observation.state']) for r in rows),
        executed_native=tuple(reorder_action(r['action'],'lerobot','native') for r in rows),
        timestamps=tuple(r['timestamp'] for r in rows), rewards=tuple(r['next.reward'] for r in rows),
        dones=tuple(r['next.done'] for r in rows), videos=copy.deepcopy(videos), provenance=copy.deepcopy(provenance),
        quality={'kind':'official_human_demonstration' if provenance['real_data'] else 'synthetic_fixture',
                 'success':None, 'recovery':None, 'demonstration_evidence':copy.deepcopy(demonstration_evidence)}, native_chunk_membership=(None,)*len(rows)).validate()

def load_official(root):
    root = Path(root)
    manifest_path=(root/'sources/official-files.json').resolve()
    if not manifest_path.is_relative_to(root.resolve()): raise ValueError('Manifest escapes input root')
    if manifest_path.stat().st_size > 65536: raise ValueError('Oversized acquisition manifest')
    manifest_bytes=manifest_path.read_bytes()
    # This milestone accepts only the reviewed acquisition, not self-attested hashes.
    if hashlib.sha256(manifest_bytes).hexdigest() != 'e3fb1d2657b7e2053f14dc730f94b922ecc519fbee3baf8f474f3b1a74742b2d':
        raise ValueError('Acquisition manifest differs from the pinned official source')
    manifest=json.loads(manifest_bytes)
    hashes={}
    for entry in manifest['files']:
        path=(root/entry['path']).resolve()
        if not path.is_relative_to(root.resolve()): raise ValueError('Source path escapes input root')
        digest=hashlib.sha256(path.read_bytes()).hexdigest()
        if digest!=entry['sha256']: raise ValueError('Source checksum mismatch')
        hashes[entry['path']]=digest
    required={'official-episode/data/chunk-000/episode_000000.parquet',
        'official-episode/meta/info.json','official-episode/meta/modality.json','official-episode/meta/tasks.jsonl'}
    required.update(f'official-episode/videos/chunk-000/observation.images.{c}/episode_000000.mp4' for c in CAMERAS)
    if not required.issubset(hashes): raise ValueError('Consumed source missing from checksum manifest')
    import pyarrow.parquet as pq
    rows=pq.read_table(root/'official-episode/data/chunk-000/episode_000000.parquet').to_pylist()
    info=json.loads((root/'official-episode/meta/info.json').read_text())
    modality=json.loads((root/'official-episode/meta/modality.json').read_text())
    tasks={r['task_index']:r['task'] for r in map(json.loads,(root/'official-episode/meta/tasks.jsonl').read_text().splitlines())}
    videos={c:{'path':f'official-episode/videos/chunk-000/observation.images.{c}/episode_000000.mp4',
        'frame_count':len(rows), 'fps':info['fps'], 'pixel_origin':'video_decoded'} for c in CAMERAS}
    provenance={'repo':manifest['repo'],'revision':manifest['revision'],'logical_dataset':LOGICAL_DATASET,
        'real_data':True,'family':manifest['family'],'episode_index':0,'simulator_seed':None,
        'fps':info['fps'],'source_sha256':hashes}
    return from_lerobot_rows(rows,modality=modality,instruction=tasks[rows[0]['task_index']],videos=videos,provenance=provenance,demonstration_evidence={'family':manifest['family'],'repo':manifest['repo'],'revision':manifest['revision'],'manifest_sha256':hashlib.sha256(manifest_bytes).hexdigest()})
