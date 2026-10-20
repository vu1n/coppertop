# Local frame and window analysis

Observed interval sidecars and offline matched-outcome reduction are documented
in [semantic effects](semantic-effects.md). They use separate derived tables;
native frame/window snapshots and state-vector retrieval retain their semantics.

This optional foundation reads an existing Chords release, creates fresh local
Lance snapshots, and queries them through DataFusion. Canonical release, signal,
raw image/video and annotation objects are never rewritten. There is no service,
deployment, model download, remote catalog or provider execution.

Install the core project normally, then the hash-locked optional dependencies:

```sh
.venv/bin/python -m pip install --require-hashes -r requirements-analysis.lock
```

The tested combination is Python 3.12.14, PyArrow 20.0.0, NumPy 2.4.6,
`pylance` 13.0.0 (import `lance`, the open format), and DataFusion 54.0.0.
The existing core pins are unchanged; `.[analysis]` also declares the direct
optional dependencies. No LanceDB service is required.

```python
from pathlib import Path
from coppertop.chords import LocalStore
from coppertop.analysis import from_release, write_snapshot, Snapshot

data = from_release(LocalStore(Path('.local/store')), RELEASE_SHA256)
snapshot_sha256 = write_snapshot(data, Path('.local/analysis/new-snapshot'))
snapshot = Snapshot('.local/analysis/new-snapshot', snapshot_sha256)
rows = snapshot.query('''
    SELECT frame_id, time_ns, split, episode_success
    FROM frames WHERE frame_index >= 6 ORDER BY episode_id, frame_index
''')
neighbors = snapshot.nearest(data.table['state'][0].as_py(), k=5,
                             filter="split = 'development'")
```

## Contracts and scope

`from_release` reads bounded release/episode/annotation metadata and verifies the
signal Parquet object's hash through the existing ObjectStore interface. It
supports the imported state16/action12 representation and generated official
rollout representation. It reads no bulk camera payloads; rows retain observation
and camera references, source hashes, release/episode IDs, initialization groups,
splits, nanosecond control timestamps and the original outcome flags. This is
verification of the consumed metadata/signals, not a new full-raw-closure audit.

Native state and action values remain float64 in the derived table. The separate
`state_vector` is an explicit float32 view of the anchor state. Nearest search is
exact unindexed squared L2 over that vector. It is a useful numerical baseline,
not a learned embedding, geometric metric across mixed units, or demonstrated
semantic similarity. No model is executed. The `windows` table retains past-only
frame IDs/timestamps and padding-validity masks; default offsets are -6,-4,-2,0.
It never reads future observations into a history.

`success=false` on a prefix is not a completed failure. `episode_success` stays
null for imported demonstrations, partial/error recordings and bounded prefixes.
The existing official recorder's completed horizon600 result can supply an
explicit outcome; it must agree with the final observation. Termination and
truncation remain separate. Synthetic provenance remains labeled synthetic.
Initialization siblings cannot cross splits, and known development provenance
cannot be promoted to held-out data.

Each snapshot is created in a new directory. Its final `snapshot.json` binds
source references, exact offsets, Arrow content digests, implementation digest,
package versions, Lance versions and physical file hashes. Retain the returned
manifest SHA256 independently. Physical Lance IDs can differ on a repeat build;
the logical `content_id` reproduces for identical inputs/code/packages. Readers
open the recorded Lance version, even if an external writer adds a newer one.
The advisory `latest_version_hint.json` is excluded from physical identity because
it is mutable and never selects the version for this reader. No append, overwrite,
compaction or old-version cleanup operation is exposed. This is application-level
immutability; external tools can still alter or delete files, causing verification
or reads to fail.

The current limit is 10,000 total frames, 100 episodes per release, 8MiB of
release metadata, 64 history positions, 64MiB of decoded signal Parquet per
release, 64MiB per derived table/query result and 256MiB of physical snapshot
files. Repeated episode metadata is checked before row expansion. Snapshot
audits and query results are read in batches with cumulative buffer-byte/row checks
(including Arrow string-view buffers); nonempty SQL results merge emitted field
nullability by position, preserving duplicate column names from joins,
which can have stricter nullability than the logical plan. Empty results retain the
planned schema. These checks are conservative for batches sharing backing buffers;
these are application bounds, not a sandbox against hostile codecs or SQL
expressions that allocate oversized individual batches. DataFusion registers the
official `lance.FFILanceTableProvider` against
each pinned Lance dataset, with one target partition. Queries use the native
streaming provider, including basic filter and column projection pushdown. SQL
DDL, DML and other statements,
including COPY, are disabled. This is a local query API, not an untrusted SQL
service or process sandbox. Snapshot construction/opening still performs bounded
content-hash validation, and `table()` explicitly materializes a bounded table.
The presence of a streaming query provider does not lift any source/snapshot bound.

