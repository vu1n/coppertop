"""Bounded filesystem round-trip of four complete CPU episode fixtures."""
import json
import os
from pathlib import Path
import shutil

from .chords import LocalStore, canonical, digest, object_path
from .cloud_stage import StageManifest, verify_stage
from .mechanism import ACTION_ORDER, Frame, Tape
from .record_archive import seal_directory, stage_archive, unpack_archive
from .streams import SMALL_BYTES, ObjectRef, file_ref

MAX_BYTES = 16 * 1024**2
MAX_OBJECTS = 10
MAX_RECEIPT_BYTES = 65536
HORIZON = 600
EPISODES = 4
RECEIPT = 'roundtrip-hashes.json'
SCOPE = 'local filesystem CPU fixture; no remote durability, real model, or simulator evidence'


def _bound(value):
    if type(value) is not int or not 0 < value <= MAX_BYTES:
        raise ValueError('Bounded local fixture byte budget required')


def _fixture_provenance(value, *, pilot=False):
    if (not isinstance(value, dict) or value.get('proposal_selected') is not False
            or value.get('paid_authorized') is not False
            or any(value.get(key, False) is not False
                   for key in ('real_model_execution', 'real_simulator_execution'))
            or (pilot and (value.get('real_model_execution') is not False
                           or value.get('real_simulator_execution') is not False))):
        raise ValueError('Explicit unselected unpaid CPU fixture provenance required')


class _Objects:
    """Measure the exact Tape.publish bytes before any filesystem mutation."""
    def __init__(self, max_bytes):
        self.max_bytes, self.raw, self.size = max_bytes, {}, 0

    def put(self, raw):
        if not isinstance(raw, bytes) or not 0 < len(raw) <= SMALL_BYTES:
            raise ValueError('Buffered fixture object exceeds bound')
        key = digest(raw)
        if key not in self.raw:
            if len(self.raw) >= MAX_OBJECTS or self.size + len(raw) > self.max_bytes:
                raise ValueError('Fixture closure exceeds local object/byte bound')
            self.raw[key] = raw
            self.size += len(raw)
        return key


def _prepare(pilot, max_bytes):
    if not isinstance(pilot, dict) or not isinstance(pilot.get('episodes'), (tuple, list)):
        raise ValueError('Complete CPU fixture pilot required')
    episodes = pilot['episodes']
    if len(episodes) != EPISODES:
        raise ValueError('Exactly four complete fixture episodes required')
    objects = _Objects(max_bytes)
    records = []
    identities = set()
    for result in episodes:
        if not isinstance(result, dict) or not isinstance(result.get('tape'), Tape):
            raise ValueError('Every episode requires its actual immutable Tape')
        tape = result['tape']
        if len(tape.actions) != HORIZON or len(tape.frames) != HORIZON + 1:
            raise ValueError('Four full 600-control fixture tapes required')
        if tape.episode_id in identities:
            raise ValueError('Distinct fixture episode identities required')
        identities.add(tape.episode_id)
        if not isinstance(result.get('accounting'), dict) or not isinstance(result.get('provenance'), dict):
            raise ValueError('Serializable episode accounting/provenance required')
        _fixture_provenance(result['provenance'])
        accounting = result['accounting']
        if (accounting.get('episode_id') != tape.episode_id or accounting.get('seed') != tape.seed
                or accounting.get('controls') != HORIZON or accounting.get('horizon') != HORIZON):
            raise ValueError('Episode Tape/accounting binding differs')
        tape_key = tape.publish(objects, result['provenance'])
        accounting_key = objects.put(canonical(result['accounting']))
        records.append({'episode_id': tape.episode_id, 'seed': tape.seed,
            'tape': tape_key, 'accounting': accounting_key, 'controls': len(tape.actions)})
    if not isinstance(pilot.get('accounting'), dict) or not isinstance(pilot.get('provenance'), dict):
        raise ValueError('Serializable pilot accounting/provenance required')
    _fixture_provenance(pilot['provenance'], pilot=True)
    if (pilot['accounting'].get('controls') != EPISODES*HORIZON
            or pilot['accounting'].get('episode_order') != [row['episode_id'] for row in records]):
        raise ValueError('Pilot accounting differs from complete episode schedule')
    if 'tapes' in pilot and tuple(pilot['tapes']) != tuple(result['tape'] for result in episodes):
        raise ValueError('Pilot tape list differs from complete episode results')
    accounting = objects.put(canonical(pilot['accounting']))
    root = objects.put(canonical({'schema_version': 1, 'kind': 'cpu_fixture_pilot_closure',
        'episodes': records, 'pilot_accounting': accounting, 'provenance': pilot['provenance'],
        'scope': SCOPE, 'paid_authorized': False, 'remote_durability_verified': False,
        'real_model_simulator_verified': False}))
    files = {'store/' + object_path(key): ObjectRef(key, len(raw)).to_dict()
             for key, raw in sorted(objects.raw.items())}
    return objects, ObjectRef(root, len(objects.raw[root])), files


