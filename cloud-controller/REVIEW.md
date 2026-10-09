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

See [engine decision](DECISION.md), [native evidence](lane-a/EVIDENCE.md) and [upstream evidence](lane-b/EVIDENCE.md) for actual test results and pins. Required Python tests were run without models or datasets. Full upstream cold installation and dprint formatting remain blocked by the documented external fetch failures. Library text was readable; supported local materialization failed twice. Neither lane's unattended recovery acceptance passed.
