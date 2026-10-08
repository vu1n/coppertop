"""Exact-approval single-use Modal binding; bulk output stays Volume→CPU→R2."""
import argparse
from datetime import datetime, timezone
from decimal import Decimal
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import sys
import time
from types import SimpleNamespace

from .chords import canonical, digest, object_path, LocalStore
from .cloud_roundtrip import _metadata
from .gpu_launch import historical_total
from .mechanism_runtime import plan as worker_plan
from .modal_reload import _container
from .modal_staging import stop_verifier
from .performance_cloud import simulator_preflight, loopback_gate
from .performance_runtime import SIMULATOR, source_gate, MODEL
from .image_check import require_native_interpreter, require_dependencies, pinned_dependencies
from .r2_session import SessionScope, R2StoreFactory, mint
from .reload_launch import (read_json, save, cli_json, hard_deadline, refresh_billing,
    selected_env, parent_credentials, delete_owned_volume, stop_and_audit_owned_app)
from .streams import SMALL_BYTES, read_small
from .record_archive import seal_directory, stage_archive, publish_archive, archive_ref
from .s3 import STREAMED_PUT_BYTES
from .archive_retention import validate_retention, cleanup_disposition, retention_report

MOUNT = '/coppertop-mechanism'
ADDITIONAL = Decimal('6')
CUMULATIVE = Decimal('64.82')


def selected_worker(workload):
    if workload == 'mechanism': return worker_plan()
    if workload in ('official_recording','official_recording_full'):
        from .official_recording_worker import plan
        return plan(full=workload=='official_recording_full')
    raise ValueError('Known bounded workload required')


def prepare(ledger, attempt, workload='mechanism', gpu='L40S', retention=None):
    """Pure preparation: no credentials, signing, provider calls or allocation."""
    if not re.fullmatch(r'mechanism-[0-9a-f]{16}', attempt):
        raise ValueError('Dedicated mechanism attempt required')
    if gpu not in ('L40S', 'A100-80GB'):
        raise ValueError('Explicit supported GPU required; no fallback')
    worker = selected_worker(workload)
    full = workload=='official_recording_full'
    additional = Decimal('8') if full else ADDITIONAL
    cumulative = CUMULATIVE if workload == 'mechanism' else Decimal('70.82')
    if Decimal(str(ledger['cumulative_cap'])) != cumulative or historical_total(ledger)+additional > cumulative:
        raise ValueError('Historical usage plus new attempt cap must fit cumulative ceiling')
    scope = SessionScope('mechanism_archive' if full else 'mechanism', 'coppertop', 'chords/mechanism/'+attempt)
    p = {'kind': 'frozen_mechanism_cloud_feasibility', 'attempt_id': attempt,
        'app_name': 'coppertop-'+attempt, 'volume_name': 'coppertop-'+attempt+'-outputs',
        'environment': 'main', 'region': 'us', 'image': worker['image'],
        'worker': worker, 'worker_sha256': digest(canonical(worker)),
        'ledger_sha256': digest(canonical(ledger)), 'retained_holds_usd': str(historical_total(ledger)),
        'additional_cap_usd': str(additional), 'cumulative_cap_usd': str(cumulative),
        'resources': {'gpu': gpu, 'gpu_cpu': 4, 'gpu_memory_mib': 65536,
            'gate_cpu': 4, 'gate_memory_mib': 16384, 'publisher_cpu': 2, 'publisher_memory_mib': 4096,
            'single_use_containers': True, 'retries': 0, 'scaledown_seconds': 2,
            'startup_seconds_each': 300, 'gate_seconds': 610, 'worker_seconds': 250, 'gpu_phase_seconds': 270,
            'publisher_seconds': 300, 'controller_seconds': 2700,
            'temporary_volume_bytes': 32*1024**2, 'metadata_bytes': 65536},
        'auth': {'ttl_seconds': 3600, 'session_count': 1, 'bucket': scope.bucket,
            'role': scope.role, 'actions': list(scope.actions), **scope.paths},
        'pricing': {'checked_utc': '2026-10-08T16:36:51+00:00',
            'modal_url': 'https://modal.com/pricing', 'r2_url': 'https://developers.cloudflare.com/r2/pricing/',
            'l40s_per_second': '.000542', 'cpu_core_second': '.0000131', 'gib_second': '.00000222',
            'region_multiplier_bound': '1.75', 'r2_incremental_rounded_requests_usd': '4.86',
            'r2_first_month_storage_unit_usd': '.015', 'compute_storage_egress_envelope_usd': '1.00'},
        'retention': 'Keep ≤24MiB immutable R2 evidence in Standard storage; no R2 deletion. Delete only new owned Volume after publication attempt and zero-active audit. Preserve cached images and all prior artifacts.',
        'paid_authorized': False, 'new_image_builds': False, 'new_model_or_asset_downloads': False}
    if full:
        p['resources'].update(worker_seconds=560,gpu_phase_seconds=600,publisher_seconds=2100,
            controller_seconds=4800,temporary_volume_bytes=2*1024**3)
        # Cover gate/startup/GPU/publication/cleanup without a session expiring
        # during the enlarged publisher budget. This requires explicit approval.
        p['auth']['ttl_seconds']=7200
        p['pricing'].update(r2_first_month_storage_unit_usd='.030',compute_storage_egress_envelope_usd='1.50')
        p['publication'] = {'format':'coppertop-episode-tar', 'part_bytes':16*1024**2,
            'concurrency':4, 'max_raw_bytes':1024**3, 'max_archive_bytes':1024**3+16*1024**2,
            'completion_receipt_bytes':65536, 'full_readback_sha256':True,
            'single_put_threshold_bytes':STREAMED_PUT_BYTES,
            'multipart_live_completion_verified':False,
            'deadline_basis':{'local_validated_archive_bytes':156098560,
                'local_upload_seconds':37.12929216702469,'local_readback_seconds':201.9889850000618,
                'modal_publication_timing_verified':False,
                'bound_extrapolation':'About28minutes at local measured rates for maximum archive;35minute deadline includes margin but does not guarantee completion.'}}
        p['resources']['producer_scratch_bytes']=2*1024**3+16*1024**2
        p['archive_retention'] = validate_retention(retention) if retention is not None else None
        p['pricing']['volume_storage_usd_per_gib_month']='0.09'
        p['retention']='Release GPU after verified archive staging. After publication failure, preserve the size-bounded owned Volume only under explicit ongoing-storage authorization. Release after verified publication or a separately authorized recovery/deletion decision; no automatic expiry.'
    if workload != 'mechanism':
        p.update(kind='official_full_rollout_recording' if full else 'official_recording_cloud_acceptance', workload=workload)
    if gpu == 'A100-80GB':
        p['pricing']['a100_80gb_per_second'] = '.000694'
        if full:
            p['pricing']['compute_storage_egress_envelope_usd'] = '2.00'
    return {'cloud_plan': p, 'cloud_plan_sha256': digest(canonical(p)), 'scope': scope}