def _plain(directory):
    path = Path(directory).absolute()
    if any(part.is_symlink() for part in (path, *path.parents)):
        raise ValueError('Local fixture paths cannot traverse symlinks')
    return path


def _inventory(directory, max_bytes):
    paths, total = {}, 0
    for path in directory.rglob('*'):
        if path.is_symlink() or not (path.is_file() or path.is_dir()):
            raise ValueError('Local fixture contains a nonordinary path')
        if path.is_file():
            size = path.stat().st_size
            total += size
            if path.stat().st_nlink != 1 or len(paths) >= MAX_OBJECTS or total > max_bytes:
                raise ValueError('Local fixture file/object/byte bound exceeded')
            paths[path.relative_to(directory).as_posix()] = path
    return {name: file_ref(path).to_dict() for name, path in sorted(paths.items())}


def _fsync_commit(directory):
    """Local fsync barrier only; this is not a provider or remote commit."""
    directory = _plain(directory)
    for path in sorted(directory.rglob('*')):
        if path.is_symlink() or not (path.is_file() or path.is_dir()):
            raise ValueError('Cannot commit a nonordinary fixture path')
        if path.is_file():
            with path.open('rb') as source:
                os.fsync(source.fileno())
    folders = [directory, *(path for path in directory.rglob('*') if path.is_dir())]
    for path in sorted(folders, key=lambda value: len(value.parts), reverse=True):
        descriptor = os.open(path, os.O_RDONLY | getattr(os, 'O_DIRECTORY', 0))
        try:
            os.fsync(descriptor)
        finally:
            os.close(descriptor)
    # Persist the directory's link in its existing parent as well.
    descriptor = os.open(directory.parent, os.O_RDONLY | getattr(os, 'O_DIRECTORY', 0))
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def _load_tape(value):
    if (value.get('schema_version') != 1 or value.get('kind') != 'mechanism_replay_tape'
            or value.get('action_order') != list(ACTION_ORDER)
            or len(value.get('actions', [])) != HORIZON
            or len(value.get('frames', [])) != HORIZON + 1):
        raise ValueError('Complete native fixture Tape required on reload')
    frames = tuple(Frame(step=row['step'], time_ns=row['time_ns'],
        components=tuple(row['components'].items()), success=row['success'],
        terminated=row['terminated'], truncated=row['truncated']) for row in value['frames'])
    tape = Tape(value['episode_id'], value['seed'],
        tuple(tuple(row) for row in value['actions']), frames,
        value['parent_episode_id'], value['inherited_frames'])
    if any(row['frame_id'] != tape.identity(index) for index, row in enumerate(value['frames'])):
        raise ValueError('Reloaded Tape frame identities differ')
    return tape


