"""Prepare offline; execution needs exact approval for one CPU build and gate."""
import argparse
import base64
import hashlib
import importlib.metadata
import json
import os
import re
import subprocess
import sys
import tempfile
import time
from decimal import Decimal
from pathlib import Path

NATIVE = '/opt/conda/bin/python'
SIMULATOR = '/work/sim/bin/python'
# Proposed budget only: execution must bind a separately approved plan hash.
CAP = Decimal('0.25')
CEILING = Decimal('48.82')
PROTECTED = {'torch', 'torchvision', 'gym', 'evdev'}
# Exact missing inventory observed in the approved CPU retest; no blanket repair.
OBSERVED_MISSING = {'cbor2', 'aiohttp', 'h2', 'frozenlist', 'hyperframe', 'propcache', 'attrs', 'yarl', 'idna', 'aiohappyeyeballs', 'grpclib', 'aiosignal', 'hpack', 'multidict', 'certifi'}
LIMITS = {'cpu': [2, 2], 'memory_mib': [4096, 4096], 'gpu': None,
          'build_seconds': 600, 'build_startup_seconds': 600,
          'gate_seconds': 900, 'gate_startup_seconds': 300,
          'drain_seconds_each': 60, 'controller_seconds': 3000,
          'max_containers': 1, 'automatic_retries': 0,
          'network_egress_allowance_bytes': 1024**3, 'report_bytes': 65536,
          'builder_tx_threshold_bytes': 768*1024**2,
          'new_image_layer_egress_allowance_bytes': 128*1024**2,
          'metadata_response_bytes': 2*1024**2,
          'maximum_wheel_download_bytes': 32*1024**2,
          'new_uncompressed_wheel_bytes': 64*1024**2}


def encoded(value):
    return json.dumps(value, sort_keys=True, separators=(',', ':')).encode()


def sha(value):
    return hashlib.sha256(encoded(value)).hexdigest()


def bounded_json(path, limit):
    with Path(path).open('rb') as source:
        raw = source.read(limit+1)
    if len(raw) > limit:
        raise ValueError('JSON report exceeds bound')
    return json.loads(raw)


def offline_environment():
    return {'PATH': '/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin',
            'HOME': '/root', 'PYTHONPATH': '/app', 'HF_HUB_OFFLINE': '1',
            'TRANSFORMERS_OFFLINE': '1', 'HF_HUB_DISABLE_TELEMETRY': '1',
            'PYTHONDONTWRITEBYTECODE': '1', 'TOKENIZERS_PARALLELISM': 'false',
            'AWS_EC2_METADATA_DISABLED': 'true', 'PIP_CONFIG_FILE': '/dev/null'}


