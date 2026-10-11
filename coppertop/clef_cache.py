"""Build a public-model runtime image; install/download work never runs on GPU."""
import hashlib
import json
import os
from pathlib import Path
import subprocess

from .colocation_worker import worker_environment

CACHE = Path('/opt/coppertop-clef')


def build():
    env = worker_environment(os.environ)
    env['COPPERTOP_CLEF_ROOT'] = str(CACHE)
    CACHE.mkdir(exist_ok=True)
    python = str(CACHE/'clef-env/bin/python')
    subprocess.run(['/opt/conda/bin/python','-m','venv',str(CACHE/'clef-env')],
                   env=env,check=True,timeout=30)
    subprocess.run([python,'-m','pip','install','--no-deps','--require-hashes',
        '--only-binary=:all:','--retries','0','--timeout','20','--no-cache-dir',
        '-r','/opt/clef-build/clef-runtime.lock'],env=env,check=True,timeout=600)
    subprocess.run([python,'-m','pip','check'],env=env,check=True,timeout=30)
    subprocess.run([python,'-c',
        'import torch,torchvision,idna,httpx; from huggingface_hub import snapshot_download; '
        'from transformers import AutoProcessor,Qwen3_5ForConditionalGeneration; '
        'from safetensors.torch import load_file'],env=env,check=True,timeout=45)
    script="""import hashlib,json
from pathlib import Path
from huggingface_hub import snapshot_download
root=Path('/opt/coppertop-clef')
meta=json.loads(Path('/opt/clef-build/model-metadata.json').read_text())
model=Path(snapshot_download(meta['repo'],revision=meta['revision'],local_dir=root/'clef-model',token=False,max_workers=4))
for item in meta['files']:
    file=model/item['path']
    if file.stat().st_size!=item['bytes']:raise ValueError('Pinned file size differs')
    if item['lfs']:
        h=hashlib.sha256()
        with file.open('rb') as source:
            for chunk in iter(lambda:source.read(1024**2),b''):h.update(chunk)
        if h.hexdigest()!=item['lfs']['sha256']:raise ValueError('Pinned file hash differs')
"""
    download_env=dict(env,HF_HUB_OFFLINE='0',TRANSFORMERS_OFFLINE='0',
                      HF_HUB_DISABLE_IMPLICIT_TOKEN='1',HF_HUB_DISABLE_TELEMETRY='1')
    subprocess.run([python,'-c',script],env=download_env,check=True,timeout=450)
    # No private episodes enter the reusable layer. Real inputs get their own CPU gate.
    check="""from coppertop.colocation_worker import load_reference_code
reference=load_reference_code('/opt/coppertop-clef/clef-model')
from transformers import AutoProcessor
from PIL import Image
encode_record=reference.encode_record
processor=AutoProcessor.from_pretrained('/opt/coppertop-clef/clef-model',local_files_only=True)
record={'state':'cache validation','images':[Image.new('RGB',(256,256)) for _ in range(3)],
 'questions':{'check':{'type':'choice','criteria':{'yes':'yes','no':'no'}}}}
encoded=encode_record(processor.tokenizer,record,processor=processor)
assert len(encoded.questions)==1
"""
    subprocess.run([python,'-c',check],env=env,check=True,timeout=45)
    packages=subprocess.check_output([python,'-m','pip','freeze'],env=env,text=True,timeout=20)
    metadata=json.loads(Path('/opt/clef-build/model-metadata.json').read_text())
    receipt={'status':'cpu_cache_validated','model_revision':metadata['revision'],
        'lock_sha256':hashlib.sha256(Path('/opt/clef-build/clef-runtime.lock').read_bytes()).hexdigest(),
        'packages':packages.splitlines(),'private_inputs_cached':False,'gpu_validation':False}
    (CACHE/'receipt.json').write_text(json.dumps(receipt,indent=2)+'\n')


if __name__=='__main__':build()
