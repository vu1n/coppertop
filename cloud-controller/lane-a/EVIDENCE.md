# Lane A: native Cloudflare Workflows

This is a local synthetic evaluation of `effect-cf` wrapping Cloudflare's native Workflow steps/events, with an official Effect SQLite Durable Object adapter for the control ledger. It is not a new durability engine. The accepted recovery gates remain strict: Lane A is **not accepted** for real-provider integration because the two autonomous restart gates fail in this pinned local workerd harness.

## Reproduce

```sh
cd cloud-controller/lane-a
npm ci --cache /workspace/coppertop/.local/npm-cache
npm run typecheck
npm run test:acceptance
npm run test:characterize
```

`test:acceptance` (also the default `test` and `check`) fails on the documented runtime recovery regressions. `test:characterize` explicitly labels those cases **KNOWN GAP CHARACTERIZATION (not acceptance)** and asserts their precise observed failure shape; it fails if those gaps unexpectedly disappear or the fixture fails differently. A green characterization run does not promote the engine to accepted. `test:safety` skips those two recovery probes; its skip count is not acceptance evidence.

All test fixtures run actual workerd processes. The provider/observer is a separate Miniflare instance with a separate SQLite persistence root and stays alive while the sidecar process is disposed and recreated. Miniflare 5 receives `resourcePersistencePath` outside `convertV4MiniflareOptions`; passing only the old `durableObjectsPersist` option loses persistence.

## Tested pins

| Component | Exact pin/source |
| --- | --- |
| effect-cf | 0.53.0; registry `gitHead` `b6390c618c796483bd32c99baf8a6411bba77ad2` |
| Effect / official SQLite DO adapter | 4.0.2; `effect@4.0.2` tag `269a7c864351231d42e6e95b7fa8f32050df3691` |
| Miniflare | 5.20260926.1-alpha |
| workerd | 1.20260926.1; actual executable spawned by Miniflare |
| workers-types / compatibility date | 5.20260926.1 / 2026-09-26; `nodejs_compat` for effect-cf's tracer |
| Node / npm | 24.19.0 / 11.9.0 |
| TypeScript / esbuild | 5.9.3 / 0.25.12 |
| Lockfile | `package-lock.json`; includes coherent Effect 4.0.2 peer dependencies |

The source pins were rechecked using npm metadata and `git ls-remote`, rather than copied from the readiness snapshot. No `@effect/vitest` or Vitest is installed: `node:test` avoids the upstream harness's Vitest 4 versus Effect/Vitest 5 mismatch. Bun, Vite+, Wrangler and cloud deployment are not required by this isolated harness and were not run. Relevant primary source implementations: [effect-cf Workflow](https://github.com/danieljvdm/effect-cf/blob/b6390c618c796483bd32c99baf8a6411bba77ad2/packages/effect-cf/src/Workflow.ts), [official SQLite DO client](https://github.com/Effect-TS/effect/blob/269a7c864351231d42e6e95b7fa8f32050df3691/packages/sql/sqlite-do/src/SqliteClient.ts).

## Implemented boundaries

`schema.ts` decodes external requests, provider call/status records, immutable artifact bytes, workflow payloads and persisted ledger graphs with Effect Schema, rejecting excess properties. The server computes the canonical RunSpec SHA-256; callers cannot choose the receipt's spec digest. Schema/epoch/deployment-code fences reject incompatible persisted data and new permissions.

`Coordinator` applies the shared pure transition and persists admission, reservations, claims, outbox intent and the due alarm in the official `SqlClient.withTransaction`. Dispatch and workload claims are consumed once and remain consumed through ambiguity, interruption, cancellation and replay. Provider call IDs are reconciled and retained for containment. Uncertain launch/provider completion without a valid artifact keeps the financial reservation. Cleanup needs the provider's independently persisted generation fence and terminal status for every owned call; accounting settles only after that proof. Unconfirmed cleanup keeps durable outbox authority and retries; native engine errors do not get silently acknowledged.

The alarm handles control outbox delivery, deadline containment and cleanup retries. It is not a second routine provider poller. Normal read-only status reconciliation belongs to native Workflow steps: event waits have a two-second timeout and at most three bounded reconciliation attempts. A dropped provider notification remains in the outbox. No-restart event-loss and status-fallback controls both pass.

Synthetic artifact content contains run ID, canonical spec digest, body claimant, validity and decision gate. The independently persisted fake provider validates the content hash and performs immutable conditional writes. Before the ledger accepts a receipt, it reads those bytes back, decodes the envelope and verifies hash, size, key and identity. Provider `completed` alone never establishes scientific success.

## Runtime result matrix

