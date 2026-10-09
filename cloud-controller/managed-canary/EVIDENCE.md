# Local preparation evidence

Base is merged PR 9, `c13ee77726be627feab228942195666c9034cc7f`.
These checks ran in the cloud-dev executor on the implementation working tree.
The captured [local-results.txt](local-results.txt) records six partial runtime
source/lock hashes; it is a provenance record, not binding identity or a runtime behavior. The subsequent Git commit records those same executable
inputs; this is not a clean-checkout run or managed-service evidence.

| Check | Result | Scope |
| --- | --- | --- |
| TypeScript strict typecheck | Passed | Native entrypoints, ledger/provider, tests and imported codecs |
| Compiler hygiene | Passed | No unused locals/parameters |
| Controller safety tests | 24 passed | One-use approval/atomic journal, raw evidence audit, pagination, bounded transient reads, ownership, transport ambiguity, deletion denial and bounds |
| Native runtime behaviors | 5 passed | Actual Miniflare/workerd, supported native probes, evidence audit, timing gates, delayed cancellation, SQLite and service binding |
| Partial-source provenance record | 1 completed | Hashes six named files; starts no runtime and claims no binding identity |
| Required Python suite | 425 total, 3 skipped | 422 passed; no official loader source enabled |
| Wrangler 4.144.0 dry-run packaging | 3 passed | Both Workers and namespace deletion migration; no upload |
| Whitespace check | Passed | `git diff --check` |
| Managed execution / deletion | Not run | Separate approval and billing gate |
| Managed host-process-loss recovery | Untested | No supported matching injection established |

Commands are in the [runbook](README.md). Installation used exact lockfiles,
`--ignore-scripts` and an ignored `.local/npm-cache`; Node was 24.19.0.
The managed package imports the pinned Lane A persisted codecs, so both locked
dependency graphs were installed. No upstream Lane B engine code was changed.

Wrangler required a writable local config/log directory in this executor; the
packaging wrapper sets XDG_CONFIG_HOME under `.local/`, disables metrics and
uses error-level logs. An initial default-cache attempt failed to create the
executor's default home cache; it was rerun using the writable `.local/` cache.
No network restriction or approval gate was bypassed.

The source build digest for this preparation is `d942b0c0fa3f27edd4fb3584967e9454e1c5baf13ab6ac827fb82ff62bad7a5b`.
An expired plan must be regenerated after approval. Generated deployment
configuration and bundles remain in ignored `.local/`, with no credentials.

| Bundle | Bytes | SHA-256 |
| --- | ---: | --- |
| provider | 551596 | `8067ee18b2fc433b347fa0c6470597b29d317771d5d71d062399f0007e738b1b` |
| controller | 348247 | `886e5ed7777929321ae196eee45b00e50ad50597f9d63e471fd490ec408ac0df` |
| retire | 392 | `b8a95c1702dc7beb3266e588b43e2e4693488d7f9d61776628f5ce656efc1377` |

Bundle bytes are executable code, distinct from the under-1-MiB synthetic
application-data scope. The test's application byte count does not measure
Cloudflare's internal history, billing metadata, or physical page allocation. The
updated full-scenario assertion recorded **201,053 bytes**, including returned raw
controller evidence, provider/ledger state, probe statuses and observer output.

API cleanup mocks use the `instances/batch/delete` POST operation inspected
in pinned Wrangler 4.144.0 and the documented `deleted_classes` migration.
Actual account-side endpoint permissions, deletion semantics and billing
remain deployment preflight/managed-test gates. DO namespace pagination
ambiguity or an API 403 aborts preflight before any upload.

The initial spike's strict restart gates retain their documented failures.
The hosted characterization jobs deliberately assert those known failures;
green CI never upgrades them to managed recovery acceptance. No Modal
variables or backups were inspected, no provider job was run, and no remote
resources, account settings, credentials or subscriptions were changed.


## PR 10 feedback verification

All [thirteen review dispositions](REVIEW.md) are recorded. On the updated working
tree, `npm run check` passed strict typecheck, compiler hygiene and **30 total
checks: 24 controller regressions, five actual runtime behaviors and one
partial-source provenance record** (49.590 seconds). The initial required offline Python
suite passed 425 total with three skips in 28.318 seconds before the fixture
correction below. Whitespace checks passed.

All three dry-run bundles were refreshed with:

```sh
npm run package -- ct-native-review-10 ../../.local/managed-canary-review-package
```

Their final hashes are listed above; provider and retirement bundle bytes remain
identical, and the controller bundle changed for timing/evidence/wait behavior.
No approval document was supplied or consumed for a managed run. Exclusive-claim
regressions used only disposable temporary local directories. Canary executable inputs remain unchanged after these checks. The subsequent
CI-fixture correction below changes only a Python test helper and documentation.


## Hosted CPU fixture correction

The first updated-head [CPU run](https://github.com/vu1n/coppertop/actions/runs/37981796769)
failed in the unchanged lost-ack Runpod source-spool test, during temporary
filesystem cleanup: `OSError: [Errno 39] Directory not empty: 'remote'`.
The fixture registered subprocess termination with unittest.addCleanup, which ran
after TemporaryDirectory exited; its producer could still create another part
while the directory was being removed. The whole sidecar CI run passed.

A test-only follow-up uses ExitStack to terminate/wait for every owned subprocess
before the enclosing temporary directory is removed, for the three fixture users.
No provider adapter or production behavior changed. All seven source-spool tests
passed in 3.046 seconds. The complete Python suite was rerun after this correction;
the final result is recorded below. Canary sources, lockfiles, build digest and
all three bundle hashes are unchanged.

Final required offline Python result after the fixture correction: **425 total,
422 passed, three skipped**, in **29.166 seconds**. No model/dataset download or
external provider call occurred.
