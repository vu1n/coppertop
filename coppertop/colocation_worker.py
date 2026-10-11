"""Bounded saved-input benchmark: two Python environments, one physical GPU."""
import base64
import contextlib
import hashlib
import io
import json
import os
from pathlib import Path
import select
import subprocess
import sys
import time
import traceback

ROOT = Path('/tmp/colocation')
CLEF_ROOT = Path(os.environ.get('COPPERTOP_CLEF_ROOT', '/tmp'))
CLEF_MODEL = CLEF_ROOT/'clef-model'
CLEF_PYTHON = str(CLEF_ROOT/'clef-env/bin/python')


def worker_environment(parent):
    # Modal injects helper packages through PYTHONPATH. Installing against that
    # path can hide missing venv dependencies when the worker later drops it.
    # Runtime imports execute pinned third-party code. Keep only the same native
    # interpreter/CUDA settings as the evaluator, plus the explicit Clef location;
    # arbitrary controller credentials must never enter these child processes.
    keep = ('PATH', 'HOME', 'LD_LIBRARY_PATH', 'NVIDIA_VISIBLE_DEVICES',
            'CUDA_VISIBLE_DEVICES', 'LANG', 'LC_ALL', 'COPPERTOP_CLEF_ROOT')
    env = {key: parent[key] for key in keep if key in parent}
    env.update(PYTHONPATH='/app', PYTHONNOUSERSITE='1', HF_HUB_OFFLINE='1',
               TRANSFORMERS_OFFLINE='1', MUJOCO_GL='egl')
    return env


def emit(value):
    print(json.dumps(value), flush=True)


def serve_worker(kind):
    import numpy as np
    import torch
    torch.set_grad_enabled(False)
    with contextlib.redirect_stdout(sys.stderr):
        if kind == 'clef':
            from PIL import Image
            sys.path.insert(0, str(CLEF_MODEL))
            from joint_schema_model import load_release_model, systemone
            (ROOT/'clef-startup.json').write_text(json.dumps({'torch':torch.__version__,
                'cuda':torch.version.cuda,'device':torch.cuda.get_device_name(),
                'free_total_bytes':torch.cuda.mem_get_info()}))
            model, processor = load_release_model(str(CLEF_MODEL), device='cuda',
                                                  local_files_only=True, attn_implementation='sdpa')
            bodies = json.loads((ROOT/'clef-inputs.json').read_text())
            for body in bodies:
                body['images'] = [Image.open(io.BytesIO(base64.b64decode(x['base64']))).convert('RGB')
                                  for x in body['images']]
        else:
            from .mechanism_policy import OfficialQuery
            query = OfficialQuery()
            records = json.loads((ROOT/'actor-inputs.json').read_text())
            arrays = np.load(ROOT/'actor-arrays.npz', allow_pickle=False)
    emit({'ready': kind})
    for line in sys.stdin:
        command = json.loads(line)
        if command == {'stop': True}:
            break
        index = command['index']
        if type(index) is not int or index not in range(3):
            raise ValueError('Three prepared cases only')
        if kind == 'clef':torch.cuda.synchronize()
        start = time.perf_counter_ns()
        with contextlib.redirect_stdout(sys.stderr), torch.inference_mode():
            if kind == 'clef':
                response = systemone(model, processor, bodies[index])
                from .decision_gate import validate_answers
                validate_answers(response['answers'], probability_decimals=4)
                result = {'answers': response['answers'], 'usage': response['usage']}
            else:
                row = records[index]
                chunk = np.asarray(query((arrays[row['state']],
                    {camera: arrays[key] for camera, key in row['cameras'].items()},
                    row['instruction']), 20261006))
                if chunk.shape != (16,12) or not np.isfinite(chunk).all():
                    raise ValueError('Invalid actor chunk')
                result = {'chunk_sha256': hashlib.sha256(chunk.tobytes()).hexdigest(),
                          'chunk': chunk.tolist()}
        if kind == 'clef':torch.cuda.synchronize()
        elapsed=(time.perf_counter_ns()-start)/1e9
        memory = {'cuda_peak_allocated':torch.cuda.max_memory_allocated(),
                  'cuda_peak_reserved':torch.cuda.max_memory_reserved()} if kind=='clef' else {}
        emit(dict(result, seconds=elapsed, **memory))
    if kind == 'actor':
        query.close()


