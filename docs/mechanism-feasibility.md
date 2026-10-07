# Frozen policy, delayed replanning: preparation gates

Status: minimal executable feasibility harness on an already allocated cached
worker. No live model/simulator execution, new compute, provisioning, credential
changes, learned gate, cache or world model were performed during preparation.
Official simulator replay and decoded candidate diversity remain unverified.
This draft does not authorize a merge or paid launch.

## What the completed pilot can support

The four-rollout report `b9fd2fa7473a6d3b7ae66905e8548083320d117bf5f9d108e4ea9ae99aaa778a`
contains seeds, initial-observation hashes, outcomes, steps, query counts, and
summed inference seconds. It contains no action/observation tapes, per-query
latencies, camera video, controller/task/RNG snapshots, or input histories.
The cloud wrapper returned the aggregate report from temporary scratch.
Consequently there is no retained trajectory from which to branch. Aggregate
inference time divided by query count is not a latency distribution.

## Pinned source findings

The companion source audit records exact upstream revisions, Git blobs and SHA256
digests; it adds source metadata, not third-party code or assets.

* RoboSuite `MjSimState` and `get_state` store time, qpos and qvel only.
  `set_state` restores those fields. This excludes actuator/control state, solver
  warm starts, mutable model arrays, Python task/controller state and RNG.
* RoboCasa CoffeeMachine holds `_turned_on` outside physics state and changes
  `model.site_rgba`. Kitchen reset samples placements and settles controllers;
  layout, style, fixture state, episode metadata and constructor RNG matter.
* OSC goals depend on previously desired goals; hybrid base mode changes goal
  interpretation. Composite controller action dictionaries, interpolators, robot
  recent buffers, and controller caches must be audited.
* Observable sampling delay, cached values, sample clock, and environment cache
  evolve separately. Fingerprinting must not call an observation update that
  itself changes these values.
* Xiaomi histories hold seven controls, sampled to four observations at interval
  two. The queue emits one native 12-control action at a time from 16-step chunks.
  A boundary follows a completed control, never an inner MuJoCo substep.
* Pinned `modeling_mibot.py` Git blob `97c67990149b78594c2f74094157b59e82df57e7`
  initializes fresh `torch.randn_like` noise in forward. This establishes a
  stochastic mechanism in source, not actual decoded candidate diversity.

Use fresh isolated simulator construction, reset, and exact action-prefix replay
as the first restoration strategy. Seed global Python/NumPy randomness as well
as environment RNG, reproduce constructor seed/config and episode seed, and
recreate cameras/rendering consistently. Do not deepcopy or fork an active EGL
process. Compare every recorded reset/prefix frame, then a known identical
continuation. Passing a short continuation is bounded empirical parity; it does
not establish equivalence for every future action.

## Implemented local seam

`coppertop.mechanism` accepts only finite float32 native 16x12 chunks and 12-control
actions in EE position, EE rotation, gripper, base4, mode order. Chords remain
analytics segments; they do not replace those controls. History/crop/padding
remain model views.

`Tape` records reset and each post-control frame, increasing control timestamps,
explicit success/termination/truncation, and hashes for physics, mutable model,
task, controller, RNG, observables, history and observation. `replay` compares a
fresh reset, full prefix and nonempty identical suffix, closes the owned adapter
on either outcome, and fails on any component mismatch. Component names are a
coverage contract, not proof that an adapter hashed every necessary field.
`publish` writes a small immutable audit sidecar through the existing ObjectStore;
it is not a replacement for Parquet signals or compressed camera videos.

`delayed_branches` checks a completed-control boundary and equal-duration arms.
Both arms execute the same pending controls for ceil(query latency/control
period) ticks. Only after arrival does replan use the candidate suffix, discarding
predictions for already elapsed ticks. This unconditioned suffix strategy is a
testable baseline; it is not RTC prefix conditioning. Reject experiments whose
old queue expires before arrival or whose comparison window has no post-arrival
control. No invented holds or free paused physics.

