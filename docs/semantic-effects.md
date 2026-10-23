# Observed effects and intervention value, offline

Chords can now index an observed change as a separately versioned interval
annotation, and reduce matched continue/replan continuations against an explicit
cost objective. These are offline data tools. They execute no model, simulator,
provider, controller or paid job. Existing admission, freshness, candidate-suffix,
budget and authorization gates are unchanged.

The target is reproducible, source-bound effect queries and conservative outcome
reduction, rather than semantic-model accuracy. The baseline remains native
state-vector squared L2 from [analysis](analysis.md). Native state16 contains no
object pose, contact or task-stage truth. This change supplies annotation and
evaluation plumbing; real annotation agreement, boundary error and retrieval
relevance against L2/telemetry rules are **unmeasured**.

## Run the complete local example

Use the existing core environment; no additional dependencies are required:

```sh
.venv/bin/python -m coppertop.effect_cli demo --output .local/effect-value-example-1
```

The destination must be fresh. It contains immutable objects under `store/`,
`effects.parquet`, `report.json`, and `example.json` with independently retainable
release/plan hashes. All annotations, native signals, audited component hashes
and success outcomes in this example are **scripted synthetic fixtures**. They
are not a recording, a physical-effect claim or evidence of better decisions.
Repeated builds have identical logical artifacts and hashes.

Expected output: two effect rows; six scheduled comparisons; five evaluated;
four valid CPU fixture comparisons in one initialization group. Their labels
are one replan, two continue, and one uncertain. One comparison lacks replay
verification and has null value/label; one lacks a branch and stays in
`missing_comparisons`. Every report states zero qualified robotics pairs and
`robotics_benefit_established=false`.

Query changes and look up their native endpoint signals:

```python
import json
from pathlib import Path
import pyarrow.compute as pc
from coppertop.chords import LocalStore
from coppertop.chord_effects import from_effect_release

root = Path('.local/effect-value-example-1')
example = json.loads((root / 'example.json').read_bytes())
data, effects = from_effect_release(LocalStore(root / 'store'), example['effect_release'])
picked = effects.filter(pc.equal(effects['after'], 'gripper'))
native_endpoints = data.table.filter(
    pc.is_in(data.table['frame_id'], value_set=picked['end_frame_id']))
print(picked.select(['entity', 'before', 'after', 'start', 'end', 'available_at']))
print(native_endpoints.select(['frame_id', 'time_ns', 'state', 'action']))
```

Arrow's scalar-key join also joins effect/frame metadata. With the pinned
PyArrow20, filter/take native list-valued state/action columns by frame identity;
its hash join does not support those list columns as non-key payloads.

## Annotate existing native data

```python
from coppertop.analysis import from_release
from coppertop.chord_effects import effect_annotation
from coppertop.chords import canonical, fork

data = from_release(store, existing_release)
effect = effect_annotation(
    data, episode_id=episode_id, start=8, end=12, available_at=12,
    entity='mug', predicate='support_relation', before='counter', after='gripper',
    evidence_kind='human_observation', provenance=annotator_rubric_sha256)
derived_release = fork(store, existing_release, overlays=(store.put(canonical(effect)),))
```

This example requires actual evidence before using those labels. `None` means
unknown; before/after can also agree (for an annotated stall). Entity, predicate
and values are bounded rubric text, not a universal ontology. Evidence kinds are
`human_observation`, `telemetry_rule`, and `synthetic_fixture`. An annotator/rule
hash identifies the provenance; it does not validate the annotation's correctness
or authenticate an author. Do not infer grasp/contact/placement from gripper or
joint movement alone. A telemetry rule must name the quantity it actually measures.

Annotations bind episode/source identities, initialization/split, original frame
IDs and nanosecond timestamps. Observations i through j describe outgoing controls
**[i,j)**; j's action is excluded. Availability must be at j or a later existing
frame. `effect_table(data, overlays, available_at=k)` returns only effects whose
evidence is available by observation k in their respective episodes. It validates
all annotations, including those filtered out. Conflicting labels remain separate;
identical duplicates fail. Inputs and table/metadata expansion are bounded by the
existing analysis limits. Typed overlays cannot overwrite legacy split metadata.

Effects live in a separate Arrow/Parquet table. Existing analysis windows and
`decision_recording.prepare_case` inputs are unchanged; effects, replay state and
outcomes are not added to actor/scorer inputs. A retrospective label associated
with a start frame is not evidence available at that frame. No training exporter
or semantic nearest-neighbor claim is introduced.

```sh
.venv/bin/python -m coppertop.effect_cli index \
  --store .local/store --release <derived-release-sha256> \
  --output .local/new-effects.parquet
```

Readers authenticate consumed metadata and signals. They do not download cameras
or weights, qualify external provenance hashes, or establish full raw-object
closure. Forks reuse original episode/signal/video objects.

## Matched value and missing evidence

`PairProtocol` freezes the initialization group/split, completed-control boundary,
absolute horizon (at most 600), remaining actor query ceiling, per-query seed
schedule, actor/continuation identities, causal input view and latency condition.
The early query consumes the same remaining ceiling as ordinary queries. All
branches/repeats/views of an initialization stay in one split, including missing
scheduled comparisons.

