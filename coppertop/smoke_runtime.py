"""Explicit portable CUDA job. No downloads, credentials, endpoints or provider APIs."""
import argparse
import ast
import hashlib
import json
import os
import signal
import time
from pathlib import Path
from .chords import LocalStore
from .gpu_smoke import (RELEASE, MODEL_REPO, MODEL_REVISION, HEAD_MODULES,
    load_window, camera_history, messages, normalized_targets, conditioning,
    velocity, flow_loss, trainable_head)

MAX_DELTA_BYTES = 2500 * 1024**2
MAX_VALIDATION_BYTES = 64 * 1024


def verify_snapshot(root, pins):
    if pins['repo'] != MODEL_REPO or pins['revision'] != MODEL_REVISION:
        raise ValueError('Native checkpoint revision required')
    for entry in pins['files']:
        name = entry['name']; candidate = root / name; path = candidate.resolve()
        if not path.is_relative_to(root.resolve()) or candidate.is_symlink() or path.stat().st_size != entry['bytes']:
            raise ValueError('Snapshot path/size mismatch')
        h = hashlib.sha256() if 'sha256' in entry else hashlib.sha1(b'blob ' + str(entry['bytes']).encode() + b'\0')
        with path.open('rb') as source:
            while chunk := source.read(1024**2): h.update(chunk)
        if h.hexdigest() != entry.get('sha256', entry.get('git_blob')):
            raise ValueError('Snapshot checksum mismatch: ' + name)


def official_messages(path, expected_blob, images, instruction):
    with path.open('rb') as source:
        data = source.read(32769)
    if len(data) > 32768 or hashlib.sha1(b'blob ' + str(len(data)).encode() + b'\0' + data).hexdigest() != expected_blob:
        raise ValueError('Evaluator differs from reviewed commit')
    tree = ast.parse(data)
    crop = next(n for n in tree.body if isinstance(n, ast.FunctionDef) and n.name == 'center_crop')
    client = next(n for n in tree.body if isinstance(n, ast.ClassDef) and n.name == 'EvalClient')
    build = next(n for n in client.body if isinstance(n, ast.FunctionDef) and n.name == '_build_messages')
    # Only the pinned crop/message methods execute; no transport or simulator.
    crop.returns = None;build.returns = None
    for method in (crop, build):
        for arg in method.args.args: arg.annotation = None
    import numpy as np
    from PIL import Image
    from types import SimpleNamespace
    from .episode import CAMERAS
    namespace = {'np': np, 'Image': Image, 'CAMERA_KEYS': CAMERAS}
    code = ast.fix_missing_locations(ast.Module(body=[crop, build], type_ignores=[]))
    exec(compile(code, str(path), 'exec'), namespace)
    return namespace['_build_messages'](SimpleNamespace(crop_ratio=.95), images, instruction)


def processor_inputs(processor, chat, sample):
    import numpy as np
    # This deliberately matches the native evaluator, including its default video
    # sampling/timestamp policy. Recorded source timing remains in the sidecar.
    return processor.apply_chat_template(chat, tokenize=True, return_dict=True,
        return_tensors='pt', do_resize=False,
        state=np.asarray([sample['state']], dtype=np.float32), robot_type='robocasa365')


def equal_inputs(left, right):
    import torch
    if set(left) != set(right): raise ValueError('Processor keys differ')
    for key in left:
        if not isinstance(left[key], torch.Tensor) or left[key].dtype != right[key].dtype or not torch.equal(left[key], right[key]):
            raise ValueError('Processor tensor parity failed: ' + key)
    for key in ('pixel_values_videos', 'video_grid_thw', 'input_ids', 'attention_mask', 'state', 'action_mask'):
        if key not in left: raise ValueError('Native processor field absent: ' + key)
    if tuple(left['state'].shape) != (1, 4, 60) or tuple(left['action_mask'].shape) != (1, 16, 60):
        raise ValueError('Native tensor geometry differs')


def euler(model, inputs, context):
    import torch
    x = torch.randn_like(inputs['action_mask'])
    for step in range(5):
        t = torch.ones((1, 1, 1), device=x.device, dtype=x.dtype) * step / 5
        x = x + velocity(model, inputs, x, t, context) * (1. / 5)
    return x


