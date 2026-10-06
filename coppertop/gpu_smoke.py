"""RoboCasa views and a native DiT-only smoke; never starts provider resources."""
import io
import json
import math
from .chords import frame_id, window_indices
from .episode import CAMERAS
from .publication import release_objects
from .xiaomi import native_state, f32

RELEASE = '4e86ad3bb106be2ae9e9fbb32f4799682c0e24ede5966aa5a0af967975e2d8ce'
MODEL_REPO = 'XiaomiRobotics/Xiaomi-Robotics-1-RoboCasa365'
MODEL_REVISION = '3a6d0293bfa90759d34a7fc48c2c62413cd7bcf4'
HEAD_MODULES = ('dit', 'state_projector', 'action_projector', 'action_output_layer',
                't_embedder', 't_projector', 'sink')


def window(rows, record, cutoff):
    n = len(rows)
    if not 0 < n <= 10000 or record['length'] != n:
        raise ValueError('Invalid bounded episode')
    if record['action_layout'] != 'EEtranslation3/EErotation3/gripper1/base4/mode1':
        raise ValueError('Canonical native action layout required')
    indices, valid_history = window_indices(cutoff, n, (-6, -4, -2, 0))
    states = [list(native_state(rows[i]['observation.state'])) + [0.] * 46 for i in indices]
    actions, masks = [], []
    for offset in range(16):
        i = cutoff + offset
        if i < n:
            action = rows[i]['action']
            if len(action) != 12 or not all(math.isfinite(v) for v in action):
                raise ValueError('Expected twelve native finite commands')
            actions.append([f32(v) for v in action] + [0.] * 48)
        else:
            actions.append([0.] * 60)
        masks.append([i < n and dim < 12 for dim in range(60)])
    return {'release': RELEASE, 'episode_id': record['episode_id'],
            'frame_id': frame_id(record['episode_id'], cutoff), 'cutoff': cutoff,
            'instruction': record['instruction'], 'history_indices': list(indices),
            'history_valid': list(valid_history),
            'history_timestamps': [rows[i]['timestamp'] for i in indices],
            'state': states, 'actions_native': actions, 'target_valid': masks,
            'target_semantics': 'executed_controller_commands', 'success': None}


def normalized_targets(sample, config):
    mean, std = config['mean'], config['std']
    if any(len(x) != 16 or any(len(row) != 60 for row in x) for x in (mean, std)):
        raise ValueError('Pinned per-horizon 16x60 action statistics required')
    targets, masks = [], []
    for t in range(16):
        target, mask = [], []
        for d in range(60):
            mu, sigma = mean[t][d], std[t][d]
            if not math.isfinite(mu) or not math.isfinite(sigma) or sigma < 0:
                raise ValueError('Invalid normalization statistics')
            active = sigma > 1e-5
            keep = active and sample['target_valid'][t][d]
            target.append(f32((sample['actions_native'][t][d] - mu) / sigma) if keep else 0.)
            mask.append(keep)
        targets.append(target); masks.append(mask)
    if not any(any(row) for row in masks):
        raise ValueError('No observed active training targets')
    return targets, masks


def load_window(store, release, cutoff):
    if release != RELEASE:
        raise ValueError('Smoke requires its pinned release')
    release_objects(store, release, max_objects=17, max_bytes=1205778)
    manifest = json.loads(store.get(release))
    if len(manifest['episodes']) != 1:
        raise ValueError('One episode required')
    record = json.loads(store.get(manifest['episodes'][0]))
    if set(record['videos']) != set(CAMERAS):
        raise ValueError('Three pinned camera streams required')
    import pyarrow.parquet as pq
    parquet = pq.ParquetFile(io.BytesIO(store.get(record['signals'])))
    if parquet.metadata.num_rows != record['length'] or sum(
            parquet.metadata.row_group(i).total_byte_size for i in range(parquet.metadata.num_row_groups)) > 64 * 1024**2:
        raise ValueError('Signal decode bound exceeded')
    sample = window(parquet.read().to_pylist(), record, cutoff)
    return sample, record


