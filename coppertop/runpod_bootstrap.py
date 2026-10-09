"""Offline locked package checks and actual CPU admission after safe restoration."""
import argparse
import gzip
import os
from pathlib import Path
import re
import shutil
import subprocess
import time

from .chords import canonical, digest
from .reload_launch import hard_deadline, read_json
from .runpod_bundle import BOUNDS, GIB, restore, safe_name
from .runpod_cloud import save
from .runpod_runtime import contract
from .streams import file_ref


def verify_packages(directory,*,runner=subprocess.run,keyring='/usr/share/keyrings/ubuntu-archive-keyring.gpg',
                    verify_signature=True,inspect_debs=True):
    """Verify offline Ubuntu Release signature -> index hash -> exact .deb hash.

    This verifies supplied bytes, never downloads/updates apt or trusts a claimed
    verification flag. Actual dpkg dependency resolution is a separate CPU gate.
    """
    directory=Path(directory);lock=read_json(directory/'lock.json',1024**2)
    if lock.get('schema_version')!=1 or lock.get('architecture')!='amd64' or lock.get('release')!='jammy':
        raise ValueError('Explicit Jammy amd64 package lock required')
    required=lock['required'];base=lock['base'];packages=lock['packages'];indexes=lock['indexes']
    if (set(required)!=set(contract()['system_packages']) or required.get('libopengl0')!='1.4.0-1'
        or not 1<=len(packages)<=400 or not 1<=len(indexes)<=16 or len(base)>400):
        raise ValueError('Complete bounded system closure required')
    if len({p['package'] for p in packages})!=len(packages) or any(p['architecture'] not in ('amd64','all') for p in packages):
        raise ValueError('One exact amd64/all version per closure package required')
    seed=read_json(Path(__file__).resolve().parents[1]/'containers/runpod-seed-inventory.json',65536)
    observed={v['package'].split(':')[0]:v['version'] for v in seed['system_packages']}
    expected={name:('1.4.0-1' if name=='libopengl0' else observed[name]) for name in contract()['system_packages']}
    if required!=expected:raise ValueError('Required system versions differ from verified seed')
    wanted={(p['package'],p['version'],p['architecture']) for p in packages}
    signed={}
    for index in indexes:
        url=index['origin']
        if not isinstance(url,str) or not url.startswith(('https://archive.ubuntu.com/ubuntu/','https://security.ubuntu.com/ubuntu/','https://snapshot.ubuntu.com/ubuntu/')):
            raise ValueError('Official Ubuntu metadata origin required')
        release=directory/safe_name(index['inrelease']);path=directory/safe_name(index['file'])
        if release.is_symlink() or path.is_symlink() or release.stat().st_size>1024**2 or path.stat().st_size>64*1024**2:
            raise ValueError('Bounded ordinary package metadata required')
        if verify_signature:
            result=runner(['gpgv','--keyring',keyring,str(release)],timeout=30,stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL)
            if result.returncode:raise ValueError('Ubuntu Release signature invalid')
        text=release.read_text();body=text.split('\n\n',1)[1].split('-----BEGIN PGP SIGNATURE-----',1)[0]
        hashes={};active=False
        for line in body.splitlines():
            if line=='SHA256:':active=True;continue
            if active and not line.startswith(' '):active=False
            if active:
                parts=line.split()
                if len(parts)==3:hashes[parts[2]]={'sha256':parts[0],'size':int(parts[1])}
        ref=file_ref(path)
        if hashes.get(index['release_name'])!=ref.to_dict():raise ValueError('Signed index hash/size differs')
        opener=gzip.open if path.suffix=='.gz' else open
        total=0;fields={}
        with opener(path,'rt',encoding='utf-8') as source:
            for line in source:
                total+=len(line.encode())
                if total>64*1024**2 or len(line)>65536:raise ValueError('Package index expansion bound')
                if line.strip()=='':
                    if (fields.get('Package'),fields.get('Version'),fields.get('Architecture')) in wanted:
                        signed[(fields['Package'],fields['Version'],fields['Architecture'])]=dict(fields)
                    fields={}
                elif not line.startswith(' ') and ': ' in line:
                    key,value=line.rstrip('\n').split(': ',1);fields[key]=value
            if (fields.get('Package'),fields.get('Version'),fields.get('Architecture')) in wanted:
                signed[(fields['Package'],fields['Version'],fields['Architecture'])]=dict(fields)
    total=installed=0;names=set()
    for item in packages:
        name=safe_name(item['file']);path=directory/name
        if not name.endswith('.deb') or name in names or path.is_symlink():raise ValueError('Ordinary unique .deb required')
        names.add(name);ref=file_ref(path);total+=ref.size
        record=signed.get((item['package'],item['version'],item['architecture']))
        if (not record or ref.to_dict()!={'sha256':item['sha256'],'size':item['size']}
            or record.get('SHA256')!=ref.sha256 or int(record.get('Size','-1'))!=ref.size
            or record.get('Filename')!=item['ubuntu_filename']):raise ValueError('Package does not match signed index')
        installed+=int(record.get('Installed-Size','0'))*1024
        if inspect_debs:
            fields=runner(['dpkg-deb','--field',str(path),'Package','Version','Architecture'],capture_output=True,timeout=10)
            if fields.returncode or len(fields.stdout)>4096:raise ValueError('Debian package inspection failed')
            actual=dict(line.split(': ',1) for line in fields.stdout.decode().splitlines() if ': ' in line)
            if actual!={'Package':item['package'],'Version':item['version'],'Architecture':item['architecture']}:
                raise ValueError('Debian package identity differs')
    if total>BOUNDS['debs'] or installed>4*GIB:raise ValueError('Package closure byte ceiling')
    if {p.relative_to(directory).as_posix() for p in directory.rglob('*.deb')}!=names:raise ValueError('Unlisted package')
    versions={item['package']:item['version'] for item in packages}
    if any(versions.get(name,base.get(name))!=version for name,version in required.items()):raise ValueError('Required package absent from closure/base')
    return lock,installed


