# Clef/Jev scoring and the first timing intervention

## Integration status, October 11, 2026

This source-only recovery has now been exercised with CPU doubles through its
actual gate/runner. Self-hosted joint-head output uses the explicit
`GatePolicy(probability_decimals=4)` contract; generic provider validation stays
strict. The runner measures completed-control age at scoring and rechecks it at
candidate arrival. It records action sources, discarded offsets, changed controls
and scorer/actor/pause/wall timing. Its Clef-off switch preserves the supplied
stock queue with zero scorer or extra actor calls.

The proposed `paused_synchronous` mode freezes controls during scoring/replanning
for functional proof; it is separate from the existing continuously paced mode.
Selecting the actual experiment design remains pending. Approximate measured
serial/overlap latency misses the continuously advancing 20Hz two-control gate,
and a 16-control candidate can expire. No simulator-mediated Clef intervention
or real-model parity test has run. Historical hosted/cached results below are
evidence, not permission for a new paid call. Current gates and the four-episode
proposal are in [the evaluation specification](clef-evaluation-readiness.md).

Use hosted Clef Flash first. Retain Jev as a state-only comparator and larger Clef
as an optional accuracy comparator. Published general decision benchmarks do not
establish a robotics winner. Fine-tuning becomes useful after measuring a repeatable
failure on labelled, grouped train/development cases, with a sealed evaluation set.
The two successful DEV1150 recordings alone cannot supply those labels.

