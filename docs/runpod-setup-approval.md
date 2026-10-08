# RunPod CPU preparation approval bundle

Prepared locally on 2026-10-08 from the [verified seed inventory](runpod-inventory-success.json).
**Proposed additional cap: USD 12, CPU preparation only. Nothing is authorized by
this document, and no export, package download, image build, allocation or transfer
has run.** The [machine-readable proposal](runpod-setup-approval.json) records every
ceiling and unresolved gate. Existing inventory reservations remain separate.

The smallest route uses the existing Modal source and existing RunPod S3 access:
export the actual runtime as a bounded split tar, relay it through the existing
local controller, and restore it onto one RunPod CPU Pod using the already pinned
public PyTorch base. No registry, account, SSH service, credential creation or
custom OCI build is needed. The runtime identity becomes **public base digest +
export manifest + package lock + bootstrap/code hashes + CPU receipt**, rather
than a claim that the public base contains this experiment.

The split exporter, local relay, offline CPU bootstrap and exact owned-cache
reuse are implemented and tested locally. See the [control documentation](runpod-preparation.md)
for executable gates, receipts and recovery. Complete offline artifacts, a real
CPU quote and public-base size metadata are still missing; no live restoration
or throughput is claimed. The prebuilt full-OCI path also remains available.

## Why this route

