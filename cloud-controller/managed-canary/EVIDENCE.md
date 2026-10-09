# Local preparation evidence

Base is merged PR 9, `c13ee77726be627feab228942195666c9034cc7f`.
These checks ran in the cloud-dev executor on the implementation working tree.
The captured [local-results.txt](local-results.txt) records the exact runtime
source/lock hashes. The subsequent Git commit records those same executable
inputs; this is not a clean-checkout run or managed-service evidence.

| Check | Result | Scope |
| --- | --- | --- |
| TypeScript strict typecheck | Passed | Native entrypoints, ledger/provider, tests and imported codecs |
| Compiler hygiene | Passed | No unused locals/parameters |
| Controller safety tests | 10 passed | Approval, ownership, transport ambiguity, deletion denial and bounds |
| Native runtime tests | 5 passed | Actual Miniflare/workerd, supported native probes, SQLite and service binding |
| Required Python suite | 425 total, 3 skipped | 422 passed; unchanged Python; no official loader source enabled |
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

The source build digest for this preparation is `b070ca02ed9d1ba0e9f38512bc7003686e60d6a44af7c1c279b1d4d0be41abfe`.
An expired plan must be regenerated after approval. Generated deployment
configuration and bundles remain in ignored `.local/`, with no credentials.

| Bundle | Bytes | SHA-256 |
| --- | ---: | --- |
| provider | 551596 | `8067ee18b2fc433b347fa0c6470597b29d317771d5d71d062399f0007e738b1b` |
| controller | 347434 | `188c7742d1669b58216acf8dcd79e40ffa48795be39fba66086ff8a03cfd2a14` |
| retire | 392 | `b8a95c1702dc7beb3266e588b43e2e4693488d7f9d61776628f5ce656efc1377` |

Bundle bytes are executable code, distinct from the under-1-MiB synthetic
application-data scope. The test's application byte count does not measure
Cloudflare's internal history, billing metadata, or physical page allocation.

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
