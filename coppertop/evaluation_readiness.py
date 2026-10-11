"""Offline Xiaomi/Clef pilot preparation; there is deliberately no launch path."""
import hashlib
import json
from pathlib import Path

from .chords import canonical
from .performance_eval import SOURCE_PINS, SOURCE_BLOBS
from .gpu_smoke import MODEL_REPO, MODEL_REVISION

BASE_COMMIT = '7d3f7e3cc0d48864ce940209efbd6add56109069'
REGISTRY_SHA256 = '607b3a94856df1e522ac849165fd0af1ebcbc53d35ca5b56f75827da616ddb02'
REGISTRY_BYTES = 6687
ENTRY_SHA256 = '18d70fade4c990210dc5b9c34285c8030eac142d9c332066c73335e1c0aaa28e'
ARMS = ('xiaomi_only', 'xiaomi_clef')
NUM_TRIALS = 50
ROOT = Path(__file__).resolve().parents[1]
FILES = ('coppertop/evaluation_readiness.py', 'tools/prepare_clef_pilot.py',
         'containers/robocasa-target50.json', 'containers/gpu-smoke-pins.json',
         'coppertop/performance_eval.py', 'coppertop/gpu_smoke.py', 'coppertop/chords.py',
         'requirements-simulator.lock', 'requirements-gpu-smoke.lock')


def target50():
    with (ROOT/'containers/robocasa-target50.json').open('rb') as source:
        raw = source.read(REGISTRY_BYTES+1)
    if hashlib.sha256(raw).hexdigest() != REGISTRY_SHA256:
        raise ValueError('Pinned target50 snapshot differs')
    return json.loads(raw)['tasks']


def episode_seed(task, episode, *, base_seed=1000, num_trials=NUM_TRIALS):
    if (type(num_trials) is not int or num_trials != NUM_TRIALS
            or type(base_seed) is not int or not 0 <= base_seed <= 2**32-2500
            or type(episode) is not int or not 0 <= episode < NUM_TRIALS):
        raise ValueError('Full 50-trial seed stride and bounded episode/base seed required')
    # Look up in the full registry, never enumerate a selected subset.
    row = next((row for row in target50() if row['task'] == task), None)
    if row is None:
        raise ValueError('Original target50 task required')
    return base_seed + row['index']*NUM_TRIALS + episode


