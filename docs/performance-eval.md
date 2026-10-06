# Paired closed-loop checkpoint pilot

Compare the pinned official Xiaomi RoboCasa365 model with the preserved native
head after one SGD update on one CloseBlenderLid episode. The latter is a smoke
checkpoint, not a trained production policy. The successful fresh-process reload
proves artifact restoration, not task success or improvement.

The first pilot runs CoffeeSetupMug, target50 index 3, with its full 600-step
official horizon, split `pretrain`. Execute episodes 0 and 1 from the official
50-trial indexing with base seed 1000: actual environment seeds 1150 and 1151.
This excludes the optimization task/episode and prior DEV seeds 8,9,12,15,23.
It is held out from this smoke optimization and exploration only; the official
pretrained model may have seen this task. Do not claim unseen benchmark data.

Run four episodes: both arms on both seeds, alternating arm order. Each episode
starts fresh model and simulator processes, seeds model RNG after loading,
and requires exact matching initial native state, simulator state, selected
physical/visual model fields, three camera arrays and task instruction across
the pair. A mismatch stops comparison; it never changes simulator state to make
the guard pass. The official evaluator resets the environment with the scheduled
seed, handles action chunks and uses `info["success"]`. Done is not success.

The pinned Xiaomi evaluator/client/server execute in their original separate
environments. The wrapper directly initializes the stock server with offline
weights and, for the second arm, the verified head; its stock forward transport
and processor action decoding are retained. The socket binds only 127.0.0.1
inside an isolated, secret-free job. There is no public endpoint. The official
pickle protocol is confined to these trusted owned processes. The child
environment excludes credentials, PYTHONHOME and MUJOCO_EGL_DEVICE_ID.

Compatibility: PandaOmron controller; EE-first native 14D state padded to 60;
12D policy actions converted by pinned RoboCasa code; three 256x256 cameras;
history 4, interval 2, crop .95, replan 16. Pin MuJoCo 3.3.1, Gymnasium .29.1,
Transformers 4.57.1, and the source revisions embedded in the plan. Simulator
checkout HEAD and clean tracked contents are checked before imports; the three
official evaluator/transport files are checked by Git blob hashes before loading.

Record successes/counts, per-episode steps/termination, inference queries and
seconds, model-load seconds, total runtime, checkpoint/model/source pins,
initial-state hashes and code hashes. Report each arm's Wilson 95% interval,
paired gains/losses and the exact discordant-pair test. Two seeds produce very
wide uncertainty; no benchmark superiority or data-utility claim is justified.
Interventions and scorer are `null`: neither is implemented or used.
Errors/timeouts/incomplete pairs remain invalid runs, not policy failures.

## Offline preparation

```sh
python tools/run_performance_eval.py --prepare > .local/performance-plan.json
```

Preparation imports no simulator/model, reads no credentials and performs no
provider calls. It returns the exact plan and SHA-256 binding, including all four
reviewed runtime module hashes. CPU fixtures exercise schedules, metrics, integrity,
pair guards and failures; they are not simulator execution.

The existing aligned image `im-jSDPqh8D8lT4vK8OLVkOvP` was read from the completed
verifier's provider metadata. `performance_eval.runtime_image` adds the four
reviewed modules with `copy=False` as runtime files. It performs no lookup,
image build, package installation or download. Its caller must supply the
already verified image handle. Preserve `/app` on PYTHONPATH for both interpreters.

Before GPU admission, a bounded CPU job must verify model snapshot hashes/native
pins, the original checkpoint receipt/head, both interpreters, the six completed
asset archives' provenance and all 123,236 extracted file lengths (24,063,148,172
bytes), source checkouts, task registry and horizon. The archived 10,986,648,838
compressed bytes were already acquired and extracted; no fresh download is
permitted. Missing cache or failed checks stop the job. The simulator gate
checks assets/imports but **does not claim reset/render execution**. The first
GPU rollout additionally checks the real EGL rendering/reset/camera/controller
path; no performance comparison is valid unless all four rollouts finish.

The cloud controller is executable locally with the pinned Modal 1.6.1 controller
Python. Prepare an exact attempt against a local proposal ledger preserving every
historical hold and setting its cumulative cap to 48:

```sh
python tools/run_performance_cloud.py --prepare \
  --inputs containers/performance-inputs.json \
  --ledger /path/to/proposed-ledger-48.json --attempt reload-16_HEX_DIGITS
```

Preparation reads public object references and code hashes only. It returns
`cloud_plan` and `cloud_plan_sha256`; approval must bind that exact digest.
Use the same inputs, ledger and attempt with `--execute` and
`--approved-cloud-plan-sha256 EXACT_HASH` only after budget approval.
The default environment file is `~/code/coppertop/.env.local`; no keys are
requested in chat. Changing code, inputs, ledger or scope invalidates approval.

