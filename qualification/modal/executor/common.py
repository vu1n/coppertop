"""Bounded local evidence and exact-byte gates. Imports no provider or model."""
import contextlib
import hashlib
import json
import math
import os
from pathlib import Path, PurePosixPath
import re
import signal
import tempfile
import time

APP_ID = r'ap-[A-Za-z0-9]{22}'
SANDBOX_ID = r'sb-[A-Za-z0-9]{22}'  # Pinned V1 only, CPU and GPU.
RUN_ID = r'ct-qual-[a-z0-9]{12}'
IDENTITY = {'workspace': 'vu-31241', 'environment': 'main',
            'environment_id': 'en-OTUlJyQRWzxoA0Kb0nLJ6r'}
IMAGE = 'im-0ZJ0cSfNXwzFEUZjAdRiSF'
KINDS = {'actor': '/opt/conda/bin/python', 'client': '/work/sim/bin/python',
         'clef': '/opt/coppertop-clef/clef-env/bin/python'}
STAGES = {'cpu': {'cpu': (4, 4), 'memory': (32768, 32768), 'gpu': None,
                  'timeout': 286, 'idle_timeout': 20},
          'gpu': {'cpu': (4, 4), 'memory': (65536, 65536), 'gpu': 'A100-80GB',
                  'timeout': 540, 'idle_timeout': 20}}


def canonical(value):
    return (json.dumps(value, sort_keys=True, separators=(',', ':'), allow_nan=False)+'\n').encode()


def sha(raw):
    return hashlib.sha256(raw).hexdigest()


def read_json(path, limit=1048576):
    path = Path(path)
    if path.is_symlink() or not path.is_file() or path.stat().st_size > limit:
        raise ValueError('Bounded regular JSON required')
    with path.open('rb') as stream:
        raw = stream.read(limit+1)
    if len(raw) > limit:
        raise ValueError('JSON bound exceeded')
    return json.loads(raw)


def durable(path, value, *, exclusive=False):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    raw = canonical(value)
    fd, name = tempfile.mkstemp(prefix='.journal-', dir=path.parent)
    try:
        with os.fdopen(fd, 'wb') as stream:
            stream.write(raw); stream.flush(); os.fsync(stream.fileno())
        if exclusive:
            os.link(name, path)
            os.unlink(name)
        else:
            os.replace(name, path)
        directory = os.open(path.parent, os.O_RDONLY | os.O_DIRECTORY)
        try:
            os.fsync(directory)
        finally:
            os.close(directory)
    finally:
        Path(name).unlink(missing_ok=True)


def relpath(name):
    if (type(name) is not str or not name or '\\' in name
            or any(x in ('', '.', '..') for x in name.split('/'))
            or PurePosixPath(name).is_absolute()):
        raise ValueError('Exact relative file path required')
    return name


def hash_file(root, entry, *, internal_symlinks=False):
    root = Path(root).resolve(strict=True)
    candidate = root/relpath(entry['path'])
    path = candidate.resolve(strict=True)
    if (not path.is_relative_to(root) or not path.is_file()
            or (not internal_symlinks and candidate.is_symlink())
            or path.stat().st_size != entry['bytes']):
        raise ValueError('File path/length differs: '+entry['path'])
    if set(entry) - {'path', 'bytes', 'sha256', 'git_blob'}:
        raise ValueError('Digest entry fields differ')
    expected = entry.get('sha256', entry.get('git_blob'))
    if not isinstance(expected, str) or not re.fullmatch(r'[a-f0-9]{64}|[a-f0-9]{40}', expected):
        raise ValueError('Independent expected digest missing: '+entry['path'])
    digest = hashlib.sha256() if 'sha256' in entry else hashlib.sha1(
        b'blob '+str(entry['bytes']).encode()+b'\0')
    actual = 0
    with path.open('rb') as stream:
        while chunk := stream.read(1048576):
            actual += len(chunk)
            if actual > entry['bytes']:
                raise ValueError('File grew beyond pinned length')
            digest.update(chunk)
    if actual != entry['bytes'] or digest.hexdigest() != expected:
        raise ValueError('File digest differs: '+entry['path'])
    return {'path': entry['path'], 'bytes': actual, 'digest': digest.hexdigest(),
            'kind': 'sha256' if 'sha256' in entry else 'git_blob'}