def output_inventory(directory, *, full=False):
    """Only digest-addressed objects and bounded metadata can leave the worker."""
    directory = Path(directory)
    if directory.is_symlink(): raise ValueError('Output symlink rejected')
    byte_limit=2*1024**3 if full else 24*1024**2
    object_bytes=64*1024**2 if full else SMALL_BYTES
    files = list(directory.rglob('*'))
    if any(path.is_symlink() for path in files) or sum(p.stat().st_size for p in files if p.is_file()) > byte_limit:
        raise ValueError('Output filesystem exceeds immutable publication bound')
    objects = {}
    if not (directory/'store').exists(): return objects
    local = LocalStore(directory/'store')
    for path in (directory/'store'/'objects').glob('*/*'):
        if not path.is_file() or path.relative_to(directory/'store').as_posix() != object_path(path.name):
            raise ValueError('Malformed immutable output path')
        ref = local.stat(path.name)
        if ref.size > object_bytes: raise ValueError('Single immutable PUT exceeds approved buffer')
        if digest(read_small(local,ref.sha256,object_bytes)) != ref.sha256: raise ValueError('Output checksum differs')
        objects[ref.sha256] = ref.size
    if len(objects) > (5999 if full else 64) or sum(objects.values()) > (1024**3-1024**2 if full else 24*1024**2-65536):
        raise ValueError('Publication object count/bytes exceed session')
    return objects


def retained_stage_report(volume, resource, policy, rate):
    """Bounded provider metadata read; measurement failure never deletes the sole copy."""
    measured_bytes=None;size_error=None
    try:
        with hard_deadline(60):
            total=0
            for number,entry in enumerate(volume.iterdir('/',recursive=True),1):
                if number>32:raise ValueError('Owned staging entry count exceeds bound')
                # Modal1.6.1 FileEntryType.FILE=1, DIRECTORY=2.
                if entry.type==2:continue
                if entry.type!=1 or type(entry.size) is not int or entry.size<0:
                    raise ValueError('Invalid owned staging entry')
                total+=entry.size
            measured_bytes=total
    except Exception as caught:
        size_error=type(caught).__name__
    return retention_report(resource,policy,rate_usd_per_gib_month=rate,
        size_bytes=measured_bytes,size_error=size_error)


