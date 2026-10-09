# Spike review record

Review scope: the additive cloud-controller branch against `a62a523e0ff16dd29415fb275e628fb910a76cc1`. The invoked local `$ship-it` workflow was limited by the user's authorization to a private draft PR. Python runtime, deployment and upstream public communication remain outside this change.

## Correctness and structure

Independent static reviews covered the shared transition/provider, both engine adapters, runtime evidence controls, documentation and CI. Confirmed findings were fixed with focused behavioral checks:

- Shared cleanup replay preserves settled accounting; completed containment retires permissions; contradictory late cleanup/call evidence quarantines admission.
- Opaque object-property IDs use data dictionaries, compound outbox/observer identities cannot collide, and exact idempotency survives a deadline boundary.
- Lane B timer controls require zero independent completion after restart before awaiting a future alarm, excluding completion in the old isolate.
- Lane A preserves the earliest safety alarm and gives each synthetic deferred launch its own durable not-before time. Native instance IDs have a valid fixed prefix. The suspended-restart control establishes native `wait_started` before stopping workerd.
- Native cleanup distinguishes actual instance creation from retired launch intent. After disabling a provider generation, it enumerates and owns every observed call before stopping them and accepting terminal proof; a call injected between the initial lookup and fence is covered by a runtime regression.

Cleanup retained explicit schemas, guards and engine boundaries. No broader structural rewrite was justified. The shared domain remains engine-neutral; each lane keeps its own dependency graph. The final evidence matrices distinguish strict failures from successful negative characterizations.

## Static security review

Trust boundary: undeployed local synthetic fixtures, with test-controlled requests, diagnostics and fake-provider bindings. Production authentication and malicious real-provider inputs require a separate integration review.

Finder candidates were rederived by fresh-context refuter agents from source coordinates and callers:

| Candidate | Resolution |
| --- | --- |
| Lane B synthetic ledger/deferred/diagnostic routes | Dropped: fixture-only caller and integration-test worker; exclusion 2 |
| Independent observer retention | Dropped: trusted fixture events and ordinary volumetric growth; exclusions 2 and 1 |
| Lane A synthetic mutations/corruption routes | Dropped: explicit local-only binding and test callers; exclusion 2 |
| Lane A operator-selected barrier delay | Dropped: trusted fixture control; exclusions 2 and 7 |
| Lane A synthetic provider reply cardinality | Dropped: trusted independent fixture binding; exclusions 2 and 12 |

Five candidates reviewed, zero confirmed, five dropped, zero unresolved within this boundary. Reviewers did not execute the target or access network services. These conclusions do not establish production authentication, hostile-provider safety or deployment readiness.

## Verification limits

See [engine decision](DECISION.md), [native evidence](lane-a/EVIDENCE.md) and [upstream evidence](lane-b/EVIDENCE.md) for actual test results and pins. Required Python tests were run without models or datasets.

The original executor's full and filtered pnpm attempts failed global JSR metadata verification (`ERR_PNPM_META_FETCH_FAIL`, including `@jsr/std__async` and `@jsr/db__redis`). Relevant locked modules were linked before failure. Hosted CI subsequently completed a clean filtered frozen pnpm 11.20.0 install and relevant builds/tests: [cloud characterization run 37927551787](https://github.com/vu1n/coppertop/actions/runs/37927551787). A full workspace cold install/check was not completed. Executor dprint retrieval of `https://plugins.dprint.dev/typescript-0.93.4.wasm` failed with `Proxy failed to connect`. No host restriction, registry routing, lockfile or patch was bypassed.

The approved Library file's complete text was read through the supported Library API. Both resolved-reference local materialization attempts, including an explicitly consumer-local destination, failed with `library file transfer failed: download failed`. This executor has no materialized plan copy. `gh`'s GraphQL privacy read failed with `Post "https://api.github.com/graphql": Forbidden`; the authorized GitHub app confirmed private visibility. No approval-review rejection occurred. Source pins were verified through Git transport.

