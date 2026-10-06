"""Bounded, offline fresh-process verification of a weights-only native head."""
import argparse
import hashlib
import json
import os
import re
import signal
import time
from pathlib import Path
from .chords import LocalStore
from .gpu_smoke import (RELEASE, MODEL_REPO, MODEL_REVISION, HEAD_MODULES,
    load_window, camera_history, messages, conditioning, velocity)
from .smoke_runtime import (MAX_DELTA_BYTES, MAX_VALIDATION_BYTES, verify_snapshot, official_messages,
    processor_inputs, equal_inputs)



def verify_artifact(path, expected_sha256, expected_bytes, max_bytes):
    """Check bounded bytes before any safetensors parser or model code runs."""
    path = Path(path)
    if not re.fullmatch(r'[0-9a-f]{64}', expected_sha256):
        raise ValueError('Expected artifact SHA256 required')
    if type(expected_bytes) is not int or not 0 < expected_bytes <= max_bytes:
        raise ValueError('Artifact size exceeds bound')
    if path.is_symlink() or not path.is_file() or path.stat().st_size != expected_bytes:
        raise ValueError('Artifact path/size mismatch')
    digest = hashlib.sha256()
    size = 0
    with path.open('rb') as source:
        while chunk := source.read(1024**2):
            size += len(chunk)
            if size > expected_bytes:
                raise ValueError('Artifact grew beyond expected size')
            digest.update(chunk)
    if size != expected_bytes or digest.hexdigest() != expected_sha256:
        raise ValueError('Artifact checksum mismatch')
    return {'sha256': digest.hexdigest(), 'bytes': size}


def load_verified_artifacts(head, validation, checkpoint_sha256, validation_sha256,
                            checkpoint_bytes, validation_bytes):
    # Both files must pass integrity checks before either is parsed.
    verify_artifact(head, checkpoint_sha256, checkpoint_bytes, MAX_DELTA_BYTES)
    verify_artifact(validation, validation_sha256, validation_bytes, MAX_VALIDATION_BYTES)
    from safetensors.torch import load_file
    return load_file(str(head), device='cpu'), load_file(str(validation), device='cpu')


def compare_velocity(expected, actual):
    import torch
    if expected.dtype != actual.dtype or expected.shape != actual.shape:
        raise ValueError('Velocity dtype/shape differs')
    if not torch.isfinite(expected).all() or not torch.isfinite(actual).all():
        raise ValueError('Velocity is not finite')
    if not torch.equal(expected, actual):
        raise ValueError('Fresh head reload changed velocity')
    return {'velocity_exact': True, 'velocity_max_abs_difference':
            (expected.float() - actual.float()).abs().max().item(),
            'velocity_dtype': str(actual.dtype), 'velocity_shape': list(actual.shape)}


def validate_case(case):
    import torch
    if set(case) != {'noisy', 't', 'expected_velocity'}:
        raise ValueError('Validation case keys differ')
    for name, shape in (('noisy', (1, 16, 60)), ('t', (1, 1, 1)),
                        ('expected_velocity', (1, 16, 60))):
        tensor = case[name]
        if tensor.dtype != torch.bfloat16 or tuple(tensor.shape) != shape or not torch.isfinite(tensor).all():
            raise ValueError('Validation case tensor differs: ' + name)
    if not torch.equal(case['t'], torch.full_like(case['t'], .5)):
        raise ValueError('Validation timestep differs')


def load_head(model, state):
    expected = {k: v for k, v in model.state_dict().items() if k.split('.')[0] in HEAD_MODULES}
    if set(state) != set(expected):
        raise ValueError('Head checkpoint keys differ')
    if any(v.dtype != expected[k].dtype or v.shape != expected[k].shape for k, v in state.items()):
        raise ValueError('Head checkpoint dtype/shape differs')
    result = model.load_state_dict(state, strict=False)
    if result.unexpected_keys or set(result.missing_keys) != set(model.state_dict()) - set(expected):
        raise ValueError('Head checkpoint mapping differs')


