# Runpod staging boundary

`RunpodStager` runs on an explicitly approved external **CPU transfer worker**.
It streams canonical R2 objects to CPU scratch through the shared `stage_objects`
contract, then uploads those verified files to a dedicated network volume through
Runpod's S3 API. It verifies every uploaded object's size/SHA256 and uploads the
shared `stage.json` receipt last. R2 stays canonical; the network volume is a
replaceable cache. Bulk payloads never traverse the Mac controller.

Before allocating a GPU, `require_ready` verifies the receipt and payloads through
S3 and binds the manifest, volume ID and exact datacenter. The GPU must also run
`verify_stage` on its mounted directory before loading anything. Pods normally
mount the volume at `/workspace`; `/runpod-volume` is the Serverless mount. Explicit
configuration supports both, with no assumption that a Pod uses the Serverless
path. Inputs are immutable by application contract; require a single writer per
manifest namespace and prevent GPU writes to input paths. Runpod does not expose
S3 ACL, policy, versioning or object-lock primitives used as enforcement here.

The adapter uses 8 MiB multipart uploads for larger payloads, not the R2-specific
conditional-write adapter. Runpod requires multipart above 500 MB. Transfers have
an independent deadline checked between reads/requests. Inject a client configured
for the exact endpoint/datacenter, finite connection/read timeouts, and no automatic
retries. A worker hard timeout and storage/transfer budget remain launch gates;
the adapter's deadline cannot interrupt an already blocked SDK request.

The implemented GPU-output boundary is `seal_runpod_output(manifest, directory)`:
close writers, then validate bounded native checkpoint references and flush/fsync
files plus the shared ready marker. GPU writes belong at
`/workspace/coppertop/runs/<run_id>/train`. This requires no S3 credential. It does
not claim a distributed filesystem commit guarantee; external S3 read-back is
required to establish handoff visibility.

`publish_output(r2, manifest, cpu_scratch, run_id=..., max_seconds=...)` is an
external CPU callback. It checks exact checkpoint filenames, pinned release,
run identity and per-artifact byte caps before fetching. It verifies the GPU's
ready marker through S3, checks HeadObject lengths, streams/checksums each file
into CPU scratch through `stage_objects`, then invokes the existing R2
`publish_smoke_checkpoint` with read-back and immutable receipt-last semantics.
Corrupt output cannot produce an R2 receipt. `restage_checkpoint` fetches the
published R2 receipt/files with the same run identity, requires exact manifest
reference equality and stages a separate `coppertop/stages/<manifest-key>`
directory for the fresh verifier. Each R2 payload is downloaded once; the upload
reuses that checksum-verified CPU scratch directory. It does not reuse the GPU's
train directory.
CPU retrieval, R2 publication and restaging each retain their original transfer
deadline rather than resetting the full duration between substeps.

The fresh GPU adapter writes `result.json` with the verified native comparison
plus the actual trusted `run_id`, `container_id` and `training_container_id`.
`seal_runpod_stage(report_manifest, reload_directory)` seals this bounded report
without discovering or guessing provider identities. After independently verified
verifier shutdown, external CPU `publish_reload` reads the exact reload-stage
marker and report through S3, checks size/SHA256 and distinct bound identities,
validates the native comparison against the checkpoint manifest, then writes and
read-back verifies the exact report object in R2. Its report key must equal the
sealed report's checksum. This is a separate durable phase after GPU stop.

These methods fit `cloud_roundtrip.PhaseCalls`: stage returns the shared commit
receipt; the separately authorized train adapter writes/seals output and returns
its manifest plus provider Pod identity; publish/restage run on external CPU;
reload uses a different Pod identity and verifies both mounted stages before
loading; publish_reload runs on CPU after verifier termination. Bind `stop_verify` to independently confirmed termination of exactly the
owned Pod. GPU completion is insufficient: `run_roundtrip` requires verified stop
before CPU publication and after verification. Allocation failures before a Pod
identity returns require provider-adapter cleanup. No concrete Pod allocation,
termination/status API binding is supplied while account access remains 403.

Terminate the GPU after durable network-volume output is confirmed; R2 publication
and restaging run with no GPU allocated. Fresh verification requires a new Pod,
verified volume/datacenter and the restaged R2 checkpoint. PID comparison cannot
prove freshness across containers. Runpod has no Modal commit/reload barrier;
filesystem/S3 visibility and fsync durability remain a bounded live integration
check rather than an established provider guarantee.

Authentication is unfinished by design. Runpod documents a **separate S3 API key**;
its S3 API does not support presigned URLs or documented temporary sessions. Never
put an existing parent Runpod/R2 key in GPU or CPU compute. The next live design
needs approval for a restricted transfer service that keeps the parent Runpod S3
credential outside compute, or an explicitly approved CPU-worker credential scope
and lifecycle. R2 can use separately approved short-lived scoped authentication.
This adapter receives an already authorized client and never discovers or configures
keys. Current Runpod account API access returned 403 and remains parked.

No Pod runner or storage resource has been provisioned. Local contract tests cover
checksums, marker ordering, partial failure, multipart bounds, endpoint/datacenter
binding, deadlines, output run/size checks, canonical R2 receipts, separate reload staging and durable fresh verifier reports; they do not establish provider integration. Provisioning must
name one dedicated Secure Cloud network volume, supported datacenter, size and
retention duration, external CPU transfer resource/bounds, authentication scope,
one GPU runtime/budget and teardown audit. Network volumes must be attached at Pod
creation and survive Pod termination, but continue accruing storage charges. Count
storage and transfer-worker/network costs separately from GPU costs and the user's
existing credit balance. Do not assume the documented CPU Pod product is available
in the selected volume datacenter or use GPU Pods merely for staging.

Official sources checked 2026-10-06:

- [S3 API](https://docs.runpod.io/storage/s3-api): endpoint table, datacenter region,
  network-volume ID as bucket, multipart and unsupported security operations.
- [Network volumes](https://docs.runpod.io/storage/network-volumes): Secure Cloud,
  mount/datacenter compatibility, attach-at-deploy, persistence and storage billing.
- [Pod lifecycle](https://docs.runpod.io/pods/manage-pods): stopping releases GPU;
  network-volume data survives stop/termination, and Pod/container data differs.
