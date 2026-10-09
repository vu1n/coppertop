# Lane B: upstream Effect Durable Object workflow engine

Tested on 2026-10-09 against PR 7322 at `aa48e3fac02d43bf7825d7005968586d26a79ae3`. `git ls-remote origin refs/pull/7322/head` returned the same commit immediately before the final runs. No upstream runtime source, manifests, lockfile, or patches were changed. Coppertop fixtures are copied into the ignored consumer-local upstream checkout by `run.sh`.

**Engine acceptance fails two required crash-recovery gates.** The engine persists completed activities and implements durable clock/deferred replay, but an interrupted initial activity has no persisted wake, and exhausted child-to-parent completion wakes strand the parent. Instability/unmerged status is not the reason for this result.

## Reproduction

```sh
bash cloud-controller/lane-b/setup.sh
bash cloud-controller/lane-b/run.sh --typecheck
bash cloud-controller/lane-b/run.sh
bash cloud-controller/lane-b/run.sh --strict --test-name-pattern='REGRESSION'
```

`run.sh` without `--strict` is a characterization suite: its two regression checks assert the observed **missing** recovery. It is not engine acceptance. `--strict` asserts the required autonomous behavior and fails with two failures on the tested commit. Additional Node test arguments, including `--test-name-pattern`, are forwarded. No target status poll or target request occurs during either post-restart regression observation window. Explicit `/resume` interventions occur only after the characterization gate has been recorded; these demonstrate that persisted work is replayable once contacted.

Captured final output is in `characterization-results.txt` and `runtime-results.txt`. The revised characterization includes future timer controls that assert zero completion through the independent observer immediately after restart before waiting for the alarm, plus the shared plain native emulator controls. Its measured duration is in the captured output.

`setup.sh` uses pnpm 11.20.0, the checked-in frozen lockfile and upstream patch configuration. In this executor both the full workspace install and the filtered install fail during global lockfile supply-chain metadata checks for inaccessible `https://npm.jsr.io` packages. The full attempt reported `ERR_PNPM_META_FETCH_FAIL` for `@jsr/std__async`; the filtered attempt reported it for `@jsr/db__redis`. Other inaccessible metadata included `@jsr/std__assert`, `std__bytes`, `std__collections`, `std__data-structures`, `std__fs`, `std__io`, `std__media-types`, `std__path`, `std__random`, and `std__streams`. No policy, registry routing, lockfile, or patch configuration was altered to bypass this blocker. The relevant npm-registry packages were downloaded, integrity-checked and linked from the existing frozen workspace resolution before the metadata failure. The runtime, build and targeted checks below used those pinned modules directly. These executor attempts are **not** successful installs. The original hosted CI subsequently completed a clean filtered frozen-lock install through `setup.sh`, followed by relevant builds and tests; see [the verification record](../REVIEW.md). This does not establish a full workspace install/check. The review revision repeats hosted checks on its published head.

## Exact tested dependency universe

| Component | Tested version/source |
| --- | --- |
| Node | 24.19.0 |
| pnpm used for installation | 11.20.0 via `npm exec --package=pnpm@11.20.0` |
| Effect | workspace 4.0.2 at the PR commit above |
| `@effect/platform-cloudflare` | workspace 4.0.0-rc.110 at the same commit |
| `@effect/sql-sqlite-do` | workspace 4.0.2 at the same commit |
| `@effect/vitest` | workspace 4.0.2 at the same commit |
| Vitest | 5.0.3; satisfies the workspace peer range |
| Cloudflare harness Miniflare | 4.20260730.0 |
| Harness workerd | 1.20260730.1 |
| Harness esbuild | 0.25.12 |
| Harness Worker types | 5.20260926.1 from the upstream lockfile |
| TypeScript | 7.0.2, patched using upstream `@effect/tsgo` 0.51.0 |
| Compatibility date | 2026-08-01, matching upstream harness |
| `pnpm-lock.yaml` SHA-256 | `ed4ea016659d410523c543cd2fd90f9ccb78e4745204c92788c3fe2291f9e445` |
| Changesets patch SHA-256 | `4b2756eb490a2ad802075423f6b08a5f50f12307ec25e117b20a79e12236b625` |

