# Official evaluator → Chords

The pinned Xiaomi `evaluate_task` remains the only rollout loop. This adapter
wraps its Gym reset/step, action converter and `EvalClient.infer`; the evaluator
owns history sampling, the action queue, cadence, success checks and video.
No simulator restoration, alternate evaluator or learned gate is introduced.

Each recorded observation retains lossless immutable `.npy` arrays for its five
named native state components and three raw RGB cameras. Canonical state is the
source16D base position/quaternion, relative EE position/quaternion and gripper
state. Source dtype/shape/bytes remain in the raw objects. Parquet indexes hold
native16D state, outgoing native12D action, stable episode/frame IDs, control
clock, query/chunk offset, reward, success, termination and truncation. The final
captured observation has a null outgoing action. A bounded horizon is explicitly
a segment cut; it does not invent a source done flag or full-episode failure.

The Xiaomi14D state/history is a model view. Query sidecars reference the exact
immutable input arrays and the corresponding source frame IDs, with clamped
history validity masks. They preserve full16×12 predicted chunks, executed and
discarded offsets, and inference submit/finish timestamps. Execution sidecars
record native-step timestamps and controls whose returned observation could not
be captured. Native actions stay EEtranslation3/EErotation3/gripper1/base4/mode1;
only the pinned source converter maps these commands to the environment.

Per-camera H264 videos are derived from the retained raw RGB frames at the
20Hz control index. Exact source bytes remain available: lossy videos never
replace them. The official composite video is copied byte-for-byte into the
store when produced, with no transcoding. Its stride is1 and fps20. Timing
fields describe worker monotonic time and simulation control index separately;
synchronous recording overhead contributes to evaluator wall time. No real-time
20Hz throughput or realistic asynchronous query delay is claimed.

Records carry run/model/source revisions, code hashes, recipe and DEV grouping.
Cadence16 and8 runs share a source initialization group for split assignment but
have distinct run/frame identities. Annotation overlays and dataset releases use
the existing Chords manifest format. Raw/input/query dependencies are included
in the ordinary publication closure. Thin forks and `publish_release` work with
LocalStore; no cloud storage is written during this implementation. Generated
records retain source provenance without inferring redistribution permissions.
The existing milestone LeRobot exporter targets the acquired human episode;
this new generated-record schema is not yet a LeRobot export view.

## Failure evidence

Diagnostics capture a bounded exception type, component and12-frame traceback
using only basename/function/line. Absolute paths, locals and arbitrary exception
text are omitted. Only enumerated owned guards and a numeric replay-control
message are retained verbatim. Query failures preserve failed-call timings and
inputs. Partial releases preserve verified observations and an explicit executed
control receipt when post-control capture fails. Video cleanup drains every owned
writer; a failed encoder is omitted rather than advertised as aligned evidence.
Artifact reads reject file/directory symlinks and nonregular files, and checksum
and transfer bytes through one validated descriptor.

## Smallest proposed live acceptance

Pure preparation, using the existing CPU dataset environment:

```sh
python -c 'import json; from coppertop.official_recording import acceptance_plan; print(json.dumps(acceptance_plan(), sort_keys=True))'
```

The proposal is **one DEV CoffeeSetupMug seed1150**, constructor1000,
trial-index0 of the official50-trial schedule, frozen official Xiaomi, one query,
execution cadence16, and a **four-control prefix**. It retains the full predicted
chunk and at most five raw observations. The64-object/24MiB closure fits the
existing bounded publication primitives. It tests the data path, not episode
success, model utility or replay parity.

A later reviewed launch would reuse the cached image, CPU cache gate and one
L40S with4cores/64GiB. Proposed GPU phase270seconds, worker240 plus10seconds
owned-server cleanup, startup300seconds, no retry. A later CPU-only publisher
would use an explicitly approved new prefix, at most256requests/24MiB and a
one-hour session, immutable readback and owned resource cleanup. Current prices,
preserved holds, exact code/plan binding, retention and dollar approval must be
prepared separately. The prior consumed attempt and its approval are not reusable.
No provider job or new credential is created by this plan.

On an already separately approved worker with the owned pinned official server
ready, the payload reuses the existing rollout entrypoint:

```sh
HF_HUB_OFFLINE=1 TRANSFORMERS_OFFLINE=1 MUJOCO_GL=egl PYTHONPATH=/app \
  /work/sim/bin/python -m coppertop.performance_runtime --rollout \
  --arm official --seed 1150 --output <NEW_OWNED_RUN_DIR> \
  --record-chords --record-run-id <UNIQUE_RUN_ID> --record-horizon 4 --replan-steps 16
```

The image upload uses `runtime_image(..., recording=True)`; legacy pilot/repair
uploads remain unchanged. New helper source hashes are recorded with the run.
The command assumes approved server readiness; it does not allocate compute or
start a model. The caller must bound the whole process and publish the complete
closure before deleting temporary storage.

## Later comparison, separate from acceptance

After data-path acceptance, compare declared cadence16 versus8 on matched DEV
seeds using the same frozen model/initialization and official evaluator. Capture
full outcomes, count every query and discarded prediction, and report compute
costs: cadence8 normally spends more inference than16. A matched-seed outcome
comparison is not proof of identical counterfactual branch state. Longer rollouts
need separately reviewed storage bounds; the current24MiB raw-byte cap must fail
rather than silently drop observations. Any realistic-delay or gated experiment
comes after useful evidence, not as a prerequisite to recording.

Local fixtures validate history/action links, boundaries, raw arrays, Parquet,
video timestamps, release closure/forks, safe failures and source-byte preservation.
An optional test executes the pinned official evaluator with fake physics/model;
it is not real RoboCasa/CUDA execution. Actual simulation/model recording,
cloud publication and live cleanup remain unrun for this new path.