The bounded controller stages the 17 source objects and five checkpoint objects
on separate CPU jobs, verifies and commits their manifests, then runs the paid
CPU cache/native-processor/simulator-import gate. No GPU starts unless all CPU
gates pass and their containers stop. The CPU gate also checks loopback transport
under external-network blocking. This runtime validation inherently needs a paid
CPU job, included in the same proposed envelope; it has not run yet.

The fresh secret-free GPU job runs `coppertop.performance_runtime` with the
verified head under `/coppertop-reload-inputs`, performs all four official
closed-loop rollouts, and returns only bounded metadata. A separate CPU publisher
writes the immutable report and verifies its readback. The controller creates one
exclusive consumed marker, never retries, stops/audits the exact App and deletes
only the captured newly created Volume identity. Original checkpoint objects and
reports remain in R2. Native failures with bounded partial metadata can produce
an invalid-run report; provider hard termination may prevent that report, but the
local terminal receipt and teardown audit still record failure. No missing or
partial result counts as policy failure or comparison success.

## Proposed resource and access envelope

The first approved attempt stopped at CPU simulator validation, before GPU
admission; its four-rollout approval is consumed and cannot be reused. Its approved
additional maximum was $10;
with $37.86459248 in retained holds, request a cumulative ceiling of $48.
Holds are reservations, not actual spent charges. No old hold is released.

One US L40S Function, `(4,4)` CPU cores and `(65536,65536)` MiB memory, timeout
1800 seconds plus at most 300 startup seconds; concurrency 1, zero retries,
single-use, minimum/buffer containers 0. Each server is limited to 420 seconds,
startup 90, each simulator 330, and the complete CUDA runner to 1800. If full
600-step episodes do not fit, report timeout; never shorten the horizon or retry.
CPU source stage: two hard cores/4GiB, 600+300 seconds. CPU cache gate: four
hard cores/16GiB, 1200+300 seconds. Checkpoint stage: two
hard cores/4GiB, 1800+300 seconds. Metadata publisher: two cores/4GiB, 300+300.
Controller deadline 5400 seconds, followed by bounded cleanup on failure.
Independent audits each take at most 60 seconds. GPU admission requires at least
3000 seconds remaining for execution, publication and teardown; otherwise stop.
Fresh billing must fit retained holds plus $10, and CPU costs above $.75 block GPU.

One newly owned input Volume, at most 3GiB. CPU verifies/commits before GPU;
GPU mounts read-only with empty secrets and blocked external networking.
Source/checkpoint permissions use separate two-hour
CPU-only sessions, limited to the 17 source and five checkpoint object paths;
report access is limited to one new attempt prefix. No listing, deleting,
multipart, renewal, permanent credential or parent credential in compute.
Bound aggregate R2 requests to 2000, new report bytes to 64KiB and provider
metadata egress to 1GiB. Checkpoint R2-to-Modal transfer is inbound.
The report uses `chords/gpu-reload/<attempt>/objects/` to retain the existing
narrow access contract; its record kind identifies this performance evaluation. No model, simulator asset or dependency download. Stop/audit the
exact App and zero active containers/Sandboxes before deleting only the captured
new Volume identity. Retain original checkpoint and immutable report in R2.

Conservative estimate at current prices, reserving a 1.75 region factor:

| Item | Upper reservation, USD |
| --- | ---: |
| L40S+4 cores+64GiB, 2100 seconds | 2.706564 |
| CPU source stage, 900 seconds | .055251 |
| CPU gate, 1500 seconds | .230790 |
| CPU transfer, 2100 seconds | .128919 |
| CPU publisher, 600 seconds | .036834 |
| 3GiB Volume for a full month | .270000 |
| Provider egress, 1GiB | .040000 |
| R2 rounded Class A and B request units | 4.860000 |
| R2 storage, conservatively 1GB-month | .015000 |
| Total envelope | 8.343358 |

Reserve $10 to cover this envelope; assume no credits or free tier. Runtime
mounts avoid the unbounded implicit COPY-build charge issue. These are proposal
calculations, not measured evaluation costs. Refresh rates, billing and exact
resource availability before consuming any approval. If new builds/assets or
scope changes become necessary, stop and revise the concrete plan.

## Sources and attribution

Xiaomi source/model are Apache-2.0; RoboCasa code is MIT and assets/datasets are
CC-BY-4.0. This wrapper imports cached source rather than copying it. Preserve
publisher attribution/license files with results and any redistributed assets.

