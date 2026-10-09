# Minimal RunPod CPU preparation controls

The committed local path is `runpod_prepare` → `ModalSource` → `runpod_bundle`
→ `Relay` → `runpod_bootstrap`. It uses the existing cached source image,
installed Modal 1.6.1/boto3 SDKs, existing Mac, existing RunPod API/S3 identity,
one new 60 GB Standard network volume and one CPU Pod on the pinned public base.
It creates no registry, account, key, listener, SSH service or GPU resource.
The [approval bundle](runpod-setup-approval.md) remains **unapproved**.

Local tests exercise real tiny tar/file restoration, interrupted multipart
transport through the shared uploader, explicit resume, lost create/completion
responses, ownership, size/time/request budgets, source preservation and cleanup.
Cryptographic/package-manager and native-library subprocesses use injected
fixtures in these tests. These checks establish local controls, not a restored
cloud runtime, real throughput, CUDA, EGL rendering or rollout success.

## Inputs and gates

Generate only a public plan locally:

```sh
PYTHONDONTWRITEBYTECODE=1 .venv/bin/python -m coppertop.runpod_prepare --config containers/runpod-preparation.example.json --output .local/runpod-preparation-plan.json
```

The example's three nulls intentionally block execution **before credential
access**. Fill them only from actual evidence:

- `CPU_quote`: exact catalog `type_id`, explicitly verified REST `flavor_id`,
  4 or 8 `vcpus`, `ram_gib` (convert provider decimal
  GB), total `hour_usd` ≤0.50, `datacenter` US-WA-1, availability and UTC timestamp.
  Preparation approval requires freshness within 15 minutes. The controller
  refreshes that same flavor/count before source work, volume creation and CPU
  creation. A denied catalog call stops; there is no alternate access route.
  Catalog IDs can include a size suffix; Pod creation uses the documented
  `cpu3c/cpu3g/cpu3m/cpu5c/cpu5g/cpu5m` flavor enum. Admission also checks the
  allocated Pod's actual flavor, vCPU count, RAM and hourly price.
- `base_metadata`: exact pinned `image`, `platform` linux/amd64 and independently
  observed `compressed_bytes` ≤20 GiB. No layer has been downloaded here.
- `package_admission`: `offline_bytes_hashes_locked: true`,
  `signature_verification_stage: "source CPU before export"`, Jammy/amd64,
  exact closure byte count including all offline metadata ≤2 GiB and `lock_sha256`.
  The Mac compares that count and lock hash before reading credentials.
  This acknowledges that the
  first admitted source-CPU check verifies signatures and `.deb` identities;
  it is not a claim they have already passed.

The offline `debs/lock.json` needs schema version 1, `release: jammy`,
`architecture: amd64`, exact `required` versions for the eight system packages,
exact unchanged `base` package versions, `packages` and `indexes`. Each package
records local `file`, package/version/architecture, size/SHA256 and official
`ubuntu_filename`. Each index records official `origin`, local `inrelease`,
local `file` and its `release_name` in the Release SHA256 table. Supply the real
InRelease, Package index and `.deb` files; no hash placeholders are accepted.

The Mac checks file hashes, sizes, index linkage and exact seed versions. It has
no installed `gpgv` or `dpkg-deb`, so those tools run on the **same source CPU
Sandbox**, using its Ubuntu archive keyring, before runtime export. They run
again on the CPU Pod before installation. No Mac tool installation or extra
builder is required. Actual `dpkg --install`, audit and exact installed-version
checks enforce dependency closure; missing, conflicting or unavailable pins
fail. The missing addition is `libopengl0=1.4.0-1`. Complete package bytes,
snapshot availability and actual keyring/tool execution remain unverified.

After direct approval, an approval file binds the exact `plan_sha256`,
`additional_cap_usd: "12.00"`, and explicit true values for
`execute_CPU_preparation`, `transfer_frozen_bytes`,
`create_one_volume_and_CPU_Pod`, `retain_up_to14days_then_delete_owned_volume`
and `accept_failure_storage_until_cleanup`. No such approval exists now.

The future admitted command uses the existing controller interpreter and
explicitly supplied existing environment keys. It never parses a secret file
or creates credentials:

```sh
<existing-controller-python> -m coppertop.runpod_prepare --execute --config <filled-public-config.json> --approval <exact-approved-file.json> --debs <locked-offline-directory> --output .local/<unique-attempt>
```

## Storage, throughput and failure rules

The archive is one ≤50 GiB stream split into at most thirteen ≤4 GiB files.
Source scratch must have ≥52 GiB free, and the Mac requires ≥64 GiB free.
The exported strong manifest covers all files, modes and links, all full source
checkouts, frozen model/assets, package metadata and admitted code. No deleted
asset ZIP or new weight download is used. Failed exports retain partial files
and never create a complete bundle marker.

The 60 GB volume stores archive shards, metadata and later ≤2 GiB of recording
evidence. **Extracted runtime files never live on that volume.** The bootstrap
rejects a restore target there, uses container disk and checks the conservative
peak of old base + staged extracted bytes + ≤4 GiB installed packages against
70 GiB. It also requires enough actual free scratch. Required trees are renamed
onto `/opt/conda`, `/usr/local` and `/work`; `/opt/model` and `/opt/evaluator.py`
retain their required absolute targets. A fresh native interpreter performs
CPU admission after restoration. Failure leaves the volume/archive unchanged.

