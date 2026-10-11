# Retained Xiaomi/Clef pilot: offline preparation

This lane prepares an experiment manifest and offline test matrix at main
`46701a30b31713d0393d069df7427daff875217d`. It has no execution option. The
existing official/one-update smoke pilot is unchanged. Its smoke head is not Clef.
Source recovery is pinned to `c381330c16dd43d75226af845555c795558cd78b`,
verified against the private remote branch before integration. The immutable
recovery provenance SHA-256 remains
`372adbebebfdfbca7cef293963c279dbf5edd702dc707d29bd3d602aad8b0364`;
it binds the recovery originals, while the preparation manifest hashes current
adaptations separately. Preparation has no paid or provider execution option.

The source-review corrections are separately pinned to
`de6eb381025b9b737d5a3ab536881aede1106e3a`: model/cache subprocesses receive an
explicit runtime-variable allowlist and fresh owned HOME/cache defaults through
child completion and cleanup. Live/unknown child status retains the owned home
and annotates the primary error. This isolates default credential discovery;
it is not a filesystem sandbox or qualified hard-stop supervisor. The exact
reference bytes execute only after authentication. Derivatives are bound in
`manifests/clef-review-derivatives.json`; original recovery metadata and bytes
at `c381330` stay intact.

The benchmark acceptance gate also requires every actor row to contain a valid
chunk hash and each saved case's hashes to match across all five phases. A
complete schedule and positive timing alone do not establish actor parity;
JSON booleans do not qualify as measured durations. The recovered latency
summaries remain historical reports.

## Missing inputs and evidence inventory

The parent recovered the Mac inventory. Clef is `Cloudflare/clef-flash`, revision
`fde727a287004204b7518dcc983fe64379776712`, reported snapshot size
19,083,377,419 bytes, BF16 `Qwen3_5ForConditionalGeneration` with the official
joint-schema head. `joint_head.safetensors` SHA-256 is
`19cdcec8c81dc9212be320fff47462ab342fbc1278be4368fb3da71241cf5ba0`.
That head hash is not a whole-snapshot hash. Runtime is Torch2.11.0+cu128 /
Transformers5.10.2 with 56 pinned wheels. The weight-free release/config inventory
and exact wheel lock are now present and hash-authenticated. The release inventory
contains sizes and LFS hashes, not a whole-snapshot byte hash. The reference
`joint_schema_model.py` is not vendored; its pinned size/hash is now required
before builder, CPU-gate or worker imports. No model runtime execution is established.
Preserve separate Xiaomi/Clef runtime pins until compatibility is
qualified; do not assume they can share one dependency environment.

Clef emits timing/usefulness probabilities, **not native actions**. Its timing
gate may authorize a fresh Xiaomi actor replacement when replan probability
≥.8, margin ≥.2, age ≤2 controls and query budget permits. Data usefulness never
intervenes. The recorded async design continues the old queue during scoring
and actor inference, discards elapsed candidate offsets, and retains the old
queue for stale/failed/uncertain results. No simulator-mediated Clef intervention
has run. Local Xiaomi weights and the prospective image remain unverified.
Existing Xiaomi file digests remain in `containers/gpu-smoke-pins.json`.

The parent verified the historical 24-call benchmark's final receipt, which names
`/Users/vuln/Documents/Codex/2026-10-05/task/coppertop-publish/docs/clef-colocation.md`,
and confirms both Apps stopped. That report and the source-only handoff are now
local, with model/config/license/dependency identities in
`manifests/clef-recovery/` and `containers/clef-runtime.lock`. Receipt/archive
identities do not supply complete payload closure or a fresh remote readback.
An explicit pilot clock, score/actor budgets, complete unused-seed ledger, image
digest, qualified runtime and fresh timeout/storage/cost quote remain missing.
No environment files, credentials or bulk model/data exports are needed for the
offline checks.

The seed inventory is bounded evidence, not a complete historical ledger:

