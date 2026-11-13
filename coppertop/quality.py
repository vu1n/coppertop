"""Demonstration-quality scoring for LeRobot v3 datasets.

Scores each episode on two separate axes and never mixes them:

* capture: was the recording itself trustworthy (clock gaps, frozen or jumping pose, tracking drift,
  truncated segments)? A capture fault says nothing about the person collecting.
* execution: among capture-clean episodes, how did this demonstration compare with others of the same
  task (duration, idle time, interior pauses, smoothness, grasp count)? Robust z-scores are computed
  within each task, so long tasks are not penalized for being long.

Signals are read-only views of the dataset; nothing is written back. Done is not success: no outcome is
inferred, and an episode with no detected grasp is reported as ambiguous rather than failed.
"""
import argparse
from dataclasses import dataclass, asdict
import json
from pathlib import Path

import numpy as np
import pyarrow as pa
import pyarrow.parquet as pq


@dataclass(frozen=True)
class Limits:
    max_speed: float = 2.0          # m/s; faster frame-to-frame motion is a tracking jump, not a hand
    max_drift: float = 1.0          # m from the episode's median position; beyond reach of a seated collector
    frozen_frames: int = 5          # identical consecutive poses that count as a frozen tracker
    idle_speed: float = 0.02        # m/s below which the end effector counts as idle
    pause_seconds: float = 1.0      # interior idle run that counts as a pause
    truncated_fraction: float = 0.25  # shorter than this share of the task median = truncated segment
    flag_quantile: float = 0.95     # execution composite above this within-task quantile is flagged


@dataclass(frozen=True)
class Layout:
    state: str = 'observation.state'
    position: tuple = (0, 3)
    gripper: int | None = 6


def load(root):
    """Return (fps, tasks_by_episode, episodes) where episodes maps index -> (timestamps, state)."""
    root = Path(root)
    info = json.loads((root / 'meta' / 'info.json').read_text())
    tasks = {}
    for path in sorted((root / 'meta' / 'episodes').rglob('*.parquet')):
        for row in pq.read_table(path, columns=['episode_index', 'tasks']).to_pylist():
            tasks[row['episode_index']] = tuple(row['tasks'] or ())
    return float(info['fps']), tasks, root


def episodes(root, layout):
    frames = pa.concat_tables(pq.read_table(path, columns=[layout.state, 'episode_index', 'timestamp'])
                              for path in sorted((Path(root) / 'data').rglob('*.parquet')))
    index = frames['episode_index'].to_numpy()
    order = np.argsort(index, kind='stable')
    index = index[order]
    times = frames['timestamp'].to_numpy().astype(np.float64)[order]
    state = np.asarray(frames[layout.state].to_pylist(), dtype=np.float64)[order]
    bounds = np.flatnonzero(np.diff(index)) + 1
    for chunk in np.split(np.arange(len(index)), bounds):
        yield int(index[chunk[0]]), times[chunk], state[chunk]


def gripper_travel(values):
    """Closing travel that counts as a grasp: a third of the dataset's open-to-closed spread.

    Relative to each episode's own open width, so a wider object (which stops the jaws earlier) or a
    recalibrated gripper still registers; a fixed absolute threshold misreads both.
    """
    low, high = np.percentile(values, [10, 90])
    return (high - low) / 3


def grasps(width, travel):
    opened_level = np.percentile(width, 95)
    closed, opened = opened_level - travel, opened_level - travel / 3
    count, is_open = 0, width[0] > closed
    for value in width:
        if is_open and value < closed:
            count, is_open = count + 1, False
        elif not is_open and value > opened:
            is_open = True
    return count


def runs(mask):
    """Lengths and starts of True runs."""
    padded = np.concatenate(([False], mask, [False])).astype(np.int8)
    edges = np.flatnonzero(np.diff(padded))
    starts, ends = edges[::2], edges[1::2]
    return starts, ends - starts


