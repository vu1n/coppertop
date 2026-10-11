"""Injected CPU episode fixtures; no provider, model, or simulator readiness claim.

Deadlines are checked before/after cooperative adapter calls. The actual gate owns
its scoring worker; uninterruptible reset/step/cancel/close calls still require an
outer process deadline in a qualified runtime. This module never launches one.
"""
from dataclasses import asdict, dataclass
import base64
import math
import threading
import time

import numpy as np

from .chords import canonical, digest
from .decision_gate import GatePolicy, validate_answers
from .decision_runner import gated_rollout
from .mechanism import COMPONENTS, Frame, Tape, native_actions
from .mechanism_runtime import input_hash

HORIZON = 600
ACTOR_SEED = 20261006
ARMS = ('xiaomi_only', 'xiaomi_clef')


@dataclass(frozen=True)
class EpisodeBounds:
    episode_seconds: float
    reset_seconds: float
    input_seconds: float
    actor_seconds: float
    scorer_seconds: float
    step_seconds: float
    cleanup_seconds: float
    handoff_seconds: float

    def __post_init__(self):
        if any(type(value) not in (int, float) or not math.isfinite(value) or value <= 0
               for value in asdict(self).values()):
            raise ValueError('Explicit positive finite deadline bounds required')
        if self.handoff_seconds > 30:
            raise ValueError('Actual bounded gate permits at most 30 seconds')


class _OperationError(Exception):
    pass


def _counts(journal):
    result = {}
    for row in journal:
        keys = [row['kind']]
        if row['kind'] == 'actor':
            keys.append('actor_' + row['phase'])
        for key in keys:
            group = result.setdefault(key, dict(submitted=0, completed=0, failed=0, timeout=0))
            group['submitted'] += 1
            if row['status'] != 'submitted':
                group[row['status']] += 1
    for key in ('actor', 'actor_initial', 'actor_normal', 'actor_extra', 'scorer',
                'reset', 'input', 'step', 'cancel', 'cleanup'):
        result.setdefault(key, dict(submitted=0, completed=0, failed=0, timeout=0))
    return result


