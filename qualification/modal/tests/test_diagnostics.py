"""Evidence excludes unknown credentials without heuristic text redaction."""
import json
from pathlib import Path
import sys
import unittest
from types import SimpleNamespace
from unittest.mock import patch
sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'executor'))
from diagnostics import error_evidence


class DiagnosticsTests(unittest.TestCase):
    def test_opaque_message_auth_url_and_local_values_are_never_formatted(self):
        local_only_secret='fixture-local-must-not-be-captured'
        class UnformattableError(Exception):
            def __str__(self):raise AssertionError('must not format exception')
        try:
            raise UnformattableError('opaque-secret https://user:password@host/private-token?jwt=secret')
        except UnformattableError as error:evidence=error_evidence(error)
        raw=json.dumps(evidence)
        for secret in (local_only_secret,'opaque-secret','password','private-token','jwt=secret'):
            self.assertNotIn(secret,raw)
        self.assertEqual(evidence['type'],'UnformattableError')
        self.assertTrue(evidence['messages_omitted'])
        self.assertFalse(evidence['truncated'])

    def test_preserves_chained_classes_and_full_stack_structure(self):
        try:
            try:raise ConnectionError('not retained')
            except ConnectionError as cause:raise TimeoutError('not retained') from cause
        except TimeoutError as error:evidence=error_evidence(error)
        self.assertEqual([x['type']for x in evidence['exception_chain']],['TimeoutError','ConnectionError'])
        self.assertEqual(evidence['exception_chain'][0]['next_relation'],'cause')
        self.assertTrue(all(set(f)=={'file','function','line'} for e in evidence['exception_chain']for f in e['frames']))
        self.assertIn('test_preserves_chained_classes',json.dumps(evidence))

    def test_numeric_codes_survive_without_response_or_error_text(self):
        error=OSError(111,'opaque socket response');error.error_code=403;error.verify_code=20
        entry=error_evidence(error)['exception_chain'][0]
        self.assertEqual((entry['errno'],entry['error_code'],entry['verify_code']),(111,403,20))
        self.assertNotIn('opaque',json.dumps(entry))

    def test_cyclic_and_oversized_exception_chains_are_bounded(self):
        errors=[ValueError('é'*100000)for _ in range(32)]
        for left,right in zip(errors,errors[1:]):left.__cause__=right
        errors[-1].__cause__=errors[0]
        evidence=error_evidence(errors[0])
        self.assertTrue(evidence['truncated'])
        self.assertEqual(len(evidence['exception_chain']),16)
        self.assertLess(len(json.dumps(evidence)),4096)

    def test_full_structural_evidence_obeys_one_64k_serialized_budget(self):
        frame=SimpleNamespace(f_code=SimpleNamespace(co_filename='é'*1024,co_name='fixture'))
        with patch('diagnostics.traceback.walk_tb',return_value=iter([(frame,1)]*128)):
            evidence=error_evidence(ValueError('never retained'))
        self.assertTrue(evidence['truncated'])
        self.assertGreater(len(evidence['exception_chain'][0]['frames']),0)
        self.assertLessEqual(len(json.dumps(evidence,separators=(',',':')).encode()),65536)


if __name__=='__main__':unittest.main()
