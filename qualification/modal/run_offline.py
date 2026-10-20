"""Run all exact reviewed fixtures with socket access denied and no skipped tests."""
from importlib.metadata import version
import json
from pathlib import Path
import shutil
import socket
import tempfile
import time
import unittest
from unittest.mock import patch
from verify_portability import verify


def denied(*args,**kwargs):raise AssertionError('Offline tests forbid network access')


def main():
    root=Path(__file__).resolve().parent;verify(root)
    pins=[line.split('==')for line in (root/'requirements-offline.txt').read_text().splitlines()
          if line and not line.startswith('#')]
    for name,expected in pins:
        if version(name)!=expected:raise ValueError('Pinned offline dependency differs: '+name)
    # Exact tests use a frozen pricing-date fixture. Refresh only the inert temp
    # plan's date so the same copied tests work after the original review date.
    with tempfile.TemporaryDirectory(prefix='coppertop-qualification-offline-')as temp:
        fixture=Path(temp)/'bundle';shutil.copytree(root,fixture,ignore=shutil.ignore_patterns('__pycache__','*.pyc'))
        p=fixture/'plan.json';plan=json.loads(p.read_text());plan['pricing']['checked_date']=time.strftime('%Y-%m-%d',time.gmtime())
        p.write_text(json.dumps(plan,sort_keys=True,separators=(',',':'))+'\n')
        with patch.object(socket.socket,'connect',denied),patch.object(socket.socket,'connect_ex',denied):
            suite=unittest.defaultTestLoader.discover(str(fixture/'tests'))
            result=unittest.TextTestRunner(verbosity=2).run(suite)
    if result.testsRun!=38 or result.skipped or not result.wasSuccessful():return 1
    print(json.dumps({'offline_tests_passed':38,'skipped':0,'provider_calls':0,'model_calls':0,'network_forbidden':True}))
    return 0


if __name__=='__main__':raise SystemExit(main())