def run(args):
    output = args.output.resolve();output.mkdir(parents=True, exist_ok=False)
    began = time.monotonic();record = {'status': 'started', 'kind': 'native_dit_head_smoke',
        'release': RELEASE, 'model_repo': MODEL_REPO, 'model_revision': MODEL_REVISION,
        'process_pid': os.getpid(),
        'steps': 1, 'batch_size': 1, 'vlm_frozen': True, 'auxiliary_choice_score_losses': False,
        'elapsed_scope': 'GPU Python smoke; excludes CPU preparation and artifact transfer',
        'save_reload_scope': 'same process and model instance; zero head, reload, compare velocity',
        'fresh_process_reload_verified': False,
        'checkpoint_scope': 'weights-only native head; no optimizer or RNG resume',
        'checkpoint_persistence': 'sandbox filesystem only; requires verified external retrieval'}
    def save():
        record['elapsed_seconds'] = time.monotonic() - began
        (output / 'result.json').write_text(json.dumps(record, sort_keys=True, indent=2) + '\n')
    def timeout(*_): raise TimeoutError('Bounded smoke deadline reached')
    signal.signal(signal.SIGALRM, timeout);signal.alarm(args.max_seconds)
    try:
        if not args.execute_cuda:
            raise ValueError('Use --execute-cuda only inside the approved disposable GPU job')
        import torch
        if not torch.cuda.is_available() or torch.cuda.device_count() != 1:
            raise ValueError('Exactly one CUDA GPU required; no CPU/MPS fallback')
        if torch.__version__.split('+')[0] != '2.8.0': raise ValueError('Reviewed torch 2.8.0 required')
        import transformers
        if transformers.__version__ != '4.57.1': raise ValueError('Reviewed transformers 4.57.1 required')
        pins = json.loads(args.pins.read_text());verify_snapshot(args.model, pins['model'])
        torch.cuda.reset_peak_memory_stats()
        processor = transformers.AutoProcessor.from_pretrained(args.model, local_files_only=True, trust_remote_code=True)
        store = LocalStore(args.store);samples=[];prepared=[]
        for cutoff in (6, 345):
            sample, episode = load_window(store, RELEASE, cutoff)
            images = camera_history(store, episode, sample['history_indices'])
            native_chat = official_messages(args.evaluator, pins['evaluator_git_blob'], images, sample['instruction'])
            native = processor_inputs(processor, native_chat, sample)
            bridge = processor_inputs(processor, messages(images, sample['instruction']), sample)
            equal_inputs(native, bridge);samples.append(sample);prepared.append(bridge)
        record['processor_parity'] = [{'cutoff': s['cutoff'], 'frame_id': s['frame_id'],
            'history_indices': s['history_indices'], 'history_timestamps': s['history_timestamps'],
            'tensor_shapes': {k: list(v.shape) for k, v in batch.items()},
            'video_grid_thw': batch['video_grid_thw'].tolist()} for s, batch in zip(samples, prepared)]
        save();start = time.monotonic()
        model, info = transformers.AutoModel.from_pretrained(args.model, local_files_only=True,
            trust_remote_code=True, attn_implementation='flash_attention_2', dtype=torch.bfloat16,
            low_cpu_mem_usage=True, output_loading_info=True)
        if any(info.get(k) for k in ('missing_keys', 'unexpected_keys', 'mismatched_keys', 'error_msgs')):
            raise ValueError('Native weight mapping incomplete')
        model=model.cuda().eval();torch.cuda.synchronize();record['model_load_seconds']=time.monotonic()-start
        inputs={k:v.to(device='cuda',dtype=model.dtype) if v.is_floating_point() else v.to('cuda')
                for k,v in prepared[0].items()}
        torch.manual_seed(7);torch.cuda.manual_seed_all(7)
        cpu_rng=torch.get_rng_state();gpu_rng=torch.cuda.get_rng_state_all()
        with torch.no_grad():native=model(**inputs).actions
        context=conditioning(model, inputs)
        torch.set_rng_state(cpu_rng);torch.cuda.set_rng_state_all(gpu_rng)
        with torch.no_grad():reference=euler(model, inputs, context)
        delta=(native.float()-reference.float()).abs().max().item()
        record['native_euler_max_abs_difference']=delta
        if not torch.equal(native, reference):raise ValueError('Native inference bridge is not bit-identical')
        parameters=trainable_head(model)
        record['trainable_parameters']=sum(p.numel() for p in parameters)
        config=processor.action_config['robocasa365']
        targets, mask=normalized_targets(samples[0], {k:v[0].tolist() for k,v in config.items()})
        action=torch.tensor([targets],device='cuda',dtype=torch.bfloat16)
        observed=torch.tensor([mask],device='cuda',dtype=torch.bool)
        noise=torch.randn_like(action);t=torch.full((1,1,1),.5,device='cuda',dtype=action.dtype)
        noisy=(1-t)*noise+t*action;target=action-noise
        before=[p.detach().flatten()[:4096].cpu().clone() for p in parameters]
        start=time.monotonic();prediction=velocity(model,inputs,noisy,t,context)
        loss=flow_loss(prediction,target,observed)
        if not torch.isfinite(loss):raise ValueError('Non-finite loss')
        loss.backward()
        gradients=[p.grad for p in parameters if p.grad is not None]
        if not gradients or not all(torch.isfinite(g).all() for g in gradients) or not any(g.abs().max()>0 for g in gradients):
            raise ValueError('No finite nonzero native-head gradient')
        if any(p.grad is not None for p in model.vlm.parameters()):raise ValueError('Frozen VLM received gradients')
        optimizer=torch.optim.SGD(parameters,lr=.01);optimizer.step();optimizer.zero_grad(set_to_none=True)
        if not any(not torch.equal(old,p.detach().flatten()[:4096].cpu()) for old,p in zip(before,parameters)):
            raise ValueError('No measured parameter update')
        torch.cuda.synchronize();record['update_seconds']=time.monotonic()-start;record['loss']=loss.item()
        with torch.no_grad():trained=velocity(model,inputs,noisy,t,context).detach()
        if not torch.isfinite(trained).all():raise ValueError('Update produced non-finite velocity')
        state={k:v.detach().cpu().contiguous() for k,v in model.state_dict().items() if k.split('.')[0] in HEAD_MODULES}
        if sum(v.numel()*v.element_size() for v in state.values())>MAX_DELTA_BYTES:raise ValueError('Head delta exceeds 2.5 GiB cap')
        from safetensors.torch import save_file,load_file
        checkpoint=output/'head.safetensors';save_file(state,str(checkpoint));del state
        if checkpoint.stat().st_size > MAX_DELTA_BYTES:raise ValueError('Serialized head exceeds artifact cap')
        digest=hashlib.sha256()
        with checkpoint.open('rb') as saved:
            while chunk:=saved.read(1024**2):digest.update(chunk)
        record['checkpoint_sha256']=digest.hexdigest()
        # A bounded numerical case permits verification in an independent process.
        validation=output/'validation.safetensors'
        case={'noisy':noisy.detach().cpu().contiguous(), 't':t.detach().cpu().contiguous(),
            'expected_velocity':trained.detach().cpu().contiguous()}
        if sum(v.numel()*v.element_size() for v in case.values())>MAX_VALIDATION_BYTES:
            raise ValueError('Validation tensors exceed 64 KiB cap')
        save_file(case,str(validation));del case
        if validation.stat().st_size>MAX_VALIDATION_BYTES:
            raise ValueError('Serialized validation exceeds 64 KiB cap')
        record['validation_bytes']=validation.stat().st_size
        record['validation_sha256']=hashlib.sha256(validation.read_bytes()).hexdigest()
        with torch.no_grad():
            for p in parameters:p.zero_()
        restored=load_file(str(checkpoint),device='cpu');result=model.load_state_dict(restored,strict=False);del restored
        if result.unexpected_keys or any(not k.startswith('vlm.') for k in result.missing_keys):
            raise ValueError('Reload mapping differs from native-head delta')
        with torch.no_grad():reloaded=velocity(model,inputs,noisy,t,context)
        if not torch.equal(trained,reloaded):raise ValueError('Saved head reload changed prediction')
        record.update(status='passed',save_reload_exact=True,checkpoint_bytes=checkpoint.stat().st_size,
            max_vram_allocated_bytes=torch.cuda.max_memory_allocated(),
            max_vram_reserved_bytes=torch.cuda.max_memory_reserved(),gpu=torch.cuda.get_device_name())
        (output/'head-manifest.json').write_text(json.dumps({'schema_version':1,'kind':'native_head_delta',
            'base_model':MODEL_REPO,'revision':MODEL_REVISION,'release':RELEASE,'modules':list(HEAD_MODULES),
            'checkpoint_sha256':record['checkpoint_sha256'],'checkpoint_bytes':checkpoint.stat().st_size,
            'validation_sha256':record['validation_sha256'],'validation_bytes':record['validation_bytes'],
            'checkpoint_file':'head.safetensors','validation_file':'validation.safetensors',
            'checkpoint_scope':'weights-only native head; no optimizer or RNG resume',
            'validation_cutoff':6,'validation_tensors':['noisy','t','expected_velocity'],
            'optimizer':'SGD','learning_rate':.01,'momentum':0,'step':1,
            'loss':'observed-active masked flow velocity MSE; not full published XR1 objective'},indent=2)+'\n')
    except Exception as error:
        record['status']='failed';record['error_type']=type(error).__name__
        # Local path/provider/SDK details and credentials never enter the report.
        save();raise
    finally:
        signal.alarm(0);save()
    return record


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    for name in ('store','model','pins','evaluator','output'):parser.add_argument('--'+name,type=Path,required=True)
    parser.add_argument('--execute-cuda',action='store_true');parser.add_argument('--max-seconds',type=int,default=600)
    args=parser.parse_args()
    if not 1<=args.max_seconds<=600:parser.error('max-seconds must be within 1..600')
    run(args)

if __name__=='__main__':main()
