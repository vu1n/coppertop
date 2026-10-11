"""Offline validation and bounded receipts for the cached colocation benchmark."""
import base64
import io
import json
import os
from pathlib import Path
import re
import signal
import subprocess
import sys

from .colocation_worker import (CLEF_MODEL, CLEF_PYTHON, CLEF_ROOT, ROOT,
                               load_reference_code, worker_environment)


def cpu_gate():
    reference = load_reference_code(CLEF_MODEL)
    from PIL import Image
    from transformers import AutoProcessor
    import torch, torchvision, idna, httpx
    import numpy as np
    encode_record = reference.encode_record
    receipt = json.loads((CLEF_ROOT/'receipt.json').read_text())
    if receipt['status'] != 'cpu_cache_validated' or receipt['private_inputs_cached']:
        raise ValueError('Invalid cache receipt')
    with worker_environment(os.environ) as env:
        subprocess.run([sys.executable, '-m', 'pip', 'check'], check=True, timeout=30,
                       env=env)
    processor = AutoProcessor.from_pretrained(str(CLEF_MODEL), local_files_only=True)
    bodies = json.loads((ROOT/'clef-inputs.json').read_text())
    if len(bodies) != 3:
        raise ValueError('Expected exactly three Clef cases')
    for body in bodies:
        if len(body['images']) != 3:
            raise ValueError('Expected three cameras')
        body['images'] = [Image.open(io.BytesIO(base64.b64decode(x['base64'], validate=True))).convert('RGB')
                          for x in body['images']]
        encoded = encode_record(processor.tokenizer, body, processor=processor)
        if len(encoded.questions) != 2 or not 0 < len(encoded.input_ids) <= 16384:
            raise ValueError('Unexpected encoded case')
    records = json.loads((ROOT/'actor-inputs.json').read_text())
    if len(records) != 3:
        raise ValueError('Expected exactly three actor cases')
    with np.load(ROOT/'actor-arrays.npz', allow_pickle=False) as arrays:
        for record in records:
            state = arrays[record['state']]
            if state.shape != (4,14) or not np.isfinite(state).all():
                raise ValueError('Invalid actor state')
            if len(record['cameras']) != 3:
                raise ValueError('Expected three actor cameras')
            for key in record['cameras'].values():
                image = arrays[key]
                if image.shape != (4,256,256,3) or image.dtype != np.uint8:
                    raise ValueError('Invalid actor camera history')
    (ROOT/'cpu-gate.json').write_text(json.dumps({'status':'passed', 'cases':3,
        'model_revision':receipt['model_revision'], 'lock_sha256':receipt['lock_sha256']}))


def require_complete(response):
    if response.get('returncode') != 0 or response.get('timeout'):
        raise ValueError('Benchmark process failed or timed out')
    result = json.loads(response['files']['result.json'])
    expected = {(kind, phase, index)
        for kind, phases in (('actor', ('warmup','alone_before','coresident_serial','overlap','alone_after')),
                             ('clef', ('warmup','coresident_serial','overlap')))
        for phase in phases for index in range(3)}
    rows = result.get('rows', [])
    actual = {(r['kind'], r['phase'], r['index']) for r in rows}
    if result.get('status') != 'passed' or len(rows) != 24 or actual != expected:
        raise ValueError('Incomplete benchmark schedule')
    import math
    if any(not isinstance(r['seconds'], (int,float)) or not math.isfinite(r['seconds']) or r['seconds'] <= 0
           for r in rows):
        raise ValueError('Invalid benchmark timing')
    actor_hashes = {}
    for row in rows:
        if row['kind'] != 'actor':
            continue
        chunk_hash = row.get('chunk_sha256')
        if not isinstance(chunk_hash, str) or re.fullmatch(r'[0-9a-f]{64}', chunk_hash) is None:
            raise ValueError('Missing or malformed actor chunk SHA256')
        if actor_hashes.setdefault(row['index'], chunk_hash) != chunk_hash:
            raise ValueError(f"Actor chunk parity failed for case {row['index']}")
    return result


def run_stage(stage):
    """Keep process-group cleanup and receipts even when the inner deadline expires."""
    ROOT.mkdir(exist_ok=True)
    if stage == 'cpu':
        command = [CLEF_PYTHON, '-m', 'coppertop.colocation_stage', 'cpu-gate']
        deadline = 150
    elif stage == 'gpu':
        command = ['/opt/conda/bin/python', '-m', 'coppertop.colocation_worker', 'benchmark']
        deadline = 450
    else:
        raise ValueError('Unknown stage')
    response = {'stage':stage, 'timeout':False, 'files':{}, 'truncated_logs':[]}
    processes = []
    with worker_environment(os.environ, processes=processes) as env, \
         (ROOT/'stage-stdout.log').open('w') as out, (ROOT/'stage-stderr.log').open('w') as err:
        process = subprocess.Popen(command, env=env, stdout=out, stderr=err, start_new_session=True)
        processes.append(process)
        try:
            process.wait(timeout=deadline)
        except subprocess.TimeoutExpired:
            response['timeout'] = True
        finally:
            # The actor and scorer inherit this process group; kill orphan children too.
            try:
                os.killpg(process.pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
            process.wait(timeout=10)
    response['returncode'] = process.returncode
    names = ('cpu-gate.json','result.json','partial.json','clef-startup.json',
             'worker-error-clef.json','worker-error-actor.json','worker-error-benchmark.json',
             'stage-stdout.log','stage-stderr.log','actor-server.log','actor-client.log','clef.log')
    for name in names:
        path = ROOT/name
        if not path.exists():
            continue
        limit = 512*1024
        if path.stat().st_size > limit:
            if name.endswith('.json'):
                raise ValueError('Oversized JSON receipt: '+name)
            response['truncated_logs'].append(name)
        with path.open('rb') as stream:
            stream.seek(max(0, path.stat().st_size-limit))
            response['files'][name] = stream.read(limit).decode('utf-8', errors='replace')
    return response


if __name__ == '__main__':
    if sys.argv[1] == 'cpu-gate':
        cpu_gate()
    else:
        print(json.dumps(run_stage(sys.argv[1])))