def run_episode(env, actor, scorer, *, episode_id, seed, clef_enabled,
                bounds: EpisodeBounds, clock=time, actor_seed=ACTOR_SEED,
                extra_actor_budget=1):
    """Run one proposed paused 600-control episode through owned injected adapters.

    env.reset/step return Frame, inputs returns the existing native input tuple,
    reset_components returns all eight exact raw component bytes. Every supplied
    resource owns close(); cancel() must cooperate on timed-out/failed operations.
    Virtual-clock callers must bind decision_runner.time to the same clock.
    Failed or late audited steps remain in a valid partial Tape. Missing/invalid
    reset evidence produces tape=None. All close attempts and secondary errors are
    retained; failures never become policy success or a silently retried query.
    """
    if (not isinstance(bounds, EpisodeBounds) or not isinstance(episode_id, str)
            or not episode_id or type(seed) is not int or seed < 0
            or type(clef_enabled) is not bool or actor_seed != ACTOR_SEED
            or type(actor_seed) is not int or type(extra_actor_budget) is not int
            or not 0 <= extra_actor_budget <= 1):
        raise ValueError('Explicit proposed fixture identity, fixed actor seed and bounded budget required')
    started = clock.monotonic_ns()
    deadline = started + int(bounds.episode_seconds * 1e9)
    journal, errors, actions, frames, sources = [], [], [], [], []
    reset_bytes = {}
    decision = None
    opportunity = 'not_reached'
    candidate = None
    changed = 0
    cleanup_used_ns = 0
    resources = []
    for name, resource in (('environment', env), ('actor', actor), ('scorer', scorer)):
        if resource is not None and all(resource is not owned for _, owned in resources):
            resources.append((name, resource))

    def note(row, error):
        errors.append({'operation': row['kind'], 'phase': row.get('phase'),
                       'error_type': type(error).__name__, 'message': str(error)[:1000],
                       'journal_index': row['index']})

    def invoke(kind, function, seconds, *, owner=None, phase=None, metadata=None,
               cleanup=False):
        nonlocal cleanup_used_ns
        began = clock.monotonic_ns()
        if not cleanup and began >= deadline:
            raise _OperationError('Episode deadline expired before ' + kind)
        limit = int(seconds * 1e9)
        cleanup_exhausted = cleanup and cleanup_used_ns >= int(bounds.cleanup_seconds * 1e9)
        if not cleanup:
            limit = min(limit, deadline - began)
        else:
            limit = min(limit, max(1, int(bounds.cleanup_seconds * 1e9)-cleanup_used_ns))
        row = {'index': len(journal), 'kind': kind, 'phase': phase,
               'submitted_ns': began, 'deadline_ns': began + limit, 'status': 'submitted',
               **(metadata or {})}
        journal.append(row)
        try:
            value = function()
            if cleanup_exhausted or clock.monotonic_ns() >= began + limit:
                raise TimeoutError(kind + ' cooperative deadline expired')
        except Exception as error:
            row.update(status='timeout' if isinstance(error, TimeoutError) else 'failed',
                       completed_ns=clock.monotonic_ns(), error_type=type(error).__name__)
            note(row, error)
            if cleanup:
                cleanup_used_ns += clock.monotonic_ns()-began
            if owner is not None:
                try:
                    invoke('cancel', lambda: owner.cancel(), bounds.cleanup_seconds,
                           phase=kind, cleanup=True)
                except Exception:
                    # The cancellation row retains the secondary failure separately.
                    pass
            raise _OperationError(kind + ' failed') from error
        row.update(status='completed', completed_ns=clock.monotonic_ns())
        if cleanup:
            cleanup_used_ns += clock.monotonic_ns()-began
        return value

    def reset():
        frame = env.reset(seed)
        if not isinstance(frame, Frame) or frame.step != 0:
            raise ValueError('Audited reset Frame at control zero required')
        raw = env.reset_components()
        if (not isinstance(raw, dict) or set(raw) != COMPONENTS
                or any(type(value) is not bytes for value in raw.values())
                or {key: digest(value) for key, value in raw.items()} != dict(frame.components)):
            raise ValueError('Exact reset component bytes must match the audited Frame')
        reset_bytes.update(raw)
        frames.append(frame)
        return frame

    def inputs():
        def capture():
            state, images, instruction = env.inputs()
            detached = (np.array(state, copy=True),
                        {key: np.array(value, copy=True) for key, value in images.items()}, instruction)
            return detached, input_hash(detached)
        value = invoke('input', capture, bounds.input_seconds, owner=env,
                       metadata={'completed_control': len(actions)})
        journal[-1]['input_sha256'] = value[1]
        return value

    def ask(snapshot, snapshot_hash, phase):
        nonlocal candidate
        caps = _counts(journal)
        if (caps['actor']['submitted'] >= 38 + extra_actor_budget
                or phase == 'extra' and caps['actor_extra']['submitted'] >= extra_actor_budget):
            raise _OperationError('Actor query budget exhausted; no retry')
        chunk = invoke('actor', lambda: native_actions(actor(snapshot, actor_seed), chunk=True),
                       bounds.actor_seconds, owner=actor, phase=phase,
                       metadata={'completed_control': len(actions), 'seed': actor_seed,
                                 'input_sha256': snapshot_hash})
        journal[-1]['chunk_sha256'] = digest(canonical({'actions': chunk}))
        if phase == 'extra':
            candidate = chunk
        return chunk

    def execute(action, source, offset):
        native = native_actions(action)
        row_index = len(journal)
        def step():
            frame = env.step(native)
            if (not isinstance(frame, Frame) or frame.step != len(actions) + 1
                    or frame.time_ns <= frames[-1].time_ns or frames[-1].stopped):
                raise ValueError('Contiguous audited post-control Frame required')
            actions.append(native)
            frames.append(frame)
            sources.append({'control': len(actions)-1, 'source': source, 'chunk_offset': offset})
            journal[row_index]['retained_in_tape'] = True
            return frame
        return invoke('step', step, bounds.step_seconds, owner=env,
                      metadata={'control': len(actions), 'action': native,
                                'retained_in_tape': False})

    try:
        invoke('reset', reset, bounds.reset_seconds, owner=env, metadata={'seed': seed})
        queue, offset, queue_source = (), 0, 'stock'
        while len(actions) < HORIZON and not frames[-1].stopped:
            if not queue or offset == len(queue):
                snapshot, snapshot_hash = inputs()
                phase = 'initial' if not actions else 'normal'
                queue = ask(snapshot, snapshot_hash, phase)
                offset, queue_source = 0, 'stock'
            if clef_enabled and len(actions) == 8 and opportunity == 'not_reached':
                opportunity = 'submitted'
                snapshot, snapshot_hash = inputs()

                class LoggedScorer:
                    def __call__(self):
                        return invoke('scorer', lambda: validate_answers(scorer(snapshot), probability_decimals=4),
                                      bounds.scorer_seconds, owner=scorer,
                                      metadata={'completed_control': 8, 'input_sha256': snapshot_hash})
                    def cancel(self):
                        invoke('cancel', scorer.cancel, bounds.cleanup_seconds, phase='scorer', cleanup=True)

                class LoggedActor:
                    def __call__(self, value, chosen_seed):
                        return ask(value, snapshot_hash, 'extra')
                    def cancel(self):
                        invoke('cancel', actor.cancel, bounds.cleanup_seconds, phase='actor', cleanup=True)

                class AuditedEnvironment:
                    def step(self, action):
                        return execute(action, 'gate_unresolved', len(actions)-8)

                before = len(actions)
                remaining = (deadline - clock.monotonic_ns()) / 1e9
                if remaining <= 0:
                    raise TimeoutError('Episode deadline expired before handoff')
                decision = gated_rollout(AuditedEnvironment(), LoggedScorer(), LoggedActor(), snapshot,
                    queue, observed_frame=8, boundary=8, seed=actor_seed, steps=8,
                    remaining_queries=extra_actor_budget, policy=GatePolicy(probability_decimals=4),
                    timeout_seconds=min(bounds.handoff_seconds, remaining), clock_mode='paused_synchronous')
                opportunity = decision['gate']['reason'] if decision['gate'] else 'failed'
                for index, source in enumerate(decision['action_sources'], before):
                    sources[index] = source
                changed += decision['changed_controls']
                if decision['error_type'] or errors:
                    raise _OperationError('Scorer/actor infrastructure failure; no retry')
                if decision['committed_steps'] is not None:
                    # Runner consumed candidate offsets0..7. Keep offsets8..15,
                    # then query normally at control24; never reuse the old expiry.
                    queue, offset, queue_source = candidate, len(actions)-before, 'candidate'
                else:
                    offset += len(actions)-before
                continue
            execute(queue[offset], queue_source, offset)
            offset += 1
    except Exception as error:
        if not isinstance(error, _OperationError) or not errors:
            errors.append({'operation': 'episode', 'error_type': type(error).__name__,
                           'message': str(error)[:1000]})
    finally:
        cleanup_started = clock.monotonic_ns()
        cleanup_deadline = cleanup_started + max(0, int(bounds.cleanup_seconds * 1e9)-cleanup_used_ns)
        for name, resource in reversed(resources):
            try:
                # Always attempt every close, even when earlier cleanup exhausted
                # its cooperative budget; late completion is never called success.
                seconds = max(1e-9, (cleanup_deadline-clock.monotonic_ns()) / 1e9)
                invoke('cleanup', lambda: resource.close(), seconds, phase=name, cleanup=True)
            except Exception:
                pass
        if clock.monotonic_ns() >= deadline:
            errors.append({'operation': 'episode', 'error_type': 'TimeoutError',
                           'message': 'Episode core wall deadline including cleanup expired'})
    tape = Tape(episode_id, seed, tuple(actions), tuple(frames)) if frames else None
    end = frames[-1] if frames else None
    accounting = {'episode_id': episode_id, 'seed': seed, 'arm': ARMS[int(clef_enabled)],
        'controls': len(actions), 'horizon': HORIZON, 'horizon_reached': len(actions) == HORIZON,
        'success': bool(end and end.success), 'terminated': bool(end and end.terminated),
        'truncated': bool(end and end.truncated), 'policy_stopped': bool(end and end.stopped),
        'infrastructure_valid': not errors, 'status': 'infrastructure_invalid' if errors else
            'policy_stopped' if end.stopped else 'horizon',
        'opportunity': opportunity, 'changed_controls_at_handoff': changed,
        'attributable_action_change': changed > 0, 'counts': _counts(journal),
        'journal': journal, 'errors': errors, 'action_sources': sources,
        'gate': {key: value for key, value in decision.items() if key not in ('actions', 'frames')}
            if decision else None,
        'reset_components': {key: {'base64': base64.b64encode(value).decode('ascii'),
                                  'sha256': digest(value), 'bytes': len(value)}
                             for key, value in sorted(reset_bytes.items())},
        'bounds': asdict(bounds), 'started_ns': started, 'finished_ns': clock.monotonic_ns(),
        'wall_ns': clock.monotonic_ns()-started, 'cleanup_wall_ns': cleanup_used_ns}
    return {'tape': tape, 'accounting': accounting, 'provenance': {
        'kind': 'injected_cpu_episode_fixture', 'task': 'CoffeeSetupMug', 'horizon': HORIZON,
        'execute': 16, 'clock_mode': 'paused_synchronous', 'decision_control': 8,
        'actor_seed_per_query': actor_seed, 'proposal_selected': False, 'paid_authorized': False,
        'frame_time_scope': 'adapter simulation timestamps; wall time is separately journaled',
        'deadline_enforcement': 'cooperative calls; qualified outer process bounds required',
        'evidence_scope': 'CPU control-flow doubles; no real-model/simulator/storage readiness'}}


