import io
import unittest
from unittest.mock import patch
from types import SimpleNamespace
from coppertop.colocation_worker import receive, send, worker_environment

class WorkerProtocolTests(unittest.TestCase):
    def test_missing_or_oversized_response_is_not_success(self):
        for data in ('', 'x'*65537):
            process=SimpleNamespace(stdout=io.StringIO(data))
            with patch('coppertop.colocation_worker.select.select',return_value=([process.stdout],[],[])):
                with self.assertRaises(ValueError):receive(process)
    def test_timeout_is_not_retried(self):
        process=SimpleNamespace(stdout=io.StringIO('{}\n'))
        with patch('coppertop.colocation_worker.select.select',return_value=([],[],[])) as wait:
            with self.assertRaises(TimeoutError):receive(process,1)
            self.assertEqual(wait.call_count,1)
    def test_case_message_round_trip(self):
        process=SimpleNamespace(stdin=io.StringIO(),stdout=io.StringIO('{"seconds":0.1}\n'))
        send(process,2)
        self.assertEqual(process.stdin.getvalue(),'{"index": 2}\n')
        with patch('coppertop.colocation_worker.select.select',return_value=([process.stdout],[],[])):
            self.assertEqual(receive(process),{'seconds':.1})


class EnvironmentTests(unittest.TestCase):
    def test_installer_and_worker_exclude_modal_dependency_injection(self):
        original={'PYTHONPATH':'/__modal/deps:/other','PYTHONHOME':'/system',
                  'PYTHONUSERBASE':'/user','VIRTUAL_ENV':'/wrong',
                  'LD_LIBRARY_PATH':'/cuda','CUDA_VISIBLE_DEVICES':'0'}
        with worker_environment(original) as env:
            self.assertEqual(env['PYTHONPATH'],'/app')
            self.assertEqual(env['PYTHONNOUSERSITE'],'1')
            self.assertEqual(env['LD_LIBRARY_PATH'],'/cuda')
            self.assertEqual(env['CUDA_VISIBLE_DEVICES'],'0')
            for key in ('PYTHONHOME','PYTHONUSERBASE','VIRTUAL_ENV'):
                self.assertNotIn(key,env)
        self.assertEqual(original['PYTHONPATH'],'/__modal/deps:/other')

    def test_injected_dependency_cannot_mask_missing_worker_package(self):
        import os
        from pathlib import Path
        import subprocess
        import sys
        import tempfile
        with tempfile.TemporaryDirectory() as folder:
            Path(folder,'colocation_injected_probe.py').write_text('available=True\n')
            parent={'PATH':'/usr/bin:/bin','PYTHONPATH':folder}
            command=[sys.executable,'-c','import colocation_injected_probe']
            self.assertEqual(subprocess.run(command,env=parent,capture_output=True,timeout=5).returncode,0)
            with worker_environment(parent) as env:
                self.assertNotEqual(subprocess.run(command,env=env,
                    capture_output=True,timeout=5).returncode,0)


class RoundedProbabilityTests(unittest.TestCase):
    def test_official_four_decimal_output_and_invalid_mass(self):
        from coppertop.decision_gate import validate_answers, QUESTIONS
        answers={name:{'type':'choice','choice':next(iter(question['criteria'])),
                 'confidence':.3333,'probabilities':{key:.3333 for key in question['criteria']}}
                 for name,question in QUESTIONS.items()}
        self.assertEqual(validate_answers(answers,probability_decimals=4)['timing']['probabilities'],answers['timing']['probabilities'])
        with self.assertRaises(ValueError):validate_answers(answers)
        answers['timing']['probabilities']['continue']=.332
        with self.assertRaises(ValueError):validate_answers(answers,probability_decimals=4)


class CachedStageTests(unittest.TestCase):
    def response(self):
        import json
        rows = []
        for kind, phases in (('actor', ('warmup','alone_before','coresident_serial','overlap','alone_after')),
                             ('clef', ('warmup','coresident_serial','overlap'))):
            rows.extend({'kind':kind,'phase':phase,'index':index,'seconds':.25}
                        for phase in phases for index in range(3))
        return {'returncode':0,'timeout':False,'files':{
            'result.json':json.dumps({'status':'passed','rows':rows})}}

    def test_complete_schedule_required(self):
        import json
        from coppertop.colocation_stage import require_complete
        response = self.response()
        self.assertEqual(len(require_complete(response)['rows']),24)
        result = json.loads(response['files']['result.json'])
        result['rows'][-1] = result['rows'][0]  # Same count, missing one distinct call.
        response['files']['result.json'] = json.dumps(result)
        with self.assertRaises(ValueError):require_complete(response)

    def test_timeout_cannot_reuse_a_passed_receipt(self):
        from coppertop.colocation_stage import require_complete
        response = self.response()
        response['timeout'] = True
        with self.assertRaises(ValueError):require_complete(response)

    def test_nonfinite_timing_rejected(self):
        import json
        from coppertop.colocation_stage import require_complete
        response = self.response()
        result = json.loads(response['files']['result.json'])
        result['rows'][0]['seconds'] = float('nan')
        response['files']['result.json'] = json.dumps(result)
        with self.assertRaises(ValueError):require_complete(response)

    def test_deadline_keeps_partial_receipts_and_kills_group(self):
        from pathlib import Path
        import subprocess
        import tempfile
        from unittest.mock import Mock
        from coppertop.colocation_stage import run_stage
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            (root/'partial.json').write_text('[{"phase":"warmup"}]')
            process = Mock(pid=1234, returncode=-9)
            process.wait.side_effect = [subprocess.TimeoutExpired('worker',450),-9]
            with patch('coppertop.colocation_stage.ROOT',root), \
                 patch('coppertop.colocation_stage.subprocess.Popen',return_value=process), \
                 patch('coppertop.colocation_stage.os.killpg') as kill:
                response = run_stage('gpu')
            self.assertTrue(response['timeout'])
            self.assertEqual(response['files']['partial.json'],'[{"phase":"warmup"}]')
            kill.assert_called_once()
