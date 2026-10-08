"""Offline plan and paired metrics for a small official RoboCasa365 evaluation."""
import hashlib
import math
from pathlib import Path
from .chords import canonical
from .gpu_smoke import MODEL_REPO, MODEL_REVISION
from .reload_launch import HEAD, RECEIPT

SOURCE_PINS = {
    'xiaomi': '0dd7aef8dc87296246aae812a1f59ccb708e5546',
    'robocasa': '456174f62b89b8fca99eaaf33949c29fec9cfc2a',
    'robosuite': '5ce6643f3092639d08f7b0f90ed1c6a84f50552c',
}
SOURCE_BLOBS = {
    'eval_robocasa365/entry.py': '03202dbd056110ea1087a4cf4e9d76fcd9848913',
    'deploy/client.py': '1944e320b1d03fdab6280cd89677e820fee4e477',
    'deploy/server.py': 'b1794c342cc90a451a10ca2cd12acf2ce200b021',
}
ARMS = ('official', 'one_update')
DEV_SEEDS = (8, 9, 12, 15, 23)
CACHED_IMAGE = 'im-uMab88d0cwA6MALkcTf5VW'
CPU_REPAIR_PLAN = 'ba018f0d7a8f88f55df8a6d4ac53c9ed7a0758d585bdaa4364bbf0d7434fd78f'
CPU_REPAIR_RECEIPT = 'ac3276dc2a381ee4b72c129557d920b67e2f1b920e133a68a315e6d86470bbf2'
RUNTIME_FILES = ('coppertop/performance_eval.py', 'coppertop/performance_runtime.py',
                 'coppertop/performance_cloud.py', 'coppertop/reload_launch.py',
                 'coppertop/dependency_gate.py', 'coppertop/image_check.py',
                 'requirements-simulator.lock')
RECORDING_FILES=('coppertop/official_recording.py','coppertop/diagnostics.py',
                 'coppertop/chords.py','coppertop/streams.py')


def runtime_sources(*, recording=False):
    root = Path(__file__).resolve().parents[1]
    return {name: hashlib.sha256((root/name).read_bytes()).hexdigest()
            for name in RUNTIME_FILES+(RECORDING_FILES if recording else ())}


def plan():
    # Keep the official 50-trial indexing when selecting just two trials.
    return {
        'schema_version': 1, 'kind': 'paired_robocasa365_pilot',
        'cached_image': CACHED_IMAGE, 'runtime_sources_sha256': runtime_sources(),
        'image_validation': {'cpu_repair_plan_sha256': CPU_REPAIR_PLAN,
            'cpu_validation_receipt_sha256': CPU_REPAIR_RECEIPT,
            'native_interpreter': '/opt/conda/bin/python',
            'simulator_interpreter': '/work/sim/bin/python',
            'gpu_execution_verified': False},
        'model': {'repo': MODEL_REPO, 'revision': MODEL_REVISION},
        'source_pins': dict(SOURCE_PINS), 'source_blobs': dict(SOURCE_BLOBS),
        'checkpoint': {'sha256': HEAD, 'bytes': 1230711168, 'receipt': RECEIPT,
                       'scope': 'one SGD update on one CloseBlenderLid episode; native head only'},
        'arms': list(ARMS), 'task': 'CoffeeSetupMug', 'task_set': 'target50',
        'task_index': 3, 'split': 'pretrain', 'horizon': 600,
        'base_seed': 1000, 'schedule_trials': 50, 'episodes': [0, 1],
        'episode_seeds': [1150, 1151], 'policy_seed': 20261006,
        'recipe': {'obs_history': 4, 'obs_interval': 2, 'replan_steps': 16,
                   'crop_ratio': .95, 'robot_type': 'robocasa365'},
        'limits': {'gpu_seconds': 1800, 'server_seconds_each': 420,
                   'server_startup_seconds_each': 90, 'rollout_seconds_each': 330,
                   'report_bytes': 65536, 'rollouts': 4, 'automatic_retries': 0},
        'training_task_excluded': 'CloseBlenderLid', 'dev_seeds_excluded': list(DEV_SEEDS),
        'intervention_policy': None, 'scorer': None,
        'claim_scope': 'paired smoke-checkpoint behavior pilot; not a benchmark or utility estimate',
    }


