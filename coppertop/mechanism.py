"""CPU contracts for prefix replay and delayed replanning, not a live runner."""
from dataclasses import dataclass
import re
from typing import Protocol

import numpy as np

from .chords import ObjectStore, canonical, digest, frame_id

COMPONENTS = frozenset(('physics', 'model', 'task', 'controller', 'rng',
                        'observables', 'history', 'observation'))
ACTION_ORDER = ('ee_pos_x', 'ee_pos_y', 'ee_pos_z', 'ee_rot_x', 'ee_rot_y',
                'ee_rot_z', 'gripper', 'base_x', 'base_y', 'base_z', 'base_yaw', 'mode')


def native_actions(value, *, chunk=False):
    array = np.asarray(value, dtype=np.float32)
    if array.shape != ((16, 12) if chunk else (12,)) or not np.isfinite(array).all():
        raise ValueError('Finite native 16x12 chunk or 12-control action required')
    return tuple(tuple(float(x) for x in row) for row in array) if chunk else tuple(float(x) for x in array)


@dataclass(frozen=True)
class Frame:
    """Post-control audit: hashes do not substitute for a coverage audit."""
    step: int
    time_ns: int
    components: tuple[tuple[str, str], ...]
    success: bool = False
    terminated: bool = False
    truncated: bool = False

    def __post_init__(self):
        if type(self.step) is not int or self.step < 0 or type(self.time_ns) is not int or self.time_ns < 0:
            raise ValueError('Nonnegative integer step/time required')
        if (type(self.components) is not tuple
                or any(type(pair) is not tuple or len(pair) != 2 for pair in self.components)
                or len(self.components) != len(COMPONENTS)
                or {key for key, _ in self.components} != COMPONENTS
                or any(not isinstance(value, str) or not re.fullmatch('[0-9a-f]{64}', value) for _, value in self.components)):
            raise ValueError('All audited components require SHA256 hashes')
        if any(type(value) is not bool for value in (self.success, self.terminated, self.truncated)):
            raise ValueError('Explicit success and boundary flags required')
        # Hash maps are semantic maps: adapter iteration order must not affect parity.
        object.__setattr__(self, 'components', tuple(sorted(self.components)))

    @property
    def stopped(self):
        return self.success or self.terminated or self.truncated

    def record(self, episode_id):
        return {'frame_id': frame_id(episode_id, self.step), 'step': self.step,
                'time_ns': self.time_ns, 'components': dict(self.components),
                'success': self.success, 'terminated': self.terminated, 'truncated': self.truncated}


class ReplayEnvironment(Protocol):
    """Each instance owns a fresh simulator; reset reproduces constructor RNG too.

    Fingerprinting must inspect existing caches without advancing observables.
    close releases the owned environment even if parity fails.
    """
    def reset(self, seed: int) -> Frame: ...
    def step(self, action: tuple[float, ...]) -> Frame: ...
    def close(self) -> None: ...


