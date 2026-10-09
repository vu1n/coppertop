# Lane A: native Cloudflare Workflows

This is a local synthetic evaluation of `effect-cf` wrapping Cloudflare's native Workflow steps/events, with an official Effect SQLite Durable Object adapter for the control ledger. It is not a new durability engine. The recovery gates remain strict: two autonomous restart gates fail in this pinned local workerd harness. The shared plain native controls reproduce a local emulator limitation outside effect-cf; managed Cloudflare recovery remains **unproven**, and this branch does not authorize real-provider integration.

## Reproduce

```sh
cd cloud-controller/lane-a
npm ci --ignore-scripts --cache ../../.local/npm-cache
npm run typecheck
npm run lint
npm run test:acceptance
npm run test:characterize
```

`test:acceptance` (also the default `test` and `check`) fails on the documented runtime recovery regressions. `test:characterize` explicitly labels those cases **KNOWN GAP CHARACTERIZATION (not acceptance)** and asserts their precise observed failure shape; it fails if those gaps unexpectedly disappear or the fixture fails differently. A green characterization run does not promote the engine to accepted. `test:safety` skips those two recovery probes; its skip count is not acceptance evidence.

All behavioral test fixtures run actual workerd processes; the final bundle metric is a build-only check. The provider/observer is a separate Miniflare instance with a separate SQLite persistence root and stays alive while the sidecar process is disposed and recreated. Miniflare 5 receives `resourcePersistencePath` outside `convertV4MiniflareOptions`; passing only the old `durableObjectsPersist` option loses persistence.

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

`Coordinator` applies the shared pure transition and persists admission, reservations, claims, outbox intent and the due alarm in the official `SqlClient.withTransaction`. Dispatch and workload claims are consumed once and remain consumed through ambiguity, interruption, cancellation and replay. Provider call IDs are reconciled and retained for containment. Uncertain launch/provider completion without a valid artifact keeps the financial reservation. Cleanup re-enumerates and registers all calls after the provider's independently persisted generation fence, cancels every active fenced call, and registers the final snapshot before checking terminal status for every owned call; accounting settles only after that proof. Unconfirmed cleanup keeps durable outbox authority and retries; native engine errors do not get silently acknowledged.

The alarm handles control outbox delivery, deadline containment and cleanup retries. Each synthetic deferred launch has a durable per-run not-before timestamp; every mutation preserves the earliest armed alarm and active safety deadline. Delivered/retired ensure intent is separate from actual native instance existence, which is recorded only after successful creation or an existing-instance observation. Cleanup acknowledgement waits for confirmed provider containment and termination of a positively known native instance. It is not a second routine provider poller. Normal read-only status reconciliation belongs to native Workflow steps: event waits have a two-second timeout and at most three bounded reconciliation attempts. A dropped provider notification remains in the outbox. No-restart event-loss and status-fallback controls both pass.

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
| Suspended native wait autonomous restart | **Failed** | Body runs once; native subscription proves a persisted `wait_started` event and four completed step outputs before stopping. Provider is finished while sidecar is down. After restart only independent observer reads occur for 25 seconds; native bounded status fallback does not run and no parent completion appears. |
| Post-failure target status intervention | Failed to restore progress | Only after each independent gate fails, one native `status()` request is made and another 12 seconds observed. Both remain `running` without completion. This intervention is separate from acceptance. |
| Matching controls without restart | Passed | First-step barrier and externally finished/lost-callback status fallback complete normally. |
| Completed native history / same-ID create | Passed | After restart, native status returns completed output; same-ID creation returns the retained instance; first/body/parent counts stay one and the receipt stays identical. |
| Actual Worker code upgrade | Passed (fail closed) | Bundles compiled with deployment epoch 1 and 2 have different module bytes. An entered incomplete v1 activity is restarted under the second module; launch/read boundaries reject epoch 1, no provider call starts and held liability remains. |
| Late launch after cancellation | Passed | A late extra call before the generation fence is found/cancelled; the fenced generation rejects later spawn and all later body claims. |
| Actual suspended-workflow cancellation | Passed | Cancellation contains the provider call, terminates the known native workflow and preserves settled cost through cleanup replay/restart. |
| Deadline containment | Passed | A suspended fake run expires as `timed_out`, closes owned calls and settles; later body claim denied. |
| Ambiguity / completion liability | Passed | Unknown launch and provider completion without receipt keep hold 10; proven cleanup then releases hold and settles cost 1. |
| First unavailable cancel / cleanup retry | Passed | A local fault proxy returns 503 for the first cancel and writes an independent outage observer. Unconfirmed cleanup keeps hold/outbox; alarm retries the owned call, confirms terminal status and settles. |
| Alarm priority across deferred admissions | Passed | A second deferred run cannot postpone an earlier safety deadline; the expired run reaches durable cleanup acknowledgement without creating a native instance. |
| Opaque leading-hyphen run ID | Passed | Fixed native instance prefix admits an accepted `-opaque` run, completes it and retains native output/accounting. |
| Late launch between lookup and generation fence | Passed | Proxy injects a real second provider call exactly at `/disable`; fenced re-enumeration registers/cancels both calls before terminal proof, settlement and cleanup acknowledgement. |
| Compatible source-definition rolling upgrade / retained old-worker routing | Not run | Only incompatible code-epoch refusal is tested; real version routing needs a separate rollout design. |
| Actual native child-to-parent workflow wake | Not run in A | This lane tests provider-to-parent notification loss. The upstream lane owns its child-workflow regression. |
| Arbitrary external crash, power loss, SQLite corruption | Not run | Miniflare teardown sends SIGKILL and awaits process exit before spawning a replacement; these controlled restarts do not simulate every crash class. |
| Real Cloudflare account durability / Modal / Python integration | Not run | No accounts, credentials, paid workloads, deployment or unpublished Mac controller state used. |
| CPU and production billing metrics | Not run | Local wall time and bundle byte size are measured; production CPU/billing cannot be inferred. |

