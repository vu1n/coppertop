"""Sealed offline container worker. No provider clients, network setup or training."""
import argparse
import contextlib
import importlib.metadata
import json
import os
from pathlib import Path
import re
import select
import signal
import socket
import subprocess
import sys
import time
from types import SimpleNamespace

sys.path.insert(0, str(Path(__file__).resolve().parent))
from common import (KINDS, canonical, checked_actions, deadline, durable, hash_file,
                    read_json, sha, verify_bundle)
PROTOCOL_STDOUT = sys.stdout.buffer


def emit(value):
    raw = canonical(value)
    if len(raw) > 1048576:
        raise ValueError('Result exceeds bound')
    PROTOCOL_STDOUT.write(raw); PROTOCOL_STDOUT.flush()


def device_occupancy(label):
    import torch
    free,total=torch.cuda.mem_get_info()
    return {'label':label,'sampled_at':time.time(),'device':0,'free_bytes':free,
            'total_bytes':total,'used_bytes':total-free,
            'method':'torch.cuda.mem_get_info; whole device sample, separate from allocator peaks'}


def sanitized_env(root, name, parent):
    scratch = root/'scratch'/name
    scratch.mkdir(parents=True, exist_ok=False, mode=0o700)
    keep = ('PATH', 'LD_LIBRARY_PATH', 'NVIDIA_VISIBLE_DEVICES', 'CUDA_VISIBLE_DEVICES', 'LANG', 'LC_ALL')
    env = {key: parent[key] for key in keep if key in parent}
    env.update(PYTHONNOUSERSITE='1', PYTHONDONTWRITEBYTECODE='1',
               HF_HUB_OFFLINE='1', TRANSFORMERS_OFFLINE='1',
               HF_HUB_DISABLE_TELEMETRY='1', TOKENIZERS_PARALLELISM='false',
               COPPERTOP_CLEF_ROOT='/opt/coppertop-clef')
    for key, name in {'HOME':'home', 'XDG_CONFIG_HOME':'config', 'XDG_CACHE_HOME':'cache',
                      'XDG_DATA_HOME':'data', 'HF_HOME':'hf', 'HF_HUB_CACHE':'hf/hub',
                      'TRANSFORMERS_CACHE':'hf/transformers', 'TORCH_HOME':'torch',
                      'PIP_CACHE_DIR':'pip'}.items():
        path = scratch/name; path.mkdir(parents=True, exist_ok=True, mode=0o700)
        env[key] = str(path)
    env['HF_TOKEN_PATH'] = str(scratch/'hf/token')
    return env


def dependency_pins(root, kind):
    if kind == 'clef':
        from urllib.parse import unquote, urlparse
        pins = {}
        for line in (root/'source/containers/clef-runtime.lock').read_text().splitlines():
            if not line or line.startswith('#'):
                continue
            filename = unquote(Path(urlparse(line.split()[0]).path).name)
            name, version = filename.split('-')[:2]
            if not re.search(r' --hash=sha256:[a-f0-9]{64}$', line):
                raise ValueError('Exact wheel archive lock required')
            key = re.sub(r'[-_.]+', '-', name).lower()
            if key in pins:
                raise ValueError('Duplicate wheel pin')
            pins[key] = version
        if len(pins) != 56:
            raise ValueError('Exact 56-wheel Clef closure required')
        return pins
    paths = ['requirements-simulator.lock'] if kind == 'client' else [
        'requirements-gpu-smoke.lock', 'requirements-r2.txt']
    pins = {'torch':'2.8.0+cu128', 'torchvision':'0.23.0+cu128', 'flash-attn':'2.8.3'} if kind == 'actor' else {}
    for name in paths:
        for line in (root/'source'/name).read_text().splitlines():
            if not line or line.startswith('#'):
                continue
            match = re.fullmatch(r'([\w.-]+)==([\w.+-]+)', line)
            if not match:
                raise ValueError('Exact installed distribution pin required')
            key = re.sub(r'[-_.]+', '-', match[1]).lower()
            if key in pins and pins[key] != match[2]:
                raise ValueError('Conflicting installed distribution pin')
            pins[key] = match[2]
    return pins


