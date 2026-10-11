import ast
import collections
import copy
import hashlib
import json
import os
import subprocess
import sys
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import numpy as np
from PIL import Image

from coppertop import evaluation_readiness as readiness
from coppertop.chords import canonical, window_indices
from coppertop.gpu_smoke import window
from coppertop.xiaomi import native_state


class ReadinessTests(unittest.TestCase):
    def test_full_task_indices_and_fifty_trial_stride_survive_subset_reordering(self):
        for task, index in (('CoffeeSetupMug', 3), ('WashLettuce', 33), ('ArrangeBreadBasket', 34)):
            for episode in (0, 2, 49):
                self.assertEqual(readiness.episode_seed(task, episode), 1000+index*50+episode)
        seeds = [readiness.episode_seed(row['task'], episode)
                 for row in readiness.target50() for episode in range(50)]
        self.assertEqual(seeds, list(range(1000, 3500)))
        self.assertEqual(readiness.episode_seed('CoffeeSetupMug', 2, base_seed=7), 159)
        for kwargs in ({'num_trials': 2}, {'num_trials': True}, {'base_seed': True}, {'base_seed': -1}):
            with self.assertRaises(ValueError): readiness.episode_seed('CoffeeSetupMug', 2, **kwargs)
        for episode in (-1, 50, True, 2.0):
            with self.assertRaises(ValueError): readiness.episode_seed('CoffeeSetupMug', episode)
        with self.assertRaises(ValueError): readiness.episode_seed('NotTarget50', 0)

    def test_all_fifty_horizons_and_correct_full_benchmark_denominator(self):
        matrix = readiness.benchmark_matrix()
        self.assertEqual([row['index'] for row in matrix], list(range(50)))
        self.assertEqual(len(set(row['task'] for row in matrix)), 50)
        self.assertEqual(collections.Counter(row['group'] for row in matrix),
                         {'atomic_seen': 18, 'composite_seen': 16, 'composite_unseen': 16})
        self.assertEqual(sum(row['episodes_per_arm'] for row in matrix), 2500)
        self.assertEqual((min(row['horizon'] for row in matrix), max(row['horizon'] for row in matrix)), (450, 4350))
        for row in matrix:
            queries = row['max_xiaomi_queries_per_episode']
            self.assertLess((queries-1)*16, row['horizon'])
            self.assertGreaterEqual(queries*16, row['horizon'])
        self.assertEqual(matrix[3]['horizon'], 600)
        self.assertEqual(matrix[3]['max_xiaomi_queries_per_episode'], 38)
        with patch.object(readiness, 'REGISTRY_SHA256', '0'*64):
            with self.assertRaisesRegex(ValueError, 'snapshot differs'): readiness.target50()

    def test_oversized_registry_is_rejected_before_parsing_with_a_bounded_read(self):
        from io import BytesIO
        original = (readiness.ROOT/'containers/robocasa-target50.json').read_bytes()
        self.assertEqual(len(original), readiness.REGISTRY_BYTES)
        # A valid prefix plus unauthenticated trailing bytes must never parse.
        source = BytesIO(original+b'altered'*1000)
        with patch.object(Path, 'open', return_value=source), \
             patch.object(source, 'read', wraps=source.read) as read, \
             patch.object(readiness.json, 'loads', side_effect=AssertionError('unauthenticated parse')):
            with self.assertRaisesRegex(ValueError, 'snapshot differs'): readiness.target50()
            read.assert_called_once_with(readiness.REGISTRY_BYTES+1)

    def test_two_candidate_pairs_alternate_arms_and_reserve_prior_dev_seeds(self):
        value = readiness.manifest()
        pilot = value['pilot']
        self.assertEqual(pilot['candidate_seeds'], [1152, 1153])
        self.assertEqual([row['arm'] for row in pilot['schedule']],
                         ['xiaomi_only', 'xiaomi_clef', 'xiaomi_clef', 'xiaomi_only'])
        prior = pilot['prior_dev_used_seeds'] + pilot['prior_dev_reserved_seeds']
        self.assertFalse(set(pilot['candidate_seeds']) & set(prior))
        for pair in (0, 1):
            a, b = pilot['schedule'][pair*2:pair*2+2]
            self.assertEqual({k: v for k, v in a.items() if k != 'arm'},
                             {k: v for k, v in b.items() if k != 'arm'})
            self.assertEqual(a['task_index'], 3)
            self.assertEqual(a['horizon'], 600)
        self.assertEqual((pilot['max_controls'], pilot['max_observations'], pilot['max_fixed16_xiaomi_queries']),
                         (2400, 2404, 152))

    def test_missing_inputs_and_unexecuted_gates_cannot_become_readiness(self):
        value = readiness.manifest()
        self.assertFalse(value['launch_ready'])
        self.assertFalse(value['paid_authorized'])
        self.assertFalse(value['cloud_actions'])
        self.assertTrue(all(v is None for v in value['inputs'].values()))
        self.assertFalse(value['protocol']['final_unseen_task_tuning'])
        self.assertFalse(value['xiaomi_model']['local_weights_verified'])
        self.assertTrue(all(row['status'] != 'passed' for row in value['preflight_matrix']))
        self.assertIn('provisional', value['pilot']['unused_seed_status'])
        prior = value['prior_evidence']
        self.assertFalse(prior['dev1150_failed_staging_attempt']['this_attempt_recording_available'])
        later = prior['later_dev1150_archives']
        self.assertTrue(later['potential_unpaired_xiaomi_baseline'])
        self.assertFalse(later['receipt_reconciled'])
        self.assertFalse(later['retained_xiaomi_clef_pair_established'])
        self.assertTrue(later['mac_rehash_matches_receipts'])

    def test_recovered_identity_remains_distinct_from_cloud_execution_proof(self):
        value = readiness.manifest()
        model = value['clef_model']
        self.assertEqual(model['repo'], 'Cloudflare/clef-flash')
        self.assertEqual(model['revision'], 'fde727a287004204b7518dcc983fe64379776712')
        self.assertEqual(model['reported_snapshot_bytes'], 19083377419)
        self.assertEqual(model['head']['sha256'], '19cdcec8c81dc9212be320fff47462ab342fbc1278be4368fb3da71241cf5ba0')
        self.assertFalse(model['cloud_weights_verified'])
        self.assertFalse(value['clef_gate']['data_usefulness_intervenes'])
        self.assertFalse(value['clef_gate']['simulator_mediated_intervention_ever_run'])
        self.assertIsNone(value['clef_gate']['query_budget'])
        archives = value['prior_evidence']['later_dev1150_archives']['archives']
        self.assertEqual([(a['cadence'], a['bytes'], a['controls'], a['queries']) for a in archives],
                         [(16, 163737600, 211, 14), (8, 204820480, 224, 28)])
        self.assertEqual(archives[0]['sha256'], '75283c1f846bc1e4e67ae89185fbe51a7a0e2341b9938c7140b4c75641eeb44b')
        self.assertEqual(archives[1]['sha256'], '80511e4d15184a6184c336df4ca651163c8209cceace7c44fe03afc0f374914b')

    def test_continuous_clock_exposes_stale_and_exhausted_candidate_design_gap(self):
        p = readiness.freshness_plan()
        self.assertEqual(p['freshness_budget_ms'], 100)
        self.assertEqual((p['scorer_boundary_controls'], p['perfect_overlap_boundary_controls'],
                          p['serial_boundary_controls']), (18, 19, 37))
        self.assertGreater(p['actor_boundary_controls'], p['candidate_chunk_controls'])
        self.assertEqual(p['illustrative_actor_usable_suffix'], 0)
        self.assertFalse(p['recorded_path_fits_freshness'])
        self.assertIsNone(p['selected_design'])
        self.assertFalse(p['runtime_gate_tests_run'])

    def test_manifest_is_deterministic_content_bound_and_has_no_launch_option(self):
        before = readiness.prepare()
        self.assertEqual(before, readiness.prepare())
        self.assertEqual(before['manifest_sha256'], hashlib.sha256(canonical(before['manifest'])).hexdigest())
        changed = copy.deepcopy(before['manifest']); changed['pilot']['num_trials'] = 2
        self.assertNotEqual(before['manifest_sha256'], hashlib.sha256(canonical(changed)).hexdigest())
        script = readiness.ROOT/'tools/prepare_clef_pilot.py'
        result = subprocess.run([sys.executable, str(script)], capture_output=True, text=True, timeout=30, check=True)
        self.assertEqual(json.loads(result.stdout), before)
        result = subprocess.run([sys.executable, str(script), '--execute'], capture_output=True, text=True, timeout=30)
        self.assertNotEqual(result.returncode, 0)
        self.assertEqual(result.stdout, '')

    def test_existing_local_view_keeps_ee_first_padding_and_causal_history(self):
        # Fixture execution of our view, not simulator/model or Clef parity.
        state = [1., 2., 3., 0., 0., 0., 1., 4., 5., 6., 0., 0., 0., 1., .1, .2]
        rows = [{'observation.state': state, 'action': list(range(12)), 'timestamp': i/20} for i in range(10)]
        original = copy.deepcopy(rows)
        record = {'length': 10, 'episode_id': 'offline', 'instruction': 'fixture',
                  'action_layout': 'EEtranslation3/EErotation3/gripper1/base4/mode1'}
        for cutoff in range(10):
            value = window(rows, record, cutoff)
            self.assertEqual(value['history_indices'], [max(0, cutoff+d) for d in (-6, -4, -2, 0)])
            self.assertEqual(value['state'][0][:3], [4., 5., 6.])
            self.assertEqual(value['state'][0][8:11], [1., 2., 3.])
            self.assertEqual(value['state'][0][14:], [0.]*46)
            self.assertEqual(len(value['state'][0]), 60)
        self.assertEqual(rows, original)


