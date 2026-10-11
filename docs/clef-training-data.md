# Clef training data: learn whether an early query helps

Start with outcome-labelled timing decisions for the frozen Xiaomi actor. Keep
training-data usefulness as a separate target. A robot succeeding eventually does
not tell us whether an earlier replan helped, and a model's probability is a
prediction, not a label. This document proposes collection; it does not launch
jobs, download weights, or claim that a Clef training/export path is validated.

## What we have

The saved execute-16 and execute-8 runs both succeeded on CoffeeSetupMug DEV1150.
Their first eight executed actions and first nine native states match. They
diverge thereafter, so later frame numbers do not form matched decision states.
The 27 causal cases and 54 hosted judgments are useful for input validation,
latency and disagreement analysis. They are **one initialization group and zero
labelled counterfactual intervention pairs**. Do not multiply the apparent sample
size by cameras, overlapping windows, providers or repeated calls.

The recordings preserve native signals, action chunks and observation objects.
They do not retain a complete restorable simulator/controller/RNG snapshot; the
initial fingerprint alone cannot establish exact branch replay. Existing tapes
remain development material. Saved shadow evidence is under
`.local/decision-gate/shadow-ready/run-0b7a086a158440379fda83c52d0fde45/`.

## Smallest useful collection protocol

1. Freeze the actor, simulator/image revisions, task and initialization schedule,
   normal cadence, control horizon, query budgets, continuation policy and latency
   condition. Register the boundary sampling rule before examining outcomes.
   Sample ordinary progress as well as suspected stalls or mismatches. Log the
   sampling probability and reason; scorer disagreement may enrich collection but
   must not become the only source of training cases.
2. At a completed control boundary, save a causal decision case plus a separate
   privileged replay object: physics and task state, controller internals, pending
   queue and its origin, actor history, RNG states and environment configuration.
   Determine what the simulator actually supports before claiming complete
   restoration. Verify a no-intervention restore against an uninterrupted prefix,
   including state, actions, task flags and documented rendering tolerances.
   If restoration is incomplete, replay the same prefix from reset and verify the
   branch state; otherwise mark the pair invalid for matched labels.
3. From that same verified boundary, run two continuations: **continue** keeps the
   old queue until its normal query boundary; **replan** requests one early actor
   chunk subject to the measured scorer and actor delay, freshness checks and
   elapsed-offset discard behavior of `decision_runner`. Old controls keep running
   while inference proceeds. After this one decision both branches use the same
   frozen continuation policy. Do not introduce semantic skill changes yet.
4. Use the same remaining control horizon and query ceiling in both arms. The
   early query consumes that ceiling; do not give its arm free extra calls. Record
   actual usage, including unused or late results. Couple stochastic actor draws
   with a documented query seed schedule and repeat selected pairs; do not assume
   different query sequences consume RNG identically. Randomize branch execution
   order to reduce cache/load confounding.
5. Record task success, termination and truncation separately, time to success,
   actual control count, defined task failures, call counts, wall delay and cost.
   Keep failures and incomplete branches. Infrastructure failures are invalid
   labels; task failure within the specified horizon is a valid outcome. A late
   intervention that changes no actions is retained as a no-handoff result, not
   evidence that a different action was harmless or useful.

A proposed plumbing pilot is four development initialization groups, two sampled
boundaries each, two arms and two repeated actor seed schedules: 32 continuations,
plus the source prefixes. Size and spending require a bounded run plan. This is a
schema/replay/label-diversity pilot, not enough to justify weight tuning or a
success-rate claim. Expand only after verifying matched branches and obtaining
both helpful and harmful/unhelpful decisions. Do not manufacture balanced labels
by dropping ordinary successful continuation cases.

## Latency is part of the treatment

The client shadow test exceeded the 0.4-second remaining queue on every call,
before adding actor latency. Worker-side measurements may differ. Bind each
label to a deployment/latency condition: scorer preparation, request, parsing,
actor inference, control-boundary handoff, actual observation age and remaining
queue. Fixed or sampled injected delays must be distinguished from live measured
delays. A zero-delay oracle experiment is a separate dataset condition.

An anticipatory gate observes an earlier prefix and predicts whether a later
query will help. It needs a new decision schema, target horizon and labels; do not
reuse midpoint labels or reveal the later observation. Likewise, adding temporal
images creates a new input view rather than silently changing the current
three-current-image view. Tune a smaller/local scorer only against the deployment
condition it can actually meet.

## Labels and uncertainty

Retain the paired outcome vector before deriving a class. A versioned objective
can compute `U = success - lambda_t * normalized_time_to_success
- lambda_q * actor_queries - lambda_s * scorer_cost - lambda_f * task_failures`.
Define the horizon-capped time for unsuccessful branches and all units, weights
and failure predicates before evaluating held-out data. Report the raw vector
alongside utility so that arbitrary weights cannot hide success regressions.
Success-first lexicographic selection is also a valid explicit objective.

