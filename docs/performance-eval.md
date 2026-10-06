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
provider calls. It returns the exact plan and SHA-256 binding, including both
new runtime module hashes. CPU fixtures exercise schedules, metrics, integrity,
pair guards and failures; they are not simulator execution.

The existing aligned image `im-jSDPqh8D8lT4vK8OLVkOvP` was read from the completed
verifier's provider metadata. `performance_eval.runtime_image` adds the two
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

After approval, the isolated CUDA job runs:

```sh
/opt/conda/bin/python -m coppertop.performance_runtime --execute-cuda \
  --plan /tmp/exact-plan.json --approved-plan-sha256 EXACT_HASH \
  --head /coppertop-inputs/bdacc7fa4604fd52a32621eb21dcbd4bb2a93375c2bd1a16247af5dae4e64d5d/head.safetensors \
  --output /tmp/paired-eval
```

The plan file contains the `plan` field of offline preparation, not its enclosing
JSON receipt. The CUDA runner creates one exclusive consumed marker, no retry,
and kills only its owned subprocess groups on completion or error. It allocates
no provider resources. A separately approval-bound cloud controller must stage
on CPU, enforce function limits, publish/read back the bounded report and audit
teardown; this draft does not implement or claim that new cloud launch.

## Proposed resource and access envelope

No paid evaluation is authorized or started. Proposed additional maximum: $10;
with $37.86459248 in retained holds, request a cumulative ceiling of $48.
Holds are reservations, not actual spent charges. No old hold is released.

One US L40S Function, `(4,4)` CPU cores and `(65536,65536)` MiB memory, timeout
1800 seconds plus at most 300 startup seconds; concurrency 1, zero retries,
single-use, minimum/buffer containers 0. Each server is limited to 420 seconds,
startup 90, each simulator 330, and the complete CUDA runner to 1800. If full
600-step episodes do not fit, report timeout; never shorten the horizon or retry.
CPU cache gate: four hard cores/16GiB, 1200+300 seconds. Checkpoint stage: two
hard cores/4GiB, 1800+300 seconds. Metadata publisher: two cores/4GiB, 300+300.
Controller deadline 5400 seconds. Independent audits each at most 60 seconds.

One newly owned input Volume, at most 3GiB. CPU verifies/commits before GPU;
GPU mounts read-only with empty secrets and blocked external networking.
Existing source/checkpoint session scopes may be reused as separate two-hour
CPU-only sessions, limited to the 17 source and five checkpoint object paths;
report access is limited to one new attempt prefix. No listing, deleting,
multipart, renewal, permanent credential or parent credential in compute.
Bound aggregate R2 requests to 2000, new report bytes to 64KiB and provider
egress to 1GiB. No model, simulator asset or dependency download. Stop/audit the
exact App and zero active containers/Sandboxes before deleting only the captured
new Volume identity. Retain original checkpoint and immutable report in R2.

Conservative estimate at current prices, reserving a 1.75 region factor:

| Item | Upper reservation, USD |
| --- | ---: |
| L40S+4 cores+64GiB, 2100 seconds | 2.706564 |
| CPU gate, 1500 seconds | .230790 |
| CPU transfer, 2100 seconds | .128919 |
| CPU publisher, 600 seconds | .036834 |
| 3GiB Volume for a full month | .270000 |
| Provider egress, 1GiB | .040000 |
| R2 rounded Class A and B request units | 4.860000 |
| R2 storage, conservatively 1GB-month | .015000 |
| Total envelope | 8.288107 |

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