OFFICIAL_ENTRY = os.environ.get('COPPERTOP_OFFICIAL_ENTRY')
OFFICIAL_REGISTRY = os.environ.get('COPPERTOP_ROBOCASA_REGISTRY')


@unittest.skipUnless(OFFICIAL_ENTRY, 'Bounded pinned official evaluator source not supplied')
class OfficialPurePreprocessingTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        raw = Path(OFFICIAL_ENTRY).read_bytes()
        if hashlib.sha256(raw).hexdigest() != readiness.ENTRY_SHA256:
            raise ValueError('Pinned official evaluator bytes required')
        # Execute only authenticated pure utilities and EvalClient with an injected
        # processor/transport. This is not a rollout, model load or official loader.
        names = {'quat_xyzw_to_axis_angle', 'observation_to_state', 'sample_history',
                 'center_crop', 'collect_images', 'EvalClient'}
        tree = ast.parse(raw)
        constants = {node.targets[0].id: ast.literal_eval(node.value) for node in tree.body
                     if isinstance(node, ast.Assign) and isinstance(node.targets[0], ast.Name)
                     and node.targets[0].id in {'CAMERA_KEYS', 'STATE_DIM', 'ACTION_DIM'}}
        recipe = readiness.manifest()['recipe']
        if (constants['CAMERA_KEYS'] != tuple(recipe['camera_keys'])
                or constants['STATE_DIM'] != recipe['state_padded_dim']
                or constants['ACTION_DIM'] != recipe['decoded_action_dim']):
            raise ValueError('Manifest camera order or state/action dimensions differ from official constants')
        nodes = [node for node in tree.body
                 if isinstance(node, (ast.FunctionDef, ast.ClassDef)) and node.name in names]
        namespace = {'np': np, 'Image': Image, 'collections': collections, 'Any': object, **constants}
        exec(compile(ast.Module(body=nodes, type_ignores=[]), str(OFFICIAL_ENTRY), 'exec'), namespace)
        cls.official = SimpleNamespace(**{name: namespace[name] for name in names})

    def test_history_repeats_first_observation_and_matches_view_at_all_queue_lengths(self):
        for cutoff in range(10):
            indices, _ = window_indices(cutoff, 10, (-6, -4, -2, 0))
            history = collections.deque(range(max(0, cutoff-6), cutoff+1), maxlen=7)
            self.assertEqual(self.official.sample_history(history, 4, 2).tolist(), list(indices))

    def test_crop_truncates_to_243_square_and_bilinearly_resizes_without_mutation(self):
        image = np.arange(256*256*3, dtype=np.uint8).reshape(256, 256, 3)
        original = image.copy()
        expected = Image.fromarray(image).crop((6, 6, 249, 249)).resize((256, 256), Image.Resampling.BILINEAR)
        actual = self.official.center_crop(image, .95)
        np.testing.assert_array_equal(np.asarray(actual), np.asarray(expected))
        np.testing.assert_array_equal(image, original)

    def test_ee_first_axis_angle_state_matches_existing_local_view(self):
        source = [1., 2., 3., 0., 0., np.sin(.2), np.cos(.2),
                  4., 5., 6., np.sin(.3), 0., 0., np.cos(.3), .1, .2]
        obs = {'state.base_position': source[:3], 'state.base_rotation': source[3:7],
               'state.end_effector_position_relative': source[7:10],
               'state.end_effector_rotation_relative': source[10:14], 'state.gripper_qpos': source[14:]}
        np.testing.assert_allclose(self.official.observation_to_state(obs), native_state(source), atol=1e-7, rtol=0)

    def test_three_camera_order_state_padding_and_first_twelve_action_decode(self):
        client = self.official.EvalClient.__new__(self.official.EvalClient)
        captured = {}
        class Processor:
            def apply_chat_template(self, messages, **kwargs):
                captured.update(messages=messages, **kwargs)
                return {'inputs': 'fixture'}
        class Tensor:
            def __init__(self, data): self.data = data
            def __getitem__(self, key): return Tensor(self.data[key])
            def float(self): return self
            def cpu(self): return self
            def numpy(self): return self.data
        actions = np.arange(16*60, dtype=np.float32).reshape(1, 16, 60)
        def transport(**kwargs):
            captured['request'] = kwargs
            return Tensor(actions)
        client.processor, client.client = Processor(), transport
        client.crop_ratio, client.robot_type = .95, 'robocasa365'
        cameras = readiness.manifest()['recipe']['camera_keys']
        images = {key: np.full((4, 256, 256, 3), index, dtype=np.uint8) for index, key in enumerate(cameras)}
        states = np.arange(4*14, dtype=np.float32).reshape(4, 14)
        decoded = client.infer(states, images, 'fixture')
        np.testing.assert_array_equal(decoded, actions[0, :, :12])
        self.assertEqual(decoded.shape, (16, 12))
        self.assertEqual(decoded.dtype, np.float32)
        np.testing.assert_array_equal(captured['state'][0, :, :14], states)
        np.testing.assert_array_equal(captured['state'][0, :, 14:], np.zeros((4, 46)))
        videos = [item['video'] for item in captured['messages'][0]['content'] if item['type'] == 'video']
        self.assertEqual([np.asarray(video[0])[0, 0, 0] for video in videos], [0, 1, 2])
        self.assertFalse(captured['do_resize'])
        self.assertEqual(captured['request']['task_id'], 'robocasa365')


