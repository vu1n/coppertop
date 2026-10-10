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
| Controller safety tests | 28 passed | One-use approval/atomic journal, raw evidence audit, pagination, bounded transient reads, ownership, transport ambiguity, deletion denial and bounds |
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

The source build digest for this preparation is `927ba64511ee6f3fd89c12d67f8ae0aa382c6766566b04378d647c57b00589c1`.
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


## First PR 10 feedback verification (06ffd1c)

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


## SDK connection-error follow-up after 36d4182

Cursor identified a real pinned-SDK mismatch in the transient-read classifier.
The prior name-based check missed SDK connection/timeout errors, whose name is
Error. Constructor-based regressions reproduced the failure against the prior
implementation. `instanceof APIConnectionError` now covers the actual timeout
subclass without treating aborts or a lookalike name as transient.

Final `npm run check`: strict typecheck and compiler hygiene passed, and **34
checks passed: 28 controller regressions, five actual workerd runtime behaviors
and one partial-source provenance record**, in 49.986 seconds. The full scenario
again recorded **201,053 application bytes**. The required offline Python suite
passed **425 total, 422 passed, three skipped**, in 28.925 seconds.

The updated packages were prepared with:

```sh
npm run package -- ct-sdk-review-10 ../../.local/managed-canary-sdk-package
```

All three dry-run bundle hashes are identical to those listed above. The new
source build digest is **927ba64511ee6f3fd89c12d67f8ae0aa382c6766566b04378d647c57b00589c1**,
reflecting the local CLI classifier change. Source changes after verification
are limited to documentation. The [re-review disposition](REVIEW.md#cursor-re-review-at-36d4182)
records that twelve prior threads were resolved by the reviewer, while the
original transient-read and new SDK-error threads remain open pending review.
No network request or credential use was needed to construct the real SDK error
instances; no managed API mutation or real provider call occurred.

## Credit approval and cleanup diagnostics

Base is the merged PR #10 commit
`69a9e9fbe44d5917ab4dc499dc496d919e4a800c`. The subsequent follow-up review at
`3654725` was reported clean, with every thread resolved and hosted CI green.
This change reconstructs the previously reviewed local credit mode in a fresh
workspace and adds cleanup diagnostics; the old ignored patch and canary receipts
were not restored. It is independently retested, rather than claimed to be the
same old patch bytes.

The default verified-headroom approval remains available. The explicit credit
mode records unverified balance, applicability and $1 headroom, accepted disclosed
DO-overage risk, and only the existing fixed synthetic scope. Both modes require
the exact current plan and preserve account, resources, ingress, deadline and
one-use claims. The CLI checks structured evidence; the trusted operator must
review the actual user's intent. It does not certify credits or interpret prose.

Fresh local validation on 2026-10-10:

- `npm run check`: strict typecheck and compiler hygiene passed; **44 checks
  passed**, comprising 38 controller regressions, five actual workerd runtime
  behaviors and one partial-source provenance record, in **52.463 seconds**.
- Credit-mode regressions exercise missing/mixed authorizations, changed scope,
  exact deadline and cross-mode replay refusal. Cleanup regressions exercise
  malformed/rejected/nonmatching responses, acknowledged-but-visible instances,
  actual SDK timeout errors with and without deletion, safe HTTP/code diagnostics,
  and retained failure history after a successful retry or a failed history write.
- The required `.venv/bin/python -m unittest discover -s tests -v` passed **425
  total, 422 passed, three skipped**, in **36.622 seconds**. No official loader
  source was enabled. No models or full datasets were downloaded.
- Wrangler 4.144.0 dry-run packages passed for both modes. The three bundle sizes
  and hashes are byte-identical to the table above. Only the local CLI source
  identity changes: build digest
  **97ba665b79ed8719e7b0123d978697def00a6b08d66877f9e48e33d2f613d32b**.
- `git diff --check` passed. No credentials were inspected, no Cloudflare or Modal
  authentication was performed, and no managed mutation or paid provider call
  occurred for this change.
- A fresh-context independent reviewer found no actionable correctness or
  security defects across all six changed files. Its separate controller run
  passed **38/38** tests, and the source digest matched this record.

Preparation commands (these create fresh local plans, not run approvals):

```sh
node controller.mjs package ct-approval-review ../../.local/managed-approval-review
node controller.mjs package ct-credit-review ../../.local/managed-credit-review user-confirmed-credits
```

### Previously reported run; raw evidence unavailable here

The prior execution record reported suite `ct-credit-20261010-1021-670f94` with
deadline `2026-10-10T10:51:12.940Z` and local patched build digest
`e314766081d2f75da48d8dc6c05db8bf971d7786d0765c7268631e862180a741`.
Authorization quoted the user's confirmation of credits for this purpose after
the prior disclosure that a $1 bound was unverified and a DO overage increment
could be $12.50. Balance, applicability and final charges were unverified.

The recorded result at `10:23:54.737Z` had all eleven supported gates passing,
six synthetic calls, 26 observations and five artifact receipts. Five calls
completed and one was cancelled. The overall command exited 1 because its first
cleanup reported eight instance failures; their response shapes and error
messages were not retained. A separate cleanup retry exited 0 at `10:25:15.110Z`.
An independent absence check at `10:27:26.592Z` reported all eight instances,
three definitions, two Workers and the owned namespace absent.

These facts are a previously reported outcome, not newly reproduced managed
evidence. The original raw output and first-failure/retry files cannot be audited
from this fresh workspace. The first failure's cause is unknown; later absence
does not prove eventual consistency, especially after definitions were deleted.
The new diagnostics preserve those distinctions on future authorized runs.

Host-process loss, managed scheduler loss and forced deadline expiry remain
untested. `realProviderReady` remains false. No Modal, Runpod, GPU or real-provider
work is established by this synthetic result. The provisional engine decision
and separate integration/budget approvals remain unchanged.