The upstream lock also contains other Miniflare versions for unrelated package harnesses; none is imported by this lane's runtime fixture. The shared provider **source** is bundled into a separate Miniflare 4 instance here. It does not import Lane A/shared package runtime dependencies or published stable Effect modules. Provider/observer SQLite persistence and runtime stay alive while the target workerd instance is restarted using `setOptions`, matching upstream's actual-runtime restart mechanism.

## Runtime results

| Gate/control | Outcome | Evidence |
| --- | --- | --- |
| Autonomous first-activity recovery | **FAILED** | First activity emits a durable external observer event, then blocks before its activity checkpoint. Pre-restart SQL has 0 activities/deferreds/clocks and alarm `null`. After workerd restart, observer entry count remains 1 for 2.5 seconds with no target contact. Explicit post-gate resume produces entry 2. |
| Lost child completion wake, bounded retries exhausted, then isolate loss | **FAILED** | Test-only RPC override rejects the original parent resume and five retries. Child is durably `Complete`; parent is durably `Suspended`, `resume_pending=0`, alarm `null`. After restart without injected loss, independent observer still sees 0 parent completions for 2.5 seconds. Explicit post-gate resume completes parent without rerunning child. |
| Concurrent duplicate execution and completed Activity replay | Passed | Concurrent same-ID calls share one Activity observer event; completed `v1` result survives restart with actual Worker module bytes changed to compiled `v2`. Harness asserts the two bundles differ. |
| Clock autonomous restart control | Passed | A persisted future clock/armed alarm completes and records to the independent observer after restart, without a target poll. An observer-zero assertion after restart rules out completion in the old isolate during asynchronous restart preparation. |
| Deferred-only legitimate suspension control | Passed | Restart leaves workflow suspended and produces no completion; legitimate external deferred completion wakes it. |
| Deterministic defect control | Passed | Defect event stays at one through restart; no defect hot loop is observed. This does not prove all defect policies. |
| Typed completion vs durable absolute deadline, with Activity replay | Passed | Schema-typed durable deferred completion wins one execution; clock deadline wins another after restart. Checkpointed announcement Activity runs once. Late completion cannot replace persisted deadline winner. |
| Application definition-version quarantine | Passed | A fixture guard fails suspended old-version work on replay after swapping actual compiled `v1` module bytes for `v2`, before executing additional work. This is an explicit application guard, not a built-in engine guarantee. |
| Cancellation/interruption replay | Passed | Public Workflow interruption is persisted as a completed failure; restart retains it; no normal completion event appears. |
| Duplicate/ambiguous external dispatch and body claim | Passed | Separate persisted provider creates one call and deliberately loses acknowledgement. Shared ledger consumes one-shot dispatch permission before the call. Explicit engine replay does not create another call; two competing body claims grant exactly one permission. |
| Cleanup/accounting/fencing | Passed | Unconfirmed cleanup retains the reservation and rejects settlement. Disabled generation plus stopped call permits settlement; settled state survives target restart. Provider rejects new launch after generation disable. |
| Atomic admission, immutable physical receipt, late dispatch after cancellation | Passed | Three concurrent admissions against capacity two admit exactly two. Physical artifact bytes are independently stored, hash/length checked and reread after restart; conflicting overwrite/receipt rejected. Already-in-flight launch arriving after cancellation remains owned, cannot obtain body permission, emits cleanup work, and is stopped/fenced before releasing reservation. |

The 2.5-second observation windows are bounded runtime evidence, not a proof over infinite time. The persisted lack of an alarm/pending wake and inspection of the upstream recovery paths establish why these executions have no guaranteed autonomous recovery. `workflowRuntime.ts` creates an initial execution without arming a wake; `runAlarm` only resumes pending deferred/clock recovery; `resumeParent` uses six in-memory RPC attempts without a persisted delivery record. These regressions deliberately distinguish initial/running work from legitimate deferred suspension and deterministic failure. No new scheduler, outbox delivery engine or speculative upstream fix was added.

