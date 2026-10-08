# RunPod full official rollout

This branch implements local RunPod controls and an offline portable runtime
recipe for the frozen Xiaomi/RoboCasa recording. No RunPod runtime, portable
image, capacity, asset transfer, or live billing has been established by these
tests. No paid action, credential creation, publishing, or bulk download is
authorized by this document. The existing Modal approval is separate.

The sibling A100 rollout succeeded at 205 controls and 13 queries, with about
147 MiB and 1,939 immutable objects, but its shared CPU publisher timed out at
900 s; the final report was absent and no complete release was verified. GPU and
temporary Volume cleanup completed. The shared publisher's full-run scalability
is therefore a **known unresolved live blocker**. This implementation reuses
`mechanism_cloud.publish_directory` unchanged and awaits that thread's diagnosis.
Passing smoke/mocked publication cannot establish the full-run path. Do not
approve a full RunPod launch until the shared publication gate is resolved and
its actual limits are incorporated in a newly reviewed plan; 900 s is a failed
historical ceiling, not a demonstrated sufficient budget.
The paid CLI consequently fails before credential access while
`full_publication_live_verified=false`. Its lifecycle engine is locally tested
through injected transports. Enabling the CLI requires a reviewed follow-up that
binds the shared publisher's real validation and measured limits to a fresh plan;
no runtime recipe or mocked receipt flips that gate.

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
inventory/export approval:

| Context tree | Container destination and purpose |
| --- | --- |
| `runtime/opt/conda` | Complete Python 3.11 native prefix, Torch 2.8/CUDA 12.8, FlashAttention 2.8.3 and active dependency closure |
| `runtime/usr/local` | Complete inherited interpreter/stdlib and CUDA support outside the prefixes; preserves venv link targets |
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
no broad repair or new acquisition follows automatically. Original system `.deb`
versions/checksums and full native inventory remain unverified until that export.

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

The Pod reruns CPU admission, executes the existing official worker with a 560 s
timeout, stops its owned server, records the compact outcome, and copies checksum
objects from local scratch to the volume. A compact inventory and record are fsynced
before a ≤1 KiB ready marker. Filesystem fsync alone does not establish S3 visibility:
the external CPU process hashes the record, inventory and every object before R2
publication, then reads every R2 byte back. It must verify the Pod absent first.

The external CPU scratch also retains `source-record.json`, `source-receipt.json`
and a `fetch-complete.json` marker written only after all object checksums pass.
Thus a later R2 timeout leaves a complete local handoff independent of GPU/volume
cleanup. The marker says `full_publication_verified=false`; it does not claim R2
publication. Keep this CPU scratch until the shared publisher diagnosis determines
the separately approved recovery path. Partial fetches have no completion marker.

The controller deletes the Pod rather than stopping it. It explicitly enumerates
and aborts up to 100 incomplete multipart uploads in the owned volume, attempts
volume deletion even if abort fails, and verifies the exact volume absent. A network
volume survives Pod termination and continues billing. If Pod absence is unknown,
the volume is preserved, cleanup is failed and the reservation remains held. Cleanup
can be retried without allocation:

```sh
python -m coppertop.runpod_cloud --recover .local/runpod-attempt/state.json \
  --approved-plan-sha256 ORIGINAL_PLAN_HASH --output .local/unused
```

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
| Local recording / CPU fetch scratch | ≤2 GiB each; encoded single artifact ≤64 MiB |
| Worker/server logs | two slots, each ≤64 MiB; inherited POSIX file-size cap |
| R2 evidence, including inventory and two report slots | ≤1 GiB and ≤6,000 immutable objects |
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
After owned-volume deletion, only ≤1 GiB R2 evidence remains: conservatively
$0.03/month at those rates, indefinitely until separate retention action. Failed
R2 single PUTs have no multipart uploads; committed partial objects remain within
the same fresh prefix and bound, referenced by the local attempt journal.

## Remaining live gates and smallest blocked step

The shared A100 publisher diagnosis owns the current scaling investigation. Await
its concrete result; this thread makes no remote reads or duplicate investigation.
Resolve full-run publication, including byte readback and surviving-evidence recovery,
before approving the rollout in step 4. Do not simply increase timeout or spend.

1. **CPU inventory only, before transfer:** one single-use function on the existing
   immutable Modal seed, at most two physical CPU cores, 16 GiB RAM, 900 s execution,
   300 s startup and 180 s exact-App stop/audit; no GPU, Volume, secrets, installation,
   downloads or bulk output. Return ≤256 KiB of interpreter/link targets, complete
   installed native/simulator versions, system package versions and per-tree byte
   totals. Stop if the export closure or ≤70 GiB bound is unavailable. Preserve all
   unrelated Apps and existing budget holds. At the checked standard function rates
   and maximum 1.75 region multiplier, `1380×(2×0.0000131 + 16×0.00000222)×1.75 =
   $0.1490478`; propose a **separate $0.20 reservation**, subject to an exact reviewed
   function/cleanup plan and refreshed ledger. This has not been executed or approved.
2. Price and approve the exact export/build/registry/CPU-transfer phase from that
   inventory; source checkouts, licenses, strong manifest and current system closure
   must accompany the existing bytes. No new large models or asset ZIP acquisition.
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