def camera_history(store, record, indices):
    import av
    import numpy as np
    result = {}
    for camera in CAMERAS:
        wanted = set(indices); frames = {}
        with av.open(io.BytesIO(store.get(record['videos'][camera]['object']))) as container:
            for index, frame in enumerate(container.decode(video=0)):
                if frame.width > 4096 or frame.height > 4096 or index >= 10000:
                    raise ValueError('Video decode bound exceeded')
                if index in wanted:
                    if frame.pts is None or abs(float(frame.pts * frame.time_base) - index / 20) > 1e-5:
                        raise ValueError('Camera timestamp mismatch')
                    frames[index] = frame.to_ndarray(format='rgb24')
                if wanted.issubset(frames):
                    break
        if not wanted.issubset(frames):
            raise ValueError('Missing camera frame')
        result[camera] = np.stack([frames[i] for i in indices])
    return result


def messages(images, instruction):
    from PIL import Image
    videos = {}
    for camera in CAMERAS:
        videos[camera] = []
        for raw in images[camera]:
            image = Image.fromarray(raw); w, h = image.size
            cw, ch = max(1, int(w * .95)), max(1, int(h * .95))
            left, top = (w - cw) // 2, (h - ch) // 2
            videos[camera].append(image.crop((left, top, left + cw, top + ch)).resize((w, h), Image.Resampling.BILINEAR))
    return [{'role': 'user', 'content': [
        {'type': 'text', 'text': 'Left camera: '}, {'type': 'video', 'video': videos[CAMERAS[0]]},
        {'type': 'text', 'text': '\nRight camera: '}, {'type': 'video', 'video': videos[CAMERAS[1]]},
        {'type': 'text', 'text': '\nWrist camera: '}, {'type': 'video', 'video': videos[CAMERAS[2]]},
        {'type': 'text', 'text': f'\n\nGenerate robot actions for the task:\n{instruction} /no_cot'}]},
        {'role': 'assistant', 'content': [{'type': 'text', 'text': '<cot></cot>'}]}]


def conditioning(model, inputs):
    import torch
    vlm_inputs = {k: v for k, v in inputs.items() if k not in ('state', 'action_mask')}
    with torch.no_grad():
        # One last-token logit is sufficient: conditioning uses the full KV cache.
        output = model.vlm(**vlm_inputs, use_cache=True, logits_to_keep=1)
    state, action_mask = inputs['state'], inputs['action_mask']
    q = 1 + state.shape[1] + action_mask.shape[1]
    positions = torch.arange(q, device=state.device).view(1, 1, -1).repeat(3, state.shape[0], 1)
    positions = positions + output.position_ids.max(dim=-1).values[..., None] + 1
    rotary = model.rotary_emb(action_mask, positions)
    causal = torch.ones(state.shape[0], q, q, device=state.device).tril()
    cache_mask = output.attention_mask[:, None, :].expand(-1, q, -1)
    mask = torch.cat([cache_mask, causal], dim=-1)[:, None].bool()
    return {'position_embeds': rotary, 'past_key_values': output.past_key_values, 'attn_mask': mask}


def velocity(model, inputs, noisy_action, timestep, context):
    return model.dit_forward(noisy_action=noisy_action, t=timestep,
        action_mask=inputs['action_mask'], state_embed=model.state_projector(inputs['state']), **context)


def flow_loss(prediction, target, observed_mask):
    if prediction.shape != target.shape or target.shape != observed_mask.shape or not observed_mask.any():
        raise ValueError('Invalid masked flow loss')
    return ((prediction.float() - target.float()) ** 2)[observed_mask.bool()].mean()


def trainable_head(model):
    model.requires_grad_(False)
    for name in HEAD_MODULES:
        getattr(model, name).requires_grad_(True)
    return [p for p in model.parameters() if p.requires_grad]
