"""Official Modal filesystem exporter and RunPod S3 CPU handoff adapters.

Only instantiated after concrete approval. No discovery, downloads, registry,
new credentials, SSH or GPU resources are used by these adapters.
"""
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import shutil
import subprocess
import tarfile
import time

from .chords import canonical, digest
from .reload_launch import hard_deadline, read_json, stop_and_audit_owned_app
from .runpod_bootstrap import verify_packages
from .runpod_bundle import BOUNDS, GIB, validate_descriptor
from .runpod_cloud import identifier, save
from .runpod_evidence import missing, runpod_s3_access_key
from .runpod_prepare import verify_control
from .runpod_relay import Relay, copy_shards, feasibility
from .runpod_staging import RunpodVolumeConfig
from .streams import checked, ObjectRef, CHUNK_BYTES, file_ref


def pack_debs(directory,target):
    directory=Path(directory)
    if directory.is_symlink() or not directory.is_dir():raise ValueError('Ordinary offline closure directory required')
    total=sum(p.stat().st_size for p in directory.rglob('*') if p.is_file())
    if total>BOUNDS['debs']:raise ValueError('Offline closure byte limit')
    with tarfile.open(target,'x',format=tarfile.PAX_FORMAT) as archive:
        for path in sorted(directory.rglob('*')):
            if path.is_symlink() or not (path.is_dir() or path.is_file()):raise ValueError('Package link/special file rejected')
            archive.add(path,arcname=path.relative_to(directory).as_posix(),recursive=False)
    if Path(target).stat().st_size>BOUNDS['debs']:raise ValueError('Closure tar limit')


def untar_code(destination,archive):
    # JSON quoting forms Python code, never a shell command.
    return """import pathlib,tarfile,shutil
d=pathlib.Path(%s)
if d.exists():shutil.rmtree(d)
d.mkdir(parents=True)
with tarfile.open(%s) as a:
 for m in a:
  assert all(p not in ('','.','..') for p in m.name.split('/')) and not m.name.startswith('/')
  assert m.isreg() or m.isdir()
  p=d/m.name
  if m.isdir():p.mkdir(parents=True,exist_ok=True)
  else:
   p.parent.mkdir(parents=True,exist_ok=True)
   with p.open('xb') as out:shutil.copyfileobj(a.extractfile(m),out,262144)
"""%(json.dumps(destination),json.dumps(archive))