def recording_acceptance_plan(*, full=False):
    p = plan()
    result = {'kind':'official_rollout_recording_acceptance', 'cloud_actions':False,
        'cached_image':p['cached_image'], 'source_pins':dict(SOURCE_PINS),
        'source_blobs':dict(SOURCE_BLOBS), 'model':p['model'], 'task':p['task'],
        'split':'development', 'source_split':'pretrain', 'seed':1150,
        'constructor_seed':1000, 'task_index':3, 'schedule_trials':50, 'episode_index':0,
        'recipe':{'obs_history':4,'obs_interval':2,'replan_steps':16,'crop_ratio':.95,
                  'robot_type':'robocasa365','video_stride':1,'video_fps':20},
        'limits':{'horizon':4,'query_calls':1,'record_bytes':24*1024**2,
                  'record_objects':64,'automatic_retries':0},
        'paid_authorized':False,
        'compute_proposal':{'gpu':'L40S','cpu':4,'memory_mib':65536,'phase_seconds':270,
            'session_seconds':240,'owned_server_cleanup_seconds':10,'startup_seconds':300,'retries':0},
        'publication_proposal':{'cpu_only':True,'objects':64,'requests':256,'bytes':24*1024**2,
            'temporary_session_seconds':3600,'approval_required':'fresh owned prefix, retention, current pricing and preserved spend ledger'},
        'acceptance':['one official model query, at most4 controls,5 source observations',
            'native action/state/query-history byte checks and frame/video alignment',
            'local immutable release closure; later approved CPU/R2 readback and owned cleanup'],
        'runtime_sha256':runtime_sources(recording=True),
        'claim':'Official evaluator recording acceptance, not replay parity, utility or20Hz throughput',
        'later_comparison':'Separate matched-seed fixed16vs8 cadence experiment; no approval implied'}

    if full:
        result.update(kind='official_full_rollout_recording',
            limits={'horizon':600,'query_calls':38,'record_bytes':1024**3,
                    'record_objects':6000,'automatic_retries':0},
            compute_proposal={'gpu':'L40S','cpu':4,'memory_mib':65536,'phase_seconds':600,
                'session_seconds':550,'owned_server_cleanup_seconds':10,'startup_seconds':300,'retries':0},
            publication_proposal={'cpu_only':True,'objects':2,'requests':256,'bytes':1024**3+32*1024**2,
                'format':'coppertop-episode-tar','max_archive_bytes':1024**3+16*1024**2,
                'single_put_threshold_bytes':256*1024**2,'multipart_part_bytes':16*1024**2,
                'temporary_session_seconds':7200,
                'approval_required':'fresh owned prefix, retention, current pricing and spend ledger'},
            acceptance=['one frozen official DEV1150 rollout; success, environment end or horizon600',
                'all query histories, native state/actions and camera timing verified',
                'immutable release and complete CPU/R2 byte readback; owned compute/storage cleanup'])
    return result


def validate_plan(value):
    if value != plan():
        raise ValueError('Exact pinned paired evaluation plan required')
    return hashlib.sha256(canonical(value)).hexdigest()


def runtime_image(sdk, image, root, *, recording=False):
    """Mount reviewed source onto the existing image; no build/install/lookup."""
    if sdk.__version__ != '1.6.1' or image.object_id != CACHED_IMAGE:
        raise ValueError('Exact previously verified warm image and SDK required')
    root = Path(root)
    for name, expected in runtime_sources(recording=recording).items():
        path = root/name
        if path.is_symlink() or hashlib.sha256(path.read_bytes()).hexdigest() != expected:
            raise ValueError('Runtime upload source differs')
        image = image.add_local_file(path, '/app/'+name, copy=False)
    return image


def wilson(successes, total):
    if type(total) is not int or total < 1 or type(successes) is not int or not 0 <= successes <= total:
        raise ValueError('Valid completed episode counts required')
    z = 1.959963984540054
    p = successes / total
    denominator = 1 + z*z/total
    center = (p + z*z/(2*total)) / denominator
    radius = z * math.sqrt(p*(1-p)/total + z*z/(4*total*total)) / denominator
    return [max(0., center-radius), min(1., center+radius)]


def summarize(value, records):
    """Errors/timeouts and unmatched initial states cannot become policy failures."""
    validate_plan(value)
    if len(records) != value['limits']['rollouts']:
        raise ValueError('Exactly four completed rollouts required')
    indexed = {}
    for row in records:
        key = (row['arm'], row['seed'])
        if key in indexed or row['arm'] not in ARMS or row['seed'] not in value['episode_seeds']:
            raise ValueError('Duplicate or out-of-schedule rollout')
        if row.get('status') != 'completed' or type(row.get('success')) is not bool:
            raise ValueError('A simulator error or timeout is not a policy failure')
        if (row.get('task') != value['task'] or type(row.get('steps')) is not int
            or not 1 <= row['steps'] <= value['horizon']
            or not row.get('initial_observation_sha256')):
            raise ValueError('Episode task, steps or initial-state guard missing')
        for name in ('wall_seconds', 'inference_seconds', 'model_load_seconds'):
            metric = row.get(name)
            if type(metric) not in (int, float) or not math.isfinite(metric) or metric < 0:
                raise ValueError('Invalid runtime metric')
        indexed[key] = row
    gains = losses = 0
    for seed in value['episode_seeds']:
        a, b = (indexed[(arm, seed)] for arm in ARMS)
        if a['initial_observation_sha256'] != b['initial_observation_sha256']:
            raise ValueError('Paired initial observations differ; comparison invalid')
        gains += int(b['success'] and not a['success'])
        losses += int(a['success'] and not b['success'])
    arms = {}
    for arm in ARMS:
        rows = [indexed[(arm, seed)] for seed in value['episode_seeds']]
        count = sum(int(row['success']) for row in rows)
        arms[arm] = {'episodes': len(rows), 'successes': count,
                     'success_rate': count/len(rows), 'wilson_95': wilson(count, len(rows)),
                     'steps': [row['steps'] for row in rows],
                     'wall_seconds': sum(row['wall_seconds'] for row in rows),
                     'inference_seconds': sum(row['inference_seconds'] for row in rows),
                     'model_load_seconds': sum(row['model_load_seconds'] for row in rows)}
    discordant = gains + losses
    p = min(1., 2*sum(math.comb(discordant, k) for k in range(min(gains, losses)+1))/2**discordant) if discordant else 1.
    return {'kind': value['kind'], 'status': 'completed', 'arms': arms,
            'paired_gains': gains, 'paired_losses': losses,
            'paired_success_rate_difference': (gains-losses)/len(value['episode_seeds']),
            'exact_discordant_pair_p_value': p,
            'interventions': None, 'interventions_reason': 'No intervention policy was run',
            'claim_scope': value['claim_scope'], 'records': records}
