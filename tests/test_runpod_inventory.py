import json
from pathlib import Path
import tempfile
import time
from types import SimpleNamespace
import unittest

from tools import run_runpod_inventory as inventory
from tools.check_runpod_readonly import selected_env


class RunPodInventoryTests(unittest.TestCase):
    def test_plan_cannot_expand_approved_scope_even_with_new_hash(self):
        prepared=inventory.prepare();inventory.validate(prepared)
        prepared['plan']['limits']=dict(prepared['plan']['limits'],execution_seconds=901)
        prepared['plan_sha256']=inventory.sha(prepared['plan'])
        with self.assertRaises(ValueError):inventory.validate(prepared)

    def test_consumed_attempt_does_not_authorize_a_corrected_attempt(self):
        with self.assertRaisesRegex(ValueError,'Fresh exact'):
            inventory.execute(inventory.prepare(),'/not-used',None)

    def test_cpu_envelope_and_egress_fit_separate_twenty_cent_cap(self):
        from decimal import Decimal
        plan=inventory.prepare()['plan']
        self.assertLess(Decimal(plan['compute_bound_usd'])+Decimal(plan['metadata_egress_bound_usd']),Decimal('.20'))
        self.assertEqual(plan['limits']['result_bytes'],256*1024)
        self.assertFalse(plan['new_image_builds']);self.assertIsNone(plan['gpu'])

    def test_definition_uses_existing_image_no_secrets_network_or_retries(self):
        seen={}
        class App:
            def __init__(self,name,**kw):seen['app']=(name,kw)
            def function(self,**kw):
                seen['function']=kw
                return lambda fn:fn
        sdk=SimpleNamespace(__version__='1.6.1',App=App,Image=SimpleNamespace(from_id=lambda image:image))
        inventory.define(sdk,inventory.prepare()['plan'])
        spec=seen['function']
        self.assertEqual(spec['image'],inventory.IMAGE)
        self.assertEqual(spec['cpu'],(2,2));self.assertEqual(spec['memory'],(16384,16384))
        self.assertEqual(spec['max_containers'],1);self.assertEqual(spec['retries'],0)
        self.assertTrue(spec['block_network']);self.assertEqual(spec['secrets'],[]);self.assertEqual(spec['volumes'],{})

    def test_network_blocked_output_uses_sync_inline_bytes_and_rejects_overflow(self):
        from unittest.mock import Mock
        p=inventory.prepare()['plan'];function=Mock()
        function.remote.return_value=inventory.encoded({'plan_sha256':inventory.sha(p),'status':'passed'})
        self.assertEqual(inventory.collect(function,p)['status'],'passed')
        function.spawn.assert_not_called()
        function.remote.return_value=b'x'*(p['limits']['result_bytes']+1)
        with self.assertRaises(ValueError):inventory.collect(function,p)

    def test_interpreter_code_does_not_depend_on_remote_launcher_path(self):
        code=inventory.child_code();compile(code,'inline-inventory','exec')
        self.assertNotIn('runpy',code);self.assertNotIn('__file__',code.replace("getattr(module,'__file__',None)",''))

    def test_tree_inventory_counts_hardlinks_logically_and_deduplicates_allocated(self):
        import os
        with tempfile.TemporaryDirectory() as temp:
            root=Path(temp);(root/'file').write_bytes(b'abc');os.link(root/'file',root/'hardlink')
            (root/'alias').symlink_to(root/'file')
            result=inventory.tree_inventory(root,time.monotonic()+10)
            self.assertEqual(result['logical_file_bytes'],6);self.assertEqual(result['regular_files'],2)
            self.assertEqual(result['symlinks'],1)
            self.assertEqual(result['allocated_bytes_unique_inode'],(root/'file').stat().st_blocks*512)
            with self.assertRaises(TimeoutError):inventory.tree_inventory(root,time.monotonic()-1)

    def test_existing_alias_configuration_is_selected_without_evaluation(self):
        with tempfile.TemporaryDirectory() as temp:
            path=Path(temp)/'config'
            path.write_text('RUNPOD_API_KEY=fake-control\nRUNPOD_S3_ACCESS_KEY=fake-id\nRUNPOD_S3_SECRET_ACCESS_KEY="fake-secret"\nUNRELATED="$(touch nope)"\n')
            values=selected_env(path)
            self.assertEqual(set(values),{'RUNPOD_API_KEY','RUNPOD_S3_ACCESS_KEY','RUNPOD_S3_SECRET_ACCESS_KEY'})
            path.write_text(path.read_text().replace('fake-control','$(touch nope)'))
            with self.assertRaises(ValueError):selected_env(path)

    def test_future_transport_accepts_existing_s3_identity_alias_and_rejects_conflicts(self):
        from coppertop.runpod_evidence import runpod_s3_access_key
        self.assertEqual(runpod_s3_access_key({'RUNPOD_S3_ACCESS_KEY':'fake-id'}),'fake-id')
        self.assertEqual(runpod_s3_access_key({'RUNPOD_S3_ACCESS_KEY_ID':'fake-id'}),'fake-id')
        with self.assertRaises(ValueError):runpod_s3_access_key({'RUNPOD_S3_ACCESS_KEY':'fake-id','RUNPOD_S3_ACCESS_KEY_ID':'different'})
        with self.assertRaises(ValueError):runpod_s3_access_key({})


if __name__=='__main__':unittest.main()