def _verify_closure(store, root, files):
    if store.stat(root.sha256) != root:
        raise ValueError('Fixture closure root size differs')
    value = json.loads(store.get(root.sha256))
    if (value.get('schema_version') != 1 or value.get('kind') != 'cpu_fixture_pilot_closure'
            or not isinstance(value.get('episodes'), list) or len(value['episodes']) != EPISODES
            or value.get('scope') != SCOPE or value.get('paid_authorized') is not False
            or value.get('remote_durability_verified') is not False
            or value.get('real_model_simulator_verified') is not False):
        raise ValueError('Unsupported fixture closure root')
    keys = {root.sha256, value['pilot_accounting']}
    pilot_accounting = json.loads(store.get(value['pilot_accounting']))
    _fixture_provenance(value.get('provenance'), pilot=True)
    if (not isinstance(pilot_accounting, dict)
            or pilot_accounting.get('controls') != EPISODES*HORIZON
            or pilot_accounting.get('episode_order') != [row['episode_id'] for row in value['episodes']]):
        raise ValueError('Pilot accounting missing on reload')
    identities = set()
    for row in value['episodes']:
        keys.update((row['tape'], row['accounting']))
        tape_value = json.loads(store.get(row['tape']))
        _fixture_provenance(tape_value.get('provenance'))
        tape = _load_tape(tape_value)
        accounting = json.loads(store.get(row['accounting']))
        if (tape.episode_id != row['episode_id'] or tape.seed != row['seed']
                or row['controls'] != HORIZON or tape.episode_id in identities
                or not isinstance(accounting, dict) or accounting.get('episode_id') != tape.episode_id
                or accounting.get('seed') != tape.seed or accounting.get('controls') != HORIZON
                or accounting.get('horizon') != HORIZON):
            raise ValueError('Episode Tape/accounting closure differs on reload')
        identities.add(tape.episode_id)
    if {'store/' + object_path(key) for key in keys} != set(files):
        raise ValueError('Fixture contains missing or unreferenced closure objects')
    return {'episodes': len(identities), 'controls': EPISODES * HORIZON,
            'frames': EPISODES * (HORIZON + 1), 'objects': len(keys)}


def reload_roundtrip(directory, *, receipt_sha256, max_bytes=MAX_BYTES):
    """Independently read every retained byte against the caller-pinned receipt."""
    _bound(max_bytes)
    directory = _plain(directory)
    receipt_path = directory/RECEIPT
    object_path(receipt_sha256)
    with receipt_path.open('rb') as source:
        raw = source.read(MAX_RECEIPT_BYTES + 1)
    if len(raw) > MAX_RECEIPT_BYTES or digest(raw) != receipt_sha256:
        raise ValueError('Pinned local round-trip receipt differs')
    value = json.loads(raw)
    files = value['files']
    if (value.get('schema_version') != 1 or value.get('kind') != 'local_fixture_roundtrip_hashes'
            or value.get('local_fsync_committed') is not True
            or not isinstance(files, dict) or not 0 < len(files) <= MAX_OBJECTS
            or sum(ObjectRef(**ref).size for ref in files.values()) > max_bytes):
        raise ValueError('Bounded local round-trip receipt required')
    for name in ('source', 'restored'):
        if _inventory(_plain(directory/name), max_bytes) != files:
            raise ValueError('Fixture file closure/checksum differs: ' + name)
    original_archive = _plain(directory/'episode.tar')
    staged_archive = _plain(directory/'stage/episode.tar')
    archive = ObjectRef(**value['descriptor']['archive'])
    if (archive.size > max_bytes + 1024**2
            or original_archive.stat().st_size != archive.size
            or staged_archive.stat().st_size != archive.size
            or file_ref(original_archive) != archive or file_ref(staged_archive) != archive):
        raise ValueError('Original or staged archive checksum differs')
    stage_manifest = StageManifest.from_dict(value['stage_manifest'])
    verify_stage(stage_manifest, directory/'stage')
    if file_ref(directory/'stage/stage.json').to_dict() != value['stage_receipt']:
        raise ValueError('Stage receipt checksum differs')
    expected_stage = {'archive.json', 'episode.tar', 'stage.json'}
    if {path.name for path in (directory/'stage').iterdir()} != expected_stage:
        raise ValueError('Unexpected local staging closure')
    root = ObjectRef(**value['root'])
    source = _verify_closure(LocalStore(directory/'source/store'), root, files)
    restored = _verify_closure(LocalStore(directory/'restored/store'), root, files)
    if restored != source:
        raise ValueError('Independently restored fixture accounting differs')
    return {'status': 'local_hash_verified', 'scope': SCOPE, **restored,
        'source_files_verified': len(files), 'restored_files_verified': len(files),
        'raw_bytes': sum(ref['size'] for ref in files.values()),
        'archive': archive.to_dict(), 'root': root.to_dict(),
        'receipt_sha256': receipt_sha256, 'local_fsync_committed': True,
        'remote_durability_verified': False, 'real_model_simulator_verified': False,
        'paid_authorized': False}