def check_dependencies(root, kind, *, pip_check=False):
    if (not os.path.samefile(sys.executable, KINDS[kind])
            or Path(sys.prefix).resolve() != Path(KINDS[kind]).parent.parent.resolve()
            or Path(sys.exec_prefix).resolve() != Path(sys.prefix).resolve()
            or sys.version_info[:2] != (3, 11)):
        raise ValueError('Separate native interpreter/prefix differs')
    versions = {key: importlib.metadata.version(key) for key in dependency_pins(root, kind)}
    if versions != dependency_pins(root, kind):
        raise ValueError('Installed pinned dependencies differ')
    if pip_check:
        result = subprocess.run([sys.executable, '-I', '-m', 'pip', 'check'],
                                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=8)
        if result.returncode:
            raise ValueError('Separate interpreter pip check failed')
    import numpy, PIL, torch, torchvision, transformers
    if kind == 'actor':
        import flash_attn
        if torch.version.cuda != '12.8' or not torch._C._GLIBCXX_USE_CXX11_ABI:
            raise ValueError('Pinned actor CUDA/Flash Attention ABI differs')
    if kind == 'client':
        import imageio, tqdm
        from coppertop.performance_runtime import load_official
        load_official('entry')
    if kind == 'clef':
        from coppertop.colocation_worker import load_reference_code
        load_reference_code('/opt/coppertop-clef/clef-model')
    return {'kind': kind, 'interpreter': KINDS[kind], 'python': list(sys.version_info[:3]),
            'versions': versions, 'cuda_abi': torch.version.cuda,
            'original_wheel_archive_bytes_verified': False, 'pip_check': pip_check}


def actor_inputs(root):
    import numpy as np
    case = read_json(root/'input/case.json')
    state = np.load(root/'input'/case['state'], allow_pickle=False)
    images = {key: np.load(root/'input'/name, allow_pickle=False) for key, name in case['cameras'].items()}
    canonical_images = {key: np.load(root/'input'/name, allow_pickle=False)
                        for key, name in case['canonical_cameras'].items()}
    if state.shape != (4, 14) or state.dtype != np.dtype('<f4') or not np.isfinite(state).all():
        raise ValueError('Frozen native state differs')
    for key, value in images.items():
        if value.shape != (4, 256, 256, 3) or value.dtype != np.uint8:
            raise ValueError('Frozen RGB history differs')
        if not np.array_equal(value, canonical_images[key.removeprefix('video.')]):
            raise ValueError('Canonical camera alias changed RGB bytes')
    return case, state, images, canonical_images


def clef_body(root):
    import numpy as np
    from PIL import Image
    body = read_json(root/'input/clef-record.json')
    _, _, images, _ = actor_inputs(root)
    decoded = []
    for index, name in enumerate(body['images']):
        with Image.open(root/'input'/name) as image:
            if image.mode != 'RGB' or image.size != (256, 256):
                raise ValueError('Frozen Clef PNG geometry differs')
            image.load(); rgb = image.copy()
        if not np.array_equal(np.asarray(rgb), list(images.values())[index][-1]):
            raise ValueError('Clef image does not match actor final frame')
        decoded.append(rgb)
    from coppertop.decision_gate import QUESTIONS
    if body['questions'] != QUESTIONS:
        raise ValueError('Pinned question schema differs')
    return {**body, 'model': 'Cloudflare/clef-flash', 'images': decoded}


