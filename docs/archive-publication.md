# Episode archive publication

The producer seals one uncompressed USTAR archive before copying it to durable
storage. The CPU publisher uploads that seekable archive with 16 MiB multipart
parts (at most four in flight), reads the entire uploaded object once through
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
deletion and unrelated keys. Single PUT is limited to 64 KiB; upload parts are
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
keep their read-only mounts. On failed/unverified full publication, the controller
stops compute and conservatively retains the owned stage, even if the GPU RPC did
not return a usable receipt. It records ownership and the deadline acknowledgement.
Verified archival publication permits stage deletion; a metadata-only failure
report does not.

Next live full-recording launch requires an explicit retention policy containing
`seconds` (60..86400), `max_bytes` (2 GiB), `max_storage_usd` (positive finite ceiling),
`cleanup_owner`, and `expiry_action="delete_owned_stage_even_if_unpublished"`.
The deadline is bounded by admission + controller limit + retention duration.
It also requires an injected `retention_backstop` provider adapter which durably
arms cleanup of the exact owned Volume and returns `armed`, `volume_id`, and
`deadline_utc`. Missing policy/adapter blocks execution before key access or
allocation. **No such provider adapter has been configured or armed here.**
The policy is not an assertion that storage is free or that an in-process timer
provides durable cleanup. Expiry deletion can discard unpublished data and must
be part of the next reviewed live authorization. No indefinite retention is implied.

### Concrete Modal scheduling proposal and unresolved provider limits

`coppertop/modal_retention.py` supplies a pure, hashed cleanup plan and local SDK
definitions using existing Modal Cron. It binds the exact workload App ID and
Volume ID/name, with intended dispatches at the deadline (rounded up to a minute),
five minutes later and ten minutes later. Each call audits zero active workload
containers/sandboxes, deletes only the exact owned Volume, then stops the cleanup
App using its actual executing container identity. The final failed call also
attempts to stop the scheduler. The cleanup call has a 360-second execution bound
and 300-second startup bound; no GPU, data mounts or secrets are added. Tests cover
early execution refusal, active compute, changed identity, absent Volume, ordered
cleanup, local SDK definition and explicit unresolved arming conditions.

This is a **plan, not an armed backstop**. Native Cron is yearless and recurs, and
Modal [schedules require deployment](https://modal.com/docs/guide/cron). A timeout,
startup failure or failed self-stop can leave the schedule alive; failed Volume
deletion can leave storage charged. Three intended dispatches do not enforce three
invocations. The plan explicitly sets `finite_schedule_enforced=false` and
`live_ready=false`; it exposes no deployment/arming method, and the full launcher
rejects it even if given a callable returning a claimed acknowledgement.

Before live full recording, a separately verified expiry mechanism must cap the
independent scheduler's lifetime and provide terminal disposition if Volume
deletion fails. The operator must approve that mechanism's deployment, any cleanup
compute/storage budget, and expiry deletion of unpublished data. The cached image
must mount and hash-check the reviewed cleanup module with `copy=False`; no new
image build is implied. A CPU-only live capability check must establish that Modal
container credentials can stop/audit the exact workload App and delete the exact
Volume; controller privileges do not prove container privileges. No credentials
need be pasted into chat, and no new token is presumed necessary. A provider/API
failure still requires a named owner to resolve cleanup; neither the storage cost
ceiling nor the existing fake tests prove enforcement. These are explicit remaining
settings/validation requirements, not an implemented readiness claim.

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

Final local verification: 326 tests ran in the CPU environment, 322 passed and
four optional SDK checks skipped. All four SDK checks passed separately in the
existing controller environment. No packages were installed. Independent
correctness/structure and security/refutation reviews resolved destructive staging
cleanup, oversized resumed parts, lost upload journals and sparse-header parsing.
The native Cron proposal's live limitations remain explicit and launch-blocking;
they are not represented as a completed provider capability check.

## Minimal next live proposal (not authorized or executed)

Use one local CPU process on the Mac with the already-installed controller SDK;
upload the synthetic 1939-file fixture to one fresh dedicated R2 prefix and fully
verify its archive/receipt. No GPU, Modal/RunPod allocation, weights, assets or new
dependency installation is needed. Use one short-lived scope with only the
documented archive actions; no automatic retries. Bound the run to 10 minutes and
remote storage to 150 MiB plus a 64 KiB receipt. Keep the local source until exact
remote cleanup is verified.

The next reviewed plan must specify a finite retention deadline and exact-key
cleanup/abort authority, including how cleanup continues if the local process
fails. The current archive session intentionally has no DeleteObject permission.
An independently approved exact-key cleanup path is needed; existing recordings
and earlier partial objects must be preserved. This proposal validates real R2
multipart/session/SDK behavior; it does not validate Modal mount/preparation delay.
Provider charges and any cumulative ceiling must be checked before approval using
the actual account balance and current [R2 pricing](https://developers.cloudflare.com/r2/pricing/).
