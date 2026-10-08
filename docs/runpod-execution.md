# RunPod full official rollout

The subsequent [CPU setup approval bundle](runpod-setup-approval.md) proposes a
smaller preparation route through existing S3 access and a mounted runtime cache,
without a registry. It has a conditional USD12 envelope and explicit remaining
local bootstrap/controller work. The prebuilt-image path documented here remains
the committed implementation; neither proposal authorizes a paid action.

This branch implements local RunPod controls and an offline portable runtime
recipe for the frozen Xiaomi/RoboCasa recording. No RunPod runtime, portable
image, capacity, asset transfer, or live billing has been established by these
tests. Two separately approved CPU inventory attempts and existing-key read-only
storage checks have now run; the corrected inventory succeeded. Their limits and
outcomes are below. This document
does not authorize another paid action, credential creation, publishing or bulk
download. The existing Modal GPU approval is separate.

The sibling A100 rollout succeeded at 205 controls and 13 queries, with about
147 MiB and 1,939 immutable objects, but its shared CPU publisher timed out at
900 s; the final report was absent and no complete release was verified. GPU and
temporary Volume cleanup completed. The shared publisher's full-run scalability
is therefore a **known unresolved live blocker**. Shared `record_archive` from commit `37dd2a3` now provides archive-first
publication here. Real full-publication verification is still required. The
preceding diagnosis found a serial PUT + HEAD + HEAD + GET
path, about 7,760 requests: first PUT at 405 seconds, then 602 objects / 45.1 MiB
over 495 seconds, with a 0.808-second median PUT interval. Full serial publication
projects about 33 minutes. The initial delay lacks telemetry; the full trajectory
cannot be reconstructed from checked survivors after source Volume deletion.
The parent completed the shared archive repair and owns its real R2 validation.
This checkout imports that core unchanged and adds the RunPod adapters.
The latest parent checkpoint uploaded all ten parts of a 156,098,560-byte archive
in 66.81 seconds, but completion/readback stalled through the 600-second deadline.
The exact archive key was deleted and observed absent; ListParts returned
NoSuchUpload, while the abort ClientError lacked its error code. The local source
is preserved. Durable phase telemetry, reconciliation and repair remain owned by
the parent; this thread performs no duplicate archive upload.
Passing smoke/mocked publication cannot establish the full-run path. Do not
approve a full RunPod launch until the shared publication gate is resolved and
its actual limits are incorporated in a newly reviewed plan; 900 s is a failed
historical ceiling, not a demonstrated sufficient budget.
The paid CLI consequently fails before credential access while
`full_publication_live_verified=false`. Its lifecycle engine is locally tested
through injected transports. Enabling the CLI requires a reviewed follow-up that
binds the shared publisher's real validation and measured limits to a fresh plan;
no runtime recipe or mocked receipt flips that gate.

## Approved inventory attempts and storage access

[Recorded evidence](runpod-inventory-result.json) identifies the one paid App
`ap-hsgWOQjcCJiSyfcrP28Q1D` and call `fc-01M4DN3PJ805PMD66A7NJ7D562`.
The function used the existing seed, two physical cores, 16 GiB, a 900-second
execution limit, 300-second startup and 180-second cleanup; no GPU, Volume,
mounted secrets, bulk export, image build or model loading was requested.
The sandbox-only preliminary connection failed before creating any App; the
provider App list verified that absence before the approved network invocation.

The remote traceback reaches `push_outputs -> package_output -> blob_upload`:
Modal tried to upload the asynchronous return through its internal R2 blob path,
but the container's network block caused DNS failure. The worker reached output
packaging; its complete inventory receipt was **not recovered**. No package,
library, checkout or footprint result is established. An earlier input-bootstrap
description was corrected after reading the traceback function names.
The App is stopped, with independently verified zero containers and sandboxes.
The refreshed meter reports **$0.00738120**, provisional, versus a conservative
controller compute estimate of $0.01224205. Billing is not final and the separate
$0.20 reservation remains held; unrelated budget ledgers were not changed.