def cpu_processor(root, kind):
    import numpy as np
    import torch
    import transformers
    check_dependencies(root, kind)
    if kind == 'client':
        from coppertop.performance_runtime import load_official
        from coppertop.gpu_smoke import messages
        entry = load_official('entry')
        processor = transformers.AutoProcessor.from_pretrained('/work/model',
            local_files_only=True, trust_remote_code=True, use_fast=False)
        case, state, images, canonical_images = actor_inputs(root)
        native = entry.EvalClient.__new__(entry.EvalClient); native.crop_ratio = .95
        padded = np.zeros((1, 4, 60), dtype=np.float32); padded[0, :, :14] = state
        kwargs = dict(tokenize=True, return_dict=True, return_tensors='pt', do_resize=False,
                      state=padded, robot_type='robocasa365')
        stock = processor.apply_chat_template(native._build_messages(images, case['instruction']), **kwargs)
        bridge = processor.apply_chat_template(messages(canonical_images, case['instruction']), **kwargs)
        if set(stock) != set(bridge):
            raise ValueError('Actual processor key sets differ')
        for key, left in stock.items():
            right = bridge[key]
            if (not isinstance(left, torch.Tensor) or not isinstance(right, torch.Tensor)
                    or left.dtype != right.dtype or left.shape != right.shape or not torch.equal(left, right)):
                raise ValueError('Actual processor tensor parity failed')
        if stock['state'].shape != (1, 4, 60) or stock['action_mask'].shape != (1, 16, 60):
            raise ValueError('Actual processor native shapes differ')
        return {'processor_parity': True, 'model_weights_loaded': False,
                'tensors': {key: {'shape': list(value.shape), 'dtype': str(value.dtype),
                                  'sha256': sha(value.contiguous().view(torch.uint8).numpy().tobytes())}
                            for key, value in stock.items()}}
    from coppertop.colocation_worker import load_reference_code
    reference = load_reference_code('/opt/coppertop-clef/clef-model')
    processor = transformers.AutoProcessor.from_pretrained('/opt/coppertop-clef/clef-model', local_files_only=True)
    encoded = reference.encode_record(processor.tokenizer, clef_body(root), processor=processor)
    batch = reference.collate_records([encoded], processor.tokenizer.pad_token_id, torch.device('cpu'))
    if not 0 < len(encoded.input_ids) <= 16384 or len(encoded.questions) != 2:
        raise ValueError('Actual joint-schema encoding invalid')
    return {'native_clef_processor_encoding': True, 'tokens': len(encoded.input_ids),
            'input_ids_sha256': sha(batch['input_ids'].numpy().tobytes()),
            'media_shapes': {key:list(value.shape) for key, value in batch['media'].items()},
            'model_weights_loaded': False}


def actor_server(root):
    check_dependencies(root, 'actor')
    import torch, transformers
    from coppertop.mechanism_policy import SeededModel
    from coppertop.performance_runtime import load_official, publish_listener_ready
    torch.set_grad_enabled(False)
    if not torch.cuda.is_available() or torch.cuda.device_count() != 1:
        raise ValueError('Exactly one native CUDA device required')
    signal.alarm(460)
    start = time.monotonic()
    before=device_occupancy('before-actor-load')
    model, info = transformers.AutoModel.from_pretrained('/work/model', local_files_only=True,
        trust_remote_code=True, attn_implementation='flash_attention_2', dtype=torch.bfloat16,
        low_cpu_mem_usage=True, output_loading_info=True)
    if any(info.get(key) for key in ('missing_keys', 'unexpected_keys', 'mismatched_keys', 'error_msgs')):
        raise ValueError('Frozen actor weight mapping incomplete')
    model = model.cuda().eval(); torch.cuda.synchronize()
    official = load_official('server')
    publish_listener_ready(official, root/'scratch/actor-ready.json', {
        'model_load_seconds': time.monotonic()-start, 'gpu_count': 1,
        'device_occupancy':[before,device_occupancy('after-actor-load')],
        'peak_allocated': torch.cuda.max_memory_allocated(),
        'peak_reserved': torch.cuda.max_memory_reserved()})
    class CountedSeededModel(SeededModel):
        calls = 0
        def __call__(self, **inputs):
            self.calls += 1
            if self.calls > 2:
                raise ValueError('Two actor forward calls maximum; no retry')
            torch.cuda.reset_peak_memory_stats()
            before=device_occupancy('before-actor-forward-'+str(self.calls))
            result = super().__call__(**inputs)
            torch.cuda.synchronize()
            durable(root/'scratch'/('actor-forward-'+str(self.calls)+'.json'), {
                'calls':self.calls,'peak_allocated':torch.cuda.max_memory_allocated(),
                'device_occupancy':[before,device_occupancy('after-actor-forward-'+str(self.calls))],
                'peak_reserved':torch.cuda.max_memory_reserved()})
            return result
    server = official.Server.__new__(official.Server)
    server.host, server.port, server.model = '127.0.0.1', 10086, CountedSeededModel(model)
    server.serve()


