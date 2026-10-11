"""One bounded scorer-mediated handoff; caller owns simulator, providers and cleanup."""
import threading
import time

from .decision_gate import GatePolicy
from .mechanism import native_actions


def gated_rollout(env, scorer, query, inputs, old_chunk, *, observed_frame, boundary,
                  seed, steps, remaining_queries, control_ns=50_000_000,
                  policy=GatePolicy(), timeout_seconds=5):
    """Scorer and candidate query run while old controls advance, with no physics pause.

    scorer() returns validated-answer-shaped judgments; scorer.cancel and query.cancel
    must interrupt their owned transports. The caller supplies detached current inputs
    and authenticates scorer input/receipt bindings. This owns a SINGLE gate/query;
    admission/billing authorization and episode-wide budgets belong to the caller.
    """
    old = native_actions(old_chunk, chunk=True)
    if (type(observed_frame) is not int or observed_frame < boundary
            or type(boundary) is not int or not 0 < boundary < 16
            or type(steps) is not int or not 0 < steps <= 16 - boundary
            or type(remaining_queries) is not int or remaining_queries < 0
            or type(control_ns) is not int or control_ns <= 0
            or type(timeout_seconds) not in (int, float) or not 0 < timeout_seconds <= 30):
        raise ValueError('Bounded native handoff required')
    submitted = time.monotonic_ns()
    deadline = submitted + int(timeout_seconds * 1e9)
    outcome = {'scorer_calls': 0, 'query_calls': 0}
    ready = threading.Event()
    closed = threading.Event()
    lock = threading.Lock()

    def work():
        try:
            outcome['scorer_calls'] = 1
            answers = scorer()
            gate_finished = time.monotonic_ns()
            age = (gate_finished - submitted + control_ns - 1) // control_ns
            outcome['gate_latency_ns'] = gate_finished - submitted
            decision = policy.decide(answers, observed_frame=observed_frame,
                current_frame=observed_frame + age,
                queued_until=observed_frame + steps, remaining_queries=remaining_queries)
            outcome['decision'] = decision
            with lock:
                admitted = (decision['request_early_replan'] and not closed.is_set()
                            and gate_finished < deadline)
                if admitted:
                    outcome['query_calls'] = 1
            if admitted:
                outcome['chunk'] = native_actions(query(inputs, seed), chunk=True)
        except Exception as error:
            outcome['error_type'] = type(error).__name__
        finally:
            outcome['finished_ns'] = time.monotonic_ns()
            ready.set()

    worker = threading.Thread(target=work, daemon=True, name='decision-owned-query')
    worker.start()
    actions, frames, timings = [], [], []
    committed = None
    try:
        for tick in range(steps):
            now = time.monotonic_ns()
            if now >= deadline:
                raise TimeoutError('Scorer/actor branch deadline expired')
            if now >= submitted + (tick + 1) * control_ns:
                raise RuntimeError('Scheduled control boundary missed')
            if ready.is_set() and 'chunk' in outcome and committed is None:
                required = (outcome['finished_ns'] - submitted + control_ns - 1) // control_ns
                if tick < required and time.monotonic_ns() >= submitted + required * control_ns:
                    raise RuntimeError('Simulator pacing overrun invalidates decision delay')
                if tick >= required:
                    committed = tick
            action = old[boundary + tick] if committed is None else outcome['chunk'][tick]
            began = time.monotonic_ns()
            frame = env.step(action)
            finished = time.monotonic_ns()
            if finished >= deadline or finished - began > control_ns:
                raise RuntimeError('Control/audit exceeded branch pacing bound')
            actions.append(action)
            frames.append(frame)
            timings.append({'control': observed_frame + tick, 'started_ns': began, 'finished_ns': finished})
            if frame.stopped:
                break
            delay = (submitted + (tick + 1) * control_ns - time.monotonic_ns()) / 1e9
            if delay > 0:
                time.sleep(min(delay, max(0, (deadline - time.monotonic_ns()) / 1e9)))
    finally:
        with lock:
            closed.set()
        cancel_failed = False
        if worker.is_alive():
            for owned in (scorer, query):
                try:
                    owned.cancel()
                except Exception:
                    cancel_failed = True
        worker.join(timeout=1)
        if worker.is_alive() or cancel_failed:
            raise RuntimeError('Owned scorer/actor cancellation failed; stop worker process')
    return {'actions': tuple(actions), 'frames': tuple(frames), 'control_timing': timings,
            'committed_steps': committed, 'scorer_calls': outcome['scorer_calls'],
            'query_calls': outcome['query_calls'], 'gate': outcome.get('decision'),
            'gate_latency_ns': outcome.get('gate_latency_ns'),
            'error_type': outcome.get('error_type'),
            'late_candidate_discarded': 'chunk' in outcome and committed is None,
            'query_seed': seed, 'submitted_ns': submitted, 'finished_ns': outcome.get('finished_ns'),
            'outcome_scope': 'one-boundary handoff; no task-success or intervention-benefit inference'}