class _FixtureClock:
    def __init__(self):
        self.now = 1_000_000_000
        self.lock = threading.Lock()
    def monotonic_ns(self):
        with self.lock:
            return self.now
    def advance(self, milliseconds):
        with self.lock:
            self.now += int(milliseconds * 1e6)
    def sleep(self, seconds):
        raise AssertionError('Paused fixtures must not sleep')


def _fixture_chunk(seed, control):
    value = np.empty((16, 12), dtype=np.float32)
    for offset in range(16):
        value[offset] = [(seed % 10) + (control+offset)/1000 + axis/10000
                         + (.5 if control == 8 else 0) for axis in range(12)]
    return native_actions(value, chunk=True)


class _FixtureEnvironment:
    def __init__(self, clock, *, stop_after=None, stop_kind='terminated', step_ms=80):
        self.clock, self.step_ms, self.stop_after, self.stop_kind = clock, step_ms, stop_after, stop_kind
        self.closed = False
    def reset(self, seed):
        self.seed, self.control = seed, 0
        self.raw = {key: canonical({'component': key, 'seed': seed}) for key in COMPONENTS}
        self.physics = self.raw['physics']
        self.clock.advance(2)
        return Frame(0, 0, tuple((key, digest(value)) for key, value in self.raw.items()))
    def reset_components(self):
        return dict(self.raw)
    def inputs(self):
        self.clock.advance(1)
        # Tiny synthetic native arrays, deliberately not official preprocessing.
        state = np.zeros((4, 14), dtype=np.float32)
        state[:, 0], state[:, 1] = self.seed, self.control
        images = {key: np.full((4, 4, 4, 3), self.control % 256, dtype=np.uint8)
                  for key in ('front', 'left', 'right')}
        return state, images, 'fixture only: CoffeeSetupMug'
    def step(self, action):
        self.clock.advance(self.step_ms)
        self.control += 1
        self.physics = canonical({'prior': digest(self.physics), 'action': action})
        components = dict(self.raw, physics=self.physics)
        stopped = self.control == self.stop_after
        return Frame(self.control, self.control * 50_000_000,
                     tuple((key, digest(value)) for key, value in components.items()),
                     success=stopped and self.stop_kind == 'success',
                     terminated=stopped and self.stop_kind == 'terminated',
                     truncated=stopped and self.stop_kind == 'truncated')
    def cancel(self):
        self.clock.advance(1)
    def close(self):
        self.closed = True
        self.clock.advance(1)


