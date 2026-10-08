"""Credential-free Pod adapter for the existing frozen official recording worker."""
import os
from pathlib import Path
import shutil
import subprocess

from .chords import LocalStore, canonical, digest
from .mechanism_cloud import output_inventory
from .reload_launch import hard_deadline, read_json
from .runpod_cloud import LIMITS, identifier, prepare, save


def run(prepared,*,mount=Path('/workspace'),scratch=Path('/tmp'),runner=subprocess.run,admit=None):
    from .runpod_runtime import cpu_admission
    admit=admit or cpu_admission
    if prepare(prepared['plan']['config'])!=prepared: raise ValueError('Mounted runtime/code/configuration differs')
    p=prepared['plan'];c=p['config']
    if (os.environ.get('COPPERTOP_PLAN_SHA256')!=prepared['plan_sha256']
        or os.environ.get('COPPERTOP_RUN_ID')!=c['run_id']
        or os.environ.get('COPPERTOP_RUNTIME_IMAGE')!=c['image']):
        raise ValueError('Pod approval identity differs')
    pod_id=identifier(os.environ['RUNPOD_POD_ID'])
    exported=read_json(Path('/opt/runpod-cpu-admission.json'))
    if digest(canonical(exported))!=c['cpu_admission_sha256']: raise ValueError('Built CPU receipt differs')
    base=Path(mount)/'coppertop/runs'/c['run_id']
    target=Path(scratch)/(c['run_id']+'-outputs')
    if target.exists() or (base/'ready.json').exists(): raise ValueError('Single-use worker already ran')
    record={'provider':'RunPod','pod_id':pod_id,'run_id':c['run_id'],
            'plan_sha256':prepared['plan_sha256'],'status':'failed','objects':{},
            'image':c['image'],'runtime_export_sha256':c['runtime_export_sha256'],
            'automatic_retries':0}
    try:
        record['admission']=admit()
        if record['admission']['status']!='passed': raise ValueError('Runtime admission failed')
        # Both child interpreters get the same attested provider identity. The
        # original stock deploy client/server remains on loopback inside this Pod.
        identity={'provider':'RunPod','image':c['image'],'runtime_export_sha256':c['runtime_export_sha256'],
                  'cpu_admission_sha256':c['cpu_admission_sha256']}
        identity_path=Path(scratch)/(c['run_id']+'-runtime.json');save(identity_path,identity)
        env={name:os.environ[name] for name in ('PATH','HOME','LD_LIBRARY_PATH','NVIDIA_VISIBLE_DEVICES','CUDA_VISIBLE_DEVICES') if name in os.environ}
        env.update(PYTHONPATH='/app',HF_HUB_OFFLINE='1',TRANSFORMERS_OFFLINE='1',
            MUJOCO_GL='egl',NVIDIA_DRIVER_CAPABILITIES='compute,utility,graphics',
            COPPERTOP_RUNPOD_RUNTIME=str(identity_path))
        process=None
        with (Path(scratch)/(c['run_id']+'-worker.log')).open('wb') as log:
            if runner is subprocess.run:
                # A whole process group is owned, including the native model server.
                process=subprocess.Popen(['/work/sim/bin/python','-m','coppertop.official_recording_worker',
                    '--execute-local','--full','--run-id',c['run_id'],'--plan-sha256',p['worker_sha256'],
                    '--output',str(target)],stdout=log,stderr=log,env=env,start_new_session=True)
                try: code=process.wait(timeout=LIMITS['worker_seconds'])
                finally:
                    from .performance_runtime import stop_owned
                    stop_owned(process)
            else:
                code=runner(['/work/sim/bin/python','-m','coppertop.official_recording_worker',
                    '--execute-local','--full','--run-id',c['run_id'],'--plan-sha256',p['worker_sha256'],
                    '--output',str(target)],stdout=log,stderr=log,env=env,timeout=LIMITS['worker_seconds']).returncode
        record['result']=read_json(target/('result.json' if code==0 else 'failure.json'),65536)
        record['status']='passed' if code==0 else 'failed'
        record['objects']=output_inventory(target,full=True)
    except BaseException as error:
        record['status']='failed';record['error_type']=type(error).__name__
        try: record['objects']=output_inventory(target,full=True)
        except Exception as inventory_error:
            record['objects']={};record['artifact_error_type']=type(inventory_error).__name__
    # Filesystem sealing is not a claim of distributed S3 visibility. The external
    # CPU fetch verifies record and every object before durable R2 publication.
    objects=record.pop('objects')
    local=LocalStore(target/'store')
    inventory=local.put(canonical(objects))
    size=local.stat(inventory).size
    # Reserve an inventory and two reports, including readback-failure recovery.
    if len(objects)+3>6000 or sum(objects.values())+size+2*65536>1024**3:
        raise ValueError('Sealed output aggregate exceeds retention bound')
    record.update(inventory=inventory,object_count=len(objects)+int(inventory not in objects),
                  object_bytes=sum(objects.values())+(size if inventory not in objects else 0))
    if len(canonical(record))>65536:
        record={key:value for key,value in record.items() if key not in ('objects','admission','result')}
        raise ValueError('Sealed metadata exceeds bound')
    output_inventory(target,full=True)
    shutil.copytree(target/'store',base/'outputs/store')
    base.mkdir(parents=True,exist_ok=True)
    save(base/'record.json',record)
    for path in base.rglob('*'):
        if path.is_file():
            with path.open('rb') as source: os.fsync(source.fileno())
    for path in [*sorted((p for p in base.rglob('*') if p.is_dir()),reverse=True),base]:
        descriptor=os.open(path,os.O_RDONLY)
        try: os.fsync(descriptor)
        finally: os.close(descriptor)
    receipt={key:record[key] for key in ('pod_id','plan_sha256','status')}
    receipt['record_sha256']=digest(canonical(record))
    save(base/'ready.json',receipt)
    with (base/'ready.json').open('rb') as source: os.fsync(source.fileno())
    return receipt


def main():
    import resource
    # Bounds worker/server logs and every generated artifact without a new runner.
    resource.setrlimit(resource.RLIMIT_FSIZE,(64*1024**2,64*1024**2))
    run_id=os.environ['COPPERTOP_RUN_ID']
    if not __import__('re').fullmatch(r'mechanism-[0-9a-f]{16}',run_id): raise ValueError('Run identity differs')
    prepared=read_json(Path('/workspace/coppertop/runs')/run_id/'plan.json',65536)
    with hard_deadline(1200): run(prepared)
    # Exit is not termination. The external controller deletes and verifies the Pod.


if __name__=='__main__':main()