The correction uses the official synchronous `Function.remote` API and
returns JSON bytes bounded at 256 KiB, rather than the asynchronous path with a
smaller negotiated inline limit. It retains the network block, zero retries and
original resource limits. The user separately approved one revised invocation
with the same $0.20 cap. [Its verified result](runpod-inventory-success.json) binds
plan `483a1cc8604aa42523188b3cefc5065536f3be40b12322ee0457ba6292e940ae`
to App `ap-MZjFFGDVPWiPbhDko11GMh`. It returned a 49,340-byte
[CPU receipt](../containers/runpod-seed-inventory.json), SHA-256
`0f56b5acf693a99a219a57ed65df729ea4b104acbe18ec7fd9d8b35a03c4ab5f`.
Worker time was 73.988 seconds; controller time was 106.621 seconds including
10.745 seconds of cleanup. Independent verification found the App stopped with
zero active containers and sandboxes. The latest meter is **$0.00600720** at
2026-10-08T15:15:34Z, provisional; its full separate $0.20 reservation remains
held. This approved attempt is consumed; no further inventory retry is authorized.

Both interpreters are Python 3.11.13 on x86_64/glibc 2.35 and pass pip check and
the requested CPU imports. Native Torch is 2.8.0+cu128 with FlashAttention 2.8.3;
simulator Torch is 2.7.1+cpu with MuJoCo 3.3.1. All 129 simulator lock entries
match. Its three extra distributions are pip 24.0 and the pinned editable
RoboCasa 1.0.1 / RoboSuite 1.5.2 sources. All three source HEADs match and their
tracked files are clean. This is observed metadata, not a CUDA, model, render or
portable-image execution result. Current file-content checksums were not rehashed.

Existing RunPod control and S3 authentication passed read-only checks:
`GET /v1/networkvolumes` found zero volumes; S3 `ListBuckets` returned HTTP 200 and
zero buckets at `https://s3api-us-wa-1.runpod.io`, signing region `US-WA-1`.
This is a documented regional capability probe, not a configured-volume claim.
No object read, write, multipart recovery or Pod mount has been verified.
The controller now accepts the existing `RUNPOD_S3_ACCESS_KEY` alias as well as
`RUNPOD_S3_ACCESS_KEY_ID`, rejecting conflicting values. Raw keys were used only
in memory and were neither printed, saved, generated nor mounted on the worker.

RunPod S3 buckets represent network-volume IDs, and the endpoint and signing
region must match that volume's data center. S3 does not support CreateBucket,
DeleteBucket or GetBucketLocation. Network volumes survive Pod termination;
explicit REST volume deletion and owned incomplete-upload cleanup remain required.
These API/lifecycle statements come from the official storage docs; the empty
account check does not prove object-level behavior for a future volume.

## Frozen experiment and provider boundary

`CoffeeSetupMug`, development seed 1150, constructor seed 1000, episode 0 of the
official 50-trial schedule, policy seed 20261006. Use the existing pinned stock
deployment server/client, model revision and evaluator. Execute 16 actions per
query; stop at success, environment termination/truncation, or 600 controls.
At most 38 queries and 601 observations. Full query histories, action attribution,
native state bytes and all three 256×256, 20 Hz videos use the existing recorder
and readback validator. Success, termination, truncation and reaching the horizon
are separate fields. A terminated unsuccessful episode is not a success.

The GPU receives three public identity variables and RunPod's own Pod ID. There
are no ports, public IP, SSH keys, provider/storage credentials, retries, alternate
GPU or framework. Inference stays in `/opt/conda`; simulation stays in `/work/sim`.
Stock inference transport remains on loopback. The child processes receive only
the runtime environment allowlist. RunPod-specific provenance replaces the Modal
image identifier inside the new Chords release; the inherited acceptance plan's
Modal identifiers remain historical experiment references, not RunPod validation.

## Complete runtime inputs

`containers/runpod-runtime.json` defines a complete **export/transplant recipe**,
not an environment reconstructed from overlay requirements. Export from the
validated immutable seed `im-uMab88d0cwA6MALkcTf5VW` only after separate CPU
export/build/transfer approval; the bounded metadata inventory is now complete:

