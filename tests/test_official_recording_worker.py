"""CPU launch contracts; fake processes never load models or allocate resources."""
import json
import os
import pickle
import shutil
from pathlib import Path
import socket
import subprocess
import sys
import tempfile
import unittest
from types import SimpleNamespace
from unittest.mock import Mock, patch

from coppertop import official_recording_worker as worker, mechanism_cloud
from coppertop.chords import LocalStore,canonical,digest
from coppertop.performance_runtime import publish_listener_ready
from test_mechanism_cloud import ledger, ATTEMPT
from test_modal_reload import App, Volume


class WorkerTests(unittest.TestCase):
    def test_plan_and_preparation_work_without_scientific_imports(self):
        script="""
import builtins
original=builtins.__import__
def guarded(name,*args,**kw):
    if name.split('.')[0] in ('numpy','torch','av','pyarrow'): raise AssertionError(name)
    return original(name,*args,**kw)
builtins.__import__=guarded
from coppertop.official_recording_worker import plan
assert plan()['acceptance']['limits']['query_calls']==1
assert plan(full=True)['acceptance']['limits']['query_calls']==38
"""
        result=subprocess.run([sys.executable,'-c',script],capture_output=True,timeout=30)
        self.assertEqual(result.returncode,0,result.stderr.decode())
        value=ledger();value['cumulative_cap']='70.82';value['apps']['ap-old']['reservation_usd']='64.81459248'
        with patch.object(mechanism_cloud,'mint',side_effect=AssertionError),patch.object(mechanism_cloud,'selected_env',side_effect=AssertionError):
            prepared=mechanism_cloud.prepare(value,ATTEMPT,'official_recording')
        self.assertEqual(prepared['cloud_plan']['cumulative_cap_usd'],'70.82')
        self.assertEqual(prepared['cloud_plan']['worker']['acceptance']['limits']['query_calls'],1)
        with self.assertRaises(ValueError):mechanism_cloud.prepare(value,ATTEMPT,'unknown')
        with self.assertRaises(ValueError):mechanism_cloud.prepare(ledger(),ATTEMPT,'official_recording')
        specs=mechanism_cloud.create_functions(SimpleNamespace(__version__='1.6.1',App=App),prepared,object(),Volume([]),Mock(),['temporary']).app.definitions
        self.assertEqual(specs['evaluate']['secrets'],[])
        self.assertEqual(specs['evaluate']['timeout'],270)
        self.assertIsNone(specs['publish']['gpu'])
        self.assertEqual(specs['publish']['secrets'],['temporary'])

    def test_cloud_invokes_only_recording_worker_and_rejects_wrong_query_count(self):
        value=ledger();value['cumulative_cap']='70.82'
        for calls in (1,2):
            with self.subTest(calls=calls),tempfile.TemporaryDirectory() as root:
                mount=Path(root)/'volume';mount.mkdir();volume=Volume([])
                attempt='mechanism-'+digest(root.encode())[:16]
                prepared=mechanism_cloud.prepare(value,attempt,'official_recording')
                self.addCleanup(shutil.rmtree,Path('/tmp')/(attempt+'-outputs'),True)
                def runner(argv,**options):
                    self.assertEqual(argv[2],'coppertop.official_recording_worker')
                    self.assertEqual(argv[-2:],['--run-id',attempt])
                    target=Path(argv[argv.index('--output')+1]);target.mkdir()
                    self.assertNotEqual(target,mount/'outputs')
                    LocalStore(target/'store').put(b'raw-state')
                    (target/'result.json').write_bytes(canonical({'status':'recording_acceptance','query_calls':calls}))
                    return SimpleNamespace(returncode=0)
                functions=mechanism_cloud.create_functions(SimpleNamespace(__version__='1.6.1',App=App),
                    prepared,object(),volume,lambda role:LocalStore(Path(root)/'destination'),[],runner)
                native_link=os.link
                def link(source,destination):
                    if Path(destination).is_relative_to(mount): raise PermissionError('Volume rejects hard links')
                    return native_link(source,destination)
                with patch.object(mechanism_cloud,'MOUNT',str(mount)),patch.dict('os.environ',{'MODAL_TASK_ID':'ta-gpu'}),patch('coppertop.chords.os.link',side_effect=link):
                    if calls==1:
                        receipt=functions.evaluate()
                        self.assertEqual(receipt['status'],'passed')
                        self.assertLessEqual(len(canonical(receipt)),1024)
                        self.assertLessEqual(len(pickle.dumps(receipt)),1024)
                        self.assertNotIn('worker_plan',receipt)
                        committed=json.loads((mount/'record.json').read_bytes())
                        self.assertEqual(receipt['record_sha256'],digest(canonical(committed)))
                        owned=mount/attempt;owned.mkdir()
                        for path in (mount/'record.json',mount/'outputs'):path.rename(owned/path.name)
                        published=functions.publish(receipt)
                        self.assertEqual(published['report_key'],receipt['record_sha256'])
                        self.assertEqual(LocalStore(Path(root)/'destination').get(digest(b'raw-state')),b'raw-state')
                        tampered=dict(receipt,record_sha256='0'*64)
                        with self.assertRaises(ValueError):functions.publish(tampered)
                        self.assertEqual(volume.events,['commit'])
                    else:
                        with self.assertRaises(ValueError):functions.evaluate()
                        self.assertEqual(volume.events,[])

    def test_full_plan_and_large_publication_use_compact_inventory(self):
        from coppertop.s3 import S3Store
        from coppertop.streams import read_small
        from test_record_archive import DiskS3
        from coppertop.record_archive import read_completion, unpack_archive
        from coppertop.multipart import UploadInterrupted
        value=ledger();value['cumulative_cap']='70.82'
        prepared=mechanism_cloud.prepare(value,ATTEMPT,'official_recording_full')
        plan=prepared['cloud_plan']
        self.assertEqual(plan['additional_cap_usd'],'8')
        self.assertEqual(prepared['scope'].role,'mechanism_archive')
        self.assertEqual(plan['worker']['acceptance']['limits']['query_calls'],38)
        with tempfile.TemporaryDirectory() as root:
            mount=Path(root)/'volume';mount.mkdir();volume=Volume([])
            attempt='mechanism-'+digest(root.encode())[:16]
            prepared=mechanism_cloud.prepare(value,attempt,'official_recording_full')
            self.addCleanup(shutil.rmtree,Path('/tmp')/(attempt+'-outputs'),True)
            self.addCleanup((Path('/tmp')/(attempt+'-episode.tar')).unlink,missing_ok=True)
            raw=b'x'*(5*1024**2)
            key=digest(raw)
            def runner(argv,**options):
                self.assertIn('--full',argv);self.assertEqual(options['timeout'],560)
                target=Path(argv[argv.index('--output')+1]);target.mkdir()
                from coppertop.streams import ObjectRef,CHUNK_BYTES
                local=LocalStore(target/'store')
                local.put_stream(ObjectRef(key,len(raw)),(raw[i:i+CHUNK_BYTES] for i in range(0,len(raw),CHUNK_BYTES)))
                # More entries than fit in the inline report; tiny fixture payloads.
                for i in range(1000):local.put(str(i).encode())
                (target/'result.json').write_bytes(canonical({'status':'recording_acceptance',
                    'query_calls':3,'controls':33,'cadence':16,'horizon':600}))
                return SimpleNamespace(returncode=0)
            native_link=os.link
            def link(source,destination):
                if Path(destination).is_relative_to(mount): raise PermissionError('Volume rejects hard links')
                return native_link(source,destination)
            client=DiskS3(Path(root)/'remote');store=S3Store(client,'coppertop',prefix=prepared['scope'].prefix)
            functions=mechanism_cloud.create_functions(SimpleNamespace(__version__='1.6.1',App=App),
                prepared,object(),volume,lambda role:store,[],runner)
            with patch.object(mechanism_cloud,'MOUNT',str(mount)),patch.dict('os.environ',{'MODAL_TASK_ID':'ta-gpu'}),patch('coppertop.chords.os.link',side_effect=link):
                receipt=functions.evaluate();self.assertLess(len(pickle.dumps(receipt)),1024)
                record=json.loads((mount/'record.json').read_bytes())
                self.assertNotIn('objects',record);self.assertLess(len(canonical(record)),65536)
                self.assertEqual(record['object_count'],1002)
                owned=mount/attempt;owned.mkdir()
                for path in (mount/'record.json',mount/'archive'):path.rename(owned/path.name)
                client.fail_part=1
                with patch('coppertop.s3.STREAMED_PUT_BYTES',0), self.assertRaises(UploadInterrupted):
                    functions.publish(receipt)
                state=json.loads((owned/'upload-state.json').read_bytes())
                self.assertEqual(state['upload_id'],'opaque-session')
                self.assertEqual(client.calls.count('create'),1)
                client.fail_part=None
                published=functions.publish(receipt)
                self.assertEqual(client.calls.count('create'),1)
                completion=read_completion(store,published['report_key'])
                restored=unpack_archive(owned/'archive'/'episode.tar',completion['archive'],Path(root)/'restored')
                self.assertEqual(read_small(LocalStore(restored/'store'),key,64*1024**2),raw)
                self.assertTrue(published['archive_verified']);self.assertTrue((owned/'upload-state.json').is_file())
                # Changed sealed bytes are rejected before remote publication.
                (owned/'archive'/'episode.tar').write_bytes(b'corrupt')
                with self.assertRaises(ValueError):functions.publish(receipt)

    def test_full_recovery_rejects_aggregate_before_any_write(self):
        with tempfile.TemporaryDirectory() as root:
            objects={digest(str(i).encode()):1 for i in range(5999)}
            destination=Mock()
            with patch.object(mechanism_cloud,'output_inventory',return_value=objects):
                with self.assertRaisesRegex(ValueError,'Recovery publication exceeds'):
                    mechanism_cloud.publish_directory(root,{'objects':objects},destination,full=True)
            destination.put.assert_not_called();destination.put_bounded.assert_not_called()

    def test_full_worker_binds_horizon_and_stops_server(self):
        with tempfile.TemporaryDirectory() as root:
            output=Path(root)/'output';process=Mock();process.poll.return_value=None
            def spawn(argv,**options):
                self.assertIn('--full-recording',argv)
                (output/'ready.json').write_text('{}');return process
            perform=Mock();validate=Mock(return_value={'status':'recording_acceptance'})
            with patch.object(worker,'stop_owned') as stop:
                result=worker.execute(output,digest(canonical(worker.plan(full=True))),
                    run_id=ATTEMPT,popen=spawn,perform=perform,validate=validate,full=True)
            self.assertEqual(perform.call_args.args[0].record_horizon,600)
            self.assertEqual(perform.call_args.args[0].replan_steps,16)
            validate.assert_called_once_with(output,full=True);stop.assert_called_once_with(process)
            self.assertGreaterEqual(result['worker_elapsed_seconds'],0)

    def test_readiness_requires_successful_listener(self):
        with tempfile.TemporaryDirectory() as root:
            path=Path(root)/'ready.json';module=SimpleNamespace()
            publish_listener_ready(module,path,{'host':'127.0.0.1'})
            with module.socket.socket(socket.AF_INET,socket.SOCK_STREAM) as listener:
                listener.bind(('127.0.0.1',0))
                self.assertFalse(path.exists())
                listener.listen(1)
                self.assertEqual(json.loads(path.read_bytes()),{'host':'127.0.0.1'})
            path.unlink()
            with module.socket.socket(socket.AF_INET,socket.SOCK_STREAM) as listener:
                listener.close()
                with self.assertRaises(OSError):listener.listen(1)
                self.assertFalse(path.exists())

    def test_worker_always_stops_owned_server_and_sanitizes_failure(self):
        for fail in (False,True):
            with self.subTest(fail=fail),tempfile.TemporaryDirectory() as root:
                output=Path(root)/'output';process=Mock();process.poll.return_value=None
                def spawn(*a,**kw):
                    (output/'ready.json').write_text('{}');return process
                perform=Mock(side_effect=ValueError('SECRET=do-not-log') if fail else None)
                with patch.object(worker,'stop_owned') as stop:
                    if fail:
                        with self.assertRaises(ValueError):worker.execute(output,digest(canonical(worker.plan())),run_id=ATTEMPT,popen=spawn,perform=perform,validate=lambda _: {})
                        self.assertNotIn('SECRET',(output/'failure.json').read_text())
                        self.assertEqual(json.loads((output/'failure.json').read_bytes())['failure']['component'],'official.evaluate_task')
                    else:
                        worker.execute(output,digest(canonical(worker.plan())),run_id=ATTEMPT,popen=spawn,perform=perform,validate=lambda _: {'status':'recording_acceptance'})
                    stop.assert_called_once_with(process)
                self.assertEqual(perform.call_count,1)
                self.assertEqual(perform.call_args.args[0].record_horizon,4)

    def test_bad_plan_creates_no_output_or_process(self):
        with tempfile.TemporaryDirectory() as root:
            output=Path(root)/'output';spawn=Mock(side_effect=AssertionError)
            with self.assertRaises(ValueError):worker.execute(output,'bad',run_id=ATTEMPT,popen=spawn)
            self.assertFalse(output.exists());spawn.assert_not_called()