@dataclass(frozen=True)
class Tape:
    episode_id: str
    seed: int
    actions: tuple[tuple[float, ...], ...]
    frames: tuple[Frame, ...]
    parent_episode_id: str | None = None
    inherited_frames: int = 0

    def __post_init__(self):
        if not isinstance(self.episode_id, str) or not self.episode_id or type(self.seed) is not int or self.seed < 0:
            raise ValueError('Episode identity and seed required')
        if (type(self.actions) is not tuple or type(self.frames) is not tuple
                or len(self.frames) != len(self.actions) + 1 or not self.frames):
            raise ValueError('Tape requires reset and every post-control frame')
        if (type(self.inherited_frames) is not int or not 0 <= self.inherited_frames <= len(self.frames)
                or (self.parent_episode_id is None and self.inherited_frames != 0)
                or (self.parent_episode_id is not None and (not isinstance(self.parent_episode_id, str)
                    or not self.parent_episode_id or self.parent_episode_id == self.episode_id))):
            raise ValueError('Explicit parent identity and inherited frame range required')
        for index, frame in enumerate(self.frames):
            if not isinstance(frame, Frame):
                raise ValueError('Audited Frame instances required')
            if frame.step != index or (index and frame.time_ns <= self.frames[index-1].time_ns):
                raise ValueError('Contiguous steps and increasing control timestamps required')
            if index < len(self.actions) and frame.stopped:
                raise ValueError('Cannot execute beyond success or episode boundary')
        for action in self.actions:
            if (type(action) is not tuple or any(type(x) is not float for x in action)
                    or native_actions(action) != action):
                raise ValueError('Tape actions must already be immutable float32 controls')

    def identity(self, index):
        episode = self.parent_episode_id if index < self.inherited_frames else self.episode_id
        return frame_id(episode, index)

    def record(self, provenance: dict) -> dict:
        """Canonical native audit payload; camera/signal objects stay external."""
        return {'schema_version': 1, 'kind': 'mechanism_replay_tape',
            'episode_id': self.episode_id, 'seed': self.seed,
            'parent_episode_id': self.parent_episode_id, 'inherited_frames': self.inherited_frames,
            'action_order': ACTION_ORDER, 'actions': self.actions,
            'frames': [dict(frame.record(self.episode_id), frame_id=self.identity(index))
                       for index, frame in enumerate(self.frames)],
            'provenance': provenance}

    def publish(self, store: ObjectStore, provenance: dict) -> str:
        return store.put(canonical(self.record(provenance)))


def replay(factory, tape: Tape, *, prefix_steps: int, continuation_steps: int):
    """Compare reset, full prefix, then identical suffix in a fresh environment.

    Returns bounded observed parity, never a claim of universal restoration.
    """
    if (type(prefix_steps) is not int or type(continuation_steps) is not int
            or prefix_steps < 0 or continuation_steps <= 0
            or prefix_steps + continuation_steps > len(tape.actions)):
        raise ValueError('Recorded prefix and nonempty continuation required')
    env = factory()
    try:
        if env.reset(tape.seed) != tape.frames[0]:
            raise ValueError('Reset parity failed')
        for index, action in enumerate(tape.actions[:prefix_steps + continuation_steps], 1):
            if env.step(action) != tape.frames[index]:
                raise ValueError(f'Replay parity failed at control {index}')
        return {'prefix_steps': prefix_steps, 'continuation_steps': continuation_steps,
                'status': 'bounded_parity', 'components': sorted(COMPONENTS)}
    finally:
        env.close()


def delayed_branches(old_chunk, candidate, *, boundary: int, latency_ns: int,
                     control_ns: int, steps: int):
    """Plan equal-duration arms from query-start chunks with a shared prefix.

    Candidate indices advance during delay; stale predictions are discarded.
    The live runner must actually keep stepping while the query is in flight.
    """
    old = native_actions(old_chunk, chunk=True)
    new = native_actions(candidate, chunk=True)
    if (any(type(x) is not int for x in (boundary, latency_ns, control_ns, steps))
            or not 0 < boundary < 16 or latency_ns < 0 or control_ns <= 0 or steps <= 0):
        raise ValueError('Completed-control boundary and integer timing required')
    delay = (latency_ns + control_ns - 1) // control_ns
    if boundary + steps > 16 or delay >= steps:
        raise ValueError('Both queues must cover delay and a post-arrival comparison')
    shared = old[boundary:boundary + delay]
    return {'committed_steps': delay, 'discarded_candidate_steps': delay,
            'continue': old[boundary:boundary + steps],
            'replan': shared + new[delay:steps]}


def candidate_diagnostics(first, repeated, alternative):
    """Callers supply same-input/same-seed repeat and different-seed candidate.

    Array evidence is necessary, but cannot verify the caller's seed/input log.
    """
    a, b, c = (np.asarray(native_actions(x, chunk=True), dtype=np.float32)
               for x in (first, repeated, alternative))
    return {'same_seed_exact': bool(np.array_equal(a, b)),
            'different_seed_distinct': bool(not np.array_equal(a, c)),
            'max_abs_difference': float(np.max(np.abs(a.astype(np.float64) - c))),
            'query_calls': 3, 'discarded_calls': 2,
            'chunk_sha256': [digest(x.tobytes()) for x in (a, b, c)]}
