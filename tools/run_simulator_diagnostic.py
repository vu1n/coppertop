"""One exact-approval CPU diagnostic on the cached image; no R2 or GPU."""
import argparse
import hashlib
import json
import re
import subprocess
import sys
import time
from decimal import Decimal
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from coppertop.chords import canonical, digest
from coppertop.cloud_roundtrip import _metadata
from coppertop.gpu_launch import historical_total
from coppertop.modal_reload import _container
from coppertop.performance_cloud import simulator_preflight
from coppertop.performance_eval import CACHED_IMAGE, runtime_image, runtime_sources
from coppertop.reload_launch import (read_json, save, refresh_billing, cli_json,
                                    hard_deadline, stop_and_audit_owned_app)

CAP = Decimal('0.10')


def prepare(ledger, attempt, root):
    if not re.fullmatch(r'diag-[0-9a-f]{16}', attempt): raise ValueError('Dedicated diagnostic attempt required')
    if 'pending-'+attempt in ledger['apps']: raise ValueError('Attempt already reserved; preserve its hold')
    total = historical_total(ledger)
    if Decimal(str(ledger['cumulative_cap'])) != 48 or total+CAP > 48:
        raise ValueError('Retained holds plus full $.10 must fit $48')
    p = {'schema_version': 1, 'kind': 'cpu_only_simulator_diagnostic', 'attempt': attempt,
        'app_name': 'coppertop-simulator-'+attempt, 'environment': 'main', 'region': 'us',
        'image': CACHED_IMAGE, 'sdk': '1.6.1', 'runtime_sources_sha256': runtime_sources(),
        'launcher_sha256': hashlib.sha256((Path(root)/'tools/run_simulator_diagnostic.py').read_bytes()).hexdigest(),
        'ledger_sha256': digest(canonical(ledger)), 'prior_holds_usd': str(total),
        'additional_cap_usd': str(CAP), 'cumulative_cap_usd': '48', 'holds_released_usd': '0',
        'limits': {'cpu': [2, 2], 'memory_mib': [4096, 4096], 'gpu': None,
            'function_seconds': 900, 'startup_seconds': 300, 'subprocess_seconds': 600,
            'controller_seconds': 1500, 'audit_seconds_each': 60, 'max_containers': 1,
            'automatic_retries': 0, 'report_bytes': 65536, 'metadata_egress_bytes': 1024**2},
        'secrets': [], 'volumes': {}, 'r2_access': False, 'block_network': True,
        'image_builds': False, 'downloads': False, 'model_loading': False,
        'estimated_compute_usd': '0.0773514', 'estimated_metadata_egress_usd': '0.000039063',
        'estimated_all_in_usd': '0.077390463', 'compute_estimate_seconds': 1260,
        'estimate_region_factor': '1.75', 'paid_authorized': False}
    return {'plan': p, 'plan_sha256': digest(canonical(p))}


def define(sdk, image):
    if sdk.__version__ != '1.6.1': raise ValueError('Exact reviewed Modal SDK required')
    app = sdk.App('coppertop-simulator-diagnostic', secrets=[], volumes={}, include_source=False)
    expected = runtime_sources()
    @app.function(image=image, serialized=True, region='us', gpu=None, cpu=(2, 2), memory=(4096, 4096),
        secrets=[], volumes={}, block_network=True, timeout=900, startup_timeout=300,
        single_use_containers=True, min_containers=0, buffer_containers=0, max_containers=1,
        retries=0, scaledown_window=2)
    def diagnostic():
        if runtime_sources() != expected: raise ValueError('Diagnostic runtime source differs')
        import os
        receipt = {'container_id': _container(os.environ.get('MODAL_TASK_ID')),
                   'simulator': simulator_preflight()}
        _metadata(receipt)
        return receipt
    return app, diagnostic