| CoffeeSetupMug DEV seeds | Evidence | Treatment |
| --- | --- | --- |
| 7 | `episode.EXPLORED_DEV_SEEDS` declares explored DEV; not proof of CoffeeSetupMug execution | Also exclude conservatively |
| 8, 9, 12, 15, 23 | `performance_eval.DEV_SEEDS`, performance pilot docs | Used; exclude |
| 1150 | Full-rollout docs report execution; mechanism config also selects it | Used; exclude |
| 1151 | Old performance pilot scheduled episode 1 | Conservatively reserve |
| 1152, 1153 | Episodes 2, 3 at base 1000, full target50 index 3 | Provisional candidates only |

The full-rollout doc describes **one failed staging attempt** that reported
success at 211 controls / 14 queries / 212 observations and retained only 9,194B
metadata. That attempt's payload is unavailable. This is not a statement that
all DEV1150 evidence was lost. The parent verified later DEV1150 execute-16 and
execute-8 archives by Mac-local rehash against their receipts in prior thread
`01a10b0d-dfbe-73a4-85b9-d3731fb24083`. Remote R2 has not been freshly rechecked.
`dev-archive-identities.json` now supplies attempts, release IDs and receipt hashes;
complete object closure and readback remain pending. No provider or bulk artifact
retrieval was performed by this lane.

| Retained Xiaomi archive | Bytes | SHA-256 | Reported outcome |
| --- | ---: | --- | --- |
| execute16 | 163,737,600 | `75283c1f846bc1e4e67ae89185fbe51a7a0e2341b9938c7140b4c75641eeb44b` | Success, 211 controls, 14 queries |
| execute8 | 204,820,480 | `80511e4d15184a6184c336df4ca651163c8209cceace7c44fe03afc0f374914b` | Success, 224 controls, 28 queries |

Both use DEV1150: base 1000, original target50 index 3, episode 0, stride 50;
actor query seed 20261006. Seeds 8, 9, 12, 15, 23, 1150 are known used and 1151 is
also excluded. 1152/1153 remain provisional until the full ledger is packaged.

Those later archives may supply an **unpaired retained Xiaomi baseline** once
receipts establish source/model/evaluator/config, initialization, seeds, complete
closure and readback. Execute-8 is a cadence experiment; the proposed stock arm
uses execute-16. Neither archive establishes a retained Xiaomi/Clef pair or two
unused DEV seeds. Preserve both histories and reconcile receipts before reuse.

## Freshness and the required experiment decision

At continuously advancing 20Hz controls, two controls are 100ms. Reported
unoptimized serial Clef scoring is about 895ms and serial actor inference about
399ms. In the actual overlap phase, Clef was about 1134ms and the actor about
918ms. At the next admission boundary, scorer-only requires 18 controls;
perfect overlap of the recorded overlap medians still requires 23, and serial
895+399ms requires 26. These are illustrative medians from three saved cases,
not shared-observation intervention measurements. The measured path cannot
produce a ≤2-control-fresh replacement
**under this continuous clock at these timings**. These approximate figures are
not a bound on every future query/hardware configuration. An unpaced simulator
can advance a different number of controls per wall second; admission must log
actual observation/admission control indices rather than infer age from nominal
simulation timestep alone.

A 16-action candidate spans 800ms under that continuous clock. At 918ms, discarding
elapsed offsets leaves no suffix. Widening freshness alone would not repair that
candidate, and the old 16-action queue can also exhaust while work is pending.
The empty-queue fallback must be explicit; silent freezes, retries, or replaying
expired controls would change the experiment.

The smallest recommended decision is a **paused synchronous functional pilot**:
at predeclared decision boundaries, freeze simulator controls while Clef scores
and, if admitted, Xiaomi replans. The same observation/control index remains
current, so age/elapsed offsets stay zero during the pause. Execute actual native
Xiaomi actions selected by the gate; report load, score, actor, pause and total
wall time for each arm. This tests simulator-mediated intervention, not real-time
execution, and must be explicitly selected before implementation/quote.