| Context tree | Container destination and purpose |
| --- | --- |
| `runtime/opt/conda` | Complete Python 3.11 native prefix, Torch 2.8/CUDA 12.8, FlashAttention 2.8.3 and active dependency closure |
| `runtime/usr/local` | Observed seed tree: two files, 3,405 bytes and one man-page link; retain it without treating it as the Python/CUDA closure |
| `runtime/work/sim` | Complete Python 3.11 simulator prefix, all 129 admission pins including CPU Torch 2.7.1 and MuJoCo 3.3.1 |
| `runtime/work/xiaomi` | Full clean checkout, including Git metadata and stock deployment client/server |
| `runtime/work/robocasa` | Full clean pinned checkout and extracted assets at their original paths |
| `runtime/work/robosuite` | Full clean pinned checkout, including Git metadata |
| `runtime/work/model` | Frozen snapshot, original license and metadata; materialize the top-level seed link if needed |
| `runtime/work/build-evidence` | Original extraction receipt, especially `assets.json` |
| `runtime/debs` | Exact offline Debian package closure for the original base's EGL/OpenGL/GLVND/Mesa, FFmpeg and GLib |

All original source revisions and model checksums remain in the repository pins.
The known model and extracted assets total **34,185,733,733 bytes (31.84 GiB)**.
The six acquired ZIPs have been deleted; neither inventory nor build requires
them. Their historical receipt verifies prior extraction; the export manifest
additionally hashes every current exported file. Full exported bytes and final
root filesystem each have a 70 GiB ceiling. Unknown native/system closure,
symlinks outside the exported trees, excess footprint or broken ABI fail admission;
no broad repair or new acquisition follows automatically.

The CPU receipt measures **47,492,000,203 logical bytes (44.23 GiB)** across the
eight distinct requested trees. `/opt/model` aliases `/work/model` and is counted
once. This is not a complete root filesystem, tar, Debian closure or OCI layer
size. The environments contain 132 distributions each. The simulator interpreter
link resolves to `/opt/conda/bin/python3.11`; its stdlib also comes from that prefix.
Native libstdc++ resolves inside `/opt/conda`, while the simulator loads the system
libstdc++; preserve both closures. The 352 observed system-package versions are
in the receipt, but offline `.deb` availability, checksums, dependencies and bytes
remain unverified.

**Required build addition:** `libOpenGL.so.0` was unavailable in both interpreters,
and `libopengl0` was not installed. EGL, GL, GLES2, GLdispatch and GLib loaded.
The portable contract retains the OpenGL requirement. Select and verify a pinned
offline `libopengl0` package and its dependency closure under the separate setup
approval, then pass real CPU admission. The seed inventory cannot pass that gate
unchanged. Neither missing-package repair nor a complete symlink audit was run.

Use the existing immutable PyTorch base digest. The official RunPod
`runpod/pytorch:1.0.2-cu1281-torch280-ubuntu2404` remains a candidate; its Python,
wheel and glibc compatibility is unverified, so the executable recipe does not
silently switch to it.

On the approved Linux CPU builder, place the specified existing trees in a fresh
ignored context, retain symlinks inside them, and materialize top-level tree links.
Copy only repository code plus those inputs. Never include a credential directory,
environment file or parent checkout as an export tree. Pre-stage the pinned base
and BuildKit frontend under the separate setup approval. Then:

```sh
python -m coppertop.runpod_runtime --inventory runtime --output runtime-manifest.json
DOCKER_BUILDKIT=1 docker build --network none --platform linux/amd64 \
  -f containers/Dockerfile.runpod -t coppertop-runpod:prepared .
```

