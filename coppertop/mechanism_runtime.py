"""One-boundary feasibility on an existing cached worker. Never provisions compute."""
import argparse
import hashlib
import json
import os
from pathlib import Path
import subprocess
import time

from .chords import LocalStore, canonical, digest
from .performance_runtime import NATIVE, MODEL, SOURCE_PINS, stop_owned
from .streams import ObjectRef, CHUNK_BYTES

MODULE = 'coppertop.mechanism_runtime'


def plan():
    root = Path(__file__).resolve().parent.parent
    files = sorted(path.relative_to(root).as_posix() for path in (root/'coppertop').glob('*.py'))
    files += ['requirements-simulator.lock', 'requirements-gpu-smoke.lock',
              'requirements-r2.txt', 'containers/gpu-smoke-pins.json']
    return {'kind': 'frozen_delayed_replan_feasibility', 'image': 'im-uMab88d0cwA6MALkcTf5VW',
        'task': 'CoffeeSetupMug', 'split': 'pretrain', 'eligibility': 'DEV',
        'constructor_seed': 1000, 'episode_seed': 1150, 'policy_seed': 20261006,
        'control_frequency_hz': 20,
        'source_pins': SOURCE_PINS, 'boundary': 4, 'comparison_controls': 12,
        'prefix_parity_controls': 4, 'identical_continuation_controls': 12,
        'limits': {'queries': 5, 'query_seconds': 5, 'startup_seconds': 90,
                   'session_seconds': 240, 'cleanup_seconds': 10, 'output_bytes': 24*1024*1024,
                   'automatic_retries': 0, 'training_updates': 0},
        'runtime_sha256': {name: digest((root/name).read_bytes()) for name in files},
        'allocates_cloud_resources': False, 'live_verified': False,
        'model_path': str(MODEL)}


def input_hash(inputs):
    import numpy as np
    states, images, instruction = inputs
    h = hashlib.sha256(canonical({'instruction': instruction}))
    for key, value in [('state', states), *sorted(images.items())]:
        array = np.ascontiguousarray(value)
        h.update(canonical({'key': key, 'dtype': str(array.dtype), 'shape': array.shape}))
        h.update(array.tobytes())
    return h.hexdigest()


class Trace:
    """Own small native signals and camera copies; no model crops or padding."""
    def __init__(self, env):
        self.env = env
        self.rows, self.cameras = [], {}

    def capture(self, frame):
        import numpy as np
        self.rows.append({'step': frame.step, 'time_ns': frame.time_ns,
                          'state': self.env.entry.observation_to_state(self.env.observation).tolist()})
        for key in self.env.entry.CAMERA_KEYS:
            self.cameras.setdefault(key, []).append(np.array(self.env.observation[key], copy=True))

    def reset(self, seed):
        frame = self.env.reset(seed)
        self.capture(frame)
        return frame

    def step(self, action):
        frame = self.env.step(action)
        self.capture(frame)
        return frame

    def close(self):
        self.env.close()


def save_trace(trace, tape, target, provenance, *, start=0, stop=None, store=None, parent=None):
    import numpy as np
    import av
    import pyarrow as pa
    import pyarrow.parquet as pq
    target.mkdir()
    store = store or LocalStore(target/'store')
    stop = len(trace.rows) if stop is None else stop
    if not 0 <= start < stop <= len(trace.rows):
        raise ValueError('Nonempty trace segment required')
    tape_key = tape.publish(store, provenance)
    rows = []
    for index in range(start, stop):
        row = trace.rows[index]
        rows.append(dict(row, frame_id=tape.identity(index),
                         action=list(tape.actions[index]) if index < len(tape.actions) else None,
                         success=tape.frames[index].success,
                         terminated=tape.frames[index].terminated,
                         truncated=tape.frames[index].truncated))
    schema = pa.schema([('step', pa.int64()), ('time_ns', pa.int64()),
        ('state', pa.list_(pa.float32(), 14)), ('frame_id', pa.string()),
        # PyArrow20 cannot read a null fixed-size-list row; Tape enforces12 controls.
        ('action', pa.list_(pa.float32())), ('success', pa.bool_()),
        ('terminated', pa.bool_()), ('truncated', pa.bool_())])
    pq.write_table(pa.Table.from_pylist(rows, schema=schema), target/'signals.parquet', compression='zstd')
    for camera, images in trace.cameras.items():
        with av.open(str(target/(camera+'.mp4')), mode='w') as container:
            stream = container.add_stream('libx264', rate=round(1e9/trace.env.control_ns))
            stream.width, stream.height = 256, 256
            stream.pix_fmt = 'yuv420p'
            for image in images[start:stop]:
                if image.shape != (256, 256, 3) or image.dtype != np.uint8:
                    raise ValueError('Pinned camera geometry differs')
                for packet in stream.encode(av.VideoFrame.from_ndarray(image, format='rgb24')):
                    container.mux(packet)
            for packet in stream.encode():
                container.mux(packet)
    artifacts = {path.name: {'bytes': path.stat().st_size, 'sha256': digest(path.read_bytes())}
                 for path in target.iterdir() if path.is_file()}
    for name, ref in artifacts.items():
        with (target/name).open('rb') as source:
            store.put_stream(ObjectRef(ref['sha256'], ref['bytes']), iter(lambda: source.read(CHUNK_BYTES), b''))
    manifest = {'tape': tape_key, 'artifacts': artifacts, 'parent': parent,
        'slice': {'start': start, 'stop': stop},
        'video': {'encoding': 'h264_yuv420p', 'loss': 'lossy camera encoding; exact source observation hashes remain in tape',
                  'first_frame_time_ns': tape.frames[start].time_ns, 'control_ns': trace.env.control_ns},
        'provenance': provenance}
    key = store.put(canonical(manifest))
    (target/'manifest.json').write_bytes(canonical(dict(manifest, object_sha256=key)))
    return key


