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
        child = worker_environment(parent)
        result = subprocess.run([sys.executable, '-c',
            'import json,os; print(json.dumps(sorted(os.environ)))'],
            env=child, capture_output=True, text=True, check=True, timeout=5)
        keys = set(json.loads(result.stdout))
        for key in ('MODAL_TOKEN_ID', 'MODAL_TOKEN_SECRET', 'AWS_ACCESS_KEY_ID',
                    'AWS_SECRET_ACCESS_KEY', 'HF_TOKEN', 'GITHUB_TOKEN',
                    'UNCLASSIFIED_PRIVATE_VALUE', 'LD_PRELOAD'):
            self.assertNotIn(key, keys)
        self.assertEqual(child['PYTHONPATH'], '/app')
        self.assertEqual(child['COPPERTOP_CLEF_ROOT'], '/fixture-clef')
        self.assertEqual(child['CUDA_VISIBLE_DEVICES'], '0')
        self.assertEqual(parent, before)

    def test_cache_install_download_and_reference_checks_share_filtered_environment(self):
        calls = []
        def run(command, **kwargs):
            calls.append(kwargs['env'])
            return subprocess.CompletedProcess(command, 0)
        parent = {'PATH': '/usr/bin:/bin', 'HOME': '/tmp/fixture-home',
                  'PRIVATE_CUSTOM_NAME': 'synthetic-only', 'HF_TOKEN': 'synthetic-only'}
        metadata = {'revision': 'fixture-revision'}
        real_read = Path.read_text
        def read(path, *args, **kwargs):
            if str(path) == '/opt/clef-build/model-metadata.json':
                return json.dumps(metadata)
            return real_read(path, *args, **kwargs)
        with tempfile.TemporaryDirectory() as folder:
            cache = Path(folder)/'cache'
            with patch.object(clef_cache, 'CACHE', cache), \
                 patch.dict(clef_cache.os.environ, parent, clear=True), \
                 patch.object(clef_cache.subprocess, 'run', side_effect=run), \
                 patch.object(clef_cache.subprocess, 'check_output', return_value='fixture==1\n') as freeze, \
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
        download = [env for env in calls if env['HF_HUB_OFFLINE'] == '0']
        self.assertEqual(len(download), 1)
        self.assertEqual(download[0]['HF_HUB_DISABLE_IMPLICIT_TOKEN'], '1')


if __name__ == '__main__':
    unittest.main()