This helper plans an offline schedule. `mechanism_runner.delayed_rollout` now
implements the asynchronous schedule with one owned query thread and bounded
transport cancellation. It advances old native controls while the query runs,
hands off only at a completed-control boundary, and discards stale candidate
indices. Per-control start/finish and query submit/finish times are logged.
Controls that exceed the real-time period or final deadline invalidate the run;
slow rendering/audit cannot make inference look cheaper in simulated time.
`candidate_diagnostics` compares a same-input/same-seed repeat and different-seed
chunk, reports hashes and counts all three diagnostic calls (two discarded).
The caller must independently log inputs and seeds; array equality alone cannot
authenticate them. Also compare converted executable controls and the actually
used suffix: diversity confined to discarded timesteps provides no intervention.

## Runnable harness and remaining live gates

`mechanism_robocasa.RoboCasaReplay` verifies the existing pinned dependency/source/
asset gates before importing the official evaluator. It reproduces global Python
and NumPy episode seeds, constructor seed1000, official pretrain task and episode
seed1150. It hashes MuJoCo `mjSTATE_INTEGRATION`, all exposed model arrays,
readable task/fixture/controller/buffer/RNG/observable fields, native observations
and detached seven-entry history. It records opaque/depth-limited fields as
omissions; `complete_state_certified` stays false. Fresh replay is the strategy,
not serializing those audit hashes as restorable snapshots.

`mechanism_policy.OfficialQuery` uses the pinned official processor, prompt/crop/
state-padding/decode path and trusted job-local loopback client/server protocol.
The owned wrapper injects a per-call seed, strips it before official forward, and
restores CPU/CUDA RNG with `fork_rng` on the single native GPU. There are no weight
updates. Connection/read timeouts are five seconds; cancellation shuts down only
that socket. The native process receives an environment allowlist without storage
credentials and is terminated as an owned process group on every exit.

`mechanism_runtime.experiment` executes one baseline chunk, saves the query-start
snapshot after four controls, collects its remaining12 controls, and makes three
same-input diagnostic calls (same-seed pair and distinct seed). A fresh simulator
must match reset, prefix4 and identical continuation12 exactly. A third fresh
simulator then reconstructs the prefix and executes a same-seed async candidate.
Its committed prefix must match the baseline and its executed suffix must differ.
All five calls, including three discarded diagnostics, have input/chunk hashes,
seed and monotonic timings. Submitted/failed calls remain in a durable journal.
This is one-boundary feasibility, not episode success or utility evidence.

The collector saves native state/action/index signals in Parquet, compressed
per-camera H264 videos and immutable audit sidecars in a single ObjectStore.
Both suffix manifests reference one shared prefix object set; no duplicated full
prefix videos. Video timestamps map through segment start/control clock; lossy
encoding is documented while exact source-camera hashes remain in the tape.
All siblings use the same root episode grouping for later split assignment.

Actual RoboCasa and CUDA execution is still unrun. Blocking live gates:

1. Exact reset/prefix/continuation parity across fresh adapter instances, including
   physics, camera/state, success, latent-state audit and query-input hashes.
   Audit omissions are retained for review; this is bounded empirical parity,
   never a claim of complete restoration for arbitrary future actions.
2. Pinned official CPU/CUDA seed-repeat behavior and decoded distinctness; async
   candidate must reproduce the diagnostic seed and differ in the used suffix.
3. Actual simulation/render/audit throughput within the native control period;
   actual transport/inference delay must leave a usable suffix. Failure stops
   the session without retries or queue-holding fallback.
4. Provider binding to the retained image plus this exact code/plan, a secretfree
   worker and CPU-staged immutable R2 publication. This local harness writes
   content-addressed outputs; it does not allocate a provider job or publish R2.
5. Fresh provider rates/retained holds, explicit cumulative spend ceiling and
   cleanup receipts before any paid launch. No dollar figure is invented here.

## Scientific gate and first bounded proposal