The Dockerfile-specific ignore file limits context membership. BuildKit binds the
large input directory rather than keeping COPY-and-move duplicates in image layers.
The manifest check authenticates exact files, sizes, hashes, modes and link targets.
All `.debs` install offline. Full original environments are transplanted at the same
paths. CPU admission checks native active closure, pins, pip check, imports and CUDA
ABI; full clean Git checkouts; model checksums; simulator pins/imports/assets; loaded
system libraries and FFmpeg. It **does not execute CUDA, instantiate the model or
reset/render the simulator**. Extract `/opt/runpod-cpu-admission.json` and record the
registry OCI digest and canonical export-manifest hash. Registry upload, export
traffic, build scratch, image layer size and registry retention require their own
priced approval; the GPU controller does not build or transfer this runtime.

## Controller and evidence lifecycle

Offline preparation:

```sh
python -m coppertop.runpod_cloud --config .local/runpod-config.json \
  --output .local/runpod-prepared.json
```

`containers/runpod-config.example.json` contains obsolete illustrative rates and
placeholder identities. It is not a launch approval. Replace it with a fresh unique
run ID, built OCI digest, export/CPU receipt hashes, a capacity quote and complete
cost reservation. The preparation hash binds all executing source and recipe files.

After exact new approval, an explicitly approved external CPU controller receives
existing RunPod control/S3 access and a pre-authorized R2 one-hour session scoped to
the exact new prefix. The controller never mints a session or sends keys to the Pod.
Supply only the named environment variables consumed by `make_transports`; it never
loads a `.env` file. That client location, CPU rental and egress must be included in
the transfer reservation. Actual token permissions/TTL must be established when
the session is approved; checking a destination string cannot prove permission scope.
`R2_TEMP_EXPIRES_UTC` supplies public session expiration metadata; execution rejects
sessions with less than 3,540 seconds or more than 3,600 seconds remaining.

```sh
python -m coppertop.runpod_cloud --config .local/runpod-config.json \
  --execute --cpu-receipt .local/runpod-cpu-admission.json \
  --approved-plan-sha256 EXACT_REVIEWED_HASH --output .local/runpod-attempt
```

The controller consumes a durable single-use journal before any allocation. It
requires a quote timestamp within 15 minutes and rechecks the exact GPU/datacenter,
CUDA 12.8 availability and price through the official catalog before allocation.
Availability is an admission check, not a capacity guarantee. It creates one owned
3 GB network volume and one noninterruptible Secure Cloud GPU Pod; names already
present abort. All REST and S3 calls have finite timeouts, redirects/retries are
disabled, and no provider response or credential is written to logs.

The Pod reruns CPU admission and executes the existing official worker with a
560 s timeout. It seals every original output file into one uncompressed USTAR
archive on local producer disk, then calls shared `stage_archive` with a POSIX
filesystem commit barrier. The compact record and ready marker acknowledge the
archive only after that barrier. Child artifacts/logs remain capped at 64 MiB;
the packaging process permits the bounded archive. Filesystem fsync alone does
not prove RunPod S3 visibility; the external CPU process streams and hashes the
staged archive after verified Pod termination, then safely unpacks and validates
the original CAS inventory and rollout result before any R2 mutation.

Shared `publish_archive` uses 16 MiB parts, at most four in flight, full streamed
R2 hash readback and a verified completion receipt. Its archive role uses the
existing shared `chords/mechanism/<run_id>` namespace and concurrent request/byte
guards. The multipart journal is fsynced locally and PUT/GET-verified on the owned
RunPod volume before each callback returns. Resume is explicit and can restore
that volume journal after a CPU process is lost. Source-volume journal requests
are additional to the shared synthetic R2 counts, and real latency is unmeasured.

External CPU scratch retains the archive, `source-record.json`, `source-receipt.json`,
safely restored original files under `outputs`, and `fetch-complete.json`. That
marker follows complete archive and CAS verification and says
`full_publication_verified=false`. R2 timeout cannot erase this local handoff.
The final publication marker separately identifies the verified completion.
Existing `LocalStore` readers work on unpacked `outputs/store`; individual CAS
files are not separately uploaded to R2.