def smoothness(position, fps):
    """Log dimensionless jerk on lightly smoothed position; higher (less negative) is smoother."""
    if len(position) < 8:
        return float('nan')
    kernel = np.ones(5) / 5
    smooth = np.column_stack([np.convolve(axis, kernel, mode='valid') for axis in position.T])
    dt = 1 / fps
    velocity = np.diff(smooth, axis=0) / dt
    jerk = np.diff(velocity, n=2, axis=0) / dt ** 2
    peak = np.linalg.norm(velocity, axis=1).max()
    duration = len(smooth) * dt
    if peak == 0:
        return float('nan')
    cost = (duration ** 3 / peak ** 2) * np.sum(np.linalg.norm(jerk, axis=1) ** 2) * dt
    return float(-np.log(cost))


def measure(times, state, fps, layout, limits, grip_band):
    position = state[:, layout.position[0]:layout.position[1]]
    step = np.diff(position, axis=0)
    speed = np.linalg.norm(step, axis=1) * fps
    dt = np.diff(times)
    frozen_starts, frozen_lengths = runs(np.all(step == 0, axis=1))
    idle_starts, idle_lengths = runs(speed < limits.idle_speed)
    interior = (idle_starts > 0) & (idle_starts + idle_lengths < len(speed))
    pauses = idle_lengths[interior] / fps
    row = {
        'frames': len(times),
        'duration_s': len(times) / fps,
        'clock_gaps': int(np.sum(np.abs(dt - 1 / fps) > 0.5 / fps)),
        'frozen_runs': int(np.sum(frozen_lengths >= limits.frozen_frames)),
        'pose_jumps': int(np.sum(speed > limits.max_speed)),
        'drift_m': float(np.linalg.norm(position - np.median(position, axis=0), axis=1).max()),
        'idle_fraction': float(np.mean(speed < limits.idle_speed)) if len(speed) else float('nan'),
        'interior_pauses': int(np.sum(pauses >= limits.pause_seconds)),
        'longest_pause_s': float(pauses.max()) if len(pauses) else 0.0,
        'smoothness': smoothness(position, fps),
        'grasps': None,
    }
    if layout.gripper is not None:
        row['grasps'] = grasps(state[:, layout.gripper], grip_band)
    return row


def robust_z(values):
    values = np.asarray(values, dtype=np.float64)
    median = np.nanmedian(values)
    scale = np.nanmedian(np.abs(values - median)) * 1.4826
    if not scale > 0:  # most peers identical (e.g. no pauses at all): fall back so outliers still show
        scale = np.nanmean(np.abs(values - median)) * 1.2533
    return (values - median) / scale if scale > 0 else np.zeros_like(values)


EXECUTION = {  # metric -> sign so that positive z always means "worse than peers on this task"
    'duration_s': 1, 'idle_fraction': 1, 'longest_pause_s': 1, 'smoothness': -1,
}


def classify(rows, limits):
    """Attach capture faults, within-task execution z-scores, a composite and reasons, in place."""
    by_task = {}
    for row in rows:
        by_task.setdefault(row['task'], []).append(row)
    for group in by_task.values():
        median_duration = float(np.median([row['duration_s'] for row in group]))
        for row in group:
            faults = []
            if row['clock_gaps']:
                faults.append(f"{row['clock_gaps']} clock gaps")
            if row['frozen_runs']:
                faults.append(f"{row['frozen_runs']} frozen-pose runs")
            if row['pose_jumps']:
                faults.append(f"{row['pose_jumps']} pose jumps over {limits.max_speed} m/s")
            if row['drift_m'] > limits.max_drift:
                faults.append(f"tracking drift {row['drift_m']:.1f} m from episode center")
            if row['duration_s'] < limits.truncated_fraction * median_duration:
                faults.append(f"truncated: {row['duration_s']:.1f}s vs task median {median_duration:.0f}s")
            row['capture_faults'] = faults
        clean = [row for row in group if not row['capture_faults']]
        scores = {name: sign * robust_z([row[name] for row in clean]) for name, sign in EXECUTION.items()}
        composite = np.nanmean(np.clip(np.column_stack(list(scores.values())), 0, None), axis=1)
        cutoff = float(np.quantile(composite, limits.flag_quantile)) if len(clean) else 0.0
        for position, row in enumerate(clean):
            row['execution_composite'] = float(composite[position])
            for name in EXECUTION:
                row[f'z_{name}'] = float(scores[name][position])
            reasons = []
            if composite[position] > cutoff:
                worst = sorted(EXECUTION, key=lambda name: -scores[name][position])[:3]
                reasons.extend(describe(name, row, scores[name][position]) for name in worst
                               if scores[name][position] > 1)
            if row['grasps'] is not None and row['grasps'] >= 3:
                reasons.append(f"{row['grasps']} grasp attempts (regrasping)")
            row['execution_flags'] = reasons
            row['ambiguous'] = (['no grasp detected: gripper sensing or a missed grasp, check video']
                                if row['grasps'] == 0 else [])
        for row in group:
            if row['capture_faults']:
                row.update(execution_composite=None, execution_flags=[], ambiguous=[],
                           **{f'z_{name}': None for name in EXECUTION})
    return rows