def cleanup_owned_stage(sdk, volume, *, name, identity, audit, gpu_started,
                        publication_verified, policy, rate):
    if (audit.get('app_stopped') is not True or any(type(audit.get(key)) is not int or audit[key]!=0
            for key in ('active_containers','active_sandboxes'))):
        raise ValueError('Zero-active compute audit required before staging cleanup')
    disposition=cleanup_disposition(gpu_started=gpu_started,publication_verified=publication_verified,
        retention=policy)
    if disposition=='retain_for_recovery':
        retained=retained_stage_report(volume,{'provider':'modal','id':identity,'name':name},policy,rate)
        if retained['size_bytes']!=0:
            return dict(audit,volume_deleted=False,volume_retained_for_recovery=True,
                retention=retained,holds_released=0)
    delete_owned_volume(sdk,name,identity)
    return dict(audit,volume_deleted=True,holds_released=0)


def publish_directory(directory, record, store, *, full=False):
    """CPU-only upload with immutable writes and complete byte/hash readback."""
    objects = {} if record.get('artifact_error_type') else output_inventory(directory,full=full)
    local = LocalStore(Path(directory)/'store') if objects or 'inventory' in record else None
    declared=json.loads(read_small(local,record['inventory'],1024**2)) if 'inventory' in record else record['objects']
    if 'inventory' in record:
        declared[record['inventory']]=local.stat(record['inventory']).size
        if len(declared)!=record['object_count'] or sum(declared.values())!=record['object_bytes']:
            raise ValueError('Inventory count/bytes differ')
    if objects != declared: raise ValueError('Committed output inventory differs')
    inventory_raw=canonical(objects) if full and 'objects' in record else None
    if inventory_raw is not None and (len(objects)+2>6000 or sum(objects.values())+len(inventory_raw)+65536>1024**3):
        raise ValueError('Recovery publication exceeds full object/byte bound')
    for key in objects:
        limit=64*1024**2 if full else SMALL_BYTES
        raw = read_small(local,key,limit)
        stored=store.put_bounded(raw,limit=limit) if full and hasattr(store,'put_bounded') else store.put(raw)
        if stored != key or read_small(store,key,limit) != raw:
            raise ValueError('Immutable artifact readback differs')
    if inventory_raw is not None:
        inventory=store.put(inventory_raw)
        if read_small(store,inventory,1024**2)!=inventory_raw: raise ValueError('Recovery inventory readback differs')
        record={key:value for key,value in record.items() if key!='objects'}
        record.update(inventory=inventory,object_count=len(objects)+int(inventory not in objects),
            object_bytes=sum(objects.values())+(len(inventory_raw) if inventory not in objects else 0))
    raw = canonical(record)
    if len(raw) > 65536: raise ValueError('Report metadata bound exceeded')
    key = store.put(raw)
    if key != digest(raw) or read_small(store, key, 65536) != raw:
        raise ValueError('Immutable feasibility report readback differs')
    return key


