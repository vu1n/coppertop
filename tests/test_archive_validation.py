"""Offline tests of approval, ownership, expiry and failure cleanup."""
import os
from pathlib import Path
import subprocess
import tempfile
import unittest
from types import SimpleNamespace
from unittest.mock import Mock, patch

from coppertop import archive_validation as v
from coppertop.multipart import UploadState
from coppertop.r2_session import SessionScope, TemporaryCredentials, mint, _session
from coppertop.s3 import S3Store
from coppertop import record_archive as a
from test_record_archive import DiskS3
from test_storage import S3Error


ENDPOINT='https://'+'a'*32+'.r2.cloudflarestorage.com'
PREFIX='chords/mechanism/mechanism-abcdef0123456789'


class CleanupFake:
    def __init__(self,*,completed=False):
        self.meta=SimpleNamespace(events=SimpleNamespace(register=lambda *args:None))
        self.objects=set();self.upload=not completed;self.calls=[]
    def abort_multipart_upload(self,**kwargs):
        self.calls.append('abort')
        if not self.upload: raise S3Error('NoSuchUpload')
        self.upload=False
    def list_parts(self,**kwargs):
        self.calls.append('list_parts')
        if not self.upload: raise S3Error('NoSuchUpload')
        return {}
    def delete_object(self,**kwargs):
        self.calls.append('delete');self.objects.discard(kwargs['Key'])
    def head_object(self,**kwargs):
        self.calls.append('head')
        if kwargs['Key'] not in self.objects: raise S3Error('NoSuchKey')
        return {'ContentLength':1}