@unittest.skipUnless(OFFICIAL_REGISTRY, 'Bounded pinned official registry source not supplied')
class OfficialRegistryTests(unittest.TestCase):
    def test_snapshot_is_exact_ast_projection_of_pinned_registry(self):
        snapshot_raw = (readiness.ROOT/'containers/robocasa-target50.json').read_bytes()
        self.assertEqual(hashlib.sha256(snapshot_raw).hexdigest(), readiness.REGISTRY_SHA256)
        snapshot = json.loads(snapshot_raw)
        raw = Path(OFFICIAL_REGISTRY).read_bytes()
        self.assertEqual(hashlib.sha256(raw).hexdigest(), snapshot['source']['sha256'])
        assignments = {node.targets[0].id: node.value for node in ast.parse(raw).body
                       if isinstance(node, ast.Assign) and isinstance(node.targets[0], ast.Name)}
        groups = {kw.arg: ast.literal_eval(kw.value) for kw in assignments['TARGET_TASKS'].keywords}
        horizons = {kw.arg: next(ast.literal_eval(item.value) for item in kw.value.keywords if item.arg == 'horizon')
                    for name in ('ATOMIC_TASK_DATASETS', 'COMPOSITE_TASK_DATASETS')
                    for kw in assignments[name].keywords}
        expected = []
        for group in ('atomic_seen', 'composite_seen', 'composite_unseen'):
            for task in groups[group]:
                expected.append({'index': len(expected), 'task': task, 'group': group, 'horizon': horizons[task]})
        self.assertEqual(readiness.target50(), expected)