def validate_models(manifest):
    if (manifest.get('schema_version') != 1 or len(manifest.get('releases', [])) != 2
            or manifest.get('missing_independent_digests') != []):
        raise ValueError('All independent pinned release digests are required before launch')
    releases = manifest['releases']
    expected = [('XiaomiRobotics/Xiaomi-Robotics-1-RoboCasa365',
                 '3a6d0293bfa90759d34a7fc48c2c62413cd7bcf4', '/work/model', 21, 10122585561),
                ('Cloudflare/clef-flash', 'fde727a287004204b7518dcc983fe64379776712',
                 '/opt/coppertop-clef/clef-model', 16, 19083367180)]
    for release, (repo, revision, root, count, size) in zip(releases, expected):
        files = release['files']
        if ((release['repo'], release['revision'], release['root']) != (repo, revision, root)
                or len(files) != count or len({f['path'] for f in files}) != count
                or sum(f['bytes'] for f in files) != size):
            raise ValueError('Pinned release inventory differs')
        docs=release.get('unverified_documentation',[])
        expected_docs=[] if repo.startswith('XiaomiRobotics/') else [{'path':'README.md',
            'upstream_inventory_bytes':10239,'authenticated':False,'required_for_runtime':False,'presence':'optional'}]
        if docs!=expected_docs:raise ValueError('Only the explicit unverified Clef README exception is admitted')
        for entry in files:
            relpath(entry['path'])
            if (type(entry['bytes']) is not int or entry['bytes'] <= 0
                    or not set(entry) <= {'path', 'bytes', 'sha256', 'git_blob'}
                    or ('sha256' in entry) == ('git_blob' in entry)
                    or not re.fullmatch(r'[a-f0-9]{64}' if 'sha256' in entry else r'[a-f0-9]{40}',
                                       entry.get('sha256', entry.get('git_blob', '')))):
                raise ValueError('Independent file identity missing or malformed')


def verify_bundle(root, plan_sha, *, require_ready=False):
    root = Path(root).resolve(strict=True)
    plan_raw = (root/'plan.json').read_bytes()
    if len(plan_raw) > 65536 or sha(plan_raw) != plan_sha:
        raise ValueError('Exact approved plan bytes required')
    plan = json.loads(plan_raw)
    execution_raw = (root/'execution-manifest.json').read_bytes()
    if len(execution_raw) > 262144 or sha(execution_raw) != plan['execution_manifest_sha256']:
        raise ValueError('Execution artifact closure differs from plan')
    execution = json.loads(execution_raw)
    entries = execution['files']
    if not entries or len(entries) > 128 or len({e['path'] for e in entries}) != len(entries):
        raise ValueError('Bounded duplicate-free execution closure required')
    for entry in entries:
        hash_file(root, entry)
    if require_ready and not plan.get('executable_ready'):
        raise ValueError('Final review and independent digest gates incomplete')
    validate_models(read_json(root/'model-manifest.json'))
    validate_prior_attempt(read_json(root/'prior-attempt-evidence.json'), plan, read_json(root/'prior-retry-evidence.json'))
    return plan, execution


