# Episode archive publication

The producer seals one uncompressed USTAR archive before copying it to durable
storage. For archives up to 256 MiB, the CPU publisher passes a native seekable
file handle to one conditional PutObject. Larger archives and any explicitly
resumed multipart upload use 16 MiB parts (at most four in flight). It reads the
entire uploaded object once through
SHA256, and only then writes and reads back a small completion receipt. It never
publishes individual episode files. The existing short feasibility workloads keep
their small-object path.

`coppertop/record_archive.py` is the shared Modal/RunPod interface:

```python
descriptor = seal_directory(output_directory, local_archive_path)
staged = stage_archive(local_archive_path, descriptor, new_stage_directory,
                       run_id=attempt_id, commit=provider_commit)
# Return the GPU control receipt only after stage_archive returns successfully.
result = publish_archive(new_stage_directory / "episode.tar", staged, s3_store,
                         record=bounded_provenance,
                         state=restored_upload_state, persist=durable_save_state)
receipt = read_completion(s3_store, result["report_key"])
unpack_archive(downloaded_archive_path, receipt["archive"], new_output_directory)
```

`s3_store` is the existing `S3Store` with an explicitly supplied authorized client.
`durable_save_state` must atomically save each `UploadState.to_dict()` snapshot and
finish its provider commit barrier before returning. A restart loads that snapshot
with `UploadState.from_dict`; resume is explicit, never an automatic retry. Parts
completed out of order without a saved receipt can be replaced safely. A retained
upload can also be explicitly aborted with `S3Store.abort(state)`. Fresh resume
authorization may be needed if a previous session's byte budget or expiry was
exhausted. No parent credential enters GPU compute.