Alternatively retain continuous async execution and the strict ≤2-control gate.
At the recorded timings, expect stale-only/non-intervening cases; this is a
latency/fallback diagnostic, not intervention proof. A capable async pilot needs
qualified latency and a nonempty unexpired candidate suffix. No freshness widening,
clock pause or model optimization is silently authorized. The manifest leaves
`selected_design=null` while recording the synchronous recommendation.

## Minimal no-GPU checks after exact source handoff

The integration imports the **actual migrated** parser/gate/runner and uses
deterministic actor/scorer/environment/clock doubles. Generic validation stays
strict; the self-hosted joint-head path explicitly selects
`GatePolicy(probability_decimals=4)`, agreeing with the benchmark without
normalizing probabilities. Exact decimal margin boundaries are preserved.
The runner checks synchronized completed-control indices at scorer admission
and checks age again at candidate handoff. Its explicit `paused_synchronous`
mode is implemented and fixture-tested as a proposed functional design; the
experiment manifest still leaves the actual pilot choice unset.

| Test | Required assertion |
| --- | --- |
| Official four-decimal rounding | Tested through actual validator and opted-in GatePolicy. Joint mass 0.9999/1.0001 agrees, with option-count precision tolerance; malformed/grid-invalid/out-of-range/NaN answers remain rejected. Strict generic mode remains strict. |
| Gate boundaries/budget | Around p=.8 and margin=.2, test .7999/.8000/.8001 and .1999/.2000/.2001; ages 0/1/2 allowed, 3 stale; budget before/at exhaustion. Data-usefulness changes alone cannot trigger actor inference or replacement. |
| Clef-off parity | With identical seeded inputs, assert exact decoded chunks, executed native controls, query counts/order and no Clef calls against stock Xiaomi. Fixture parity proves adapter control flow only; real model/evaluator parity remains a runtime gate. |
| Actual intervention path | A schema-valid high-replan fixture authorizes an actor-produced 12D chunk; execute its changed commands through a fake step interface and retain decision/proposal/executed trace. Clef probabilities must never become controller actions. |
| Async freshness/suffix | Advance an injected control clock while the old queue executes. Fresh candidates apply only at a control boundary; discard elapsed offsets; reject aged>2, exhausted suffix, failures and uncertainty while retaining the old queue. Add 895/918ms scenarios without sleeps; separately verify explicit empty-queue fallback. |
| Proposed synchronous design | Actual runner fixture holds the same control index during virtual 895ms scoring +399ms actor work, executes changed native commands, and reports 1294ms pause plus control wall time. Clef-off makes no scorer/extra actor calls and preserves the supplied stock queue. No continuous 20Hz claim. |
| Complete accounting | Journal all score/actor submissions, discarded candidates, failures and timeouts. Distinguish no-replan, stale-score, stale-candidate, exhausted-suffix, exhausted-budget, unchanged-action, applied-change and infrastructure-invalid outcomes. Zero interventions means the mechanism was not demonstrated; never manufacture improvement or retry/tune to force one. |

`test_evaluation_intervention.py` covers rounding, thresholds/margins/ages/budget,
off-path queue parity, actual applied/unchanged actions, usefulness, failed actor,
timeout and terminal stop. `test_evaluation_async.py` uses virtual time and owned
thread barriers: age2 admits the suffix, age3 rejects it despite a fresh scorer,
and in-progress controls do not fabricate completed age. Reference-code tests
reject hash mismatch at actual builder/CPU-gate/worker boundaries before runtime
imports. These are CPU fixture checks, not real Clef/Xiaomi inference, complete
official-loader execution, RoboCasa rollouts or benefit proof. Full-episode
queue exhaustion requires the outer controller to stop or make a separately
bounded normal replan; the runner never replays expired controls. Whole-episode
journaling and durable staging commit/reload/hash verification remain runtime
gates before any approved GPU run.