def actor_client(root):
    import numpy as np
    from coppertop.performance_runtime import load_official
    from coppertop.mechanism_policy import OfficialQuery
    check_dependencies(root, 'client')
    entry = load_official('entry')
    from transformers import AutoProcessor
    processor = AutoProcessor.from_pretrained('/work/model', local_files_only=True,
                                             trust_remote_code=True, use_fast=False)
    case, state, images, _ = actor_inputs(root)
    def request(route):
        if route == 'stock':
            native = entry.Client.__new__(entry.Client)
            native.processor = processor
            native.client_socket = socket.create_connection(('127.0.0.1', 10086), timeout=5)
            native.client_socket.settimeout(20)
            # Exact pinned stock client/server/decode; only the explicit seed enters transport.
            def seeded(**inputs):
                return native(**{**inputs, '_coppertop_seed': 20261006})
            client = entry.EvalClient.__new__(entry.EvalClient)
            client.client, client.processor = seeded, processor
            client.robot_type, client.crop_ratio = 'robocasa365', .95
            try:
                return client.infer(state, images, case['instruction'])
            finally:
                native.close()
        query = OfficialQuery()  # Actual migrated actor path; no scorer or simulator is invoked.
        try:
            return query((state, images, case['instruction']), 20261006)
        finally:
            query.close()
    emit({'ready':'actor-client'})
    for call, route in enumerate(('stock', 'clef-disabled'),1):
        command = json.loads(sys.stdin.buffer.readline(1025))
        if command != {'route': route}:
            raise ValueError('Two fixed routes only; no replay')
        with deadline(20), contextlib.redirect_stdout(sys.stderr):
            result = request(route)
        if (not isinstance(result, np.ndarray) or result.dtype != np.float32
                or result.shape != (16,12) or not result.flags.c_contiguous or not np.isfinite(result).all()):
            raise ValueError('Native decoded actions differ from required dtype/shape')
        np.save(root/'scratch'/('action-'+route+'.npy'), result, allow_pickle=False)
        emit({'route': route, 'action_sha256': sha(result.tobytes()), 'scorer_calls': 0,
              'actor_forward':read_json(root/'scratch'/('actor-forward-'+str(call)+'.json'))})


def clef_worker(root):
    check_dependencies(root, 'clef')
    import torch
    from coppertop.colocation_worker import load_reference_code
    from coppertop.decision_gate import validate_answers
    reference = load_reference_code('/opt/coppertop-clef/clef-model')
    torch.set_grad_enabled(False); torch.cuda.reset_peak_memory_stats()
    start = time.monotonic()
    before=device_occupancy('before-clef-load')
    model, processor = reference.load_release_model('/opt/coppertop-clef/clef-model', device='cuda',
                                                   local_files_only=True, attn_implementation='sdpa')
    torch.cuda.synchronize()
    emit({'ready':'clef', 'model_load_seconds':time.monotonic()-start,
          'device_occupancy':[before,device_occupancy('after-clef-load')],
          'peak_allocated':torch.cuda.max_memory_allocated(), 'peak_reserved':torch.cuda.max_memory_reserved()})
    if json.loads(sys.stdin.buffer.readline(1025)) != {'call':1}:
        raise ValueError('Exactly one Clef call allowed')
    with deadline(20), contextlib.redirect_stdout(sys.stderr), torch.inference_mode():
        before=device_occupancy('before-clef-forward')
        response = reference.systemone(model, processor, clef_body(root))
        validate_answers(response['answers'], probability_decimals=4)
        torch.cuda.synchronize()
    emit({'answers':response['answers'], 'usage':response['usage'],
          'device_occupancy':[before,device_occupancy('after-clef-forward')],
          'peak_allocated':torch.cuda.max_memory_allocated(), 'peak_reserved':torch.cuda.max_memory_reserved()})


class Child:
    def __init__(self, root, name, kind, mode, args):
        self.buffer = b''
        self.process = subprocess.Popen([KINDS[kind], '-I', str(root/'executor/worker.py'),
            '--root', str(root), '--plan-sha256', args.plan_sha256, '--run-id', args.run_id, '--mode', mode,
            '--kind', kind], stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
            env=sanitized_env(root, name, os.environ), start_new_session=True)
    def receive(self, seconds):
        end = time.monotonic()+seconds
        while b'\n' not in self.buffer:
            remaining = end-time.monotonic()
            if remaining <= 0 or not select.select([self.process.stdout], [], [], remaining)[0]:
                raise TimeoutError('Owned child protocol deadline expired')
            block = os.read(self.process.stdout.fileno(), 65536)
            if not block or len(self.buffer)+len(block)>1048576:
                raise ValueError('Missing or oversized owned child receipt')
            self.buffer += block
        line, self.buffer = self.buffer.split(b'\n', 1)
        result = json.loads(line)
        if not isinstance(result, dict) or result.get('error_type'):
            raise ValueError('Owned native child failed')
        return result
    def send(self, value):
        self.process.stdin.write(canonical(value)); self.process.stdin.flush()
    def stop(self):
        # Child sessions are isolated from the supervisor and observer. Kill all owned descendants.
        for sig in (signal.SIGTERM, signal.SIGKILL):
            try:
                os.killpg(self.process.pid, sig)
            except ProcessLookupError:
                pass
            try:
                self.process.wait(timeout=2)
            except subprocess.TimeoutExpired:
                continue
            if sig == signal.SIGKILL:
                break
        # Send SIGKILL even if the direct child already exited; descendants can retain its group.
        try:
            os.killpg(self.process.pid, signal.SIGKILL)
        except ProcessLookupError:
            pass
        self.process.wait(timeout=2)


