"""Synthetic environment boundary checks; never use ambient credential values."""
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

from coppertop import clef_cache
from coppertop.colocation_worker import worker_environment


class EnvironmentBoundaryTests(unittest.TestCase):
    def test_reference_child_receives_runtime_allowlist_without_ambient_secrets(self):
        parent = {'PATH': '/usr/bin:/bin', 'HOME': '/tmp/fixture-home',
            'LD_LIBRARY_PATH': '/fixture-cuda', 'CUDA_VISIBLE_DEVICES': '0',
            'NVIDIA_VISIBLE_DEVICES': 'fixture-device', 'LANG': 'C.UTF-8',
            'LC_ALL': 'C.UTF-8', 'COPPERTOP_CLEF_ROOT': '/fixture-clef',
            'MODAL_TOKEN_ID': 'synthetic-only', 'MODAL_TOKEN_SECRET': 'synthetic-only',
            'AWS_ACCESS_KEY_ID': 'synthetic-only', 'AWS_SECRET_ACCESS_KEY': 'synthetic-only',
            'HF_TOKEN': 'synthetic-only', 'GITHUB_TOKEN': 'synthetic-only',
            'UNCLASSIFIED_PRIVATE_VALUE': 'synthetic-only',
            'PYTHONPATH': '/injected', 'LD_PRELOAD': '/injected'}
        before = dict(parent)
        with worker_environment(parent) as child:
            home = Path(child['HOME'])
            self.assertNotEqual(child['HOME'], parent['HOME'])
            self.assertEqual(home.stat().st_mode & 0o777, 0o700)
            result = subprocess.run([sys.executable, '-c',
                'import json,os; print(json.dumps(sorted(os.environ)))'],
                env=child, capture_output=True, text=True, check=True, timeout=5)
            self.assertTrue(home.exists())
        self.assertFalse(home.exists())
        keys = set(json.loads(result.stdout))
        for key in ('MODAL_TOKEN_ID', 'MODAL_TOKEN_SECRET', 'AWS_ACCESS_KEY_ID',
                    'AWS_SECRET_ACCESS_KEY', 'HF_TOKEN', 'GITHUB_TOKEN',
                    'UNCLASSIFIED_PRIVATE_VALUE', 'LD_PRELOAD'):
            self.assertNotIn(key, keys)
        self.assertEqual(child['PYTHONPATH'], '/app')
        self.assertEqual(child['COPPERTOP_CLEF_ROOT'], '/fixture-clef')
        self.assertEqual(child['CUDA_VISIBLE_DEVICES'], '0')
        self.assertEqual(parent, before)

    def test_child_home_and_all_cache_paths_cannot_discover_controller_sentinels(self):
        with tempfile.TemporaryDirectory() as directory:
            controller = Path(directory)
            for name in ('.aws/credentials', '.cache/huggingface/token',
                         '.config/synthetic-service/token'):
                path = controller/name
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_text('synthetic-controller-only')
            parent = {'PATH': '/usr/bin:/bin', 'HOME': str(controller),
                'XDG_CONFIG_HOME': str(controller/'.config'),
                'XDG_CACHE_HOME': str(controller/'.cache'),
                'HF_HOME': str(controller/'.cache/huggingface'),
                'HF_TOKEN_PATH': str(controller/'.cache/huggingface/token')}
            script = """import json, os
from pathlib import Path
home = Path.home()
paths = [home/'.aws/credentials', home/'.cache/huggingface/token',
         Path(os.environ['XDG_CONFIG_HOME'])/'synthetic-service/token',
         Path(os.environ['HF_TOKEN_PATH'])]
Path(os.environ['HF_HUB_CACHE'], 'child-probe').write_text('synthetic-child-only')
print(json.dumps({'home': str(home), 'credentials_found': any(path.exists() for path in paths)}))
"""
            with worker_environment(parent) as first, worker_environment(parent) as second:
                home = Path(first['HOME'])
                self.assertNotEqual(first['HOME'], second['HOME'])
                for key in ('HOME', 'XDG_CONFIG_HOME', 'XDG_CACHE_HOME', 'XDG_DATA_HOME',
                            'HF_HOME', 'HF_HUB_CACHE', 'HF_TOKEN_PATH', 'TRANSFORMERS_CACHE',
                            'TORCH_HOME', 'PIP_CACHE_DIR'):
                    self.assertTrue(Path(first[key]).is_relative_to(home), key)
                result = subprocess.run([sys.executable, '-c', script], env=first,
                    capture_output=True, text=True, check=True, timeout=5)
                observed = json.loads(result.stdout)
                self.assertEqual(observed['home'], str(home))
                self.assertFalse(observed['credentials_found'])
                self.assertTrue((Path(first['HF_HUB_CACHE'])/'child-probe').is_file())
                self.assertFalse((Path(second['HF_HUB_CACHE'])/'child-probe').exists())
            self.assertFalse(home.exists())
            self.assertEqual((controller/'.aws/credentials').read_text(), 'synthetic-controller-only')
            self.assertFalse((controller/'.cache/huggingface/child-probe').exists())

    def test_cache_install_download_and_reference_checks_share_filtered_environment(self):
        calls = []
        def run(command, **kwargs):
            calls.append(kwargs['env'])
            self.assertTrue(Path(kwargs['env']['HOME']).is_dir())
            return subprocess.CompletedProcess(command, 0)
        parent = {'PATH': '/usr/bin:/bin', 'HOME': '/tmp/fixture-home',
                  'PRIVATE_CUSTOM_NAME': 'synthetic-only', 'HF_TOKEN': 'synthetic-only'}
        metadata = {'revision': 'fixture-revision'}
        real_read = Path.read_text
        def read(path, *args, **kwargs):
            if str(path) == '/opt/clef-build/model-metadata.json':
                return json.dumps(metadata)
            return real_read(path, *args, **kwargs)
        def freeze_output(command, **kwargs):
            self.assertTrue(Path(kwargs['env']['HOME']).is_dir())
            return 'fixture==1\n'
        with tempfile.TemporaryDirectory() as folder:
            cache = Path(folder)/'cache'
            with patch.object(clef_cache, 'CACHE', cache), \
                 patch.dict(clef_cache.os.environ, parent, clear=True), \
                 patch.object(clef_cache.subprocess, 'run', side_effect=run), \
                 patch.object(clef_cache.subprocess, 'check_output', side_effect=freeze_output) as freeze, \
                 patch.object(Path, 'read_text', read), \
                 patch.object(Path, 'read_bytes', return_value=b'fixture-lock'):
                clef_cache.build()
                calls.append(freeze.call_args.kwargs['env'])
            self.assertTrue((cache/'receipt.json').is_file())
        self.assertEqual(len(calls), 7)
        for env in calls:
            self.assertNotIn('PRIVATE_CUSTOM_NAME', env)
            self.assertNotIn('HF_TOKEN', env)
            self.assertEqual(env['PYTHONPATH'], '/app')
            self.assertNotEqual(env['HOME'], parent['HOME'])
            self.assertFalse(Path(env['HOME']).exists())
        self.assertEqual(len({env['HOME'] for env in calls}), 1)
        download = [env for env in calls if env['HF_HUB_OFFLINE'] == '0']
        self.assertEqual(len(download), 1)
        self.assertEqual(download[0]['HF_HUB_DISABLE_IMPLICIT_TOKEN'], '1')

    def test_stage_keeps_home_until_real_child_exits_and_removes_it_afterwards(self):
        from coppertop import colocation_stage as stage
        homes = []
        real_popen = subprocess.Popen
        script = """import json, os, sys
from pathlib import Path
home = Path.home()
assert home.is_dir()
Path(sys.argv[1], 'cpu-gate.json').write_text(json.dumps({'home': str(home),
    'credential_found': (home/'.aws/credentials').exists()}))
"""
        def start(command, **kwargs):
            homes.append(Path(kwargs['env']['HOME']))
            self.assertTrue(homes[-1].is_dir())
            return real_popen([sys.executable, '-c', script, str(stage.ROOT)], **kwargs)
        with tempfile.TemporaryDirectory() as folder:
            controller = Path(folder)/'controller'
            (controller/'.aws').mkdir(parents=True)
            (controller/'.aws/credentials').write_text('synthetic-controller-only')
            with patch.object(stage, 'ROOT', Path(folder)/'stage'), \
                 patch.dict(stage.os.environ, {'PATH': '/usr/bin:/bin', 'HOME': str(controller)}, clear=True), \
                 patch.object(stage.subprocess, 'Popen', side_effect=start):
                response = stage.run_stage('cpu')
            observed = json.loads(response['files']['cpu-gate.json'])
            self.assertEqual(response['returncode'], 0)
            self.assertFalse(response['timeout'])
            self.assertEqual(observed['home'], str(homes[0]))
            self.assertFalse(observed['credential_found'])
            self.assertFalse(homes[0].exists())
            self.assertTrue((controller/'.aws/credentials').is_file())

    def test_benchmark_owns_distinct_homes_through_all_worker_waits(self):
        from coppertop import colocation_worker as worker
        real_popen = subprocess.Popen
        real_send = worker.send
        script = """import json, os, sys
from pathlib import Path
assert Path.home().is_dir()
print(json.dumps({'ready': sys.argv[1]}), flush=True)
for line in sys.stdin:
    if json.loads(line) == {'stop': True}: break
    assert Path.home().is_dir()
    print(json.dumps({'seconds': .1}), flush=True)
"""
        for failure in (False, True):
            with self.subTest(failure=failure), tempfile.TemporaryDirectory() as folder:
                homes, processes, terminated, killed = [], [], [], []
                def start(command, **kwargs):
                    home = Path(kwargs['env']['HOME'])
                    homes.append(home)
                    self.assertTrue(home.is_dir())
                    kind = command[-1] if command[-1] in ('actor', 'clef') else 'server'
                    if kind == 'server':
                        (worker.ROOT/'ready.json').write_text('{}')
                    process = real_popen([sys.executable, '-c', script, kind], **kwargs)
                    processes.append(process)
                    self.addCleanup(process.stdin.close)
                    self.addCleanup(process.stdout.close)
                    wait, terminate, kill = process.wait, process.terminate, process.kill
                    timeout_sent = False
                    def waited(*args, **options):
                        nonlocal timeout_sent
                        self.assertTrue(home.is_dir())
                        if failure and options.get('timeout') == 5 and not timeout_sent:
                            timeout_sent = True
                            raise subprocess.TimeoutExpired('synthetic cleanup wait', 5)
                        result = wait(*args, **options)
                        self.assertTrue(home.is_dir())
                        return result
                    def terminated_with_home():
                        self.assertTrue(home.is_dir())
                        terminated.append(home)
                        return terminate()
                    def killed_with_home():
                        self.assertTrue(home.is_dir())
                        killed.append(home)
                        return kill()
                    process.wait, process.terminate, process.kill = waited, terminated_with_home, killed_with_home
                    return process
                def send(process, index):
                    if failure and len(processes) == 3:
                        raise RuntimeError('synthetic protocol failure')
                    return real_send(process, index)
                with patch.object(worker, 'ROOT', Path(folder)), \
                     patch.dict(worker.os.environ, {'PATH': '/usr/bin:/bin', 'HOME': '/synthetic-controller'}, clear=True), \
                     patch.object(worker.subprocess, 'Popen', side_effect=start), \
                     patch.object(worker.subprocess, 'check_output', return_value='1, 2'), \
                     patch.object(worker, 'send', side_effect=send), patch.object(worker, 'emit'):
                    if failure:
                        with self.assertRaisesRegex(RuntimeError, 'synthetic protocol failure'):
                            worker.benchmark()
                    else:
                        worker.benchmark()
                self.assertEqual(len(set(homes)), 3)
                self.assertTrue(all(process.poll() is not None for process in processes))
                self.assertTrue(all(not home.exists() for home in homes))
                self.assertEqual(len(terminated), 3 if failure else 2)
                self.assertEqual(len(killed), 3 if failure else 0)
                self.assertEqual(json.loads((Path(folder)/'result.json').read_text())['status'],
                                 'failed' if failure else 'passed')

    def test_stage_timeout_keeps_home_through_group_kill_and_final_wait(self):
        from coppertop import colocation_stage as stage
        from types import SimpleNamespace
        homes, waits, kills = [], [], []
        def wait(*, timeout):
            self.assertTrue(homes[0].is_dir())
            waits.append(timeout)
            if len(waits) == 1:
                raise subprocess.TimeoutExpired('synthetic stage', timeout)
            return -9
        process = SimpleNamespace(pid=1234, returncode=-9, wait=wait, poll=lambda: -9)
        def start(command, **kwargs):
            homes.append(Path(kwargs['env']['HOME']))
            self.assertTrue(homes[0].is_dir())
            return process
        def kill(pid, signal):
            self.assertTrue(homes[0].is_dir())
            kills.append(pid)
        with tempfile.TemporaryDirectory() as folder:
            with patch.object(stage, 'ROOT', Path(folder)), \
                 patch.dict(stage.os.environ, {'HOME': '/synthetic-controller'}, clear=True), \
                 patch.object(stage.subprocess, 'Popen', side_effect=start), \
                 patch.object(stage.os, 'killpg', side_effect=kill):
                response = stage.run_stage('cpu')
            self.assertTrue(response['timeout'])
            self.assertEqual(waits, [150, 10])
            self.assertEqual(kills, [1234])
            self.assertFalse(homes[0].exists())

    def test_unresolved_child_status_retains_home_and_primary_failure(self):
        import shutil
        from types import SimpleNamespace
        for status in ('live', 'unknown', 'exited'):
            with self.subTest(status=status):
                def poll():
                    if status == 'unknown':
                        raise OSError('synthetic status unavailable')
                    return None if status == 'live' else 0
                primary = RuntimeError('synthetic operation failure')
                with self.assertRaisesRegex(RuntimeError, 'synthetic operation failure') as raised:
                    with worker_environment({'HOME': '/synthetic-controller'},
                                            processes=[SimpleNamespace(poll=poll)]) as env:
                        home = Path(env['HOME'])
                        raise primary
                self.assertIs(raised.exception, primary)
                if status == 'exited':
                    self.assertFalse(home.exists())
                else:
                    self.addCleanup(shutil.rmtree, home)
                    self.assertTrue(home.is_dir())
                    self.assertIn(str(home), '\n'.join(primary.__notes__))
                    self.assertIn('incomplete cleanup', '\n'.join(primary.__notes__))


if __name__ == '__main__':
    unittest.main()
