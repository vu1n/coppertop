"""Synthetic transport tests; never an official episode or cloud execution."""
import io
import json
from pathlib import Path
import shutil
import tarfile
import tempfile
import threading
import time
import tracemalloc
import unittest
from types import SimpleNamespace
from unittest.mock import patch

from coppertop import record_archive as a
from coppertop.archive_retention import cleanup_disposition, validate_retention, retention_report
from coppertop.chords import canonical, digest, LocalStore
from coppertop.multipart import UploadInterrupted, UploadState
from coppertop.r2_session import BoundedClient, SessionScope
from coppertop.s3 import S3Store
from coppertop.streams import file_ref
from test_storage import S3Error


POLICY = {'max_bytes':2*1024**3, 'accept_ongoing_storage_charges':True,
          'cleanup_owner':'operator', 'release_condition':'verified_publication_or_explicit_recovery_or_deletion'}


class DiskS3:
    """Filesystem fake avoids counting an in-memory server as publisher memory."""
    def __init__(self, root, latency=0):
        self.root=Path(root); self.root.mkdir()
        self.calls=[]; self.fail_part=None; self.corrupt=False; self.latency=latency
        self.lock=threading.Lock(); self.inflight=0; self.peak=0
        self.meta=SimpleNamespace(events=SimpleNamespace(register=lambda *args:None))

    def call(self, name):
        with self.lock: self.calls.append(name)
        if self.latency: time.sleep(self.latency)

    def object(self,key): return self.root/key.replace('/','_')

    def head_object(self, *, Bucket, Key):
        self.call('head')
        if not self.object(Key).exists(): raise S3Error('NoSuchKey')
        return {'ContentLength':self.object(Key).stat().st_size}

    def get_object(self, *, Bucket, Key):
        self.call('get')
        path=self.object(Key)
        if self.corrupt and path.stat().st_size>65536:
            return {'ContentLength':path.stat().st_size, 'Body':io.BytesIO(b'corrupt')}
        return {'ContentLength':path.stat().st_size,'Body':path.open('rb')}

    def put_object(self, *, Bucket, Key, Body, IfNoneMatch, **kwargs):
        self.call('put')
        if IfNoneMatch!='*': raise AssertionError('conditional write required')
        with self.object(Key).open('xb') as out:
            if isinstance(Body,bytes):out.write(Body)
            else:shutil.copyfileobj(Body,out,256*1024)
        return {}

    def create_multipart_upload(self, **kwargs):
        self.call('create'); (self.root/'upload').mkdir()
        return {'UploadId':'opaque-session'}

    def upload_part(self, *, PartNumber, Body, **kwargs):
        with self.lock:
            self.inflight+=1; self.peak=max(self.peak,self.inflight)
        try:
            self.call('part')
            if PartNumber==self.fail_part: raise OSError('interrupted')
            (self.root/'upload'/str(PartNumber)).write_bytes(Body)
            # Not an MD5, SHA256, or a provider checksum capability.
            return {'ETag':f'opaque-etag-{PartNumber}'}
        finally:
            with self.lock: self.inflight-=1

    def list_parts(self, **kwargs):
        self.call('list')
        return {'IsTruncated':False,'Parts':[{'PartNumber':int(p.name),'Size':p.stat().st_size,
            'ETag':'opaque-etag-'+p.name} for p in (self.root/'upload').iterdir()]}

    def complete_multipart_upload(self, *, Key, MultipartUpload, **kwargs):
        self.call('complete')
        with self.object(Key).open('xb') as out:
            for part in MultipartUpload['Parts']:
                with (self.root/'upload'/str(part['PartNumber'])).open('rb') as source:
                    shutil.copyfileobj(source,out,256*1024)
        return {}

    def abort_multipart_upload(self, **kwargs):
        self.call('abort'); shutil.rmtree(self.root/'upload')


def fixture(root, count, size):
    root.mkdir()
    unit,remainder=divmod(size,count)
    for number in range(count):
        path=root/f'camera-state/{number:06}.bin'
        path.parent.mkdir(exist_ok=True)
        # Bytes vary per file, and need not be compressible for the uncompressed transport.
        block=bytes([number%251])*min(unit+1,256*1024)
        remaining=unit+(number<remainder)
        with path.open('wb') as out:
            while remaining:
                chunk=block[:remaining];out.write(chunk);remaining-=len(chunk)


