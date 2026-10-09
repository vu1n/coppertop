"""Real bounded tar/file/IPC controls; no Modal allocation or full seed export."""
from contextlib import nullcontext
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import Mock,patch

from coppertop import runpod_bundle as bundle
from coppertop import runpod_prepare as prep
from coppertop.runpod_source import ModalSource
from coppertop.streams import file_ref
from test_runpod_preparation import config,source


class Sandbox:
    """Run the exact public adapter commands against local native subprocesses."""
    def __init__(self,root,*,corrupt=False,lose_ack=False):
        self.root=root;self.corrupt=corrupt;self.lose_ack=lose_ack;self.processes=[]
        self.copies=[];self.high_water=0;self.filesystem=self
    def exec(self,*args,timeout):
        args=[sys.executable,*args[1:]]
        if '--spool-root' in args:
            args[args.index('--spool-root')+1]=str(self.root)
            # 4KiB parts exercise many acknowledgements with a tiny seed.
            code="from coppertop.runpod_bundle import export_spool; export_spool(%r,%r,max_seconds=%r,part_bytes=4096)"%(
                str(self.root),args[args.index('--output')+1],timeout)
            args=[sys.executable,'-c',code]
        process=subprocess.Popen(args,stdout=subprocess.PIPE,stderr=subprocess.PIPE,text=True)
        self.processes.append(process)
        if self.lose_ack and '--ack-spool' in args:
            process.wait();raise OSError('lost acknowledgement')
        return process
    def copy_to_local(self,remote,local):
        remote=Path(remote);self.copies.append(remote.name)
        if remote.name=='part.tar':
            self.high_water=max(self.high_water,remote.stat().st_size)
            self.assert_single_part(remote.parent)
        shutil.copyfile(remote,local)
        if self.corrupt and remote.name=='part.tar':Path(local).write_bytes(b'corrupt')
    def assert_single_part(self,path):
        if list(path.glob('*.tar'))!=[path/'part.tar']:raise AssertionError('Source accumulates archive parts')
    def close(self):
        for process in self.processes:
            if process.poll() is None:process.terminate()
            process.wait(timeout=5)
            process.stdout.close();process.stderr.close()