def installed_versions(versions,runner):
    for name,expected in versions.items():
        if not re.fullmatch(r'[a-z0-9][a-z0-9+.-]*(?::amd64)?',name):raise ValueError('Package name invalid')
        value=runner(['dpkg-query','-W','-f=${Version}\t${Status}',name],capture_output=True,timeout=10)
        if value.returncode or value.stdout.decode()!=expected+'\tinstall ok installed':raise ValueError('Active offline closure/version differs')


def bootstrap(bundle,*,volume=Path('/workspace'),root=Path('/'),scratch=Path('/tmp/coppertop-restore'),
              runner=subprocess.run,admit=None,max_seconds=6300,root_bytes=None):
    """Archive stays on volume. Extract/install/restore on container rootfs only."""
    from .runpod_runtime import cpu_admission
    admit=admit or cpu_admission
    if not 0<max_seconds<=6300:raise ValueError('CPU bootstrap deadline')
    root=Path(root);volume=Path(volume);scratch=Path(scratch);began=time.monotonic()
    descriptor=read_json(Path(bundle)/'bundle.json',65536)
    marker=Path(bundle)/'cpu-receipt.json'
    if os.environ.get('COPPERTOP_BOOTSTRAP_MODE')=='rollout':
        from .runpod_cloud import identifier
        marker=volume/'coppertop/runs'/identifier(os.environ['COPPERTOP_RUN_ID'])/'bootstrap.json'
    if marker.exists() or scratch.exists():raise ValueError('CPU bootstrap is single use; failed source preserved')
    with hard_deadline(max_seconds):
        if root_bytes is None:
            value=runner(['du','-sx','--block-size=1',str(root)],capture_output=True,timeout=120)
            if value.returncode or len(value.stdout)>1024:raise ValueError('Rootfs occupancy unavailable')
            root_bytes=int(value.stdout.split()[0])
        manifest=read_json(Path(bundle)/'manifest.json',BOUNDS['manifest'])
        # Conservative peak includes old public-base trees, staged .debs and
        # all extracted files. Reject before writing, even if replacing frees space.
        if root_bytes+manifest['bytes']+4*GIB>BOUNDS['rootfs']:raise ValueError('Container rootfs peak exceeds 70 GiB')
        value=restore(bundle,scratch,volume=volume,max_seconds=max_seconds-120)
        lock,_=verify_packages(scratch/'debs',runner=runner)
        installed_versions(lock['base'],runner)
        packages=[str(scratch/'debs'/p['file']) for p in lock['packages']]
        result=runner(['dpkg','--install',*packages],stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL,timeout=300)
        if result.returncode:raise ValueError('Offline dependency closure install failed')
        audit=runner(['dpkg','--audit'],capture_output=True,timeout=30)
        if audit.returncode or audit.stdout or audit.stderr:raise ValueError('Offline dpkg audit failed')
        installed_versions({**lock['base'],**{p['package']:p['version'] for p in lock['packages']},**lock['required']},runner)
        for prefix in [*contract()['required_trees'],'app']:
            destination=root/prefix;source=scratch/prefix
            destination.parent.mkdir(parents=True,exist_ok=True)
            if destination.is_symlink():destination.unlink()
            elif destination.exists():shutil.rmtree(destination)
            source.rename(destination)
        for path,target in (('opt/model','/work/model'),('opt/evaluator.py','/work/xiaomi/eval_robocasa365/entry.py')):
            alias=root/path
            if alias.exists() or alias.is_symlink():raise ValueError('Required absolute alias collision')
            alias.symlink_to(target)
        shutil.copyfile(root/'app/containers/gpu-smoke-pins.json',root/'opt/gpu-smoke-pins.json')
        (root/'opt/runpod-export.sha256').write_text(descriptor['manifest_sha256'])
        # Fresh interpreter after replacing /opt/conda; never claim this
        # bootstrap process's already loaded modules prove the restored runtime.
        if admit is cpu_admission:
            env=dict(os.environ,PYTHONPATH='/app',PATH='/opt/conda/bin:/usr/sbin:/usr/bin:/sbin:/bin',
                PYTHONDONTWRITEBYTECODE='1',HF_HUB_OFFLINE='1',TRANSFORMERS_OFFLINE='1')
            receipt_path=root/'opt/runpod-cpu-admission.json'
            result=runner(['/opt/conda/bin/python','-m','coppertop.runpod_runtime','--cpu-admission','--output',str(receipt_path)],
                env=env,stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL,timeout=min(610,max_seconds-(time.monotonic()-began)))
            if result.returncode:raise ValueError('Restored CPU admission failed')
            receipt=read_json(receipt_path,65536)
        else:receipt=admit()
        if receipt.get('status')!='passed' or receipt.get('system_libraries_loaded') is not True:
            raise ValueError('Actual library/import admission required')
        if receipt.get('cuda_executed') is not False or receipt.get('render_reset_executed') is not False:
            raise ValueError('CPU phase cannot attest GPU execution')
        save(root/'opt/runpod-cpu-admission.json',receipt)
        result={'status':'passed','manifest_sha256':descriptor['manifest_sha256'],'tar_sha256':descriptor['tar_sha256'],
            'cpu_admission':receipt,'package_lock_sha256':digest(canonical(lock)),
            'absolute_paths_preserved':True,'extracted_on_network_volume':False,'elapsed_seconds':time.monotonic()-began,
            'plan_sha256':os.environ.get('COPPERTOP_PLAN_SHA256'),'pod_id':os.environ.get('RUNPOD_POD_ID')}
        save(marker,result)
    return result


def main():
    parser=argparse.ArgumentParser(description=__doc__);parser.add_argument('--bundle',type=Path)
    parser.add_argument('--verify-packages',type=Path)
    args=parser.parse_args()
    if args.verify_packages:
        verify_packages(args.verify_packages);return
    if not args.bundle:parser.error('--bundle or --verify-packages required')
    bootstrap(args.bundle)


if __name__=='__main__':main()