def describe(name, row, z):
    text = {
        'duration_s': f"slow: {row['duration_s']:.0f}s",
        'idle_fraction': f"idle {row['idle_fraction']:.0%} of the episode",
        'longest_pause_s': f"{row['longest_pause_s']:.1f}s mid-task pause",
        'smoothness': 'jerky motion',
    }[name]
    return f'{text} ({z:+.1f} robust z vs task)'


def trend(rows, window):
    rows = sorted(rows, key=lambda row: row['episode_index'])
    out = []
    for start in range(0, len(rows) - window + 1, max(1, window // 2)):
        chunk = rows[start:start + window]
        scored = [row['execution_composite'] for row in chunk if row['execution_composite'] is not None]
        out.append({
            'first_episode': chunk[0]['episode_index'], 'last_episode': chunk[-1]['episode_index'],
            'capture_fault_rate': float(np.mean([bool(row['capture_faults']) for row in chunk])),
            'execution_flag_rate': float(np.mean([bool(row['execution_flags']) for row in chunk])),
            'median_composite': float(np.median(scored)) if scored else None,
        })
    return out


def score(root, layout=Layout(), limits=Limits()):
    fps, tasks, root = load(root)
    data = list(episodes(root, layout))
    band = None
    if layout.gripper is not None:
        band = gripper_travel(np.concatenate([state[:, layout.gripper] for _, _, state in data]))
    rows = []
    for index, times, state in data:
        row = {'episode_index': index, 'task': ' | '.join(tasks.get(index, ())) or 'unknown'}
        row.update(measure(times, state, fps, layout, limits, band))
        rows.append(row)
    classify(rows, limits)
    return {'fps': fps, 'gripper_travel': band, 'limits': asdict(limits), 'layout': asdict(layout),
            'episodes': rows}


def write(result, destination, window=50):
    destination = Path(destination)
    destination.mkdir(parents=True, exist_ok=False)
    rows = result['episodes']
    table = [dict(row, capture_faults='; '.join(row['capture_faults']),
                  execution_flags='; '.join(row['execution_flags']), ambiguous='; '.join(row['ambiguous']))
             for row in rows]
    pq.write_table(pa.Table.from_pylist(table), destination / 'episodes.parquet')
    summary = {key: value for key, value in result.items() if key != 'episodes'}
    summary.update(
        episodes=len(rows),
        capture_faulted=sum(bool(row['capture_faults']) for row in rows),
        execution_flagged=sum(bool(row['execution_flags']) for row in rows),
        ambiguous=sum(bool(row['ambiguous']) for row in rows),
        trend=trend(rows, window),
    )
    (destination / 'summary.json').write_text(json.dumps(summary, indent=2, default=float))
    return summary


def main():
    parser = argparse.ArgumentParser(description='Score demonstration quality in a LeRobot v3 dataset')
    parser.add_argument('dataset', type=Path)
    parser.add_argument('destination', type=Path)
    parser.add_argument('--state', default='observation.state')
    parser.add_argument('--position', default='0:3', help='slice of the state vector holding xyz metres')
    parser.add_argument('--gripper', type=int, default=6, help='state index of gripper width; -1 for none')
    parser.add_argument('--window', type=int, default=50)
    args = parser.parse_args()
    start, stop = (int(x) for x in args.position.split(':'))
    layout = Layout(args.state, (start, stop), None if args.gripper < 0 else args.gripper)
    summary = write(score(args.dataset, layout), args.destination, args.window)
    print(json.dumps({key: summary[key] for key in ('episodes', 'capture_faulted', 'execution_flagged',
                                                    'ambiguous')}))


if __name__ == '__main__':
    main()
