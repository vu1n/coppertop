import hashlib
import json
from pathlib import Path
import subprocess
import tempfile
import unittest
from unittest.mock import patch

from coppertop import evaluation_audit as evidence
from coppertop.chords import canonical


class EvaluationAuditTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        # A shallow hosted checkout may lack this immutable historical object.
        # Skip only Git's precise missing-object result, never authentication or
        # repository errors, and never fetch history during the test run.
        result = subprocess.run(
            ['git', '--no-replace-objects', 'cat-file', '--batch-check'],
            input=evidence.RECOVERY_COMMIT + '\n', text=True, cwd=evidence.ROOT,
            stdout=subprocess.PIPE, stderr=subprocess.PIPE, check=True, timeout=10)
        if result.stdout == evidence.RECOVERY_COMMIT + ' missing\n':
            raise unittest.SkipTest('Original recovery source commit '
                + evidence.RECOVERY_COMMIT + ' is absent from this checkout; '
                '23-file exact-original authentication requires locally supplied history')
        if result.stdout.split()[0:2] != [evidence.RECOVERY_COMMIT, 'commit']:
            raise ValueError('Unexpected original recovery Git object identity/type')
        cls.provenance, cls.originals, _ = evidence._recovery(evidence.ROOT)
        cls.git_sources = {evidence.PROVENANCE_PATH:
                           (evidence.ROOT / evidence.PROVENANCE_PATH).read_bytes(),
                           **cls.originals}

    def fixture(self, root):
        for name, data in self.git_sources.items():
            path = root / name
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(data)
        for name in ('coppertop/episode.py', 'coppertop/performance_eval.py'):
            (root / name).write_bytes((evidence.ROOT / name).read_bytes())
        (root / 'tools').mkdir()
        (root / 'tools/proposal.py').write_text('episode_seeds = [1152, 1153]\n')

    def git(self):
        return patch.object(evidence, '_git_bytes',
                            side_effect=lambda root, name: self.git_sources[name])

    def test_actual_originals_schema_and_model_metadata_authenticate_without_assets(self):
        value = evidence.audit()
        self.assertEqual(value['recovery_commit'], evidence.RECOVERY_COMMIT)
        self.assertEqual(value['original_entry_count'], 23)
        self.assertTrue(all(row['original_verified'] for row in value['entries']))
        self.assertEqual(value['decision_schema']['original_sha256'],
                         self.provenance['schema_sha256'])
        model = value['model_metadata']
        self.assertEqual(model['file_count'], 17)
        self.assertEqual(model['total_declared_bytes'], 19083377419)
        self.assertEqual(sum(item['path'].startswith('model-') and
                             item['path'].endswith('.safetensors')
                             for item in model['files']), 4)
        self.assertEqual(model['reference_implementation']['sha256'],
                         '0e304cf7c6500e8bb59bef7e2afd2c6373f82596dfb3b57d1aa93c175e2dc3a3')
        self.assertFalse(model['weights_read'])
        self.assertFalse(model['reference_module_read'])
        self.assertFalse(model['runtime_verified'])
        self.assertFalse(value['provider_calls'])
        self.assertFalse(value['cloud_writes'])
        json.dumps(value, allow_nan=False)

    def test_current_derivatives_are_separate_from_verified_originals(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            self.fixture(root)
            name = 'coppertop/decision_runner.py'
            (root / name).write_bytes(self.originals[name] + b'\n# derivative\n')
            with self.git():
                result = evidence.audit(root)
            row = next(row for row in result['entries'] if row['path'] == name)
            self.assertEqual(row['current_status'], 'derivative')
            self.assertTrue(row['original_verified'])
            self.assertEqual(row['original_sha256'], hashlib.sha256(self.originals[name]).hexdigest())
            self.assertNotEqual(row['original_sha256'], row['current_sha256'])

    def test_original_authentication_uses_exact_commit_without_replacement_objects(self):
        class Result:
            returncode = 0
            stdout = b'original'
        with patch.object(evidence.subprocess, 'run', return_value=Result()) as run:
            self.assertEqual(evidence._git_bytes(evidence.ROOT, 'coppertop/episode.py'), b'original')
        args = run.call_args.args[0]
        self.assertEqual(args, ['git', '--no-replace-objects', '--no-pager', 'show',
                                f'{evidence.RECOVERY_COMMIT}:coppertop/episode.py'])
        self.assertEqual(run.call_args.kwargs['timeout'], 10)

    def test_changed_original_bytes_fail_authentication_even_with_valid_current_file(self):
        changed = dict(self.git_sources)
        changed['coppertop/decision_runner.py'] += b'changed'
        with patch.object(evidence, '_git_bytes', side_effect=lambda root, name: changed[name]):
            with self.assertRaisesRegex(ValueError, 'authentication failed'):
                evidence.audit()

    def test_immutable_provenance_and_packaging_metadata_cannot_be_rewritten(self):
        for name in (evidence.PROVENANCE_PATH, 'manifests/clef-recovery/dev-archive-identities.json'):
            with self.subTest(name=name), tempfile.TemporaryDirectory() as directory:
                root = Path(directory)
                self.fixture(root)
                (root / name).write_bytes(self.git_sources[name] + b'\n')
                with self.git(), self.assertRaisesRegex(ValueError, 'Immutable recovery'):
                    evidence.audit(root)

    def test_declared_exploration_is_excluded_without_becoming_proven_task_execution(self):
        value = evidence.seed_ledger()
        self.assertEqual(value['used'], [8, 9, 12, 15, 23, 1150])
        self.assertEqual(value['explored'], [7, 8, 9, 12, 15, 23])
        self.assertEqual(value['reserved'], [1151])
        self.assertEqual(value['exclusions'], [7, 8, 9, 12, 15, 23, 1150, 1151])
        self.assertEqual(value['reported_execution_seeds'], [1150])
        self.assertEqual(value['proven_execution_seeds'], [])
        self.assertFalse(value['actual_candidate_unused'])
        self.assertFalse(value['full_history_complete'])
        self.assertTrue(value['accessible_inventory_complete'])
        self.assertEqual([row['seed'] for row in value['candidate_checks']], [1152, 1153])
        self.assertTrue(all(not row['actual_candidate_unused'] for row in value['candidate_checks']))
        self.assertTrue(all(not name.startswith('tests/') for name in value['evidence_hashes']))

    def test_tests_proposals_policy_seeds_and_generated_histories_do_not_count_as_executed(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            self.fixture(root)
            (root / 'tests/executed.py').write_text('DEV_SEEDS = [1152, 777]\n')
            (root / '.local').mkdir()
            (root / '.local/history.json').write_text('{"seed": 1153, "executed": true}')
            (root / 'docs/proposal.md').write_text('Proposed DEV seed 1153; policy seed 20261006.\n')
            with self.git():
                value = evidence.seed_ledger(root)
            self.assertNotIn(1152, value['exclusions'])
            self.assertNotIn(1153, value['reported_execution_seeds'])
            self.assertNotIn(20261006, value['exclusions'])
            self.assertTrue(any(row['path'] == 'docs/proposal.md' for row in value['references']))
            self.assertTrue(all(row['status'] == 'reference_only' for row in value['references']))
            self.assertFalse(any(name.startswith(('tests/', '.local/')) for name in value['evidence_hashes']))

    def test_receipt_hash_identities_are_not_raw_receipt_bytes_or_archive_closure(self):
        value = evidence.receipt_audit()
        self.assertTrue(value['identity_metadata_authenticated'])
        self.assertFalse(value['raw_receipts_available_in_scope'])
        self.assertFalse(value['archive_payloads_read'])
        self.assertFalse(value['complete_archive_closure_verified'])
        self.assertEqual(value['separate_failed_attempt']['retained_metadata_bytes'], 9194)
        for row in value['archives']:
            self.assertTrue(row['recorded_success'])
            self.assertFalse(row['completion_receipt_bytes_verified_here'])
            self.assertFalse(row['manifest_bytes_verified_here'])
            self.assertFalse(row['archive_sha256_verified_here'])
            self.assertFalse(row['release_closure_verified_here'])
            self.assertFalse(row['remote_readback_verified_here'])

    def test_actual_matching_metadata_bytes_can_be_recognized_without_payload_claims(self):
        name = 'manifests/clef-recovery/dev-archive-identities.json'
        identity = json.loads(self.originals[name])
        receipt = canonical({'kind': 'fixture_receipt', 'ok': True})
        manifest = canonical({'kind': 'fixture_manifest', 'objects': []})
        for entry in identity['archives']:
            entry['completion_receipt_sha256'] = hashlib.sha256(receipt).hexdigest()
            entry['manifest_sha256'] = hashlib.sha256(manifest).hexdigest()
        result = evidence._receipt_audit({name: canonical(identity)},
                                        {'docs/receipt.json': receipt, 'docs/manifest.json': manifest})
        self.assertTrue(result['raw_receipts_available_in_scope'])
        self.assertTrue(all(row['manifest_bytes_verified_here'] for row in result['archives']))
        self.assertFalse(result['complete_archive_closure_verified'])
        self.assertFalse(result['archive_payloads_read'])

    def test_bounded_reads_and_symlinks_reject_incomplete_inventory(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            self.fixture(root)
            (root / 'docs/oversized.md').write_bytes(b'x' * (evidence.MAX_FILE_BYTES + 1))
            with self.git(), self.assertRaisesRegex(ValueError, 'byte bound'):
                evidence.seed_ledger(root)
            (root / 'docs/oversized.md').unlink()
            (root / 'docs/redirect.md').symlink_to(root / 'coppertop/episode.py')
            with self.git(), self.assertRaisesRegex(ValueError, 'Symlink'):
                evidence.seed_ledger(root)
            (root / 'docs/redirect.md').unlink()
            (root / 'docs/redirected_directory').symlink_to(root / 'tests', target_is_directory=True)
            with self.git(), self.assertRaisesRegex(ValueError, 'Symlink'):
                evidence.seed_ledger(root)

    def test_inventory_total_bound_and_unsafe_paths_fail_closed(self):
        for name in ('../secret', '/etc/passwd', 'docs/../secret', 'docs//secret', 'docs\\secret'):
            with self.subTest(name=name), self.assertRaisesRegex(ValueError, 'repository-relative'):
                evidence._path(name)
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            self.fixture(root)
            with self.git(), patch.object(evidence, 'MAX_INVENTORY_FILES', 1), \
                    self.assertRaisesRegex(ValueError, 'bounded source scope'):
                evidence.seed_ledger(root)


class PortableEvaluationMetadataTests(unittest.TestCase):
    def fixture(self, root):
        for name in (evidence.PROVENANCE_PATH,
                     'manifests/clef-recovery/dev-archive-identities.json',
                     'coppertop/episode.py', 'coppertop/performance_eval.py'):
            path = root / name
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes((evidence.ROOT / name).read_bytes())
        (root / 'docs').mkdir()
        (root / 'tools').mkdir()

    def test_seed_receipt_and_manifest_metadata_do_not_use_git_or_subprocess(self):
        from coppertop import evaluation_readiness
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            self.fixture(root)
            self.assertFalse((root / '.git').exists())
            with patch.object(evidence, '_git_bytes', side_effect=AssertionError('No original history')) as git, \
                 patch.object(evidence.subprocess, 'run', side_effect=AssertionError('No process or fetch')) as process:
                ledger = evidence.seed_ledger(root)
                receipts = evidence.receipt_audit(root)
                manifest = evaluation_readiness.manifest()
            git.assert_not_called()
            process.assert_not_called()
        self.assertEqual(ledger['exclusions'], [7, 8, 9, 12, 15, 23, 1150, 1151])
        self.assertFalse(ledger['full_history_complete'])
        self.assertFalse(ledger['actual_candidate_unused'])
        self.assertTrue(receipts['identity_metadata_authenticated'])
        self.assertFalse(receipts['raw_receipts_available_in_scope'])
        self.assertFalse(receipts['complete_archive_closure_verified'])
        self.assertFalse(manifest['launch_ready'])

    def test_portable_metadata_rejects_changed_pinned_provenance_or_identity(self):
        for name in (evidence.PROVENANCE_PATH, 'manifests/clef-recovery/dev-archive-identities.json'):
            with self.subTest(name=name), tempfile.TemporaryDirectory() as directory:
                root = Path(directory)
                self.fixture(root)
                with (root / name).open('ab') as output:
                    output.write(b'\n')
                with patch.object(evidence.subprocess, 'run', side_effect=AssertionError('No process')):
                    with self.assertRaisesRegex(ValueError, 'Pinned current'):
                        evidence.seed_ledger(root)
                    with self.assertRaisesRegex(ValueError, 'Pinned current'):
                        evidence.receipt_audit(root)

    def test_full_audit_does_not_substitute_current_metadata_for_missing_original_history(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            self.fixture(root)
            with self.assertRaisesRegex(ValueError, 'Cannot authenticate original recovery source'):
                evidence.audit(root)


if __name__ == '__main__':
    unittest.main()
