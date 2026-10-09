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
