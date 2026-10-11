"""Sealed episode transport; unpack restores the existing directory/ObjectStore format."""
import hashlib
import io
import json
import os
from pathlib import Path
import shutil
import sys
import tarfile
import time

from .chords import canonical
from .cloud_stage import StageManifest, commit_directory, safe_name
from .streams import CHUNK_BYTES, ObjectRef, checked, file_chunks, file_ref, read_small
from .publication_trace import phase

MANIFEST = 'coppertop-archive-manifest.json'
# Up to 6000 CAS objects plus bounded worker/provenance sidecars.
MAX_FILES = 6020
MAX_RAW_BYTES = 1024**3
MAX_ARCHIVE_BYTES = MAX_RAW_BYTES + 16*1024**2
MAX_MANIFEST_BYTES = 2*1024**2
PART_BYTES = 16*1024**2


def _plain_path(path):
    path = Path(path).absolute()
    # macOS exposes its system temp roots through fixed /private aliases.
    # Normalize those two aliases, while rejecting application-owned symlinks.
    if sys.platform=='darwin':
        for alias in ('/var','/tmp'):
            root=Path(alias)
            if path.is_relative_to(root) and root.is_symlink() and root.resolve()==Path('/private'+alias):
                path=Path('/private'+alias)/path.relative_to(root)
    if any(p.is_symlink() for p in (path, *path.parents)):
        raise ValueError('Symlink path rejected')
    return path


def _entries(value):
    if value.get('schema_version') != 1 or value.get('format') != 'coppertop-episode-tar':
        raise ValueError('Unsupported archive manifest')
    entries = value.get('files')
    if not isinstance(entries, dict) or not 1 <= len(entries) <= MAX_FILES:
        raise ValueError('Archive file count outside bound')
    total = 0
    names = set(entries)
    for name, ref in entries.items():
        safe_name(name)
        if name == MANIFEST or '\\' in name or ':' in name:
            raise ValueError('Reserved or nonportable archive path')
        if any('/'.join(name.split('/')[:n]) in names for n in range(1, len(name.split('/')))):
            raise ValueError('Archive file/directory collision')
        total += ObjectRef(**ref).size
    if total > MAX_RAW_BYTES:
        raise ValueError('Archive raw-byte bound exceeded')
    return entries


def _header(name, size):
    info = tarfile.TarInfo(name)
    info.size, info.mode, info.mtime = size, 0o644, 0
    return info


class _HashReader:
    def __init__(self, source):
        self.source, self.hash, self.size = source, hashlib.sha256(), 0

    def read(self, size=-1):
        data = self.source.read(size)
        self.hash.update(data)
        self.size += len(data)
        return data


def seal_directory(directory, archive):
    """Two bounded sequential source passes; deterministic USTAR, no compression."""
    started = time.monotonic()
    directory, archive = _plain_path(directory), _plain_path(archive)
    if not directory.is_dir() or archive == directory or directory in archive.parents:
        raise ValueError('Archive must be outside its source directory')
    paths = {}
    total = 0
    for path in directory.rglob('*'):
        if path.is_symlink() or not (path.is_dir() or path.is_file()):
            raise ValueError('Archive source must contain only ordinary files/directories')
        if path.is_file():
            if path.stat().st_nlink != 1:
                raise ValueError('Hard-linked source rejected')
            paths[path.relative_to(directory).as_posix()] = path
            total += path.stat().st_size
            if total > MAX_RAW_BYTES:
                raise ValueError('Archive source raw-byte bound exceeded')
            if len(paths) > MAX_FILES:
                raise ValueError('Archive source file count exceeds bound')
    files = {name: file_ref(paths[name]).to_dict() for name in sorted(paths)}
    manifest = {'schema_version': 1, 'format': 'coppertop-episode-tar', 'files': files}
    _entries(manifest)
    raw = canonical(manifest)
    if len(raw) > MAX_MANIFEST_BYTES:
        raise ValueError('Archive manifest exceeds bound')
    # Exclusive creation preserves prior archives and the sole original source.
    created = False
    try:
        with archive.open('xb') as output:
            created = True
            with tarfile.open(fileobj=output, mode='w', format=tarfile.USTAR_FORMAT) as tar:
                tar.addfile(_header(MANIFEST, len(raw)), io.BytesIO(raw))
                for name, path in sorted(paths.items()):
                    _plain_path(path)
                    with path.open('rb') as source:
                        reader = _HashReader(source)
                        tar.addfile(_header(name, files[name]['size']), reader)
                        if reader.size != files[name]['size'] or reader.hash.hexdigest() != files[name]['sha256'] or source.read(1):
                            raise ValueError('Source changed while sealing')
            output.flush()
            os.fsync(output.fileno())
        ref = file_ref(archive)
        if ref.size > MAX_ARCHIVE_BYTES:
            raise ValueError('Sealed archive exceeds transport bound')
    except BaseException:
        if created:
            archive.unlink()
        raise
    return {'schema_version': 1, 'format': 'coppertop-episode-tar', 'archive': ref.to_dict(),
            'file_count': len(files), 'raw_bytes': sum(r['size'] for r in files.values()),
            'manifest_sha256': hashlib.sha256(raw).hexdigest(), 'seal_seconds': time.monotonic()-started}


