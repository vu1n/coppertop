# Full frozen-policy recording baseline

## Reconciliation, October 11, 2026

The failed staging attempt described below is `mechanism-b187485658b65c94`;
its archive remains lost and its retained metadata is 9,194 bytes. Later DEV1150
attempts produced separate retained Xiaomi archives: execute16
`mechanism-3ae4005ef2000970` (163,737,600 bytes, SHA-256
`75283c1f846bc1e4e67ae89185fbe51a7a0e2341b9938c7140b4c75641eeb44b`)
and execute8 `mechanism-d7dfbc710920da91` (204,820,480 bytes, SHA-256
`80511e4d15184a6184c336df4ca651163c8209cceace7c44fe03afc0f374914b`).
Their existing Mac-local bytes were rehashed against receipts on October 11;
remote objects have not been freshly rechecked. Identity/release/receipt hashes
are in `manifests/clef-recovery/dev-archive-identities.json`. These unpaired
Xiaomi recordings establish no Clef comparison. Statements below about "current"
lost recording/metadata describe that earlier failed attempt, not all DEV1150
evidence. See [the evaluation inventory](clef-evaluation-readiness.md) for current
pairing, seed and readback requirements.

`official_recording_full` extends the verified short recorder through one official
CoffeeSetupMug DEV episode (seed1150, constructor1000, task index3, episode0).
The pinned stock evaluator owns history construction, action execution and stopping.
The frozen Xiaomi actor executes16 native controls per query. Stop on success,
termination, truncation or horizon600; at most38 queries and601 observations.
`done` is not success. This collects a baseline for a later separately approved
matched execute8 experiment; it does not demonstrate data utility or training gains.

The passive recorder preserves native16D state, native float32 16×12 decoded
proposals, executed/discarded offsets, query histories, raw camera arrays, control
and inference clocks, and simulator outcomes. Three compressed camera videos have
20Hz index timestamps. The stock composite video is copied unchanged. Model14D
state/history conventions stay in the query overlay. The initial observation hash
includes simulator physics and model geometry for a future matched reset check.

Readback checks every query, including clamped initial history masks, exact state,
camera and action bytes, final unused chunk offsets, all video frame times and
terminal boundaries. `episode.json` retains success, controls, inference calls,
model-load time, inference time and rollout wall time. The worker result also
reports elapsed time including startup, validation and owned server shutdown.
Cloud phase timing and startup overhead must be kept separate from rollout time.

## Bounded transport

Keep at most1GiB of immutable evidence and6000 CAS objects in a fresh attempt
prefix, including the inventory and report (the recorder reserves those slots and
1MiB of metadata headroom). A separate immutable inventory avoids embedding thousands of digests in
Modal's inline response; the response remains a ≤1KiB hash receipt, the report
≤64KiB. The producer seals one bounded archive. CPU publication uses a streamed
conditional single PUT up to256MiB, or16MiB multipart parts for larger archives
and explicit multipart resumes, followed by full archive SHA256 readback and a
verified completion receipt. See [archive publication](archive-publication.md)
for bounds, transport tests and the live validation gate. Producer scratch is
bounded to2GiB+16MiB and the exact owned temporary Volume to2GiB. After verified
publication and zero-active compute audit, the Volume is deleted. Publication
failure preserves staging only under explicit ongoing-storage acceptance.
Retained R2 data continues to incur Standard storage charges.

## Launch boundary

Pure preparation:

```
python -m coppertop.mechanism_cloud --ledger PATH --attempt mechanism-HEX16 \
  --workload official_recording_full
```

Preparation allocates nothing and creates no credentials. Execution requires the
exact reviewed code/ledger/plan hash and explicit approval of $8 additional,
within the existing $70.82 cumulative ceiling. The existing cached image is reused;
no model/asset downloads, image builds, training updates or automatic retries.
GPU workers receive no R2 credentials. Only the CPU publisher receives one
two-hour temporary archive session for that exact prefix, including the bounded
multipart operations. Full execution also requires explicit size-bounded ongoing
Volume storage acceptance and its release condition.

Hard limits: CPU cache gate610s; selected L40S or A100-80GB/4CPU/64GiB phase600s, worker560s,
official session550s plus ≤10s server cleanup; CPU publisher2100s; each phase
startup≤300s; whole controller4800s with cleanup admission reserve. Pricing expires
24h after review, and live billing is refreshed before each phase. Lagging provider
usage is not a verified remaining credit balance. The financial envelope does not
promise completion within the deadlines: capacity or publication overruns fail
without retry and retain only verified bounded evidence.

CPU tests use fake model/physics, including the actual pinned official evaluator
source. They establish recording contracts, not CUDA execution or a live full
recording. The successful four-control live acceptance remains separate evidence.