For repeated valid pairs, derive `delta = U(replan) - U(continue)` and retain all
replicate deltas. A positive margin supports `replan`; a reliably nonpositive or
harmful effect supports `continue`; insufficient evidence supports `uncertain`.
The margin and uncertainty estimator belong to the label recipe. Two repeats do
not establish a calibrated probability or narrow confidence interval. Sparse
pilot results may remain provisional rather than becoming hard training labels.

Keep a separate `eligibility` field for stale observations, insufficient queue,
exhausted budget and unsupported replay. These runtime exclusions must not teach
the visual model that a useful scene is intrinsically a `continue` scene. An
eligible but outcome-neutral extra query can still be `continue` after its real
query cost. Include harmful interventions, normal progress, failed recoveries,
ambiguous visibility and genuine uncertainty. Human descriptions can annotate
visible events, but are not counterfactual outcome labels.

## Minimal immutable overlay

Reuse the current ObjectStore and release/fork manifests; add versioned JSON
overlays and a Parquet index, not another service. A proposed annotation contains:

| Field | Meaning |
|---|---|
| `kind`, `schema_version` | `timing_outcome_pair`, initially version 1 |
| `case_id`, `episode_id`, `frame_id` | Existing immutable causal input identity |
| `initialization_group`, `split_manifest` | Leakage grouping and frozen assignment |
| `input_view`, `question_schema` | Hashes of causal fields, image/history view and timing options |
| `replay_object`, `replay_verification` | Privileged restoration bytes and measured checks; excluded from model input |
| `collection_plan`, `selection_probability` | Frozen sampling, horizons, budgets, randomization and seed schedule |
| `latency_condition` | Deployment, requested/actual delays, ages, deadlines and handoff offsets |
| `branches` | Continue/replan episode references, outcomes, calls, costs and actual intervention status per replicate |
| `eligibility`, `invalid_reason` | Runtime feasibility and technical validity, independently recorded |
| `label_recipe`, `label_status` | Objective, margin/uncertainty method and provisional/validated status |
| `delta_samples`, `timing_label` | Derived utilities and continue/replan/uncertain; nullable when invalid |
| `provenance` | Actor/scorer identity, code/image/source revisions, licenses, native conventions and annotation author |

Hashes bind existing camera/video, Parquet and native action objects; forks reuse
them. Preserve state16 and native action12 unchanged. Do not encode model padding,
crop, normalization or action reorder into canonical truth. Physical shards group
compatible schemas/encodings. Splits, quality and task are overlays, not storage
directory partitions. The training exporter allowlists causal input fields and
the target; replay state, future frames, rewards and outcome evidence stay outside
model inputs. Validate object closure, identities, causal frame bounds, action
attribution, label recipe and cross-split grouping before producing a release.

## Splits, tuning and evaluation

Assign initialization groups before choosing windows or annotating. All branches,
repeats, revisions, camera views and crops of a group remain together. DEV1150 and
previously explored seeds 8, 9, 12, 15 and 23 remain development; group identity also
includes the task/configuration, so an integer seed alone is not a global key.
Registry entries should conservatively capture related replays even when a source
revision changes. A proposed later split is 60/20/20 of new initialization groups
for train/development/sealed test; sample count depends on observed effect variance.
Additional task/layout holdouts support broader claims, not an initialization-only
split. If a sealed group informs debugging or threshold choice, move it to
development in a new manifest and replenish the sealed set.

First calibrate untuned scores and thresholds on grouped development labels.
Then test a small adapter/head update only after validating the official Clef
training path and joint-head/export compatibility. Keep the input and outcome
objective fixed when comparing tuned and untuned models. Evaluate calibration,
abstention coverage, false/harmful interventions, paired success and measured
end-to-end latency; bootstrap at initialization-group level. Compare complete
policies with fixed cadence and budget-matched random timing, counting scorer
cost and late calls. Validation on saved examples cannot substitute for these
closed-loop outcome comparisons. No current data establish a preferred tuning
method, dataset size or model as the robotics state of the art.

## Separate training-data usefulness experiment

Replace the vague `useful` rubric with a declared learner, training recipe and
target evaluation distribution. A human task-progress/visibility annotation can
be a cheap proxy, explicitly named as such. A downstream utility label requires
selected versus random datasets with matched eligible transition coverage,
duplicate handling and training compute, followed by evaluation on sealed actor
tasks/initializations. Record learner seeds and uncertainty; subset-level gains
do not identify each member's marginal contribution without another experiment.
Store these as `dataset_utility` overlays, never reuse timing labels or the 54
shadow predictions as utility ground truth. This objective can share raw data
with the timing task while maintaining separate labels, releases and claims.