| Acceptance row | Result | Actual evidence |
| --- | --- | --- |
| Atomic concurrent admission / last slot / reservation | Passed | Three concurrent submissions admit two and reject one; grant count, holds and outbox agree after restart. |
| Canonical digest and exact idempotency | Passed | Reordered input keys replay the same digest without another reservation; changed spec conflicts. |
| Strict request and stored-state codecs | Passed | Excess spec field rejected; unknown stored schema and mismatched epochs reject operations after actual process restart. |
| Official adapter rollback / nested transaction / scheduler yield | Passed | Injected child and outer failures, an interrupt and a yield leave only committed `outer`/`after-yield` rows. |
| SQL + alarm transaction rollback | Passed | Failed SQL/alarm transaction preserves the earlier committed alarm. |
| Duplicate dispatch and body claims | Passed | Eight concurrent dispatch requests and eight body claimants each grant exactly one new permission. |
| Ambiguous spawn acknowledgement | Passed | Provider commits one call then replies 503; reconciliation recovers ownership without another launch. |
| Lost provider wake without restart | Passed | Dropped wake remains durable; native event/status fallback reaches immutable receipt and settlement. |
| First incomplete activity autonomous restart | **Failed** | Independent `first-start` is durable before the first step completes; no prior Workflow timer or checkpoint. After restart only provider `/observe` is read for 25 seconds. No `first`, body or completion appears. |
| Suspended native wait autonomous restart | **Failed** | Body runs once; provider is finished while sidecar is down. After restart only independent observer reads occur for 25 seconds; native bounded status fallback does not run and no parent completion appears. |
| Post-failure target status intervention | Failed to restore progress | Only after each independent gate fails, one native `status()` request is made and another 12 seconds observed. Both remain `running` without completion. This intervention is separate from acceptance. |
| Matching controls without restart | Passed | First-step barrier and externally finished/lost-callback status fallback complete normally. |
| Completed native history / same-ID create | Passed | After restart, native status returns completed output; same-ID creation returns the retained instance; first/body/parent counts stay one and the receipt stays identical. |
| Actual Worker code upgrade | Passed (fail closed) | Bundles compiled with deployment epoch 1 and 2 have different module bytes. An entered incomplete v1 activity is restarted under the second module; launch/read boundaries reject epoch 1, no provider call starts and held liability remains. |
| Late launch after cancellation | Passed | A late extra call before the generation fence is found/cancelled; the fenced generation rejects later spawn and all later body claims. |
| Actual suspended-workflow cancellation | Passed | Cancellation contains the provider call, terminates the known native workflow and preserves settled cost through cleanup replay/restart. |
| Deadline containment | Passed | A suspended fake run expires as `timed_out`, closes owned calls and settles; later body claim denied. |
| Ambiguity / completion liability | Passed | Unknown launch and provider completion without receipt keep hold 10; proven cleanup then releases hold and settles cost 1. |
| First unavailable cancel / cleanup retry | Passed | A local fault proxy returns 503 for the first cancel and writes an independent outage observer. Unconfirmed cleanup keeps hold/outbox; alarm retries the owned call, confirms terminal status and settles. |
| Compatible source-definition rolling upgrade / retained old-worker routing | Not run | Only incompatible code-epoch refusal is tested; real version routing needs a separate rollout design. |
| Actual native child-to-parent workflow wake | Not run in A | This lane tests provider-to-parent notification loss. The upstream lane owns its child-workflow regression. |
| Abrupt SIGKILL, power loss, SQLite corruption | Not run | Tests dispose/recreate actual workerd processes; they do not simulate every crash class. |
| Real Cloudflare account durability / Modal / Python integration | Not run | No accounts, credentials, paid workloads, deployment or unpublished Mac controller state used. |
| CPU and production billing metrics | Not run | Local wall time and bundle byte size are measured; production CPU/billing cannot be inferred. |

The final strict run executed **13 scenarios: 11 passed, 2 failed, 0 skipped**, after a clean locked install and successful typecheck. The bundled lane was **405,928 bytes** (unminified ES module). That run took **140.82 seconds**, including fixture startup/shutdown and the two observation/intervention windows. Successful independent observer waits measured 524 ms for ambiguous-run completion, 1,345 ms for the first-step no-restart control, and 1,979 ms for external-finish notification-loss fallback; these are single local observations, not benchmark estimates.

The separately run final characterization completed **13/13 with zero skips** in **140.85 seconds**, including the two explicitly negative recovery characterizations. It also verifies the native cancellation status is `terminated` and the fake first-cancel outage has an independent durable observer record.

Detailed commands, actual counts, elapsed observer waits and bundle size are in `runtime-results.txt` and `characterization-results.txt`. All numbers are local synthetic measurements; observer waits exclude fixture startup and do not represent GPU performance.

## Integration prerequisites and recommendation

Keep this as the native baseline candidate, with the restart failure treated as an unresolved local-runtime gate. Diagnose whether the missing rehydration/wake lies in the pinned native workerd/Miniflare engine or requires different supported lifecycle setup; do not add a custom scheduler to obtain a pass or extrapolate the local failure directly to Cloudflare production. A narrowly scoped upstream regression could be useful, but no upstream communication or submission is authorized.

Real integration still requires reviewed nonsecret Mac controller state and a small authenticated Modal Python launch/status/cancel gateway, including equivalent generation fencing and settlement proof. Keep science, official report validation, GPU work and bulk Modal-to-R2 transfers in Python. The sidecar never belongs in the latency-sensitive robot action loop. This fixture's local-only endpoints and fixed synthetic principal/grant do not establish production authentication.