def prepare(ledger, attempt, root):
    sys.path.insert(0, str(root))
    from coppertop.gpu_launch import historical_total
    from coppertop.image_check import pinned_dependencies
    from coppertop.performance_eval import CACHED_IMAGE, RUNTIME_FILES, SOURCE_PINS
    if (not re.fullmatch(r'repair-[0-9a-f]{16}', attempt)
        or 'pending-'+attempt in ledger['apps']
        or Decimal(str(ledger['cumulative_cap'])) != Decimal('48.57')):
        raise ValueError('Dedicated repair identity and retained prior ceiling required')
    total = historical_total(ledger)
    if total+CAP > CEILING:
        raise ValueError('All previous holds plus full repair reservation must fit')
    audit_bytes = (root/'containers/simulator-dependency-audit.json').read_bytes()
    audit = json.loads(audit_bytes)
    pins = pinned_dependencies(root/'requirements-simulator.lock')
    for source in audit['editable_sources']:
        if source['revision'] != SOURCE_PINS[source['name']]:
            raise ValueError('Official source dependency revision differs')
    wheels = {}
    if set(audit['packages']) != set(pins):
        raise ValueError('Complete audited simulator baseline required')
    for name, record in audit['packages'].items():
        if record['version'] != pins[name]:
            raise ValueError('Audit and lock differ')
        wheel = record.get('wheel')
        if name in PROTECTED or name not in OBSERVED_MISSING:
            continue
        if (not isinstance(wheel, dict) or type(wheel['bytes']) is not int or wheel['bytes'] <= 0
            or not re.fullmatch(r'[0-9a-f]{64}', wheel['sha256'])
            or not re.fullmatch(r'[A-Za-z0-9_.+-]+\.whl', wheel['filename'])
            or record['metadata_url'] != 'https://pypi.org/pypi/'+name+'/'+pins[name]+'/json'):
            raise ValueError('Exact bounded official wheel required')
        wheels[name] = {**wheel, 'metadata_url': record['metadata_url']}
    payload = {}
    for name in RUNTIME_FILES:
        path = root/name
        if path.is_symlink() or not path.is_file() or path.stat().st_size > 1024**2:
            raise ValueError('Regular reviewed source required')
        raw = path.read_bytes()
        payload[name] = {'sha256': hashlib.sha256(raw).hexdigest(),
                         'base64': base64.b64encode(raw).decode()}
    native = pinned_dependencies(root/'requirements-gpu-smoke.lock', root/'requirements-r2.txt')
    launcher = root/'tools/run_cpu_image_repair.py'
    p = {'kind': 'cpu_image_repair_and_validation', 'attempt': attempt,
         'app_name': 'coppertop-'+attempt, 'seed_image': CACHED_IMAGE,
         'sdk': '1.6.1', 'region': 'us', 'environment': 'main',
         'launcher_sha256': hashlib.sha256(launcher.read_bytes()).hexdigest(),
         'ledger_sha256': sha(ledger), 'prior_holds_usd': str(total),
         'holds_released_usd': '0', 'additional_cap_usd': str(CAP),
         'cumulative_cap_usd': str(CEILING), 'limits': dict(LIMITS),
         'native_pins': native, 'simulator_pins': pins, 'wheel_selection': wheels,
         'source_pins': dict(SOURCE_PINS),
         'repair_allowlist': sorted(OBSERVED_MISSING),
         'static_audit_is_runtime_proof': False,
         'simulator_success': {'status': 'passed', 'task': 'CoffeeSetupMug', 'horizon': 600,
            'render_reset_executed': False, 'assets': {'files': 123236,
            'extracted_bytes': 24063148172, 'archive_bytes': 10986648838,
            'verification': 'immutable image provenance plus completed CRC extraction receipt and every cached file length'}},
         'maximum_wheel_download_bytes': min(sum(w['bytes'] for w in wheels.values()),
                                             LIMITS['maximum_wheel_download_bytes']),
         'audit_sha256': hashlib.sha256(audit_bytes).hexdigest(), 'source_payload': payload,
         'source_delivery': 'SDK entrypoint mount plus serialized reviewed bytes; no COPY layer',
         'repair_policy': 'Native inventory first; reject differing simulator versions; only the 15 observed missing historical pins plus idna',
         'retained': sorted(PROTECTED), 'secrets': [], 'volumes': {}, 'r2_access': False,
         'gpu_or_model_loading': False, 'model_or_asset_downloads': False,
         'network': 'Only bounded official PyPI metadata resolution and selected wheel bytes during build; gate blocked',
         'compute_envelope_seconds': 3300, 'region_factor': '1.75',
         'compute_envelope_basis': '3000 controller + 60 context exit + 180 audit + 60 builder drain overlap',
         'estimated_compute_bound_usd': '0.202587',
         'estimated_network_egress_allowance_usd': '0.04',
         'estimated_all_in_bound_usd': '0.242587',
         'rollback': 'Original image untouched; record new image as unusable unless gate passes; never update GPU selector',
         'paid_authorized': False}
    if len(encoded(p)) > 512*1024:
        raise ValueError('Serialized repair plan exceeds bound')
    return {'plan': p, 'plan_sha256': sha(p)}


