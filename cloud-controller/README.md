# Coppertop cloud orchestration spike

This additive, local synthetic spike compares native Cloudflare Workflows through `effect-cf` with the existing upstream Effect Cloudflare DO workflow engine. It preserves the Python runtime. All fixtures use synthetic identities and independent SQLite provider/observer storage.

Each directory has its own dependency universe:

- `shared/`: engine-neutral TypeScript contracts, pure transitions, generated operational histories, and the independent fake provider. No Effect dependency.
- `lane-a/`: exact released Effect/SQL adapter and `effect-cf`, with native Cloudflare steps and waits.
- `lane-b/`: the upstream PR's exact source, coherent workspace dependencies, lockfile and patches. Its setup/build happens under ignored `.local/upstream/effect`.

The ledger owns atomic admission, lifetime grant consumption, money holds, one-shot dispatch/body claims, cancellation, cleanup, immutable receipt publication and settlement. Workflow progression belongs to each engine. A provider spawn can succeed without a reply; replay cannot restore either permission. Unknown execution retains liability. Quarantine blocks new work while allowing recorded containment and accounting.

The fake provider's calls, cost observations, artifacts and observer events are stored separately from either orchestrator. Its generation-disable operation is an intentionally strong synthetic containment primitive. Actual Modal containment, queuing and transport retries still require integration verification.

## Run locally

Use Node 24.19.0. From the repository root:

```sh
cd cloud-controller/shared
npm ci --ignore-scripts --cache ../../.local/npm-cache
npm run typecheck
npm run lint
npm test
```

Each lane's evidence file records its commands, exact runtime and acceptance results. Run the native and upstream suites separately. Both strict acceptance commands expose known recovery gaps; characterization checks must never be read as acceptance passing.

No provider accounts, credentials, datasets, model weights, GPU hardware or deployment are needed. Synthetic API handlers and fault injection routes are local test fixtures. They do not establish production authentication, authorization or an integration-ready Modal gateway. The initial local fixtures have no deployment script. The additive
[managed canary package](managed-canary/README.md) supplies private entrypoints,
bounded local controls and nonmutating deployment packaging. Its deployment and
spending approval remain separate. A prior authorized synthetic managed run was
reported as passing supported controls and later cleanup; its raw local receipts
were not restored. See the [current evidence limitations](managed-canary/EVIDENCE.md#credit-approval-and-cleanup-diagnostics).

## Real-provider boundary

The later boundary is a small authenticated Python Modal gateway for fixed launch/status/cancel operations. GPU science, validation and bulk Modal-to-R2 transfers remain in Python. `coppertop/performance_cloud.py` separates poorly today: `create_functions`/`validate_report` contain science, `execute_phases` lifecycle, and `_live` allocation/authentication/spawn/billing/cleanup. The sidecar cannot call `_live` from a Worker or port that entire controller.

Before genuine integration, reconcile the unpublished Mac controller changes and a reviewed migration manifest of nonsecret state, preserving authorization consumption, ownership, archive hashes and cache-image provenance. Prove the supported authenticated invocation route, independent provider stop route, immutable deployment identity, scoped access, native provider spend limits and bounded authorization. Then obtain separate staging/deployment and paid-canary authorization. The sidecar stays outside the robot action loop.

## Source and scope

Base: `a62a523e0ff16dd29415fb275e628fb910a76cc1`. Approved Library plan: `libfile_228a7e5e2b308191b51ebb9955c64ae5`, version 1, `2026-10-09-coppertop-effect-cloud-sidecar-plan.md`. Its complete text was read through Library. Both supported materialization attempts returned a transfer that failed to download; this executor has no materialized plan copy.

The revised Effect/Cloudflare plan supersedes the older Absurd/DBOS preference in repository instructions. Other safety constraints remain. Delivery is a private draft PR; no merge, deployment, credentials, paid runs, Mac modification or public upstream communication is authorized.
