# Durability-engine decision

Keep Lane A (`effect-cf` with native Cloudflare Workflows) as the baseline for the next bounded investigation. Do not promote PR 7322 at the tested commit. **Neither lane meets unattended recovery acceptance in the tested local runtimes.** The draft is an evidence-producing spike, and it must not enable real-provider launches.

Lane A's atomic official-adapter ledger, one-shot dispatch/body claims, ambiguous launch handling, immutable artifact receipts and completed native history work. Its incomplete first step and suspended event wait fail to progress autonomously after a real workerd restart. Those same controls progress without restart. A target status intervention after the independent observation window did not restore progress. This identifies a local native-Workflow harness/runtime recovery boundary; it does not establish the behavior of Cloudflare's managed production scheduler.

Lane B has useful Effect-native Workflow/Activity/Clock/Deferred APIs and actual durable storage. Clock recovery and completed activity replay work without an always-on runner. The interrupted first Activity has no persisted clock/deferred/checkpoint or armed alarm. Exhausting the child's original parent notification plus five retries leaves a completed child and suspended parent without a pending wake or alarm. A subsequent explicit resume works. These are concrete correctness gaps at the exact upstream source, independent of its unstable/unmerged status.

## Tested pins

| | Lane A | Lane B |
| --- | --- | --- |
| Integration source | `effect-cf` 0.53.0, `b6390c618c796483bd32c99baf8a6411bba77ad2` | PR 7322, `aa48e3fac02d43bf7825d7005968586d26a79ae3` |
| Effect / SQLite adapter | Registry 4.0.2; Effect release `269a7c864351231d42e6e95b7fa8f32050df3691` | Same-commit workspace 4.0.2, with Cloudflare package 4.0.0-rc.110 |
| Test runner | Node 24.19.0 / `node:test` | Node 24.19.0 / `node:test`; upstream Vitest 5.0.3 |
| Miniflare / workerd | 5.20260926.1-alpha / 1.20260926.1 | 4.20260730.0 / 1.20260730.1 |
| Configured compatibility date | 2026-09-26 | 2026-08-01, upstream harness configuration |
| TypeScript | 5.9.3 | 7.0.2 plus upstream `@effect/tsgo` 0.51.0 patch |
| Dependency lock | Separate npm lockfiles; shared source has no Effect dependency | pnpm 11.20.0, exact upstream lock and patches |

Lane A uses a direct Miniflare harness and Node assertions. It does not install `@effect/vitest` or the Cloudflare Vitest plugin, so the upstream effect-cf Vitest-4 versus `@effect/vitest` Vitest-5 peer mismatch is absent from this tested graph. Lane B tests its coherent workspace Vitest 5.0.3 graph. Bun, Vite+ and Wrangler are not required by these local harnesses; their upstream harness versions are not claimed as tested dependencies.

## Acceptance evidence

“Unit” proves the shared pure decision only. “Runtime” means the actual Miniflare/workerd fixture executed against persisted SQLite. Characterization commands assert the two known missing-recovery observations per lane and may exit successfully; strict acceptance commands fail. See [Lane A evidence](lane-a/EVIDENCE.md) and [Lane B evidence](lane-b/EVIDENCE.md) for commands, observation intervals, native/engine diagnostics and actual counts.

