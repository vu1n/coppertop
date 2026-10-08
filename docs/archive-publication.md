# Episode archive publication

The producer seals one uncompressed USTAR archive before copying it to durable
storage. The CPU publisher uploads a seekable archive up to 256 MiB with one
streamed conditional PUT; larger archives and explicit multipart resumes use
16 MiB parts (at most four in flight). It reads the entire uploaded object through
SHA256, and only then writes and reads back a small completion receipt. It never
publishes individual episode files. The existing short feasibility workloads keep
their small-object path. The shared core is unchanged from
`55b324807dc3c86428ed44c34eb892c686b79acc`, including durable phase and readback
progress telemetry. The main task verified a 156,098,560-byte Mac-to-R2 archive
(37.1 s upload, 202.0 s full SHA256 readback, 255.4 s total), its completion receipt
and exact test-object/upload cleanup. The confirmation and core hashes are in
[archive-live-admission.json](../containers/archive-live-admission.json).
This test establishes neither a restored RunPod runtime nor RunPod S3 performance;
the 1 GiB multipart boundary still needs its separately approved live validation.

`coppertop/record_archive.py` is the shared Modal/RunPod interface:

```python
descriptor = seal_directory(output_directory, local_archive_path)
staged = stage_archive(local_archive_path, descriptor, new_stage_directory,
                       run_id=attempt_id, commit=provider_commit)
# Return the GPU control receipt only after stage_archive returns successfully.
result = publish_archive(new_stage_directory / "episode.tar", staged, s3_store,
                         record=bounded_provenance,
                         state=restored_upload_state, persist=durable_save_state,
                         observe=durable_phase_telemetry)
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
deletion and unrelated keys. In-memory PUT is limited to a 64 KiB receipt; an
ordinary seekable file can use streamed PUT up to 256 MiB. Upload parts are
limited to 16 MiB. Request and byte quotas are locked across concurrent workers.
The Cloudflare [temporary-credential action documentation](https://developers.cloudflare.com/r2/api/s3/temporary-credentials/)
lists these actions; the [boto3 example](https://developers.cloudflare.com/r2/examples/aws/boto3/)
documents the low-level multipart approach. This work did not mint credentials.

## Durability and cleanup

Provider adapters may resolve their configured, owned mount root before
appending relative attempt paths. The RunPod adapter carries the boundary from
the parent's Modal fix `572af3a`. The generic archive functions still reject
symlink source/destination paths, descendant links and archive-member traversal.
Resolving an arbitrary archive path is not an allowed workaround. Local alias
fixtures are not proof of a provider's real mount or durability barrier.

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
| Failed-attempt shape: 1939 files | 154493961 | 156098560 | 10 | 19 | about 64.1 MiB |
| Worst supported CAS shape: 6000 files | 1073741824 | 1078988800 | 65 | 74 | about 64.1 MiB |

The second profile has no injected network latency. Counts include HEAD, create,
list-parts, each part, completion, one full archive GET, and receipt PUT/two HEADs/GET.
They exclude staging-provider commit requests. The old 1939-object-plus-report
path required approximately 7760 R2 requests. These are CPU synthetic transport
measurements, not official-loader execution or cloud throughput measurements.
The realistic profile also verifies every restored file and existing reader bytes.

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

Final local verification: 324 CPU tests ran; 321 passed and three optional SDK
checks skipped. All three SDK checks passed separately in the existing controller
environment. No packages were installed. Independent
correctness/structure and security/refutation reviews resolved destructive staging
cleanup, oversized resumed parts, lost upload journals and sparse-header parsing.
The size-bounded retention transitions and resource cost accounting are tested
locally; no live provider capability check is claimed.

## Minimal next live proposal (not authorized or executed)

Use one local CPU process on the Mac with the already-installed controller SDK;
upload the synthetic 1939-file fixture to one fresh dedicated R2 prefix and fully
verify its archive/receipt. No GPU, Modal/RunPod allocation, weights, assets or new
dependency installation is needed. Use one short-lived scope with only the
documented archive actions; no automatic retries. Bound the run to 10 minutes and
remote storage to 150 MiB plus a 64 KiB receipt. Keep the local source until exact
remote cleanup is verified.

The next reviewed plan must specify retained-size/ongoing-storage terms and
exact-key cleanup/abort authority. A failed local process leaves its preserved
source available for an explicitly authorized recovery/deletion decision. The current archive session intentionally has no DeleteObject permission.
An independently approved exact-key cleanup path is needed; existing recordings
and earlier partial objects must be preserved. This proposal validates real R2
multipart/session/SDK behavior; it does not validate Modal mount/preparation delay.
Provider charges and any cumulative ceiling must be checked before approval using
the actual account balance and current [R2 pricing](https://developers.cloudflare.com/r2/pricing/).
