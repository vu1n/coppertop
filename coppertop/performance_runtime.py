"""Offline preparation and an explicit CUDA runner; never allocates cloud resources."""
import argparse
import hashlib
import importlib.util
import json
import os
import re
import signal
import subprocess
import sys
import time
import traceback
from pathlib import Path
from .chords import canonical
from .performance_eval import plan, validate_plan, summarize, SOURCE_BLOBS, SOURCE_PINS
from .reload_launch import hard_deadline, read_json, save, HEAD
from .smoke_reload import verify_artifact, load_head

NATIVE = '/opt/conda/bin/python'
SIMULATOR = '/work/sim/bin/python'
OFFICIAL = Path('/work/xiaomi')
MODEL = Path('/work/model')
PORT = 10086
MODULE = 'coppertop.performance_runtime'
GATE_REASONS = {'Cached simulator package missing', 'Pinned simulator checkout revision differs',
    'Pinned simulator tracked content differs',
    'Official task registry/horizon changed', 'Completed six-archive cache receipt required',
    'Cached archive provenance incomplete', 'Cached simulator asset path/length differs',
    'Pinned cached asset inventory differs', 'Official source symlink rejected'} | {
    'Pinned simulator dependency differs: '+name for name in ('gymnasium', 'mujoco', 'transformers')} | {
    'Pinned official source differs: '+name for name in SOURCE_BLOBS}


def source_gate():
    for name, expected in SOURCE_BLOBS.items():
        path = OFFICIAL/name
        if path.is_symlink():
            raise ValueError('Official source symlink rejected')
        with path.open('rb') as source:
            raw = source.read(65537)
        if len(raw) > 65536 or hashlib.sha1(b'blob '+str(len(raw)).encode()+b'\0'+raw).hexdigest() != expected:
            raise ValueError('Pinned official source differs: '+name)


