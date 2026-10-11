"""One bounded scorer-mediated handoff; caller owns simulator, providers and cleanup."""
import threading
import time

from .decision_gate import GatePolicy
from .mechanism import native_actions


def candidate_admission(*, observed_frame, current_frame, candidate_controls=16,
                        max_age_controls=2):
    """Check actual control-index age again at arrival; never replay expired offsets."""
    if (any(type(value) is not int or value < 0 for value in
            (observed_frame, current_frame, candidate_controls, max_age_controls))
            or current_frame < observed_frame or candidate_controls != 16
            or max_age_controls > 8):
        raise ValueError('Bounded completed-control indices required')
    elapsed = current_frame - observed_frame
    reason = ('exhausted_candidate_suffix' if elapsed >= candidate_controls else
              'stale_candidate' if elapsed > max_age_controls else 'candidate_ready')
    return {'reason': reason, 'apply': reason == 'candidate_ready',
            'age_controls': elapsed, 'discarded_offsets': min(elapsed, candidate_controls)}


def gated_rollout(env, scorer, query, inputs, old_chunk, *, observed_frame, boundary,
                  seed, steps, remaining_queries, control_ns=50_000_000,
                  policy=GatePolicy(), timeout_seconds=5, clock_mode='continuous_async',
                  enabled=True, on_action_selected=None):
    """Scorer and candidate query run while old controls advance, with no physics pause.

    An explicit paused_synchronous mode freezes controls during scorer/actor work
    for functional tests and reports that pause; it makes no real-time claim.
    scorer() returns validated-answer-shaped judgments; scorer.cancel and query.cancel
    must interrupt their owned transports. The caller supplies detached current inputs
    and authenticates scorer input/receipt bindings. This owns a SINGLE gate/query;
    admission/billing authorization and episode-wide budgets belong to the caller.
    on_action_selected receives source/offset/change attribution before env.step;
    it reports a selection, not execution. The caller retains it only with a valid
    audited post-control Frame, including a late Frame that precedes a timeout.
    """
    old = native_actions(old_chunk, chunk=True)
    if (type(observed_frame) is not int or observed_frame < boundary
            or type(boundary) is not int or not 0 < boundary < 16
            or type(steps) is not int or not 0 < steps <= 16 - boundary
            or type(remaining_queries) is not int or remaining_queries < 0
            or type(control_ns) is not int or control_ns <= 0
            or type(timeout_seconds) not in (int, float) or not 0 < timeout_seconds <= 30
            or clock_mode not in ('continuous_async', 'paused_synchronous')
            or type(enabled) is not bool
            or on_action_selected is not None and not callable(on_action_selected)):
        raise ValueError('Bounded native handoff required')
    submitted = time.monotonic_ns()
    deadline = submitted + int(timeout_seconds * 1e9)
    outcome = {'scorer_calls': 0, 'query_calls': 0}
    ready = threading.Event()
    closed = threading.Event()
    lock = threading.Lock()
    current_control = observed_frame

    def work():
        try:
            outcome['scorer_calls'] = 1
            answers = scorer()
            gate_finished = time.monotonic_ns()
            outcome['gate_latency_ns'] = gate_finished - submitted
            with lock:
                decision = policy.decide(answers, observed_frame=observed_frame,
                    current_frame=current_control,
                    queued_until=observed_frame + steps, remaining_queries=remaining_queries)
                outcome['decision'] = decision
                admitted = (decision['request_early_replan'] and not closed.is_set()
                            and gate_finished < deadline)
                if admitted:
                    outcome['query_calls'] = 1
            if admitted:
                actor_started = time.monotonic_ns()
                try:
                    outcome['chunk'] = native_actions(query(inputs, seed), chunk=True)
                finally:
                    outcome['actor_latency_ns'] = time.monotonic_ns() - actor_started
        except BaseException as error:
            outcome['error_type'] = type(error).__name__
        finally:
            outcome['finished_ns'] = time.monotonic_ns()
            ready.set()

    worker = None
    if enabled:
        worker = threading.Thread(target=work, daemon=True, name='decision-owned-query')
        worker.start()
    actions, frames, timings, action_sources = [], [], [], []
    changed_controls = 0
    committed = None
    arrival = None
    pause_ns = 0
    primary_error = None
    try:
        if clock_mode == 'paused_synchronous' and enabled:
            if not ready.wait(timeout_seconds):
                raise TimeoutError('Paused scorer/actor branch deadline expired')
            pause_ns = time.monotonic_ns() - submitted
            if pause_ns >= int(timeout_seconds * 1e9):
                raise TimeoutError('Paused scorer/actor branch deadline expired')
        controls_started = time.monotonic_ns() if clock_mode == 'paused_synchronous' else submitted
        for tick in range(steps):
            now = time.monotonic_ns()
            if now >= deadline:
                raise TimeoutError('Scorer/actor branch deadline expired')
            if (clock_mode == 'continuous_async'
                    and now >= controls_started + (tick + 1) * control_ns):
                raise RuntimeError('Scheduled control boundary missed')
            if ready.is_set() and 'chunk' in outcome and committed is None and arrival is None:
                required = (0 if clock_mode == 'paused_synchronous' else
                    (outcome['finished_ns'] - submitted + control_ns - 1) // control_ns)
                if tick < required and time.monotonic_ns() >= submitted + required * control_ns:
                    raise RuntimeError('Simulator pacing overrun invalidates decision delay')
                if tick >= required:
                    arrival = candidate_admission(observed_frame=observed_frame,
                        current_frame=observed_frame + tick,
                        max_age_controls=policy.max_age_controls)
                    if arrival['apply']:
                        committed = tick
            action = old[boundary + tick] if committed is None else outcome['chunk'][tick]
            changed_action = action != old[boundary + tick]
            source = {'control': observed_frame + tick,
                'source': 'old_queue' if committed is None else 'candidate',
                'chunk_offset': boundary + tick if committed is None else tick}
            if on_action_selected is not None:
                on_action_selected({**source, 'changed_action': changed_action})
            began = time.monotonic_ns()
            frame = env.step(action)
            finished = time.monotonic_ns()
            with lock:
                current_control += 1
            if finished >= deadline:
                raise TimeoutError('Control/audit branch deadline expired')
            if clock_mode == 'continuous_async' and finished - began > control_ns:
                raise RuntimeError('Control/audit exceeded branch pacing bound')
            actions.append(action)
            frames.append(frame)
            action_sources.append(source)
            changed_controls += changed_action
            timings.append({'control': observed_frame + tick, 'started_ns': began, 'finished_ns': finished})
            if frame.stopped:
                break
            delay = (controls_started + (tick + 1) * control_ns - time.monotonic_ns()) / 1e9
            if delay > 0 and clock_mode == 'continuous_async':
                time.sleep(min(delay, max(0, (deadline - time.monotonic_ns()) / 1e9)))
    except BaseException as error:
        primary_error = error
        raise
    finally:
        with lock:
            closed.set()
        cleanup_errors = []
        first_cleanup_error = None

        def cleanup_failure(operation, error):
            nonlocal first_cleanup_error
            if first_cleanup_error is None:
                first_cleanup_error = error
            cleanup_errors.append({'operation': operation,
                'error_type': type(error).__name__, 'message': str(error)[:1000]})

        alive = False
        if worker is not None:
            try:
                alive = worker.is_alive()
            except BaseException as error:
                alive = True
                cleanup_failure('worker_status', error)
        if alive:
            for name, owned in (('scorer', scorer), ('actor', query)):
                try:
                    owned.cancel()
                except BaseException as error:
                    cleanup_failure('cancel_' + name, error)
        if worker is not None:
            try:
                worker.join(timeout=1)
            except BaseException as error:
                cleanup_failure('worker_join', error)
            try:
                if worker.is_alive():
                    cleanup_failure('worker_alive', RuntimeError('Owned worker still alive'))
            except BaseException as error:
                cleanup_failure('worker_status', error)
        if cleanup_errors:
            error = primary_error if primary_error is not None else RuntimeError(
                'Owned scorer/actor cancellation failed; stop worker process')
            error.runner_cleanup_errors = cleanup_errors
            for failure in cleanup_errors:
                error.add_note('Owned scorer/actor cleanup failed; stop worker process: '
                    + failure['operation'] + ': ' + failure['error_type'] + ': ' + failure['message'])
            if primary_error is None:
                raise error from first_cleanup_error
    return {'actions': tuple(actions), 'frames': tuple(frames), 'control_timing': timings,
            'action_sources': action_sources, 'changed_controls': changed_controls,
            'attributable_action_change': changed_controls > 0,
            'committed_steps': committed, 'scorer_calls': outcome['scorer_calls'],
            'query_calls': outcome['query_calls'], 'gate': outcome.get('decision'),
            'gate_latency_ns': outcome.get('gate_latency_ns'),
            'actor_latency_ns': outcome.get('actor_latency_ns'),
            'error_type': outcome.get('error_type'),
            'late_candidate_discarded': 'chunk' in outcome and committed is None,
            'candidate_admission': arrival, 'clock_mode': clock_mode, 'pause_ns': pause_ns,
            'clef_enabled': enabled,
            'wall_ns': time.monotonic_ns() - submitted,
            'queue_exhaustion': 'caller must stop or perform a separately bounded normal replan; no expired-control replay',
            'query_seed': seed, 'submitted_ns': submitted, 'finished_ns': outcome.get('finished_ns'),
            'outcome_scope': 'one-boundary handoff; no task-success or intervention-benefit inference'}