def create_functions(sdk, prepared, image, volume, store_factory, session, runner=subprocess.run):
    """Local registration only; exactly three sequential single-use functions."""
    if sdk.__version__ != '1.6.1': raise ValueError('Modal1.6.1 required')
    p = prepared['cloud_plan']
    full=p.get('workload')=='official_recording_full'
    resources=p['resources']
    mount = volume.with_mount_options(sub_path='/'+p['attempt_id'])
    app = sdk.App(p['app_name'], secrets=[], volumes={}, include_source=False)
    common = dict(image=image, region='us', serialized=True, retries=0, single_use_containers=True,
        min_containers=0, buffer_containers=0, max_containers=1, scaledown_window=2, startup_timeout=300)
    def binding():
        if selected_worker(p.get('workload','mechanism')) != p['worker']: raise ValueError('Mounted worker plan differs')
    @app.function(**dict(common, name='mechanism-cache-gate', cpu=(4,4), memory=(16384,16384),
        gpu=None, timeout=610, secrets=[], block_network=True, volumes={}))
    def gate():
        binding(); require_native_interpreter(); loopback_gate(); source_gate()
        require_dependencies(pinned_dependencies(Path('/app/requirements-gpu-smoke.lock'), Path('/app/requirements-r2.txt')))
        from .smoke_runtime import verify_snapshot
        verify_snapshot(MODEL, read_json(Path('/app/containers/gpu-smoke-pins.json'))['model'])
        result = simulator_preflight(runner)
        return {'container_id': _container(os.environ.get('MODAL_TASK_ID')), 'status': result['status'],
                'simulator': result, 'plan_sha256': prepared['cloud_plan_sha256']}
    @app.function(**dict(common, name='mechanism-feasibility', cpu=(4,4), memory=(65536,65536),
        gpu=resources['gpu'], timeout=resources['gpu_phase_seconds'], secrets=[], block_network=True, volumes={MOUNT: mount}))
    def evaluate():
        binding()
        target = Path(MOUNT)/'outputs'
        # LocalStore atomically links objects; the live Volume rejected links.
        # Finish on the worker filesystem, then copy checked bytes to the Volume.
        scratch = Path('/tmp')/(p['attempt_id']+'-outputs')
        env = {key: os.environ[key] for key in ('PATH','HOME','LD_LIBRARY_PATH','NVIDIA_VISIBLE_DEVICES','CUDA_VISIBLE_DEVICES') if key in os.environ}
        env.update(HF_HUB_OFFLINE='1', TRANSFORMERS_OFFLINE='1', MUJOCO_GL='egl', PYTHONPATH='/app')
        with (Path('/tmp')/(p['attempt_id']+'-worker.log')).open('wb') as log:
            try:
                module = 'coppertop.official_recording_worker' if p.get('workload') in ('official_recording','official_recording_full') else 'coppertop.mechanism_runtime'
                identity_args = ['--run-id',p['attempt_id'], *(['--full'] if full else [])] if p.get('workload') in ('official_recording','official_recording_full') else []
                process = runner([SIMULATOR, '-m', module, '--execute-local',
                    '--plan-sha256', p['worker_sha256'], '--output', str(scratch), *identity_args],
                    stdout=log, stderr=log, env=env, timeout=resources['worker_seconds'])
                passed = process.returncode == 0
            except subprocess.TimeoutExpired:
                passed = False
        result_path = scratch/('result.json' if passed else 'failure.json')
        result = read_json(result_path, 65536) if result_path.is_file() else {'status':'failed','comparison_valid':False}
        recording = p.get('workload') in ('official_recording','official_recording_full')
        expected_status, expected_queries = ('recording_acceptance',1) if recording else ('bounded_feasibility',5)
        if passed and (result.get('status') != expected_status or
                (not full and result.get('query_calls') != expected_queries) or
                (full and (not 1<=result.get('query_calls',0)<=38 or result.get('cadence')!=16 or result.get('horizon')!=600))):
            raise ValueError('Feasibility gate result differs')
        record = {'container_id': _container(os.environ.get('MODAL_TASK_ID')),
            'plan_sha256': prepared['cloud_plan_sha256'], 'attempt_id': p['attempt_id'],
            'status': 'passed' if passed else 'failed', 'result': result,
            'objects': output_inventory(scratch,full=full), 'worker_plan': p['worker']}
        # Publish even failed live gates; never report them as policy failures.
        record['query_journal'] = read_json(scratch/'queries.json', 65536) if (scratch/'queries.json').is_file() else {'calls':[]}
        if full:
            # The complete inventory is an immutable object, outside Modal's inline report.
            objects=record.pop('objects')
            inventory=LocalStore(scratch/'store').put(canonical(objects))
            inventory_size=LocalStore(scratch/'store').stat(inventory).size
            objects[inventory]=inventory_size
            record.update(inventory=inventory,object_count=len(objects),object_bytes=sum(objects.values()))
        if len(canonical(record)) > (60*1024 if full else 65536): raise ValueError('Metadata envelope exceeds bound')
        if full:
            save(scratch/'cloud-record.json', record)
            descriptor=seal_directory(scratch, Path('/tmp')/(p['attempt_id']+'-episode.tar'))
            # Write the bounded control metadata before the same provider barrier
            # that commits the verified archive. No per-file Volume staging.
            save(Path(MOUNT)/'record.json', record)
            staged=stage_archive(Path('/tmp')/(p['attempt_id']+'-episode.tar'), descriptor,
                Path(MOUNT)/'archive', run_id=p['attempt_id'], commit=volume.commit)
        elif scratch.exists():
            output_inventory(scratch,full=full)
            shutil.copytree(scratch,target)
            output_inventory(target,full=full)
        if not full:
            save(Path(MOUNT)/'record.json', record); volume.commit()
        # A full record triggered Modal's blob upload despite block_network.
        # Keep the control response small; the CPU publisher reads the Volume.
        receipt={key:record[key] for key in ('container_id','plan_sha256','attempt_id','status')}
        receipt['record_sha256']=digest(canonical(record))
        if full:
            receipt.update(durable_archive_verified=True, archive=archive_ref(staged).to_dict())
            receipt['stage_timings']={key:staged[key] for key in ('seal_seconds','durable_copy_seconds')}
        if len(canonical(receipt))>1024: raise ValueError('Control receipt exceeds inline bound')
        return receipt
    @app.function(**dict(common, name='mechanism-cpu-publisher', cpu=(2,2), memory=(4096,4096),
        gpu=None, timeout=resources['publisher_seconds'], secrets=list(session), volumes={MOUNT: volume.with_mount_options(read_only=not full)}))
    def publish(record):
        publisher_started=time.monotonic()
        binding()
        incoming = digest(canonical(record))
        # GPU scheduling can fail before its subdirectory exists. Mount the owned
        # Volume root read-only, then read the attempt path without creating it.
        directory=Path(MOUNT)/p['attempt_id']
        if full and (directory/'archive'/'archive.json').is_file():
            descriptor=read_json(directory/'archive'/'archive.json',65536)
            committed=read_json(directory/'record.json',65536)
            if not record.get('recovery') and (digest(canonical(committed)) != record.get('record_sha256')
                    or archive_ref(descriptor).to_dict() != record.get('archive')
                    or any(committed.get(key)!=record.get(key) for key in ('container_id','plan_sha256','attempt_id','status'))):
                raise ValueError('Committed archive control receipt differs')
            if not record.get('recovery'):
                descriptor.update(record['stage_timings'])
            from .multipart import UploadState
            journal=directory/'upload-state.json'
            state=UploadState.from_dict(read_json(journal,65536)) if journal.exists() else None
            def persist(value):
                save(journal,value); volume.commit()
            publication=publish_archive(directory/'archive'/'episode.tar', descriptor,
                store_factory('mechanism_archive'), record=committed,state=state,persist=persist,
                publisher_started=publisher_started)
            return dict(publication, container_id=_container(os.environ.get('MODAL_TASK_ID')),
                source_record_sha256=incoming, published_status=record['status'],
                plan_sha256=prepared['cloud_plan_sha256'], archive_verified=True)
        if full and not record.get('recovery'):
            raise ValueError('Complete staged archive required')
        if record.get('recovery') is True:
            # Failed provider RPCs cannot bypass durable failure publication. Only
            # committed Volume bytes are available; missing/uncommitted data is explicit.
            record = dict(record)
            path = directory/'record.json'
            if path.is_file():
                try: record['committed_record'] = read_json(path, 32768)
                except (OSError, ValueError): record['committed_record_unavailable'] = True
            target = directory/'outputs'
            try: record['objects'] = output_inventory(target,full=full)
            except (OSError, ValueError) as error:
                record['objects'] = {}; record['artifact_error_type'] = type(error).__name__
            journal = target/'queries.json'
            if journal.is_file():
                try: record['query_journal'] = read_json(journal, 32768)
                except (OSError, ValueError): record['query_journal_unavailable'] = True
        elif 'record_sha256' in record:
            committed=read_json(directory/'record.json',65536)
            if (digest(canonical(committed))!=record['record_sha256']
                    or any(committed.get(key)!=record.get(key) for key in ('container_id','plan_sha256','attempt_id','status'))):
                raise ValueError('Committed feasibility receipt differs')
            record=committed
        elif read_json(directory/'record.json', 65536) != record:
            raise ValueError('Committed feasibility record differs')
        # Recovery without a sealed stage records failure only; partial outputs
        # cannot masquerade as a complete recording.
        if full:
            record['objects']={}
        key = publish_directory(directory/'outputs', record, store_factory('mechanism_archive' if full else 'mechanism'),full=full)
        return {'container_id': _container(os.environ.get('MODAL_TASK_ID')), 'report_key': key,
                'source_record_sha256': incoming, 'published_status': record['status'],
                'plan_sha256': prepared['cloud_plan_sha256']}
    return SimpleNamespace(app=app, gate=gate, evaluate=evaluate, publish=publish)


