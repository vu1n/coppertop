"""Offline Xiaomi/Clef pilot preparation; there is deliberately no launch path."""
import hashlib
import json
import math
from pathlib import Path

from .chords import canonical
from .performance_eval import SOURCE_PINS, SOURCE_BLOBS
from .gpu_smoke import MODEL_REPO, MODEL_REVISION
from .evaluation_audit import seed_ledger

BASE_COMMIT = '46701a30b31713d0393d069df7427daff875217d'
REGISTRY_SHA256 = '607b3a94856df1e522ac849165fd0af1ebcbc53d35ca5b56f75827da616ddb02'
REGISTRY_BYTES = 6687
ENTRY_SHA256 = '18d70fade4c990210dc5b9c34285c8030eac142d9c332066c73335e1c0aaa28e'
ARMS = ('xiaomi_only', 'xiaomi_clef')
NUM_TRIALS = 50
CLEF_REPO = 'Cloudflare/clef-flash'
CLEF_REVISION = 'fde727a287004204b7518dcc983fe64379776712'
CLEF_HEAD_SHA256 = '19cdcec8c81dc9212be320fff47462ab342fbc1278be4368fb3da71241cf5ba0'
RECOVERY_COMMIT = 'c381330c16dd43d75226af845555c795558cd78b'
RECOVERY_REVIEW_COMMIT = 'de6eb381025b9b737d5a3ab536881aede1106e3a'
RECOVERY_INPUTS = {
    'source-provenance.json': '372adbebebfdfbca7cef293963c279dbf5edd702dc707d29bd3d602aad8b0364',
    'model-release.json': '04f2b73887a050974eb57924978ad157deb837c692d82c3390821d55d961a386',
    'config.json': '66f87f6fb2616b46604daf2a9c67ddc87938296d07156efa34d59b5be49e3238',
    'dev-archive-identities.json': '0b18bdc50073e78508b1363440eb08cc3314a15bef00775d30315ef4da935d30'}
CLEF_LOCK_SHA256 = '8f7dd61474e3b4a0f8b40eb014cd04a46fe14263835b9f7fa7d8a9b7b5634019'
ROOT = Path(__file__).resolve().parents[1]
FILES = ('coppertop/evaluation_readiness.py', 'tools/prepare_clef_pilot.py',
         'containers/robocasa-target50.json', 'containers/gpu-smoke-pins.json',
         'coppertop/performance_eval.py', 'coppertop/gpu_smoke.py', 'coppertop/chords.py',
         'requirements-simulator.lock', 'requirements-gpu-smoke.lock',
         'coppertop/decision_gate.py', 'coppertop/decision_runner.py',
         'coppertop/clef_cache.py', 'coppertop/colocation_worker.py',
         'coppertop/colocation_stage.py', 'containers/clef-runtime.lock',
         'coppertop/evaluation_audit.py', 'coppertop/evaluation_episode.py',
         'coppertop/evaluation_roundtrip.py', 'coppertop/record_archive.py',
         'tools/verify_clef_readiness.py', 'manifests/clef-review-derivatives.json',
         *(f'manifests/clef-recovery/{name}' for name in RECOVERY_INPUTS))


def recovery_inputs():
    values = {}
    for name, expected in RECOVERY_INPUTS.items():
        with (ROOT/'manifests/clef-recovery'/name).open('rb') as source:
            raw = source.read(32769)
        if hashlib.sha256(raw).hexdigest() != expected:
            raise ValueError('Pinned recovery metadata differs')
        values[name] = json.loads(raw)
    with (ROOT/'containers/clef-runtime.lock').open('rb') as source:
        if hashlib.file_digest(source, 'sha256').hexdigest() != CLEF_LOCK_SHA256:
            raise ValueError('Pinned Clef runtime lock differs')
    return values


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