class ModalSource:
    def __init__(self):self.app=None;self.context=None;self.sandbox=None;self.closed=False;self.output=None
    def validate_inputs(self,prepared,debs,control):
        # The Mac has neither gpgv nor dpkg-deb. Hash/size/index linkage is
        # checked locally; official signature+binary inspection is the first
        # admitted source-CPU check, before runtime export or RunPod allocation.
        directory=Path(debs)
        if directory.is_symlink() or not directory.is_dir():raise ValueError('Ordinary offline closure directory required')
        paths=list(directory.rglob('*'))
        if any(p.is_symlink() or not (p.is_file() or p.is_dir()) for p in paths):raise ValueError('Offline closure links/special files rejected')
        actual_bytes=sum(p.stat().st_size for p in paths if p.is_file())
        if actual_bytes!=prepared['plan']['config']['package_admission']['bytes'] or actual_bytes>BOUNDS['debs']:
            raise ValueError('Exact approved closure bytes, including metadata, differ')
        lock,_=verify_packages(directory,verify_signature=False,inspect_debs=False)
        if digest(canonical(lock))!=prepared['plan']['config']['package_admission']['lock_sha256']:
            raise ValueError('Exact approved offline package lock differs')
        verify_control(control,prepared['plan']['code'])
        self.validated_control=file_ref(control).to_dict()
        self.validated_debs=digest(canonical(lock))
    def collect(self,prepared,output,*,debs,control):
        self.validate_inputs(prepared,debs,control)
        import modal
        if modal.__version__!='1.6.1':raise ValueError('Pinned Modal1.6.1 required')
        output=Path(output);output.mkdir(parents=True,exist_ok=False);self.output=output
        if shutil.disk_usage(output).free<64*GIB:raise ValueError('Mac needs64GiB free and must remain online')
        p=prepared['plan'];began=time.monotonic()
        with hard_deadline(7020):
            self.app=modal.App('coppertop-'+p['config']['attempt']+'-export',secrets=[],volumes={},include_source=False)
            self.context=self.app.run(environment_name='main')
            ownership={'app_name':'coppertop-'+p['config']['attempt']+'-export','plan_sha256':prepared['plan_sha256']}
            save(output/'source-state.json',dict(ownership,pending='app'))
            with hard_deadline(300):self.context.__enter__()
            save(output/'source-state.json',dict(ownership,app_id=self.app.app_id,pending='sandbox'))
            self.sandbox=modal.Sandbox.create('sleep','6720',app=self.app,image=modal.Image.from_id(p['source_image']),
                cpu=(2,2),memory=(16384,16384),gpu=None,timeout=6720,secrets=[],volumes={},
                region='us',block_network=True,env={'PYTHONPATH':'/app','PYTHONDONTWRITEBYTECODE':'1',
                    'HF_HUB_OFFLINE':'1','TRANSFORMERS_OFFLINE':'1'})
            save(output/'source-state.json',dict(ownership,app_id=self.app.app_id,sandbox_id=self.sandbox.object_id,pending=None))
            remote='/tmp/coppertop-export';fs=self.sandbox.filesystem
            self.command('/opt/conda/bin/python','-c',"from pathlib import Path; import os; Path('/tmp/coppertop-probe').write_bytes(os.urandom(8388608))",seconds=30)
            before=time.monotonic();fs.copy_to_local('/tmp/coppertop-probe',output/'probe.bin');elapsed=time.monotonic()-before
            if (output/'probe.bin').stat().st_size!=8*1024**2:raise ValueError('Source transfer probe size differs')
            sample={'bytes':8*1024**2,'seconds':elapsed,'checked_utc':datetime.now(timezone.utc).isoformat()}
            feasibility([sample],bytes_each=50*GIB,seconds_left=7020-(time.monotonic()-began)-3600)
            (output/'probe.bin').unlink()
            fs.copy_from_local(control,'/tmp/coppertop-control.tar')
            self.command('/opt/conda/bin/python','-c',untar_code('/app','/tmp/coppertop-control.tar'),seconds=60)
            debtar=output/'debs.tar';pack_debs(debs,debtar);fs.copy_from_local(debtar,'/tmp/coppertop-debs.tar')
            self.command('/opt/conda/bin/python','-c',untar_code('/debs','/tmp/coppertop-debs.tar'),seconds=120)
            debtar.unlink()
            self.command('/opt/conda/bin/python','-m','coppertop.runpod_bootstrap','--verify-packages','/debs',seconds=300)
            self.command('/opt/conda/bin/python','-m','coppertop.runpod_bundle','--export-root','/',
                '--output',remote,'--seconds','3600',seconds=3600)
            for name,limit in (('bundle.json',65536),('manifest.json',BOUNDS['manifest'])):
                fs.copy_to_local(remote+'/'+name,output/name)
                if (output/name).stat().st_size>limit:raise ValueError('Source metadata bound')
            descriptor=validate_descriptor(read_json(output/'bundle.json',65536))
            # Re-measure right before copying, after the potentially long export.
            before=time.monotonic();fs.copy_to_local('/tmp/coppertop-probe',output/'probe.bin');elapsed=time.monotonic()-before
            sample={'bytes':8*1024**2,'seconds':elapsed,'checked_utc':datetime.now(timezone.utc).isoformat()}
            (output/'probe.bin').unlink()
            copy_shards(fs,remote,output,descriptor,journal=output/'copy-state.json',sample=sample,
                seconds_left=7020-(time.monotonic()-began))
        return output
    def command(self,*args,seconds):
        process=self.sandbox.exec(*args,timeout=seconds)
        with hard_deadline(seconds):
            if process.wait()!=0:raise ValueError('Bounded source command failed; preserve only local evidence')
    def close(self):
        if self.closed:return
        if self.app is None:self.closed=True;return
        with hard_deadline(180):
            if self.sandbox:self.sandbox.terminate()
            if self.context:
                try:
                    with hard_deadline(30):self.context.__exit__(None,None,None)
                except Exception:pass # Still require the independent absence audit below.
            result=stop_and_audit_owned_app(self.app.app_id)
            if self.output:save(self.output/'source-stop.json',result)
            self.closed=True