def verify_restored(root):
    rows = []
    for release in read_json(root/'model-manifest.json')['releases']:
        inventory=verify_release_inventory(release['root'],release['files'],
            release.get('unverified_documentation',[]))
        seen = set()
        for entry in release['files']:
            path = (Path(release['root'])/entry['path']).resolve(strict=True)
            if path in seen:
                raise ValueError('Duplicate restored release target')
            seen.add(path)
            rows.append({'repo':release['repo'], **hash_file(release['root'], entry, internal_symlinks=True)})
    index = read_json(root/'source-index.json')
    for entry in index['files']:
        if entry['path'].startswith('official/'):
            restored = '/work/xiaomi'
            item = {key:value for key,value in entry.items() if key in ('path','bytes','sha256')}
            item['path'] = item['path'].removeprefix('official/')
            hash_file(restored, item)
    return {'files':len(rows), 'bytes':sum(row['bytes'] for row in rows), 'identities':rows,
            'unverified_documentation':inventory['unverified_documentation'],
            'fresh_process_pid':os.getpid(), 'official_sources_verified':True}


def verify_release_inventory(directory, files, documentation=()):
    directory=Path(directory).resolve(strict=True)
    expected={entry['path'] for entry in files}
    allowed_docs={entry['path'] for entry in documentation}
    if allowed_docs not in (set(),{'README.md'}):raise ValueError('Exact root README exception only')
    actual=set();visited=0
    for parent,dirs,names in os.walk(directory,followlinks=False):
        visited+=len(dirs)+len(names)
        if visited>128:raise ValueError('Restored release directory inventory exceeds bound')
        for name in dirs:
            path=Path(parent)/name
            if path.relative_to(directory).as_posix() in allowed_docs:
                raise ValueError('Optional README documentation must be a confined regular file')
            if path.is_symlink():raise ValueError('Restored release directory symlink not allowlisted')
        for name in names:
            path=Path(parent)/name
            if not path.is_file() or not path.resolve(strict=True).is_relative_to(directory):
                raise ValueError('Restored release inventory contains special file or path escape')
            actual.add(path.relative_to(directory).as_posix())
    present_docs=actual&allowed_docs
    if actual-present_docs!=expected:
        raise ValueError('Restored release inventory differs; no cache metadata exclusions assumed')
    return {'files':len(expected),'unexpected_files':0,
            'unverified_documentation':[{'path':name,'present':name in present_docs,
                'authenticated':False,'runtime_used':False} for name in sorted(allowed_docs)]}


