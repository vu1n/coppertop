"""Offline checks in the interpreter that will execute the workload."""
import importlib
import os
import re
import subprocess
import sys


class LockMismatch(ValueError):
    def __init__(self, differences):
        super().__init__('Pinned simulator lock differs')
        self.differences = [{**item, 'observed': item['observed']
                            if item['observed'] is None or re.fullmatch(
                                r'[0-9][A-Za-z0-9.+-]{0,127}', item['observed']) else 'omitted'}
                            for item in differences]


def require_pip_check(runner=subprocess.run):
    # Output may contain local paths or direct URLs from unrelated distributions.
    # Keep the admission decision, never relay arbitrary package-manager text.
    env = {key: value for key, value in os.environ.items()
           if not key.startswith('PIP_') and key not in ('PYTHONHOME', 'PYTHONPATH')}
    env['PIP_CONFIG_FILE'] = os.devnull
    result = runner([sys.executable, '-m', 'pip', '--isolated',
                     '--disable-pip-version-check', 'check'],
                    stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                    timeout=60, env=env)
    if result.returncode != 0:
        raise ValueError('Installed dependency closure failed pip check')


def require_imports(names):
    # Metadata alone cannot detect missing module files in an installed wheel.
    for name in names:
        importlib.import_module(name)