## Next A100 baseline proposal

The 2026-10-08 local CPU/R2 test verified a156098560-byte streamed PUT, full
SHA256 readback, receipt and exact test-object cleanup. Upload took37.1s,
readback202.0s and the controller255.4s. This closes the transport gate for that
size; the historical timeout investigation is closed without a root-cause claim.
Modal mount/startup/publication timing remains unverified. At the local measured
rates, the maximum supported1GiB+16MiB archive would need about28minutes for
upload/readback. The35minute publication deadline includes margin, not a completion
guarantee. Archives above256MiB still use multipart; its live completion and full
readback remain unverified. No larger archive is approved or supported implicitly.

Propose one A100-80GB frozen-Xiaomi DEV1150 execute16 baseline, ending on success,
environment termination/truncation or600 controls, at most38 inference calls.
Report success separately from done, calls, controls, model/inference/rollout/worker
times, seal/staging/publication times, archive/receipt verification and compute/
owned-Volume cleanup. Keep the existing cached image and no automatic retry.

Financial terms: $8 additional within the existing$70.82 cumulative admission
ceiling, including a$2 Modal compute/egress envelope and a$5 R2 invoice allowance.
The bound uses current [Modal prices](https://modal.com/pricing) and the existing
1.75 region multiplier ceiling. The local ledger snapshot is not a current credit
balance; refresh live billing before admission and each phase. The controller
budget is not a provider-enforced invoice ceiling.

R2 access uses only one fresh two-hour capability for the new exact attempt prefix,
with Get/Head/Put and bounded multipart actions, at most256 requests and
1GiB+32MiB in each read/write direction. The archive is at most1GiB+16MiB; its
receipt at most64KiB. GPU receives no R2 credentials. Canonical R2 evidence remains
in Standard storage, nominally at most about$0.0164/month at the archive size bound
before [R2 billing rounding/allowances](https://developers.cloudflare.com/r2/pricing/).
This ongoing retention is separate from the one-attempt spend envelope.

On failed/unverified publication, stop compute and retain only the exact owned
staging Volume, bounded to2GiB, under operator responsibility. At$0.09/GiB/month,
its maximum ongoing cost is$0.18/month before allowances. Release it after verified
publication or separately authorized recovery/deletion. No automatic expiry or
new scheduler is added. Verified publication permits deletion after a zero-active
compute audit. Staging failure before a durable checkpoint can still lose bytes
held only on disposable worker disk; retained staging is not a guarantee against
that failure class.

## A100 execution result and staging correction

The approved A100 DEV1150 actor run reported success after211 controls and14
inference calls, with212 observations. Rollout wall time was55.75s, total
inference7.14s, model load9.46s and worker elapsed190.66s. The local worker
validated the native recording, but the full artifact did not reach durable
storage: archive staging rejected Modal's provider-created Volume mount alias
as a symlink before copying the payload. The controller stopped and audited
all compute. Only9194bytes of control metadata remain in the exact owned Volume;
that metadata is also recovered locally. This is actor-success evidence, not a
published dataset release or a verified archive round-trip. No GPU retry occurred.

The Modal adapter now resolves only the configured provider mount root before
appending archive or attempt paths. Generic archive symlink checks remain unchanged
for descendants and source files. The fake-provider regression uses the same
symlinked mount layout through staging, publication and multipart resume.
This fixes future staging; it cannot restore the completed attempt's lost worker
bytes. Any new GPU attempt requires new spending authorization.

The next approval plan now adds a real provider-boundary check before GPU dispatch.
The existing CPU cache gate mounts the same owned Volume and subpath as the GPU,
seals a4KiB deterministic fixture into an archive capped at32KiB, stages it,
commits and reloads the Volume, then verifies the whole archive and restored bytes.
This stays inside the gate's existing610s hard budget and adds no separate Volume,
CPU container, R2 access or scheduler. A missing, failed or oversized preflight
receipt forbids GPU dispatch. These checks have only run against local mocks;
the actual Modal mount/commit/reload round-trip still needs approved execution.
This is a storage-boundary fixture, not robot/model or official-loader execution.

Current retained state is exactly the9,194-byte metadata Volume
`vo-ZTJ3CANaIxouEDS7yVdVPL`, named
`coppertop-mechanism-b187485658b65c94-outputs`. Its measured ongoing cost at the
published rate is about$0.00000077/month before allowances. The full generated
source payload and sealed archive were not durably staged or recovered and are
unavailable after the disposable worker stopped. All compute was audited inactive.
The retained metadata and prior evidence remain untouched; no deletion or new
storage has been performed while preparing this preflight.