class _FixtureActor:
    def __init__(self, clock):
        self.clock, self.closed = clock, False
    def __call__(self, inputs, seed):
        self.clock.advance(3)
        return _fixture_chunk(int(inputs[0][0, 0]), int(inputs[0][0, 1]))
    def cancel(self):
        self.clock.advance(1)
    def close(self):
        self.closed = True
        self.clock.advance(1)


class _FixtureScorer:
    def __init__(self, clock):
        self.clock, self.closed = clock, False
    def __call__(self, inputs):
        self.clock.advance(5)
        return {'timing': {'type': 'choice', 'choice': 'replan', 'confidence': .9,
                          'probabilities': {'replan': .9, 'continue': .1, 'uncertain': 0}},
                'data_usefulness': {'type': 'choice', 'choice': 'uncertain', 'confidence': 1,
                                   'probabilities': {'useful': 0, 'not_useful': 0, 'uncertain': 1}}}
    def cancel(self):
        self.clock.advance(1)
    def close(self):
        self.closed = True
        self.clock.advance(1)


def run_fixture_pilot(*, bounds=None):
    """Four full CPU-double episodes on provisional seeds; never selects a live pilot."""
    from unittest.mock import patch
    bounds = bounds or EpisodeBounds(120, .2, .2, .2, .2, .2, .5, 3)
    episodes = []
    for seed, order in ((1152, ARMS), (1153, tuple(reversed(ARMS)))):
        for arm in order:
            clock = _FixtureClock()
            env, actor = _FixtureEnvironment(clock), _FixtureActor(clock)
            scorer = _FixtureScorer(clock) if arm == 'xiaomi_clef' else None
            with patch('coppertop.decision_runner.time', clock):
                episodes.append(run_episode(env, actor, scorer,
                    episode_id=f'provisional-dev{seed}/{arm}', seed=seed,
                    clef_enabled=arm == 'xiaomi_clef', bounds=bounds, clock=clock))
    resets_match = all(set(episodes[index]['accounting']['reset_components']) == COMPONENTS and
                       episodes[index]['accounting']['reset_components'] ==
                       episodes[index+1]['accounting']['reset_components'] for index in (0, 2))
    stock_parity = all(result['tape'] is not None and result['tape'].actions == tuple(
        _fixture_chunk(result['tape'].seed, control)[offset]
        for control in range(0, HORIZON, 16) for offset in range(min(16, HORIZON-control)))
        for result in episodes if result['accounting']['arm'] == 'xiaomi_only')
    totals = {key: sum(result['accounting']['counts'][key]['submitted'] for result in episodes)
              for key in ('actor', 'actor_initial', 'actor_normal', 'actor_extra', 'scorer', 'input', 'step')}
    changes = all(result['accounting']['attributable_action_change']
                  for result in episodes if result['accounting']['arm'] == 'xiaomi_clef')
    accounting = {'status': 'fixture_passed' if resets_match and stock_parity and changes and
        totals['actor'] <= 154 and totals['scorer'] <= 2 and
        all(result['accounting']['infrastructure_valid'] and result['accounting']['horizon_reached']
            for result in episodes) else 'fixture_failed',
        'episode_order': [result['accounting']['episode_id'] for result in episodes],
        'provisional_seeds': [1152, 1153], 'unused_seed_verified': False,
        'matching_reset_component_bytes': resets_match, 'stock_fixture_parity': stock_parity,
        'attributable_fixture_changes': changes,
        'totals': totals, 'controls': sum(result['accounting']['controls'] for result in episodes),
        'actor_cap': 154, 'scorer_cap': 2, 'automatic_retries': 0}
    return {'episodes': tuple(episodes), 'tapes': tuple(result['tape'] for result in episodes),
            'accounting': accounting, 'provenance': {
                'kind': 'four_episode_cpu_fixture', 'proposal_selected': False,
                'paid_authorized': False, 'real_model_execution': False,
                'real_simulator_execution': False, 'fixture_bounds': asdict(bounds),
                'limits': 'No official preprocessing/loader, runtime parity, unused-seed ledger, remote storage or live quote qualification'}}