def execute_phases(functions, prepared, output, *, admission, invoke, stopped, cleanup):
    state = {'status':'started', 'phases':{}, 'automatic_retries':0}
    error = None
    resources=prepared['cloud_plan']['resources']
    try:
        admission()
        for name, function, args, bound in (('gate', functions.gate, (), 920),
                ('evaluate', functions.evaluate, (), resources['gpu_phase_seconds']+310), ('publish', functions.publish, None, resources['publisher_seconds']+310)):
            if name == 'publish': args = (state['phases']['evaluate'],)
            if name == 'evaluate':
                state['gpu_started']=True; save(Path(output)/'outcome.json',state)
            try: result = invoke(function, args, bound)
            except Exception as caught:
                if name != 'evaluate': raise
                result = {'container_id':None, 'plan_sha256':prepared['cloud_plan_sha256'],
                    'attempt_id':prepared['cloud_plan']['attempt_id'], 'status':'failed', 'recovery':True,
                    'objects':{}, 'result':{'status':'failed','comparison_valid':False,'error_type':type(caught).__name__}}
            _metadata(result)
            state['phases'][name] = result; save(Path(output)/'outcome.json', state)
            if name=='evaluate' and 'archive' in result and result.get('durable_archive_verified') is not True:
                raise ValueError('GPU release requires verified durable archive checkpoint')
            if name=='evaluate' and prepared['cloud_plan'].get('publication') and not result.get('recovery'):
                from .streams import ObjectRef
                ref=ObjectRef(**result.get('archive',{}))
                if not 0<ref.size<=prepared['cloud_plan']['publication']['max_archive_bytes']:
                    raise ValueError('Bounded archive checkpoint required before GPU release')
            if result.get('plan_sha256') != prepared['cloud_plan_sha256'] or stopped(result['container_id']) is not True:
                raise ValueError('Phase binding/stop verification failed')
            if name == 'gate' and result.get('status') != 'passed':
                raise ValueError('CPU gate failed; GPU forbidden')
            if name == 'publish':
                prior=state['phases']['evaluate']; expected=digest(canonical(prior))
                if (result.get('source_record_sha256') != expected or result.get('published_status') != prior['status']
                        or not re.fullmatch('[0-9a-f]{64}', result.get('report_key',''))
                        or (not prior.get('recovery') and 'archive' not in prior and result['report_key'] != prior.get('record_sha256',expected))
                        or ('archive' in prior and (result.get('archive_verified') is not True or result.get('archive') != prior['archive']))):
                    raise ValueError('Publication receipt differs')
        state['status'] = state['phases']['evaluate']['status']
        state['durable_report_verified'] = (state['phases']['publish'].get('archive_verified') is True
            if prepared['cloud_plan'].get('publication') else True)
    except BaseException as caught:
        error = caught; state.update(status='failed', error_type=type(caught).__name__)
    finally:
        try:
            save(Path(output)/'outcome.json',state)
        except BaseException as caught:
            state.update(status='failed',outcome_save_error_type=type(caught).__name__)
            if error is None:error=caught
        try:
            state['cleanup'] = receipt = cleanup()
            retained=(state['status']=='failed' and receipt.get('volume_retained_for_recovery') is True
                and receipt.get('retention',{}).get('ongoing_charges_accepted') is True)
            if receipt.get('app_stopped') is not True or (receipt.get('volume_deleted') is not True and not retained) or any(type(receipt.get(k)) is not int or receipt[k] != 0 for k in ('active_containers','active_sandboxes')):
                raise ValueError('Owned cleanup not verified')
        except BaseException as caught:
            state.update(status='failed', cleanup_error_type=type(caught).__name__)
            if error is None: error = caught
        save(Path(output)/'outcome.json', state)
    if error is not None: raise error
    return state


