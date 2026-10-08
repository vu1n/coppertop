# Full frozen-policy recording baseline

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
≤64KiB. CPU publication performs conditional single PUTs of at most64MiB each,
then complete byte/hash readback. No multipart permissions or unfinished uploads
are needed. Objects exceeding this cap fail loudly. Local scratch and the exact
owned temporary Volume are bounded to2GiB each; the Volume is deleted after the
publication attempt and zero-active compute audit. Retained R2 data continues to
incur Standard storage charges.

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
one-hour temporary GetObject/HeadObject/PutObject session for that exact prefix.

Hard limits: CPU cache gate610s; L40S/4CPU/64GiB phase600s, worker560s,
official session550s plus ≤10s server cleanup; CPU publisher900s; each phase
startup≤300s; whole controller3600s with cleanup admission reserve. Pricing expires
24h after review, and live billing is refreshed before each phase. Lagging provider
usage is not a verified remaining credit balance. The financial envelope does not
promise completion within the deadlines: capacity or publication overruns fail
without retry and retain only verified bounded evidence.

CPU tests use fake model/physics, including the actual pinned official evaluator
source. They establish recording contracts, not CUDA execution or a live full
recording. The successful four-control live acceptance remains separate evidence.