The 256 MiB threshold fits below both the documented
[R2 5 GiB single-part limit](https://developers.cloudflare.com/r2/platform/limits/)
and [RunPod's less-than-500 MB PutObject limit](https://docs.runpod.io/storage/s3-api)
(checked 2026-10-08). This is a size compatibility check, not a claim that the
RunPod storage API or conditional headers have been validated. R2 remains the
canonical destination from either compute provider. Single PUT has no part-level
resume; an uncertain response fails without retry or multipart fallback.

The archive contains `coppertop-archive-manifest.json` followed by sorted ordinary
files. The manifest records every relative file path, byte count and SHA256. The
whole archive's SHA256 and size live outside the archive in its descriptor and
completion receipt, avoiding a self-referential hash. Normalized USTAR headers
make archive bytes reproducible across source mtimes and permissions; file bytes
and relative paths remain identical. Empty directories and filesystem permissions
are not dataset content. Parquet, videos, native state/action semantics, timestamps,
schemas, provenance, overlays and release/fork objects are preserved byte for byte.
No transcoding or model-specific transformation occurs.

Unpacking restores the existing output directory, including `store/objects`, so
the current `LocalStore` and release readers work unchanged. It verifies the whole
archive first, preflights ordinary USTAR headers before invoking `tarfile`, then
checks every file against the internal manifest. It rejects traversal, unexpected
members, duplicates, links, sparse files, GNU/PAX extensions, collisions, missing
files and corruption. It uses exclusive creation and never `extractall`.

Bounds: 1 GiB raw file bytes, up to 6000 CAS objects plus 20 bounded sidecars, 2 MiB
internal manifest, 1 GiB + 16 MiB archive, 60 KiB provenance record and 64 KiB completion receipt. Worst producer
scratch is 2 GiB + 16 MiB because source and archive coexist; this is explicit in
the new live plan. Durable staging remains bounded to 2 GiB. Python multipart
payload memory is at most four 16 MiB parts plus bounded metadata/read buffers.
ETags are opaque completion handles, not MD5/SHA integrity proofs. Integrity does
not depend on optional provider checksum response fields.

R2 uses a distinct `mechanism_archive` role. It adds only multipart create, upload,
list-parts, complete and abort operations to Get/Head/Put, for one dedicated
content-addressed attempt prefix. It denies bucket/object enumeration, object
deletion and unrelated keys. Byte-buffer PUTs are limited to 64 KiB. Only this
archive role also permits a native regular file, positioned at zero, with exact
ContentLength of at most 256 MiB. The full write budget is reserved before dispatch.
Upload parts are
limited to 16 MiB. Request and byte quotas are locked across concurrent workers.
The Cloudflare [temporary-credential action documentation](https://developers.cloudflare.com/r2/api/s3/temporary-credentials/)
lists these actions; the [boto3 example](https://developers.cloudflare.com/r2/examples/aws/boto3/)
documents the low-level multipart approach. This work did not mint credentials.

## Durability and cleanup

`stage_archive` copies and hashes the payload, verifies staged bytes, then calls
the provider commit barrier. The GPU control receipt includes the archive identity
and an explicit verified-durability acknowledgement. Successful phase ordering is
stage acknowledgement → verified GPU termination → CPU publication → verified
completion receipt → zero-active audit → deletion of the exact owned stage.

Copy or commit failure preserves source and all copied staging bytes. There is no
durability acknowledgement on failure. A worker crash or provider timeout **before
a durable checkpoint** can still lose data held only on its disposable local
disk; retaining an empty/partial Volume cannot reconstruct it. This implementation
does not claim to solve that failure class. It never intentionally deletes a
staged recording because publication failed.

The full Modal publisher mounts the owned Volume writable solely to persist the
bounded upload journal; it never rewrites the sealed archive. Other publishers
keep their read-only mounts. After failed/unverified full publication the controller
stops compute, then retains nonempty or unmeasured owned staging only when the
launch explicitly accepts ongoing size-bounded storage. This also covers a GPU RPC
that did not return a usable receipt. A provider metadata read reports all retained
file bytes (32-entry/60-second bound); failure reports an unknown size and the
measurement error, rather than deleting the only possible complete copy. A verified
empty owned stage is deleted. Verified archival publication permits stage deletion;
a metadata-only failure report does not.

The retained-resource receipt includes exact provider/resource ID/name, measured
bytes or an explicit measurement error, maximum authorized bytes, whether the
measurement fits that authorization, monthly rate and estimated cost, cleanup
owner and release condition. It is saved as `retention.json` alongside ownership
and outcome evidence. A size violation or unknown measurement requires follow-up;
neither is represented as verified bounded storage. Compute is stopped before
retention is reported.

Next live full-recording approval must include this explicit policy:

```json
{
  "max_bytes": 2147483648,
  "accept_ongoing_storage_charges": true,
  "cleanup_owner": "operator",
  "release_condition": "verified_publication_or_explicit_recovery_or_deletion"
}
```

The policy and rate are bound into the exact cloud-plan approval hash. Missing
policy or unaccepted ongoing charges block execution before key access/allocation.
At the published [Modal Volume rate](https://modal.com/pricing) of $0.09/GiB/month
(checked 2026-10-08), 2 GiB costs at most $0.18/month before account allowances;
the synthetic 156098560-byte archive is about $0.0131/month plus small metadata.
Actual invoice amounts depend on usage, duration and account allowances. This is
ongoing storage acceptance, not a fixed total-spend ceiling or an automatic expiry.
The next live approval must separately reconcile the actual account balance and
bounded compute/R2 request costs. No paid retained resource is authorized now.

Release is verified publication followed by deletion of the exact owned stage,
or a separately authorized recovery/deletion decision. A later recovery still
needs its own compute/session authorization and exact ownership verification.
A failed cleanup API call remains a reported failure, requiring the named owner to
resolve it; it is never silently called successful. If a hard time limit is desired,
the live plan must explicitly choose expiry deletion even if that discards the only
unpublished archive, or use an independently verified expiry mechanism. This work
adds no recurring cleanup service, deployment, timer or scheduler configuration.

## Measurements and validation limits

Local filesystem fake, with 20 ms simulated latency per request:

| Synthetic profile | Raw bytes | Archive bytes | Parts | Requests | Python peak |
| --- | ---: | ---: | ---: | ---: | ---: |
| Failed-attempt shape: 1939 files, forced multipart | 154493961 | 156098560 | 10 | 19 | about 64.1 MiB |
| Same shape, streamed single PUT | 154493961 | 156098560 | 0 | 7 | about 0.51 MiB |
| Worst supported CAS shape: 6000 files | 1073741824 | 1078988800 | 65 | 74 | about 64.1 MiB |

The single-PUT and worst-case profiles have no injected network latency.
Multipart counts include HEAD, create, list-parts, each part, completion, one full
archive GET, and receipt PUT/two HEADs/GET. Single PUT removes the multipart RPCs.
They exclude staging-provider commit requests. The old 1939-object-plus-report
path required approximately 7760 R2 requests. These are CPU synthetic transport
measurements, not official-loader execution or cloud throughput measurements.
The realistic profile also verifies every restored file and existing reader bytes.
An existing boto3/botocore 1.43.108 SDK test consumes the native 156098560-byte
request body through a mocked HTTP transport in bounded chunks: one dispatch and
about 2.43 MiB Python peak. No real HTTP request is made by that test.

Returned timings cover seal, durable copy (including verification and commit),
publisher preparation (including source verification and client setup), time from
publisher function entry to first upload dispatch, upload (including journal
commits), full readback, and receipt publication. The stored receipt includes
timings available before its own publication; the final receipt duration is
returned out of band to avoid a recursive timing/hash dependency. Container
bootstrap/mount delay before function entry still needs provider telemetry.
The previous 405-second pre-upload delay is unexplained; these local results do
not establish that packaging eliminates it.

Tests cover stable archive identity, restored existing ObjectStore bytes, bounded
memory/requests, interruption/resume with opaque ETags, corrupt readback, completion
ordering, incomplete receipts, source preservation, staging commit failure,
oversized resumed parts, malformed/special tar members, role/query denial, and
cleanup ordering. The fake Modal-function test interrupts publication and verifies
that the durable journal reuses the same upload ID on explicit reinvocation.

Final local verification including the validation harness: 344 CPU tests ran;
341 passed and three optional SDK
checks skipped. All three SDK checks passed separately in the existing controller
environment, as did the sixteen harness tests. No packages were installed. Independent
correctness/structure and security/refutation reviews resolved destructive staging
cleanup, oversized resumed parts, lost upload journals and sparse-header parsing.
The size-bounded retention transitions and resource cost accounting are tested
locally; no live provider capability check is claimed.

## Ready local CPU validation harness (live execution not authorized)

`coppertop.archive_validation` prepares the synthetic 1939-file fixture locally,
seals its 156098560-byte archive, and creates a fresh exclusive run directory,
random dedicated attempt prefix, public plan and disabled approval example. It
never reads credentials or calls providers during preparation. The plan binds
the archive hash, six named runtime-file hashes, the single-PUT threshold and Python executable path;
it does not attest every transitive dependency or the interpreter binary.

The existing controller runtime has boto3/botocore 1.43.108 and s3transfer 0.19.2.
No installation, GPU, Modal/RunPod allocation, weights or dataset download is
needed. From the repository root, offline preparation is:

```sh
/Users/vuln/Documents/Codex/2026-10-05/task/gpu-smoke-check/controller-venv/bin/python -m coppertop.archive_validation --prepare .local/archive-validation-new
```

Preparation accepts `--execution-seconds` and `--publication-requests-max` to
reduce the original 600-second and 32-request ceilings. Only positive integers
within those ceilings are accepted. Both limits are bound to the fresh plan's
approval hash and enforced during execution; cleanup retains its separate bounds.

After direct approval of the exact plan, copy `approval.example.json` to
`approval.json` in that directory and set its five explicit boolean approvals
to true. These cover execution, session derivation, failure cleanup and unresolved
cleanup storage acceptance. No secrets belong in this file. The execution command
below is for that separately authorized step:

```sh
/Users/vuln/Documents/Codex/2026-10-05/task/gpu-smoke-check/controller-venv/bin/python -m coppertop.archive_validation --execute .local/archive-validation-new --approval .local/archive-validation-new/approval.json --env-file /Users/vuln/code/coppertop/.env.local
```

Execution validates approval before accessing existing local configuration. It
permits at most two existing-token verification API requests under a separate
120-second deadline, then locally derives a 20-minute publisher capability for
the exact configured account, `coppertop` bucket and fresh attempt prefix.
The parent token is never passed to the publisher child or written to evidence.
The child gets only temporary publisher credentials and uses the existing
`mechanism_archive` role with streamed full SHA256 readback and verified completion
receipt. Both objects explicitly request `STANDARD` storage. Publication has a
600-second subprocess deadline, no automatic retries/resume, and caps of 32 R2
requests and 150 MiB + 64 KiB writes. Socket timeouts are 30 seconds connect /
60 seconds read with one SDK attempt.

Before each object-creating operation, a fresh-key HEAD must establish absence.
An existing matching archive is rejected even on the shared publisher fast path.
The intended owned key is fsynced before dispatch; multipart creation intent is
journaled separately so a single PUT does not require an upload ID. Multipart state is atomically
fsynced after each contiguous part. Metadata files are bounded to 64 KiB. Local
source, sealed archive, journals and results are preserved on every outcome.

On timeout, the parent kills and waits for the child before cleanup. Cleanup has
its own 300-second hard deadline, including SDK setup, and derives a fresh
20-minute capability for at most two exact journaled object paths. Only HeadObject,
DeleteObject, AbortMultipartUpload and ListParts are permitted; there is no prefix,
download, bucket listing, object enumeration or unrelated-key access. Cleanup
uses at most 16 R2 requests. Publisher credentials have ten minutes beyond the
execution budget; cleanup credentials have fifteen minutes beyond their cleanup
budget. Credential expiry never acts as object expiry.

On success or failure, cleanup aborts the exact known upload, checks ListParts
reports NoSuchUpload, deletes only the synthetic attempt's objects and verifies
absence with HEAD. Uncertain completion and receipt failure follow this same
path. Invalid upload journals do not skip trusted exact-key deletion; abort
verification fails explicitly. A lost CreateMultipartUpload response can leave
an unknown upload ID, which is reported as unverified cleanup rather than widening
access. `result.json` records process, publication and cleanup outcomes; the
command fails if either publication or cleanup is unverified.

Remote storage is intended to last under fifteen minutes after publication
starts. No scheduler or expiry mechanism is added. If cleanup fails, the operator
must resolve the exact reported resources; approval explicitly accepts bounded
ongoing storage until resolution. No hard storage-duration guarantee is claimed.
Verified cleanup means observed upload/object absence; killing the child cannot
revoke a completion already accepted by R2. Late-completion ordering remains a
real-provider verification limit.

Current [R2 pricing](https://developers.cloudflare.com/r2/pricing/) is $0.015 per
GB-month Standard storage, $4.50/million Class A, $0.36/million Class B and free
egress. Nominal usage for the short run is below $0.001; a month at the
157351936-byte cap is about $0.00236 before rounding. Daily peak storage and
rounded billing units apply; account allowances are not assumed. The plan allows
$0 cloud compute and a conservative $5 additional R2 invoice allowance: one added
Class A, Class B and storage billing unit totals $4.875. This is an authorization
allowance, not a provider-enforced invoice ceiling or lifetime retention cap.
Billing-period boundaries, unrelated activity and unresolved storage can affect
the invoice; reconcile actual account billing before launch. DeleteObject and
AbortMultipartUpload are free operations. No new permanent key is requested.

Offline tests drive the publisher child interface through a filesystem fake:
12 requests including ownership HEADs and a second receipt verification, no parts,
one full archive GET and two small receipt GETs. This extends the seven-request core
measurement above. Controller mocks cover timeout, uncertain completion, receipt
failure, expired cleanup credentials, invalid journals, destination collisions
and source preservation. The first live multipart attempt proved session access
and part acknowledgements; single PUT, full readback and completion remain
unverified on R2. Modal startup/mount delay and RunPod inventory are
separate pending actions.

## First live validation and revised diagnostics

The separately approved 2026-10-08 multipart validation acknowledged all ten
parts (156098560 bytes) in 66.81 seconds, about 2.23 MiB/s, then exceeded the
600-second publication deadline. No full hash readback or completion receipt was
verified. The previous harness did not durably record phase entry, so the actual
blocking phase is unknown. SDK completion response draining can exceed a socket
inactivity timeout; this is a possible blocking path, not a proven cause.

Cleanup observed NoSuchUpload, a successful exact-key delete, and HEAD absence.
An earlier abort returned ClientError whose code was not retained, so the original
overall cleanup result remains false. A late accepted completion cannot be excluded.
Original evidence is preserved in `.local/archive-validation-20261008-ready/`,
including `result.json`, `measurements.json` and `closeout.json`. The cleanup window
and process-local capabilities expired; no further provider calls were made.
A further check needs fresh authorization for a five-minute cleanup-only window
and twenty-minute capability allowing Head/Delete/Abort/ListParts for that exact
archive key and known upload ID, with no new publication or broad listing.

The revised harness fsyncs `publisher-telemetry.json` and `cleanup-telemetry.json`
on phase/RPC entry, success and error, including duration, allowlisted HTTP status,
request ID, error code and readback bytes. It omits response bodies, headers,
credentials and frame locals. Long blocking phases also trigger one bounded stack
snapshot after sixty seconds in `publisher-stacks.log`. Killed children leave their
last entered phase durable. Multipart telemetry and explicit recovery remain intact.
The old plan is single-use and no longer matches the revised implementation hash.
The minimum next validation is a newly prepared and explicitly approved local CPU
single-PUT upload/readback/receipt/cleanup test; it needs no GPU or model download.

## RunPod integration and subsequent shared evidence

The main task subsequently verified the 156,098,560-byte streamed single-PUT
archive, full SHA256 readback, completion receipt and exact test cleanup
(37.1 s upload, 202.0 s readback, 255.4 s total). The earlier checkpoints above
remain historical evidence. The confirmation and unchanged shared-core hashes
are bound in [archive-live-admission.json](../containers/archive-live-admission.json).
This establishes neither full-size multipart completion nor RunPod runtime/S3
performance; no paid validation was duplicated in the RunPod task.

The [RunPod preparation controls](runpod-preparation.md) add bounded source export,
local relay, container-disk restoration and owned-cache recovery. The recording
adapter applies parent fix `572af3a` by resolving only its configured provider
mount root before appending run/archive paths. The generic archive functions
continue to reject symlink source/destination paths, descendant links and member
traversal. Resolving an arbitrary archive path is not an allowed workaround.
Actual provider mount/bootstrap admission and paid rollout remain separate gates.