def native_gate(p):
    """Executed by the native interpreter before installing or changing files."""
    if (not os.path.samefile(sys.executable, NATIVE) or sys.prefix != '/opt/conda'
        or sys.version_info[:2] != (3, 11)):
        raise ValueError('Native interpreter differs')
    from packaging.requirements import Requirement
    from packaging.utils import canonicalize_name
    observed = {}
    pending = list(p['native_pins'])+['torch', 'torchvision', 'flash-attn']
    while pending:
        name = canonicalize_name(pending.pop())
        if name in observed:
            continue
        if len(observed) >= 256:
            raise ValueError('Native closure exceeds inventory bound')
        version = importlib.metadata.version(name)
        if not re.fullmatch(r'[0-9][A-Za-z0-9.+-]{0,127}', version):
            raise ValueError('Native version identifier differs')
        observed[name] = version
        for line in importlib.metadata.requires(name) or []:
            requirement = Requirement(line)
            if requirement.marker is not None and not requirement.marker.evaluate({'extra': ''}):
                continue
            actual = importlib.metadata.version(requirement.name)
            if actual not in requirement.specifier:
                raise ValueError('Native active dependency conflict')
            pending.append(requirement.name)
    if any(observed[name] != version for name, version in p['native_pins'].items()):
        raise ValueError('Native overlay changed; repair rejected')
    for name, version in (('torch', '2.8.0'), ('torchvision', '0.23.0'), ('flash-attn', '2.8.3')):
        if observed[name].split('+')[0] != version:
            raise ValueError('Cached native stack changed')
    subprocess.run([sys.executable, '-m', 'pip', '--isolated', 'check'], check=True,
                   timeout=60, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    import torch, torchvision, flash_attn, transformers, av, pyarrow, numpy, boto3
    if (torch.version.cuda != '12.8' or not torch._C._GLIBCXX_USE_CXX11_ABI
        or torch.__version__.split('+')[0] != '2.8.0'
        or torchvision.__version__.split('+')[0] != '0.23.0'
        or flash_attn.__version__ != '2.8.3'):
        raise ValueError('Cached CUDA ABI changed')
    return {'status': 'passed', 'versions': dict(sorted(observed.items())),
            'active_closure_checked': True, 'pip_check': True,
            'imports_verified': True, 'cuda_abi_verified': True,
            'full_model_instantiated': False, 'cuda_executed': False}


def validate_result(result, p):
    if (len(encoded(result)) > LIMITS['report_bytes'] or not isinstance(result, dict)
        or set(result) != {'status', 'native', 'simulator', 'plan_sha256', 'gpu_started'}
        or result['status'] != 'passed' or result['plan_sha256'] != sha(p)
        or result['gpu_started'] is not False or result['simulator'] != p['simulator_success']):
        raise ValueError('Complete CPU validation receipt differs')
    native = result['native']
    if (not isinstance(native, dict) or set(native) != {'status', 'versions', 'active_closure_checked',
        'pip_check', 'imports_verified', 'cuda_abi_verified', 'full_model_instantiated', 'cuda_executed'}
        or native['status'] != 'passed' or native['full_model_instantiated'] is not False
        or native['cuda_executed'] is not False
        or any(native[key] is not True for key in ('active_closure_checked', 'pip_check', 'imports_verified', 'cuda_abi_verified'))):
        raise ValueError('Native inventory receipt differs')
    versions = native['versions']
    if (not isinstance(versions, dict) or not 1 <= len(versions) <= 256
        or any(not isinstance(name, str) or not re.fullmatch(r'[a-z0-9][a-z0-9-]{0,127}', name)
               or not isinstance(version, str) or not re.fullmatch(r'[0-9][A-Za-z0-9.+-]{0,127}', version)
               for name, version in versions.items())
        or any(versions.get(name) != version for name, version in p['native_pins'].items())
        or any(versions.get(name, '').split('+')[0] != version for name, version in
               (('torch', '2.8.0'), ('torchvision', '0.23.0'), ('flash-attn', '2.8.3')))):
        raise ValueError('Complete native versions differ')
    return result


def child(python, flags, directory, timeout):
    output = directory/'child.json'
    output.unlink(missing_ok=True)
    result = subprocess.run([python, str(Path(__file__).resolve()), *flags,
                    '--plan', str(directory/'plan.json'), '--output', str(output)],
                   timeout=timeout, env=offline_environment(),
                   stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    report = bounded_json(output, LIMITS['report_bytes'])
    if result.returncode:
        if (isinstance(report, dict) and report.get('status') == 'failed'
            and report.get('check') in ('native_inventory', 'simulator_inventory')):
            public = {'status': 'failed', 'check': report['check']}
            for key in ('error_type', 'missing_module'):
                if isinstance(report.get(key), str) and re.fullmatch(r'[A-Za-z0-9_.]{1,128}', report[key]):
                    public[key] = report[key]
            print(encoded(public).decode(), flush=True)
        raise RuntimeError('CPU prerequisite gate failed')
    return report


def select_repair(p, versions):
    if set(versions) != set(p['simulator_pins']):
        raise ValueError('Complete simulator inventory required')
    missing = [name for name, actual in versions.items() if actual is None]
    if any(actual is not None and actual != p['simulator_pins'][name]
           for name, actual in versions.items()):
        raise ValueError('Simulator version difference requires review')
    if PROTECTED.intersection(missing):
        raise ValueError('Inherited CPU/source-only package absent')
    if set(missing)-set(p['repair_allowlist']):
        raise ValueError('Unrelated historical package missing; no installation allowed')
    selected = sorted(set(missing)|{'idna'})
    if sum(p['wheel_selection'][name]['bytes'] for name in selected) > p['maximum_wheel_download_bytes']:
        raise ValueError('Missing closure exceeds reviewed small repair footprint')
    return selected


def network_tx():
    # Modal containers may omit sysfs; procfs exposes the same byte counters.
    sysfs = Path('/sys/class/net')
    if not sysfs.exists():
        with Path('/proc/net/dev').open() as source:
            raw = source.read(65537)
        if len(raw) > 65536:
            raise ValueError('Network inventory exceeds bound')
        lines = raw.splitlines()
        if (len(lines) < 3 or 'Receive' not in lines[0] or 'Transmit' not in lines[0]
            or not 1 <= len(lines[2:]) <= 64):
            raise ValueError('Bounded Linux network counters required')
        total = 0
        for line in lines[2:]:
            interface, separator, counters = line.partition(':')
            fields = counters.split()
            if (separator != ':' or not re.fullmatch(r'[A-Za-z0-9_.-]{1,64}', interface.strip())
                or len(fields) != 16 or any(not re.fullmatch(r'[0-9]{1,24}', value) for value in fields)):
                raise ValueError('Network counter differs')
            if interface.strip() != 'lo':
                total += int(fields[8])
        return total
    interfaces = list(sysfs.iterdir())
    if not 1 <= len(interfaces) <= 64:
        raise ValueError('Bounded Linux network counters required')
    total = 0
    for interface in interfaces:
        if interface.name == 'lo':
            continue
        with (interface/'statistics/tx_bytes').open() as source:
            value = source.read(32)
        if not re.fullmatch(r'[0-9]{1,24}\n?', value):
            raise ValueError('Network counter differs')
        total += int(value)
    return total


def simulator_inventory(p):
    import importlib.util
    if not os.path.samefile(sys.executable, SIMULATOR):
        raise ValueError('Simulator interpreter differs')
    for name in ('robocasa', 'robosuite'):
        spec = importlib.util.find_spec(name)
        if spec is None or not spec.origin:
            raise ValueError('Inherited editable source absent')
        checkout = Path(spec.origin).resolve().parent.parent
        revision = subprocess.check_output(['git', '-C', str(checkout), 'rev-parse', 'HEAD'],
                                           timeout=60, text=True).strip()
        dirty = subprocess.check_output(['git', '-C', str(checkout), 'status', '--porcelain',
                                          '--untracked-files=no'], timeout=60)
        if revision != p['source_pins'][name] or dirty:
            raise ValueError('Inherited editable source differs')
    result = {}
    for name in p['simulator_pins']:
        try: result[name] = importlib.metadata.version(name)
        except importlib.metadata.PackageNotFoundError: result[name] = None
        if result[name] is not None and not re.fullmatch(r'[0-9][A-Za-z0-9.+-]{0,127}', result[name]):
            raise ValueError('Simulator version identifier differs')
    return result


def repair_image_worker(p):
    """Only one bounded Image.run_function step; no implicit COPY builds."""
    import urllib.request
    import urllib.parse
    import zipfile
    deadline = time.monotonic()+570
    initial_tx = network_tx()
    def remaining(maximum):
        left = deadline-time.monotonic()
        if left <= 0:
            raise TimeoutError('Repair deadline reached')
        transmitted = network_tx()-initial_tx
        if transmitted < 0 or transmitted > LIMITS['builder_tx_threshold_bytes']:
            raise ValueError('Builder network envelope exceeded')
        return min(maximum, left)
    class NoRedirect(urllib.request.HTTPRedirectHandler):
        def redirect_request(self, *args, **kwargs):
            raise ValueError('Wheel download redirect rejected')
    opener = urllib.request.build_opener(NoRedirect, urllib.request.ProxyHandler({}))
    with tempfile.TemporaryDirectory(prefix='coppertop-repair-') as scratch:
        directory = Path(scratch)
        (directory/'plan.json').write_bytes(encoded(p))
        native = child(NATIVE, ['--native-gate'], directory, remaining(180))
        versions = child(SIMULATOR, ['--simulator-inventory'], directory, remaining(270))
        print(encoded({'check': 'simulator_inventory', 'versions': versions}).decode(), flush=True)
        selected = select_repair(p, versions)
        total = unpacked = 0
        requirements = []
        for name in selected:
            wheel = p['wheel_selection'][name]
            with opener.open(wheel['metadata_url'], timeout=remaining(15)) as response:
                raw = response.read(LIMITS['metadata_response_bytes']+1)
            if len(raw) > LIMITS['metadata_response_bytes']:
                raise ValueError('Release metadata exceeds bound')
            candidates = [entry for entry in json.loads(raw)['urls']
                          if entry['filename'] == wheel['filename']
                          and entry['digests']['sha256'] == wheel['sha256']
                          and entry['size'] == wheel['bytes']]
            if len(candidates) != 1:
                raise ValueError('Bound selected wheel not found')
            url = urllib.parse.urlsplit(candidates[0]['url'])
            if (len(candidates[0]['url']) > 2048 or url.scheme != 'https' or url.netloc != 'files.pythonhosted.org'
                or not url.path.startswith('/packages/') or url.query or url.fragment):
                raise ValueError('Official wheel destination required')
            path = directory/wheel['filename']
            digest = hashlib.sha256()
            count = 0
            with opener.open(candidates[0]['url'], timeout=remaining(15)) as response, path.open('xb') as target:
                while True:
                    remaining(15)
                    block = response.read(min(1024**2, wheel['bytes']-count+1))
                    if not block:
                        break
                    count += len(block); total += len(block)
                    if count > wheel['bytes'] or total > p['maximum_wheel_download_bytes']:
                        raise ValueError('Wheel bytes exceed bound')
                    digest.update(block); target.write(block)
            if count != wheel['bytes'] or digest.hexdigest() != wheel['sha256']:
                raise ValueError('Selected wheel checksum differs')
            with zipfile.ZipFile(path) as archive:
                unpacked += sum(item.file_size for item in archive.infolist())
            if unpacked > LIMITS['new_uncompressed_wheel_bytes']:
                raise ValueError('Wheel extraction footprint exceeds bound')
            requirements.append(str(path)+' --hash=sha256:'+wheel['sha256'])
        lock = directory/'selected.lock'
        lock.write_text('\n'.join(requirements)+'\n')
        env = offline_environment()
        env.pop('PYTHONPATH')
        subprocess.run([SIMULATOR, '-m', 'pip', '--isolated', 'install', '--no-input',
            '--disable-pip-version-check', '--no-index', '--no-deps', '--no-cache-dir',
            '--only-binary=:all:', '--require-hashes', '--force-reinstall', '--no-compile', '-r', str(lock)],
            check=True, timeout=remaining(180), env=env,
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        for name, entry in p['source_payload'].items():
            raw = base64.b64decode(entry['base64'], validate=True)
            if hashlib.sha256(raw).hexdigest() != entry['sha256']:
                raise ValueError('Reviewed source checksum differs')
            path = Path('/app')/name
            if (path.is_symlink() or not path.parent.is_dir()
                or Path('/app').is_symlink() or Path('/app/coppertop').is_symlink()):
                raise ValueError('Owned source path differs')
            path.write_bytes(raw)
        receipt = {'status': 'repaired_pending_validation', 'native': native,
                   'reinstalled': selected, 'downloaded_bytes': total,
                   'uncompressed_wheel_bytes': unpacked, 'plan_sha256': sha(p)}
        target = Path('/work/build-evidence/coppertop-cpu-repair.json')
        target.write_bytes(encoded(receipt))
        remaining(15)


def validation_worker(p):
    with tempfile.TemporaryDirectory(prefix='coppertop-validation-') as scratch:
        directory = Path(scratch)
        (directory/'plan.json').write_bytes(encoded(p))
        native = child(NATIVE, ['--native-gate'], directory, 180)
        # The project's complete pin, pip/import, source and assets gate is reused.
        sys.path.insert(0, '/app')
        from coppertop.performance_cloud import simulator_preflight
        simulation = simulator_preflight()
        if simulation.get('status') != 'passed':
            raise ValueError('Complete simulator validation failed')
        return validate_result({'status': 'passed', 'native': native, 'simulator': simulation,
                                'plan_sha256': sha(p), 'gpu_started': False}, p)


def define(sdk, p):
    if sdk.__version__ != p['sdk']:
        raise ValueError('Pinned SDK required')
    image = sdk.Image.from_id(p['seed_image'])
    image = image.run_function(repair_image_worker, kwargs={'p': p},
        cpu=(2, 2), memory=(4096, 4096), timeout=600, gpu=None,
        secrets=[], volumes={}, region='us', include_source=True)
    app = sdk.App(p['app_name'], secrets=[], volumes={}, include_source=False)
    gate = app.function(name='cpu-repair-validation', image=image,
        cpu=(2, 2), memory=(4096, 4096), timeout=900, startup_timeout=300,
        gpu=None, secrets=[], volumes={}, region='us', block_network=True,
        single_use_containers=True, min_containers=0, buffer_containers=0,
        max_containers=1, retries=0, scaledown_window=2,
        include_source=True)(validation_worker)
    return app, gate, image


def execute(prepared, ledger, root, approved, *, sdk=None):
    sys.path.insert(0, str(root))
    from coppertop.reload_launch import save, refresh_billing, hard_deadline, stop_and_audit_owned_app
    p = prepared['plan']
    if approved != prepared['plan_sha256'] or prepare(ledger, p['attempt'], root) != prepared:
        raise ValueError('Exact current repair approval required')
    output = root/'.local/cpu-image-repair'/p['attempt']; output.mkdir(parents=True, exist_ok=True)
    with (output/'consumed.json').open('x') as target:
        json.dump({'plan_sha256': prepared['plan_sha256']}, target)
    current = json.loads(json.dumps(ledger)); current['cumulative_cap'] = str(CEILING)
    current = refresh_billing(current, output, additional_cap=CAP)
    pending = 'pending-'+p['attempt']
    current['apps'][pending] = {'state': 'unsettled_bounded', 'known_reported_usd': '0',
        'reservation_usd': str(CAP), 'description': p['app_name']}
    save(output/'ledger.json', current)
    app = context = None; entered = False
    state = {'status': 'failed', 'plan_sha256': prepared['plan_sha256'], 'holds_released_usd': '0'}
    def bind():
        if app and app.app_id and pending in current['apps']:
            current['apps'][app.app_id] = current['apps'].pop(pending)
            save(output/'ownership.json', {'app_id': app.app_id, 'volumes': []})
            save(output/'ledger.json', current)
    try:
        with hard_deadline(3000):
            if sdk is None:
                import modal as sdk
            app, gate, image = define(sdk, p)
            context = app.run(name=p['app_name'], environment_name='main')
            with hard_deadline(1500): context.__enter__()
            entered = True
            bind()
            result = gate.spawn(p).get(timeout=1200)
            validate_result(result, p)
            if not re.fullmatch(r'im-[A-Za-z0-9]+', image.object_id) or image.object_id == p['seed_image']:
                raise ValueError('Bound repaired image gate differs')
            state.update(status='passed', image_id=image.object_id, receipt=result)
    except BaseException as error:
        state['error_type'] = type(error).__name__
    finally:
        try:
            bind()
            if entered:
                try:
                    with hard_deadline(60): context.__exit__(None, None, None)
                except BaseException as error:
                    state['context_exit_error_type'] = type(error).__name__
            if app and app.app_id:
                with hard_deadline(180):
                    for attempt in range(3):
                        try:
                            state['cleanup'] = stop_and_audit_owned_app(app.app_id)
                            break
                        except RuntimeError:
                            if attempt == 2: raise
                            time.sleep(2)
            elif context is not None:
                raise RuntimeError('Partial App start lacks ownership identity')
            key = app.app_id if app and app.app_id else pending
            current = refresh_billing(current, output, app_id=app.app_id if app else None,
                                      reservation_key=key, additional_cap=CAP)
        except BaseException as error:
            state.update(status='failed', cleanup_error_type=type(error).__name__)
        save(output/'outcome.json', state)
    return state


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--ledger', type=Path); parser.add_argument('--attempt')
    parser.add_argument('--execute', action='store_true'); parser.add_argument('--approved-plan-sha256')
    parser.add_argument('--native-gate', action='store_true')
    parser.add_argument('--simulator-inventory', action='store_true')
    parser.add_argument('--plan', type=Path); parser.add_argument('--output', type=Path)
    args = parser.parse_args(argv)
    try:
        if args.native_gate or args.simulator_inventory:
            p = bounded_json(args.plan, 512*1024)
            if args.native_gate:
                result = native_gate(p)
            else:
                result = simulator_inventory(p)
            args.output.write_bytes(encoded(result)); return 0
        root = Path(__file__).resolve().parents[1]
        ledger = bounded_json(args.ledger, 262144)
        prepared = prepare(ledger, args.attempt, root)
        result = execute(prepared, ledger, root, args.approved_plan_sha256) if args.execute else prepared
        print(json.dumps(result)); return 0 if not args.execute or result['status'] == 'passed' else 1
    except Exception as error:
        if (args.native_gate or args.simulator_inventory) and args.output:
            report = {'status': 'failed', 'check': 'native_inventory' if args.native_gate else 'simulator_inventory',
                      'error_type': type(error).__name__, 'reason': 'exception_text_omitted'}
            if isinstance(error, ImportError) and isinstance(error.name, str) and re.fullmatch(r'[A-Za-z0-9_.]{1,128}', error.name):
                report['missing_module'] = error.name
            args.output.write_bytes(encoded(report))
        print('CPU repair blocked: '+type(error).__name__, file=sys.stderr); return 1


if __name__ == '__main__': raise SystemExit(main())
