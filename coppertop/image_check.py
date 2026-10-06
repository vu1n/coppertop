"""Remote CPU image gate: imports/processor only, never instantiates model weights."""
import argparse
import hashlib
import importlib.metadata
import json
import os
import subprocess
import sys
from pathlib import Path
from .chords import LocalStore
from .gpu_smoke import RELEASE,load_window,camera_history,messages
from .smoke_runtime import verify_snapshot,official_messages,processor_inputs,equal_inputs


def recipe_digest(root):
    names = ['containers/Dockerfile.gpu-smoke','containers/gpu-smoke-pins.json',
             'requirements-gpu-smoke.lock']
    names += sorted(str(p.relative_to(root)) for p in (root/'coppertop').glob('*.py'))
    digest = hashlib.sha256()
    for name in names:
        data=(root/name).read_bytes();digest.update(name.encode()+b'\0'+hashlib.sha256(data).digest())
    return digest.hexdigest()


def validate(args):
    if sys.version_info[:2] != (3,11):raise ValueError('Python 3.11 required')
    if os.environ.get('HF_HUB_OFFLINE') != '1' or os.environ.get('TRANSFORMERS_OFFLINE') != '1':
        raise ValueError('Offline native model inspection required')
    expected={}
    for line in (args.root/'requirements-gpu-smoke.lock').read_text().splitlines():
        if line and not line.startswith('#'):
            name,version=line.split('==');expected[name]=version
    for name,version in expected.items():
        if importlib.metadata.version(name)!=version:raise ValueError('Pinned dependency differs: '+name)
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