`Continuation` holds an existing audited native `Tape`, independently retained
success/termination/truncation flags, a submitted-query journal, infrastructure
validity, task failures, total wall time, pause time and success wall time.
Resource times/calls are measured **from the decision boundary**, while tapes
include the complete prefix. Journals include failed, late and still-submitted
calls and charge every submission. Actual clock/latency semantics belong to the
hash-bound latency-condition artifact; callers must retain it. Failed/incomplete
calls invalidate labels. A terminated task or completed common horizon supplies
a bounded task outcome; an early truncated or unfinished prefix does not.

`ReplayEvidence` compares all eight existing audited components, native actions,
seed and control timestamps across the full prefix and an actual no-intervention
suffix. It also compares native observation identities across the full prefix
through the decision boundary. Branches and the restored replay must share the
uninterrupted tape's represented prefix identities; legitimate child tapes keep
distinct suffix identities. Equal telemetry alone cannot bind unrelated episodes
to one case. Both branch prefixes must match the reference. Equality of reset alone
does not qualify. The coverage hash identifies the external restoration audit;
matching component hashes cannot establish its completeness. Version1 labels
only `synthetic_fixture` replay evidence. `recorded_replay` is retained with
`simulator_restoration_coverage_unqualified`, null utility/delta and no label.
There is deliberately no boolean override that promotes it to qualified robotics
evidence. A future integration needs a separately reviewed qualification adapter.

`UtilityRecipe` requires explicit nonnegative weights and abstention margin:

```
U = success * success_weight - wall_seconds * wall_weight
    - submitted_actor_queries * actor_weight - submitted_scorer_queries * scorer_weight
    - (actor_cost_units + scorer_cost_units) * cost_weight - task_failures * failure_weight
delta = U(replan) - U(continue)
```

The recipe binds an explicit `cost_unit_name` and `task_failure_definition_sha256`;
retain the definition artifact with the experiment's provenance. Weights are choices, not learned calibration. Raw
success flags, horizon-capped controls to success, actual controls, wall/pause
times, calls and costs remain beside utility. Labels are provisional: replan
above the positive margin, continue below the negative margin, uncertain within
the margin. This is a single-pair rule, not an uncertainty estimator or calibrated
probability. No-handoff results retain their query costs and are identified
separately; they cannot establish a different action's benefit. Changed controls
are measured from overlapping executed controls, not inferred from scorer output.

`evaluate_pair` keeps runtime eligibility separate. Saved plans validate
eligibility and exclusion-reason metadata even when branches or objects are
missing. Ineligible cases do not
become intrinsic continue labels. Missing replay, unknown outcomes, unqualified
restoration and infrastructure failures have explicit reasons and null labels.
Malformed artifacts, protocol/seed/budget violations and corruption fail loudly;
their original artifacts remain intact.

Persist `continuation_record(branch)` and `replay_record(evidence)` using
`store.put(canonical(...))`. An `intervention_evaluation_plan` v1 contains a recipe
and a `comparisons` list. Each entry has a unique `comparison_id`, `protocol`
(the dataclass fields), `continue`/`replan` artifact hashes (nullable when absent),
`replay` hash (nullable), `eligible`, and `ineligible_reason`. The demo saves a
complete inspectable example. The CLI re-reads hash-checked evidence and recomputes
outcomes/labels; saved model judgments or saved labels are never input labels:

```sh
.venv/bin/python -m coppertop.effect_cli evaluate \
  --store .local/store --plan <evaluation-plan-sha256> \
  --output .local/new-value-report.json
```

An empty plan returns zero counts and missing-evidence declarations. Missing
branch objects remain scheduled in the report. Corrupt present objects are not
treated as missing. Bounds are 1,000 scheduled comparisons, 8MiB of consumed
plan/evidence metadata, 32MiB of cumulatively referenced metadata processing
(including cache hits), 1,024 UTF-8 bytes per episode/parent identifier, 600 controls
per tape, 128 actor queries and 256 submitted queries per continuation. Identifier
limits apply before tape construction/serialization; canonical source bytes are
preserved. Outputs refuse overwrites.

## Evidence and next experiment

The tests establish source/interval binding, availability, disagreement retention,
fork reuse, native serialization compatibility, explicit costs, helpful/harmful/
neutral/no-handoff fixture outcomes, replay/prefix checks, unknown-prefix
abstention, failed-query accounting, grouping, missing objects and corruption
rejection. They do not measure annotation agreement, physics or intervention value.

As documented in [training-data](clef-training-data.md), the retained DEV1150
cadence runs are one initialization and zero labelled counterfactual pairs. No
matching archives are present in this saved environment. This implementation
does not relabel those runs, tune a model or infer benefit from their successes.

The smallest next step is to qualify no-intervention restoration coverage on one
recorded initialization and bind the measured protocol/latency/call evidence.
Then a separately approved, bounded matched continuation pilot can supply the
first real pair; it cannot establish superiority. Semantic quality separately
needs independent interval annotations on frozen grouped recordings, comparing
boundary error and retrieval relevance with telemetry rules and native L2. No
GPU/simulator/provider execution or spending is authorized by this implementation.

```sh
.venv/bin/python -m unittest discover -s tests -p 'test_chord_effects.py' -v
.venv/bin/python -m unittest discover -s tests -p 'test_intervention_value.py' -v
.venv/bin/python -m unittest discover -s tests -p 'test_effect_cli.py' -v
.venv/bin/python -m unittest discover -s tests -v
```