class ArchiveValidationTests(unittest.TestCase):
    def setup_case(self,root,*,completed=False):
        source=root/'source';source.mkdir();(source/'one').write_bytes(b'preserve original')
        descriptor=a.seal_directory(source,root/'episode.tar')
        ref=a.archive_ref(descriptor);key=PREFIX+'/objects/'+ref.sha256[:2]+'/'+ref.sha256
        plan={'bucket':'coppertop','prefix':PREFIX,'descriptor':descriptor,
              'publication_requests_max':32,'remote_bytes_max':v.REMOTE_BYTES}
        fake=CleanupFake(completed=completed)
        scope=v.CleanupScope('archive_validation_cleanup','coppertop',PREFIX,(key,))
        client=v.CleanupClient(fake,scope,ENDPOINT,1200,300,clock=lambda:0,monotonic=lambda:0)
        state=UploadState('coppertop/'+key,'owned-upload',ref,a.PART_BYTES).to_dict()
        return plan,key,fake,client,state

    def test_timeout_stops_child_before_cleanup_and_aborts_upload(self):
        with tempfile.TemporaryDirectory() as root:
            root=Path(root);plan,key,fake,client,state=self.setup_case(root)
            runner=Mock(side_effect=subprocess.TimeoutExpired('publisher',600))
            result=v.run_publisher(['publisher'],{},runner=runner)
            self.assertTrue(result['timed_out'])
            self.assertEqual(runner.call_args.kwargs['timeout'],600)
            cleaned=v.cleanup_owned(client,plan,[key],state)
            self.assertTrue(cleaned['verified']);self.assertEqual(fake.calls,['abort','list_parts','delete','head'])
            self.assertEqual((root/'source'/'one').read_bytes(),b'preserve original')

    def test_uncertain_completion_and_receipt_failure_delete_exact_objects(self):
        for receipt_failed in (False,True):
            with self.subTest(receipt_failed=receipt_failed),tempfile.TemporaryDirectory() as root:
                root=Path(root);plan,key,fake,client,state=self.setup_case(root,completed=True)
                keys=[key];fake.objects.add(key)
                if receipt_failed:
                    receipt=PREFIX+'/objects/bb/'+'b'*64;keys.append(receipt);fake.objects.add(receipt)
                    scope=v.CleanupScope('archive_validation_cleanup','coppertop',PREFIX,tuple(keys))
                    client=v.CleanupClient(fake,scope,ENDPOINT,1200,300,clock=lambda:0,monotonic=lambda:0)
                cleaned=v.cleanup_owned(client,plan,keys,state)
                self.assertTrue(cleaned['verified']);self.assertFalse(fake.objects)

    def test_expired_credentials_preserve_source_and_report_cleanup_failure(self):
        with tempfile.TemporaryDirectory() as root:
            root=Path(root);plan,key,fake,client,state=self.setup_case(root)
            client.clock=lambda:1200
            result=v.cleanup_owned(client,plan,[key],state)
            self.assertFalse(result['verified']);self.assertEqual(fake.calls,[])
            self.assertEqual((root/'source'/'one').read_bytes(),b'preserve original')

    def test_lost_create_response_is_not_claimed_clean(self):
        with tempfile.TemporaryDirectory() as root:
            root=Path(root);plan,key,fake,client,state=self.setup_case(root)
            result=v.cleanup_owned(client,plan,[key],None)
            self.assertFalse(result['verified'])
            self.assertEqual(result['errors'][0]['error_type'],'UploadIdentityUnknown')

    def test_invalid_upload_journal_still_deletes_exact_objects(self):
        with tempfile.TemporaryDirectory() as root:
            root=Path(root);plan,key,fake,client,state=self.setup_case(root)
            fake.objects.add(key)
            result=v.cleanup_owned(client,plan,[key],{'untrusted':'malformed'})
            self.assertFalse(result['verified']);self.assertFalse(fake.objects)
            self.assertEqual(fake.calls,['delete','head'])
            with self.assertRaises(ValueError):v.save(root/'huge.json',{'upload_id':'x'*65536})
            self.assertFalse((root/'huge.json').exists())

    def test_exact_cleanup_scope_and_guard_deny_broader_access(self):
        key=PREFIX+'/objects/aa/'+'a'*64
        scope=v.CleanupScope('archive_validation_cleanup','coppertop',PREFIX,(key,))
        credential=mint('parent-id','parent-secret',endpoint=ENDPOINT,scope=scope,now=100,ttl_seconds=1200)
        _,expiry=_session(credential.environment(),ENDPOINT,scope,101)
        self.assertEqual(expiry,1300);self.assertEqual(scope.paths['prefixPaths'],[])
        client=v.CleanupClient(CleanupFake(),scope,ENDPOINT,1300,1000,clock=lambda:101,monotonic=lambda:0)
        url=ENDPOINT+'/coppertop/'+key
        for method,suffix in [('DELETE',''),('HEAD',''),('DELETE','?uploadId=owned'),('GET','?uploadId=owned')]:
            client.guard(SimpleNamespace(method=method,url=url+suffix))
        for method,suffix in [('GET',''),('PUT',''),('GET','?uploads'),('DELETE','?uploadId=owned&extra=x')]:
            with self.assertRaises(ValueError):client.guard(SimpleNamespace(method=method,url=url+suffix))
        with self.assertRaises(ValueError):client.call('delete_object',Bucket='coppertop',Key='unrelated')
        self.assertGreater(v.SESSION_SECONDS-v.EXECUTION_SECONDS-v.CLEANUP_SECONDS,0)

    def test_journal_precedes_receipt_put_even_when_response_fails(self):
        with tempfile.TemporaryDirectory() as root:
            root=Path(root);plan,key,fake,client,state=self.setup_case(root)
            fake=DiskS3(root/'remote');scope=SessionScope('mechanism_archive','coppertop',PREFIX)
            journal=v.JournalClient(fake,plan,root);store=S3Store(journal,'coppertop',prefix=PREFIX)
            def fail(**kwargs):
                owned=v.load(root/'owned-keys.json')['keys']
                self.assertIn(kwargs['Key'],owned)
                raise TimeoutError('uncertain receipt put')
            fake.put_object=fail
            with self.assertRaises(TimeoutError):store.put(b'receipt')
            self.assertEqual(len(v.load(root/'owned-keys.json')['keys']),1)

    def test_existing_destination_never_becomes_owned(self):
        with tempfile.TemporaryDirectory() as root:
            root=Path(root);plan,key,fake,client,state=self.setup_case(root)
            fake.objects.add(key);fake.create_multipart_upload=Mock()
            journal=v.JournalClient(fake,plan,root)
            with self.assertRaises(ValueError):journal.create_multipart_upload(Bucket='coppertop',Key=key)
            self.assertFalse((root/'owned-keys.json').exists());fake.create_multipart_upload.assert_not_called()
            store=S3Store(journal,'coppertop',prefix=PREFIX)
            with self.assertRaises(ValueError):store.multipart_file(a.archive_ref(plan['descriptor']),root/'episode.tar')
            self.assertFalse((root/'owned-keys.json').exists());fake.create_multipart_upload.assert_not_called()

    def test_prepare_and_approval_never_read_credentials_or_call_sdk(self):
        with tempfile.TemporaryDirectory() as root,patch.object(v,'sdk_client',side_effect=AssertionError('no SDK')):
            directory=Path(root)/'new'
            sha=v.prepare(directory);plan=v.load(directory/'plan.json');approval=v.load(directory/'approval.example.json')
            self.assertEqual(approval['plan_sha256'],sha)
            self.assertEqual(plan['descriptor']['archive']['size'],156098560)
            v.validate_plan(plan,directory)
            with self.assertRaises(ValueError):v.validate_approval(plan,approval)
            for key,value in list(approval.items()):
                if value is False:approval[key]=True
            v.validate_approval(plan,approval)
            approval['execute']=1
            with self.assertRaises(ValueError):v.validate_approval(plan,approval)
            with self.assertRaises(FileExistsError):v.prepare(directory)

    def test_controller_failure_paths_always_report_cleanup_and_preserve_source(self):
        for mode in ('timeout','uncertain_completion','receipt_failure','expired_cleanup','invalid_journal'):
            with self.subTest(mode=mode),tempfile.TemporaryDirectory() as root:
                directory=Path(root)/'attempt';v.prepare(directory)
                plan=v.load(directory/'plan.json');approval=v.load(directory/'approval.example.json')
                for key,value in list(approval.items()):
                    if value is False:approval[key]=True
                v.save(directory/'approval.json',approval)
                ref=a.archive_ref(plan['descriptor'])
                key=plan['prefix']+'/objects/'+ref.sha256[:2]+'/'+ref.sha256
                keys=[key];fake=CleanupFake(completed=mode in ('uncertain_completion','receipt_failure'))
                fake.objects.add(key)
                if mode=='receipt_failure':
                    receipt=plan['prefix']+'/objects/bb/'+'b'*64;keys.append(receipt);fake.objects.add(receipt)
                def stopped(*args,**kwargs):
                    v.save(directory/'owned-keys.json',{'keys':keys})
                    if mode=='invalid_journal':(directory/'upload-state.json').write_text('x'*70000)
                    else:v.save(directory/'upload-state.json',UploadState('coppertop/'+key,'owned-upload',ref,a.PART_BYTES).to_dict())
                    return {'exit_code':None,'timed_out':mode=='timeout'}
                def credentials(*args,**kwargs):
                    expired=mode=='expired_cleanup' and isinstance(kwargs['scope'],v.CleanupScope)
                    return TemporaryCredentials('dummy','dummy','dummy',0 if expired else 2**40)
                with patch('coppertop.reload_launch.selected_env',return_value={'CLOUDFLARE_R2_URL':ENDPOINT}),\
                     patch('coppertop.reload_launch.parent_credentials',return_value=('dummy','dummy')),\
                     patch.object(v,'run_publisher',side_effect=stopped),\
                     patch.object(v,'mint',side_effect=credentials) as signing,\
                     patch.object(v,'sdk_client',return_value=fake):
                    result=v.execute(directory,directory/'approval.json','never-read.env')
                self.assertEqual(len(list((directory/'source'/'camera-state').glob('*.bin'))),1939)
                self.assertTrue((directory/'result.json').exists())
                self.assertFalse(result['publication']['verified'])
                self.assertEqual(signing.call_count,2)
                self.assertEqual(signing.call_args.kwargs['scope'].paths['prefixPaths'],[])
                if mode=='expired_cleanup':
                    self.assertFalse(result['cleanup']['verified']);self.assertTrue(fake.objects)
                elif mode=='invalid_journal':
                    self.assertFalse(result['cleanup']['verified']);self.assertFalse(fake.objects)
                else:
                    self.assertTrue(result['cleanup']['verified']);self.assertFalse(fake.objects)
                with self.assertRaises(FileExistsError):
                    v.execute(directory,directory/'approval.json','never-read.env')

    def test_real_child_interface_publishes_and_reads_receipt_with_fake_transport(self):
        with tempfile.TemporaryDirectory() as root:
            root=Path(root);directory=root/'attempt';v.prepare(directory)
            plan=v.load(directory/'plan.json');approval=v.load(directory/'approval.example.json')
            for key,value in list(approval.items()):
                if value is False:approval[key]=True
            v.save(directory/'approval.json',approval)
            scope=SessionScope('mechanism_archive','coppertop',plan['prefix'])
            credentials=mint('dummy','dummy',endpoint=ENDPOINT,scope=scope,ttl_seconds=1200)
            fake=DiskS3(root/'fake-remote')
            with patch.object(v,'sdk_client',return_value=fake),patch.dict(os.environ,credentials.environment()):
                v.publish_child(directory,ENDPOINT,directory/'approval.json')
            result=v.load(directory/'publication.json')
            self.assertTrue(result['verified']);self.assertEqual(result['requests'],24)
            self.assertEqual(len(v.load(directory/'owned-keys.json')['keys']),2)
            self.assertEqual(fake.calls.count('part'),10)
            # One streamed full archive read; both receipt verifications are small.
            self.assertEqual(fake.calls.count('get'),3)


if __name__=='__main__':unittest.main()