- [Pinned official evaluator](https://github.com/XiaomiRobotics/Xiaomi-Robotics-1/blob/0dd7aef8dc87296246aae812a1f59ccb708e5546/eval_robocasa365/entry.py)
- [Pinned RoboCasa registry and license](https://github.com/robocasa/robocasa/tree/456174f62b89b8fca99eaaf33949c29fec9cfc2a)
- [Pinned model](https://huggingface.co/XiaomiRobotics/Xiaomi-Robotics-1-RoboCasa365/tree/3a6d0293bfa90759d34a7fc48c2c62413cd7bcf4)
- [Modal prices](https://modal.com/pricing/), [R2 prices and rounding](https://developers.cloudflare.com/r2/pricing/)

## First live attempt and diagnostic correction

Attempt `reload-9b8bee11ac163e72` used reviewed commit
`eb15b57e8717b7ae684c719904c6d97b4bc18cc7`. Source and checkpoint CPU staging
committed and stopped; native cache/processor validation reached the simulator
subprocess, which failed. GPU never started; zero rollouts ran. Exact-App
shutdown, zero containers/Sandboxes and temporary Volume absence were independently
verified. Original checkpoint objects remain preserved. No evaluation report or
success-rate estimate exists for this invalid attempt.

The original wrapper captured but discarded the simulator's stdout/stderr.
Provider logs therefore establish only `Simulator source/assets/import CPU gate
failed`; they do not establish the underlying exception. Recorded dependency
versions, receipt size, asset counts and byte totals match the gate. Official asset
extraction writes 12 Git-tracked cabinet-panel XML paths; legitimate asset changes
could conflict with whole-checkout cleanliness, but this is not a proven cause.
Do not relax source or asset validation based on that possibility.

The corrected child emits a structured failure receipt with the failing check,
exception type, controlled guard reason or missing module, and traceback filename,
line and function. It excludes arbitrary exception text, source lines, locals and
raw stderr. The parent validates the bounded schema and persists failure metadata
before teardown. Invalid output retains only exit status and output byte counts;
timeout/missing interpreter retain exception type. Failed receipts still forbid
GPU. Captured output uses temporary files; retained/read metadata is bounded,
while third-party output spool size is constrained by the existing job lifetime.

The smallest next live diagnostic is one CPU-only job on the same cached image,
running `performance_cloud.simulator_preflight()` with the reviewed runtime mounts.
It needs no R2 access, secrets, input Volume, model loading, GPU, builds or downloads.
It would reveal the failing simulator check, not establish rendering or performance.
No such diagnostic retry has been authorized or executed. Previous run receipts
and all budget holds remain unchanged. Changed code invalidates the old launch
digest; any further paid action needs a separately bound approval.

## Minimal CPU-only diagnostic proposal

`tools/run_simulator_diagnostic.py` prepares one exact-approval Function using the
same cached image and four runtime mounts. It does not access R2, create keys,
inject secrets, mount Volumes, load models, allocate GPU, build images or download.
One US container has two physical CPU cores and 4GiB RAM as hard limits, a
900-second function timeout, 300-second startup timeout, 600-second simulator
subprocess timeout, concurrency one, zero retries and no warm/buffer containers.
The local controller is limited to 1500 seconds, followed by bounded exact-App
shutdown/audits (60 seconds each). Only its captured App is stopped; there is no
storage resource to delete. Structured metadata is retained locally.

At the published Modal rates, reserving a 1.75 region factor and 1260 seconds
(startup, runtime and a 60-second shutdown allowance), compute is $0.0773514.
A conservative 1MiB metadata allowance adds $0.000039063. Builds, new storage,
R2 operations and model downloads add zero. Total estimate: **$0.077390463**,
without free credits. Reserve **$0.10**, rather than another $10.

Retained historical holds total $47.86459248, leaving $0.13540752 under the
existing $48 ceiling. The new reservation yields $47.96459248, retaining every
old hold and leaving $0.03540752. Fresh billing is mandatory before allocation
and after cleanup; unaccounted spend or an exceeded old hold blocks execution.
The diagnostic refuses an existing pending attempt rather than replacing it.

```sh
python tools/run_simulator_diagnostic.py \\
  --ledger /path/to/preserved-current-ledger.json --attempt diag-16_HEX_DIGITS
```

This default prepares the plan without provider access. After separate approval,
the same arguments plus `--execute --approved-plan-sha256 EXACT_HASH` consume the
exact code/ledger-bound plan once. No diagnostic has been launched by preparation.

## CPU diagnostic result

Approved attempt `diag-a072f9d9b1774f30`, commit
`61086635b34227340af7d88ee13a874022abee28`, retained the exact child failure:
`TimeoutExpired` at `checkout_robocasa`, from `git status --porcelain
--untracked-files=no` with its original 10-second timeout. This establishes why
this diagnostic stopped; it does not recover the original evaluation's discarded
exception or establish whether any tracked files differ. Dependencies and assets
were not reached. No GPU, R2, Volume or model loading occurred.

Git checkout commands now allow 60 seconds each within the same 600-second
preflight; revision and cleanliness checks remain mandatory. The longer allowance
has not been verified live. The initial shutdown audit raced container draining;
a later independent exact-App audit proved stopped/zero containers/Sandboxes.
The diagnostic launcher now waits through up to three exact-App metadata audits
within 180 seconds, without repeating diagnostic work. Persistent activity still
fails cleanup. Retained holds total $47.96459248, and the consumed diagnostic
approval cannot be reused for changed code or another job.
