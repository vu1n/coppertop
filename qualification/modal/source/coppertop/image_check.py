"""Remote CPU image gate: imports/processor only, never instantiates model weights."""
import argparse
import hashlib
import importlib.metadata
import json
import os
import re
import subprocess
import sys
from pathlib import Path
from .chords import LocalStore, canonical
from .gpu_smoke import RELEASE,load_window,camera_history,messages
from .smoke_runtime import verify_snapshot,official_messages,processor_inputs,equal_inputs

NATIVE_PYTHON = '/opt/conda/bin/python'


def require_native_interpreter():
    # GPU reload invokes this executable; metadata from another environment
    # cannot prove that the checkpoint's runtime is aligned.
    try:
        same = os.path.samefile(sys.executable, NATIVE_PYTHON)
    except OSError:
        same = False
    prefix = Path('/opt/conda').resolve()
    if (not same or Path(sys.prefix).resolve() != prefix
        or Path(sys.exec_prefix).resolve() != prefix):
        raise ValueError('Build/gate interpreter differs from native GPU interpreter')


def pinned_dependencies(*paths):
    """Merge exact locks without allowing resolver options or conflicting pins."""
    pins, conflicts = {}, []
    for path in paths:
        with Path(path).open('rb') as source:
            raw = source.read(65537)
        if len(raw) > 65536:
            raise ValueError('Dependency lock exceeds bound')
        for line in raw.decode().splitlines():
            if not line or line.startswith('#'):
                continue
            match = re.fullmatch(r'([A-Za-z0-9][A-Za-z0-9_.-]*)==([0-9][A-Za-z0-9.+-]*)', line)
            if not match:
                raise ValueError('Exact distribution pins required')
            name = re.sub(r'[-_.]+', '-', match[1]).lower()
            version = match[2]
            if name in pins and pins[name] != version:
                conflicts.append({'package': name, 'versions': [pins[name], version]})
            pins[name] = version
    if conflicts:
        raise ValueError('Conflicting dependency pins: ' + canonical(conflicts).decode())
    if not pins or len(pins) > 256:
        raise ValueError('Bounded nonempty dependency lock required')
    return dict(sorted(pins.items()))


def dependency_report(expected, observed):
    differences = []
    for name, version in sorted(expected.items()):
        actual = observed.get(name)
        if name not in observed:
            status = 'unknown'
        elif actual is None:
            status = 'missing'
        elif actual != version:
            status = 'different'
        else:
            continue
        differences.append({'package': name, 'expected': version,
                            'observed': actual, 'status': status})
    return {'package_count': len(expected), 'matched_count': len(expected)-len(differences),
            'differences': differences}


def installed_dependencies(expected):
    versions = {}
    for name in expected:
        try:
            versions[name] = importlib.metadata.version(name)
        except importlib.metadata.PackageNotFoundError:
            versions[name] = None
    return versions


def require_dependencies(expected):
    report = dependency_report(expected, installed_dependencies(expected))
    if report['differences']:
        raise ValueError('Pinned dependencies differ: ' + canonical(report).decode())
    return report


def recipe_digest(root):
    names = ['containers/Dockerfile.gpu-smoke','containers/gpu-smoke-pins.json',
             'requirements-gpu-smoke.lock']
    names += sorted(str(p.relative_to(root)) for p in (root/'coppertop').glob('*.py'))
    digest = hashlib.sha256()
    for name in names:
        data=(root/name).read_bytes();digest.update(name.encode()+b'\0'+hashlib.sha256(data).digest())
    return digest.hexdigest()


def validate(args):
    from .dependency_gate import require_pip_check
    require_native_interpreter()
    if sys.version_info[:2] != (3,11):raise ValueError('Python 3.11 required')
    if os.environ.get('HF_HUB_OFFLINE') != '1' or os.environ.get('TRANSFORMERS_OFFLINE') != '1':
        raise ValueError('Offline native model inspection required')
    require_dependencies(pinned_dependencies(args.root/'requirements-gpu-smoke.lock'))
    require_pip_check()
    import torch,torchvision,flash_attn,transformers,av,pyarrow,numpy
    if torch.__version__.split('+')[0]!='2.8.0' or torchvision.__version__.split('+')[0]!='0.23.0':
        raise ValueError('Immutable CUDA base packages differ')
    if torch.version.cuda!='12.8' or not torch._C._GLIBCXX_USE_CXX11_ABI or flash_attn.__version__!='2.8.3':
        raise ValueError('Flash Attention wheel ABI differs')
    pins=json.loads(args.pins.read_text());verify_snapshot(args.model,pins['model'])
    config=transformers.AutoConfig.from_pretrained(args.model,trust_remote_code=True,local_files_only=True)
    from transformers.dynamic_module_utils import get_class_from_dynamic_module
    native=get_class_from_dynamic_module(config.auto_map['AutoModel'],args.model,local_files_only=True)
    if not callable(getattr(native,'dit_forward',None)):raise ValueError('Native differentiable method missing')
    processor=transformers.AutoProcessor.from_pretrained(args.model,trust_remote_code=True,local_files_only=True)
    store=LocalStore(args.store);windows=[]
    for cutoff in (6,345):
        sample,record=load_window(store,RELEASE,cutoff)
        images=camera_history(store,record,sample['history_indices'])
        original=official_messages(args.evaluator,pins['evaluator_git_blob'],images,sample['instruction'])
        left=processor_inputs(processor,original,sample)
        right=processor_inputs(processor,messages(images,sample['instruction']),sample)
        equal_inputs(left,right)
        windows.append({'cutoff':cutoff,'tensor_shapes':{k:list(v.shape) for k,v in left.items()}})
    for entrypoint in ('coppertop.smoke_runtime','coppertop.smoke_reload'):
        subprocess.run([sys.executable,'-m',entrypoint,'--help'],check=True,
                       stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL,timeout=10)
    return {'status':'passed','image_id':args.image_id,'recipe_sha256':recipe_digest(args.root),
        'snapshot_verified':True,'imports_verified':True,'processor_parity_verified':True,
        'entrypoint_verified':True,'windows':windows,'full_model_instantiated':False,'cuda_executed':False}


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    for name in ('root','store','model','pins','evaluator','output'):
        parser.add_argument('--'+name,type=Path,required=True)
    parser.add_argument('--image-id',required=True);args=parser.parse_args()
    result=validate(args)
    with args.output.open('x') as target:json.dump(result,target,indent=2);target.write('\n')

if __name__=='__main__':main()