RunPod supports Docker CPU Pods and network volumes, but its current CPU runtime
does not support Docker-in-Docker. A CPU Pod can restore files and install the
locked offline packages directly; it should not be booked as a nested Docker
builder. The same restoration can later run on a separately approved GPU Pod.
[Official CPU runtime announcement](https://www.runpod.io/blog/enhanced-cpu-pods-docker-network).

RunPod's REST create API exposes the image, start command, CPU configuration,
network volume and container disk. Use that ordinary Pod path, with a hash-checked
bootstrap on the mounted volume and no published ports.
[Pod API](https://docs.runpod.io/api-reference/pods/POST/pods).

The OCI-build alternative remains the existing Dockerfile, but needs a Linux
builder that supports builds and an existing registry destination. Neither is
verified here, and an OCI push would add another large copy and access/retention
questions. It is not required for this proposed first preparation attempt.

## Provider and resource choices

| Location | Proposed resources | Limit and role |
| --- | --- | --- |
| Existing Modal account | One Sandbox on `im-uMab88d0cwA6MALkcTf5VW`; 2 physical cores, 16 GiB, hard limits, US placement; no GPU, new Volume or mounted secrets | 6,720 s work + 300 s startup + 180 s stop/audit = 7,200 s; export only; zero retries; network block retained |
| Existing Mac/controller | Existing installed Modal/boto3 SDKs and isolated ignored directory | ≤512 MiB process memory, ≤54 GiB scratch; require ≥64 GiB free before starting; current read-only disk check found 176,537,600,000 bytes free |
| Existing RunPod account, US-WA-1 | One new Standard 60 GB network volume, exact owned ID, S3 endpoint/region US-WA-1 | One cache; no global volume or additional storage service; no allocation until quote/admission gates pass |
| RunPod CPU preparation Pod | Secure Cloud, Linux amd64, prefer 4 vCPU with ≥16 GiB; allow at most 8 vCPU/32 GiB if needed to meet RAM, at ≤USD 0.50/h total | One Pod, 100 GB container disk, 6,300 s work + 600 s startup + 300 s deletion/audit = 7,200 s; no GPU, ports, public IP or SSH; no provider/storage keys inside |

No fallback datacenter, GPU substitution or larger instance is implicit. CPU
type, RAM ratio, actual price and US-WA-1 availability must be captured before
allocation. The official catalog provides those fields;
[CPU catalog API](https://docs.runpod.io/api-reference-v2/catalog/list-cpu-types).

**Actual quote blocker:** the bounded existing-key catalog GET returned HTTP 403;
the browser console displayed its sign-in page. No permission change or login
was performed. A 403 alone does not establish its cause. Obtain the exact quote
through the existing account's normal console or a working read-only catalog
route. The USD 0.50/h figure is an admission ceiling, not an observed quote.
[Read-only check receipt](runpod-setup-cpu-quotes.json).

## Exact contents and transfer ceilings

The export preserves these full trees, including Git metadata, licenses, model
metadata and all recorded extracted assets. `/opt/model` is a top-level alias for
`/work/model`, not another 10 GB payload.

| Distinct source tree | Observed logical bytes |
| --- | ---: |
| `/opt/conda` | 9,560,449,338 |
| `/usr/local` | 3,405 |
| `/work/sim` | 2,727,489,935 |
| `/work/xiaomi` | 3,253,041 |
| `/work/robocasa` | 24,144,979,496 |
| `/work/robosuite` | 910,679,856 |
| `/work/model` | 10,122,587,772 |
| `/work/build-evidence` | 22,557,360 |
| **Measured subtotal** | **47,492,000,203 (44.23 GiB)** |

This covers 263,951 regular files, 2,673 links and 26,836 directories. Hash all
exported files and preserve modes/link targets. Materialize only the top-level
model alias; keep the simulator interpreter link into `/opt/conda`. No deleted
asset ZIPs, new model snapshot, pip reinstall or environment reconstruction.

Additional maximums are 2 GiB of exact offline system `.debs`, 64 MiB strong
manifest, 16 MiB code/bootstrap and 1 GiB tar headers/padding. Their combined
upper bound with the observed trees is **50,797,111,755 bytes**, below the stricter
**50 GiB / 53,687,091,200-byte tar cap**. Exceeding any component stops preparation.
This is a ceiling, not a claim that those unacquired packages fit.

Split that single tar stream into at most thirteen ≤4 GiB files. Modal's public
filesystem API reads files up to 5 GB, so a single 50 GiB `copy_to_local` call is
invalid. Copy shards individually with the installed public API and verify each
local hash. No whole-file in-memory read and no asynchronous bulk return.
[Modal filesystem API](https://modal.com/docs/guide/sandbox-files).

The local controller uploads those same shards with existing RunPod S3 keys,
16 MiB multipart parts from the existing shared uploader, at most four concurrent
parts and one current upload. At most 3,200 archive data parts, thirteen data objects,
≤32 prefix entries and ≤4,096 S3
calls are admitted. Each shard is below the 10 GB size at which RunPod documents
completion timeouts; completion still has a 300 s timeout and is not assumed safe.
One ≤8 MiB preflight object exercises upload/full readback/deletion/absence first, inside
the same byte/time/reservation limits. Actual multipart completion and abort remain
live gates. Disable boto3 request retries; do not replay
an export, shard copy or upload automatically. Modal's internal control-plane
transport recovery is not a new workload approval and must remain inside the
deadline and byte limits. Keep exact upload IDs and phase/byte telemetry durably local.

RunPod maps S3 objects to files on its volume. Use a streamed full GET checksum,
not ETag, before marking staging complete. Presigned URLs are explicitly
unsupported, so no presigned credentials are derived or transmitted. Raw keys
remain in the existing local process. The maximum payload in each direction is
50 GiB +96 MiB; budget 55 GiB of Modal egress including control/protocol slack. Readback
from RunPod adds up to that same payload to the Mac, without another disk copy.
[S3 compatibility and limits](https://docs.runpod.io/storage/s3-api).

The 60 GB volume must also fit temporary multipart assembly: ≤54 GiB plus ≤96 MiB
sidecars is 58,082,721,792 bytes, below 60,000,000,000 bytes. Later recording staging
uses ≤2 GiB after source uploads are complete; incomplete source uploads block
that reuse. Capacity accounting includes hidden upload parts, not only visible
object bytes.

The CPU Pod sees the shards through `/workspace`. Extraction is only on its
100 GB container disk: the 60 GB volume never contains an extracted runtime
copy. The bootstrap rejects a target on that volume and bounds old base +
extracted trees + installed packages at 70 GiB before writing. Mounted shard
prehash and streamed restoration can read up to 100 GiB from that volume;
these are internal Pod/volume reads, without a second archive copy.

 Verify each shard and aggregate
hash, safely concatenate/extract as a stream and restore the required absolute
paths. Do not form a second50 GiB archive. Install only the preverified offline
package closure, restore code into `/app`, then run the existing actual CPU
admission. All visible root files must fit the existing 70 GiB ceiling; container
writes must fit 100 GB. Public base layers must be metadata-measured at ≤20 GiB
compressed before allocation; no layers were downloaded for this proposal.

## OpenGL and system closure

The seed is Python 3.11.13 / glibc 2.35 on Ubuntu Jammy, with native Torch 2.8 / CUDA 12.8 / 
FlashAttention 2.8.3 and simulator CPU Torch 2.7.1 / MuJoCo 3.3.1. Keep the existing
PyTorch digest; the RunPod Ubuntu 24.04 image remains an unverified candidate.

The missing addition is **`libopengl0=1.4.0-1`, amd64, Jammy**. Ubuntu publishes
that version and its dependency on `libglvnd0=1.4.0-1`, matching the observed seed;
the displayed package size is 35.6 kB. Exact URL, bytes, SHA256 and installation
are still unverified. Do not remove the admission requirement.
[Ubuntu package](https://packages.ubuntu.com/jammy/amd64/libopengl0).

Lock the eight required system packages and their dependency closure against the
observed 352-package inventory and the public base. Preserve the seed's GLVND,
Mesa, FFmpeg and GLib versions where they differ from the base; reject incompatible
or unavailable pins. Use signed official Ubuntu metadata and its snapshot archive
for older versions when available, with the 2 GiB package cap. Availability and a
matching snapshot must be proved; do not silently substitute current versions or
export an unbounded `/usr` tree. Package acquisition occurs after approval on the
local controller; the source Sandbox remains network-blocked. The Mac first
checks all declared hashes and sizes. Its missing `gpgv`/`dpkg-deb` tools run on
the same source CPU Sandbox as the first check before runtime export, then
again on the restoring CPU Pod. No extra Linux builder or Mac installation.
[Official Ubuntu snapshots](https://snapshot.ubuntu.com).

## Spending, time and retention

The two paid compute resources are sequential and each has a 2 h inclusive limit.
Local S3 staging/readback has a separate 2 h limit with no rented CPU. Overall
preparation is bounded at 6 h and never extended automatically. Deadline failures
produce an incomplete receipt and cleanup handoff, not another attempt.

| Reservation | USD bound |
| --- | ---: |
| Modal Sandbox CPU/RAM, 7,200 s, current published Sandbox rates and 1.75 region multiplier | 2.338056 |
| Modal 55 GiB egress atUSD 0.04/GiB, assuming no free allowance | 2.20 |
| RunPod CPU, 2 h at the unquotedUSD 0.50/h admission ceiling | 1.00 |
| RunPod 100 GB container disk for 2 h, rounded up using a 28-day month | 0.03 |
| Full first month of allocated 60 GB Standard network volume atUSD 0.07/GB-month | 4.20 |
| **Subtotal** | **9.768056** |
| Tax/rate drift/cleanup contingency within proposed cap | 2.231944 |
| **Proposed additional CPU preparation cap** | **12.00** |

Modal calculation: `7200×(2×0.00003942 +16×0.00000667)×1.75`. Those are Sandbox
rates, not the cheaper Function rates used for the inventory. Keep existing
reservations separate and hold the new one until reconciled bills.
[Modal pricing](https://modal.com/pricing),
[region multipliers](https://modal.com/docs/guide/region-selection),
[egress billing](https://modal.com/docs/guide/network-egress-billing).

RunPod documents per-second Pod/container billing, hourly network-volume billing,
and no ingress/egress charge; its S3 API adds no separate pricing. Confirm the
actual all-in quote and invoice basis before allocation. No allowance, registry
storage, GPU charge or R2 spend is part of this proposal. Stop before allocating
if the confirmed envelope including taxes exceedsUSD 12.
[RunPod pricing](https://docs.runpod.io/pods/pricing).

Keep the existing immutable Modal image unchanged. Preserve local export shards
until full staged readback and the actual CPU receipt are locally verified; then
remove only those owned scratch files. Failures preserve source copies and
journals until reconciliation. Retain the one verified RunPod cache for at most
14 days pending the separate GPU decision. The proposed approval must authorize
exact-volume deletion on cancellation or that deadline after preserving local
receipts and verifying a surviving source. No cleanup scheduler is being claimed.

The volume survives CPU Pod termination. Stop and verify compute absent at every
exit; abort only journaled/prefix-owned multipart uploads, verify absent, and
explicitly delete the owned volume when its retention ends. An independent
reachable cleanup operator must be named before launch. If cleanup fails, report
the resource IDs and continuing **USD 4.20/month allocated-volume charge**; the
first-month reservation does not make retention free or prove later deletion.
No timer here is a provider-enforced dollar cap.
[Network volume lifecycle](https://docs.runpod.io/storage/network-volumes).

## Receipts and later GPU boundary

1. **Admission before spending:** current CPU flavor/RAM/rate/US-WA-1 availability;
   public-base digest/platform/layer-byte metadata; official signed `.deb` lock;
   passing tiny local exporter/relay/bootstrap controls; exact plan/code hash,
   cleanup owner and accepted retention.
2. **Export:** immutable seed identity, all file hashes/modes/links, clean source
   revisions, frozen model checksums/licenses, original extraction receipt, exact
   shard sizes/SHA256 and aggregate tar digest. Reject changed or oversized bytes.
3. **Transfer:** complete local shard verification, source Sandbox/App stopped
   with zero active resources; RunPod multipart journal and full GET checksums;
   preflight object absence and mounted/S3 path agreement. No ready marker on
   incomplete transfer or completion ambiguity.
4. **CPU preparation:** actual restored native/simulator interpreters, active
   closure and pip check; all 129 simulator pins; clean full source checkouts;
   frozen model file checksums; loaded OpenGL/EGL/GLVND/GLib and FFmpeg. Bind the
   CPU receipt to base, manifest, package/bootstrap/code hashes and owned volume;
   save it locally before CPU Pod deletion and verify that Pod absent.
5. **Closeout:** exact resource/billing/retention report; recover or delete only
   owned staging/incomplete uploads; hold financial reservations until billing
   finality. A lost create/completion response gets one bounded reconciliation,
   then an explicit operator handoff, never a blind new upload.

CPU admission does not run CUDA, instantiate the model, reset/render the simulator
or establish a working rollout. A later fresh GPU approval must recheck capacity,
driver / CUDA 12.8, EGL, model query and the full frozen DEV1150 execution-16 rollout
(≤600 controls / 38 queries / 601 observations), recorder boundaries and publication.
The same 60 GB cache can hold ≤50 GiB input +≤2 GiB recording stage, but the current
cache path now binds the existing 60 GB volume, reserves up to 6,300 s setup
and three hours of GPU cost, and stops compute before an explicit fresh-session
publication handoff. Those limits need separate GPU approval. The main task
confirmed shared R2 upload/full readback/receipt/cleanup passed; no R2 test was
repeated here and no RunPod runtime is established by that result.

The only fact preventing a firm current compute quote is the inaccessible CPU
catalog/authenticated console. Package closure, base-layer bytes and throughput
are bounded admission/verification work, not reasons to book unspecified
infrastructure. If the exact CPU quote meets the fixed limits, this bundle needs
one CPU-preparation approval after the missing artifacts are concrete. Recent
real 8 MiB throughput probes must predict completion at twice measured time
plus 60 s overhead before bulk work; stale/slow samples stop. The Mac must stay
awake and online for all six hours with a reachable independent cleanup operator.
Probe results never prove sustained 50 GiB throughput.