The first prototype tried DataFusion55's generic PyArrow dataset adapter, which
calls Lance fragment methods with incompatible arguments (`scanner` positional
schema / `get_fragments(filter=...)`). That is not the documented native integration.
The official FFI provider instead rejected DataFusion55 with `Incompatible DataFusion
table provider major version 54; expected 55`. Lance13's package test metadata
also declares DataFusion>=54,<55. Pinning DataFusion54 resolves that ABI mismatch
without changing PyArrow or Lance. A native probe returned the expected filtered
rows and an FFI `FilteredReadExec` plan. There is no silent Arrow fallback: an
unsupported provider/runtime combination fails registration explicitly. This is
a resolved integration/configuration issue, not a verified upstream limitation
of the supported combination or a whole-system performance result.

## Verification

```sh
.venv/bin/python -m unittest discover -s tests -p 'test_analysis*.py' -v
.venv/bin/python -m unittest discover -s tests -v
```

Unit fixtures compare SQL filtering/grouping with PyArrow, exact Lance neighbors
with NumPy, and verify native values, unknown outcomes, DEV grouping, history
boundaries, immutable-version reads, repeatable content identity and corruption
rejection. Native-provider regression tests forbid the Python `to_table` query
bridge, check registration failure stays closed and verify queries do not change
snapshot files. Lance/DataFusion integration tests skip when optional dependencies are
absent. `tests/test_analysis_prefix.py` is an opt-in smoke test using existing
preserved local archives; `COPPERTOP_PREFIX_ARCHIVES` is a JSON array of
`{"path": "/existing/evidence.tar", "sha256": "<verified digest>"}` entries.
It verifies archive/state/action bytes and writes only disposable derived output.
Those prefixes are real simulator traces but unfinished, repeated DEV evidence:
zero completed outcome labels and no predictive-performance claim. They are not
committed as a dataset or made eligible for training.

Official API references: [Lance read/write and snapshots](https://lance.org/guide/read_and_write/),
[official Lance/DataFusion FFI integration](https://lance.org/integrations/datafusion/),
[DataFusion Arrow registration](https://datafusion.apache.org/python/user-guide/data-sources.html),
[DataFusion SQL options](https://datafusion.apache.org/python/autoapi/datafusion/context/index.html).

## SlateDB replacement assessment

SlateDB is a plausible storage-engine upgrade for mutable native-process state,
not an automatic replacement for all Coppertop persistence. Current upstream
source at `a5c4fd22b36c836a89d8c4f27bf0b7b045e62e32` implements transactions with
Snapshot and SerializableSnapshot isolation; transactions are not merely a future
RFC. Its WAL/recovery, atomic batches, writer fencing, scans, caching and compaction
are useful machinery we should not rebuild. It supports object-store persistence,
including R2. No SlateDB package or migration is included here.

| Existing code | What a SlateDB migration could remove | What remains |
| --- | --- | --- |
| Restored `executor/common.py:durable`, `executor/ledger.py:Ledger` and similar Python JSON journals | File temp/link/replace/fsync mechanics, JSON-file read/write backend and local flock implementation, after one-writer ownership and transaction mapping are validated | Parent-ledger hashes, immutable history, active holds, budget/call accounting, consumed authorization, no-replay transitions, cleanup confirmation and recovery decisions |
| `chords.py:LocalStore` / `s3.py:S3Store` | Possibly a small mutable lookup/catalog backend; there is no custom LSM/WAL/compactor to remove today | SHA256 object identities, raw Parquet/MP4/NPY bytes, conditional immutable publication, release/fork/overlay semantics, provenance and full-byte readback. Do not move bulk immutable payloads into a KV engine without a reason |
| `cloud-controller/lane-a/worker.ts:Coordinator` | SQL table/read/write plumbing if moved to a supported native service | `shared/domain.ts:transition`, codecs/schema/epoch checks, idempotency, one-use dispatch/body claims, outbox delivery, alarms, Workflow scheduling, provider ambiguity and independent cleanup. The current SQL+alarm transaction boundary must be re-proven |

Awaiting `put` or transaction commit alone does not acknowledge durable storage:
the returned WriteHandle must reach `await_durable`, or an explicit flush must
complete, before acknowledging a consumed grant or performing an external effect.
Remote readers can see an older database view; they cannot authorize launches from
stale admission state. One active fenced writer is not a distributed multiwriter
scheduler or an exactly-once guarantee for provider calls. Rust/Python service use
is a natural fit; Workers deployment compatibility is not established here.

Thus SlateDB can improve the JSON/flock backend and is worth an isolated future
equivalence spike. It is not yet demonstrated equivalent to the complete DO/SQLite
coordinator, and it does not replace Chords semantics. Object-store durability
adds PUT/GET latency and background-process/operational responsibility. This
foundation needs none of that migration.

Sources: [current transaction API](https://github.com/slatedb/slatedb/blob/a5c4fd22b36c836a89d8c4f27bf0b7b045e62e32/slatedb/src/db.rs),
[durability acknowledgement](https://slatedb.io/docs/design/writes/),
[reader visibility](https://slatedb.io/docs/design/readers/),
[quickstart and integrations](https://slatedb.io/docs/get-started/quickstart/).
