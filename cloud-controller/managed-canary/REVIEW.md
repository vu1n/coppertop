# PR 10 review dispositions

Review: [Cursor review at c66172b](https://github.com/vu1n/coppertop/pull/10#issuecomment-6087271559).
All thirteen inline findings were assessed against the actual implementation and
pinned SDK. The following changes preserve the synthetic scope and the original
provisional engine decision. They authorize no managed run.

| Comment | Disposition and regression/evidence |
| --- | --- |
| 4233543523: reusable approval | Approval now binds the exact deadline and cleanup authorization. An exclusive permanent per-suite registry claim and exclusive execution receipt are consumed before API contact, including before preflight. Full cleanup, concurrent invocations, another output directory and re-planning cannot reuse the suite in this executor. Preparation preserves a consumed execution's original plan. Tested with complete execution/teardown/re-execution, real atomic file claims, races and directory/re-plan changes. This local barrier depends on retaining its journal; it is not account-wide across fresh workspaces. |
| 4233543511: unauditable verdict | Workflow output includes bounded raw payload, provider snapshot, ledger and native statuses. The CLI retains raw evidence before teardown and recomputes acceptance locally, storing both verdicts. Missing/contradictory raw data or altered gates fail; raw failed evidence survives cleanup. Actual workerd output passes the same audit; JSON field reordering is accepted without changing identity. |
| 4233543541: observer independence | Runbook now says the observer has no binding to target instances but shares the managed Workflows scheduler. DO expiry alarms provide containment independently of Workflow scheduling. Local recomputation improves auditability, not scheduler independence or recovery proof. |
| 4233543521: decision relevance | DECISION and the runbook state a managed pass supplies only no-restart native primitive and ledger controls, a prerequisite worth approving only on its own merits. It cannot satisfy the matching-process-loss criterion or change the provisional choice. |
| 4233543520: timing gates | Sleep requires at least 10 seconds between recorded observations, event timeout at least 3 seconds. Timeout/retry additionally requires a recorded delay start and a later retry at least 1 second later. The runtime regression rejects too-short timestamps and missing delay-start evidence. |
| 4233543512: pagination | Confirmed the pinned SDK advances pages and exhausts on empty results. Missing total-page metadata now uses this same empty-page rule. Validated total-page metadata is still accepted; malformed metadata, 403/network failures and an unexhausted ten-page cap fail closed. Mock tests cover empty-page exhaustion, retained ownership and metadata/ceiling failures. A short nonempty page is deliberately not treated as exhaustion. |
| 4233543522: transient status reads | Only status polling tolerates up to three consecutive connection, 429 or 5xx failures within the existing sample/deadline limits. The fourth fails; authentication denial and malformed status fail immediately. All mutations keep SDK retries disabled. Mock tests exercise recovery and failure for each class. |
| 4233543529: cleanup/spend coupling | Validated execution receipts retain explicit cleanup authorization and suite/build/deadline identity. Cleanup uses that receipt without the original spend approval, fresh headroom attestation or an unexpired deadline. It preserves current-source/config checks because the retirement migration is still a mutation. Informational billing state is named operator-attestation-required; the execution approval gate is operative. Atomic-file tests cover retained cleanup authority and mismatched receipts. |
| 4233543538: cancel wait race | Event/cancel waits now have 15-minute ceilings, exceeding the bounded controller readiness/delivery path including explicit activity retry ceilings, within the 30-minute suite deadline. A real workerd regression observes the cancel probe still waiting after the old 20-second ceiling, then verifies provider containment. Actual managed latency remains untested. |
| 4233543531: hung-promise mechanism | Replaced the never-resolving no-I/O Promise with a pending 5-second runtime timer inside a 1-second step timeout, after durable delay-start recording. The local supported probe passes; the runbook still leaves managed cancellation mechanism unverified and never calls it host loss. |
| 4233543547: overstated provenance | Renamed the hash-printing test as a partial-source provenance record with no runtime behavior. Counts now separate actual workerd behaviors from this record. Source build identity still covers the executable sources and locked graphs listed in controller.mjs; the record does not claim installed binding identity. |
| 4233543524: account-owner email | Removed the display name/email from the runbook; retained the nonsecret target account ID. No credentials or account settings changed. |
| 4233543519: non-forced deletion | Kept the documented SDK script path deliberately without force, so binding conflicts cannot implicitly delete DO state. Added explanatory code/runbook text and a 409 mock showing provider deletion is attempted only after explicit namespace removal/confirmation. This differs from Wrangler's services deletion path and remains a live API caveat. |

The review's qualitative claim that leftovers cost almost nothing is not adopted
as a billing guarantee. Subscription/allowance reads remain denied; cleanup loss,
rounding and account-specific billing still prevent asserting a $1 cap.

Managed deployment, instance deletion/termination semantics, namespace migration
propagation and Worker deletion permissions remain unverified. No deployment,
spending, merge, Modal call, credential change or security change was performed.
Host-process loss remains untested, and realProviderReady remains false.


The first CPU CI attempt on 06ffd1c exposed a preexisting local subprocess/tempdir
teardown race in the source-spool fixture, unrelated to managed-canary code.
The follow-up fixes test cleanup ordering only, reruns the seven affected tests
and the full Python suite, and preserves the production adapter and canary graph.
This is tracked in the [evidence](EVIDENCE.md#hosted-cpu-fixture-correction), rather
than hiding the failed attempt with repeated CI retries.