def benchmark_matrix():
    return [dict(row, episodes_per_arm=NUM_TRIALS,
                 max_controls_per_arm=NUM_TRIALS*row['horizon'],
                 max_xiaomi_queries_per_episode=(row['horizon']+15)//16)
            for row in target50()]


def manifest():
    rows = target50()
    pilot = next(row for row in rows if row['task'] == 'CoffeeSetupMug')
    episodes = [2, 3]
    schedule = []
    for pair, episode in enumerate(episodes):
        order = ARMS if pair % 2 == 0 else tuple(reversed(ARMS))
        for arm in order:
            schedule.append({'pair': pair, 'arm': arm, 'task': pilot['task'],
                'task_index': pilot['index'], 'episode_index': episode,
                'seed': episode_seed(pilot['task'], episode), 'horizon': pilot['horizon']})
    source_hashes = {}
    for name in FILES:
        with (ROOT/name).open('rb') as source:
            source_hashes[name] = hashlib.file_digest(source, 'sha256').hexdigest()
    return {
        'schema_version': 1, 'kind': 'retained_paired_xiaomi_clef_dev_pilot',
        'base_commit': BASE_COMMIT, 'stage': 'offline_preparation',
        'launch_ready': False, 'paid_authorized': False, 'cloud_actions': False,
        'source_pins': dict(SOURCE_PINS), 'source_blobs': dict(SOURCE_BLOBS),
        'evaluator_sha256': ENTRY_SHA256, 'preparation_sources_sha256': source_hashes,
        'xiaomi_model': {'repo': MODEL_REPO, 'revision': MODEL_REVISION,
            'file_hash_manifest': 'containers/gpu-smoke-pins.json',
            'local_weights_verified': False},
        'inputs': {
            'clef_code': None, 'clef_weights': None, 'clef_config': None,
            'clef_intervention_spec': None, 'complete_dev_seed_ledger': None,
            'runtime_image_digest': None, 'fresh_timeout_storage_cost_quote': None},
        'protocol': {'robocasa_version': '1.0.1', 'task_set': 'target50',
            'environment_split': 'pretrain', 'kitchen_object_split': 'pretrain',
            'groups': {'atomic_seen': 18, 'composite_seen': 16, 'composite_unseen': 16},
            'episodes_per_task_per_arm': NUM_TRIALS, 'episodes_per_arm': len(rows)*NUM_TRIALS,
            'horizon_range': [min(row['horizon'] for row in rows), max(row['horizon'] for row in rows)],
            'final_unseen_task_tuning': False},
        'recipe': {'obs_history': 4, 'obs_interval': 2, 'history_offsets': [-6, -4, -2, 0],
            'history_queue_length': 7, 'crop_ratio': .95, 'crop_resize': 'bilinear to original size',
            'replan_steps': 16, 'camera_shape': [256, 256, 3],
            'camera_keys': ['video.robot0_agentview_left', 'video.robot0_agentview_right',
                            'video.robot0_eye_in_hand'],
            'state': 'EE relative position3/axis-angle3/gripper2/base position3/axis-angle3',
            'state_native_dim': 14, 'state_padded_dim': 60, 'decoded_action_dim': 12,
            'action_conversion': 'pinned robocasa.utils.env_utils.convert_action',
            'success': 'info.success; never infer from done'},
        'pilot': {'eligibility': 'DEV', 'base_seed': 1000, 'num_trials': NUM_TRIALS,
            'seed_formula': 'base_seed + full_target50_index * num_trials + episode_index',
            'seed7': 'upstream default/reference, not a mandated rule',
            'policy_seed': 20261006, 'policy_seed_scope': 'same initial diffusion RNG after model load in each arm',
            'candidate_episodes': episodes, 'candidate_seeds': [row['seed'] for row in schedule[::2]],
            'unused_seed_status': 'provisional; complete historical DEV ledger required',
            'prior_dev_used_seeds': [8, 9, 12, 15, 23, 1150],
            'prior_dev_reserved_seeds': [1151],
            'seed_evidence': ['coppertop/performance_eval.py::DEV_SEEDS',
                'docs/performance-eval.md (scheduled 1150/1151)',
                'docs/full-rollout-recording.md (executed 1150)',
                'coppertop/mechanism_runtime.py (1150 feasibility configuration)'],
            'arms': {'xiaomi_only': 'frozen Xiaomi; Clef disabled',
                     'xiaomi_clef': 'same frozen Xiaomi plus actual Clef action intervention'},
            'schedule': schedule, 'same_initialization_required': True,
            'initialization_guard': 'native state, simulator integration state, model fields, all camera bytes, instruction',
            'fresh_model_and_simulator_per_episode': True, 'automatic_retries': 0,
            'termination': 'success, environment termination/truncation, or full registry horizon',
            'max_controls': 4*pilot['horizon'], 'max_observations': 4*(pilot['horizon']+1),
            'max_fixed16_xiaomi_queries': 4*((pilot['horizon']+15)//16),
            'query_bound_scope': 'stock execute16 only; extra Clef/probe queries need separate bounds',
            'claim_scope': 'two DEV pairs establish feasibility only; no statistical superiority claim'},
        'acceptance': {
            'clef_off_parity': 'same seeded inputs, decoded chunks, executed controls and query counts as stock Xiaomi',
            'actual_intervention': 'retain proposed/executed actions, Clef decisions and at least one attributable action change',
            'before_gpu': 'durable staging fixture commit/reload with archive and restored-byte hashes',
            'retained_results': 'both arms complete; raw observations/query inputs/actions plus videos and initial-state hashes; full artifact hash readback',
            'accounting': 'journal all submitted/completed/failed/timed-out queries; retain every scheduled episode outcome',
            'invalid_runs': 'infrastructure errors/timeouts/missing artifacts or unmatched initial states separate from policy outcomes',
            'freeze': 'pin Clef/model/config/evaluator/image before runs; no final unseen-task tuning'},
        'prior_evidence': {
            'dev1150_failed_staging_attempt': {'reported_success': True, 'controls': 211, 'queries': 14,
                'observations': 212, 'retained_metadata_bytes': 9194,
                'this_attempt_recording_available': False, 'usable_as_retained_pair': False},
            'later_dev1150_archives': {'cadences': [16, 8],
                'status': 'parent reports fully published and hash-verified; exact receipts not present at base commit',
                'source_thread': '01a10b0d-dfbe-73a4-85b9-d3731fb24083',
                'run_ids': None, 'archive_refs': None, 'release_sha256': None,
                'receipt_reconciled': False, 'potential_unpaired_xiaomi_baseline': True,
                'retained_xiaomi_clef_pair_established': False},
            'clef_latency_24_calls': {'status': 'parent verified historical 24-call completion and both Apps stopped; not rollout-success evidence',
                'historical_report': '/Users/vuln/Documents/Codex/2026-10-05/task/coppertop-publish/docs/clef-colocation.md',
                'report_available_in_cloud_dev': False, 'implementation_hash_receipt': None},
            'one_update_head': 'smoke-only; never substitute for Clef'},
        'preflight_matrix': [
            {'gate': 'pins_seed_stride_horizons', 'status': 'offline_checkable',
             'test': 'tests/test_evaluation_readiness.py'},
            {'gate': 'history_crop_state_padding_action_decode', 'status': 'offline_checkable',
             'test': 'tests/test_evaluation_readiness.py; optional hash-authenticated pure official functions'},
            {'gate': 'clef_code_weights_config_intervention', 'status': 'missing_inputs'},
            {'gate': 'complete_unused_dev_seed_ledger', 'status': 'missing_input'},
            {'gate': 'clef_off_parity_and_actual_intervention', 'status': 'not_run'},
            {'gate': 'durable_artifact_commit_reload', 'status': 'not_run'},
            {'gate': 'simulator_reset_render_matched_init', 'status': 'not_run'},
            {'gate': 'four_retained_closed_loop_episodes', 'status': 'not_run'},
            {'gate': 'fresh_bound_quote_and_paid_authorization', 'status': 'missing_inputs'}],
        'budget_inputs_required': ['exact Clef code/weight/config and image digests',
            'complete unused DEV seed ledger', 'all Xiaomi/Clef/probe query bounds and per-query timeouts',
            'per-episode startup/load/runtime timeouts covering full600 horizons',
            'CPU preflight/staging/readback/cleanup resource seconds and GPU resource seconds',
            'raw-array/video/archive byte and object bounds, durable retention/readback destination',
            'fresh rates and preserved cumulative spend ledger; exact-plan financial authorization'],
        'sources': ['https://robocasa.ai/docs/build/html/benchmarking/multitask_learning.html',
            'https://github.com/XiaomiRobotics/Xiaomi-Robotics-1/blob/'+SOURCE_PINS['xiaomi']+'/eval_robocasa365/entry.py',
            'https://github.com/robocasa/robocasa/blob/'+SOURCE_PINS['robocasa']+'/robocasa/utils/dataset_registry.py'],
    }


def prepare():
    value = manifest()
    return {'manifest': value, 'manifest_sha256': hashlib.sha256(canonical(value)).hexdigest(),
            'benchmark_matrix': benchmark_matrix()}
