# Managed native canary preparation

This package prepares a bounded Cloudflare Workflows canary on the provisional
Lane A baseline (`effect-cf` 0.53.0 / Effect 4.0.2). It reuses the shared admission
and accounting transition and Lane A's exact persisted-data codecs. The local
fixtures from the initial spike are not deployed. Python, Modal, real providers,
GPU work, R2 and the robot action loop are outside this package.

**No managed test has run. Deployment and incremental spending need a separate
approval. A $1 billing bound is currently unverified.**

## Fixed resources and private control path

Default preparation uses account `a06ff95ff66e0f58f269da6d2fb99021`, which Cloudflare
previously returned as `Vuluan@gmail.com's Account`. This identifier is not a
credential. `CLOUDFLARE_API_TOKEN` is used only through normal client
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
The observer has no binding to the target workflows. During its observation
window it reads only provider storage, independently of the target scheduler.
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
| First activity timeout and retry | First step records at least two attempts, then completes within configured retry limits | Local control passed; managed not run |
| Durable sleep | Separate before/after observations around native `sleep(10 seconds)` | Local control passed; managed not run |
| Durable event | Independently observed wait, supported event delivery, matching payload and completion | Local control passed; managed not run |
| Event timeout | No event delivery; native three-second wait timeout followed by completion | Local control passed; managed not run |
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
`restart` changes replay state. The timeout probe hangs an activity once and
uses the configured native timeout/retry; it does not kill an isolate.

Every report keeps `hostProcessLoss: "untested"` and `realProviderReady: false`,
even when all supported gates pass. Such a pass cannot close the original
unattended host-loss recovery gate or authorize real-provider integration. A
separate decision may explicitly rely on the documented managed durability
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

Tests cover denial/approval boundaries, exact ownership, existing-name conflicts,
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

After the parent obtains the separate approval and verifies billing headroom,
refresh the package using a unique `ct-...` suite name. Preparation fixes both
resource names and an absolute deadline; do not reuse an expired plan or another
suite's resources. Preserve the exact tested revision and its build digest.

`execute` requires a local approval document naming the suite and build digest,
with `approved: true`, `incrementalDollarCap: 1`,
`billingHeadroomVerified: true`, and `subscriptionChangeAuthorized: false`.
An implementation-only request does not provide those facts. No approved file
is supplied in this PR.

```sh
# Only after the separate approval and billing verification:
node controller.mjs execute ct-UNIQUE ../../.local/managed-canary-package /path/to/approval.json
# Retry only that execution's containment/deletion using its saved receipt:
node controller.mjs cleanup ct-UNIQUE ../../.local/managed-canary-package /path/to/approval.json
```

Before mutation, read-only preflight requires absent exact Worker/Workflow names,
no old matching namespace, and a complete bounded namespace listing. Any 403,
network error or unknown pagination stops execution. Plan/configuration identity
is checked against current source; configs are regenerated from that verified
scope before invocation. Normal proxy routing is retained, and API transport
retries are disabled. The suite create is attempted once; a lost reply is
reconciled by its exact ID.

The runner polls at most 900 times and stops starting new execution work at the
absolute deadline. It always attempts teardown in `finally`. Teardown first
uses Wrangler's supported `POST .../instances/batch/delete` operation for each
owned ID, confirms absence, removes definitions and the controller Worker, runs
the provider's `deleted_classes` migration, checks the namespace is absent, then
removes the provider Worker. It never deletes a preexisting name or unrelated
namespace. If namespace deletion is denied, the provider Worker is retained to
preserve cleanup authority. A retry uses the original scope and nonsecret local
receipt; partial cleanup is not labeled success.

Native activity/step loops, independent expiry alarms and CLI cleanup provide
bounded application authority, but they are not a platform kill switch or
account-wide monetary limit. Executor death, a managed scheduler stall, or loss
of cleanup permission may require a separate containment retry. The nonsecret
local receipt records every attempted resource owner before deployment and saves
output before deletion. A cleanup failure stays an explicit blocker; it does
not reopen launch permission. No remote mutations have been exercised in this PR.