def archive_ref(descriptor):
    if descriptor.get('schema_version') != 1 or descriptor.get('format') != 'coppertop-episode-tar':
        raise ValueError('Unsupported archive descriptor')
    ref = ObjectRef(**descriptor['archive'])
    if not 0 < ref.size <= MAX_ARCHIVE_BYTES:
        raise ValueError('Archive transport size outside bound')
    return ref


def stage_archive(archive, descriptor, directory, *, run_id, commit):
    """Provider commit is the durability barrier; GPU control receipt comes afterward."""
    started = time.monotonic()
    archive, directory = _plain_path(archive), _plain_path(directory)
    ref = archive_ref(descriptor)
    directory.mkdir(parents=True, exist_ok=False)
    with (directory/'episode.tar').open('xb') as output:
        for chunk in checked(file_chunks(archive), ref):
            output.write(chunk)
        output.flush()
        os.fsync(output.fileno())
    staged = dict(descriptor, durable_copy_seconds=time.monotonic()-started)
    (directory/'archive.json').write_bytes(canonical(staged))
    manifest = StageManifest(run_id, ref.sha256,
        {'episode.tar': ref, 'archive.json': file_ref(directory/'archive.json')})
    # On any failure preserve both producer bytes and all copied stage bytes.
    # An uncertain commit acknowledgement must not trigger destructive cleanup.
    commit_directory(manifest, directory, commit=commit)
    staged['durable_copy_seconds']=time.monotonic()-started
    return staged


