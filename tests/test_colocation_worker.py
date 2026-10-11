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
            for phase in phases:
                for index in range(3):
                    row = {'kind':kind,'phase':phase,'index':index,'seconds':.25}
                    if kind == 'actor':
                        row['chunk_sha256'] = f'{index+1:064x}'
                    rows.append(row)
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

    def test_boolean_timing_rejected_for_every_scheduled_row(self):
        import json
        from coppertop.colocation_stage import require_complete
        for index in range(24):
            for value in (True, False):
                response = self.response()
                result = json.loads(response['files']['result.json'])
                row = result['rows'][index]
                with self.subTest(kind=row['kind'], phase=row['phase'], case=row['index'], value=value):
                    row['seconds'] = value
                    response['files']['result.json'] = json.dumps(result)
                    with self.assertRaisesRegex(ValueError, 'Invalid benchmark timing'):
                        require_complete(response)

    def test_positive_integer_seconds_remain_valid_json_numbers(self):
        import json
        from coppertop.colocation_stage import require_complete
        response = self.response()
        result = json.loads(response['files']['result.json'])
        for row in result['rows']:
            row['seconds'] = 1
        response['files']['result.json'] = json.dumps(result)
        self.assertEqual(require_complete(response), result)

    def test_valid_case_hashes_preserve_unmodified_result_in_any_row_order(self):
        import json
        from coppertop.colocation_stage import require_complete
        response = self.response()
        result = json.loads(response['files']['result.json'])
        self.assertEqual(len({row['chunk_sha256'] for row in result['rows']
                              if row['kind'] == 'actor'}), 3)
        self.assertTrue(all('chunk_sha256' not in row for row in result['rows']
                            if row['kind'] == 'clef'))
        self.assertEqual(require_complete(response), result)
        result['rows'].reverse()
        response['files']['result.json'] = json.dumps(result)
        self.assertEqual(require_complete(response), result)

    def test_missing_actor_hash_rejected_for_every_case_and_phase(self):
        import json
        from coppertop.colocation_stage import require_complete
        for phase in ('warmup','alone_before','coresident_serial','overlap','alone_after'):
            for index in range(3):
                with self.subTest(phase=phase, index=index):
                    response = self.response()
                    result = json.loads(response['files']['result.json'])
                    row = next(row for row in result['rows']
                        if (row['kind'], row['phase'], row['index']) == ('actor', phase, index))
                    del row['chunk_sha256']
                    response['files']['result.json'] = json.dumps(result)
                    with self.assertRaisesRegex(ValueError, 'Missing or malformed actor chunk SHA256'):
                        require_complete(response)

    def test_actor_hash_requires_exact_lowercase_hex(self):
        import json
        from coppertop.colocation_stage import require_complete
        malformed = (None, '', '0'*63, '0'*65, 'A'*64, 'g'*64, '0'*64+'\n',
                     '0'*63+'ｆ', 0, False, [], {})
        for value in malformed:
            with self.subTest(value=value):
                response = self.response()
                result = json.loads(response['files']['result.json'])
                result['rows'][0]['chunk_sha256'] = value
                response['files']['result.json'] = json.dumps(result)
                with self.assertRaisesRegex(ValueError, 'Missing or malformed actor chunk SHA256'):
                    require_complete(response)

    def test_actor_parity_rejects_one_changed_hash_in_every_case_and_phase(self):
        import json
        from coppertop.colocation_stage import require_complete
        for phase in ('warmup','alone_before','coresident_serial','overlap','alone_after'):
            for index in range(3):
                with self.subTest(phase=phase, index=index):
                    response = self.response()
                    result = json.loads(response['files']['result.json'])
                    row = next(row for row in result['rows']
                        if (row['kind'], row['phase'], row['index']) == ('actor', phase, index))
                    row['chunk_sha256'] = 'f'*64
                    response['files']['result.json'] = json.dumps(result)
                    with self.assertRaisesRegex(ValueError, f'Actor chunk parity failed for case {index}'):
                        require_complete(response)

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