The review revision adds `raw-native.test.mjs`, which runs the engine-neutral do/sleep/do program from shared source. It uses this lane's own Miniflare 5/workerd dependencies; the upstream wrapper tests the same source under its coherent Miniflare 4 pin. The raw suite contains one installed-source assertion and three actual-workerd controls: no restart completes, while dispose/recreate and setOptions during persisted sleep stall for at least eight observer-only seconds and through the original ten-second sleep due time plus three seconds. The original due time must still be in the future after restart. Read-only inspection of only fixture-generated target SQLite establishes the completed first step and active sleep entry before restart; target inspection stops before restart. The observer remains a separate live persisted runtime.

Both installed native bindings have a no-op heap-peek `handleNextAlarm` and no engine alarm handler. Status is a persisted metadata/log read. Event delivery can cold-initialize a native execution, so the conclusion is limited to the selected timer-restart program. This is evidence about local emulation, not a managed production fault or a universal failure of native workflows. Completed native history replay remains a positive restart control; it does not prove autonomous in-flight recovery.

Final runtime inputs at `1791e8667ebcc639533b3cb9d6c3b20a9f2cbd21`: strict **20 checks, 18 passed, 2 failed**, 222.36 seconds; characterization **20 passed, 0 failed**, 199.10 seconds; zero skips in both. Each suite contains 18 actual-workerd behavioral checks, one installed-source assertion and one build-only bundle metric. The four raw controls contain one source assertion and three runtime behaviors; their two restart stalls remain negative characterizations even inside the strict suite. Bundle size remains 409,200 bytes. Counts, timings and bundle hash appear in `runtime-results.txt` and `characterization-results.txt`. The two strict recovery failures remain tripwires. Each log records the tested Coppertop commit/tree, dirty input status, SHA-256 source/lock/installed native binding hashes, command, mode and explicitly supplied installation flags. The derived application bundle has a hash. Captured logs are committed after execution, so their source commit precedes the evidence-only commit. Historical flags are not inferred from the reproduction example. Local timings include fixture startup/shutdown and observation windows; they are not GPU or managed scheduler performance estimates.

## Integration prerequisites and recommendation

Keep this as the provisional native candidate. The raw controls complete the plain-native comparison for these pins: the selected timer stall does not require effect-cf. A changed native binding triggers rerunning the raw controls and both strict gates. Managed acceptance requires either a separately authorized bounded test with a supported matching fault mechanism or an explicitly documented reliance on the managed durability contract. The exposed lifecycle APIs do not establish a host-process-kill mechanism; restart/terminate/redeploy have different semantics. Do not add a custom scheduler to obtain a pass or extrapolate local failure to production. A narrowly scoped upstream regression could be useful, but no upstream communication or submission is authorized.

Real integration still requires reviewed nonsecret Mac controller state and a small authenticated Modal Python launch/status/cancel gateway, including equivalent generation fencing and settlement proof. Keep science, official report validation, GPU work and bulk Modal-to-R2 transfers in Python. The sidecar never belongs in the latency-sensitive robot action loop. This fixture's local-only endpoints and fixed synthetic principal/grant do not establish production authentication.
