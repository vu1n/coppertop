"""R2-compatible streaming adapter. The caller supplies an already authorized S3 client."""
import hashlib
import time
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from typing import Mapping, Protocol, Iterable, Callable
from urllib.parse import urlsplit
from .chords import digest, object_path
from .streams import ObjectRef, CHUNK_BYTES, SMALL_BYTES, checked, read_small, verify, file_ref
from .multipart import UploadState, UploadInterrupted, MIN_PART_BYTES, MAX_PARTS


class S3Client(Protocol):
    def put_object(self, **kwargs) -> dict: ...
    def get_object(self, **kwargs) -> dict: ...
    def head_object(self, **kwargs) -> dict: ...
    def create_multipart_upload(self, **kwargs) -> dict: ...
    def upload_part(self, **kwargs) -> dict: ...
    def list_parts(self, **kwargs) -> dict: ...
    def complete_multipart_upload(self, **kwargs) -> dict: ...
    def abort_multipart_upload(self, **kwargs) -> dict: ...


@dataclass(frozen=True)
class R2Config:
    endpoint: str
    bucket: str
    prefix: str = 'chords'
    region: str = 'auto'

    def __post_init__(self):
        url = urlsplit(self.endpoint)
        if url.scheme != 'https' or not url.hostname or url.username or url.password or url.query or url.fragment or url.path not in ('', '/'):
            raise ValueError('Object endpoint must be an HTTPS origin without credentials')
        if not self.bucket or '/' in self.bucket or self.region != 'auto':
            raise ValueError('R2 requires an explicit bucket and auto region')
        _prefix(self.prefix)

    @classmethod
    def from_mapping(cls, public: Mapping[str, str]):
        # No environment/credential discovery. Only public configuration is consumed.
        return cls(public['R2_ENDPOINT'], public['R2_BUCKET'], public.get('R2_PREFIX', 'chords'))

    def bind(self, client: S3Client):
        return S3Store(client, self.bucket, prefix=self.prefix)


def _prefix(value: str) -> str:
    if value and (value.startswith('/') or value.endswith('/') or any(p in ('', '.', '..') for p in value.split('/'))):
        raise ValueError('Object prefix must be relative path segments')
    return value


def _code(error: Exception) -> str | None:
    response = getattr(error, 'response', {})
    return response.get('Error', {}).get('Code') if isinstance(response, dict) else None