def validate_prior_attempt(evidence, plan, second_evidence):
    """Admission for this exact linked retry; no resource or budget reset."""
    link = plan.get('retry_link', {})
    predecessor = 'ct-qual-7f01ab6093fa'
    old_plan = 'db2790675f403524201446fe291109c09f6cb2f39c3b7380ab17a18eb00bac66'
    old_execution = '4c712bbbbb33e5759e6ef745d1bb774dd9fe955684d6ffb5b7244c3fc78d4f3c'
    app = 'ap-G6y78K8L0Ao8hTgdgL1n3F'
    sandbox = 'sb-FwTYCFBX508FuG0PuALRlS'
    expected_link = {'predecessor_run_id': predecessor, 'predecessor_plan_sha256': old_plan,
        'predecessor_execution_manifest_sha256': old_execution, 'prior_evidence':'prior-attempt-evidence.json',
        'cpu_seconds_reserved':11,'gpu_seconds_reserved':0,'first_reserve_usd':'0.00714406',
        'aggregate_cpu_reserved_seconds':480,'aggregate_gpu_reserved_seconds':660,
        'aggregate_total_reserve_usd':'1.82899735','aggregate_total_allowance_usd':'2.00',
        'actor_calls_previously_used':0,'clef_calls_previously_used':0,'aggregate_actor_calls_max':2,
        'aggregate_clef_calls_max':1,'aggregate_egress_reserve_gib':1,'retry_index':1,
        'task_level_retry_authorized':True,'aggregate_apps_max':2,'aggregate_sandboxes_max':3}
    expected_link.update(predecessor_run_id='ct-qual-11fc55100dc7',
        predecessor_plan_sha256='3aba30d42b3c6d9272292bba1df84bb1b78ab5b868ddf6bf67ee335b0ae5c463',
        predecessor_execution_manifest_sha256='8dc020c30b68503bf835df13e82b365c2d38ad6595d381a0780ac8e03f8dbd29',
        cpu_seconds_reserved=74,first_reserve_usd='0.04806004',retry_index=2,
        aggregate_apps_max=3,aggregate_sandboxes_max=4,second_prior_evidence='prior-retry-evidence.json')
    if (link != expected_link or evidence.get('schema_version') != 1
            or evidence.get('predecessor_run_id') != predecessor
            or evidence.get('failure_phase') != 'sdk_hydration_before_upload'
            or any(evidence.get(k) != 0 for k in ('uploads','worker_execs','actor_calls','clef_calls','gpu_seconds'))):
        raise ValueError('Exact predecessor and aggregate retry accounting required')
    records = evidence['records']
    names = {'consumed-attempt.json','ownership.json','result.json','cleanup.json','terminal-readonly-audit.json'}
    if set(records) != names or any(sha(canonical(row['data'])) != row['sha256'] for row in records.values()):
        raise ValueError('Original canonical attempt receipt bytes required')
    data = {name: row['data'] for name,row in records.items()}
    result = data['result.json']; ownership = data['ownership.json']; cleanup = data['cleanup.json']
    consumed = data['consumed-attempt.json']; terminal = data['terminal-readonly-audit.json']
    for record in (result, ownership, cleanup, consumed):
        if record.get('run_id') != predecessor or record.get('plan_sha256') != old_plan:
            raise ValueError('Predecessor receipt identity differs')
    for record in (ownership, cleanup, terminal):
        if record.get('app_id') != app or record.get('workspace') != IDENTITY['workspace'] or record.get('environment') != 'main':
            raise ValueError('Predecessor exact owned App differs')
    for audit in (cleanup, terminal['audit']):
        if (not audit.get('confirmed') or not audit.get('app_stopped') or audit.get('app_id') != app
                or audit.get('active_containers') != 0 or audit.get('active_sandboxes') != 0):
            raise ValueError('Independent predecessor absence required')
    if (result.get('execution_manifest_sha256') != old_execution or consumed.get('execution_manifest_sha256') != old_execution
            or result.get('status') != 'failed' or result.get('primary_error_type') != 'AssertionError'
            or result.get('stages') != {} or ownership.get('stage') != 'cpu' or ownership.get('sandbox_id') is not None
            or terminal.get('sandboxes') != [{'id':sandbox,'app_id':app,'created_at':1791720946.210392}]):
        raise ValueError('Only the known pre-upload CPU hydration failure admits this retry')
    start = ownership['execution_deadline'] - 360
    end = cleanup['finished_at']
    if (not math.isfinite(start) or not math.isfinite(end) or not start < end
            or math.ceil(end-start) != 11 or not start <= terminal['sandboxes'][0]['created_at'] <= end):
        raise ValueError('Conservative first CPU lifetime reserve differs')
    records=second_evidence['records']
    names={'consumed-attempt.json','ownership.json','result.json','cleanup.json',
           'terminal-readonly-audit.json','task-metadata-readonly.json'}
    if (set(records)!=names or any(sha(canonical(row['data']))!=row['sha256'] for row in records.values())
            or second_evidence.get('upload_exec_start')!='unknown_without_phase_counters'
            or second_evidence.get('worker_exec_start')!='unconfirmed_without_phase_counters'
            or second_evidence.get('worker_execs') is not None
            or second_evidence.get('actor_calls')!=0 or second_evidence.get('clef_calls')!=0):
        raise ValueError('Exact second predecessor receipts and unknown exec-start status required')
    data={name:row['data'] for name,row in records.items()}
    app='ap-LVc8znAJp9ouka7bFnTJFO';sandbox='sb-Q7rG8b0n84zkJTPKiCM2QS'
    predecessor='ct-qual-11fc55100dc7';old_plan=expected_link['predecessor_plan_sha256']
    old_execution=expected_link['predecessor_execution_manifest_sha256']
    for name in ('consumed-attempt.json','ownership.json','result.json','cleanup.json'):
        if data[name].get('run_id')!=predecessor or data[name].get('plan_sha256')!=old_plan:
            raise ValueError('Second predecessor identity differs')
    result=data['result.json'];ownership=data['ownership.json'];cleanup=data['cleanup.json']
    terminal=data['terminal-readonly-audit.json'];task=data['task-metadata-readonly.json']
    for audit in (cleanup,terminal['audit']):
        if (audit.get('app_id')!=app or not audit.get('confirmed') or not audit.get('app_stopped')
                or audit.get('active_containers')!=0 or audit.get('active_sandboxes')!=0):
            raise ValueError('Second predecessor independent absence required')
    if (result.get('status')!='failed' or result.get('primary_error_type')!='TimeoutError'
            or result.get('stages')!={} or result.get('execution_manifest_sha256')!=old_execution
            or ownership.get('stage')!='cpu' or ownership.get('sandbox_id')!=sandbox
            or ownership.get('app_id')!=app or task.get('app_id')!=app or task.get('sandbox_id')!=sandbox
            or task.get('gpu_type')!='CPU' or len(terminal['finished_sandboxes'])!=1
            or terminal['finished_sandboxes'][0]['id']!=sandbox):
        raise ValueError('No GPU allocation or native GPU call in second predecessor required')
    start=ownership['execution_deadline']-349;end=cleanup['finished_at']
    if not math.isfinite(start) or not math.isfinite(end) or not start<end or math.ceil(end-start)!=63:
        raise ValueError('Conservative second CPU reserve must remain63s')