The original [CPU run 37927551721](https://github.com/vu1n/coppertop/actions/runs/37927551721) passed 425 tests with 3 skipped. No official source, models or datasets were downloaded. Revised runtime logs record executable source identity and actual installation provenance; reproduction examples cannot establish unrecorded historical flags. The later log-only commit is distinct from the tested executable-input commit.

## PR 9 review dispositions

All ten inline threads and the overall review were read against source. No public upstream communication or deployment was performed.

| Review comment | Disposition |
| --- | --- |
| 4230063336: analogous B controls / raw native control | B clock/deadline and deferred controls now read as passed. One shared plain native program is tested with each lane's isolated pin and both restart methods. |
| 4230063339: provisional A rationale / branch wording | A is provisional because a local native stall is reproduced outside effect-cf; managed acceptance remains unproven. B's source defects are separate. DECISION uses branch for implementation scope. |
| 4230063340: provider fallback under restart row | No-restart provider fallback has its own row. A native child-workflow gate remains not run. |
| 4230063354: operational notes in decision | Library/gh details moved here; engine decision retains only build limits relevant to comparison. |
| 4230063341: unequal strict scopes/windows | Decision table gives 25s+12s diagnostic versus 2.5s+resume, and explains full A strict versus two selected B regressions. Counts do not rank reliability. |
| 4230063343: bounded next step / decision triggers | Local attribution ends at these pins. Pin changes or relevant upstream fixes trigger named reruns. Managed testing requires separate authorization and a supported matching fault mechanism. |
| 4230063345: stale install claim | Successful hosted filtered frozen install is distinct from failed executor attempts and uncompleted full workspace checking. |
| 4230063346: local emulator vs managed engine | A is unproven in managed runtime. Source and raw controls establish only the local timer limitation. |
| 4230063350: failed local gate vs production acceptance | Both are explicit: two strict local failures remain, managed behavior not evaluated. |
| 4230063361: cross-pin fairness / provenance | Raw controls use identical shared source with pin-specific dependencies. Logs record commit/tree, input hashes, installed binding hashes and actual command/install flags. |

The overall review's claim that cold native execution enters only through create/restart/resume is too broad: `receiveEvent` also calls `init()` in both installed bindings. The statement that every native program stalls is therefore not adopted. Its proposed managed host-process kill has no established mechanism in the pinned Workflow API; lifecycle restart/terminate or redeployment changes fault semantics. Zero billed usage is not guaranteed. These are remaining design decisions for separately authorized integration, not omitted actions within this synthetic spike.

The initial three-second raw sleep control caught an invalid fault window in a later run at each pin: completion was already visible immediately after async setOptions preparation. Those runs cannot establish loss before timer completion and are not accepted as recovery evidence. The revised ten-second control requires more than eight seconds remaining before restart, a future original due time after restart, zero immediate completion, and observer-only coverage through that due time plus three seconds. Assertions were strengthened; no engine code or scheduler was changed. The filtered upstream strict command also excludes an unmatched raw-test module that Node otherwise counts as a passing file.

A final source audit verified that identical-options `setOptions()` unconditionally updates configuration and restarts workerd at both pins. `Runtime.updateConfig()` awaits disposal, then spawns a new process; disposal sends SIGKILL and awaits its exit. Installed Miniflare 4 `dist/src/index.js` coordinates: setOptions 104551, updateConfig 83650, dispose 83744. Miniflare 5: setOptions 124733, updateConfig 107169, dispose 107253. This is controlled process-loss evidence, not a no-op or merely an in-process reload. Arbitrary external crashes, power loss and managed host eviction remain untested. No new PID instrumentation or fault mechanism was added.

Final local verification on executable inputs at `1791e8667ebcc639533b3cb9d6c3b20a9f2cbd21`: shared 16/16; native strict 18 passed/2 failed, characterization 20/20; upstream characterization 15/15 and selected strict regressions 0 passed/2 failed. All runtime provenance records were clean and their eight source/hash records match the committed executable inputs. Shared/native typecheck and compiler hygiene, upstream fixture typecheck/oxlint, relevant upstream TypeScript and Babel builds (500/3/29 files), and existing upstream Vitest 10/10 passed. The unchanged Python suite completed 425 tests with three skipped in 48.128 seconds. No unrelated files or Python runtime changes were made. Evidence-only changes after that source commit do not alter runtime inputs; hosted CI verifies the final published head.

## Follow-up review at b3b27e7

Cursor independently reproduced all recorded counts, bundle/binding hashes and clean provenance, and confirmed the substantive findings were addressed. The remaining documentation findings were applied without changing runtime sources:

| Comment | Disposition |
| --- | --- |
| 4230665134: non-discriminating preference | The opening now states the distinction: B has confirmed engine recovery defects; A delegates to an unverified managed scheduler. The raw timer control does not claim to reproduce the incomplete first activity. |
| 4230665137: negative stalls included in pass counts | Each count row separates application/engine checks from raw controls and explicitly says that restart stalls pass as negative characterizations, including in A strict. |
| 4230665124: dense next-step paragraph | Decision, prerequisites and pass/stall criteria are separate bullets, due before any real-provider integration. The bundle metric no longer recounts implementation history. |

The managed API caveat was checked against the pinned `WorkflowInstance` declaration (`workers-types/index.d.ts:18312`) and the [official Workers API](https://developers.cloudflare.com/workflows/build/workers-api/): its listed lifecycle methods do not expose host-process termination, and restart changes replay state. This establishes the inspected API surface, not the absence of every possible managed fault-injection mechanism. No deployment or account action was performed.

Runtime sources, locks and captured results remain identical to b3b27e7. The documentation diff was checked for whitespace and local-link targets; all recorded executable-input hashes were reverified. Automatic hosted checks run on each new published head; their verified results are reported in the PR. No additional local runtime rerun is claimed for this wording-only change.