def load_official(name):
    source_gate()
    path = OFFICIAL/('deploy/server.py' if name == 'server' else 'eval_robocasa365/entry.py')
    sys.path.insert(0, str(OFFICIAL/'deploy'))
    spec = importlib.util.spec_from_file_location('coppertop_official_'+name, path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def simulator_gate(progress=lambda check: None):
    # Authenticate executable checkout contents before importing simulator code.
    for name in ('robocasa', 'robosuite'):
        progress('checkout_'+name)
        spec = importlib.util.find_spec(name)
        if spec is None or not spec.origin:
            raise ValueError('Cached simulator package missing')
        root = Path(spec.origin).resolve().parent.parent
        revision = subprocess.check_output(['git', '-C', str(root), 'rev-parse', 'HEAD'], timeout=10, text=True).strip()
        dirty = subprocess.check_output(['git', '-C', str(root), 'status', '--porcelain', '--untracked-files=no'], timeout=10)
        if revision != SOURCE_PINS[name]:
            raise ValueError('Pinned simulator checkout revision differs')
        if dirty:
            raise ValueError('Pinned simulator tracked content differs')
    progress('dependencies')
    import gymnasium, mujoco, transformers
    for module, expected in ((gymnasium, '0.29.1'), (mujoco, '3.3.1'), (transformers, '4.57.1')):
        if module.__version__ != expected:
            raise ValueError('Pinned simulator dependency differs: '+module.__name__)
    progress('task_registry')
    from robocasa.utils.dataset_registry import TASK_SET_REGISTRY
    from robocasa.utils.dataset_registry_utils import get_task_horizon
    p = plan()
    if (list(TASK_SET_REGISTRY[p['task_set']])[p['task_index']] != p['task']
        or get_task_horizon(p['task']) != p['horizon']):
        raise ValueError('Official task registry/horizon changed')
    progress('official_source')
    source_gate()
    progress('asset_inventory')
    assets = asset_gate()
    return {'status': 'passed', 'task': p['task'], 'horizon': p['horizon'],
            'render_reset_executed': False, 'assets': assets}


def simulator_gate_receipt():
    """Keep useful failure locations without copying exception text, locals or logs."""
    current = 'startup'
    def progress(check):
        nonlocal current
        current = check
    try:
        return simulator_gate(progress)
    except Exception as error:
        # Third-party messages can contain arbitrary values; only our exact guards
        # and module identifiers are public diagnostic fields.
        reason = str(error) if type(error) is ValueError and str(error) in GATE_REASONS else 'exception_text_omitted'
        frames = traceback.extract_tb(error.__traceback__)[-16:]
        trace = [{'file': Path(frame.filename).name, 'line': frame.lineno, 'function': frame.name}
                 for frame in frames]
        diagnostic = {'check': current, 'error_type': type(error).__name__, 'reason': reason, 'trace': trace}
        if isinstance(error, ImportError) and isinstance(error.name, str) and re.fullmatch(r'[A-Za-z0-9_.]{1,128}', error.name):
            diagnostic['missing_module'] = error.name
        if isinstance(error, subprocess.CalledProcessError): diagnostic['exit_code'] = error.returncode
        if isinstance(error, OSError) and type(error.errno) is int: diagnostic['errno'] = error.errno
        return {'status': 'failed', 'render_reset_executed': False, 'diagnostic': diagnostic}


def asset_gate():
    receipt = read_json(Path('/work/build-evidence/assets.json'), limit=32*1024**2)
    archives = receipt.get('archives', [])
    if receipt.get('status') != 'completed' or len(archives) != 6:
        raise ValueError('Completed six-archive cache receipt required')
    files = total = compressed = 0
    seen = set()
    for archive in archives:
        if archive.get('status') != 'completed' or archive.get('received_bytes') != archive.get('expected_bytes'):
            raise ValueError('Cached archive provenance incomplete')
        compressed += archive['received_bytes']
        for item in archive['files']:
            path = Path(item['path'])
            if (str(path) in seen or not path.resolve().is_relative_to(Path('/work/robocasa'))
                or path.is_symlink() or not path.is_file() or path.stat().st_size != item['bytes']):
                raise ValueError('Cached simulator asset path/length differs')
            seen.add(str(path)); files += 1; total += item['bytes']
    if files != 123236 or total != 24063148172 or compressed != 10986648838:
        raise ValueError('Pinned cached asset inventory differs')
    return {'files': files, 'extracted_bytes': total, 'archive_bytes': compressed,
            'verification': 'immutable image provenance plus completed CRC extraction receipt and every cached file length'}


def serve(args):
    from .image_check import require_native_interpreter, require_dependencies, pinned_dependencies
    from .smoke_runtime import verify_snapshot
    import torch, transformers
    require_native_interpreter()
    require_dependencies(pinned_dependencies(Path('/app/requirements-gpu-smoke.lock'), Path('/app/requirements-r2.txt')))
    if not torch.cuda.is_available() or torch.cuda.device_count() != 1:
        raise ValueError('Exactly one approved CUDA device required')
    p = plan()
    signal.alarm(p['limits']['server_seconds_each'])
    torch.set_grad_enabled(False)
    pins = read_json(Path('/app/containers/gpu-smoke-pins.json'))
    verify_snapshot(MODEL, pins['model'])
    if args.arm == 'one_update':
        verify_artifact(args.head, HEAD, p['checkpoint']['bytes'], p['checkpoint']['bytes'])
    began = time.monotonic()
    model, info = transformers.AutoModel.from_pretrained(MODEL, local_files_only=True,
        trust_remote_code=True, attn_implementation='flash_attention_2', dtype=torch.bfloat16,
        low_cpu_mem_usage=True, output_loading_info=True)
    if any(info.get(k) for k in ('missing_keys', 'unexpected_keys', 'mismatched_keys', 'error_msgs')):
        raise ValueError('Official model mapping incomplete')
    model = model.cuda().eval()
    if args.arm == 'one_update':
        from safetensors.torch import load_file
        load_head(model, load_file(str(args.head), device='cpu'))
    torch.cuda.synchronize()
    load_seconds = time.monotonic()-began
    # Each arm/episode gets the same initial diffusion RNG after model loading.
    torch.manual_seed(p['policy_seed']); torch.cuda.manual_seed_all(p['policy_seed'])
    official_server = load_official('server').Server
    server = official_server.__new__(official_server)
    server.host, server.port, server.model = '127.0.0.1', PORT, model
    save(args.output/'ready.json', {'arm': args.arm, 'model_load_seconds': load_seconds,
                                  'policy_seed': p['policy_seed'], 'host': '127.0.0.1'})
    # Stock local client/server transport. No listener is exposed outside the job.
    server.serve()


def observation_fingerprint(observation, entry, physics=None):
    import numpy as np
    h = hashlib.sha256()
    arrays = [('native_state', entry.observation_to_state(observation))]
    arrays += [(key, observation[key]) for key in entry.CAMERA_KEYS]
    if physics is not None:
        arrays += [('physics', physics.sim.get_state().flatten())]
        arrays += [('model/'+key, getattr(physics.sim.model, key))
                   for key in ('geom_rgba', 'cam_pos', 'cam_quat', 'light_pos', 'light_dir')]
    for key, value in arrays:
        array = np.ascontiguousarray(value)
        if key in entry.CAMERA_KEYS and (array.shape != (256, 256, 3) or array.dtype != np.uint8):
            raise ValueError('Pinned camera geometry/dtype differs')
        if not np.isfinite(array).all():
            raise ValueError('Nonfinite initial observation')
        h.update(canonical({'key': key, 'shape': list(array.shape), 'dtype': str(array.dtype)}))
        h.update(array.tobytes())
    h.update(observation['annotation.human.task_description'].encode())
    return h.hexdigest()


def rollout(args):
    import numpy as np, random
    p = plan(); entry = load_official('entry'); simulator_gate()
    import gymnasium as gym
    from robocasa.utils.dataset_registry_utils import get_task_horizon
    from robocasa.utils.env_utils import convert_action
    episode = p['episode_seeds'].index(args.seed)
    random.seed(args.seed); np.random.seed(args.seed)
    settings = entry.parse_args(['--model-path', str(MODEL), '--task-name', p['task'],
        '--seed', str(p['base_seed']), '--num-trials', str(p['schedule_trials']),
        '--split', p['split'], '--task-set', p['task_set']])
    entry.validate_args(settings)
    record = {'arm': args.arm, 'seed': args.seed, 'task': p['task'], 'status': 'started'}
    inference_seconds = 0.; queries = 0

    class TracedEnvironment:
        def __init__(self, env): self.env = env
        def reset(self, **kwargs):
            observation, info = self.env.reset(**kwargs)
            record['initial_observation_sha256'] = observation_fingerprint(observation, entry, self.env.unwrapped.env)
            return observation, info
        def step(self, action):
            value = self.env.step(action)
            record['terminated'], record['truncated'] = bool(value[2]), bool(value[3])
            return value
        def close(self): self.env.close()

    class TracedGym:
        @staticmethod
        def make(*a, **kw): return TracedEnvironment(gym.make(*a, **kw))

    client = entry.EvalClient(str(MODEL), '127.0.0.1', PORT, 'robocasa365', .95)
    native_infer = client.infer
    def infer(*a, **kw):
        nonlocal inference_seconds, queries
        began = time.monotonic(); action = native_infer(*a, **kw)
        inference_seconds += time.monotonic()-began; queries += 1
        if action.shape != (16, 12) or not np.isfinite(action).all():
            raise ValueError('Native decoded action geometry/finiteness differs')
        return action
    client.infer = infer
    began = time.monotonic()
    try:
        stats = entry.evaluate_task(p['task'], p['task_index'], settings, client, TracedGym(),
            get_task_horizon, convert_action, args.output, episode_indices=[episode],
            show_progress=False, write_task_stats=True)
        result = stats['episodes'][0]
        if result['seed'] != args.seed or stats['horizon'] != p['horizon']:
            raise ValueError('Official seed/horizon differs')
        record.update(status='completed', success=result['success'], steps=result['steps'],
            wall_seconds=time.monotonic()-began, inference_seconds=inference_seconds, model_queries=queries,
            model_load_seconds=read_json(args.output/'ready.json')['model_load_seconds'],
            recipe=p['recipe'], intervention_policy=None)
        save(args.output/'episode.json', record)
    finally:
        client.close()


def stop_owned(process):
    if process.poll() is None:
        os.killpg(process.pid, signal.SIGTERM)
        try: process.wait(timeout=5)
        except subprocess.TimeoutExpired:
            os.killpg(process.pid, signal.SIGKILL); process.wait(timeout=5)


def run(args):
    p = read_json(args.plan); approved = validate_plan(p)
    if args.approved_plan_sha256 != approved:
        raise ValueError('Exact approved paired plan required')
    args.output.mkdir(parents=True, exist_ok=False)
    with (args.output/'consumed.json').open('x') as f:
        f.write(json.dumps({'plan_sha256': approved, 'automatic_retries': 0}))
    keep = ('PATH', 'HOME', 'LD_LIBRARY_PATH', 'NVIDIA_VISIBLE_DEVICES', 'CUDA_VISIBLE_DEVICES',
            'LANG', 'LC_ALL')
    env = {key: os.environ[key] for key in keep if key in os.environ}
    env.update(HF_HUB_OFFLINE='1', TRANSFORMERS_OFFLINE='1', MUJOCO_GL='egl', PYTHONPATH='/app')
    records = []
    started = time.monotonic()
    try:
        with hard_deadline(p['limits']['gpu_seconds']):
            for episode, seed in enumerate(p['episode_seeds']):
                # Alternate ordering; each rollout has fresh model and simulator processes.
                for arm in p['arms'][::1 if episode % 2 == 0 else -1]:
                    target = args.output/f'{seed}-{arm}'; target.mkdir()
                    server = worker = None
                    common = ['--arm', arm, '--seed', str(seed), '--output', str(target)]
                    try:
                        with (target/'server.log').open('wb') as log:
                            server = subprocess.Popen([NATIVE, '-m', MODULE, '--serve', '--head', str(args.head), *common],
                                stdout=log, stderr=log, env=env, start_new_session=True)
                            deadline = time.monotonic()+p['limits']['server_startup_seconds_each']
                            while not (target/'ready.json').exists():
                                if server.poll() is not None or time.monotonic() >= deadline:
                                    raise RuntimeError('Native server startup failed or timed out')
                                time.sleep(.1)
                            with (target/'simulator.log').open('wb') as simlog:
                                worker = subprocess.Popen([SIMULATOR, '-m', MODULE, '--rollout', *common],
                                    stdout=simlog, stderr=simlog, env=env, start_new_session=True)
                                if worker.wait(timeout=p['limits']['rollout_seconds_each']) != 0:
                                    raise RuntimeError('Official rollout failed; see bounded job logs')
                    finally:
                        for process in (worker, server):
                            if process is not None: stop_owned(process)
                    row = read_json(target/'episode.json'); records.append(row)
                    save(args.output/'progress.json', {'completed_rollouts': len(records), 'records': records})
                    if len(records) % 2 == 0:
                        if records[-1]['initial_observation_sha256'] != records[-2]['initial_observation_sha256']:
                            raise ValueError('Initial paired observations differ; stop comparison')
            result = summarize(p, records)
            result.update(plan_sha256=approved, provenance=p, elapsed_seconds=time.monotonic()-started)
            if len(canonical(result)) > p['limits']['report_bytes']:
                raise ValueError('Evaluation report exceeds bound')
            save(args.output/'result.json', result)
    except BaseException as error:
        save(args.output/'failure.json', {'status': 'error', 'error_type': type(error).__name__,
             'completed_rollouts': len(records), 'elapsed_seconds': time.monotonic()-started,
             'automatic_retries': 0, 'result_comparison_valid': False})
        raise


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    mode = parser.add_mutually_exclusive_group(required=True)
    for name in ('prepare', 'simulator-gate', 'serve', 'rollout', 'execute-cuda'):
        mode.add_argument('--'+name, action='store_true')
    parser.add_argument('--plan', type=Path)
    parser.add_argument('--approved-plan-sha256')
    parser.add_argument('--head', type=Path)
    parser.add_argument('--output', type=Path)
    parser.add_argument('--arm', choices=plan()['arms'])
    parser.add_argument('--seed', type=int)
    args = parser.parse_args(argv)
    if args.prepare: print(json.dumps({'plan': plan(), 'plan_sha256': validate_plan(plan()), 'cloud_actions': False}))
    elif args.simulator_gate:
        receipt = simulator_gate_receipt()
        print(json.dumps(receipt))
        if receipt['status'] != 'passed': raise SystemExit(1)
    elif args.serve: serve(args)
    elif args.rollout: rollout(args)
    else: run(args)


if __name__ == '__main__':
    main()