| Gate | Shared / Lane A | Lane B |
| --- | --- | --- |
| Immutable spec, idempotency conflict, lifetime grant/hold | Unit + runtime passed | Shared unit + concurrent runtime admission passed |
| Last budget/concurrency slot | Unit + concurrent SQLite runtime passed | Concurrent SQLite runtime passed |
| Official adapter rollback, nesting, scheduler yield, SQL+alarm | Runtime passed | Relevant adapter build; equivalent full transaction regression not run |
| Dispatch/body reply loss, duplicates, uncertain liability | Unit + runtime passed | Unit + independent provider/SQLite runtime passed |
| Cancellation, late launch registration and generation fence | Unit + runtime passed | Unit + independent provider runtime passed |
| Physical immutable artifact bytes, hash/length, receipt CAS | Unit + runtime passed | Shared provider + runtime passed |
| Completed native step / Activity result replay | Runtime passed | Runtime passed |
| Incomplete first activity, autonomous restart | **Runtime failed** | **Runtime failed** |
| Native suspended callback/timeout wait, autonomous restart | **Runtime failed** | Clock autonomous and deferred controls passed; native wait is inapplicable |
| Exhausted child-to-parent wake after restart | Provider wake/fallback control passed; native child engine scenario not run | **Runtime failed** |
| Actual changed-code replay / incompatible definition | Runtime passed; incompatible epoch fenced | Runtime passed; application version guard quarantines |
| Cancellation/interruption and cleanup independence | Runtime containment controls passed | Interruption replay + manual synthetic cleanup controls passed |
| Cleanup unavailable, persisted retry authority | Runtime fault injection passed | Equivalent autonomous supervisor fault gate not run |
| Money immutability, negative cleanup evidence, quarantine | 8,000 generated unit history steps + focused regressions passed | Shared source; fixture-specific edge matrix not independently rerun |
| Epoch/schema restore safety | Unit + runtime fail-closed passed | Application version guard; full storage-restore scenario not run |
| Provider transport retries, real billing/queue/stop semantics | Not run | Not run |
| Auth/signature replay and real provider endpoint access | Not run; synthetic-only API | Not run; trusted fixture mutations |
| Alarm exhaustion/missing-alarm scheduled repair | Full exhaustion/cron gate not run | Engine gaps reproduced; application supervisor not implemented |
| Production eviction, deployment, real R2/Modal, paid canary | Not run; outside authorization | Not run; outside authorization |

Shared typecheck/compiler hygiene and 16 checks passed, including independently persisted provider/observer/artifact restart. Lane A's locked install, typecheck and compiler hygiene passed; its strict suite has **14 passed, 2 failed**, and its explicit negative characterization has **16 passed**. Each native suite includes 15 workerd behavioral checks and one bundle metric. Lane B's characterization has **11 passed**; strict recovery has **2 failed**. Existing upstream tests passed **10/10**. The existing Python suite completed **425 tests with 3 skipped**; no Python runtime code changed. The bounded official-loader integration was not enabled because no `COPPERTOP_SOURCE` was supplied. No models or datasets were downloaded.

## Build and verification limits

The full and filtered pnpm workspace installs fail at `npm.jsr.io` during global lockfile metadata verification (`@jsr/std__async`, `@jsr/db__redis`, and related JSR packages). Relevant integrity-checked locked modules were available for the successful upstream package builds, targeted tests and actual-runtime probes. This is not a completed cold install. Upstream dprint formatting also fails to retrieve `typescript-0.93.4.wasm` from `plugins.dprint.dev`. These hosts/policies were not bypassed.

The approved Library plan was text-readable but both supported local materialization attempts failed with `library file transfer failed: download failed`. `gh`'s GraphQL privacy query was denied; the authorized GitHub app separately confirmed repository visibility `private`. Source commits were verified with Git transport. No approval-review rejection occurred.

Performance observations are fixture measurements. Lane A's unminified bundle is 409,200 bytes. Lane B's unminified bundle is 1,633,472 bytes, gzip 341,463 after strengthening its timer controls. Each lane records its own bundle/timing measurements, including observation/intervention windows. These workloads differ in application coverage, so source-line or bundle size alone does not establish a maintenance or cost win. Production CPU, storage retention, request/step bills and real wake counts remain unmeasured. No cloud price or savings claim is made.

## Next step and integration prerequisites

First isolate the Lane A native Workflow local restart behavior against the exact pinned Cloudflare harness, retaining independent observer controls. Preserve Lane B's narrow initial-Activity and child-wake regressions as a potential upstream contribution. No public issue, message, fork or PR is authorized. Reconsider B after a bounded upstream-supported recovery change passes these controls; do not construct another durability engine or add a permanent wake poller.

Real integration still needs reviewed unpublished Mac controller code and nonsecret migration state; a small authenticated Python Modal launch/status/cancel gateway; immutable provider deployment identity; independent stop/containment authority; supported client invocation; scoped access; native spend limits; reviewed grants, pricing and resource ownership; R2 archive/receipt preservation; and separately approved staging/deployment/canary tests. Manual epoch recovery and reconciling additional late costs after settlement remain explicit unsupported operations. The sidecar does not enter the robot action loop.
