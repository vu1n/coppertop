# Bounded Modal CPU seam preparation

This is a local Python SDK launch/receipt/cancellation check, separate from the
Cloudflare canary and orchestration engine. It does not create an HTTP gateway or
establish Cloudflare-to-Modal integration, managed recovery, cost settlement,
generation fencing, GPU science or durable R2 publication.

The user approved one CPU-only attempt and a **$0.25 operational allowance** after
disclosure that the provider invoice ceiling is unverified. Every executable
approval must bind the reviewed source and fresh plan, the actual configured
workspace, existing image ID, that allowance/risk acceptance and exact cleanup
scope. The transcript belongs with the local execution authority. A prior
Cloudflare credit approval supplies no Modal authority.

The default command only prepares local metadata and source hashes:

```sh
.venv/bin/python tools/run_modal_seam.py prepare
```

It leaves unknown workspace/image fields null and creates no approval document.
After the configured workspace and an existing immutable image have been
verified, preparation accepts `--workspace` and `--image-id`. The SDK is pinned to
`modal==1.6.1`; the remote image must run Python 3.12 and the standard library.
Before allocation, a bounded read-only `ImageFromId` lookup verifies the exact ID
and Python 3.12 metadata. The `Image.from_id` handle remains lazy for SDK execution;
the pinned SDK does not support its on-demand hydration. This single internal
lookup is version-bound and never calls `ImageGetOrCreate` or requests a build.
The old GPU/model seed image is not selected or used as a fallback.

There are no image build commands, dependency installs, secrets or project-tree
uploads. Only the empty package initializer and the exact standalone probe are
mounted at startup with `add_local_file(copy=False)`. In the pinned SDK this
preserves the existing base image ID. The mounted probe hashes its own bytes
against the approved request before executing. Unknown image identity or
mandatory construction of a new base image remains a prerequisite requiring
cost review inside the approved total allowance; this implementation refuses an
implicit build. [Modal image documentation](https://modal.com/docs/sdk/py/latest/Image)

## Fixed scope and observed gates

- One ephemeral App in `main`, one Function, at most one container, two sequential
  spawn attempts. No persistent deployment, public endpoint, GPU, Volume, Sandbox,
  R2 access, injected compute secrets or subscription change.
- CPU request/limit `(0.125, 0.125)`, memory `(256, 256)` MiB, single-use containers,
  zero retries, zero minimum/buffer containers and a two-second scaledown window.
  Input timeout is 30 seconds and startup timeout 60 seconds. Function networking
  and Modal API access are blocked; the client retains normal control-plane access.
- The local supervisor fixes one absolute 180-second execution deadline before
  spawning its worker. The probe checks that authority. It then allows 60 seconds
  for worker termination, independent App stop and both absence queries.
- Completion must return exactly `bytes(range(32))`, SHA-256
  `630dcd2966c4336691125448bbb25b4ff412a49c732db2c8abc1b8581bd710dd`, correct
  run/generation/body/source identity and nonsecret runtime container ID. The raw
  bytes are saved exclusively under their hash, fsynced and read back. This is a
  local receipt; cost stays null and billing stays unverified.
- The second input validates the same payload and sufficient remaining hold
  authority before emitting a structured start marker,
  then holds for 20 seconds. The adapter requires a matching stdout marker and
  the target container present in the exact App before cancelling that exact call
  with `terminate_containers=True`. It requires that container absent before App
  stop and within 20 seconds of the spawn attempt's start. Slow startup, missing
  observations or natural completion after that interval fail conservatively.
  No critical gate relies on the best-effort call graph. The receipt records
  cancellation acknowledgement and containment observations, without asserting
  a native terminal-state classification. [FunctionCall documentation](https://modal.com/docs/sdk/py/latest/FunctionCall)

At published Function rates, the requested CPU/memory for the whole 240-second
execution/cleanup envelope is approximately **$0.0005262**, excluding higher
actual CPU usage and service overhead. Modal bills the higher of requested or
actual resources and includes load/idle time. The CPU resource guide describes
throttling as a soft limit. The $0.25 approval is an operational allowance, not
a newly verified invoice guarantee. No account plan, remaining credit balance or
provider usage has been read in the offline checks. No settings are changed.
[Pricing](https://modal.com/pricing), [resource limits](https://modal.com/docs/guide/resources).

## Execution authority and independent cleanup

`execute --plan ... --approval ... --output ...` checks the complete current plan
and exact approval before reading credentials or starting a process. The approval
requires `approved: true`, the plan SHA, exact workspace and `main`,
`operationalAllowanceUsd: "0.25"`, `invoiceCapVerified: false`,
`boundedExposureAccepted: true`, `cleanupAuthorized: true`,
`imageBuildAuthorized: false` and `persistentDeploymentAuthorized: false`.
The trusted operator must bind the user's actual authorization; the CLI does
not interpret prose or independently establish a billing cap.

An exclusive per-suite local claim blocks another directory or replay. The worker
also checks that claim, exact approval, fixed supervisor deadline and exclusive
worker-start marker before importing Modal. It requires the normal injected
`MODAL_TOKEN_ID`/`MODAL_TOKEN_SECRET` pair, verifies the service-returned workspace,
looks up existing `main` without creation, rejects a conflicting App name and
validates the existing image. It never reads token values or backups itself.

The parent supervisor retains the plan and nonsecret ownership journal, ignores
raw worker stdout/stderr, kills a stuck worker process group and stops only the
saved exact App ID, including the SDK's 22-character suffix requirement so the
CLI cannot interpret it as a deployment name. Stop acknowledgement alone never passes: exact-App container
and Sandbox lists must both be empty. Malformed, foreign, duplicate or oversized
metadata, denial and uncertain absence remain failures. Unknown App identity
after allocation is an explicit orphan-risk blocker; no App is adopted by name.
Known identity discovered during failed context entry is retained for cleanup.

`cleanup --plan ... --output ...` uses the original validated ownership and
cleanup authority after plan expiry or source repair. Its plan, approval and
execution claim must match the original saved receipts; current source hashes
are a launch-only gate. It cannot relaunch or build. Append-only
sanitized history retains both failed and successful containment attempts;
messages omit arbitrary API text, URLs, headers, bodies and causes.

This supervisor survives child death/hangs, not loss of the whole local executor.
SDK initialization can allocate an App before returning an ID; if the caller dies
in that window, independent absence cannot be established here. No account-wide
kill switch or autonomous native deadline kill is claimed.

Offline validation uses:

```sh
.venv/bin/python -m unittest discover -s tests -p test_modal_seam.py -v
.venv/bin/python -m unittest discover -s tests -v
```

Local fixtures exercise malformed receipts, authorization/source/scope refusal,
ambiguous ownership, cancellation and independent cleanup failure. Lazy pinned
SDK definitions are checked without authentication. These are not provider
execution results. Actual identity, price/usage and cleanup evidence must be
preserved from the separately approved single live attempt.