class SourceSpoolTests(unittest.TestCase):
    def adapter(self,root,**kwargs):
        sandbox=Sandbox(source(root/'seed'),**kwargs);adapter=ModalSource();adapter.sandbox=sandbox
        self.addCleanup(sandbox.close);(root/'local').mkdir()
        return adapter,sandbox

    def test_actual_adapter_spools_tar_then_restores_identical_files(self):
        with tempfile.TemporaryDirectory() as root,patch('coppertop.runpod_source.check_memory',return_value=0):
            root=Path(root);adapter,sandbox=self.adapter(root)
            descriptor=adapter.collect_spool(str(root/'remote'),root/'local',seconds=30)
            self.assertLessEqual(sandbox.high_water,4096);self.assertGreater(sandbox.copies.count('part.tar'),1)
            self.assertEqual(list((root/'remote').glob('*.tar')),[])
            self.assertEqual(sandbox.copies.count('manifest.json'),1)
            journal=json.loads((root/'local/copy-state.json').read_bytes())
            self.assertEqual(journal['status'],'complete');self.assertEqual(journal['copied_bytes'],descriptor['tar_bytes'])
            self.assertEqual(journal['requested_bytes'],descriptor['tar_bytes'])
            self.assertEqual(journal['parts'],sandbox.copies.count('part.tar'))
            self.assertEqual(journal['commands'],2*journal['parts']+1)
            bundle.restore(root/'local',root/'restored',volume=root/'local',free_bytes=3*bundle.GIB)
            self.assertEqual(file_ref(root/'restored/work/model/model.bin'),file_ref(root/'seed/work/model/model.bin'))
            self.assertEqual(os.readlink(root/'restored/work/sim/bin/python'),'/opt/conda/bin/python')

    def test_corrupt_copy_or_lost_ack_never_replays_or_creates_complete_bundle(self):
        for failure in ('corrupt','lost_ack'):
            with self.subTest(failure=failure),tempfile.TemporaryDirectory() as root,\
                    patch('coppertop.runpod_source.check_memory',return_value=0):
                root=Path(root);adapter,sandbox=self.adapter(root,corrupt=failure=='corrupt',lose_ack=failure=='lost_ack')
                with self.assertRaises((ValueError,OSError)):
                    adapter.collect_spool(str(root/'remote'),root/'local',seconds=30)
                self.assertEqual(sandbox.copies.count('part.tar'),1)
                self.assertFalse((root/'local/bundle.json').exists());self.assertTrue((root/'seed').exists())
                journal=json.loads((root/'local/copy-state.json').read_bytes())
                self.assertEqual(journal['status'],'incomplete');self.assertIsNotNone(journal['pending'])

    def test_spool_completion_checksum_and_part_limits_fail_closed(self):
        for failure in ('completion','part_limit','commands'):
            with self.subTest(failure=failure),tempfile.TemporaryDirectory() as root,\
                    patch('coppertop.runpod_source.check_memory',return_value=0):
                root=Path(root);adapter,sandbox=self.adapter(root)
                original=adapter.command
                def command(*args,**kwargs):
                    result=original(*args,**kwargs)
                    if failure=='completion' and kwargs.get('output'):
                        value=json.loads(result)
                        if value.get('kind')=='complete':value['tar_sha256']='0'*64
                        return json.dumps(value)
                    return result
                adapter.command=command
                limits={'source_spool_parts':1} if failure=='part_limit' else {'source_metadata_commands':1} if failure=='commands' else {}
                with patch.dict(prep.LIMITS,limits),self.assertRaises(ValueError):
                    adapter.collect_spool(str(root/'remote'),root/'local',seconds=30)
                self.assertFalse((root/'local/bundle.json').exists())

    def test_source_spool_uses_small_disk_guard_and_preserves_source_on_low_disk(self):
        with tempfile.TemporaryDirectory() as root:
            root=Path(root);seed=source(root/'seed')
            free=2*bundle.GIB+bundle.BOUNDS['manifest']+bundle.BOUNDS['spool']-1
            with patch.object(bundle.shutil,'disk_usage',return_value=SimpleNamespace(free=free)):
                with self.assertRaisesRegex(ValueError,'spool scratch'):
                    bundle.export_spool(seed,root/'spool',max_seconds=1)
            self.assertFalse((root/'spool/ready.json').exists());self.assertTrue(seed.exists())

    def test_producer_waits_for_ack_and_stops_at_deadline_without_completion(self):
        with tempfile.TemporaryDirectory() as root:
            root=Path(root);seed=source(root/'seed')
            with self.assertRaises(TimeoutError):bundle.export_spool(seed,root/'spool',max_seconds=.2,part_bytes=1024)
            self.assertEqual((root/'spool/part.tar').stat().st_size,1024)
            receipt=bundle.spool_status(root/'spool')
            self.assertEqual(receipt['kind'],'part');self.assertEqual(receipt['index'],0)
            with self.assertRaisesRegex(ValueError,'acknowledgement'):
                bundle.acknowledge_spool(root/'spool',1,receipt['sha256'])
            self.assertTrue((root/'spool/part.tar').exists())
            bundle.acknowledge_spool(root/'spool',0,receipt['sha256'])
            self.assertFalse((root/'spool/part.tar').exists());self.assertFalse((root/'spool/ready.json').exists())

    def test_source_peak_and_streaming_limits_are_bound_to_approval_plan(self):
        prepared=prep.prepare(config());limits=prepared['plan']['limits']
        self.assertEqual(limits['source_spool_bytes'],64*1024**2)
        self.assertEqual(limits['source_spool_parts']*limits['source_spool_bytes'],limits['export_bytes'])
        self.assertLess(limits['source_free_bytes'],7*bundle.GIB)
        self.assertEqual(prepared['plan']['additional_cap_usd'],'12.00')
        self.assertEqual(prepared['plan']['cost_bound_usd'],'9.768056')

    def test_live_collect_uses_spool_after_package_checks_and_removes_upload_scratch(self):
        with tempfile.TemporaryDirectory() as root:
            root=Path(root);debs=root/'debs';debs.mkdir();control=root/'control.tar';control.write_bytes(b'fixture')
            fs=Mock()
            fs.copy_to_local.side_effect=lambda remote,local:Path(local).write_bytes(b'p'*(8*1024**2))
            sandbox=SimpleNamespace(filesystem=fs,object_id='sb-fixture')
            app=Mock(app_id='ap-fixture');sdk=SimpleNamespace(__version__='1.6.1',App=Mock(return_value=app),
                Image=SimpleNamespace(from_id=Mock()),Sandbox=SimpleNamespace(create=Mock(return_value=sandbox)))
            app.run.return_value=nullcontext()
            adapter=ModalSource();adapter.validate_inputs=Mock();adapter.command=Mock();adapter.collect_spool=Mock()
            prepared=prep.prepare(config())
            with patch.dict(sys.modules,modal=sdk),patch('coppertop.runpod_source.shutil.disk_usage',
                    return_value=SimpleNamespace(free=64*bundle.GIB)):
                adapter.collect(prepared,root/'output',debs=debs,control=control)
            self.assertEqual(sdk.Sandbox.create.call_args.kwargs['cpu'],(2,2))
            self.assertEqual(sdk.Sandbox.create.call_args.kwargs['memory'],(16384,16384))
            self.assertTrue(sdk.Sandbox.create.call_args.kwargs['block_network'])
            self.assertNotIn('ephemeral_disk',sdk.Sandbox.create.call_args.kwargs)
            commands=[call.args for call in adapter.command.call_args_list]
            self.assertIn(str(prepared['plan']['limits']['source_free_bytes']),commands[0][-1])
            self.assertIn('--verify-packages',commands[-2])
            self.assertIn('coppertop-debs.tar',commands[-1][-1])
            self.assertNotIn('--export-root',[arg for command in commands for arg in command])
            adapter.collect_spool.assert_called_once()
            self.assertEqual(adapter.collect_spool.call_args.args,('/tmp/coppertop-export',root/'output'))


if __name__=='__main__':unittest.main()