def stage(root, args):
    # O_EXCL consumes each container stage before any model or processor call.
    (root/'scratch').mkdir(exist_ok=True, mode=0o700)
    durable(root/'scratch'/('consumed-'+args.kind+'.json'), {'plan_sha256':args.plan_sha256}, exclusive=True)
    signal.signal(signal.SIGTERM, lambda *_: (_ for _ in ()).throw(KeyboardInterrupt()))
    signal.alarm(260 if args.kind=='cpu' else 460)
    children, phases, cleanup_errors = [], [], []
    primary = None
    def phase(name, seconds, function):
        start = time.monotonic()
        with deadline(seconds):
            value = function()
        phases.append({'phase':name, 'seconds':time.monotonic()-start, 'result':value})
        durable(root/'scratch/partial.json', {'phases':phases})
        if sum(path.stat().st_size for path in (root/'scratch').rglob('*') if path.is_file()) > 268435456:
            raise ValueError('Observed owned scratch exceeds 256 MiB')
        return value
    def start(name, kind, mode):
        child = Child(root, name, kind, mode, args); children.append(child); return child
    def single(name, kind, mode, seconds):
        child = start(name, kind, mode)
        value = child.receive(seconds)
        child.process.wait(timeout=min(2, seconds))
        if child.process.returncode:
            raise ValueError('Owned child exit differs')
        return value
    try:
        phase('fresh-restored-hash', 180 if args.kind=='cpu' else 120, lambda: verify_restored(root))
        if args.kind == 'cpu':
            def deps():
                return [single('deps-'+kind,kind,'dependencies',18) for kind in KINDS]
            phase('three-interpreter-dependencies',60,deps)
            phase('actual-native-processors',60,lambda:[
                single('processor-client','client','processor',30),
                single('processor-clef','clef','processor',30)])
        else:
            holder = {}
            def actor_load():
                server = start('actor-server','actor','server')
                end = time.monotonic()+90
                while not (root/'scratch/actor-ready.json').exists():
                    if server.process.poll() is not None or time.monotonic() >= end:
                        raise TimeoutError('Owned actor readiness deadline')
                    time.sleep(.05)
                ready = read_json(root/'scratch/actor-ready.json')
                actor = start('actor-client','client','actor-client')
                if actor.receive(max(.001,end-time.monotonic())) != {'ready':'actor-client'}:
                    raise ValueError('Actor client readiness differs')
                holder['actor'] = actor
                return ready
            phase('actor-load',90,actor_load)
            actor = holder['actor']
            clef = start('clef','clef','clef')
            phase('clef-load',90,lambda:clef.receive(90))
            def call_actor(route):
                actor.send({'route':route}); return actor.receive(20)
            phase('stock-actor-call',20,lambda:call_actor('stock'))
            phase('disabled-clef-actor-call',20,lambda:call_actor('clef-disabled'))
            def call_clef():
                clef.send({'call':1}); return clef.receive(20)
            phase('official-clef-joint-head-call',20,call_clef)
            import numpy as np
            phase('exact-decoded-action-parity',20,lambda:checked_actions(
                np.load(root/'scratch/action-stock.npy',allow_pickle=False),
                np.load(root/'scratch/action-clef-disabled.npy',allow_pickle=False)))
    except BaseException as error:
        primary = type(error).__name__
    finally:
        signal.alarm(0)
        for child in reversed(children):
            try:
                child.stop()
            except BaseException as error:
                cleanup_errors.append(type(error).__name__)
    result = {'status':'passed' if primary is None and not cleanup_errors else 'failed',
              'stage':args.kind, 'run_id':args.run_id, 'plan_sha256':args.plan_sha256,
              'execution_manifest_sha256':sha((root/'execution-manifest.json').read_bytes()),
              'phases':phases, 'primary_error_type':primary, 'child_cleanup_errors':cleanup_errors,
              'owned_direct_children_reaped':all(c.process.poll() is not None for c in children),
              'actor_calls':2 if args.kind=='gpu' and primary is None else None,
              'clef_calls':1 if args.kind=='gpu' and primary is None else None,
              'training_steps':0, 'simulator_controls':0, 'provider_calls':0}
    durable(root/'scratch/result.json',result); emit(result)
    return 0 if result['status']=='passed' else 1


def main():
    p = argparse.ArgumentParser()
    p.add_argument('--root',type=Path,required=True); p.add_argument('--plan-sha256',required=True)
    p.add_argument('--run-id',required=True); p.add_argument('--mode',required=True)
    p.add_argument('--kind',required=True)
    args = p.parse_args(); root = args.root.resolve(strict=True)
    verify_bundle(root,args.plan_sha256)
    sys.path.insert(0,str(root/'source'))
    if args.mode=='stage':
        if args.kind not in ('cpu','gpu'):raise ValueError('Fixed stage required')
        return stage(root,args)
    # All children have a short alarm before third-party imports; no shell or environment dump.
    signal.alarm(460 if args.mode in ('server','actor-client','clef') else 90)
    with contextlib.redirect_stdout(sys.stderr):
        if args.mode=='dependencies':result=check_dependencies(root,args.kind,pip_check=True)
        elif args.mode=='processor':result=cpu_processor(root,args.kind)
        elif args.mode=='server':actor_server(root); return 0
        elif args.mode=='actor-client':
            # Protocol stdout is restored only by the explicitly owned emit function below.
            result=None
        elif args.mode=='clef':result=None
        else:raise ValueError('Fixed worker mode required')
    with contextlib.redirect_stdout(sys.stderr):
        if args.mode=='actor-client':actor_client(root)
        elif args.mode=='clef':clef_worker(root)
        else:emit(result)
    return 0


if __name__=='__main__':
    try:
        raise SystemExit(main())
    except Exception as error:
        emit({'error_type':type(error).__name__, 'message':'Bounded native operation failed'})
        raise SystemExit(1)