class ArchiveTests(unittest.TestCase):
    def test_deterministic_roundtrip_existing_store_bytes_and_no_overwrite(self):
        with tempfile.TemporaryDirectory() as root:
            root=Path(root); source=root/'source'; store=LocalStore(source/'store')
            key=store.put(b'original camera bytes');(source/'episode.json').write_bytes(b'{"clock":"native"}\n')
            first=a.seal_directory(source,root/'one.tar')
            second=a.seal_directory(source,root/'two.tar')
            self.assertEqual(first['archive'],second['archive'])
            with self.assertRaises(FileExistsError):a.seal_directory(source,root/'one.tar')
            restored=a.unpack_archive(root/'one.tar',first,root/'restored')
            self.assertEqual(LocalStore(restored/'store').get(key),b'original camera bytes')
            self.assertEqual((restored/'episode.json').read_bytes(),(source/'episode.json').read_bytes())
            self.assertEqual(list(source.rglob('*.tar')),[])

    def test_source_and_staging_preserved_on_failed_commit(self):
        with tempfile.TemporaryDirectory() as root:
            root=Path(root);fixture(root/'source',3,100)
            descriptor=a.seal_directory(root/'source',root/'episode.tar')
            with self.assertRaises(OSError):
                a.stage_archive(root/'episode.tar',descriptor,root/'stage',run_id='attempt',
                    commit=lambda:(_ for _ in ()).throw(OSError('commit failed')))
            self.assertTrue((root/'episode.tar').exists());self.assertTrue((root/'source').exists())
            self.assertTrue((root/'stage'/'episode.tar').exists())
            self.assertFalse((root/'stage'/'stage.json').exists())
            events=[]
            staged=a.stage_archive(root/'episode.tar',descriptor,root/'stage-success',run_id='attempt',commit=lambda:events.append('committed'))
            self.assertEqual(events,['committed']);self.assertGreater(staged['durable_copy_seconds'],0)
            self.assertEqual(file_ref(root/'stage-success'/'episode.tar').to_dict(),descriptor['archive'])

    def test_paths_types_duplicates_missing_and_checksums_rejected(self):
        cases=[('../escape',b'x',tarfile.REGTYPE),('/escape',b'x',tarfile.REGTYPE),
               ('valid',b'x',tarfile.SYMTYPE),('valid',b'x',tarfile.LNKTYPE),
               ('valid',b'x',tarfile.CHRTYPE),('valid',b'x',tarfile.GNUTYPE_SPARSE),('valid',b'wrong',tarfile.REGTYPE),
               ('unknown',b'x',tarfile.REGTYPE),('duplicate',b'x',tarfile.REGTYPE),
               ('missing',b'x',tarfile.REGTYPE)]
        for name,data,kind in cases:
            with self.subTest(name=name,kind=kind),tempfile.TemporaryDirectory() as root:
                root=Path(root); manifest={'schema_version':1,'format':'coppertop-episode-tar',
                    'files':{'valid':{'sha256':digest(b'x'),'size':1}}};raw=canonical(manifest)
                archive=root/'bad.tar'
                with tarfile.open(archive,'w',format=tarfile.USTAR_FORMAT) as tar:
                    tar.addfile(a._header(a.MANIFEST,len(raw)),io.BytesIO(raw))
                    if name!='missing':
                        member=a._header('valid' if name=='duplicate' else name,len(data));member.type=kind
                        tar.addfile(member,io.BytesIO(data) if kind==tarfile.REGTYPE else None)
                        if name=='duplicate':tar.addfile(a._header('valid',1),io.BytesIO(b'x'))
                descriptor={'schema_version':1,'format':'coppertop-episode-tar','archive':file_ref(archive).to_dict(),
                    'manifest_sha256':digest(raw),'file_count':1,'raw_bytes':1}
                with self.assertRaises(ValueError):a.unpack_archive(archive,descriptor,root/'out')
                self.assertFalse((root/'out').exists());self.assertTrue(archive.exists())
                self.assertFalse((root/'escape').exists())

    def test_symlink_source_and_destination_and_raw_limits(self):
        with tempfile.TemporaryDirectory() as root:
            root=Path(root);fixture(root/'source',2,20)
            (root/'source'/'link').symlink_to(root/'source'/'camera-state'/'000000.bin')
            with self.assertRaises(ValueError):a.seal_directory(root/'source',root/'episode.tar')
            (root/'source'/'link').unlink()
            with patch.object(a,'MAX_RAW_BYTES',10):
                with self.assertRaises(ValueError):a.seal_directory(root/'source',root/'episode.tar')
            self.assertFalse((root/'episode.tar').exists())

    def test_special_headers_rejected_before_tarfile_parser_and_source_scan(self):
        with tempfile.TemporaryDirectory() as root:
            root=Path(root);archive=root/'pax.tar'
            with tarfile.open(archive,'w',format=tarfile.PAX_FORMAT) as tar:
                member=tarfile.TarInfo(a.MANIFEST);member.size=2
                member.pax_headers={'comment':'untrusted metadata'}
                tar.addfile(member,io.BytesIO(b'{}'))
            descriptor={'schema_version':1,'format':'coppertop-episode-tar','archive':file_ref(archive).to_dict()}
            with patch.object(a.tarfile,'open',side_effect=AssertionError('general parser must not run')):
                with self.assertRaises(ValueError):a.unpack_archive(archive,descriptor,root/'out')
            fixture(root/'source',1,20)
            with patch.object(a,'MAX_RAW_BYTES',10),patch.object(a,'file_ref',side_effect=AssertionError('hash must not run')):
                with self.assertRaises(ValueError):a.seal_directory(root/'source',root/'source.tar')

    def test_resume_part_size_and_opaque_etag_guards(self):
        with tempfile.TemporaryDirectory() as root:
            root=Path(root);path=root/'payload';path.write_bytes(b'x')
            client=DiskS3(root/'remote');store=S3Store(client,'bucket')
            ref=file_ref(path)
            state=UploadState('bucket/'+store.key(ref.sha256),'existing',ref,32*1024**2)
            with patch('coppertop.s3.file_ref',side_effect=AssertionError('bound must precede source read')):
                with self.assertRaises(ValueError):store.multipart_file(ref,path,state=state)
            with patch.object(client,'upload_part',return_value={}):
                with self.assertRaises(UploadInterrupted):store.multipart_file(ref,path)
            self.assertNotIn('complete',client.calls)

    def test_interrupt_resume_corrupt_readback_and_completion_last(self):
        with tempfile.TemporaryDirectory() as root:
            root=Path(root);fixture(root/'source',10,33*1024**2)
            descriptor=a.seal_directory(root/'source',root/'episode.tar')
            client=DiskS3(root/'remote',.001);store=S3Store(client,'bucket')
            client.fail_part=2;saved=[]
            with patch('coppertop.s3.STREAMED_PUT_BYTES',0),self.assertRaises(UploadInterrupted):
                a.publish_archive(root/'episode.tar',descriptor,store,record={},persist=saved.append)
            self.assertNotIn('complete',client.calls);self.assertNotIn('put',client.calls)
            self.assertEqual(file_ref(root/'episode.tar').to_dict(),descriptor['archive'])
            state=UploadState.from_dict(saved[-1]);self.assertEqual(len(state.parts),1)
            client.fail_part=None;client.corrupt=True
            with self.assertRaises(UploadInterrupted):
                a.publish_archive(root/'episode.tar',descriptor,store,record={},state=state)
            self.assertNotIn('put',client.calls)
            client.corrupt=False
            result=a.publish_archive(root/'episode.tar',descriptor,store,record={},state=state)
            receipt=a.read_completion(store,result['report_key'])
            self.assertEqual(receipt['verified_sha256'],descriptor['archive']['sha256'])
            self.assertLess(client.calls.index('complete'),client.calls.index('put'))
            incomplete=LocalStore(root/'metadata'); bad=dict(receipt,verified_sha256='0'*64)
            with self.assertRaises(ValueError):a.read_completion(incomplete,incomplete.put(canonical(bad)))
            missing={key:value for key,value in receipt.items() if key!='record'}
            with self.assertRaises(ValueError):a.read_completion(incomplete,incomplete.put(canonical(missing)))

    def test_missing_retention_and_cleanup_order_contract(self):
        for value in (None,dict(POLICY,max_bytes=0),dict(POLICY,accept_ongoing_storage_charges=False),dict(POLICY,release_condition='automatic deletion')):
            with self.assertRaises(ValueError):validate_retention(value)
        self.assertEqual(cleanup_disposition(gpu_started=False,publication_verified=False,retention=None),'delete')
        self.assertEqual(cleanup_disposition(gpu_started=True,publication_verified=True,retention=None),'delete')
        with self.assertRaises(ValueError):cleanup_disposition(gpu_started=True,publication_verified=False,retention=None)
        self.assertEqual(cleanup_disposition(gpu_started=True,publication_verified=False,retention=POLICY),'retain_for_recovery')

    def test_retained_resource_size_and_ongoing_cost_accounting(self):
        resource={'provider':'modal','id':'vo-owned','name':'owned-stage'}
        measured=retention_report(resource,POLICY,rate_usd_per_gib_month='0.09',size_bytes=1024**3)
        self.assertEqual(measured['estimated_monthly_usd'],'0.09')
        self.assertEqual(measured['maximum_authorized_monthly_usd'],'0.18')
        self.assertTrue(measured['within_size_authorization']);self.assertFalse(measured['automatic_expiry'])
        over=retention_report(resource,POLICY,rate_usd_per_gib_month='0.09',size_bytes=2*1024**3+1)
        self.assertFalse(over['within_size_authorization'])
        unknown=retention_report(resource,POLICY,rate_usd_per_gib_month='0.09',size_error='TimeoutError')
        self.assertIsNone(unknown['size_bytes']);self.assertIsNone(unknown['estimated_monthly_usd'])
        self.assertEqual(unknown['size_error_type'],'TimeoutError')

    def test_archive_role_queries_and_legacy_denial(self):
        with tempfile.TemporaryDirectory() as root:
            fake=DiskS3(Path(root)/'remote')
            prefix='chords/mechanism/mechanism-abcdef0123456789'
            scope=SessionScope('mechanism_archive','coppertop',prefix)
            endpoint='https://'+'a'*32+'.r2.cloudflarestorage.com'
            client=BoundedClient(fake,scope,endpoint,1000,lambda:1)
            url=endpoint+'/coppertop/'+prefix+'/objects/aa/'+'a'*64
            for query,method in [('uploads','POST'),('uploadId=opaque&partNumber=1','PUT'),
                    ('uploadId=opaque&part-number-marker=0','GET'),('uploadId=opaque','POST'),('uploadId=opaque','DELETE')]:
                client.destination_guard(SimpleNamespace(url=url+'?'+query,method=method))
            for query,method in [('delete','POST'),('uploadId=opaque&partNumber=0','PUT'),('uploadId=opaque&extra=1','GET'),('', 'DELETE')]:
                with self.assertRaises(ValueError):client.destination_guard(SimpleNamespace(url=url+('?' if query else '')+query,method=method))
            legacy=BoundedClient(fake,SessionScope('mechanism_full','coppertop',prefix),endpoint,1000,lambda:1)
            with self.assertRaises(ValueError):legacy.create_multipart_upload(Bucket='coppertop',Key=prefix+'/objects/aa/'+'a'*64)

    def test_realistic_1939_file_147mib_request_memory_latency(self):
        with tempfile.TemporaryDirectory() as root:
            root=Path(root);fixture(root/'source',1939,154493961)
            descriptor=a.seal_directory(root/'source',root/'episode.tar')
            staged=a.stage_archive(root/'episode.tar',descriptor,root/'stage',run_id='benchmark',commit=lambda:None)
            client=DiskS3(root/'remote',.02);store=S3Store(client,'bucket')
            tracemalloc.start(); started=time.monotonic()
            with patch('coppertop.s3.STREAMED_PUT_BYTES',0):
                result=a.publish_archive(root/'stage'/'episode.tar',staged,store,record={'synthetic':True})
            elapsed=time.monotonic()-started;peak=tracemalloc.get_traced_memory()[1];tracemalloc.stop()
            parts=(descriptor['archive']['size']+a.PART_BYTES-1)//a.PART_BYTES
            self.assertEqual(client.calls.count('part'),parts)
            self.assertEqual(client.calls.count('get'),2) # full archive + small receipt
            self.assertEqual(len(client.calls),parts+9)
            self.assertLessEqual(client.peak,4);self.assertLess(peak,90*1024**2)
            self.assertEqual(set(result['timings']),{'seal_seconds','durable_copy_seconds','publisher_prep_seconds',
                'source_check_seconds','first_upload_seconds','upload_seconds','readback_seconds','receipt_seconds'})
            restored=a.unpack_archive(root/'stage'/'episode.tar',staged,root/'restored')
            self.assertEqual(sum(p.stat().st_size for p in restored.rglob('*') if p.is_file()),154493961)
            print(json.dumps({'synthetic_archive_benchmark':{'files':1939,'raw_bytes':154493961,
                'archive_bytes':descriptor['archive']['size'],'requests':len(client.calls),'parts':parts,
                'concurrency_peak':client.peak,'python_peak_bytes':peak,'simulated_request_latency_seconds':.02,
                'publisher_seconds':elapsed,'timings':result['timings']}}))

    def test_supported_worst_case_6000_files_1gib(self):
        with tempfile.TemporaryDirectory() as root:
            root=Path(root);fixture(root/'source',6000,1024**3)
            descriptor=a.seal_directory(root/'source',root/'episode.tar')
            self.assertLessEqual(descriptor['archive']['size'],a.MAX_ARCHIVE_BYTES)
            client=DiskS3(root/'remote'); store=S3Store(client,'bucket')
            tracemalloc.start()
            result=a.publish_archive(root/'episode.tar',descriptor,store,record={'synthetic':True})
            peak=tracemalloc.get_traced_memory()[1];tracemalloc.stop()
            self.assertLess(peak,90*1024**2);self.assertEqual(client.calls.count('part'),65)
            self.assertEqual(len(client.calls),74)
            print(json.dumps({'synthetic_archive_worst_case':{'files':6000,'raw_bytes':1024**3,
                'archive_bytes':descriptor['archive']['size'],'requests':len(client.calls),
                'python_peak_bytes':peak,'timings':result['timings']}}))