As checked October 9, 2026: [Clef Flash](https://huggingface.co/Cloudflare/clef-flash)
has open Apache-2.0 weights, a vision backbone and a joint schema head.
[Workers AI](https://developers.cloudflare.com/workers-ai/models/clef-flash/)
hosts it at `@cf/cloudflare/clef-flash` and lists $0.09/M input tokens.
[Jev on OpenRouter](https://openrouter.ai/docs/guides/community/jev) currently
documents text input. [The System One endpoint](https://openrouter.ai/docs/guides/community/typesafe-sdk)
accepts `typesafe/jev-1.13` and returns the served model identity.
[Cloudflare's tuning announcement](https://blog.cloudflare.com/clef-decision-models/)
describes an assisted service preceding a future self-service platform. These
sources give implementation options, not measured Coppertop accuracy or latency.

## Implemented behavior

`decision_recording.prepare_case` reads a specified immutable episode from a bound
release, keeps the native semantics, and writes a separate causal decision case.
Inputs contain three sampled native state observations, up to eight past executed
actions, the already-issued queue's remaining predictions, and three current-frame
raw RGB images. PNG encoding preserves pixels. This is not the actor's crop/history
view and does not pretend to contain visual motion history or privileged object poses.
The case is limited to development recordings. It excludes future frames, future
executed actions, reward, success, terminal flags, and the final evaluator result.
Even at a recorded query boundary it uses the preceding chunk, not the new query.

The two independent questions are early replan timing and predicted data usefulness.
The latter never triggers intervention and is never a ground-truth utility label.
All option probabilities are retained. Overlays bind the case, frame, schema,
provider, requested/served model, request hash, input modality, monotonic latency
and typed usage. Raw recordings and release manifests are unchanged.

`DecisionProvider` supports Cloudflare's REST result envelope and OpenRouter's
System One response. Hosted model IDs do not pin hidden server weights; receipts
explicitly record that limitation. Jev receives native state/action history without
images, labelled as state-only; do not report that arm as the same-input multimodal
comparison. Transport uses one direct HTTPS POST with bounded request/response,
no redirects, proxies, retries, arbitrary hosts, or provider error-body logs.
`BoundScorer` binds immutable inputs and owns cancellation of one request.

`decision_runner.gated_rollout` implements one control-boundary experiment. It
advances old controls while the scorer runs. A strong, fresh replan judgment may
start one seeded Xiaomi query; old controls also continue during that query.
Candidate indices already elapsed since submission are discarded. Uncertain,
invalid, stale, exhausted-budget or failed judgments leave the old queue running.
Late candidates are discarded. A finished episode stops stepping. Pacing overruns
invalidate the branch; simulator pauses cannot count as free inference time.
Cancellation failure requires the caller to stop its worker process.

The initial 0.8 probability/0.2 margin threshold and two-control maximum age are
provisional, not calibrated. The runner admits at most one scorer and one actor
call, while the outer controller must enforce authorization, whole-episode budgets,
initial pairing, process deadlines and cleanup. The existing cloud evaluator has
not been changed to launch an intervention; its successful cadence runs remain
controls. No real provider response or simulator-mediated gate has been executed
in the initial build. CPU mock handoffs are plumbing evidence only. The subsequent
hosted shadow results below execute real provider calls, but not a simulator gate.

## Local use

Create a case from an already-local immutable store:

```sh
python -m coppertop.decision_cli prepare \
  --source-store PATH_TO_UNPACKED/store --release RELEASE_SHA256 \
  --episode-record EPISODE_RECORD_SHA256 --frame 8 \
  --output-store .local/decision-gate/store
```

Preview exact request size/hash without credentials or network:

```sh
python -m coppertop.decision_cli score \
  --store .local/decision-gate/store --case CASE_SHA256 --provider cloudflare
```

After a separately authorized single API call, append `--execute`. The CLI reads
existing `CLOUDFLARE_ACCOUNT_ID`/`CLOUDFLARE_API_TOKEN` (or its
`CLOUDFLARE_AUTH_TOKEN` alias), or `OPENROUTER_API_KEY`
for `--provider openrouter`, from its process environment. It never reads `.env`
files or changes keys. Use your existing local environment launcher; do not paste
keys into chat. A failed request may be billable; its immutable failure receipt
records unknown billing and no retries. Each invocation makes at most one call.
The CLI does not impose a project-wide monetary ceiling or authorize spending.

## Experiment and tuning sequence

1. Run a bounded hosted shadow batch on the prepared causal DEV cases. Measure
   schema validity, abstention, response/total latency and disagreements. These
   successful tapes have no counterfactual replan-benefit labels; shadow scores
   cannot prove intervention benefit.
2. Authenticate a detached gate input and run one real scorer/actor handoff in the
   disposable simulator. Measure end-to-end delay, executed prefix, arrival and
   candidate offsets. Require paired reset and bounded continuation evidence;
   one matching initial fingerprint does not certify complete replay state.
3. Freeze the timing policy and compare against both fixed cadences and a random
   timing gate under equal actor/scorer budgets on grouped development seeds.
   Log success separately from termination and include harmful/unhelpful replans.
4. Collect outcome-grounded intervention labels from those matched experiments.
   Group by initialization, not frame, before train/development/test splitting.
   Tune thresholds before weights. If persistent visual decision errors remain,
   test head/adapters with the schema probability objective rather than a generic
   chat SFT recipe; joint-head compatibility and the released training path need
   a separate smoke test. Compare tuned against untuned on frozen evaluation
   with calibration, false interventions, outcome and latency. Do not train on
   scorer predictions as if they were counterfactual labels.

Keep the original selected-versus-random dataset-utility experiment separate:
equal eligible transition coverage and training budget, no runtime-intervention
claim. No training, model download, deployment or paid provider call is included
in the initial local build.

## Actual hosted shadow test, October 9

The authorized batch made 27 Clef Flash vision calls and 27 Jev state-only calls
over the 27 prepared DEV cases. All responses passed schema, probability and
request-binding checks, with no retry, GPU allocation, R2 operation, key change,
training or model download. Its plan bound was $0.23182848, below $0.25.

| Provider | Calls | Median client round-trip | p95 client round-trip | Input tokens | Cost |
|---|---:|---:|---:|---:|---:|
| Clef Flash | 27 | 1.125 s | 1.775 s | 116,408 | $0.01047672 estimated |
| Jev | 27 | 0.749 s | 0.866 s | 123,957 | $0.005206194 response-reported |

Client round-trip includes a fresh TLS connection, network and hosted inference;
it excludes request preparation and is not model-only or Modal-worker latency.
All observations exceeded the midpoint's 0.4 s remaining queue and the provisional
0.1 s freshness window, before adding Xiaomi inference. The current implementation
would admit zero early actor queries. A real handoff was consequently not launched.
Worker-side connection reuse/local or anticipatory scoring needs its own timing
test before paying for simulator intervention; these results do not establish
that hosted scoring is impossible from every deployment location.

Ignoring delay, neither provider crossed the 0.8 replan threshold. Clef's largest
replan probability was 0.4648; Jev's was 0.12. Clef chose `continue` 23 times and
`replan` four times; Jev chose `continue` for all 27. Do not lower the threshold
merely to manufacture interventions on successful trajectories.

Clef chose `not_useful` for all cases, while Jev chose `useful` for 26 and
`uncertain` for one. Winning probabilities were only 0.487–0.634 and 0.50–0.63,
respectively. Different input modalities, no counterfactual labels, one DEV
initialization, and a vague training-usefulness rubric prevent any accuracy,
calibration or data-selection benefit claim. Retain predictions for review,
collect labelled temporal examples, and test the rubric before tuning weights.

Ignored evidence directory:
`.local/decision-gate/shadow-ready/run-0b7a086a158440379fda83c52d0fde45/`.
`analysis.json` binds all 54 actual receipts/overlays and projected timing
admission. This projection is a deterministic policy check using measured
latencies, not simulator or official-loader execution. Cloudflare's fee is
estimated from its returned tokens at published pricing; final billing may differ.

## Connection-reuse follow-up

`decision_latency.SerialSession` measures connection setup, request send, response
headers and body read separately. It is a serial benchmark transport, not a
replacement for the cancellable live scorer. A stale connection fails without a
retry; each session is explicitly closed. The prepared local plan at
`.local/decision-gate/latency-ready/plan.json` selects execute-16 frames 8, 104 and
200 for each provider, with a cold and reused-connection request per case: 12
requests, context-price bound $0.05151744 and reserve $0.06. Warm requests follow
identical cold requests, so provider caching and order may affect total latency.
This experiment cannot isolate model inference or establish worker-side latency.

After explicit payload/destination approval, all 12 calls completed without
retries. All six warm requests reused their connections. Median cold/warm latency
was 1.226/0.617 seconds for Clef and 0.714/0.592 seconds for Jev (three pairs each).
Median cold connection setup was 0.153 and 0.129 seconds respectively. Every call
still exceeded both the 0.1-second freshness limit and 0.4-second remaining queue,
before actor inference. Total estimated cost was $0.00347928 (Cloudflare input-token
estimate plus OpenRouter response-reported cost). No GPU, R2 or live intervention
was used. Evidence: `.local/decision-gate/latency-ready/analysis.json` and individual
receipts. This small ordered sample does not isolate caching effects or establish
worker-side latency; worker-side placement remains untested.

The separate [Clef training-data plan](clef-training-data.md) specifies matched
continuations, latency-aware outcome labels and initialization-group splits.
Current shadow predictions are not counterfactual training labels.
