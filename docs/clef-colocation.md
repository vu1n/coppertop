# Clef and Xiaomi in one GPU container

## Latest result: cached benchmark completed

The approved cached attempt completed successfully: CPU image build and all three
real-input checks passed, followed by 15 actor and 9 Clef calls on one A100-80GB.
Reusable image: `im-0ZJ0cSfNXwzFEUZjAdRiSF`. It retains public weights and dependency
packages; the DEV inputs were supplied separately at runtime. Both owned apps
were stopped and audited: zero active containers and sandboxes. No live controls
or training ran.

Medians below exclude warmup; each cell has three saved DEV cases in one session.

| Condition | Actor median | Clef median |
| --- | ---: | ---: |
| Actor alone, before Clef load | 385.6 ms | — |
| Both loaded, serial calls | 398.7 ms | 894.8 ms |
| Overlapping calls | 917.7 ms | 1133.7 ms |
| Actor alone, after Clef exit | 396.9 ms | — |

Overlapping work slowed the actor about 2.38× relative to its initial baseline.
All actor chunk hashes were identical for each case across phases. Clef's serial
range was 885.2–895.0 ms; overlap was 1129.6–1143.4 ms. These results miss both the
100 ms freshness target and 400 ms remaining-queue window. This configuration is
unsuitable for the current fast intervention gate. They do not establish the
latency of an optimized implementation: the official wrapper still uses the
Torch fallback without optimized FLA/causal-conv1d kernels. Initial saved-PNG decode
and future frame IPC are excluded from the Clef timings. This is a small DEV
feasibility measurement, not a tail-latency estimate or intervention outcome.

Post-load GPU memory snapshots were 10,653 MiB for the actor and 29,355 MiB with
both models loaded; these are not peak inference measurements. Local receipts:
`.local/decision-gate/colocation-cache/{image,cpu-response,gpu-response,result,analysis,cleanup}.json`.
All returned logs and JSON receipts fit their limits without truncation.
The conservative resource allowance for this attempt was $0.92173264 including
small receipt egress; settled charges were not queried. The user subsequently
waived repeated budget approval for routine future test spending.


This saved-input benchmark tests the existing public Clef Flash model alongside
our frozen Xiaomi actor. It does not tune weights or control the simulator.

The actor retains its cached Torch 2.8 / Transformers 4.57.1 environment. Clef
runs in a separate Python environment in the same container and on the same
A100-80GB, using Torch 2.11.0+cu128, torchvision 0.26.0+cu128, Transformers 5.10.2,
Accelerate 1.11.0 and Pillow 11.3.0. The official joint-schema head is used;
ordinary text generation is not a substitute for it. Clef revision is
`fde727a287004204b7518dcc983fe64379776712`, totaling 19,083,377,419 release bytes.
Additional dependency storage is estimated at 5–12 GiB; the actual resolved
packages are recorded in the CPU staging pip report.

CPU staging installs the isolated environment, verifies pinned release file sizes
and LFS SHA256 hashes, constructs the processor and encodes all three real inputs.
The final direct launcher performs setup and inference in one sandbox. Network
access remains available for public downloads; inference sets Hugging Face and
Transformers offline flags, which are not a firewall. No secrets, R2 access or
named Volumes are used. Every owned sandbox must be stopped and checked after
success or failure. An earlier snapshot request timed out and had a one-hour TTL.

The three Clef cases are saved execute-16 DEV1150 frames 8, 104 and 200; actor
inputs are the preceding recorded query histories at frames 0, 96 and 192.
These test latency under representative saved inputs; they are not a shared
observation-time intervention pair. Images are decoded once before timed calls.
Clef call timing includes its official tokenizer/processor, transfer, forward and
answer conversion, but excludes initial PNG decoding. Actor timing includes the
existing loopback official client, processing, forward and action decoding. The
client uses `/work/sim/bin/python` (CPU Torch); the model server uses the original
`/opt/conda/bin/python`. Clef uses `/tmp/clef-env/bin/python`. No CUDA operations
are performed in the actor client. Prepared arrays/images remain in worker memory;
these timings exclude transporting a newly observed frame into a worker process.