def receive(process, seconds=60):
    if not select.select([process.stdout], [], [], seconds)[0]:
        raise TimeoutError('Worker response deadline')
    line = process.stdout.readline(65537)
    if not line or len(line)>65536:
        raise ValueError('Missing or oversized worker response')
    return json.loads(line)


def send(process, index):
    process.stdin.write(json.dumps({'index': index})+'\n')
    process.stdin.flush()


def benchmark():
    ROOT.mkdir(exist_ok=True)
    processes = []
    logs = []
    rows = []
    def start(command, name):
        log = (ROOT/(name+'.log')).open('w'); logs.append(log)
        process = subprocess.Popen(command, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
            stderr=log, text=True, bufsize=1, env=worker_environment(os.environ))
        processes.append(process)
        return process
    def memory():
        return subprocess.check_output(['nvidia-smi','--query-gpu=memory.used,memory.total',
            '--format=csv,noheader,nounits'], text=True, timeout=5).strip()
    def call(process, kind, phase, index):
        send(process,index)
        row=dict(receive(process),kind=kind,phase=phase,index=index)
        rows.append(row)
        (ROOT/'partial.json').write_text(json.dumps(rows))
        return row
    try:
        server = start(['/opt/conda/bin/python','-c',
            "from pathlib import Path; from coppertop.mechanism_policy import serve; serve(Path('/tmp/colocation'), lifetime_seconds=450)"], 'actor-server')
        deadline=time.monotonic()+90
        while not (ROOT/'ready.json').exists():
            if server.poll() is not None or time.monotonic()>deadline:
                raise RuntimeError('Actor server unavailable')
            time.sleep(.2)
        actor=start(['/work/sim/bin/python','-m','coppertop.colocation_worker','actor'],'actor-client')
        if receive(actor)!= {'ready':'actor'}:raise ValueError('Actor readiness differs')
        for phase in ('warmup','alone_before'):
            for index in range(3): call(actor,'actor',phase,index)
        before=memory()
        clef=start([CLEF_PYTHON,'-m','coppertop.colocation_worker','clef'],'clef')
        if receive(clef,90)!= {'ready':'clef'}:raise ValueError('Clef readiness differs')
        loaded=memory()
        for index in range(3):call(clef,'clef','warmup',index)
        for index in range(3):
            call(actor,'actor','coresident_serial',index)
            call(clef,'clef','coresident_serial',index)
        for index in range(3):
            start_ns=time.perf_counter_ns()
            send(actor,index);send(clef,index)
            for process,kind in ((actor,'actor'),(clef,'clef')):
                rows.append(dict(receive(process),kind=kind,phase='overlap',index=index))
            rows[-1]['pair_wall_seconds']=(time.perf_counter_ns()-start_ns)/1e9
            (ROOT/'partial.json').write_text(json.dumps(rows))
        clef.stdin.write('{"stop":true}\n');clef.stdin.flush();clef.wait(timeout=15)
        for index in range(3):call(actor,'actor','alone_after',index)
        result={'status':'passed','rows':rows,'memory_mib':{'actor_only':before,'both_loaded':loaded},
                'same_container':True,'gpu_count':1,'live_interventions':0,'training':False}
        (ROOT/'result.json').write_text(json.dumps(result))
        emit({'status':'passed','actor_calls':15,'clef_calls':9})
    except BaseException as error:
        failure={'status':'failed','error_type':type(error).__name__,'message':str(error)[:1000],
                 'rows':rows,'worker_errors':{p.name:json.loads(p.read_text()) for p in ROOT.glob('worker-error-*.json')}}
        (ROOT/'result.json').write_text(json.dumps(failure))
        raise
    finally:
        for process in reversed(processes):
            if process.poll() is None:process.terminate()
        for process in reversed(processes):
            try:process.wait(timeout=5)
            except subprocess.TimeoutExpired:process.kill();process.wait(timeout=5)
        for log in logs:log.close()


if __name__=='__main__':
    try:
        if sys.argv[1]=='benchmark':benchmark()
        else:serve_worker(sys.argv[1])
    except BaseException as error:
        (ROOT/('worker-error-'+sys.argv[1]+'.json')).write_text(json.dumps({
            'type':type(error).__name__,'traceback':traceback.format_exc()[-16000:]}))
        raise