## Prepare and verify

From the isolated checkout, using the repo's existing Python environment:

```sh
mkdir -p .local/evaluation-readiness
/workspace/coppertop/.venv/bin/python tools/prepare_clef_pilot.py \
  > .local/evaluation-readiness/manifest.json
/workspace/coppertop/.venv/bin/python -m unittest discover \
  -s tests -p 'test_evaluation*.py' -v
/workspace/coppertop/.venv/bin/python -m unittest discover \
  -s tests -p test_clef_identity.py -v
```

`manifest_sha256` binds canonical manifest bytes, including preparation sources,
model file-manifest and locks. The output deliberately says `launch_ready=false`
and `paid_authorized=false`. Unknown inputs remain `null`; fixture checks cannot
turn the unexecuted preflight gates into passed gates.

`containers/robocasa-target50.json` is an AST constant projection of the pinned
official registry: original task order, 18 atomic-seen / 16 composite-seen /
16 composite-unseen tasks, and each horizon. Its digest and source-byte digest
are pinned. No simulator module is imported to derive it.

For existing bounded copies of the official source files, additionally set
`COPPERTOP_OFFICIAL_ENTRY` and `COPPERTOP_ROBOCASA_REGISTRY`. Tests authenticate
their exact SHA-256 before checking the registry projection and executing pure
evaluator preprocessing functions with an injected processor/transport. These
tests are not complete official-loader execution, model inference, simulator
execution, or Clef-off parity. Without source paths, those five tests skip.

Main's declared Modal seam test dependencies must also be available for the full
suite. In this environment, the shared venv lacked three declared pins. They were
installed only into an ignored target, without modifying that venv:

```sh
uv --cache-dir .local/uv-cache pip install \
  --python /workspace/coppertop/.venv/bin/python \
  --target .local/controller-test-deps --no-deps \
  truststore==0.10.4 aiohttp-socks==0.12.0 python-socks==3.1.1
PYTHONPATH="$PWD/.local/controller-test-deps" \
  /workspace/coppertop/.venv/bin/python -m unittest discover -s tests -v
```

The target supplies the pins from `requirements-modal-seam.txt`; it does not
alter credential/TLS/network configuration or invoke a provider. Set the two
official-source path variables above when the authenticated bounded copies are
available. The two existing real official-loader tests still require
`COPPERTOP_SOURCE` and its already-acquired loader fixtures.

| Gate | Offline evidence | Remaining runtime/input gate |
| --- | --- | --- |
| Task membership/horizon/seed schedule | All 50 original indices; 2,500 unique seeds; per-task horizons and query bounds | Confirm prospective runtime registry matches pins |
| History/crop/state/cameras/decode | Causal offsets -6,-4,-2,0; .95 crop; EE-first14D padded60; three cameras; first12D decode | Real processor/model and stock Clef-off parity |
| Two pairs | Same task/index/seed/horizon per pair; alternating arm order | Complete unused-seed ledger and matching native/simulator/camera/instruction initialization |
| Clef intervention | Actual gate/runner CPU fixture executes actor-produced 12D change; unchanged candidate is not counted as a change | Real-model/off parity and attributable simulator action change; full-episode integration |
| Rounding/freshness design | Actual parser/gate precision parity, candidate age2/3, completed-control clock and proposed paused mode tested | Explicit pilot clock selection, qualified latency and full-episode empty-queue semantics |
| Durable retention | Manifest names commit/reload/hash gate | Storage fixture round-trip before GPU; full result archive closure/readback before acceptance |
| Outcomes/accounting | Manifest requires every query/failure/timeout and scheduled outcome | Separate infrastructure-invalid episodes from policy outcomes; no silent retries |

## Proposed milestone and budget inputs