Multipart assembly accounts for **50 GiB archive +4 GiB current shard +96 MiB
sidecars =58,082,721,792 bytes**, below 60,000,000,000 allocated bytes. Input
upload parts and an extracted copy cannot be treated as free space. Reuse is
blocked while any input upload is incomplete. The relay uses the shared
16 MiB seekable uploader, at most four parts in flight, ≤4,096 S3 requests and
≤50 GiB +96 MiB payload in each direction; explicit upload cleanup has its own
≤64-request/300-second window. Boto3 retries are disabled. Source copies reserve
cumulative egress bytes before dispatch and never replay partial files silently.

Recent real 8 MiB probes must predict completion at **twice measured duration
plus 60 seconds overhead** within remaining phase time. Source probing happens
before export and again before shard copying; staging probes upload and full
readback before bulk upload. Slow/stale/nonfinite samples fail closed. These
probes do not guarantee sustained throughput; hard deadlines still stop the
attempt. The source, relay and CPU phases each have an inclusive two-hour
envelope, with a six-hour whole-attempt bound. The Mac must remain awake and
online for the entire attempt; a reachable independent operator must stop owned
compute if it loses connectivity. No provider dollar cap or cleanup scheduler
is implied by the controller timers.

The full controller scope is durable before source work. All state, intended
creates, upload IDs, budgets and phase telemetry are fsynced
locally before the next mutation. The ready marker is last, after full SHA256
readback. Resume uses identical local bytes and the same owned volume/upload IDs;
it never resets request/byte budgets or the two-hour staging clock. Out-of-order
parts may be replaced only during explicit resume inside the remaining budget.
A lost creation ID requires exact-prefix reconciliation; zero/multiple matches
stay unresolved and preserve source. Uncertain completion checks the existing
full object before further upload. Failed compute is terminated and absence
checked; failed storage remains charged and explicitly reported at $4.20/month.

The recovery CLI requires the exact original plan hash before reading keys:

```sh
<existing-controller-python> -m coppertop.runpod_prepare --recover .local/<attempt>/state.json --approved-plan-sha256 <exact-hash> --resume-staging
<existing-controller-python> -m coppertop.runpod_prepare --recover .local/<attempt>/state.json --approved-plan-sha256 <exact-hash> --abort-uploads
<existing-controller-python> -m coppertop.runpod_prepare --recover .local/<attempt>/state.json --approved-plan-sha256 <exact-hash> --delete-cache
```

Each action needs direct scope approval; these examples do not authorize it.
Resume does not rent or rerun a CPU Pod. Cleanup reconciles a lost resource create
without replacements, verifies Pod absence, hashes the surviving local archive,
aborts only owned uploads and explicitly deletes/observes absence of the exact
volume. The deletion CLI rejects a volume containing subsequent GPU evidence;
that evidence requires the GPU recovery/publication/source-preservation rules.
Source crash recovery stops only the source App ID bound to the same plan and
attempt name, with a separate 180-second stop and zero-active audit. A lost App
creation response without a durable ID requires the named operator to reconcile
that exact attempted App; the controller does not adopt a different App or
restart export. RunPod compute cleanup still proceeds if source cleanup fails.

The RunPod recording adapter applies the mount-root boundary from the parent's
Modal fix `572af3ab96c0f368ad83cac45d9a86bdd4f59e17`: it resolves only the
configured provider mount root before appending the owned run/archive path.
The shared archive core is unchanged. Local tests stage through a symlinked root
and still reject a symlink beneath it or a symlinked archive source. Actual RunPod
mount visibility and bootstrap remain a later live gate; the parent's paid Modal
mount preflight is not repeated here.

## Later owned-cache rollout and publication

After an actual CPU pass, `owned-cache.json` binds the original ownership,
manifest, tar, code and CPU receipt hashes and ≤14-day retention deadline.
`runpod_cloud` accepts that exact `runtime_cache` alongside the usual frozen
rollout configuration. It checks ownership, actual preparation receipt, unchanged
code and no incomplete inputs or other attached Pod. It creates **one GPU Pod
and zero volumes**, rehashes/restores the bundle on container disk, and waits
for fresh Pod CPU admission before the existing recording worker. A failed or
completed rollout keeps the prepared cache under its original retention terms.

The cache path reserves up to 6,300 s setup, 1,200 s rollout and 180 s cleanup, with
conservative three-hour GPU cost accounting. A one-hour publication session
cannot cover the worst-case restore. The CLI therefore reads no R2 credentials
during cache restore/rollout: it stops the GPU and records `awaiting_publication`.
An explicit recovery invocation with a fresh exact one-hour session publishes
the sealed archive through the existing shared interface. There is no automatic
credential derivation or renewed session. Preserve evidence until full readback
and receipt verification or explicitly approved source-preserving deletion.

The shared core at `55b324807dc3c86428ed44c34eb892c686b79acc` is integrated unchanged,
including ≤256 MiB streamed PUT and durable phase telemetry. The main task
confirmed its Mac-to-R2 test passed (37.1 s upload, 202.0 s full readback, 255.4 s total);
that evidence is bound to the exact shared files in
`containers/archive-live-admission.json`. It establishes neither RunPod S3 nor
restored runtime performance. No validation was duplicated here.