class LiveTransport:
    def __call__(self,owner,journal,resume):
        import boto3
        from botocore.config import Config
        dc=owner['datacenter'];config=RunpodVolumeConfig(owner['volume_id'],dc,f'https://s3api-{dc.lower()}.runpod.io/')
        client=boto3.client('s3',endpoint_url=config.endpoint,region_name=dc,
            aws_access_key_id=runpod_s3_access_key(os.environ),aws_secret_access_key=os.environ['RUNPOD_S3_SECRET_ACCESS_KEY'],
            config=Config(connect_timeout=10,read_timeout=20,retries={'total_max_attempts':1},s3={'addressing_style':'path'}))
        return Relay(config,client,owner=owner,journal=journal,resume=resume)
    def probe(self,relay):
        """One8MiB owned probe; durable intent, bounded cleanup, measured two directions."""
        key=relay.prefix+'/probe';size=8*1024**2;relay.allowed[key]=size
        if relay.state.get('probe_attempted'):raise ValueError('Probe is single use; reconcile before recovery')
        relay.metadata(relay.prefix+'/owner.json',canonical(relay.owner))
        relay.state['probe_attempted']=True;relay.persist();data=os.urandom(size);samples=[]
        with hard_deadline(300):
            before=time.monotonic();relay.client.put_object(Bucket=relay.config.volume_id,Key=key,Body=data)
            samples.append({'bytes':size,'seconds':time.monotonic()-before,'checked_utc':datetime.now(timezone.utc).isoformat()})
            before=time.monotonic();response=relay.client.get_object(Bucket=relay.config.volume_id,Key=key)
            body=response['Body']
            try:
                if response['ContentLength']!=size:raise ValueError('Probe readback size')
                for _ in checked(iter(lambda:body.read(CHUNK_BYTES),b''),ObjectRef(digest(data),size)):pass
            finally:body.close()
            samples.append({'bytes':size,'seconds':time.monotonic()-before,'checked_utc':datetime.now(timezone.utc).isoformat()})
            relay.client.delete_object(Bucket=relay.config.volume_id,Key=key)
            try:relay.client.head_object(Bucket=relay.config.volume_id,Key=key)
            except Exception as error:
                if not missing(error):raise
            else:raise ValueError('Probe deletion unverified')
        relay.state.update(probe_verified=True,probe_samples=samples);relay.persist();return samples
    def wait_CPU(self,relay,prepared,pod_id,seconds):
        key=relay.prefix+'/bundle/cpu-receipt.json';relay.allowed[key]=65536
        with hard_deadline(seconds):
            while True:
                try:value=relay.client.head_object(Bucket=relay.config.volume_id,Key=key)
                except Exception as error:
                    if not missing(error):raise
                    time.sleep(10);continue
                if value['ContentLength']>65536:raise ValueError('CPU receipt exceeds64KiB')
                response=relay.client.get_object(Bucket=relay.config.volume_id,Key=key);body=response['Body']
                try:
                    raw=body.read(65537)
                    if len(raw)>65536 or len(raw)!=response['ContentLength']:raise ValueError('CPU receipt length differs')
                    result=json.loads(raw)
                finally:body.close()
                if result.get('plan_sha256')!=prepared['plan_sha256'] or result.get('pod_id')!=pod_id:raise ValueError('CPU receipt identity differs')
                return result