The first prospective paid milestone is two confirmed-unused DEV CoffeeSetupMug
seeds × two arms = four episodes, all with the full 600-control horizon (unless
success or environment termination/truncation occurs first). Freeze Xiaomi,
Clef, config, evaluator and image identities before execution. Alternate
`xiaomi_only, xiaomi_clef` then `xiaomi_clef, xiaomi_only`. Clef-off parity and
actual action intervention remain mandatory. Two pairs support feasibility only;
make no statistical superiority claim and do not tune final unseen tasks.

At stock execute-16, the four-episode maximum is 2,400 controls, 2,404 source
observations and 152 stock Xiaomi queries. Extra actor/Clef/probe queries need independent
bounds. These counts are quote inputs, not cost or timeout estimates. Obtain exact
model/Clef/evaluator/image digests, all query deadlines, per-episode load/startup/
runtime allowances, CPU staging/cache/readback/cleanup and GPU resource seconds,
raw-array/video/archive object and byte bounds, retention destination/duration,
fresh prices and the preserved spend ledger before quoting an approval envelope.
Also freeze decision opportunities, Clef score and extra actor query budgets,
query deadlines, chosen clock semantics and stale/failure/empty-queue fallback.
Quote the 19,083,377,419-byte Clef cache/source transfer only from exact artifact
manifests; qualify the 56-wheel lock and isolated runtime compatibility. Measure
peak combined model/activation/KV memory before choosing resources, and obtain
latency distribution/control-index-age evidence for any async qualification.
Include synchronous pauses in per-episode/total timeout and all billing bounds.
Old paid approvals cannot authorize this new experiment. No paid action is
authorized by this preparation.

### Smallest quote variant and independent readiness work

One minimal functional variant proposes **one predeclared decision opportunity at
completed control 8 in each Clef episode**, if the episode has not stopped. Keep
p≥.8/margin≥.2, permit at most one score and one extra actor query per Clef episode,
and never retry or add opportunities because a judgment did not replan. This is
a proposal requiring selection, not a frozen experiment config. Across four
episodes it bounds stock actor calls at 4×38=152, additional actor calls at 2,
and Clef scores at 2: **actor≤154, scorer≤2**, including failed/timed-out calls.
Zero attributable changes remain a valid measured result but fail to demonstrate
intervention; do not tune the gate to force them. At each queue boundary, the
outer controller performs a separately bounded normal actor query or marks an
infrastructure failure; it never replays expired controls. Clef-off follows stock
execution, without scoring pauses.

For a defensible quote, use fresh rates with bounded resource time for **every**
CPU build/qualification/staging/readback/cleanup job and all four GPU episodes:

```
episode_time_bound = startup + actor_load + optional_Clef_load + reset
                   + 600 * qualified_max_step_wall_time
                   + actor_call_cap * actor_end_to_end_deadline
                   + scorer_call_cap * scorer_end_to_end_deadline
                   + archive_commit + cleanup
quote = sum(job_time_bound * (GPU_rate + CPU_cores * CPU_rate
                             + RAM_GiB * RAM_rate))
      + storage_GB_months * storage_rate + object_request_cost
      + readback_and_egress_cost + declared_margin
```

All rates must use compatible units and the selected region/resource type.
End-to-end deadlines include fresh observation preparation/IPC, processor,
forward and decoding; the saved-input medians exclude some of that work.
Outer process deadlines must cover these component bounds and cleanup. Full 600
controls are not 30 seconds of wall time: paused functional execution does not
enforce a nominal 50ms wall budget per simulator step, and all scoring/actor waits
are counted. The continuous mode retains its separate pacing checks. Select
resource sizes from qualified peak model/activation/KV and host-memory evidence,
not the historical 29,355MiB post-load snapshot. Prices, bounds and the ledger are
not presently sufficient for a numeric all-in quote.

