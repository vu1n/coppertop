# Cloud transfer and checkpoint round-trip

R2 is canonical. Modal Volumes and Runpod network volumes are disposable staging
caches; the Mac handles bounded manifests, phase receipts and reports only. The
old `gpu_launch.launch` reliability relay is a legacy execution path and is not
used by this refactor. Its consumed approval/reservation must never be reused.

`StageManifest` binds a run/release to at most 64 immutable object references and
3 GiB total bytes. Core inputs use the existing `objects/<prefix>/<sha>` layout;
native checkpoint stages contain the four pinned output artifacts. Temporary
files occupy a reserved namespace, so payload names cannot collide with receipt
or implementation paths. CPU staging checks actual lengths and SHA256 before a
provider commit barrier returns a readiness receipt. A marker alone does not
prove a provider commit. Failure retains diagnostics and produces no usable gate.
The 10.12 GB base model is outside this data-stage bound: Modal reuses a separately
validated image; Runpod needs an approved verified model image/cache. No fallback
model download is supplied.

`cloud_roundtrip.run_roundtrip` consumes trusted bound `PhaseCalls`. Bind Modal's
six Function `.remote` methods, or the analogous Runpod transfer operations plus
an independently reviewed Pod runner. Call order is CPU stage, GPU train,
independent stop verification, CPU R2 publication, CPU R2 restaging, separate GPU
verifier, independent stop verification, CPU durable report publication. Provider
calls must own cleanup if allocation fails before an identity is returned. The
controller records partial failure and unverified cleanup distinctly, never
retries, and rejects reuse of an existing run directory. Freshness requires a
different provider container identity; PID inequality across containers proves
nothing. Native result checks share the model/revision, release, cutoff, artifact
length/hash and exact finite velocity contract in `cloud_smoke`.

The fixed native commands accept cloud-mounted paths. They preserve the richer
sidecars and head-weights-only scope; no optimizer/RNG resumption is implied.
`smoke_artifacts` remains the canonical checkpoint publication implementation:
stream payloads, verify R2 read-back, commit the immutable receipt last. Successful
report publication must match its committed manifest hash.

[Modal adapter](modal-staging.md) registers Functions locally, using mounted
Function `Volume.commit`, per-run subpaths, CPU-only role-scoped transfer secrets,
read-only GPU input mounts and separate single-use GPU Functions. GPU secrets are
empty and outbound network is blocked. The image must contain this updated
package plus the reviewed native dependencies/model; the old image receipt does
not validate a new recipe. SDK 1.6.1 registration was checked offline. Registration
is not deployment, mounted-storage execution, CUDA verification or teardown proof.

[Runpod adapter](runpod-staging.md) uses its documented network-volume S3 protocol
and datacenter constraints. It never substitutes Modal commit behavior or assumes
CPU-only Pods are available beside the selected GPU. External CPU scratch and S3
read-back establish transfer gates; filesystem visibility must still be proved
live. Its S3 authentication is separate from R2, has no documented temporary or
presigned route, and must stay outside GPU compute. Current account API 403,
model-cache availability and concrete Pod lifecycle integration remain blockers.

## Next live authorization

Local implementation approval does not authorize any of the following. Before a
live run, produce fresh read-only billing/access gates and a priced envelope that
retains every earlier unsettled reservation (currently conservative Modal total
$21.86459248 against the previous $22 cap). The old $6.50 single attempt is consumed.
Do not infer Runpod's reported $25 credit balance is verified API access or capacity.

For Modal, name two dedicated Coppertop Volumes, region, storage byte ceiling,
retention/cleanup policy and storage cost. Authorize the updated CPU image gate,
short-lived sessions for exact R2 input objects and a dedicated output prefix,
and their CPU Secret bindings. Parent keys remain local; session expiry has no
renewal/fallback. The default bounds are each CPU transfer Function 1,800 seconds,
each GPU Function 600 seconds, training subprocess 480 seconds, verifier subprocess
120 seconds, and separate startup 300 seconds. Specify one train and one fresh
verifier, or a reload-only follow-up using the existing durable checkpoint; no
retries/upgrades. Account for startup, commit/shutdown latency, CPU transfers,
network egress, R2 operations/storage and Volume retention separately. The stage
3 GiB bound is per manifest, not the total retained Volume bill; checkpoints have
both generated and R2-restaged copies, and failures may leave partials.

For Runpod, first resolve the existing 403 through the user's normal account setup
without changing permissions automatically. Name a supported Secure Cloud
volume/datacenter, capacity and retention; verify matching GPU capacity and an
existing reviewed base-model image/cache. Approve an external CPU transfer route,
its independent hard time/byte limits and separately reviewed Runpod S3 access
mechanism. Any new key, token, secret binding, proxy or volume is a separate explicit
approval item. Price staging/storage/egress separately from Pod compute; stopping
or terminating a Pod does not delete its network volume or end storage charges.
No new cloud resources, credentials, paid jobs, deployments or Git publication
were performed by this implementation.