def execute(prepared, ledger, root, env_file, approved_sha256, *, sdk=None, runner=subprocess.run):
    """One exact approval, one retained hold, no fallback/retry or shared cleanup."""
    checked = prepare(ledger, prepared['cloud_plan']['attempt_id'], prepared['cloud_plan'].get('workload','mechanism'),
        prepared['cloud_plan']['resources']['gpu'], prepared['cloud_plan'].get('archive_retention'))
    if (approved_sha256 != prepared['cloud_plan_sha256']
            or digest(canonical(prepared['cloud_plan'])) != approved_sha256
            or checked['cloud_plan_sha256'] != approved_sha256):
        raise ValueError('Exact proposed plan/code/ledger approval required')
    prepared = checked  # Never sign a caller-supplied scope merely matching a hash field.
    pricing_time = datetime.fromisoformat(prepared['cloud_plan']['pricing']['checked_utc'])
    if not 0 <= (datetime.now(timezone.utc)-pricing_time).total_seconds() <= 86400:
        raise ValueError('Pricing review expired; reprepare approval before launch')
    if sdk is None: import modal as sdk
    if sdk.__version__ != '1.6.1': raise ValueError('Modal1.6.1 required')
    p = prepared['cloud_plan']; root = Path(root).resolve()
    full=p.get('workload')=='official_recording_full'
    if full:
        validate_retention(p.get('archive_retention'))
    output = root/'.local/mechanism'/p['attempt_id']
    if any(path.is_symlink() for path in (root/'.local', output.parent, output)):
        raise ValueError('Dedicated ignored attempt directory required')
    output.mkdir(parents=True, exist_ok=True)
    with (output/'consumed.json').open('x') as marker: marker.write(canonical({'plan_sha256':approved_sha256}).decode())
    owned = {'created':False, 'attempted':False, 'volume_id':None, 'context':None, 'entered':False}
    functions = volume = None
    current = ledger
    pending = 'pending-'+p['attempt_id']
    deadline=time.monotonic()+p['resources']['controller_seconds']
    def ownership():
        save(output/'ownership.json', {k:v for k,v in owned.items() if k != 'context'} | {'app_id': functions.app.app_id if functions else None,'volume_name':p['volume_name']})
    def bind():
        if functions and functions.app.app_id and pending in current['apps']:
            identity = functions.app.app_id
            if not re.fullmatch(r'ap-[A-Za-z0-9]+', identity) or identity in current['apps']: raise ValueError('App ledger identity collision')
            current['apps'][identity] = current['apps'].pop(pending); save(output/'ledger.json', current); ownership()
    def billing():
        nonlocal current
        bind(); identity = functions.app.app_id if functions else None
        key = identity if identity in current['apps'] else pending
        current = refresh_billing(current, output, app_id=identity, reservation_key=key, runner=runner, additional_cap=int(p['additional_cap_usd']))
    def admission():
        billing()
        owned['attempted']=True; ownership()
        with hard_deadline(60):
            sdk.Volume.objects.create(p['volume_name'], environment_name='main', allow_existing=False)
            owned['created']=True; volume.hydrate(); owned['volume_id']=volume.object_id; ownership()
        owned['context']=functions.app.run(name=p['app_name'], environment_name='main')
        with hard_deadline(300): owned['context'].__enter__()
        owned['entered']=True; bind()
    def invoke(function, args, bound):
        billing()
        remaining=deadline-time.monotonic()
        if function is functions.evaluate and remaining < p['resources']['gpu_phase_seconds']+310+p['resources']['publisher_seconds']+310+240:
            raise TimeoutError('Insufficient whole-attempt time for GPU/publication/cleanup')
        if remaining <= 0: raise TimeoutError('Controller deadline exhausted')
        return function.spawn(*args).get(timeout=min(bound,remaining))
    def stopped(identity):
        if identity is None:
            # A failed RPC supplied no task ID. Prove the exact app has no GPU
            # task/sandbox left before its single CPU recovery publisher runs.
            # Do not terminate individual tasks: Modal can reschedule cancelled
            # inputs even with retries=0. Wait for the bounded Function instead.
            idle_deadline=time.monotonic()+120
            while time.monotonic() < idle_deadline:
                containers=cli_json([sys.executable,'-m','modal','container','list','--app-id',functions.app.app_id,'--env','main','--json'],runner,65536)
                script='import modal,json,sys; print(json.dumps([s.object_id for s in modal.Sandbox.list(app_id=sys.argv[1])]))'
                sandboxes=cli_json([sys.executable,'-c',script,functions.app.app_id],runner,65536)
                if containers == [] and sandboxes == []: return True
                time.sleep(2)
            return False
        return stop_verifier(functions.app.app_id, environment='main', command=(sys.executable,'-m','modal'), runner=runner)(identity)
    def cleanup():
        bind(); ownership()
        context_error = None
        if owned['entered']:
            try:
                with hard_deadline(60): owned['context'].__exit__(None,None,None)
            except BaseException as caught: context_error=type(caught).__name__
            owned['entered']=False
        receipt = {'app_stopped':True,'active_containers':0,'active_sandboxes':0}
        if functions and functions.app.app_id: receipt=stop_and_audit_owned_app(functions.app.app_id, runner=runner)
        if owned['created']:
            if not owned['volume_id']:
                with hard_deadline(60): volume.hydrate(); owned['volume_id']=volume.object_id; ownership()
            outcome=read_json(output/'outcome.json') if (output/'outcome.json').is_file() else {}
            if full:
                receipt=cleanup_owned_stage(sdk,volume,name=p['volume_name'],identity=owned['volume_id'],audit=receipt,
                    gpu_started=outcome.get('gpu_started',False),publication_verified=outcome.get('durable_report_verified',False),
                    policy=p['archive_retention'],rate=p['pricing']['volume_storage_usd_per_gib_month'])
                if receipt.get('volume_retained_for_recovery'):
                    save(output/'retention.json',receipt['retention'])
                billing()
                return dict(receipt,context_exit_error_type=context_error)
            delete_owned_volume(sdk,p['volume_name'],owned['volume_id'])
        elif owned['attempted']: raise RuntimeError('Unconfirmed Volume creation requires ownership audit')
        billing()
        return dict(receipt,volume_deleted=True,holds_released=0,context_exit_error_type=context_error)
    try:
        with hard_deadline(p['resources']['controller_seconds']):
            current = refresh_billing(ledger, output, runner=runner, additional_cap=int(p['additional_cap_usd']))
            current['apps'][pending]={'state':'unsettled_bounded','known_reported_usd':'0','reservation_usd':p['additional_cap_usd'],'description':p['app_name']}
            save(output/'ledger.json',current)
            for command, field in (('app','description'),('volume','name')):
                rows = cli_json([sys.executable,'-m','modal',command,'list','--env','main','--json'],runner)
                if any(row.get(field) == p['app_name' if command == 'app' else 'volume_name'] for row in rows): raise ValueError('Dedicated resource already exists')
            values=selected_env(env_file); access,secret=parent_credentials(values); endpoint=values['CLOUDFLARE_R2_URL']
            scope=prepared['scope']
            session=[sdk.Secret.from_dict(mint(access,secret,endpoint=endpoint,scope=scope,ttl_seconds=p['auth']['ttl_seconds']).environment())]
            del values,access,secret
            with hard_deadline(60): image=sdk.Image.from_id(p['image'])
            if image.object_id != p['image']: raise ValueError('Cached image differs')
            for name, expected in p['worker']['runtime_sha256'].items():
                path=root/name
                if path.is_symlink() or digest(path.read_bytes()) != expected: raise ValueError('Approved upload code differs')
                if not name.startswith('coppertop/'):
                    image=image.add_local_file(path,'/app/'+name,copy=False)
            image=image.add_local_dir(root/'coppertop','/app/coppertop',copy=False,
                ignore=lambda path: path.suffix != '.py')
            volume=sdk.Volume.from_name(p['volume_name'],environment_name='main',create_if_missing=False)
            functions=create_functions(sdk,prepared,image,volume,R2StoreFactory(endpoint,(scope,)),session,runner)
            return execute_phases(functions,prepared,output,admission=admission,invoke=invoke,stopped=stopped,cleanup=cleanup)
    except BaseException as error:
        save(output/'launch-failure.json',{'status':'failed','error_type':type(error).__name__,'consumed':True,'automatic_retries':0})
        raise


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--ledger',type=Path,required=True)
    parser.add_argument('--attempt',required=True)
    parser.add_argument('--workload',choices=('mechanism','official_recording','official_recording_full'),default='mechanism')
    parser.add_argument('--gpu', choices=('L40S','A100-80GB'), default='L40S')
    parser.add_argument('--archive-retention',type=Path,help='Explicit size-bounded ongoing staging-storage authorization and release condition JSON')
    parser.add_argument('--execute',action='store_true')
    parser.add_argument('--approved-cloud-plan-sha256')
    parser.add_argument('--env-file',type=Path,default=Path.home()/'code/coppertop/.env.local')
    args=parser.parse_args(); ledger=read_json(args.ledger)
    prepared=prepare(ledger,args.attempt,args.workload,args.gpu,
        read_json(args.archive_retention) if args.archive_retention else None)
    if not args.execute:
        print(json.dumps({key:value for key,value in prepared.items() if key!='scope'})); return 0
    result = execute(prepared,ledger,Path(__file__).resolve().parent.parent,args.env_file,args.approved_cloud_plan_sha256)
    return 0 if result.get('status') == 'passed' else 1


if __name__ == '__main__': raise SystemExit(main())