After publication failure, compute is stopped and the exact owned stage is
retained only under the explicit ongoing-storage policy in the approved plan.
`retention.json` reports resource ID, bounded prefix size or measurement error,
3 GB allocated capacity, approved monthly rate, cleanup owner and required recovery.
The size check is one page of at most 32 entries for the archive stage, not a
recursive dataset scan. Unknown/over-limit size is reported for follow-up, never
represented as verified bounded storage. RunPod bills allocated capacity: at the
illustrative $0.07/GB-month, this volume is $0.21/month regardless of used bytes.
There is no scheduler, expiry service or claim of a fixed lifetime-spend cap.

A verified archive completion permits owned-volume deletion; a metadata failure
report does not. Explicit deletion of unpublished source requires separate operator
authorization. Any journaled R2 multipart upload must first be aborted and verified
absent using fresh exact-scoped authority. If multipart creation succeeded but its
response was lost before saving the upload ID, the current shared API cannot
reconcile it: RunPod reports unresolved state and blocks a fresh upload/source
deletion pending separately authorized exact-key orphan reconciliation. Shared
core was not independently modified to add enumeration privileges.

Owned-volume deletion audits GPU absence, aborts up to 100 incomplete source-volume
uploads, deletes only that volume, and verifies its absence. Default recovery
stops/audits compute and retains unpublished data; it never allocates resources:

```sh
python -m coppertop.runpod_cloud --recover .local/runpod-attempt/state.json \
  --approved-plan-sha256 ORIGINAL_PLAN_HASH --output .local/unused
```

A separately authorized `--resume-publication` or `--delete-unpublished` selects
one explicit operation. Cleanup-only recovery requires no R2 publication credentials.
Neither command is authorized by this document.

Lost allocation responses are reconciled by exact unique owned name/specification
only after that name was found absent. Multiple matches fail closed. Startup or
ready-marker failures attempt one sealed-record recovery after verified termination,
before deleting the volume. Corrupt/incomplete evidence is rejected; a sanitized
metadata failure report is published instead. A publication attempt is never
automatically repeated. Recovery does not upgrade a failed controller run to success.
If a controller is killed outright, its journal is the cleanup handoff. There is
no provider-side shutdown guarantee; launch requires a reachable independent
cleanup operator. No reservation is released by the implementation based on tests
or a stopped resource alone.

## Exact resource and cost reservations

| Item | Ceiling |
| --- | --- |
| New GPU Pods / network volumes | 1 / 1; volume 3,000,000,000 bytes |
| Container disk | 100 GB; admitted root filesystem ≤70 GiB |
| Local recording / CPU fetch scratch | ≤2 GiB + 96 MiB each, including archive, original files and bounded logs |
| Worker/server logs | two slots, each ≤64 MiB; inherited POSIX file-size cap |
| Raw recording / archive / staging | ≤1 GiB / ≤1 GiB + 16 MiB / ≤2 GiB; ≤6,000 CAS files plus 20 sidecars |
| R2 publication | one archive plus ≤64 KiB completion and bounded failure metadata; 16 MiB parts, concurrency ≤4 |
| Setup / ready-run / CPU publish | 900 / 1,200 / 900 seconds |
| Main controller / overall handoff / cleanup / recovery metadata | 3,000 / 3,300 / 180 / 60 seconds |
| Official worker / server session | 560 / 550 seconds, plus owned shutdown |
| Credential session | one pre-approved scoped R2 session, ≤3,600 seconds |

Recovery fetch takes place after GPU absence; even setup 900 + run 1,200 + stop 60
+ cleanup 180 seconds is below one GPU hour. The conditional compute reservation
is **one entire current quoted GPU hour**, not just inference seconds. Deletion
failure or controller death can continue billing beyond that envelope; no local
timer is a provider-enforced dollar cap.

Sealed-record recovery has at most 900 s and shares the 3,300 s overall handoff
deadline. Cleanup plus final metadata adds at most 240 s, for a 3,540 s total
controller envelope. The approved R2 session must have that remaining lifetime
at controller start; an already old session cannot satisfy it.