def _preflight_tar(archive, size):
    """Reject GNU/PAX metadata before tarfile can allocate/recursively parse it."""
    seen=set()
    with archive.open('rb') as source:
        while source.tell()<size:
            block=source.read(512)
            if len(block)!=512:
                raise ValueError('Truncated archive header')
            if block==b'\0'*512:
                if source.read(512)!=b'\0'*512:
                    raise ValueError('Archive terminator missing')
                while chunk:=source.read(CHUNK_BYTES):
                    if chunk.strip(b'\0'):
                        raise ValueError('Unexpected bytes after archive terminator')
                return
            if block[257:263]!=b'ustar\0':
                raise ValueError('Only plain USTAR archives are supported')
            member=tarfile.TarInfo.frombuf(block,'utf-8','strict')
            safe_name(member.name)
            limit=MAX_MANIFEST_BYTES if not seen else MAX_RAW_BYTES
            if (member.type not in (tarfile.REGTYPE,tarfile.AREGTYPE) or member.linkname or member.name in seen
                    or not 0<=member.size<=limit or len(seen)>MAX_FILES):
                raise ValueError('Unsupported or oversized archive header')
            if not seen and member.name!=MANIFEST:
                raise ValueError('Archive manifest must be first')
            seen.add(member.name)
            end=source.tell()+((member.size+511)//512)*512
            if end>size:
                raise ValueError('Truncated archive member')
            source.seek(end)
    raise ValueError('Archive terminator missing')


def unpack_archive(archive, descriptor, destination):
    """No extractall: verify whole object, bounded manifest, then every file byte."""
    archive, destination = _plain_path(archive), _plain_path(destination)
    ref = archive_ref(descriptor)
    if archive.stat().st_size != ref.size or file_ref(archive) != ref:
        raise ValueError('Archive checksum differs')
    _preflight_tar(archive,ref.size)
    destination.mkdir(parents=True, exist_ok=False)
    try:
        with tarfile.open(archive, mode='r|') as tar:
            members = iter(tar)
            first = next(members, None)
            if first is None or first.name != MANIFEST or not first.isreg() or first.size > MAX_MANIFEST_BYTES or first.pax_headers:
                raise ValueError('Missing ordinary bounded archive manifest')
            raw = tar.extractfile(first).read(MAX_MANIFEST_BYTES+1)
            if hashlib.sha256(raw).hexdigest() != descriptor['manifest_sha256']:
                raise ValueError('Internal manifest checksum differs')
            entries = _entries(json.loads(raw))
            if len(entries) != descriptor['file_count'] or sum(r['size'] for r in entries.values()) != descriptor['raw_bytes']:
                raise ValueError('Archive descriptor differs from manifest')
            seen = set()
            for member in members:
                name = member.name
                if name not in entries or name in seen or not member.isreg() or member.pax_headers or member.linkname:
                    raise ValueError('Unexpected archive member/type')
                expected = ObjectRef(**entries[name])
                if member.size != expected.size:
                    raise ValueError('Archive member size differs')
                target = _plain_path(destination/name)
                target.parent.mkdir(parents=True, exist_ok=True)
                source = tar.extractfile(member)
                with target.open('xb') as output:
                    chunks = iter(lambda: source.read(CHUNK_BYTES), b'')
                    for chunk in checked(chunks, expected):
                        output.write(chunk)
                seen.add(name)
            if seen != set(entries):
                raise ValueError('Archive incomplete')
        return destination
    except BaseException as error:
        try:
            shutil.rmtree(destination)
        except BaseException as cleanup_error:
            error.add_note('Archive extraction cleanup failed; partial evidence retained at '
                f'{destination}: {type(cleanup_error).__name__}: {cleanup_error}')
        raise


def publish_archive(archive, descriptor, store, *, record, state=None, persist=lambda value: None,
                    concurrency=4, publisher_started=None,observe=lambda event:None):
    """One payload + verified completion receipt; timing is returned out of band."""
    started = time.monotonic() if publisher_started is None else publisher_started
    archive = _plain_path(archive)
    ref = archive_ref(descriptor)
    if not isinstance(record,dict) or len(canonical(record))>60*1024:
        raise ValueError('Bounded record required before archive upload')
    timings = {key: descriptor[key] for key in ('seal_seconds', 'durable_copy_seconds') if key in descriptor}
    timings['publisher_prep_seconds'] = time.monotonic()-started
    if not hasattr(store, 'upload_file'):
        raise ValueError('Archive publication requires a seekable file store')
    def transfer_measured(stage, seconds):
        timings[stage+'_seconds'] = seconds
        if stage=='source_check':
            timings['publisher_prep_seconds']=time.monotonic()-started
        if stage=='first_upload':
            timings['first_upload_seconds']=time.monotonic()-started
    store.upload_file(ref, archive, state=state, persist=persist,
                         concurrency=concurrency, measured=transfer_measured,observe=observe)
    receipt = {'schema_version': 1, 'kind': 'coppertop-archive-completion',
               'archive': descriptor, 'record': record, 'verified_sha256': ref.sha256,
               'timings_before_receipt': dict(timings)}
    raw = canonical(receipt)
    if len(raw) > 65536:
        raise ValueError('Completion receipt exceeds metadata bound')
    before = time.monotonic()
    with phase(observe,'receipt_put'): key = store.put(raw)
    with phase(observe,'receipt_verify'):
        if read_small(store, key, 65536) != raw:
            raise ValueError('Completion receipt readback differs')
    timings['receipt_seconds'] = time.monotonic()-before
    return {'report_key': key, 'archive': ref.to_dict(), 'timings': timings}


def read_completion(store, key):
    receipt = json.loads(read_small(store, key, 65536))
    fields={'schema_version','kind','archive','record','verified_sha256','timings_before_receipt'}
    if (not isinstance(receipt,dict) or set(receipt)!=fields or receipt.get('schema_version') != 1
            or receipt.get('kind') != 'coppertop-archive-completion'
            or not isinstance(receipt['record'],dict) or not isinstance(receipt['timings_before_receipt'],dict)):
        raise ValueError('Verified archive completion required')
    ref = archive_ref(receipt['archive'])
    if receipt.get('verified_sha256') != ref.sha256:
        raise ValueError('Incomplete archive completion receipt')
    return receipt
