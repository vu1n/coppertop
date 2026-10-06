# Reload an existing native checkpoint

`python tools/run_reload_only.py --prepare --plan PLAN.json --ledger LEDGER.json`
validates the local plan, retained historical holds and exact code recipe. It
prints public hashes without loading an env file, looking up providers or
creating resources. The plan and billing ledger are operator metadata retained
outside Git. The launcher supports this fixed checkpoint milestone only.

After explicit approval of the full plan, the same command with `--execute
--approved-plan-sha256 HASH --env-file ~/code/coppertop/.env.local` consumes one
attempt before credential reads, temporary signing or provider access. Changed
code/plan hashes, expired sessions, missing cache or existing Volume stop the
attempt. There is no retraining, model download, upgrade or retry.

The CPU source stage streams the pinned 17-object release from R2 into one new
input Volume, verifies hashes and acknowledges mounted commit. An independent
stop check precedes a secret-free CPU image gate that verifies the updated code,
all model snapshot hashes, native package/ABI imports and actual processor parity.
It instantiates no model weights. CPU then fetches the original immutable
checkpoint receipt and four artifacts directly from R2, verifies and commits
them, and stops before one L40S verifier starts.

GPU inputs are read-only and network is blocked. Native verification writes
only a report in local `/tmp`; at most 64 KiB of report metadata returns through
the controller to a CPU publisher. Verified GPU shutdown gates publication.
Conditional immutable R2 publication and exact read-back precede final teardown.
This needs one Volume: no checkpoint is trained and no bulk output is generated.
The general training round-trip adapter retains its separate input/output
Volumes because its GPU writes a large checkpoint.

Source and checkpoint manifests preserve the old checkpoint run ID. Freshness
compares actual Modal `ta-` container IDs against the authenticated original
training task ID. A Sandbox `sb-` ID remains provenance and cannot prove
freshness through a cross-namespace comparison. Passing proves head weights
restore exact finite velocity; it does not resume optimizer/RNG state.

The reviewed SDK is Modal 1.6.1. Build/gate use four hard CPU cores and 16 GiB;
transfers use two hard cores and 4 GiB; the single GPU verifier uses one L40S,
four hard cores and 64 GiB. CPU build is at most 600 seconds, entire App entry
900; source stage 600; CPU gate 1,200; checkpoint restage 1,800; GPU Function
300 with native subprocess 120; report publisher 300. Function startup is
separately bounded to 300, each audit to 60, and the whole controller to 7,200.
All containers are single-use with zero retries/warm minimum/buffer.

The warm seed retains the native CUDA model and wheel. Only seven pinned pure
Python R2 packages in `requirements-r2.txt` are installed by the CPU build,
without dependency resolution; existing package directories occupy about
30.4 MiB. No native/model fallback installation occurs. Stale project sources
and bytecode are removed only within the disposable image's project package.
Offline SDK registration and resource-spec checks do not execute this build.

Three two-hour locally signed temporary R2 sessions grant source GET/HEAD for
17 exact paths, checkpoint GET/HEAD for five exact paths, and report GET/HEAD/PUT
only for the new attempt's `objects/` prefix. Only the corresponding CPU Function
receives each ephemeral Secret. The image builder, gate and GPU receive no
transfer secret. Parent credentials remain local, with no named persistent
Secret, listing/deletion/multipart rights, renewal or fallback. Request caps are
128/512/8 by role; SDK connection/read limits are 30/60 seconds with one total
attempt. Report writes stay below 64 KiB. [R2 temporary credential semantics](https://developers.cloudflare.com/r2/api/s3/temporary-credentials/).

Fresh billing uses the pinned CLI's actual schema. A full $8 pending hold is
saved before credential signing, then transferred to the actual App ID when
known. All old holds remain; failed allocation or stopped resources do not
release reservations. Cleanup stops and independently audits only the owned
App, then verifies the newly created Volume identity before deletion. Ambiguous
creation or active resources block deletion and surface an ownership receipt;
any unresolved Volume continues to incur storage charges until safely removed.
Original R2 objects and the existing seed remain untouched.

Local tests cover staged corruption, timing/replay barriers, exact report and
session binding, billing lag, allocation collision, partial App startup, resource
ownership and cleanup failures. Live CPU build, authentication, mounted
visibility, fresh CUDA reload and final provider teardown require the separately
approved run. [Modal pricing](https://modal.com/pricing) and
[region pricing](https://modal.com/docs/guide/region-selection) inform its envelope.
