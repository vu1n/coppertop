import json
import tempfile
import unittest
from datetime import datetime,timezone
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock,patch
from coppertop.gpu_launch import historical_total,preflight,launch,stop_owned,prepare_cached,check_sdk

NOW=datetime(2026,10,5,tzinfo=timezone.utc)
APPROVAL={'provider':'modal','paid':True,'input_upload':True,'output_retrieval':True,'additional_usd':'8','overall_usd':'20'}
RECEIPT={'status':'passed','recipe_sha256':'recipe','image_id':'im-tested',
 'snapshot_verified':True,'imports_verified':True,'processor_parity_verified':True,'entrypoint_verified':True}
BILLING={'fetched_at_utc':NOW.isoformat(),'ledger':{'schema_version':2,'unbounded_spend':[],
 'apps':{'old':{'state':'unsettled_bounded','known_reported_usd':'.2','reservation_usd':'3'}}}}

class LaunchTests(unittest.TestCase):
 def test_historical_reservations_are_not_released(self):
  self.assertEqual(str(historical_total(BILLING['ledger'])),'3')
 def test_unapproved_stale_or_unvalidated_never_launches(self):
  for receipt,approval,billing in [(RECEIPT,{},BILLING),({**RECEIPT,'imports_verified':False},APPROVAL,BILLING),
    (RECEIPT,APPROVAL,{**BILLING,'fetched_at_utc':'2026-10-04T00:00:00Z'})]:
   with self.assertRaises(ValueError):preflight(receipt,approval,billing,'recipe',NOW)
 def test_cap_includes_old_holds(self):
  ledger={'schema_version':2,'unbounded_spend':[],'apps':{'old':{'state':'unsettled_bounded','known_reported_usd':'1','reservation_usd':'13'}}}
  with self.assertRaises(ValueError):preflight(RECEIPT,APPROVAL,{**BILLING,'ledger':ledger},'recipe',NOW)
 def fixture(self):
  sdk=Mock();sdk.__version__='1.6.1';app=SimpleNamespace(app_id='ap-test');sb=Mock(object_id='sb-test')
  sdk.Sandbox.create.return_value=sb;sdk.Sandbox.list.return_value=[sb]
  sb.poll.side_effect=lambda: 0 if sb.terminate.called else None
  sb.exec.return_value=Mock(returncode=1)
  return sdk,app,sb
 def test_failed_job_stops_and_retains_reservation_no_retry(self):
  sdk,app,sb=self.fixture()
  with tempfile.TemporaryDirectory() as d:
   root=Path(d);reserve=root/'hold.json'
   with patch('coppertop.gpu_launch.release_objects',return_value=('a',)),patch('coppertop.gpu_launch.object_path',return_value='objects/a'):
    with self.assertRaises(RuntimeError):launch(sdk,app,RECEIPT,APPROVAL,BILLING,'recipe',Mock(),root/'out',reserve,now=NOW,sleep=lambda _:None)
   self.assertEqual(json.loads(reserve.read_text())['reservation_usd'],'8')
   self.assertEqual(sdk.Sandbox.create.call_count,1);sb.terminate.assert_called_once_with(wait=False)
   options=sdk.Sandbox.create.call_args.kwargs
   self.assertEqual((options['timeout'],options['cpu'],options['memory']),(900,(4,4),(65536,65536)))
   self.assertTrue(options['block_network']);self.assertEqual(options['secrets'],[])
   self.assertIsNone(options['idle_timeout'])
   self.assertTrue(json.loads((root/'out/launch.json').read_text())['shutdown']['stopped'])
 def test_create_ack_loss_discovers_and_stops_owned_job(self):
  sdk,app,sb=self.fixture();sdk.Sandbox.create.side_effect=RuntimeError('lost acknowledgement')
  with tempfile.TemporaryDirectory() as d:
   root=Path(d)
   with patch('coppertop.gpu_launch.release_objects',return_value=()):
    with self.assertRaises(RuntimeError):launch(sdk,app,RECEIPT,APPROVAL,BILLING,'recipe',Mock(),root/'out',root/'hold',now=NOW,sleep=lambda _:None)
   sb.terminate.assert_called_once_with(wait=False)
 def test_shutdown_failure_cannot_report_success(self):
  sdk,app,sb=self.fixture();sb.poll.side_effect=None;sb.poll.return_value=None
  with self.assertRaises(RuntimeError):stop_owned(sdk,app,sb,{'coppertop-smoke':'test'},sleep=lambda _:None)
  self.assertEqual(sb.terminate.call_count,5)
 def test_upload_failure_still_terminates(self):
  sdk,app,sb=self.fixture();sb.filesystem.write_bytes.side_effect=OSError('upload failed')
  with tempfile.TemporaryDirectory() as d:
   root=Path(d)
   with patch('coppertop.gpu_launch.release_objects',return_value=('a',)),patch('coppertop.gpu_launch.object_path',return_value='objects/a'):
    with self.assertRaises(OSError):launch(sdk,app,RECEIPT,APPROVAL,BILLING,'recipe',Mock(),root/'out',root/'hold',now=NOW,sleep=lambda _:None)
   sb.terminate.assert_called_once_with(wait=False);sb.exec.assert_not_called()

 def test_shutdown_discovery_error_still_requests_known_termination(self):
  sdk,app,sb=self.fixture();sdk.Sandbox.list.side_effect=OSError('discovery unavailable')
  with self.assertRaises(OSError):stop_owned(sdk,app,sb,{'coppertop-smoke':'test'},sleep=lambda _:None)
  sb.terminate.assert_called_once_with(wait=False)
 def test_success_retrieves_only_bounded_checksummed_outputs(self):
  import hashlib
  from coppertop.gpu_smoke import RELEASE
  sdk,app,sb=self.fixture();sb.exec.return_value.returncode=0
  head=b'head-delta';digest=hashlib.sha256(head).hexdigest()
  files={'head.safetensors':head,'result.json':json.dumps({'status':'passed','release':RELEASE,'checkpoint_sha256':digest}).encode(),
    'head-manifest.json':json.dumps({'release':RELEASE,'checkpoint_sha256':digest}).encode()}
  sb.filesystem.stat.side_effect=lambda name:SimpleNamespace(size=len(files[name.rsplit('/',1)[1]]))
  sb.filesystem.copy_to_local.side_effect=lambda remote,dest:Path(dest).write_bytes(files[remote.rsplit('/',1)[1]])
  with tempfile.TemporaryDirectory() as d:
   root=Path(d)
   with patch('coppertop.gpu_launch.release_objects',return_value=()):
    result=launch(sdk,app,RECEIPT,APPROVAL,BILLING,'recipe',Mock(),root/'out',root/'hold',now=NOW,sleep=lambda _:None)
   self.assertEqual(result['status'],'passed');self.assertTrue(result['shutdown']['stopped'])
   self.assertEqual(sb.filesystem.copy_to_local.call_count,3)
   self.assertEqual(result['runtime_status'],'passed');self.assertEqual(result['retrieval_status'],'verified')
 def test_oversized_result_never_copies_and_stops(self):
  sdk,app,sb=self.fixture();sb.exec.return_value.returncode=0
  sb.filesystem.stat.return_value=SimpleNamespace(size=65537)
  with tempfile.TemporaryDirectory() as d:
   root=Path(d)
   with patch('coppertop.gpu_launch.release_objects',return_value=()):
    with self.assertRaises(ValueError):launch(sdk,app,RECEIPT,APPROVAL,BILLING,'recipe',Mock(),root/'out',root/'hold',now=NOW,sleep=lambda _:None)
   sb.filesystem.copy_to_local.assert_not_called();sb.terminate.assert_called_once_with(wait=False)

 def cpu_fixture(self,root):
  import hashlib
  project=root/'project';(project/'containers').mkdir(parents=True);(project/'coppertop').mkdir()
  evaluator=root/'evaluator.py';data=b'official';evaluator.write_bytes(data)
  pin={'evaluator_git_blob':hashlib.sha1(b'blob 8\0'+data).hexdigest()}
  (project/'containers/gpu-smoke-pins.json').write_text(json.dumps(pin))
  (project/'containers/Dockerfile.gpu-smoke').write_text('ENTRYPOINT []')
  (project/'requirements-gpu-smoke.lock').write_text('')
  (project/'coppertop/runtime.py').write_text('')
  return project,evaluator
 def test_cpu_gate_failure_never_allocates_gpu_and_retains_hold(self):
  sdk,app,sb=self.fixture()
  with tempfile.TemporaryDirectory() as d:
   root=Path(d);project,evaluator=self.cpu_fixture(root)
   with patch('coppertop.gpu_launch.release_objects',return_value=()):
    with self.assertRaises(RuntimeError):prepare_cached(sdk,app,'im-cache',APPROVAL,BILLING,project,evaluator,Mock(),root/'cpu',root/'hold',now=NOW,sleep=lambda _:None)
   compile(sb.exec.call_args.args[2],'<bounded-cpu-bootstrap>','exec')
   options=sdk.Sandbox.create.call_args.kwargs
   self.assertEqual((options['gpu'],options['timeout'],options['cpu'],options['memory']),(None,1200,(4,4),(16384,16384)))
   self.assertEqual(json.loads((root/'hold').read_text())['reservation_usd'],'8')
   sb.snapshot_filesystem.assert_not_called();sb.terminate.assert_called_once_with(wait=False)
 def test_cpu_success_snapshots_only_after_validation_then_stops(self):
  from coppertop.image_check import recipe_digest
  sdk,app,sb=self.fixture();sb.exec.return_value.returncode=0
  with tempfile.TemporaryDirectory() as d:
   root=Path(d);project,evaluator=self.cpu_fixture(root)
   receipt={**RECEIPT,'recipe_sha256':recipe_digest(project),'image_id':'im-cache'}
   content=json.dumps(receipt).encode()
   sb.filesystem.stat.return_value=SimpleNamespace(size=len(content))
   sb.filesystem.copy_to_local.side_effect=lambda remote,dest:Path(dest).write_bytes(content)
   sb.snapshot_filesystem.return_value=SimpleNamespace(object_id='im-prepared')
   with patch('coppertop.gpu_launch.release_objects',return_value=()):
    got=prepare_cached(sdk,app,'im-cache',APPROVAL,BILLING,project,evaluator,Mock(),root/'cpu',root/'hold',now=NOW,sleep=lambda _:None)
   self.assertEqual(got['image_id'],'im-prepared')
   self.assertEqual(json.loads((root/'hold').read_text())['stage'],'cpu_passed')
   sb.snapshot_filesystem.assert_called_once_with(timeout=30,ttl=3600)
   sb.terminate.assert_called_once_with(wait=False)

 def test_installed_older_sdk_fails_before_provider_calls(self):
  sdk=Mock();sdk.__version__='1.4.3'
  with self.assertRaises(ValueError):check_sdk(sdk)
  sdk.Sandbox.create.assert_not_called()


