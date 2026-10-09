# Durability-engine decision

Prefer Lane A (`effect-cf` with native Cloudflare Workflows) provisionally, because its application safety boundaries work and a local native restart limitation is reproduced without effect-cf. **Managed unattended recovery remains unproven. Lane B fails two required recovery gates in its own implementation at the tested commit.** Neither lane is approved for real-provider integration. This branch produces evidence; it cannot enable real-provider launches.

Lane A's atomic official-adapter ledger, one-shot dispatch/body claims, ambiguous launch handling, immutable artifact receipts and completed native history work. Its incomplete first step and suspended event wait stall after a real workerd restart in Miniflare. The matching no-restart controls complete; a subsequent status intervention does not restore progress. The plain native control below reproduces a timer-only stall without Effect or effect-cf under both lanes' emulator pins. This establishes a local harness limitation, not evidence that managed Cloudflare recovery fails or that the original two fixtures have no additional cause. Completed-history replay is a positive restart control, but does not prove autonomous recovery of in-flight work.

Lane B has useful Effect-native Workflow/Activity/Clock/Deferred APIs and actual durable storage. Clock recovery and completed activity replay work without an always-on runner. The interrupted first Activity has no persisted clock/deferred/checkpoint or armed alarm. Exhausting the child's original parent notification plus five retries leaves a completed child and suspended parent without a pending wake or alarm. A subsequent explicit resume works. These are concrete correctness gaps at the exact upstream source, independent of its unstable/unmerged status.

Here, a dispatch claim consumes permission to call the provider once; a body claim consumes permission to execute the workload once. A receipt CAS is an immutable conditional write that rejects a conflicting receipt. Native history means persisted Cloudflare step outputs and status, rather than a fresh run of the workflow body.

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
| Incomplete first activity, autonomous restart | **Local strict gate failed; managed behavior unproven** | **Engine gate failed** |
| Suspended callback/timeout restart | **Local native wait strict gate failed; managed behavior unproven** | Analogous autonomous clock/deadline and legitimate deferred controls passed; a native wait API is not used |
| Exhausted child-to-parent wake after restart | Native child engine scenario not run | **Engine gate failed** |
| Lost provider wake/status fallback without restart | Runtime passed; no restart in this control | Shared provider behavior passed; equivalent automatic supervisor fallback not run |
| Plain native do/sleep/do, both pins and restart methods | Local emulator characterization; see controls below | Same plain control using this lane's own Miniflare/workerd universe |
| Actual changed-code replay / incompatible definition | Runtime passed; incompatible epoch fenced | Runtime passed; application version guard quarantines |
| Cancellation/interruption and cleanup independence | Runtime containment controls passed | Interruption replay + manual synthetic cleanup controls passed |
| Cleanup unavailable, persisted retry authority | Runtime fault injection passed | Equivalent autonomous supervisor fault gate not run |
| Money immutability, negative cleanup evidence, quarantine | 8,000 generated unit history steps + focused regressions passed | Shared source; fixture-specific edge matrix not independently rerun |
| Epoch/schema restore safety | Unit + runtime fail-closed passed | Application version guard; full storage-restore scenario not run |
| Provider transport retries, real billing/queue/stop semantics | Not run | Not run |
| Auth/signature replay and real provider endpoint access | Not run; synthetic-only API | Not run; trusted fixture mutations |
| Alarm exhaustion/missing-alarm scheduled repair | Full exhaustion/cron gate not run | Engine gaps reproduced; application supervisor not implemented |
| Production eviction, deployment, real R2/Modal, paid canary | Not run; outside authorization | Not run; outside authorization |

Counts and commands for the review revision are recorded in each lane's evidence file and captured outputs. Counts are not a relative reliability score: Lane A runs its complete strict suite, while Lane B's strict command selects only the two named recovery regressions. Green CI characterizes known failures; it never turns either lane's strict gate into a pass. Shared tests also exercise independently persisted provider/observer/artifact restart. Python runtime code is unchanged; the bounded official-loader integration remains disabled because no `COPPERTOP_SOURCE` was supplied.

