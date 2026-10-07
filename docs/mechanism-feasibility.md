# Frozen policy, delayed replanning: preparation gates

Status: local contracts only. No new compute, model loads, cloud provisioning,
credential changes, learned gate, cache, or world model. This is not a runnable
paid experiment bundle. Official simulator replay and decoded candidate diversity
remain unverified. Do not merge or launch this draft on the strength of toy tests.

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

This helper plans an offline schedule; it neither invokes a query nor advances a
real simulator concurrently. A future runner must prove that implementation.
`candidate_diagnostics` compares a same-input/same-seed repeat and different-seed
chunk, reports hashes and counts all three diagnostic calls (two discarded).
The caller must independently log inputs and seeds; array equality alone cannot
authenticate them. Also compare converted executable controls and the actually
used suffix: diversity confined to discarded timesteps provides no intervention.

## Missing code and blocking gates

1. A pinned CoffeeSetupMug adapter implementing `ReplayEnvironment`: explicit
   per-field coverage of MuJoCo integration state, mutable model arrays, fixtures,
   episode metadata/clock, controller/interpolator/buffers, Python and NumPy RNG,
   observable caches and the seven-entry policy history. Audit unsupported fields
   rather than silently dropping them. Capture data without mutating state.
2. A baseline tape collector and isolated prefix-replay process with render parity,
   real identical-action continuation, and explicit maximum replay budget.
3. An owned inference wrapper controlling per-query CPU/CUDA RNG without modifying
   frozen weights or pinned official forward/processor semantics. Verify same
   inputs/seed replay and actual decoded/converted different-seed diversity.
4. An asynchronous query runner: immutable query-start instruction/history/camera
   references, monotonic submit/start/finish timestamps, result/input hashes and
   seed. Continue physics with the logged pending queue while inference runs.
   Result arrival at the next control boundary must be recorded, including
   process/transport overhead. Never supply a later observation to that query.
5. Native canonical signals/index export to Parquet and per-camera compressed video
   with clock, frame identity, checksums, provenance and immutable release refs.
   Audit sidecars and fork overlays share prefix objects; no duplicate raw prefix.
   R2 publication stays CPU staged, with bounded output and cleanup receipts.
6. Per-call compute accounting including repeats, discarded candidates, monitor,
   resets/replay and both branches. Stable root episode/seed/scene grouping keeps
   all sibling branches in the same train/validation/test partition.

These are live feasibility blockers, not failures of the independent CPU tests.
No official simulator or official policy execution is claimed by this draft.

## Scientific gate and first bounded proposal

Test the counterfactual net benefit of a delayed query with Xiaomi frozen. Both
branches share the state, old committed actions during delay and outcome horizon.
Measure success, completion steps, elapsed time and total compute independently;
predeclare the cost penalty before using a combined utility. A short branch's
geometric progress is not evidence of episode success or training benefit.

Once the missing runner is reviewable, propose a *feasibility-only* session using
the retained repaired image: one DEV episode, one boundary after four completed
controls, three diagnostic inference calls, one full-prefix replay with four
identical continuation controls, then one short matched continue/replan pair
with measured delay. Cap each comparison at four controls after query start;
if measured latency leaves no usable suffix, record infeasible and stop. No
training, no retries, no asset/model downloads or rebuilds. All outputs need a
size cap and durable receipts before temporary storage cleanup.

This is a workload bound, not a spend authorization. An exact image/code/plan
binding, provider rate, time limits, current retained holds, output/storage cap,
and cumulative dollar ceiling are still needed before a paid proposal is ready.
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

Preparation verification: 10 mechanism tests passed; the complete CPU suite with
the acquired official source enabled ran 258 tests, 256 passed and two optional
SDK checks skipped. Independent structure, correctness and static security review
completed. Component ordering and tape identity/frame/scalar validation findings
were fixed and rechecked; no remaining findings in this draft's scope. No new
dependencies were installed. The live blockers above remain open.
