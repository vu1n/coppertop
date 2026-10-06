import copy
import hashlib
import json
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from coppertop.performance_eval import plan, validate_plan, summarize, wilson, runtime_image, CACHED_IMAGE
from coppertop import performance_runtime as runtime


def records():
    return [{'arm': arm, 'seed': seed, 'task': 'CoffeeSetupMug', 'status': 'completed',
             'success': arm == 'one_update', 'steps': 600, 'initial_observation_sha256': 'a'*64,
             'wall_seconds': 50., 'inference_seconds': 20., 'model_load_seconds': 10.}
            for seed in (1150, 1151) for arm in ('official', 'one_update')]


class PerformanceTests(unittest.TestCase):
    def test_cached_runtime_upload_mounts_only_reviewed_code_without_build(self):
        calls = []
        class Image:
            object_id = CACHED_IMAGE
            def add_local_file(self, path, remote, *, copy):
                calls.append((path, remote, copy)); return self
        image = Image()
        self.assertIs(runtime_image(SimpleNamespace(__version__='1.6.1'), image, Path(runtime.__file__).parents[1]), image)
        self.assertEqual(len(calls), 4)
        self.assertTrue(all(not call[2] for call in calls))
        image.object_id = 'im-unrelated'
        with self.assertRaises(ValueError): runtime_image(SimpleNamespace(__version__='1.6.1'), image, Path('.'))

    def test_pinned_official_schedule_and_exclusions(self):
        value = plan()
        self.assertEqual(value['episode_seeds'], [value['base_seed'] + value['task_index']*50 + e for e in value['episodes']])
        self.assertNotEqual(value['task'], value['training_task_excluded'])
        self.assertFalse(set(value['episode_seeds']) & set(value['dev_seeds_excluded']))
        self.assertEqual(value['horizon'], 600)
        self.assertEqual(value['limits']['automatic_retries'], 0)
        for key in ('horizon', 'model', 'episode_seeds', 'recipe', 'checkpoint'):
            changed = copy.deepcopy(value); changed[key] = None
            with self.assertRaises(ValueError): validate_plan(changed)

    def test_plan_source_mappings_are_not_shared_mutable_references(self):
        changed = plan(); changed['source_blobs'].clear(); changed['source_pins'].clear()
        self.assertEqual(len(plan()['source_blobs']), 3)
        self.assertEqual(len(plan()['source_pins']), 3)

    def test_dirty_simulator_checkout_rejected_before_package_import(self):
        spec = SimpleNamespace(origin='/cache/robocasa/robocasa/__init__.py')
        with patch.object(runtime.importlib.util, 'find_spec', return_value=spec), \
             patch.object(runtime.subprocess, 'check_output', side_effect=[runtime.SOURCE_PINS['robocasa'], b' M robocasa/__init__.py']), \
             patch('builtins.__import__', side_effect=AssertionError('premature simulator import')):
            with self.assertRaisesRegex(ValueError, 'checkout is changed'):
                runtime.simulator_gate()

    def test_prepare_is_offline_and_has_no_provider_or_model_execution(self):
        with patch.object(runtime, 'serve', side_effect=AssertionError('model loading')), \
             patch.object(runtime, 'simulator_gate', side_effect=AssertionError('simulator import')), \
             patch('builtins.print') as printed:
            runtime.main(['--prepare'])
        value = json.loads(printed.call_args.args[0])
        self.assertFalse(value['cloud_actions'])
        self.assertEqual(value['plan_sha256'], validate_plan(value['plan']))

    def test_pair_metrics_and_tiny_sample_uncertainty(self):
        result = summarize(plan(), records())
        self.assertEqual(result['paired_gains'], 2)
        self.assertEqual(result['paired_losses'], 0)
        self.assertEqual(result['exact_discordant_pair_p_value'], .5)
        self.assertLess(result['arms']['one_update']['wilson_95'][0], .5)
        self.assertIsNone(result['interventions'])

    def test_errors_incomplete_duplicates_and_wrong_task_cannot_count_as_failure(self):
        for altered in (records()[:-1], records() + records()[:1]):
            with self.assertRaises(ValueError): summarize(plan(), altered)
        for change in ({'status': 'timeout'}, {'success': 1}, {'steps': 601},
                       {'seed': 8}, {'task': 'CloseBlenderLid'}, {'wall_seconds': float('nan')}):
            changed = records(); changed[0].update(change)
            with self.assertRaises(ValueError): summarize(plan(), changed)
        changed = records(); changed[1] = changed[0]
        with self.assertRaises(ValueError): summarize(plan(), changed)

    def test_initial_state_mismatch_invalidates_comparison(self):
        changed = records(); changed[1]['initial_observation_sha256'] = 'b'*64
        with self.assertRaisesRegex(ValueError, 'initial observations differ'):
            summarize(plan(), changed)

    def test_tied_pairs_have_no_discordance(self):
        values = records()
        for row in values: row['success'] = False
        result = summarize(plan(), values)
        self.assertEqual(result['exact_discordant_pair_p_value'], 1.)
        self.assertEqual(result['paired_success_rate_difference'], 0.)

    def test_initial_fingerprint_detects_camera_and_state_changes(self):
        import numpy as np
        entry = SimpleNamespace(CAMERA_KEYS=('a', 'b', 'c'), observation_to_state=lambda obs: obs['state'])
        observation = {'state': np.zeros(14, dtype=np.float32),
            'annotation.human.task_description': 'Put the mug under the coffee machine',
            **{key: np.zeros((256, 256, 3), dtype=np.uint8) for key in entry.CAMERA_KEYS}}
        expected = runtime.observation_fingerprint(observation, entry)
        observation['b'][0, 0, 0] = 1
        self.assertNotEqual(expected, runtime.observation_fingerprint(observation, entry))
        observation['b'] = np.zeros((128, 128, 3), dtype=np.uint8)
        with self.assertRaises(ValueError): runtime.observation_fingerprint(observation, entry)

    def test_source_integrity_rejects_modified_official_code(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            sources = {'deploy/server.py': b'pass\n'}
            (root/'deploy').mkdir(); (root/'deploy/server.py').write_bytes(b'pass\n')
            blobs = {name: hashlib.sha1(b'blob '+str(len(raw)).encode()+b'\0'+raw).hexdigest() for name, raw in sources.items()}
            with patch.object(runtime, 'OFFICIAL', root), patch.object(runtime, 'SOURCE_BLOBS', blobs):
                runtime.source_gate()
                (root/'deploy/server.py').write_bytes(b'changed\n')
                with self.assertRaises(ValueError): runtime.source_gate()

    def test_uncertainty_rejects_invalid_counts(self):
        for counts in ((0, 0), (3, 2), (-1, 2)):
            with self.assertRaises(ValueError): wilson(*counts)