def roundtrip(directory, pilot=None, *, max_bytes=MAX_BYTES):
    """Publish, locally commit, and independently reload four full CPU fixtures.

    The target must be a new directory. Source and archive bytes survive any
    failure; only an incomplete, newly created restored directory is removed.
    """
    _bound(max_bytes)
    directory = _plain(directory)
    if directory.exists():
        raise FileExistsError('Round-trip directory already exists')
    if pilot is None:
        from .evaluation_episode import run_fixture_pilot
        pilot = run_fixture_pilot()
    objects, root, files = _prepare(pilot, max_bytes)
    directory.mkdir(parents=True, exist_ok=False)
    source = directory/'source'
    store = LocalStore(source/'store')
    for key, raw in objects.raw.items():
        if store.put(raw) != key:
            raise ValueError('Published fixture object identity differs')
    if _inventory(source, max_bytes) != files:
        raise ValueError('Published source closure differs')
    _verify_closure(LocalStore(source/'store'), root, files)
    descriptor = seal_directory(source, directory/'episode.tar')
    if descriptor['archive']['size'] > max_bytes + 1024**2:
        raise ValueError('Fixture archive exceeds local byte bound')
    stage_archive(directory/'episode.tar', descriptor, directory/'stage',
        run_id='clef-cpu-fixture', commit=lambda: _fsync_commit(directory/'stage'))
    stage_manifest = StageManifest('clef-cpu-fixture', descriptor['archive']['sha256'],
        {'episode.tar': ObjectRef(**descriptor['archive']),
         'archive.json': file_ref(directory/'stage/archive.json')})
    receipt = {'schema_version': 1, 'kind': 'local_fixture_roundtrip_hashes',
        'local_fsync_committed': True,
        'root': root.to_dict(), 'files': files, 'descriptor': descriptor,
        'stage_manifest': stage_manifest.to_dict(),
        'stage_receipt': file_ref(directory/'stage/stage.json').to_dict()}
    raw = canonical(receipt)
    if len(raw) > MAX_RECEIPT_BYTES:
        raise ValueError('Fixture hash receipt exceeds bound')
    restored = directory/'restored'
    restored_created = False
    try:
        unpack_archive(directory/'stage/episode.tar', descriptor, restored)
        restored_created = True
        _fsync_commit(directory)
        # The hash receipt is created only after the complete data fsync barrier.
        with (directory/RECEIPT).open('xb') as output:
            output.write(raw)
            output.flush()
            os.fsync(output.fileno())
        _fsync_commit(directory)
        result = reload_roundtrip(directory, receipt_sha256=digest(raw), max_bytes=max_bytes)
    except BaseException as error:
        if restored_created and restored.exists():
            try:
                shutil.rmtree(restored)
            except BaseException as cleanup_error:
                error.add_note('Restored fixture cleanup failed; partial evidence retained at '
                    f'{restored}: {type(cleanup_error).__name__}: {cleanup_error}')
        raise
    return dict(result, directory=str(directory), receipt=str(directory/RECEIPT),
        max_raw_bytes=max_bytes, max_objects=MAX_OBJECTS,
        max_retained_bytes=2*max_bytes + 2*(max_bytes + 1024**2) + 2*MAX_RECEIPT_BYTES)