Test the counterfactual net benefit of a delayed query with Xiaomi frozen. Both
branches share the state, old committed actions during delay and outcome horizon.
Measure success, completion steps, elapsed time and total compute independently;
predeclare the cost penalty before using a combined utility. A short branch's
geometric progress is not evidence of episode success or training benefit.

The runnable workload is a *feasibility-only* session using the retained repaired
image: one DEV episode, one boundary after four completed controls, five total
inference calls, one reset/prefix4/continuation12 parity replay, and one matched
continue/replan comparison of at most12 controls after query start. If delay
leaves no usable suffix or a pacing gate fails, record infeasible and stop. No
training, no retries, no asset/model downloads or rebuilds. All outputs need a
size cap and durable receipts before temporary storage cleanup.

This is a workload bound, not a spend authorization. The prepared manifest binds
all package source files, dependency locks and model pins, with a240-second
session,90-second server startup,5-second query timeout,24MiB output limit and
zero retries. Cleanup can consume an additional10 seconds. Provider binding,
current rates/holds and a cumulative dollar ceiling remain prerequisites.
No dollar figure is invented here. Gate on replay, diversity and latency first;
do not spend on the headroom experiment until those pass.

If feasibility passes, collect episode-length paired outcomes and compare native
fixed cadence, validation-tuned periodic cadence, progress/deviation trigger and
random budget-matched trigger. An offline best-branch oracle is only a headroom
upper bound. Split whole episodes/seeds/scenes with siblings kept together and
count discarded inference. Only measured headroom justifies a small decision
model. Policy finetuning comes later with neither/policy-only/decision-only/both
ablation at matched budgets.

Prior art narrows the claim: [TempoWAM](https://arxiv.org/html/2608.09492v1)
studies adaptive progress monitoring; [CA³C](https://arxiv.org/html/2610.07949v1)
studies candidate consequences and commitment on RoboCasa, including CoffeeSetupMug;
[RTC](https://arxiv.org/abs/2506.07339) studies asynchronous committed-prefix
execution. Broad adaptive replanning is not a novelty claim here.

## Verification

CPU tests demonstrate latent-state replay failures, successful full-prefix replay
in a toy environment, per-component mismatch detection, closure on failure,
native action preservation, boundary/delay guards and immutable sidecar identity.
They do not execute RoboCasa, official Xiaomi, or live async inference.

Preparation verification is recorded in the PR and local handoff. Tests include
full fake collector/replay/async integration, real Parquet/video output, shared
prefix references, failed-call accounting, fast-query handoff, final-control
pacing/deadline rejection and owned cancellation. These are not official live
simulator/CUDA checks.

## Commands on a future approved existing worker

Preparation is CPU-only and allocates nothing:

```sh
/work/sim/bin/python -m coppertop.mechanism_runtime --prepare > mechanism-plan.json
```

After a separately approved provider session stages the reviewed package at
`/app` over the retained image, the following is the executable worker payload.
It consumes the prepared manifest's SHA, validates it against the staged code,
and starts only its own native model server. The output directory must be new.

```sh
HF_HUB_OFFLINE=1 TRANSFORMERS_OFFLINE=1 MUJOCO_GL=egl PYTHONPATH=/app \
  /work/sim/bin/python -m coppertop.mechanism_runtime --execute-local \
  --plan-sha256 <SHA256_FROM_PREPARED_MANIFEST> --output /tmp/mechanism-feasibility
```

No command above creates cloud infrastructure, uploads data, or authorizes spend.
CPU stage/publication and provider cleanup must wrap this payload before launch.

Local verification:20 mechanism tests passed. The source-enabled CPU suite ran
268 tests:266 passed, two optional SDK checks skipped. Tighten and independent
structure/correctness/security review completed. Timing, inherited-frame identity,
startup readiness and nullable Parquet readback findings were fixed and rechecked.
The security recursion candidate was independently refuted: its production caller
only traverses trusted pinned simulator state. Cached-model loader extraction is
deferred as advisory; existing paid-run code was preserved. No dependencies were
installed locally. Actual simulator/CUDA behavior and all live gates remain unrun.