def execute(prepared, ledger, root, approved, *, sdk=None, runner=subprocess.run):
    if approved != prepared['plan_sha256'] or prepare(ledger, prepared['plan']['attempt'], root) != prepared:
        raise ValueError('Exact current diagnostic approval required')
    p = prepared['plan']; root = Path(root).resolve()
    output = root/'.local/simulator-diagnostic'/p['attempt']
    if any(path.is_symlink() for path in (root/'.local', output.parent, output)):
        raise ValueError('Dedicated ignored output path required')
    output.mkdir(parents=True, exist_ok=True)
    with (output/'consumed.json').open('x') as marker:
        marker.write(json.dumps({'plan_sha256': approved, 'automatic_retries': 0}))
    app = context = None; entered = attempted = False; current = ledger
    pending = 'pending-'+p['attempt']; state = {'status': 'started', 'gpu_started': False, 'r2_access': False}
    error = None
    def bind():
        if app and app.app_id and pending in current['apps']:
            if not re.fullmatch(r'ap-[A-Za-z0-9]+', app.app_id) or app.app_id in current['apps']:
                raise ValueError('Captured App collides with historical ledger')
            current['apps'][app.app_id] = current['apps'].pop(pending)
            save(output/'ledger.json', current)
    try:
        with hard_deadline(1500):
            current = refresh_billing(current, output, additional_cap=CAP, runner=runner)
            if pending in current['apps']: raise ValueError('Attempt already reserved; preserve its hold')
            current['apps'][pending] = {'state': 'unsettled_bounded', 'known_reported_usd': '0',
                'reservation_usd': str(CAP), 'description': 'coppertop-simulator-diagnostic'}
            save(output/'ledger.json', current)
            if sdk is None: import modal as sdk
            if sdk.__version__ != '1.6.1': raise ValueError('Exact reviewed Modal SDK required')
            apps = cli_json([sys.executable, '-m', 'modal', 'app', 'list', '--env', 'main', '--json'], runner)
            if any(item.get('description') == p['app_name'] for item in apps):
                raise ValueError('Dedicated App already exists; never adopt')
            with hard_deadline(60): image = sdk.Image.from_id(CACHED_IMAGE)
            image = runtime_image(sdk, image, root)
            app, function = define(sdk, image)
            context = app.run(name=p['app_name'], environment_name='main'); attempted = True
            with hard_deadline(300): context.__enter__()
            entered = True; bind()
            save(output/'ownership.json', {'app_id': app.app_id, 'environment': 'main', 'volumes': []})
            receipt = function.spawn().get(timeout=1200); _metadata(receipt)
            _container(receipt['container_id'])
            state['receipt'] = receipt
            state['status'] = 'passed' if receipt['simulator']['status'] == 'passed' else 'failed'
            save(output/'outcome.json', state)
    except BaseException as caught:
        error = caught; state.update(status='failed', error_type=type(caught).__name__)
    finally:
        try:
            bind()
            if entered:
                try:
                    with hard_deadline(60): context.__exit__(None, None, None)
                except BaseException as caught: state['context_exit_error_type'] = type(caught).__name__
            if app and app.app_id:
                # A returned single-use Function can still be draining. Repeat
                # only the exact-App stop/audit, never the paid diagnostic.
                with hard_deadline(180):
                    for audit_attempt in range(3):
                        try:
                            state['cleanup'] = stop_and_audit_owned_app(app.app_id, runner=runner)
                            break
                        except RuntimeError:
                            if audit_attempt == 2: raise
                            time.sleep(2)
            elif attempted:
                raise RuntimeError('Partial App start lacks identity; ownership audit required')
            else:
                state['cleanup'] = {'app_created': False, 'active_containers': 0, 'active_sandboxes': 0}
            current = refresh_billing(current, output, app_id=app.app_id if app else None,
                reservation_key=app.app_id if app and app.app_id in current['apps'] else pending if pending in current['apps'] else None,
                additional_cap=CAP, runner=runner)
            state['holds_released_usd'] = '0'
        except BaseException as caught:
            state.update(status='failed', cleanup_error_type=type(caught).__name__)
            if error is None: error = caught
        save(output/'outcome.json', state)
    if error: raise error
    return state


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--ledger', type=Path, required=True); parser.add_argument('--attempt', required=True)
    parser.add_argument('--execute', action='store_true'); parser.add_argument('--approved-plan-sha256')
    args = parser.parse_args(argv); root = Path(__file__).resolve().parents[1]
    try:
        ledger = read_json(args.ledger); prepared = prepare(ledger, args.attempt, root)
        if not args.execute:
            print(json.dumps(prepared)); return 0
        outcome = execute(prepared, ledger, root, args.approved_plan_sha256)
        print(json.dumps(outcome)); return 0 if outcome['status'] == 'passed' else 1
    except Exception as error:
        print('Diagnostic blocked: '+type(error).__name__, file=sys.stderr); return 1


if __name__ == '__main__': raise SystemExit(main())