The historical image `im-0ZJ0cSfNXwzFEUZjAdRiSF` may avoid a rebuild only after
current availability, immutable identity, weight/source hashes, exact isolated
locks and CPU processor/input qualification pass. Its ID alone proves none of
those current gates. If unavailable or mismatched, include a CPU-only rebuild
and cache transfer in the quote before reserving GPU time. New integrated source
must be bound to the actual runtime payload even when reusing weights.

Storage preparation is required in either case: select durable destination and
retention, freeze object/byte bounds, commit a small source fixture, reload its
complete closure and compare archive/restored-byte hashes **before GPU**. The
existing local/fake storage tests do not prove the actual remote storage boundary.
Do not make provider/storage calls under this offline authorization.

Available-source audit now authenticates all 23 original recovery entries and
separately hashes current derivatives. It records used/reserved/explored seeds,
including declared explored seed 7, without claiming 1152/1153 are unused. Raw
completion receipts and historical archive payloads are absent here, so their
closure and complete historical ledger remain inputs.

Full original-byte authentication requires the exact `c381330` Git object in
local history and fails if it is absent. Shallow checkouts skip only that
source-dependent test class for Git's exact missing-object result. The portable
seed/receipt checks still verify pinned current metadata and run without Git;
they do not replace original-byte verification or fetch missing history.

`evaluation_episode.run_fixture_pilot` now executes four 600-control CPU-double
episodes through the actual gate, including candidate offsets 8..15 after the
control 8 handoff, bounded normal queries, full journals, partial failure tapes and
all cleanup attempts. Its fixture result requires matched reset bytes, stock
parity and attributable fake action changes. Failed/uncertain/unchanged results
never force retries. This is an injected CPU adapter; real simulator recording
plumbing and outer hard process deadlines remain runtime work.

Selected action source and native-change attribution are bound before each
control and retained with its valid audited Frame. A valid late Frame therefore
keeps its candidate/old-queue source even when the control or handoff deadline
then fails; an invalid or missing Frame does not claim execution. Runner cleanup
reports secondary cancellation/join failures while preserving the primary
failure, including cancellation exceptions. The regressions cover both deadline
paths, identical admitted actions and failed cleanup.
Worker scorer/extra-actor cancellation is recorded as a failed submitted operation
and makes the episode infrastructure-invalid. A worker readiness signal alone
cannot qualify an operation that raised a cancellation exception.

`evaluation_roundtrip` uses the existing LocalStore/Tape/archive/stage machinery
to seal, locally fsync, independently reload and hash the complete ten-object
fixture closure. Raw closure is capped at 16MiB and total retained local copies at
66.125MiB. Corrupt/missing files fail; originals survive; cleanup failures retain
the primary error. The source and restored closure include accounting and every
audited Tape frame/action, not real camera/model payloads or remote durability.

The canonical unpacker's cleanup fix changes its source hash. The historical
`containers/archive-live-admission.json` receipt remains immutable and therefore
does not qualify the modified archive code. Its existing admission guard correctly
rejects the new bytes until an actual separately authorized commit/readback check
passes; no live receipt or runtime qualification was fabricated to satisfy tests.

Reproduce all available no-cost evidence in a new ignored directory:

```sh
/workspace/coppertop/.venv/bin/python tools/verify_clef_readiness.py \
  --directory .local/evaluation-readiness/new-offline-verification
```

The command emits the preparation manifest, source/seed/receipt audit, complete
pilot accounting, local hash receipt and verification summary. It has no execute
option and leaves selection, live readiness and paid authorization false.

Complete local official-loader checks only if their
already-acquired source/fixtures are supplied. Those inputs are not fabricated
when absent. The exact remaining decision is functional pause versus a separate
async latency/freshness experiment, plus opportunity/query/fallback bounds; paid
approval must name the resulting complete CPU/storage/four-episode plan and its
fresh all-in monetary cap. No statistical superiority claim follows from two pairs.

### Concrete unselected deadline proposal

