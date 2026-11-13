"""Demonstration-quality scoring on synthetic LeRobot v3 fixtures; no dataset downloads."""
import json
from pathlib import Path
import tempfile
import unittest

import numpy as np
import pyarrow as pa
import pyarrow.parquet as pq

from coppertop.quality import Layout, grasps, score, write

FPS = 10


def reach(seconds, rng, *, pause_at=None, pause_seconds=0, regrasps=1):
    """A hand moving to a cup, grasping, carrying it and releasing, with small tracking noise."""
    frames = int(seconds * FPS)
    t = np.linspace(0, 1, frames)
    path = np.column_stack([0.3 * np.sin(np.pi * t), 0.2 * t, 0.1 * np.sin(2 * np.pi * t)])
    if pause_at is not None:
        hold = int(pause_seconds * FPS)
        cut = int(pause_at * frames)
        path = np.concatenate([path[:cut], np.repeat(path[cut:cut + 1], hold, axis=0), path[cut:]])
    path += rng.normal(0, 1e-4, path.shape)
    width = np.full(len(path), 0.08)
    start = int(len(path) * 0.3)
    for k in range(regrasps - 1):  # failed attempts: close briefly, reopen, try again
        width[start + 10 * k:start + 10 * k + 5] = 0.055
    width[start + 10 * (regrasps - 1):int(len(path) * 0.8)] = 0.055
    rotation = np.zeros((len(path), 3))
    return np.column_stack([path, rotation, width])


def dataset(root, episodes):
    root = Path(root)
    (root / 'meta' / 'episodes' / 'chunk-000').mkdir(parents=True)
    (root / 'data' / 'chunk-000').mkdir(parents=True)
    (root / 'meta' / 'info.json').write_text(json.dumps({'fps': FPS}))
    rows, meta = [], []
    for index, (task, state) in enumerate(episodes):
        meta.append({'episode_index': index, 'tasks': [task]})
        for frame, values in enumerate(state):
            rows.append({'observation.state': values.astype(np.float32).tolist(), 'episode_index': index,
                         'timestamp': np.float32(frame / FPS)})
    pq.write_table(pa.Table.from_pylist(rows), root / 'data' / 'chunk-000' / 'file-000.parquet')
    pq.write_table(pa.Table.from_pylist(meta), root / 'meta' / 'episodes' / 'chunk-000' / 'file-000.parquet')
    return root


class QualityTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        rng = np.random.default_rng(0)
        cls.tmp = tempfile.TemporaryDirectory()
        normal = [('cup', reach(rng.uniform(38, 44), rng)) for _ in range(30)]
        long_task = [('stack', reach(rng.uniform(95, 105), rng)) for _ in range(30)]
        drift = reach(40, rng)
        drift[:, :3] += np.linspace(0, 1, len(drift))[:, None] * np.array([3.0, 0, 0])
        jump = reach(40, rng)
        jump[200:, 0] += 0.5
        special = [
            ('cup', drift),                                    # 30: tracking drift
            ('cup', jump),                                     # 31: pose jump
            ('cup', reach(3, rng)),                            # 32: truncated segment
            ('cup', reach(40, rng, pause_at=0.5, pause_seconds=40)),  # 33: long mid-task pause
            ('cup', reach(40, rng, regrasps=4)),               # 34: regrasping
        ]
        cls.root = dataset(Path(cls.tmp.name) / 'data', normal + special + long_task)
        cls.result = score(cls.root, Layout())
        cls.rows = {row['episode_index']: row for row in cls.result['episodes']}

    @classmethod
    def tearDownClass(cls):
        cls.tmp.cleanup()

    def test_capture_faults_are_separated_from_execution(self):
        self.assertIn('tracking drift', ' '.join(self.rows[30]['capture_faults']))
        self.assertIn('pose jumps', ' '.join(self.rows[31]['capture_faults']))
        self.assertIn('truncated', ' '.join(self.rows[32]['capture_faults']))
        for index in (30, 31, 32):
            self.assertIsNone(self.rows[index]['execution_composite'])
            self.assertEqual(self.rows[index]['execution_flags'], [])

    def test_execution_flags_name_the_reason(self):
        self.assertFalse(self.rows[33]['capture_faults'])
        self.assertIn('pause', ' '.join(self.rows[33]['execution_flags']))
        self.assertEqual(self.rows[34]['grasps'], 4)
        self.assertIn('regrasping', ' '.join(self.rows[34]['execution_flags']))

    def test_long_tasks_are_compared_within_their_task(self):
        long_rows = [row for row in self.rows.values() if row['task'] == 'stack']
        self.assertTrue(all(not row['capture_faults'] for row in long_rows))
        self.assertLessEqual(sum(bool(row['execution_flags']) for row in long_rows), 2)

    def test_clean_episodes_mostly_unflagged_and_grasp_once(self):
        clean = [self.rows[index] for index in range(30)]
        self.assertTrue(all(not row['capture_faults'] for row in clean))
        self.assertTrue(all(row['grasps'] == 1 for row in clean))
        self.assertLessEqual(sum(bool(row['execution_flags']) for row in clean), 2)

    def test_grasp_detection_is_relative_to_open_width(self):
        wide_object = np.concatenate([np.full(20, 0.082), np.full(20, 0.069), np.full(20, 0.082)])
        self.assertEqual(grasps(wide_object, 0.009), 1)
        chatter = np.concatenate([np.full(10, 0.08), np.full(3, 0.05), np.full(3, 0.08), np.full(3, 0.05)])
        self.assertEqual(grasps(chatter, 0.009), 2)

    def test_write_refuses_existing_destination(self):
        destination = Path(self.tmp.name) / 'out'
        summary = write(self.result, destination, window=10)
        self.assertEqual(summary['episodes'], 65)
        self.assertEqual(summary['capture_faulted'], 3)
        self.assertEqual(pq.read_table(destination / 'episodes.parquet').num_rows, 65)
        with self.assertRaises(FileExistsError):
            write(self.result, destination)


if __name__ == '__main__':
    unittest.main()
