# Managed native canary preparation

This package prepares a bounded Cloudflare Workflows canary on the provisional
Lane A baseline (`effect-cf` 0.53.0 / Effect 4.0.2). It reuses the shared admission
and accounting transition and Lane A's exact persisted-data codecs. The local
fixtures from the initial spike are not deployed. Python, Modal, real providers,
GPU work, R2 and the robot action loop are outside this package.

**Every new deployment and run needs its own scope-bound approval. A $1 billing
bound remains unverified.** A prior authorized synthetic run was reported as
passing its supported controls and later cleanup; its raw local receipts were
not restored in this workspace. See [evidence and limitations](EVIDENCE.md#credit-approval-and-cleanup-diagnostics).

## Fixed resources and private control path

Default preparation uses account `a06ff95ff66e0f58f269da6d2fb99021`, previously
returned by Cloudflare. This identifier is not a credential.
`CLOUDFLARE_API_TOKEN` is used only through normal client
authentication when an approved execution begins. No credential is sent to a
Worker, logged, written to a plan or copied into a receipt. Modal variables,
including backup credentials, are not read.

| Resource | Default preparation name / bound |
| --- | --- |
| Worker defining probes and the controller | `ct-native-canary-01-control` |
| Independent synthetic provider and observer Worker | `ct-native-canary-01-provider` |
| Workflow definitions | `ct-native-canary-01-suite`, `ct-native-canary-01-probes`, `ct-native-canary-01-observe` |
| Workflow instances | One controller, six probes, one independent observer: **eight total** |
| SQLite Durable Object namespace | One `CanaryStore` class in the provider Worker |
| Application objects | Fixed names `ledger`, `provider`, `observer`: **three total** |
| Synthetic application data | Under 1 MiB; physical receipts are bounded, and observations have count/size caps |
| Execution window | Absolute deadline 30 minutes after preparation; no cron, daemon or automatic suite retry |
| CPU configuration | 1,000 ms on each Worker; all workflow activity retry/timeout limits explicit |

Both Workers disable `workers.dev`, preview URLs, routes and observability.
Default HTTP entrypoints reject requests. The controller uses Cloudflare's
[authenticated Workflow API](https://developers.cloudflare.com/workflows/build/trigger-workflows/).
Internal calls use the named `SyntheticProvider` service entrypoint. There is
no public endpoint, Access application, new credential or credential-upload step.
The observer is a separate Workflow with no binding to target instances. During
its observation window it reads only provider storage. It shares the managed
Workflows scheduler and does not provide scheduler-independent evidence. Deadline
containment uses Durable Object alarms, which do not depend on Workflow scheduling.
Target status is read for setup/duplicate diagnostics, event/cancellation
operations and final validation; those contacts do not establish autonomous
recovery.

The SQLite ledger uses the official Effect adapter's transaction boundary for
admission, holds and one-shot dispatch/body claims. A provider launch can commit
and return an injected 503; reconciliation finds the call without dispatching
again. Concurrent repeated claims cannot mint another permission. Fixed run
specifications contain the source build digest and deadline. Restore/deployment
identity mismatches fail closed. Hash and byte-length checks read physical
synthetic artifact bytes before immutable receipt publication. Settlement needs
independently confirmed generation fencing and terminal calls. Synthetic cost
units in that ledger are **not Cloudflare dollars**.

## Acceptance and limits

| Managed probe / check | Required evidence | Present status |
| --- | --- | --- |
| Immediate control | One provider call/body, valid physical receipt, closed accounting, native terminal status | Local control passed; managed not run |
| First activity timeout and retry | First step durably records a timer-delay start and a later retry at least one second later, then completes within configured retry limits | Local control passed; managed not run |
| Durable sleep | Before/after native sleep observations separated by at least 10 seconds | Local control passed; managed not run |
| Durable event | Independently observed wait, supported event delivery, matching payload and completion | Local control passed; managed not run |
| Event timeout | No event delivery; native wait timeout followed by completion, at least three seconds after observed wait-start | Local control passed; managed not run |
| Cancellation | Provider fence and terminal proof independent of target; target terminated, no completion receipt | Local control passed; managed not run |
| Duplicate launch / claims | Exact native ID and repeated dispatch/body permission do not create additional work | Local controls passed; managed not run |
| Deadline containment | Separate provider/ledger alarms contain calls without a target workflow contact | Local control passed; managed not run |
| Temporary-resource deletion | API confirms all eight instance IDs, three definitions, namespace and both Worker names absent | Transport mock passed; managed not run |
| Host-process loss / eviction | Supported fault matching host loss, with independent unattended recovery evidence | **Untested; no supported injection mechanism established** |

A supported activity timeout, Effect interruption, lifecycle `restart`,
`terminate`, redeployment and an ordinary native sleep are different faults.
None is presented as host-process loss. The currently documented
[Workflow lifecycle API](https://developers.cloudflare.com/workflows/build/workers-api/)
exposes no host-process kill. The pinned API surface was inspected as well; this
is not a claim that Cloudflare can never offer another injection mechanism.
`restart` changes replay state. The timeout probe awaits a five-second runtime
timer after durably recording its
start, inside a one-second step timeout. Pending timed I/O avoids the no-I/O
hung-promise detector; a later retry must be recorded. This is a local timeout
control, not proof of the managed cancellation mechanism or host loss. Event and
cancel waits have 15-minute ceilings, above the controller readiness/delivery
path including explicit retries, and remain within the absolute suite deadline.

The controller returns bounded raw provider observations/calls/artifacts, the
ledger and native terminal statuses. The CLI saves them before teardown and
recomputes every supported gate locally, comparing it with the reported verdict.
Both the reported and local verdicts remain in the receipt; contradictory evidence
fails closed and is retained for audit. These observations still originate inside
Cloudflare and do not prove scheduler-independent recovery.

Every report keeps `hostProcessLoss: "untested"` and `realProviderReady: false`,
even when all supported gates pass. Such a pass cannot close the original
unattended host-loss recovery gate or authorize real-provider integration. A
managed pass establishes only the no-restart primitive and ledger controls: a
prerequisite control result, not recovery evidence. It does not satisfy the
managed-test criterion or change the provisional engine choice. Approve that run
only if the control result is worth the approval on its own. A separate decision
may explicitly rely on the documented managed durability
contract as an unverified assumption, or wait for a supported matching fault.

## Local verification and packaging

Use Node 24.19.0. Install the exact Lane A graph because this package imports its
persisted codecs; the shared domain remains independent of Effect:

```sh
npm ci --prefix cloud-controller/lane-a --ignore-scripts --cache .local/npm-cache
npm ci --prefix cloud-controller/managed-canary --ignore-scripts --cache .local/npm-cache
cd cloud-controller/managed-canary
npm run typecheck
npm run lint
npm test
npm run package -- ct-native-canary-01 ../../.local/managed-canary-package
```

`plan` and `package` are local operations. Packaging runs pinned Wrangler
4.144.0 with `deploy --dry-run` for both active bundles and the deletion migration.
It uploads nothing and creates no cloud resources. Generated configuration,
plans, bundles and logs go under ignored `.local/`. The local test driver has
additional bindings solely for the harness and is never packaged for deployment.
The actual managed Worker entrypoints, native steps, SQLite transactions and
named service binding execute in the pinned Miniflare/workerd harness. No
restart is injected into these supported-control tests.

Tests include 28 controller safety regressions, five actual workerd runtime
behaviors and one partial-source provenance record (not a runtime test). They
cover denial/approval boundaries, exact ownership, existing-name conflicts,
403/network failures, lost launch/deletion replies, partial deployment cleanup,
namespace cleanup denial, unsupported pagination, evidence failures and the
finite runner polling budget. Green mocked teardown is not managed deletion
proof. The separate initial spike's strict local restart gates remain unchanged.

## Billing gate

Authentication is verified, and Workers settings returned usage model
`standard`. Neither result verifies subscription tier or remaining allowance.
Subscription and Container quota reads returned Cloudflare 403/code 10000.
Credentials will not be broadened and network denials will not be bypassed.

[Durable Object pricing](https://developers.cloudflare.com/durable-objects/platform/pricing/)
rounds excess compute usage up to a billable unit. A paid-account duration
overage can therefore add **$12.50**, already above $1. Free accounts reject
operations past their free limits. Storage writes, requests and existing-account
usage must also be considered. [Workflow pricing](https://developers.cloudflare.com/workflows/reference/pricing/)
includes CPU, requests, steps and retained state; waiting does not consume CPU.
No subscription change or new monthly charge is permitted here.

The proposed $1 operational cap requires confirmed free-plan behavior or
sufficient existing included allowance across every applicable meter, with no
unapproved subscription change. A conservative allowance check should reserve
at least 1,000 DO GB-seconds, 8,192 requests, 20,000 SQL writes, 100,000 SQL reads,
512 Workflow steps, 10 million CPU-ms, and 1 MiB of application storage including
cleanup slack. Platform metadata/storage overhead is not measured by the
application-byte assertion. These are conservative planning reserves, not fresh
billing observations or native invoice caps. If allowance, cleanup permissions,
or the account's billing behavior cannot be verified, **do not execute under a
$1 approval**. No native account-wide spending setting is changed.

## Approved execution and cleanup protocol

After the parent obtains the separate approval under one of the modes below,
refresh the package using a unique `ct-...` suite name. Preparation fixes both
resource names and an absolute deadline; do not reuse an expired plan or another
suite's resources. Preserve the exact tested revision and its build digest.

Both modes require an approval naming the exact suite, build digest and plan
`deadline`, with `approved: true`, `cleanupAuthorized: true`, and
`subscriptionChangeAuthorized: false`. The complete plan must match the fixed
resource, ingress, duration and synthetic-only scope before a claim or API call.
An implementation request or a prior suite's approval does not authorize a run.
No executable approval file is supplied in this PR.

The default `verified-headroom` mode retains `incrementalDollarCap: 1` and
`billingHeadroomVerified: true`. `billingMode` may be omitted for this mode for
compatibility. The operator must verify the allowance described above; the CLI
cannot independently establish billing headroom.

An explicitly selected `user-confirmed-credits` mode records a different
authorization: the user confirms credits for this fixed synthetic run after
disclosure that the $1 bound is unverified and DO overage can add a $12.50
increment. It requires `billingMode: "user-confirmed-credits"`,
`incrementalDollarCap: null`, `billingHeadroomVerified: false`, and this separate
`creditAuthorization` object (replace evidence placeholders with the exact
reviewed messages):

```json
{
  "spendingScope": "this-bounded-synthetic-run-only",
  "userConfirmedCreditsForPurpose": true,
  "dollarOneCapUnverified": true,
  "possibleDoOverageIncrementDollars": 12.5,
  "overageRiskAccepted": true,
  "creditBalanceVerified": false,
  "creditApplicabilityVerified": false,
  "evidence": {
    "source": "user-message",
    "userMessage": {
      "messageId": "REPLACE_WITH_MESSAGE_ID",
      "at": "REPLACE_WITH_UTC_ISO_TIMESTAMP",
      "text": "REPLACE_WITH_EXACT_USER_AUTHORIZATION"
    },
    "priorDisclosure": {
      "at": "REPLACE_WITH_UTC_ISO_TIMESTAMP",
      "text": "REPLACE_WITH_EXACT_PRIOR_RISK_DISCLOSURE"
    }
  }
}
```

Evidence must be nonblank bounded strings and valid UTC timestamps with the
disclosure no later than the user message, and neither in the future. The trusted
operator must review the actual message and risk acceptance; structural checks do
not interpret natural language or prove a credit balance or applicability.
`null` records the lack of a verified monetary ceiling. It grants only the exact
bounded synthetic run; it grants no additional launches, provider jobs,
subscriptions or unlimited resource use. The two modes cannot be mixed.

Select the mode during preparation, before requesting the exact approval:

```sh
node controller.mjs package ct-UNIQUE ../../.local/managed-canary-package user-confirmed-credits
```

Before any API contact, execution atomically consumes a per-suite claim under
`.local/managed-canary-executions/` and exclusively creates `execution.json`.
Concurrent invocation, another output directory, a completed teardown or a
re-planned deadline cannot reuse that suite in the same executor. A preflight
failure also consumes the claim. Preparation refuses to overwrite a directory
containing an execution receipt, preserving its cleanup plan. Keep the registry
and receipts: this local
protection is not an account-wide replay barrier across fresh workspaces or
manually deleted journals. A fresh suite requires fresh approval.

```sh
# Only after the separate exact approval and its billing-mode requirements:
node controller.mjs execute ct-UNIQUE ../../.local/managed-canary-package /path/to/approval.json
# Retry only that execution's containment/deletion using its saved receipt:
node controller.mjs cleanup ct-UNIQUE ../../.local/managed-canary-package
```

Before mutation, read-only preflight requires absent exact Worker/Workflow names,
no old matching namespace, and a complete bounded namespace listing. The listing
accepts validated total-page metadata or walks to an empty page, matching the
pinned SDK, with a ten-page ceiling. Any 403, network error, malformed metadata or
unexhausted ceiling stops preflight. Plan/configuration identity
is checked against current source; configs are regenerated from that verified
scope before invocation. Normal proxy routing is retained, and API transport
automatic SDK retries are disabled. Status GETs alone tolerate up to three
consecutive transient connection, 429 or 5xx failures inside the existing
900-sample/deadline budget. A fourth failure, authentication denial or unsupported
status schema stops the run. Connection errors use the pinned SDK's
`APIConnectionError` class (including its timeout subclass), rather than
`Error.name`; aborts and lookalike names fail immediately. Mutations are never retried. The suite create is
attempted once; a lost reply is
reconciled by its exact ID.

The runner polls at most 900 times and stops starting new execution work at the
absolute deadline. It always attempts teardown in `finally`. Teardown first
uses Wrangler's supported `POST .../instances/batch/delete` operation for each
owned ID, confirms absence, removes definitions and the controller Worker, runs
the provider's `deleted_classes` migration, checks the namespace is absent, then
removes the provider Worker. Worker script deletions deliberately omit `force`: a
binding conflict remains a blocker rather than implicitly removing DO state. A
refused controller deletion does not skip the provider namespace confirmation.
This SDK path differs from Wrangler's Worker services deletion path. The prior
run's first instance-deletion failure remains unexplained because its response
details were not captured. A later absence check does not identify its cause.
It never deletes
a preexisting name or unrelated
namespace. If namespace deletion is denied, the provider Worker is retained to
preserve cleanup authority. A retry uses the original scope and nonsecret local
receipt; partial cleanup is not labeled success. Cleanup validates exact
suite/build/deadline identity and the receipt's saved `cleanupAuthorized` flag. It
does not need the original approval file, fresh billing attestation or an
unexpired deadline, and it only uses the deletion migration, never active deploy.
Preserve the tested source/configs and receipt for retries.

Each instance check now records its phase, initial absence, whether deletion was
attempted, a response summary, any sanitized deletion error and final absence.
The summary distinguishes unsupported arrays, reported errors, a missing
requested ID and an instance still visible after an acknowledged deletion.
Failures retain the SDK error class, numeric HTTP status and bounded numeric API
codes. Messages are controlled classifications; arbitrary API text, response
bodies, causes, URLs and headers are omitted because they can contain credentials.
A lost reply can be reconciled only by the same exact-ID absence checks.

`cleanup-history.jsonl` appends the time, execution scope and sanitized cleanup
result before replacing the latest `execution.json`. Keep both files. A successful
retry preserves earlier failures, and a failed history write leaves the prior
receipt intact. Later absence is an observation, not proof of eventual
consistency; deleting the parent definition can also change later GET results.

Native activity/step loops, independent expiry alarms and CLI cleanup provide
bounded application authority, but they are not a platform kill switch or
account-wide monetary limit. Executor death, a managed scheduler stall, or loss
of cleanup permission may require a separate containment retry. The nonsecret
local receipt records every attempted resource owner before deployment and saves
output before deletion. A cleanup failure stays an explicit blocker; it does
not reopen launch permission. No remote mutations have been exercised for this
approval/diagnostics change.