The schedule is three actor warmups, three actor-alone calls, then Clef load and
three Clef warmups. Three serial pairs measure co-residency; three overlapping
pairs probe contention. Clef then exits and three actor-alone calls check drift.
Total: 15 actor and 9 Clef calls. Timing includes CUDA synchronization. Only Clef emits process allocator metrics; whole-device nvidia-smi snapshots
report combined occupancy. Actor outputs arrive after server-side computation,
so the CPU client needs no CUDA synchronization. Overlap is requested
by two processes, not proof of concurrent GPU kernels. With only three cases per
phase, report individual observations and medians, not robust tail estimates.

The prepared launch uses at most 900 seconds of CPU staging (4 CPU, 32 GiB) and
600 seconds of GPU lifetime (A100-80GB, 4 CPU, 64 GiB), no application retries, and
a $3 envelope. At checked rates, multiplying sandbox CPU/memory and GPU bounds
by a conservative 1.75 region factor gives $1.927002, excluding negligible result
egress. Internal artifact-download transport behavior is not an inference retry.
No existing spend hold is released by this test.

Attempt evidence is under `.local/decision-gate/colocation-*`:

- `colocation-ready`: dependency installation passed; inherited offline mode
  blocked the model download. CPU cleanup verified.
- `colocation-repair`: model verification and all three camera encodes passed;
  filesystem snapshot timed out. CPU cleanup verified.
- `colocation-direct`: actor server loaded; client used the wrong interpreter
  and lacked imageio. Cleanup verified; client wiring subsequently corrected.
- `colocation-client-fix`: six actor calls recovered after local controller loss;
  no Clef result. Recovery confirmed no active sandboxes. An empty-log retrieval
  error prevented retrieval of the Clef failure log.
- `colocation-diagnostic`: bounded final attempt with structured worker errors,
  startup GPU metadata, and text-stream log retrieval. Its prepared combined
  resource upper bound is $2.9167376, including terminated earlier attempts and
  the final 450-second maximum. This is a resource estimate, not settled billing.

The launcher stops after failure. Manual repairs are separate consumed attempts;
none retries an inference call automatically. No live robot controls are run.
Failure receipts are saved before process cleanup so an exception cannot masquerade
as a completed benchmark. CPU fixture/protocol tests are separate
from actual GPU execution; success is reported only from the latter's result.


## Latest measured outcome

The final diagnostic captured the failure before Clef inference: `idna` was
missing when Hugging Face imported httpx. CPU installation had seen dependencies
through Modal's injected `PYTHONPATH`, while worker execution used `/app` only.
The environment was therefore incompletely isolated despite its earlier pip check.
The repair uses the same clean environment for installation and execution, strips
inherited Python home/user-base/virtualenv settings, disables user-site packages,
and checks idna/httpx/snapshot_download imports explicitly. Local regression tests
pass; the repaired environment has not yet been rerun on a GPU.

The diagnostic completed six actor calls (three warmup, three measured), with
measured median 0.339536418 seconds. Clef made zero inference calls. At its startup,
73,482,502,144 GPU bytes were free; this is not proof that Clef loaded or fits under
inference load. All owned sandboxes are confirmed stopped. Conservative resource
upper bound across attempts is $2.7713014 including 30-second margins, leaving
$0.2286986 of the $3 envelope; actual billing is not reconciled. No new run is
permitted by the prepared environment-fix launcher without a sufficient approved
budget. Its proposed resource upper bound is another $1.006866.

Exact evidence: `.local/decision-gate/colocation-diagnostic/analysis.json`,
`result.json`, `worker-error-clef.json`, and `cleanup.json`. The unexecuted repair
is `.local/decision-gate/colocation-env-fix/`; prior consumed plans remain unchanged.


## Environment-fixed attempt and rounded-output repair

The authorized envelope was increased to $4.10. `colocation-env-fix` passed the
isolated dependency checks, loaded both models on A100-80GB and completed a Clef
forward pass. The benchmark then rejected the official wrapper's four-decimal
probabilities because the generic sum tolerance was 1e-5. This was a validation
failure, not evidence that model inference failed. Cleanup is confirmed.

The benchmark now opts into a four-decimal grid check and a sum-error bound of
`number_of_options * 0.00005`, plus floating-point epsilon. It retains the original
probabilities; it does not silently normalize them. Default validation and the
live gate remain strict. Regression tests cover admissible rounding drift and
invalid probability mass.

