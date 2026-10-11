"""Local closure tests consume four actual 600-control CPU episode results."""
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from coppertop import evaluation_roundtrip as artifacts
from coppertop.chords import canonical, digest
from coppertop.streams import file_ref


class FixtureRoundtripTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        from coppertop.evaluation_episode import run_fixture_pilot
        cls.pilot = run_fixture_pilot()

    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.directory = Path(temporary.name)/'roundtrip'

    def run_roundtrip(self):
        return artifacts.roundtrip(self.directory, self.pilot)

    def receipt(self):
        return json.loads((self.directory/artifacts.RECEIPT).read_bytes())

    def preserved_references(self, directory=None):
        directory = directory or self.directory
        descriptor = json.loads((directory/'stage/archive.json').read_bytes())
        _, _, files = artifacts._prepare(self.pilot, artifacts.MAX_BYTES)
        return files, descriptor['archive']

    def assert_producer_preserved(self, inventory, archive):
        self.assertEqual(artifacts._inventory(self.directory/'source', artifacts.MAX_BYTES), inventory)
        self.assertEqual(file_ref(self.directory/'episode.tar').to_dict(), archive)

    def test_four_complete_episode_tapes_accounting_and_every_file_reload(self):
        result = self.run_roundtrip()
        receipt = self.receipt()
        self.assertEqual([len(row['tape'].actions) for row in self.pilot['episodes']], [600]*4)
        self.assertEqual([len(row['tape'].frames) for row in self.pilot['episodes']], [601]*4)
        self.assertEqual((result['episodes'], result['controls'], result['frames']), (4, 2400, 2404))
        self.assertEqual(result['status'], 'local_hash_verified')
        self.assertTrue(result['local_fsync_committed'])
        self.assertFalse(result['remote_durability_verified'])
        self.assertFalse(result['real_model_simulator_verified'])
        self.assertFalse(result['paid_authorized'])
        self.assertLessEqual(result['raw_bytes'], artifacts.MAX_BYTES)
        self.assertLessEqual(result['objects'], artifacts.MAX_OBJECTS)
        self.assertEqual(result['source_files_verified'], len(receipt['files']))
        self.assertEqual(result['restored_files_verified'], len(receipt['files']))
        self.assertEqual(file_ref(self.directory/'episode.tar').to_dict(), result['archive'])
        self.assertEqual(file_ref(self.directory/'stage/episode.tar').to_dict(), result['archive'])
        reloaded = artifacts.reload_roundtrip(self.directory, receipt_sha256=result['receipt_sha256'])
        self.assertEqual(reloaded['root'], result['root'])
        self.assertEqual(reloaded['objects'], result['objects'])
        store = artifacts.LocalStore(self.directory/'restored/store')
        root = json.loads(store.get(result['root']['sha256']))
        self.assertEqual(json.loads(store.get(root['pilot_accounting'])), self.pilot['accounting'])
        for original, row in zip(self.pilot['episodes'], root['episodes']):
            self.assertEqual(artifacts._load_tape(json.loads(store.get(row['tape']))), original['tape'])
            self.assertEqual(store.get(row['accounting']), canonical(original['accounting']))

    def test_default_consumes_actual_fixture_pilot_and_local_fsync_barriers(self):
        with patch('coppertop.evaluation_episode.run_fixture_pilot', return_value=self.pilot) as pilot, \
             patch.object(artifacts.os, 'fsync', wraps=artifacts.os.fsync) as fsync:
            result = artifacts.roundtrip(self.directory)
        pilot.assert_called_once_with()
        self.assertGreater(fsync.call_count, result['objects'])
        self.assertTrue(result['local_fsync_committed'])

    def test_byte_and_episode_bounds_precede_any_output_write(self):
        for kwargs in ({'max_bytes': 1}, {'max_bytes': True}, {'max_bytes': artifacts.MAX_BYTES+1}):
            with self.subTest(kwargs=kwargs), self.assertRaises(ValueError):
                artifacts.roundtrip(self.directory, self.pilot, **kwargs)
            self.assertFalse(self.directory.exists())
        with self.assertRaisesRegex(ValueError, 'four complete'):
            artifacts.roundtrip(self.directory, dict(self.pilot, episodes=self.pilot['episodes'][:3]))
        self.assertFalse(self.directory.exists())
        first = self.pilot['episodes'][0]
        with self.assertRaisesRegex(ValueError, 'Distinct'):
            artifacts.roundtrip(self.directory, dict(self.pilot, episodes=[first]*4))
        self.assertFalse(self.directory.exists())

    def test_mismatched_accounting_and_nonfixture_provenance_fail_before_writes(self):
        result = self.pilot['episodes'][0]
        changed = dict(result, accounting=dict(result['accounting'], controls=599))
        with self.assertRaisesRegex(ValueError, 'binding'):
            artifacts.roundtrip(self.directory, dict(self.pilot, episodes=(changed, *self.pilot['episodes'][1:])))
        self.assertFalse(self.directory.exists())
        with self.assertRaisesRegex(ValueError, 'fixture provenance'):
            artifacts.roundtrip(self.directory, dict(self.pilot,
                provenance=dict(self.pilot['provenance'], proposal_selected=True)))
        self.assertFalse(self.directory.exists())

    def test_existing_destination_and_symlink_are_preserved(self):
        self.directory.mkdir()
        sentinel = self.directory/'original'
        sentinel.write_bytes(b'original user bytes')
        with self.assertRaises(FileExistsError):
            self.run_roundtrip()
        self.assertEqual(sentinel.read_bytes(), b'original user bytes')
        linked = self.directory.parent/'linked'
        linked.symlink_to(self.directory, target_is_directory=True)
        with self.assertRaisesRegex(ValueError, 'symlink'):
            artifacts.roundtrip(linked, self.pilot)
        self.assertEqual(sentinel.read_bytes(), b'original user bytes')

    def test_episode_runtime_claims_are_rejected_before_any_write(self):
        first = self.pilot['episodes'][0]
        for key in ('real_model_execution', 'real_simulator_execution'):
            changed = dict(first, provenance=dict(first['provenance'], **{key: True}))
            pilot = dict(self.pilot, episodes=(changed, *self.pilot['episodes'][1:]))
            with self.subTest(key=key), self.assertRaisesRegex(ValueError, 'fixture provenance'):
                artifacts.roundtrip(self.directory, pilot)
            self.assertFalse(self.directory.exists())

    def test_extraction_cleanup_failure_preserves_primary_error_and_producer(self):
        from coppertop import record_archive
        self.run_roundtrip()
        descriptor = self.receipt()['descriptor']
        wrong = dict(descriptor, manifest_sha256='0'*64)
        destination = self.directory/'failed-extraction'
        with patch.object(record_archive.shutil, 'rmtree', side_effect=OSError('cleanup failure')):
            with self.assertRaisesRegex(ValueError, 'Internal manifest checksum') as raised:
                record_archive.unpack_archive(self.directory/'episode.tar', wrong, destination)
        self.assertTrue(destination.exists())
        self.assertTrue(any('cleanup failure' in note for note in raised.exception.__notes__))
        inventory, archive = self.preserved_references()
        self.assert_producer_preserved(inventory, archive)

    def test_failed_local_commit_preserves_source_archive_and_copied_stage(self):
        with patch.object(artifacts, '_fsync_commit', side_effect=OSError('local commit failed')):
            with self.assertRaisesRegex(OSError, 'local commit failed'):
                self.run_roundtrip()
        source = artifacts._inventory(self.directory/'source', artifacts.MAX_BYTES)
        self.assertTrue(source)
        self.assertEqual(file_ref(self.directory/'episode.tar'), file_ref(self.directory/'stage/episode.tar'))
        self.assertFalse((self.directory/'stage/stage.json').exists())
        self.assertFalse((self.directory/'restored').exists())
        self.assertFalse((self.directory/artifacts.RECEIPT).exists())

    def test_corrupt_staged_archive_is_rejected_without_removing_producer(self):
        unpack = artifacts.unpack_archive

        def corrupt_then_unpack(archive, descriptor, destination):
            with Path(archive).open('r+b') as output:
                output.write(b'corrupt')
            return unpack(archive, descriptor, destination)

        with patch.object(artifacts, 'unpack_archive', side_effect=corrupt_then_unpack):
            with self.assertRaisesRegex(ValueError, 'checksum'):
                self.run_roundtrip()
        files, archive = self.preserved_references()
        self.assert_producer_preserved(files, archive)
        self.assertTrue((self.directory/'stage/episode.tar').exists())
        self.assertFalse((self.directory/'restored').exists())

    def test_missing_or_corrupt_restored_objects_fail_and_owned_restore_is_cleaned(self):
        unpack = artifacts.unpack_archive
        for corruption in ('missing', 'changed', 'extra'):
            with self.subTest(corruption=corruption):
                target = self.directory.parent/corruption

                def corrupt_restore(archive, descriptor, destination):
                    restored = unpack(archive, descriptor, destination)
                    path = next(path for path in (restored/'store/objects').rglob('*') if path.is_file())
                    if corruption == 'missing':
                        path.unlink()
                    elif corruption == 'changed':
                        with path.open('r+b') as output:
                            output.write(b'x')
                    else:
                        (restored/'extra-unreferenced.bin').write_bytes(b'unreferenced')
                    return restored

                with patch.object(artifacts, 'unpack_archive', side_effect=corrupt_restore):
                    with self.assertRaises(ValueError):
                        artifacts.roundtrip(target, self.pilot)
                files, archive = self.preserved_references(target)
                self.assertEqual(artifacts._inventory(target/'source', artifacts.MAX_BYTES), files)
                self.assertEqual(file_ref(target/'episode.tar').to_dict(), archive)
                self.assertFalse((target/'restored').exists())

    def test_cleanup_does_not_remove_a_preexisting_restore_on_unpack_refusal(self):
        unpack = artifacts.unpack_archive

        def existing_restore(archive, descriptor, destination):
            destination.mkdir()
            (destination/'unrelated').write_bytes(b'preserve')
            return unpack(archive, descriptor, destination)

        with patch.object(artifacts, 'unpack_archive', side_effect=existing_restore):
            with self.assertRaises(FileExistsError):
                self.run_roundtrip()
        self.assertEqual((self.directory/'restored/unrelated').read_bytes(), b'preserve')
        files, archive = self.preserved_references()
        self.assert_producer_preserved(files, archive)

    def test_cleanup_failure_preserves_primary_error_and_partial_evidence(self):
        primary = ValueError('primary checksum failure')
        with patch.object(artifacts, 'reload_roundtrip', side_effect=primary), \
             patch.object(artifacts.shutil, 'rmtree', side_effect=OSError('restore cleanup failed')):
            with self.assertRaisesRegex(ValueError, 'primary checksum failure') as raised:
                self.run_roundtrip()
        self.assertIs(raised.exception, primary)
        note = '\n'.join(raised.exception.__notes__)
        self.assertIn('OSError: restore cleanup failed', note)
        self.assertIn(str(self.directory/'restored'), note)
        self.assertTrue((self.directory/'restored/store').is_dir())
        files, archive = self.preserved_references()
        self.assert_producer_preserved(files, archive)
        self.assertEqual(file_ref(self.directory/'stage/episode.tar').to_dict(), archive)

    def test_independent_reload_rejects_missing_corrupt_and_unreferenced_bytes(self):
        result = self.run_roundtrip()
        # The inventory names are source-relative.
        path = self.directory/'restored'/next(iter(self.receipt()['files']))
        original = path.read_bytes()
        for changed in (b'x'+original[1:], original[:-1]):
            with self.subTest(size=len(changed)):
                path.write_bytes(changed)
                with self.assertRaisesRegex(ValueError, 'closure/checksum'):
                    artifacts.reload_roundtrip(self.directory, receipt_sha256=result['receipt_sha256'])
        path.write_bytes(original)
        extra = self.directory/'restored/unreferenced'
        extra.write_bytes(b'extra')
        with self.assertRaises(ValueError):
            artifacts.reload_roundtrip(self.directory, receipt_sha256=result['receipt_sha256'])
        extra.unlink()
        path.unlink()
        with self.assertRaisesRegex(ValueError, 'closure/checksum'):
            artifacts.reload_roundtrip(self.directory, receipt_sha256=result['receipt_sha256'])
        self.assertTrue((self.directory/'episode.tar').exists())

    def test_receipt_authentication_precedes_json_parsing(self):
        result = self.run_roundtrip()
        receipt = self.directory/artifacts.RECEIPT
        receipt.write_bytes(b'corrupt receipt')
        with patch.object(artifacts.json, 'loads', side_effect=AssertionError('unauthenticated parse')):
            with self.assertRaisesRegex(ValueError, 'receipt differs'):
                artifacts.reload_roundtrip(self.directory, receipt_sha256=result['receipt_sha256'])
        self.assertNotEqual(digest(receipt.read_bytes()), result['receipt_sha256'])


if __name__ == '__main__':
    unittest.main()