| Recovery observation | Lane A | Lane B |
| --- | --- | --- |
| Target isolation | Only independent provider/observer contacted during gate | Only independent provider/observer contacted during gate |
| Required gate window | 25 seconds after restart, then separate 12-second diagnostic status intervention | 2.5 seconds after restart, then separate explicit resume intervention |
| Supporting evidence | Plain native control and pinned emulator source isolate the local timer limitation | Persisted absent wake/alarm plus inspected initial-execution and parent-delivery paths |
| Interpretation | Finite local stall; managed scheduler cannot be evaluated here | Finite observed stall backed by a concrete lack of durable recovery ownership |

The shared plain native program executes `step.do` → `step.sleep(3 seconds)` → `step.do`. Each lane injects its own dependencies and persistence options. Before either restart, read-only inspection of only fixture-generated SQLite proves the first step succeeded, sleep started, an active sleep entry was persisted and sleep had not completed. Both `dispose/recreate` and `setOptions` require zero immediate completion and observe the independent observer for at least eight seconds without contacting the target. The no-restart control must complete. These controls characterize the pinned emulators; they are not managed acceptance tests.

In both inspected native bindings, `TimePriorityQueue.handleNextAlarm()` only peeks at the heap and the engine has no Durable Object alarm handler. Native status reads metadata/logs rather than re-entering the program. Event delivery **can** cold-initialize execution; there is no claim that every native program stalls or that create/restart/resume are the only entry paths. The source hashes in the control output identify the exact inspected installed implementations.

## Build and verification limits

Original executor attempts at full and filtered pnpm installation failed during JSR metadata verification. The original hosted CI subsequently completed a clean **filtered frozen-lock install** with pnpm 11.20.0, followed by relevant builds and tests. A full workspace cold install/check was not run to completion. dprint plugin retrieval remains blocked in the executor. See [review verification record](REVIEW.md) for operational failures and hosted run links; no policy or registry routing was bypassed.

Each revised runtime log records the tested Coppertop commit/tree, dirty input status, source/lock/installed binding SHA-256 values, command and explicitly supplied installation provenance. Derived worker bundles also have hashes. Logs are committed after execution; the recorded source commit identifies their executable inputs, not the later evidence-only commit. Unrecorded historical installation flags must not be inferred from a reproduction command.

Performance observations are fixture measurements. Lane A's unminified bundle is 409,200 bytes. Lane B's unminified bundle is 1,633,472 bytes, gzip 341,463 after strengthening its timer controls. Each lane records its own bundle/timing measurements, including observation/intervention windows. These workloads differ in application coverage, so source-line or bundle size alone does not establish a maintenance or cost win. Production CPU, storage retention, request/step bills and real wake counts remain unmeasured. No cloud price or savings claim is made.

## Next step and integration prerequisites

The plain native control closes the local attribution investigation for these pins. Keep strict regressions as tripwires; do not repeatedly tune the same harness or add a permanent wake poller. A change to either installed native binding triggers rerunning the raw no-restart/restart controls and Lane A's strict gates. If the local runtime gains supported autonomous recovery, require both strict gates plus the existing safety/replay controls to pass before revisiting acceptance.

The next decision is whether to authorize a separate bounded managed-runtime test or explicitly accept reliance on Cloudflare's documented durability contract. A future test needs an approved account/deployment, a supported fault-injection mechanism whose semantics match the intended crash, an independent observer, a fixed spend/time bound and defined cleanup. The exposed Workflow lifecycle APIs do not establish a host-process-kill mechanism. `restart`, `terminate`, throwing or redeployment must not silently substitute for process loss. A documentation-based decision is an explicit assumption, not an observed pass. No zero-cost guarantee or deployment is made in this spike.

At the next engine decision point, recheck PR 7322's head. A changed upstream source or a relevant recovery fix triggers rerunning the two strict regressions and completed/clock/deferred/defect controls on a coherent new workspace. Select B only after durable initial-execution and exhausted-parent-wake recovery pass without target contact. Until then retain the narrow regressions as a potential contribution; no public issue, message, fork or PR is authorized, and no new durability engine is proposed.

Real integration still needs reviewed unpublished Mac controller code and nonsecret migration state; a small authenticated Python Modal launch/status/cancel gateway; immutable provider deployment identity; independent stop/containment authority; supported client invocation; scoped access; native spend limits; reviewed grants, pricing and resource ownership; R2 archive/receipt preservation; and separately approved staging/deployment/canary tests. Manual epoch recovery and reconciling additional late costs after settlement remain explicit unsupported operations. The sidecar does not enter the robot action loop.
