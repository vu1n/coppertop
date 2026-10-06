"""Bounded local view check. No provider API, tokenizer/model load or secret access."""
import argparse
import hashlib
import json
import sys
from pathlib import Path
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from coppertop.chords import LocalStore
from coppertop.gpu_smoke import RELEASE,load_window,camera_history,messages,normalized_targets
from coppertop.smoke_runtime import official_messages


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--store',type=Path,required=True);parser.add_argument('--source',type=Path,required=True)
    parser.add_argument('--output',type=Path,required=True);args=parser.parse_args()
    args.output.mkdir(parents=True,exist_ok=False)
    pins=json.loads((Path(__file__).resolve().parents[1]/'containers/gpu-smoke-pins.json').read_text())
    config_pin=next(x for x in pins['model']['files'] if x['name']=='preprocessor_config.json')
    with (args.source/'training-data/sources/native-preprocessor_config.json').open('rb') as source:
        config_bytes=source.read(config_pin['bytes']+1)
    if len(config_bytes)!=config_pin['bytes'] or hashlib.sha1(b'blob '+str(len(config_bytes)).encode()+b'\0'+config_bytes).hexdigest()!=config_pin['git_blob']:
        raise ValueError('Cached normalization differs from native checkpoint')
    config=json.loads(config_bytes)['action_config']['robocasa365']
    evaluator=args.source/'modal-xr1/sources/xiaomi-eval_robocasa365_entry.py'
    import numpy as np
    reports=[];store=LocalStore(args.store)
    for cutoff in (6,345):
        sample,record=load_window(store,RELEASE,cutoff);images=camera_history(store,record,sample['history_indices'])
        targets,mask=normalized_targets(sample,config)
        ours=messages(images,sample['instruction']);native=official_messages(evaluator,pins['evaluator_git_blob'],images,sample['instruction'])
        crop_hashes=[]
        for a,b in zip(ours,native):
            if a['role']!=b['role'] or len(a['content'])!=len(b['content']):raise ValueError('Message parity failed')
            for left,right in zip(a['content'],b['content']):
                if left['type']!=right['type']:raise ValueError('Message type parity failed')
                if left['type']=='text':
                    if left['text']!=right['text']:raise ValueError('Prompt parity failed')
                else:
                    if len(left['video'])!=len(right['video']):raise ValueError('Video count parity failed')
                    for x,y in zip(left['video'],right['video']):
                        if not np.array_equal(np.asarray(x),np.asarray(y)):raise ValueError('Crop parity failed')
                        crop_hashes.append(hashlib.sha256(np.asarray(x).tobytes()).hexdigest())
        report={**sample,'targets_normalized':targets,'loss_mask':mask,'crop_sha256':crop_hashes}
        (args.output/f'window-{cutoff}.json').write_text(json.dumps(report,indent=2)+'\n')
        reports.append({'cutoff':cutoff,'history_indices':sample['history_indices'],'observed_active_targets':sum(map(sum,mask)),
            'state_shape':[4,60],'action_shape':[16,60],'message_crop_parity':True,'camera_rgb_shapes':{k:list(v.shape) for k,v in images.items()}})
    result={'release':RELEASE,'windows':reports,'processor_token_tensor_parity_executed':False,'native_weights_loaded':False,'CUDA_executed':False}
    (args.output/'preparation.json').write_text(json.dumps(result,indent=2)+'\n');print(json.dumps(result))

if __name__=='__main__':main()