## Cross-pin plain native control and provenance

`run.sh --raw-native` runs the same shared do/sleep/do control as Lane A, using only this workspace's Miniflare 4/workerd dependencies. Miniflare 4 needs **both** `workflowsPersist` and `durableObjectsPersist`; the initial probe caught this fixture omission before restart, it was corrected, and that setup failure is not a recovery result. The final probe proves completed first-step and active persisted sleep state before restart, requires the original ten-second sleep due time to remain in the future after restart, then uses only an independently persisted observer for at least eight seconds and through that due time plus three seconds. It checks both setOptions and dispose/recreate plus a positive no-restart control. Installed-source assertions characterize the same missing native timer scheduling at this pin, outside the upstream Effect engine. A successful negative characterization is not managed acceptance.

Final runtime inputs were tested at `1791e8667ebcc639533b3cb9d6c3b20a9f2cbd21`. Runtime logs identify the tested Coppertop commit/tree, clean or dirty selected inputs, source and lock/patch hashes, installed native binding hashes, actual command/mode and explicit installation provenance. Upstream commit is recorded separately; run.sh rejects a changed tracked upstream tree. Derived application bundles have hashes. Local retained modules after the original partial install are distinguished from hosted clean filtered installation. Logs are committed after the executable source commit; their recorded source hashes can be compared with the published tree and hosted rerun.

## Checks and limits

| Check | Outcome |
| --- | --- |
| Final characterization/controls command | 15 passed, 0 failed, 0 skipped; 157.07 seconds; **two engine acceptance gates remain failed** |
| Strict regression command | Only two selected REGRESSION tests: 0 passed, 2 failed, 0 skipped; 19.35 seconds |
| Upstream existing Workflow integration + CloudflareWorkflowEngine tests | 10/10 passed using workspace Vitest 5.0.3 |
| Relevant upstream Effect/SQLite-DO/Cloudflare TypeScript build | Passed, including forced rebuild after upstream tsgo compiler patch |
| Relevant upstream Babel build phases | Passed: Effect 500 files, SQLite DO 3 files, Cloudflare 29 files |
| Coppertop fixture strict typecheck | Passed; explicit `Decision`/unknown external reply types; no `any` |
| Targeted upstream oxlint | Passed |
| dprint formatting | Blocked: `https://plugins.dprint.dev/typescript-0.93.4.wasm` reports `Proxy failed to connect`; cache redirected to writable `.local` |
| Full workspace install/check/lint | Blocked/not run to completion; unrelated JSR metadata dependency access unresolved |
| Public upstream contribution, deployed Cloudflare recovery, real Modal integration | Not run; outside authorization |
| Wake loss before retry exhaustion, production schema migration, real provider economics | Not run |

Unminified target bundle: **1,633,472 bytes**, gzip **341,463 bytes** using the pinned esbuild and no minification. This includes the workflow/cluster client modules imported for the fixture's observation seam; it is not a production bundle size estimate. Fixture scope is approximately 500 source lines before documentation/scripts. The ledger is a test-only trusted-mutation safety adapter: it decodes the request before any state read and executes the pure decision and SQLite write without an intervening await. It is not an authenticated public admission API, provider gateway, automatic reconciliation service, or full application outbox dispatcher. The shared behavior is demonstrated through real durable storage and the independent provider, while remaining domain edge cases live in the shared suite.

## Recommendation

Keep this lane as an upstream contribution candidate and retain the two minimal strict regressions. At the next engine decision point, recheck the upstream head. A changed source or relevant recovery fix triggers rerunning both strict regressions and completed/clock/deferred/defect controls on the new coherent workspace. Reevaluate after initial execution and exhausted child wake delivery recover autonomously, while those controls remain valid. Do not select this tested commit as Coppertop's unattended durability engine based on successful status polling or replay alone. No public contribution is authorized yet. Real integration still requires the reviewed Python Modal launch/status/cancel boundary, reconciling unpublished Mac controller behavior/nonsecret state, and separate authentication/schema/migration review; GPU science and bulk transfers stay in Python.
