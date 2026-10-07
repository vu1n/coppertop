"""CPU launch contracts; fake processes never load models or allocate resources."""
import json
from pathlib import Path
import socket
import subprocess
import sys
import tempfile
import unittest
from types import SimpleNamespace
from unittest.mock import Mock, patch

from coppertop import official_recording_worker as worker, mechanism_cloud
from coppertop.chords import canonical,digest
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
        prepared=mechanism_cloud.prepare(value,ATTEMPT,'official_recording')
        for calls in (1,2):
            with self.subTest(calls=calls),tempfile.TemporaryDirectory() as root:
                mount=Path(root);volume=Volume([])
                def runner(argv,**options):
                    self.assertEqual(argv[2],'coppertop.official_recording_worker')
                    self.assertEqual(argv[-2:],['--run-id',ATTEMPT])
                    target=mount/'outputs';target.mkdir()
                    (target/'result.json').write_bytes(canonical({'status':'recording_acceptance','query_calls':calls}))
                    return SimpleNamespace(returncode=0)
                functions=mechanism_cloud.create_functions(SimpleNamespace(__version__='1.6.1',App=App),
                    prepared,object(),volume,Mock(),[],runner)
                with patch.object(mechanism_cloud,'MOUNT',str(mount)),patch.dict('os.environ',{'MODAL_TASK_ID':'ta-gpu'}):
                    if calls==1:
                        self.assertEqual(functions.evaluate()['status'],'passed')
                        self.assertEqual(volume.events,['commit'])
                    else:
                        with self.assertRaises(ValueError):functions.evaluate()
                        self.assertEqual(volume.events,[])

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
