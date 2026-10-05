"""Execute the installed official loader offline; never substitute fixture inspection."""
import argparse
import importlib.metadata
import json
import os
from pathlib import Path

# Offline gates apply before importing Hugging Face libraries.
os.environ['HF_HUB_OFFLINE'] = '1'
os.environ['HF_DATASETS_OFFLINE'] = '1'


def verify(root: Path) -> dict:
    import av
    import numpy as np
    import pyarrow.parquet as pq
    import torch
    from lerobot.datasets.lerobot_dataset import LeRobotDataset
    from .lerobot import LOADER_VERSION

    if importlib.metadata.version('lerobot') != LOADER_VERSION:
        raise ValueError('Unexpected official loader version')
    info = json.loads((root / 'meta/info.json').read_text())
    dataset = LeRobotDataset('local/coppertop', root=root, video_backend='pyav')
    rows = pq.read_table(root / 'data/chunk-000/episode_000000.parquet').to_pylist()
    assert len(dataset) == len(rows) == 347
    assert dataset.episode_data_index['from'].tolist() == [0]
    assert dataset.episode_data_index['to'].tolist() == [347]
    expected_video = {}
    for camera in dataset.meta.video_keys:
        path = root / info['video_path'].format(episode_chunk=0, episode_index=0, video_key=camera)
        with av.open(str(path)) as stream:
            frames = list(stream.decode(video=0))
            assert len(frames) == len(rows)
            for i, frame in enumerate(frames):
                assert abs(float(frame.pts * frame.time_base) - rows[i]['timestamp']) < 1e-5
            expected_video[camera] = [frame.to_ndarray(format='rgb24') for frame in frames]
    max_error = {'observation.state': 0., 'action': 0.}
    for i, row in enumerate(rows):
        item = dataset[i]
        assert item['frame_index'].item() == i
        assert item['episode_index'].item() == 0
        assert item['timestamp'].item() == row['timestamp']
        assert item['next.done'].item() == row['next.done']
        for name in max_error:
            expected = torch.tensor(row[name])
            assert torch.equal(item[name], expected), (i, name)
            max_error[name] = max(max_error[name], float(np.max(np.abs(item[name].numpy().astype(np.float64) - np.asarray(row[name])))))
        for camera in dataset.meta.video_keys:
            expected = torch.from_numpy(expected_video[camera][i]).permute(2, 0, 1).float() / 255
            assert torch.equal(item[camera], expected), (i, camera)
    windows = LeRobotDataset('local/coppertop', root=root, video_backend='pyav',
                            delta_timestamps={'action': [0., 1 / info['fps']]})
    assert windows[0]['action_is_pad'].tolist() == [False, False]
    assert windows[len(rows)-1]['action_is_pad'].tolist() == [False, True]
    return {'status': 'passed', 'loader': 'lerobot==' + LOADER_VERSION, 'format': 'v2.1',
            'frames_checked': len(rows), 'camera_frames_checked': 3 * len(rows),
            'episode_bounds': [0, len(rows)], 'video_backend': 'pyav',
            'state_action_tensor_parity': 'bit-identical to official float32 conversion',
            'float64_to_float32_max_abs_error': max_error,
            'camera_pixels': 'all decoded frames equal', 'camera_pts': 'all within 1e-5 seconds',
            'boundary_padding': 'first and last frame verified', 'offline': True}


if __name__ == '__main__':
    parser = argparse.ArgumentParser()
    parser.add_argument('root', type=Path)
    args = parser.parse_args()
    print(json.dumps(verify(args.root.resolve()), indent=2))
