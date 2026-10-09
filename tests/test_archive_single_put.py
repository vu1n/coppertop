"""Offline native seekable-body publication; no provider calls."""
from pathlib import Path
import tempfile
import tracemalloc
import unittest
from unittest.mock import Mock

from coppertop import record_archive as a
from coppertop.r2_session import BoundedClient,SessionScope
from coppertop.s3 import S3Store,STREAMED_PUT_BYTES
from coppertop.streams import ObjectRef,file_ref
from coppertop.multipart import UploadState
from test_record_archive import DiskS3,fixture


ENDPOINT='https://'+'a'*32+'.r2.cloudflarestorage.com'
PREFIX='chords/mechanism/mechanism-abcdef0123456789'


class SinglePutTests(unittest.TestCase):
    def test_realistic_archive_uses_one_streamed_put_and_full_hash_readback(self):
        with tempfile.TemporaryDirectory() as root:
            root=Path(root);fixture(root/'source',1939,154493961)
            descriptor=a.seal_directory(root/'source',root/'episode.tar')
            fake=DiskS3(root/'remote')
            scope=SessionScope('mechanism_archive','coppertop',PREFIX)
            client=BoundedClient(fake,scope,ENDPOINT,1000,lambda:1)
            store=S3Store(client,'coppertop',prefix=PREFIX)
            tracemalloc.start()
            try:
                result=a.publish_archive(root/'episode.tar',descriptor,store,record={'synthetic':True})
                peak=tracemalloc.get_traced_memory()[1]
            finally:tracemalloc.stop()
            self.assertLess(peak,8*1024**2)
            self.assertEqual(len(fake.calls),7);self.assertEqual(fake.calls.count('put'),2)
            self.assertNotIn('create',fake.calls);self.assertNotIn('complete',fake.calls)
            self.assertEqual(fake.calls.count('get'),2)
            self.assertEqual(client.write_bytes,descriptor['archive']['size']+fake.object(store.key(result['report_key'])).stat().st_size)
            self.assertEqual(a.read_completion(store,result['report_key'])['verified_sha256'],descriptor['archive']['sha256'])
            self.assertEqual(file_ref(root/'episode.tar').to_dict(),descriptor['archive'])
            print({'single_put_local_filesystem_fake':{'archive_bytes':descriptor['archive']['size'],
                'publisher_requests':7,'python_peak_bytes':peak,'source_files':1939}})

    def test_uncertain_put_and_corrupt_readback_never_publish_receipt_or_retry(self):
        for failure in ('put_response','readback'):
            with self.subTest(failure=failure),tempfile.TemporaryDirectory() as root:
                root=Path(root);fixture(root/'source',1,128*1024)
                descriptor=a.seal_directory(root/'source',root/'episode.tar')
                fake=DiskS3(root/'remote');store=S3Store(fake,'bucket')
                events=[]
                if failure=='put_response':
                    original=fake.put_object
                    def lost(**kwargs):original(**kwargs);raise TimeoutError('response lost')
                    fake.put_object=lost
                else:fake.corrupt=True
                with self.assertRaises((TimeoutError,ValueError)):
                    a.publish_archive(root/'episode.tar',descriptor,store,record={},observe=events.append)
                self.assertEqual(fake.calls.count('put'),1)
                self.assertNotIn('create',fake.calls);self.assertNotIn('complete',fake.calls)
                self.assertFalse(any(e['stage']=='receipt_put' for e in events))
                self.assertEqual(file_ref(root/'episode.tar').to_dict(),descriptor['archive'])
                self.assertTrue((root/'source'/'camera-state'/'000000.bin').exists())

    def test_shared_threshold_and_saved_multipart_state_keep_resume_path(self):
        store=S3Store(Mock(),'bucket');store.put_file=Mock();store.multipart_file=Mock()
        for size in (STREAMED_PUT_BYTES,STREAMED_PUT_BYTES+1):
            ref=ObjectRef('a'*64,size);store.upload_file(ref,Path('unused'))
        self.assertEqual(store.put_file.call_count,1);self.assertEqual(store.multipart_file.call_count,1)
        ref=ObjectRef('a'*64,1024);state=UploadState('bucket/objects/aa/'+'a'*64,'upload',ref,a.PART_BYTES)
        store.upload_file(ref,Path('unused'),state=state)
        self.assertEqual(store.multipart_file.call_count,2)
        self.assertIs(store.multipart_file.call_args.kwargs['state'],state)

    def test_role_allows_only_exact_ordinary_file_length_and_keeps_receipt_bytes_bound(self):
        with tempfile.TemporaryDirectory() as root:
            root=Path(root);path=root/'file';path.write_bytes(b'contents')
            fake=DiskS3(root/'remote');scope=SessionScope('mechanism_archive','coppertop',PREFIX)
            client=BoundedClient(fake,scope,ENDPOINT,1000,lambda:1)
            key=PREFIX+'/objects/aa/'+'a'*64
            with path.open('rb') as body:
                for length in (True,7,9,STREAMED_PUT_BYTES+1):
                    with self.assertRaises(ValueError):
                        client.put_object(Bucket='coppertop',Key=key,Body=body,ContentLength=length,IfNoneMatch='*')
                body.seek(1)
                with self.assertRaises(ValueError):client.put_object(Bucket='coppertop',Key=key,Body=body,ContentLength=8,IfNoneMatch='*')
            with self.assertRaises(ValueError):client.put_object(Bucket='coppertop',Key=key,Body=b'x'*65537,IfNoneMatch='*')
            self.assertEqual(client.requests,0);self.assertEqual(fake.calls,[])


if __name__=='__main__':unittest.main()