def freshness_plan():
    """Illustrative clock arithmetic from reported timings, not a latency bound."""
    control_ms, age_limit, chunk_controls = 50, 2, 16
    scorer_ms, serial_actor_ms, overlap_scorer_ms, actor_ms = 895, 399, 1134, 918
    overlap_controls = math.ceil(max(overlap_scorer_ms, actor_ms)/control_ms)
    return {'clock_assumption': 'continuously advancing controls at 20Hz; admission at next control boundary',
        'control_ms': control_ms, 'max_age_controls': age_limit,
        'freshness_budget_ms': control_ms*age_limit,
        'reported_approx_scorer_ms': scorer_ms, 'reported_approx_overlap_actor_ms': actor_ms,
        'reported_approx_serial_actor_ms': serial_actor_ms,
        'reported_approx_overlap_scorer_ms': overlap_scorer_ms,
        'scorer_boundary_controls': math.ceil(scorer_ms/control_ms),
        'perfect_overlap_boundary_controls': overlap_controls,
        'serial_boundary_controls': math.ceil((scorer_ms+serial_actor_ms)/control_ms),
        'actor_boundary_controls': math.ceil(actor_ms/control_ms),
        'candidate_chunk_controls': chunk_controls,
        'candidate_window_ms': chunk_controls*control_ms,
        'illustrative_actor_usable_suffix': max(0, chunk_controls-math.ceil(actor_ms/control_ms)),
        'recorded_path_fits_freshness': max(overlap_scorer_ms, actor_ms) <= control_ms*age_limit,
        'scope': 'these reported timings under this clock; not every future latency or an unpaced/paused simulator',
        'selected_design': None, 'recommended_design': 'paused_synchronous_functional_pilot',
        'decision_required': 'pause controls during scoring/replanning for functional proof, or qualify continuous async latency first',
        'async_requirements': 'old queue continues; admission checks measured control-index age and nonempty unexpired candidate suffix',
        'synchronous_requirements': 'freeze simulator at predeclared decision boundary; report pause/load/score/actor/total wall time; no real-time claim',
        'rounding_contract': 'self-hosted joint head explicitly selects GatePolicy(probability_decimals=4); generic default remains strict',
        'fixture_gate_tests': 'tests/test_evaluation_intervention.py; tests/test_evaluation_async.py',
        'runtime_gate_tests_run': False}


def pilot_proposal():
    """Unselected failure caps, not qualified performance or paid authorization."""
    return {
        'kind': 'paused_functional_four_episode_proposal',
        'selected': False, 'runtime_qualified': False, 'paid_authorized': False,
        'clock_mode': 'paused_synchronous', 'horizon_controls': 600,
        'decision_completed_controls': [8], 'execute_controls': 16,
        'replan_probability_min': .8, 'margin_min': .2, 'max_age_controls': 2,
        'actor_seed': 20261006,
        'actor_seed_scope': 'proposed explicit per-query seed; real stock parity remains required',
        'per_stock_episode': {'actor_calls': 38, 'scorer_calls': 0, 'input_calls': 38},
        'per_clef_episode': {'actor_calls': 39, 'scorer_calls': 1, 'input_calls': 40,
            'extra_actor_calls': 1},
        'four_episode_caps': {'controls': 2400, 'observations': 2404,
            'actor_calls': 154, 'scorer_calls': 2, 'input_calls': 156},
        'automatic_retries': 0,
        'proposed_core_deadlines_seconds': {'episode': 435, 'reset_and_initialization_capture': 30,
            'input': .5, 'actor': 5, 'scorer': 5, 'step_and_audit': .25,
            'total_cooperative_cleanup': 10, 'score_actor_handoff': 10},
        'core_component_sum_seconds': {'stock': 399, 'clef': 410},
        'proposed_outer_deadlines_seconds': {'startup': 90, 'actor_load': 120,
            'optional_clef_load': 120, 'archive_commit': 120, 'process_teardown': 30,
            'stock_job': 795, 'clef_job': 915, 'four_episode_jobs': 3420},
        'model_load_order': 'Clef first when enabled, then actor; fresh models per episode',
        'actor_lifetime_constraint': 'proposed 600s alarm: actor load120 +core435 <600; qualify actual launcher',
        'deadline_scope': 'proposed hard failure caps, not measured completion bounds; CPU callbacks require cooperative cancellation and an outer process supervisor',
        'existing_launcher': '450s colocation benchmark is not this pilot launcher',
        'fallback': {
            'uncertain_or_unchanged': 'retain existing queue; no additional opportunity or retry',
            'scorer_or_extra_actor_failure': 'retain only unexpired old queue; journal failure and infrastructure-invalid outcome',
            'queue_exhaustion': 'one bounded normal actor query or infrastructure-invalid stop; never replay expired controls',
            'terminal': 'stop on explicit success/termination/truncation; never infer success from done',
            'horizon': 'stop after600 controls with explicit horizon outcome; do not fabricate success',
            'cancellation_or_cleanup_failure': 'preserve partial evidence and primary error; stop owned worker process through separate outer supervisor'},
        'storage': {'fixture_max_bytes': 16*1024**2, 'fixture_scope': 'local synthetic accounting/Tape closure only',
            'production_raw_bytes_per_episode_cap': 1024**3,
            'production_objects_per_episode_cap': 6000,
            'production_archive_bytes_per_episode_cap': 1024**3+16*1024**2,
            'destination': None, 'retention_seconds': None, 'actual_commit_reload_verified': False},
        'quote': {'monetary_amount': None, 'rates_verified': False,
            'formula': 'sum(all job seconds * compatible GPU/CPU/RAM rates) +retained storage +requests +readback/egress +declared margin',
            'gpu_episode_seconds_cap': 3420,
            'additional_cpu_job_bounds_required': ['conditional image rebuild/cache staging',
                'runtime/processor/parity qualification', 'actual durable fixture commit/reload',
                'result publication/readback/closure and cleanup'],
            'missing_inputs': ['selected resource/region and peak model/activation/KV/host memory',
                'current image availability/digest and actual runtime payload hashes',
                'qualified startup/load/input/query/step/archive bounds within proposed caps',
                'actual durable destination/retention/object counts and complete hash readback',
                'fresh rates and preserved cumulative spend ledger',
                'bounded qualification/probe scope and exact-plan monetary approval']}}