class S3Store:
    def __init__(self, client: S3Client, bucket: str, *, prefix: str = 'chords'):
        if not bucket or '/' in bucket:
            raise ValueError('Explicit bucket required')
        self.client = client
        self.bucket = bucket
        self.prefix = _prefix(prefix)

    def key(self, digest_key: str) -> str:
        path = object_path(digest_key)
        return f'{self.prefix}/{path}' if self.prefix else path

    def put(self, data: bytes) -> str:
        return self.put_bounded(data,limit=SMALL_BYTES)

    def put_bounded(self, data: bytes, *, limit: int) -> str:
        if type(limit) is not int or not SMALL_BYTES<=limit<=64*1024**2 or type(data) is not bytes or len(data)>limit:
            raise ValueError('Immutable single PUT exceeds explicit buffer bound')
        key = digest(data)
        try:
            self.client.put_object(Bucket=self.bucket, Key=self.key(key), Body=data,
                                   IfNoneMatch='*', ContentType='application/octet-stream')
        except Exception as error:
            if _code(error) != 'PreconditionFailed':
                raise
            if read_small(self,key,limit) != data:
                raise ValueError('Existing object corrupted') from error
        return key

    def get(self, key: str) -> bytes:
        return read_small(self, key)

    def stat(self, key: str) -> ObjectRef:
        try:
            response = self.client.head_object(Bucket=self.bucket, Key=self.key(key))
        except Exception as error:
            if _code(error) not in ('NoSuchKey', 'NotFound', '404'):
                raise
            raise FileNotFoundError(key) from error
        return ObjectRef(key, response['ContentLength'])

    def iter_bytes(self, key: str, *, offset: int = 0, length: int | None = None):
        ref = self.stat(key)
        if type(offset) is not int or offset < 0 or offset > ref.size or (length is not None and (type(length) is not int or length < 0 or offset + length > ref.size)):
            raise ValueError('Invalid object range')
        count = ref.size - offset if length is None else length
        if not count:
            return
        options = {} if offset == 0 and length is None else {'Range': f'bytes={offset}-{offset+count-1}'}
        response = self.client.get_object(Bucket=self.bucket, Key=self.key(key), **options)
        body = response['Body']
        try:
            if response['ContentLength'] != count:
                raise ValueError('Incorrect range response length')
            if options and response.get('ContentRange') != f'bytes {offset}-{offset+count-1}/{ref.size}':
                raise ValueError('Incorrect range response')
            remaining = count
            while remaining:
                chunk = body.read(min(CHUNK_BYTES, remaining))
                if not chunk or len(chunk) > min(CHUNK_BYTES, remaining):
                    raise ValueError('Truncated or oversized object stream')
                remaining -= len(chunk)
                yield chunk
            if body.read(1):
                raise ValueError('Object stream exceeds response length')
        finally:
            body.close()

    def put_stream(self, ref: ObjectRef, chunks: Iterable[bytes]) -> ObjectRef:
        if ref.size <= SMALL_BYTES:
            data = b''.join(checked(chunks, ref))
            if self.put(data) != ref.sha256:
                raise ValueError('Object identity mismatch')
            return ref
        return self.multipart(ref, chunks)

    def multipart(self, ref: ObjectRef, chunks: Iterable[bytes], *, state: UploadState | None = None,
                  persist: Callable[[dict], None] = lambda value: None, part_size: int = MIN_PART_BYTES) -> ObjectRef:
        """Retry re-reads source to hash it, but sends only parts without saved receipts.

        One writer owns an upload session. Persist snapshots durably; after a crash
        before receipt persistence, re-uploading the same part number replaces it.
        Completion has no assumed conditional semantics in the R2 Workers API.
        """
        destination = f'{self.bucket}/{self.key(ref.sha256)}'
        if state is not None:
            state.validate()
            if state.ref != ref or state.destination != destination:
                raise ValueError('Upload state belongs to a different destination/object')
            part_size = state.part_size
        else:
            candidate = UploadState(destination, 'pending', ref, part_size)
            candidate.validate()
        try:
            existing = self.stat(ref.sha256)
        except FileNotFoundError:
            pass
        else:
            if existing != ref:
                raise ValueError('Existing object size mismatch')
            verify(self, ref)
            return ref
        if state is None:
            upload = self.client.create_multipart_upload(Bucket=self.bucket, Key=self.key(ref.sha256))
            state = UploadState(destination, upload['UploadId'], ref, part_size)
        try:
            persist(state.to_dict())
            self._reconcile_parts(state)
            buffer = bytearray()
            number = 0
            def send(data):
                nonlocal number
                number += 1
                sha = hashlib.sha256(data).hexdigest()
                if number <= len(state.parts):
                    saved = state.parts[number-1]
                    if saved['sha256'] != sha or saved['size'] != len(data):
                        raise ValueError('Resumed source part changed')
                    return
                result = self.client.upload_part(Bucket=self.bucket, Key=self.key(ref.sha256),
                    UploadId=state.upload_id, PartNumber=number, Body=data)
                state.parts.append({'number': number, 'etag': result['ETag'],
                                    'size': len(data), 'sha256': sha})
                persist(state.to_dict())
            for chunk in checked(chunks, ref):
                buffer.extend(chunk)
                if len(buffer) >= part_size:
                    send(bytes(buffer[:part_size]))
                    del buffer[:part_size]
            if buffer:
                send(bytes(buffer))
            if number != len(state.parts) or number == 0:
                raise ValueError('Incomplete multipart source')
            self.client.complete_multipart_upload(Bucket=self.bucket, Key=self.key(ref.sha256),
                UploadId=state.upload_id, MultipartUpload={'Parts': [
                    {'PartNumber': p['number'], 'ETag': p['etag']} for p in state.parts]})
            verify(self, ref)
            return ref
        except Exception as error:
            raise UploadInterrupted(state) from error

    def abort(self, state: UploadState):
        state.validate()
        if state.destination != f'{self.bucket}/{self.key(state.ref.sha256)}':
            raise ValueError('Upload state belongs to a different destination')
        self.client.abort_multipart_upload(Bucket=self.bucket, Key=self.key(state.ref.sha256), UploadId=state.upload_id)

    def multipart_file(self, ref, path, *, state=None, persist=lambda value: None,
                       part_size=16*1024**2, concurrency=4, measured=lambda stage, seconds: None):
        """Bounded seekable transport, explicit resume, opaque ETags, one GET verification.

        Persist only the contiguous receipt prefix. Parts completed out of order
        before a crash can safely be replaced on an explicit resume.
        """
        if type(concurrency) is not int or not 1 <= concurrency <= 4:
            raise ValueError('Multipart concurrency must be 1..4')
        destination = f'{self.bucket}/{self.key(ref.sha256)}'
        if state is not None:
            state.validate()
            if state.ref != ref or state.destination != destination:
                raise ValueError('Upload state belongs to another destination/object')
            part_size = state.part_size
        else:
            UploadState(destination, 'pending', ref, part_size).validate()
        if part_size>16*1024**2:
            raise ValueError('Seekable multipart parts must be at most 16MiB, including resume')
        before = time.monotonic()
        if path.is_symlink() or path.stat().st_size != ref.size or file_ref(path) != ref or not ref.size:
            raise ValueError('Seekable immutable source differs')
        measured('source_check', time.monotonic()-before)
        options = dict(Bucket=self.bucket, Key=self.key(ref.sha256))
        try:
            existing = self.stat(ref.sha256)
        except FileNotFoundError:
            existing = None
        if existing is not None:
            if existing != ref:
                raise ValueError('Existing archive size differs')
            self._verify_known(ref, measured)
            return ref
        if state is None:
            state = UploadState(destination, self.client.create_multipart_upload(**options)['UploadId'], ref, part_size)
        try:
            persist(state.to_dict())
            self._reconcile_parts(state)
            count = (ref.size+part_size-1)//part_size
            first_upload = time.monotonic()
            def send(number):
                size = min(part_size, ref.size-(number-1)*part_size)
                with path.open('rb') as source:
                    source.seek((number-1)*part_size)
                    data = source.read(size)
                if len(data) != size:
                    raise ValueError('Source truncated during multipart upload')
                sha = hashlib.sha256(data).hexdigest()
                if number <= len(state.parts):
                    saved = state.parts[number-1]
                    if saved['sha256'] != sha:
                        raise ValueError('Resumed source part differs')
                    return saved
                result = self.client.upload_part(**options, UploadId=state.upload_id, PartNumber=number, Body=data)
                etag = result.get('ETag')
                if not isinstance(etag, str) or not etag:
                    raise ValueError('Missing opaque multipart ETag')
                return dict(number=number, etag=etag, size=size, sha256=sha)
            measured('first_upload', time.monotonic()-before)
            with ThreadPoolExecutor(max_workers=concurrency) as pool:
                # Batches bound resident payloads and in-flight requests. A failed
                # batch is drained before returning resumable state to the caller.
                for start in range(1, count+1, concurrency):
                    futures = [pool.submit(send, number) for number in range(start, min(count+1,start+concurrency))]
                    for future in futures:
                        part = future.result()
                        if part['number'] > len(state.parts):
                            state.parts.append(part)
                            persist(state.to_dict())
            if file_ref(path) != ref:
                raise ValueError('Source changed during multipart upload')
            self.client.complete_multipart_upload(**options, UploadId=state.upload_id,
                MultipartUpload={'Parts': [{'PartNumber': p['number'], 'ETag': p['etag']} for p in state.parts]})
            measured('upload', time.monotonic()-first_upload)
            self._verify_known(ref, measured)
            return ref
        except Exception as error:
            raise UploadInterrupted(state) from error

    def _verify_known(self, ref, measured):
        before = time.monotonic()
        response = self.client.get_object(Bucket=self.bucket, Key=self.key(ref.sha256))
        body = response['Body']
        try:
            if response['ContentLength'] != ref.size:
                raise ValueError('Archive readback length differs')
            for _ in checked(iter(lambda: body.read(CHUNK_BYTES), b''), ref):
                pass
        finally:
            body.close()
        measured('readback', time.monotonic()-before)

    def _reconcile_parts(self, state):
        remote, marker = {}, 0
        while True:
            page = self.client.list_parts(Bucket=self.bucket, Key=self.key(state.ref.sha256),
                UploadId=state.upload_id, PartNumberMarker=marker)
            for part in page.get('Parts', []):
                number = part['PartNumber']
                if type(number) is not int or not 1 <= number <= MAX_PARTS or number in remote:
                    raise ValueError('Invalid remote multipart number')
                remote[number] = part
            if not page.get('IsTruncated'):
                break
            following = page['NextPartNumberMarker']
            if type(following) is not int or not marker < following <= MAX_PARTS:
                raise ValueError('Multipart pagination marker outside bounded part range')
            marker = following
        for part in state.parts:
            actual = remote.get(part['number'])
            if not actual or actual['ETag'] != part['etag'] or actual['Size'] != part['size']:
                raise ValueError('Saved multipart receipt missing or changed remotely')