@contextlib.contextmanager
def deadline(seconds):
    if seconds <= 0:
        raise TimeoutError('Phase authority expired')
    old_handler = signal.getsignal(signal.SIGALRM)
    old_timer = signal.getitimer(signal.ITIMER_REAL)
    start = time.monotonic()
    def expired(*_):
        raise TimeoutError('Bounded phase deadline expired')
    signal.signal(signal.SIGALRM, expired)
    signal.setitimer(signal.ITIMER_REAL, min(seconds, old_timer[0]) if old_timer[0] else seconds)
    try:
        yield
    finally:
        signal.setitimer(signal.ITIMER_REAL, 0)
        signal.signal(signal.SIGALRM, old_handler)
        if old_timer[0] > time.monotonic()-start:
            signal.setitimer(signal.ITIMER_REAL, old_timer[0]-(time.monotonic()-start), old_timer[1])


def checked_actions(left, right):
    # Import only during remote native execution or an explicit offline fixture test.
    import numpy as np
    for value in (left, right):
        if (not isinstance(value, np.ndarray) or value.dtype != np.dtype('float32')
                or value.shape != (16, 12) or not value.flags.c_contiguous
                or not np.isfinite(value).all()):
            raise ValueError('Already finite C-order float32 (16,12) actions required')
    if left.dtype.str != right.dtype.str or left.tobytes() != right.tobytes():
        raise ValueError('Stock and disabled-Clef decoded action bytes differ')
    return {'shape': [16, 12], 'dtype': left.dtype.str, 'order': 'C',
            'sha256': sha(left.tobytes()), 'bytes': left.nbytes, 'finite': True}