`cost_bound` deliberately reserves a **full first month** of 100 GB container disk
and 3 GB network volume, plus **two billable R2 GB** (R2 rounds 1 GiB upward), rounded
request units and an explicit CPU-transfer/egress reservation. It assumes no free
tier. With the example's historical Ada $0.84/h, disk $0.10/GB-month, volume
$0.07/GB-month, R2 $0.015/GB-month, $4.86 requests and $1 CPU/transfer reservation:

`0.84 + 100×0.10 + 3×0.07 + 2×0.015 + 4.86 + 1 = $16.94`.

The historical A100 $1.59/h substitutes to $17.69. These are arithmetic examples,
not current capacity or launch proposals. Any new setup/bulk-transfer/registry
spend is excluded and needs its own exact reservation before execution. Prices
and transfer reservation must be rechecked; $25 in the example is unapproved.
After owned-volume deletion, the bounded R2 archive/receipt remains: conservatively
$0.03/month at those rates until separately authorized retention action. Interrupted
R2 multipart uploads remain journaled for explicit resume/abort. Retained RunPod
staging adds allocated-volume charges ($0.21/month at the illustrative rate),
accepted explicitly outside the initial one-month reservation.

## Remaining live gates and smallest blocked step

The shared A100 publisher diagnosis owns the current scaling investigation. Await
its repair and full validation; this thread performs no duplicate investigation.
Resolve full-run publication, including byte readback and surviving-evidence recovery,
before approving the rollout in step 4. Do not simply increase timeout or spend.

1. **CPU metadata inventory complete:** the single revised invocation used two
   physical CPU cores, 16 GiB RAM, 900 s execution, 300 s startup and 180 s exact-App
   cleanup; ≤256 KiB output, zero retries, no GPU, Volume, mounted secrets, install,
   downloads or bulk transfer. Its conservative preflight compute envelope was
   `1380×(2×0.0000131 + 16×0.00000222)×1.75 = $0.1490538`, plus at most
   $0.000009765625 metadata egress, within the separate approved $0.20 cap. Both
   historical inventory reservations remain held pending final bills. No additional
   remote inventory is necessary for the current local deliverable or authorized.
2. **Smallest blocked phase:** price and approve the exact export/build/registry/CPU-transfer phase from that
   inventory; source checkouts, licenses, strong manifest and current system closure
   must accompany the existing bytes. The measured tree subtotal is 47,492,000,203
   bytes; offline Debian closure and tar/manifest overhead must fit the 75,161,927,680-byte
   export ceiling. Builder scratch, compressed OCI bytes, registry retention and
   egress are still unmeasured and need named locations, quoted rates and explicit
   financial caps before export or build. No RunPod setup cap has been approved.
   No new large models or asset ZIP acquisition. Do not treat the ≤70 GiB ceiling
   as a measured image size or a priced bulk-transfer approval.
3. Build on Linux x86_64; retain passing real CPU receipt and measured OCI layer bytes.
   Validate the actual RunPod driver/EGL and a model query in the first approved Pod.
   The initial path intentionally attempts the full bounded rollout; an OOM, ABI,
   render, setup or 560 s worker deadline failure is evidence, never an automatic retry.
4. Refresh capacity and every rate, approve the exact plan hash and new reservation,
   exact owned resources, fresh R2 prefix/session and retention. Establish independent
   cleanup reachability first. Then run one rollout, verify all recorder boundaries,
   terminate the GPU, verify full publication/readback, delete volume and reconcile
   live bills. No financial approval has been inherited from the Modal experiment.

Official sources used for implementation and price arithmetic (checked 2026-10-08):
[Pod REST](https://docs.runpod.io/api-reference/pods/POST/pods),
[Pod deletion](https://docs.runpod.io/api-reference/pods/DELETE/pods/podId),
[network volumes](https://docs.runpod.io/storage/network-volumes),
[S3 compatibility](https://docs.runpod.io/storage/s3-api),
[Modal rates](https://modal.com/pricing),
[regional multiplier](https://modal.com/docs/guide/region-selection),
[R2 billing](https://developers.cloudflare.com/r2/pricing/).