class RetrievalFailureTests(unittest.TestCase):
 fixture = LaunchTests.fixture
 def test_missing_checkpoint_stat_or_copy_preserves_runtime_evidence(self):
  from coppertop.gpu_smoke import RELEASE
  for failed_phase in ('artifact_stat','artifact_copy'):
   with self.subTest(failed_phase=failed_phase), tempfile.TemporaryDirectory() as d:
    sdk,app,sb=self.fixture();sb.exec.return_value.returncode=0
    files={'result.json':json.dumps({'status':'passed','release':RELEASE}).encode(),
      'head-manifest.json':b'{}','head.safetensors':b'head'}
    def stat(remote):
     name=remote.rsplit('/',1)[1]
     if name=='head.safetensors' and failed_phase=='artifact_stat':raise FileNotFoundError('private-secret-like-detail')
     return SimpleNamespace(size=len(files[name]))
    def copy(remote,dest):
     name=remote.rsplit('/',1)[1]
     if name=='head.safetensors' and failed_phase=='artifact_copy':raise FileNotFoundError('private-secret-like-detail')
     Path(dest).write_bytes(files[name])
    sb.filesystem.stat.side_effect=stat;sb.filesystem.copy_to_local.side_effect=copy
    root=Path(d)
    with patch('coppertop.gpu_launch.release_objects',return_value=()):
     with self.assertRaises(FileNotFoundError):launch(sdk,app,RECEIPT,APPROVAL,BILLING,'recipe',Mock(),root/'out',root/'hold',now=NOW,sleep=lambda _:None)
    content=(root/'out/launch.json').read_text();record=json.loads(content)
    self.assertEqual(record['failed_phase'],failed_phase);self.assertEqual(record['artifact'],'head.safetensors')
    self.assertEqual(record['runtime_status'],'passed');self.assertEqual(record['retrieval_status'],'incomplete')
    self.assertTrue(record['shutdown']['stopped']);self.assertNotIn('private-secret-like-detail',content)
    self.assertEqual(sdk.Sandbox.create.call_count,1);self.assertFalse((root/'out/head.safetensors').exists())

 def test_bad_runtime_receipt_stops_before_checkpoint_transfer(self):
  sdk,app,sb=self.fixture();sb.exec.return_value.returncode=0
  payload=json.dumps({'status':'failed','release':'wrong'}).encode()
  sb.filesystem.stat.return_value=SimpleNamespace(size=len(payload))
  sb.filesystem.copy_to_local.side_effect=lambda remote,dest:Path(dest).write_bytes(payload)
  with tempfile.TemporaryDirectory() as d:
   root=Path(d)
   with patch('coppertop.gpu_launch.release_objects',return_value=()):
    with self.assertRaises(ValueError):launch(sdk,app,RECEIPT,APPROVAL,BILLING,'recipe',Mock(),root/'out',root/'hold',now=NOW,sleep=lambda _:None)
   self.assertEqual(sb.filesystem.copy_to_local.call_count,1)
   self.assertTrue(json.loads((root/'out/launch.json').read_text())['shutdown']['stopped'])

 def test_checksum_failure_does_not_claim_retrieval_verified(self):
  from coppertop.gpu_smoke import RELEASE
  sdk,app,sb=self.fixture();sb.exec.return_value.returncode=0
  files={'head.safetensors':b'corrupted',
   'result.json':json.dumps({'status':'passed','release':RELEASE,'checkpoint_sha256':'0'*64}).encode(),
   'head-manifest.json':json.dumps({'release':RELEASE,'checkpoint_sha256':'0'*64}).encode()}
  sb.filesystem.stat.side_effect=lambda remote:SimpleNamespace(size=len(files[remote.rsplit('/',1)[1]]))
  sb.filesystem.copy_to_local.side_effect=lambda remote,dest:Path(dest).write_bytes(files[remote.rsplit('/',1)[1]])
  with tempfile.TemporaryDirectory() as d:
   root=Path(d)
   with patch('coppertop.gpu_launch.release_objects',return_value=()):
    with self.assertRaises(ValueError):launch(sdk,app,RECEIPT,APPROVAL,BILLING,'recipe',Mock(),root/'out',root/'hold',now=NOW,sleep=lambda _:None)
   record=json.loads((root/'out/launch.json').read_text())
   self.assertEqual(record['failed_phase'],'artifact_verify');self.assertEqual(record['runtime_status'],'passed')
   self.assertEqual(record['retrieval_status'],'incomplete');self.assertTrue(record['shutdown']['stopped'])