def run(args):
    if not 1 <= args.max_seconds <= 120:
        raise ValueError('Fresh process deadline must be within 1..120 seconds')
    output = args.output.resolve()
    output.mkdir(parents=True, exist_ok=False)
    began = time.monotonic()
    record = {'status': 'started', 'kind': 'native_head_fresh_process_reload',
        'process_pid': os.getpid(), 'release': RELEASE, 'cutoff': 6,
        'model_repo': MODEL_REPO, 'model_revision': MODEL_REVISION,
        'checkpoint_scope': 'weights-only native head; no optimizer or RNG resume',
        'fresh_process_reload_verified': False,
        'checkpoint_sha256': args.checkpoint_sha256, 'checkpoint_bytes': args.checkpoint_bytes,
        'validation_sha256': args.validation_sha256, 'validation_bytes': args.validation_bytes}
    def save():
        record['elapsed_seconds'] = time.monotonic() - began
        (output / 'result.json').write_text(json.dumps(record, sort_keys=True, indent=2) + '\n')
    def timeout(*_):
        raise TimeoutError('Bounded fresh reload deadline reached')
    signal.signal(signal.SIGALRM, timeout)
    signal.alarm(args.max_seconds)
    try:
        if not args.execute_cuda:
            raise ValueError('Use --execute-cuda only inside the approved disposable GPU job')
        # Integrity is independent of CUDA availability and precedes safetensors load.
        state, case = load_verified_artifacts(args.head, args.validation,
            args.checkpoint_sha256, args.validation_sha256, args.checkpoint_bytes, args.validation_bytes)
        import torch
        import transformers
        validate_case(case)
        if not torch.cuda.is_available() or torch.cuda.device_count() != 1:
            raise ValueError('Exactly one CUDA GPU required; no CPU/MPS fallback')
        if torch.__version__.split('+')[0] != '2.8.0' or transformers.__version__ != '4.57.1':
            raise ValueError('Reviewed torch 2.8.0 and transformers 4.57.1 required')
        pins = json.loads(args.pins.read_text())
        verify_snapshot(args.model, pins['model'])
        processor = transformers.AutoProcessor.from_pretrained(args.model, local_files_only=True, trust_remote_code=True)
        store = LocalStore(args.store)
        sample, episode = load_window(store, RELEASE, 6)
        images = camera_history(store, episode, sample['history_indices'])
        native_chat = official_messages(args.evaluator, pins['evaluator_git_blob'], images, sample['instruction'])
        native = processor_inputs(processor, native_chat, sample)
        prepared = processor_inputs(processor, messages(images, sample['instruction']), sample)
        equal_inputs(native, prepared)
        model, info = transformers.AutoModel.from_pretrained(args.model, local_files_only=True,
            trust_remote_code=True, attn_implementation='flash_attention_2', dtype=torch.bfloat16,
            low_cpu_mem_usage=True, output_loading_info=True)
        if any(info.get(k) for k in ('missing_keys', 'unexpected_keys', 'mismatched_keys', 'error_msgs')):
            raise ValueError('Native weight mapping incomplete')
        load_head(model, state)
        del state
        model = model.cuda().eval()
        inputs = {k: v.to(device='cuda', dtype=model.dtype) if v.is_floating_point() else v.to('cuda')
                  for k, v in prepared.items()}
        with torch.no_grad():
            context = conditioning(model, inputs)
            actual = velocity(model, inputs, case['noisy'].to('cuda'), case['t'].to('cuda'), context)
        torch.cuda.synchronize()
        record.update(compare_velocity(case['expected_velocity'], actual.detach().cpu()))
        record.update(status='passed', fresh_process_reload_verified=True)
    except Exception as error:
        record.update(status='failed', error_type=type(error).__name__)
        raise
    finally:
        signal.alarm(0)
        save()
    return record


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    for name in ('store', 'model', 'pins', 'evaluator', 'head', 'validation', 'output'):
        parser.add_argument('--' + name, type=Path, required=True)
    for name in ('checkpoint-sha256', 'validation-sha256'):
        parser.add_argument('--' + name, required=True)
    for name in ('checkpoint-bytes', 'validation-bytes'):
        parser.add_argument('--' + name, type=int, required=True)
    parser.add_argument('--max-seconds', type=int, default=120)
    parser.add_argument('--execute-cuda', action='store_true')
    args = parser.parse_args()
    if not 1 <= args.max_seconds <= 120:
        parser.error('max-seconds must be within 1..120')
    run(args)


if __name__ == '__main__':
    main()