`pilot_proposal()` records operational failure caps, not qualified performance:
each stock episode allows 38 actor/input calls; each Clef episode allows 39 actor,
40 input and one scorer call. The extra actor uses the same detached control-8
input as the scorer. All failures/timeouts consume a submitted call; retries and
additional decision opportunities are zero. An admitted candidate covers controls
8 through 23, including the eight controls after the single-boundary helper ends;
normal replanning resumes only when its queue ends. Rejecting it preserves only
the unexpired old queue.

| Proposed core cap | Seconds | Meaning |
| --- | ---: | --- |
| Reset plus initial-state capture | 30 | Includes matched initialization evidence |
| Each detached input | .5 | Input preparation is separately counted |
| Each actor / each scorer call | 5 / 5 | End-to-end preparation/IPC/forward/decode deadlines |
| Each native step plus audit | .25 | Functional wall cap; no 20Hz throughput claim |
| All cooperative close operations | 10 | Preserve primary failure and attempt all owned cleanup |
| Full episode core | 435 | Reset, inputs, queries, up to600 steps and cooperative cleanup |

The conservative component sums are 399 seconds stock and 410 seconds Clef,
leaving headroom within 435. These caps must pass actual runtime qualification;
they do not establish that a rollout finishes within them. A timed-out normal
actor stops an empty queue as infrastructure-invalid. A failed scorer/extra actor
retains only available old controls while recording infrastructure-invalid status.
Uncertain judgments and unchanged actions are valid policy outcomes. Explicit
success/termination/truncation stops immediately; reaching 600 controls records a
horizon outcome without manufacturing success. Cancellation/cleanup failure
preserves partial evidence and requires the outer supervisor to stop the worker.

Proposed outer phase allowances are startup 90, actor load 120, optional Clef
load 120, archive commit 120 and process teardown 30 seconds in addition to the
435-second core: stock job≤795s and Clef job≤915s, **four GPU episode jobs≤3420s**.
Load Clef first when enabled, then the actor: a proposed 600-second actor alarm
covers actor load 120 plus core 435. The existing 450-second colocation benchmark
launcher is not this pilot launcher. Outer process supervision, interruptible
operations and deadline coverage remain qualification inputs; the CPU callback
fixtures cannot terminate an uninterruptible simulator or model process.

Apply fresh selected-region GPU/CPU/RAM rates to these conservative episode-job
caps, plus separately bounded CPU image/cache/qualification/storage/readback jobs,
retained GB-months, requests, egress and an explicit margin. Any qualification
probe calls need their own caps. Image/model/config/evaluator/runtime payload
identities, peak host/device memory, exact actual storage destination/retention,
all CPU job bounds, current rates and the preserved spend ledger are still required.
If measurements exceed a proposed cap, revise the proposal and obtain approval
before execution; do not shorten horizons or silently widen deadlines. This
proposal is unselected and carries no spending or provider authorization.

The later full comparison is **2,500 episodes per arm**, 5,000 for two arms,
on RoboCasa1.0.1 target50 in the pretrain kitchen/object split, 50 trials per task.
Retain every original target50 index and `num_trials=50` even for subsets:
`seed = base_seed + full_target50_index * num_trials + episode_index`.
Upstream seed 7 is a reference/default, not a required rule. Registry horizons
range from 450 to 4,350; never shorten them to meet timeouts.

Protocol sources: [RoboCasa multitask protocol](https://robocasa.ai/docs/build/html/benchmarking/multitask_learning.html),
[pinned Xiaomi evaluator](https://github.com/XiaomiRobotics/Xiaomi-Robotics-1/blob/0dd7aef8dc87296246aae812a1f59ccb708e5546/eval_robocasa365/entry.py),
[pinned RoboCasa registry](https://github.com/robocasa/robocasa/blob/456174f62b89b8fca99eaaf33949c29fec9cfc2a/robocasa/utils/dataset_registry.py).
