"""Bounded asynchronous control loop; resource allocation belongs to the caller."""
import threading
import time

from .mechanism import native_actions


def delayed_rollout(env, query, inputs, old_chunk, *, boundary, seed,
                    control_ns, steps, timeout_seconds):
    """Keep stepping old controls; accept a seeded query only at control boundaries.

    query.cancel must interrupt its owned transport. One query is in flight;
    callers own environment cleanup and stop the worker process on failed cancel.
    The snapshot supplied here must be detached from evolving simulator arrays.
    """
    old = native_actions(old_chunk, chunk=True)
    if (type(boundary) is not int or not 0 < boundary < 16
            or type(steps) is not int or not 0 < steps <= 16 - boundary
            or type(control_ns) is not int or control_ns <= 0
            or not 0 < timeout_seconds <= 30):
        raise ValueError('Bounded native queue and positive timing required')
    outcome = {}
    ready = threading.Event()
    submitted = time.monotonic_ns()

    def work():
        try:
            outcome['chunk'] = native_actions(query(inputs, seed), chunk=True)
        except Exception as error:
            outcome['error'] = type(error).__name__
        finally:
            outcome['finished_ns'] = time.monotonic_ns()
            ready.set()

    worker = threading.Thread(target=work, daemon=True, name='mechanism-owned-query')
    worker.start()
    frames, actions = [], []
    timings = []
    committed = None
    deadline = submitted + int(timeout_seconds * 1e9)
    try:
        for tick in range(steps):
            if time.monotonic_ns() >= deadline:
                raise TimeoutError('Query/branch deadline expired')
            if committed is None and ready.is_set():
                if 'error' in outcome:
                    raise RuntimeError('Owned query failed: ' + outcome['error'])
                # Slow rendering/audit must not make inference appear cheaper in
                # simulated control time. Refuse a handoff before the latency's
                # committed control horizon has actually been executed.
                required = (outcome['finished_ns'] - submitted + control_ns - 1) // control_ns
                if tick < required and time.monotonic_ns() >= submitted + required*control_ns:
                    raise RuntimeError('Simulator pacing overrun invalidates query delay')
                if tick >= required:
                    committed = tick
            action = old[boundary + tick] if committed is None else outcome['chunk'][tick]
            actions.append(action)
            began = time.monotonic_ns()
            frame = env.step(action)
            finished = time.monotonic_ns()
            if finished >= deadline:
                raise TimeoutError('Control completed beyond branch deadline')
            if finished - began > control_ns:
                raise RuntimeError('Control/audit duration exceeds real-time period')
            timings.append({'control': boundary+tick, 'started_ns': began,
                            'finished_ns': finished})
            frames.append(frame)
            if frame.stopped:
                break
            # Physics advances during inference; pace subsequent control boundaries.
            delay = (submitted + (tick + 1) * control_ns - time.monotonic_ns()) / 1e9
            if delay > 0:
                time.sleep(min(delay, max(0, (deadline - time.monotonic_ns()) / 1e9)))
        if committed is None:
            raise TimeoutError('No query handoff before pending queue/comparison ended')
        return {'frames': tuple(frames), 'actions': tuple(actions),
                'committed_steps': committed, 'query_calls': 1,
                'query_seed': seed, 'submitted_ns': submitted,
                'finished_ns': outcome['finished_ns'],
                'latency_ns': outcome['finished_ns'] - submitted,
                'control_timing': timings}
    finally:
        if worker.is_alive():
            query.cancel()
        worker.join(timeout=1)
        if worker.is_alive():
            raise RuntimeError('Owned query cancellation failed; stop worker process')
