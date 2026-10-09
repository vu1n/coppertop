"""Offline export inventory and CPU admission; never acquire a model or environment."""
import argparse
import os
from pathlib import Path, PurePosixPath
import stat
import subprocess

from .chords import canonical, digest
from .reload_launch import hard_deadline, read_json, save

ROOT = Path(__file__).resolve().parents[1]


def contract():
    return read_json(ROOT/'containers/runpod-runtime.json')


def _entries(directory):
    """Hash the actual complete export, including Git metadata and link targets."""
    directory = Path(directory)
    spec = contract()
    roots=[*spec['required_trees'],'debs']
    if directory.is_symlink(): raise ValueError('Runtime root symlink rejected')
    for path in directory.rglob('*'):
        name=path.relative_to(directory).as_posix()
        if not any(name==root or name.startswith(root+'/') or root.startswith(name+'/') for root in roots):
            raise ValueError('Unexpected runtime path outside exported prefixes')
        if path.is_symlink() and any(root.startswith(name+'/') for root in roots):
            raise ValueError('Runtime parent directory symlink rejected')
    entries = {}
    total = 0
    for tree in [*spec['required_trees'], 'debs']:
        base = directory/tree
        if not base.is_dir() or base.is_symlink():
            raise ValueError('Complete runtime tree missing: '+tree)
        for path in sorted(base.rglob('*')):
            name = path.relative_to(directory).as_posix()
            metadata = path.lstat()
            mode = stat.S_IMODE(metadata.st_mode)
            if stat.S_ISLNK(metadata.st_mode):
                target = os.readlink(path)
                # Export links retain container absolute paths, never host paths.
                logical = PurePosixPath(target) if target.startswith('/') else PurePosixPath('/'+name).parent/target
                normalized = os.path.normpath(str(logical)).lstrip('/')
                if not any(normalized == root or normalized.startswith(root+'/') for root in spec['required_trees']):
                    raise ValueError('Runtime link escapes exported prefixes')
                entries[name] = {'link': target, 'mode': mode}
            elif stat.S_ISREG(metadata.st_mode):
                from .streams import file_ref
                ref = file_ref(path)
                total += ref.size
                if total > spec['rootfs_bytes_ceiling']:
                    raise ValueError('Runtime export exceeds 70 GiB ceiling')
                entries[name] = dict(ref.to_dict(), mode=mode)
            elif not stat.S_ISDIR(metadata.st_mode):
                raise ValueError('Runtime export contains a special file')
            if len(entries) > 500000:
                raise ValueError('Runtime file inventory exceeds bound')
    return entries, total


def inventory(directory, *, verify=None):
    """Caller exports the approved seed first; this only reads local artifact bytes."""
    entries, total = _entries(directory)
    spec = contract()
    if any(name not in entries for name in spec['required_files']):
        raise ValueError('Required executable/source/asset receipt missing')
    if not any(name.endswith('.deb') for name in entries if name.startswith('debs/')):
        raise ValueError('Offline system dependency closure required')
    result = {'schema_version':1, 'contract_sha256':digest(canonical(spec)),
              'entries':entries, 'bytes':total, 'expected_seed':spec['validated_seed']}
    if len(canonical(result)) > spec['export_manifest_bytes_ceiling']:
        raise ValueError('Runtime export manifest exceeds bound')
    if verify is not None and result != verify:
        raise ValueError('Runtime export bytes/membership/modes changed')
    return result


def cpu_admission():
    """Actual interpreter/import/checksum checks, no CUDA or simulator reset claim."""
    from tools.run_cpu_image_repair import native_gate
    from .image_check import pinned_dependencies
    from .performance_cloud import simulator_preflight
    from .performance_runtime import source_gate, MODEL
    from .performance_eval import SOURCE_PINS
    from .smoke_runtime import verify_snapshot
    with hard_deadline(610):
        native = native_gate({'native_pins':pinned_dependencies(
            ROOT/'requirements-gpu-smoke.lock', ROOT/'requirements-r2.txt')})
        for name, expected in SOURCE_PINS.items():
            source = Path('/work')/name
            revision = subprocess.check_output(['git','-C',str(source),'rev-parse','HEAD'],timeout=120).decode().strip()
            dirty = subprocess.check_output(['git','-C',str(source),'status','--porcelain','--untracked-files=no'],timeout=120)
            if revision != expected or dirty:
                raise ValueError('Full pinned source checkout changed')
        source_gate()
        verify_snapshot(MODEL,read_json(ROOT/'containers/gpu-smoke-pins.json')['model'])
        simulator = simulator_preflight()
        if simulator['status'] != 'passed':
            raise ValueError('Simulator admission failed')
        # Check installed system libraries rather than merely a recipe list.
        import ctypes
        for name in ('libEGL.so.1','libOpenGL.so.0','libGL.so.1','libGLESv2.so.2','libGLdispatch.so.0','libglib-2.0.so.0'):
            ctypes.CDLL(name)
        for package in contract()['system_packages']:
            status=subprocess.check_output(['dpkg-query','-W','-f=${Status}',package],timeout=5).decode()
            if status!='install ok installed': raise ValueError('System package admission failed')
        subprocess.run(['ffmpeg','-version'],check=True,timeout=10,stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL)
    from .official_recording_worker import plan as worker_plan
    return {'status':'passed','native':native,'simulator':simulator,
            'runtime_export_sha256':(Path('/opt/runpod-export.sha256').read_text().strip()),
            'worker_sha256':digest(canonical(worker_plan(full=True))),
            'model_checksums_verified':True,'full_sources_verified':True,
            'system_libraries_loaded':True,'cuda_executed':False,'render_reset_executed':False}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--inventory',type=Path)
    parser.add_argument('--verify',type=Path)
    parser.add_argument('--cpu-admission',action='store_true')
    parser.add_argument('--output',type=Path,required=True)
    args = parser.parse_args()
    if args.cpu_admission:
        result = cpu_admission()
    elif args.inventory:
        result = inventory(args.inventory,verify=read_json(args.verify,64*1024**2) if args.verify else None)
    else:
        parser.error('Choose --inventory or --cpu-admission')
    save(args.output,result)


if __name__ == '__main__': main()