`colocation-rounded` runs the same workload as one short-lived Modal Function,
with lower CPU/memory rates than Sandbox. Its bound includes 20 seconds startup,
280 seconds execution and 2 seconds scaledown. The supplied conservative combined
resource estimate is $4.09110548, within $4.10; settled billing remains separate.
No additional job runs concurrently. Results and cleanup must be read before
claiming the benchmark completed. Clef currently uses the Torch fallback for its
linear-attention path; optimized FLA/causal-conv1d kernels are not installed, so
this is not an optimized-serving latency result.


## Closure of the $4.10 attempt

The environment repair passed and both models loaded together; Clef completed a
forward pass before rounded-output validation stopped that attempt. After the
precision repair, `colocation-rounded` reached its 265-second subprocess deadline
and returned only a timeout, without timing receipts. Its owned app was stopped
and audited with zero active containers and sandboxes. The full comparison is
**incomplete**. There is no measured scorer latency or actor contention result.

The final conservative resource upper estimate is $4.09110548 under the approved
$4.10 envelope; this is not a settled invoice. No further paid work was launched.
32 focused CPU tests pass. The next attempt should stage a reusable validated
runtime before reserving a GPU benchmark window; repeating package/model setup
inside short evaluation windows proved unreliable. Preserve the successful
same-GPU loading/forward evidence without calling it a latency benchmark pass.
Final evidence: `.local/decision-gate/colocation-rounded/analysis.json`,
`response.json`, and `cleanup.json`.

## Prepared reusable runtime (not executed)

The next launcher is `.local/decision-gate/colocation-cache/run.py`. It builds a
CPU-only Modal image from the existing Xiaomi image, saves the image ID, validates
all three real saved inputs on CPU, then calls a separate A100-80GB function.
GPU execution contains no package installation or model download. No cloud work
has been launched for this preparation.

`containers/clef-runtime.lock` freezes the 56 exact wheel URLs and SHA256 hashes
from the successful isolated environment. `coppertop/clef_cache.py` installs them
with dependency resolution disabled and hashes required, checks imports and pip
consistency, downloads the pinned 19,083,377,419-byte public Clef release, checks
its file sizes/LFS hashes, and encodes synthetic images offline. Public weights
and dependencies remain in the reusable image under `/opt/coppertop-clef`.
The separate 5.4 MB runtime payload contains the saved DEV inputs and project
source; it is mounted only into disposable validation/benchmark containers.
No credentials, R2 access, named Volumes, model training, or live controls enter
this attempt. This retains the existing Torch fallback; it does not add optimized
FLA/causal-conv1d serving kernels.

The CPU gate rechecks dependencies and encodes all real Clef cases, and checks
actor state/camera array shapes. The benchmark requires all 15 actor and 9 scorer
observations with finite positive timings. Timeout handling terminates the worker
process group and returns partial JSON receipts intact, with explicitly marked
bounded log tails. Cleanup attempts both owned apps even if either audit fails.
The actor's default lifetime remains 240 seconds; this benchmark selects 450.

Resource accounting uses the full builder startup plus execution limits: 2,660
seconds including 60 seconds cleanup, 4 CPU cores/32 GiB. CPU gate: 322 seconds
including startup/scaledown/cleanup, 4 cores/32 GiB. GPU: 622 seconds including
startup/scaledown/cleanup, one A100-80GB, 4 cores/64 GiB. Standard Modal rates checked
2026-10-09 give $0.92073264 for these resources, plus $0.001 allowed for small
receipt egress. These are conservative resource estimates, not settled invoices
or a provider billing enforcement mechanism. The proposed additional envelope is
$2 ($6.10 total for this colocation sequence); it remains unapproved. No automatic
retry is enabled. A consumed-plan receipt prevents rerunning this prepared launch.

Local verification: 45 focused tests (10 colocation, 24 decision-gate, 11 mechanism
runner) pass. Offline construction with Modal1.6.1 accepts the builder definition;
its resource converter emits both requested and maximum CPU/memory values.
Independent static review found the cleanup issue above, now fixed, and no other
confirmed structure/correctness/security blocker. Actual Linux dependency checks,
image build, real CPU gate, and GPU timing remain pending execution.
