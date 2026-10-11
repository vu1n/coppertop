# Retained Xiaomi/Clef pilot: offline preparation

This lane prepares an experiment manifest and offline test matrix at main
`7d3f7e3cc0d48864ce940209efbd6add56109069`. It has no execution option. The
existing official/one-update smoke pilot is unchanged. Its smoke head is not Clef.

## Missing inputs and evidence inventory

At this base commit, repository searches found no Clef inference/intervention
implementation, weight artifact, or evaluation config. Exact code revision,
checkpoint digest/location, config digest, Clef-off setting and action-intervention
semantics are required. Latency from 24 Clef calls does not establish rollout
success. Local Xiaomi weights and the prospective image have not been verified.
Existing Xiaomi file digests remain in `containers/gpu-smoke-pins.json`.

The parent verified the historical 24-call benchmark's final receipt, which names
`/Users/vuln/Documents/Codex/2026-10-05/task/coppertop-publish/docs/clef-colocation.md`,
and confirms both Apps stopped. This is a Mac-local historical location, not an
available cloud-dev input. The minimal reviewed nonsecret handoff is that report
and its implementation/hash receipt; the exact Clef inference/intervention source
files with revision/file digests and license; pinned runtime dependencies; the
evaluation config with off-switch, action interface, RNG and query/timeout bounds;
and a checkpoint manifest with immutable location, file sizes and SHA-256 values
(or an explicit weight-free design). Weight transfer/execution is a separate
step after identity review. No environment files, credentials or bulk model/data
exports are needed to identify these inputs.

The seed inventory is bounded evidence, not a complete historical ledger:

| CoffeeSetupMug DEV seeds | Evidence | Treatment |
| --- | --- | --- |
| 8, 9, 12, 15, 23 | `performance_eval.DEV_SEEDS`, performance pilot docs | Used; exclude |
| 1150 | Full-rollout docs report execution; mechanism config also selects it | Used; exclude |
| 1151 | Old performance pilot scheduled episode 1 | Conservatively reserve |
| 1152, 1153 | Episodes 2, 3 at base 1000, full target50 index 3 | Provisional candidates only |

The full-rollout doc describes **one failed staging attempt** that reported
success at 211 controls / 14 queries / 212 observations and retained only 9,194B
metadata. That attempt's payload is unavailable. This is not a statement that
all DEV1150 evidence was lost. The parent subsequently reports fully published,
hash-verified DEV1150 execute-16 and execute-8 archives in prior thread
`01a10b0d-dfbe-73a4-85b9-d3731fb24083`. Their exact run IDs, object/release hashes,
and receipts are absent from this checkout; Personal Context lookup returned
unavailable. No provider or bulk artifact retrieval was performed.

Those later archives may supply an **unpaired retained Xiaomi baseline** once
receipts establish source/model/evaluator/config, initialization, seeds, complete
closure and readback. Execute-8 is a cadence experiment; the proposed stock arm
uses execute-16. Neither archive establishes a retained Xiaomi/Clef pair or two
unused DEV seeds. Preserve both histories and reconcile receipts before reuse.

## Prepare and verify

From the isolated checkout, using the repo's existing Python environment:

```sh
mkdir -p .local/evaluation-readiness
/workspace/coppertop/.venv/bin/python tools/prepare_clef_pilot.py \
  > .local/evaluation-readiness/manifest.json
/workspace/coppertop/.venv/bin/python -m unittest discover \
  -s tests -p test_evaluation_readiness.py -v
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

| Gate | Offline evidence | Remaining runtime/input gate |
| --- | --- | --- |
| Task membership/horizon/seed schedule | All 50 original indices; 2,500 unique seeds; per-task horizons and query bounds | Confirm prospective runtime registry matches pins |
| History/crop/state/cameras/decode | Causal offsets -6,-4,-2,0; .95 crop; EE-first14D padded60; three cameras; first12D decode | Real processor/model and stock Clef-off parity |
| Two pairs | Same task/index/seed/horizon per pair; alternating arm order | Complete unused-seed ledger and matching native/simulator/camera/instruction initialization |
| Clef intervention | Missing inputs explicit; smoke head rejected as substitute | Retain Clef decisions and proposed/executed actions; verify an attributable action change |
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
observations and 152 Xiaomi queries. Extra Clef/probe queries need independent
bounds. These counts are quote inputs, not cost or timeout estimates. Obtain exact
model/Clef/evaluator/image digests, all query deadlines, per-episode load/startup/
runtime allowances, CPU staging/cache/readback/cleanup and GPU resource seconds,
raw-array/video/archive object and byte bounds, retention destination/duration,
fresh prices and the preserved spend ledger before quoting an approval envelope.
Old paid approvals cannot authorize this new experiment. No paid action is
authorized by this preparation.

The later full comparison is **2,500 episodes per arm**, 5,000 for two arms,
on RoboCasa1.0.1 target50 in the pretrain kitchen/object split, 50 trials per task.
Retain every original target50 index and `num_trials=50` even for subsets:
`seed = base_seed + full_target50_index * num_trials + episode_index`.
Upstream seed 7 is a reference/default, not a required rule. Registry horizons
range from 450 to 4,350; never shorten them to meet timeouts.

Protocol sources: [RoboCasa multitask protocol](https://robocasa.ai/docs/build/html/benchmarking/multitask_learning.html),
[pinned Xiaomi evaluator](https://github.com/XiaomiRobotics/Xiaomi-Robotics-1/blob/0dd7aef8dc87296246aae812a1f59ccb708e5546/eval_robocasa365/entry.py),
[pinned RoboCasa registry](https://github.com/robocasa/robocasa/blob/456174f62b89b8fca99eaaf33949c29fec9cfc2a/robocasa/utils/dataset_registry.py).