def manifest():
    rows = target50()
    recovered = recovery_inputs()
    available_ledger = seed_ledger()
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
        'clef_model': {'repo': CLEF_REPO, 'revision': CLEF_REVISION,
            'reported_snapshot_bytes': 19083377419, 'dtype': 'BF16',
            'architecture': 'Qwen3_5ForConditionalGeneration',
            'head': {'file': 'joint_head.safetensors', 'sha256': CLEF_HEAD_SHA256},
            'output': 'official joint-schema timing/usefulness probabilities; not native actions',
            'evidence': 'weight-free metadata authenticated from verified recovery commit; no local weights',
            'cloud_weights_verified': False},
        'clef_runtime': {'torch': '2.11.0+cu128', 'transformers': '5.10.2',
            'reported_pinned_wheels': 56, 'wheel_lock_available': True,
            'wheel_lock_sha256': CLEF_LOCK_SHA256, 'wheel_runtime_executed': False,
            'shared_xiaomi_environment_qualified': False},
        'inputs': {
            'clef_code_commit': RECOVERY_COMMIT,
            'clef_snapshot_manifest_sha256': RECOVERY_INPUTS['model-release.json'],
            'clef_config_sha256': RECOVERY_INPUTS['config.json'],
            'clef_intervention_spec': None, 'complete_dev_seed_ledger': None,
            'runtime_image_digest': None, 'fresh_timeout_storage_cost_quote': None},
        'clef_gate': {'replan_probability_min': .8, 'margin_min': .2,
            'max_age_controls': 2, 'query_budget': None,
            'data_usefulness_intervenes': False,
            'native_action_source': 'frozen Xiaomi actor; Clef may authorize replacement',
            'async_semantics': 'continue old queue during scoring/actor inference; discard elapsed candidate offsets',
            'fallback': 'stale/failed/uncertain retains old queue; empty-queue handling must be specified',
            'probability_contract': 'explicit four-decimal parser/gate parity; no silent normalization',
            'simulator_mediated_intervention_ever_run': False},
        'freshness_design': freshness_plan(),
        'pilot_proposal': pilot_proposal(),
        'available_dev_seed_audit': available_ledger,
        'recovery': {'commit': RECOVERY_COMMIT, 'review_correction_commit': RECOVERY_REVIEW_COMMIT, 'provenance_sha256': RECOVERY_INPUTS['source-provenance.json'],
            'originals': 'recovery provenance binds c381330 original bytes; preparation_sources binds current derivatives',
            'original_copied_file_count': len(recovered['source-provenance.json']['copied_files']),
            'reference_code_vendored': False, 'runtime_parity_verified': False,
            'release_inventory_scope': 'file sizes and LFS hashes; not a whole-snapshot byte hash or local weight verification'},
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
            'prior_dev_used_seeds': recovered['dev-archive-identities.json']['development_seeds'],
            'prior_dev_reserved_seeds': recovered['dev-archive-identities.json']['additional_holdout_exclusion'],
            'prior_dev_explored_seeds': available_ledger['explored'],
            'conservative_seed_exclusions': available_ledger['exclusions'],
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
            'query_bound_scope': 'stock execute16 only; extra actor/Clef/probe queries need separate bounds',
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
                'status': 'parent-verified Mac local rehash matches receipts; remote R2 not freshly rechecked',
                'source_thread': '01a10b0d-dfbe-73a4-85b9-d3731fb24083',
                'run_ids': [row['attempt'] for row in recovered['dev-archive-identities.json']['archives']],
                'archive_refs': None,
                'release_sha256': [row['release'] for row in recovered['dev-archive-identities.json']['archives']],
                'identity_manifest_sha256': RECOVERY_INPUTS['dev-archive-identities.json'],
                'receipt_reconciled': False, 'mac_rehash_matches_receipts': True,
                'archives': [{'cadence': row['cadence'], 'bytes': row['archive_bytes'],
                    'sha256': row['archive_sha256'], 'recorded_success': row['recorded_success'],
                    'controls': row['controls'], 'queries': row['query_calls']}
                    for row in recovered['dev-archive-identities.json']['archives']],
                'initialization': {'base_seed': 1000, 'task_index': 3, 'episode_index': 0,
                    'num_trials': 50, 'seed': 1150, 'actor_query_seed': 20261006},
                'potential_unpaired_xiaomi_baseline': True,
                'retained_xiaomi_clef_pair_established': False},
            'clef_latency_24_calls': {'status': 'parent verified historical 24-call completion and both Apps stopped; not rollout-success evidence',
                'historical_report': '/Users/vuln/Documents/Codex/2026-10-05/task/coppertop-publish/docs/clef-colocation.md',
                'report_available_in_cloud_dev': True,
                'implementation_hash_receipt': RECOVERY_INPUTS['source-provenance.json'],
                'mac_inventory_recovered': True, 'source_only_migration_commit': RECOVERY_COMMIT},
            'one_update_head': 'smoke-only; never substitute for Clef'},
        'preflight_matrix': [
            {'gate': 'pins_seed_stride_horizons', 'status': 'offline_checkable',
             'test': 'tests/test_evaluation_readiness.py'},
            {'gate': 'history_crop_state_padding_action_decode', 'status': 'offline_checkable',
             'test': 'tests/test_evaluation_readiness.py; optional hash-authenticated pure official functions'},
            {'gate': 'clef_code_config_identity', 'status': 'offline_checkable'},
            {'gate': 'clef_weights_runtime_intervention', 'status': 'not_run'},
            {'gate': 'four_decimal_rounding_and_gate_parity', 'status': 'offline_checkable',
             'test': 'tests/test_evaluation_intervention.py'},
            {'gate': 'explicit_sync_async_clock_design', 'status': 'decision_required'},
            {'gate': 'complete_unused_dev_seed_ledger', 'status': 'missing_input'},
            {'gate': 'available_source_seed_receipt_audit', 'status': 'offline_checkable',
             'test': 'tests/test_evaluation_audit.py'},
            {'gate': 'full600_scheduler_journal', 'status': 'offline_fixture_checkable',
             'test': 'tests/test_evaluation_episode.py'},
            {'gate': 'bounded_local_artifact_roundtrip', 'status': 'offline_fixture_checkable',
             'test': 'tests/test_evaluation_roundtrip.py'},
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
            'selected clock design and frozen decision opportunities/score budget/actor budget/empty-queue fallback',
            'Clef 19083377419-byte cache and 56-wheel lock; isolated runtime compatibility and peak model/activation/KV memory',
            'qualified latency distribution and actual control-index age, candidate suffix and stale/failed/unchanged counters',
            'fresh rates and preserved cumulative spend ledger; exact-plan financial authorization'],
        'sources': ['https://robocasa.ai/docs/build/html/benchmarking/multitask_learning.html',
            'https://github.com/XiaomiRobotics/Xiaomi-Robotics-1/blob/'+SOURCE_PINS['xiaomi']+'/eval_robocasa365/entry.py',
            'https://github.com/robocasa/robocasa/blob/'+SOURCE_PINS['robocasa']+'/robocasa/utils/dataset_registry.py'],
    }


def prepare():
    value = manifest()
    return {'manifest': value, 'manifest_sha256': hashlib.sha256(canonical(value)).hexdigest(),
            'benchmark_matrix': benchmark_matrix()}
