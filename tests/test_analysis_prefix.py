"""Opt-in integration over preserved native prefixes, never completed failure labels."""
import io
import json
import os
from pathlib import Path
import tarfile
import tempfile
import unittest

import numpy as np
import pyarrow as pa

from coppertop.analysis import FRAME_SCHEMA, FrameData, Snapshot, write_snapshot
from coppertop.chords import canonical, digest, frame_id
from coppertop.official_recording import STATE_FIELDS
from test_analysis import HAS_ANALYSIS


def recovered_prefixes(archives):
    rows, sources = [], []
    for item in archives:
        path = Path(item['path'])
        if path.is_symlink() or path.stat().st_size > 150 * 1024**2 or digest(path.read_bytes()) != item['sha256']:
            raise ValueError('Pinned bounded smoke archive bytes required')
        sources.append({'kind': 'native_prefix_smoke_only', 'sha256': item['sha256'],
                        'complete_episode': False, 'predictive_performance_evidence': False})
        with tarfile.open(path) as archive:
            for seed in (1319902946, 1319902947):
                member = f'{seed}/result.json'
                raw = archive.extractfile(member).read()
                result = json.loads(raw)
                if not result['real_simulator_execution'] or result['native_model_execution'] or len(result['frames']) != 9:
                    raise ValueError('Expected real simulator-only nine-observation prefix')
                action_path = path.parent.parent / 'inputs' / f'reset-{seed}-native-actions.npy'
                action_bytes = action_path.read_bytes()
                if digest(action_bytes) != result['action_input']['file_sha256']:
                    raise ValueError('Retained native action bytes differ')
                actions = np.load(io.BytesIO(action_bytes), allow_pickle=False)
                if actions.shape != (8, 12) or actions.dtype != np.float32:
                    raise ValueError('Expected exact native action12 prefix')
                group = f'RoboCasa/CoffeeSetupMug/constructor{result["constructor_seed"]}/reset{seed}'
                episode = group + '/' + result['plan_sha256']
                provenance = {'archive_sha256': item['sha256'], 'member': member,
                              'plan_sha256': result['plan_sha256'], 'real_simulator': True,
                              'native_model_execution': False, 'action_file_sha256': digest(action_bytes)}
                for frame in result['frames']:
                    state = []
                    for field, width in STATE_FIELDS:
                        ref = frame['arrays']['observation/' + field]
                        value_bytes = archive.extractfile(f'{seed}/objects/{ref["object"]}').read()
                        if digest(value_bytes) != ref['object']:
                            raise ValueError('Native state object hash differs')
                        value = np.load(io.BytesIO(value_bytes), allow_pickle=False)
                        if list(value.shape) != [width] or str(value.dtype) != ref['dtype']:
                            raise ValueError('Native state geometry differs')
                        state.extend(value.tolist())
                    index = frame['index']
                    rows.append({'release_id': None, 'episode_ref': digest(raw), 'episode_id': episode,
                                 'initialization_group': group, 'split': 'development',
                                 'data_kind': 'real_simulator_prefix_smoke_only',
                                 'provenance_sha256': digest(canonical(provenance)), 'source_sha256': digest(raw),
                                 'frame_id': frame_id(episode, index), 'frame_index': index,
                                 'time_ns': frame['control_time_ns'], 'state': state,
                                 'action': actions[index].tolist() if index < 8 else None,
                                 'observation_ref': frame['components']['observation'],
                                 'camera_refs': canonical({name: ref['object'] for name, ref in frame['arrays'].items()
                                                           if name.startswith('observation/video.')}).decode(),
                                 'success': frame['success'], 'terminated': frame['terminated'],
                                 'truncated': frame['truncated'], 'episode_success': None,
                                 'outcome_scope': 'unfinished eight-control prefix; final outcome unknown'})
    return FrameData(pa.Table.from_pylist(rows, schema=FRAME_SCHEMA), tuple(sources)).validate()


@unittest.skipUnless(HAS_ANALYSIS and os.environ.get('COPPERTOP_PREFIX_ARCHIVES'),
                     'Set COPPERTOP_PREFIX_ARCHIVES to existing pinned local smoke archives')
class PreservedPrefixTests(unittest.TestCase):
    def test_real_prefix_queries_and_exact_state_neighbors_without_outcome_claim(self):
        archives = json.loads(os.environ['COPPERTOP_PREFIX_ARCHIVES'])
        data = recovered_prefixes(archives)
        with tempfile.TemporaryDirectory() as directory:
            checksum = write_snapshot(data, Path(directory) / 'snapshot')
            snapshot = Snapshot(Path(directory) / 'snapshot', checksum)
            counts = snapshot.query('SELECT COUNT(*) AS n, COUNT(DISTINCT initialization_group) AS groups, '
                                    'COUNT(episode_success) AS known_outcomes, MAX(time_ns) AS last_ns FROM frames').to_pylist()[0]
            self.assertEqual(counts, {'n': 18 * len(archives), 'groups': 2, 'known_outcomes': 0, 'last_ns': 400_000_000})
            filtered = snapshot.query('SELECT frame_id, time_ns FROM frames WHERE frame_index >= 6 ORDER BY frame_id').to_pylist()
            baseline = sorted([{'frame_id': r['frame_id'], 'time_ns': r['time_ns']} for r in data.table.to_pylist()
                               if r['frame_index'] >= 6], key=lambda r: r['frame_id'])
            self.assertEqual(filtered, baseline)
            query = data.table['state'][0].as_py()
            neighbors = snapshot.nearest(query, k=len(archives)).to_pylist()
            self.assertEqual([row['_distance'] for row in neighbors], [0.] * len(archives))
            self.assertTrue(all(row['episode_success'] is None for row in neighbors))
        for item in archives:
            self.assertEqual(digest(Path(item['path']).read_bytes()), item['sha256'])


if __name__ == '__main__':
    unittest.main()