def experiment(factory, query, *, seed=1150, policy_seed=20261006, log_sink=lambda records: None):
    """Three fresh adapters, five counted calls, one bounded branch, no training."""
    from .mechanism import Tape, candidate_diagnostics, native_actions, replay
    from .mechanism_runner import delayed_rollout
    count, logs = 0, []

    def ask(inputs, chosen_seed):
        nonlocal count
        count += 1
        start = time.monotonic_ns()
        record = {'call': count, 'seed': chosen_seed, 'input_sha256': input_hash(inputs), 'submitted_ns': start}
        logs.append(record)
        log_sink(logs)
        try:
            result = native_actions(query(inputs, chosen_seed), chunk=True)
        except Exception as error:
            record.update(finished_ns=time.monotonic_ns(), error_type=type(error).__name__)
            log_sink(logs)
            raise
        record.update(finished_ns=time.monotonic_ns(), chunk_sha256=digest(canonical({'actions': result})))
        log_sink(logs)
        return result

    baseline = Trace(factory())
    try:
        frames = [baseline.reset(seed)]
        old = ask(baseline.env.inputs(), policy_seed)
        for index, action in enumerate(old):
            frames.append(baseline.step(action))
            if index == 3:
                snapshot = baseline.env.inputs()
            if frames[-1].stopped and index < len(old)-1:
                raise ValueError('Baseline ended before required 16-control tape')
        root_group = f'CoffeeSetupMug/pretrain/constructor1000/episode{seed}'
        identity = digest(canonical({'policy_seed': policy_seed, 'actions': old, 'pins': SOURCE_PINS}))
        tape = Tape(root_group+'/'+identity, seed, old, tuple(frames))
        first, repeat, alternative = (ask(snapshot, chosen) for chosen in (policy_seed+1, policy_seed+1, policy_seed+2))
        diagnostics = candidate_diagnostics(first, repeat, alternative)
        if not diagnostics['same_seed_exact'] or not diagnostics['different_seed_distinct']:
            raise ValueError('Actual decoded candidate repeat/diversity gate failed')
        coverage = baseline.env.coverage
    finally:
        baseline.close()
    parity = replay(factory, tape, prefix_steps=4, continuation_steps=12)
    branch = Trace(factory())
    try:
        branch_frames = [branch.reset(seed)]
        for action in old[:4]:
            branch_frames.append(branch.step(action))
        if tuple(branch_frames) != tape.frames[:5] or input_hash(branch.env.inputs()) != input_hash(snapshot):
            raise ValueError('Branch/query-start prefix differs')

        class LoggedQuery:
            def __call__(self, inputs, chosen_seed): return ask(inputs, chosen_seed)
            def cancel(self): query.cancel()

        delayed = delayed_rollout(branch, LoggedQuery(), snapshot, old, boundary=4,
            seed=policy_seed+2, control_ns=branch.env.control_ns, steps=12, timeout_seconds=5)
        committed = delayed['committed_steps']
        if delayed['actions'][committed:] != alternative[committed:len(delayed['actions'])]:
            raise ValueError('Async same-seed candidate differs from diagnostic')
        if tuple(delayed['frames'][:committed]) != tape.frames[5:5+committed]:
            raise ValueError('Committed physics/observation prefix differs')
        branch_identity = digest(canonical({'seed': policy_seed+2, 'actions': delayed['actions']}))
        branch_tape = Tape(tape.episode_id+'/replan/'+branch_identity, seed, old[:4]+delayed['actions'],
                           tuple(branch_frames)+delayed['frames'],
                           parent_episode_id=tape.episode_id, inherited_frames=4+committed+1)
        # Distinctness must reach the suffix actually used, not just discarded predictions.
        if delayed['actions'][committed:] == old[4+committed:4+len(delayed['actions'])]:
            raise ValueError('Executed continue/replan suffixes are identical')
        summary = {key: value for key, value in delayed.items() if key not in ('frames', 'actions')}
        summary.update(status='bounded_feasibility', parity=parity, candidates=diagnostics,
            query_calls=count, discarded_query_calls=3, query_log=logs, coverage=coverage, root_group=root_group,
            baseline_success=tape.frames[-1].success, replan_success=branch_tape.frames[-1].success,
            claim='Replay/diversity/latency feasibility only; no episode utility estimate')
        return summary, baseline, tape, branch, branch_tape
    finally:
        branch.close()


def execute(output):
    from .mechanism_robocasa import RoboCasaReplay
    from .mechanism_policy import OfficialQuery
    from .reload_launch import hard_deadline
    p = plan()
    output.mkdir(parents=True, exist_ok=False)
    server = query = None
    try:
        with hard_deadline(240), (output/'server.log').open('wb') as log:
            keep = ('PATH', 'HOME', 'LD_LIBRARY_PATH', 'NVIDIA_VISIBLE_DEVICES',
                    'CUDA_VISIBLE_DEVICES', 'LANG', 'LC_ALL')
            server_env = {key: os.environ[key] for key in keep if key in os.environ}
            server_env.update(HF_HUB_OFFLINE='1', TRANSFORMERS_OFFLINE='1',
                              MUJOCO_GL='egl', PYTHONPATH='/app')
            server = subprocess.Popen([NATIVE, '-m', MODULE, '--serve', '--output', str(output)],
                stdout=log, stderr=log, env=server_env, start_new_session=True)
            deadline = time.monotonic()+90
            while not (output/'ready.json').exists():
                if server.poll() is not None or time.monotonic() >= deadline:
                    raise TimeoutError('Owned native server startup failed')
                time.sleep(.05)
            query = OfficialQuery()
            from .reload_launch import save
            result, baseline, tape, branch, branch_tape = experiment(RoboCasaReplay, query,
                log_sink=lambda records: save(output/'queries.json', {'calls': records}))
            provenance = {'plan': p, 'plan_sha256': digest(canonical(p)), 'root_group': result['root_group']}
            store = LocalStore(output/'store')
            common = 4 + result['committed_steps']
            prefix = save_trace(baseline, tape, output/'prefix', provenance, stop=common, store=store)
            result['artifacts'] = {'prefix': prefix,
                'continue': save_trace(baseline, tape, output/'continue', provenance, start=common, store=store, parent=prefix),
                'replan': save_trace(branch, branch_tape, output/'replan', provenance, start=common, store=store, parent=prefix)}
            (output/'result.json').write_bytes(canonical(result))
            if sum(f.stat().st_size for f in output.rglob('*') if f.is_file()) > p['limits']['output_bytes']:
                raise ValueError('Bounded output size exceeded; no publication')
    except BaseException as error:
        (output/'failure.json').write_bytes(canonical({'status': 'failed', 'error_type': type(error).__name__,
            'comparison_valid': False, 'automatic_retries': 0}))
        raise
    finally:
        if query is not None: query.cancel()
        if server is not None: stop_owned(server)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    mode = parser.add_mutually_exclusive_group(required=True)
    mode.add_argument('--prepare', action='store_true')
    mode.add_argument('--serve', action='store_true')
    mode.add_argument('--execute-local', action='store_true')
    parser.add_argument('--output', type=Path)
    parser.add_argument('--plan-sha256')
    args = parser.parse_args()
    if args.prepare:
        p = plan()
        print(json.dumps({'plan': p, 'plan_sha256': digest(canonical(p))}))
    elif args.serve:
        from .mechanism_policy import serve
        serve(args.output)
    else:
        if os.environ.get('HF_HUB_OFFLINE') != '1' or args.plan_sha256 != digest(canonical(plan())):
            raise ValueError('Offline exact plan binding required on existing worker')
        execute(args.output)


if __name__ == '__main__':
    main()
