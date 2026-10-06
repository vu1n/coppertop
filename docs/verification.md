# Verification record

CPU suite: **11 tests passed**, including stable frame/group IDs, immutable objects and corruption detection, release/fork reference sharing, boundary masks, reversible action semantics, rejection of unpinned acquisitions and unsafe camera names, real official import, unknown success/recovery labels, independent byte-identical exports, exact original signal rows and unchanged MP4 hashes. `COPPERTOP_SOURCE` pointed only to the already acquired original local training-data directory. No original files were written.

Official loader: installed unmodified **lerobot==0.3.3**, LeRobot format **v2.1**, Python **3.11.15**, torch **2.7.1**, torchvision **0.22.1**, datasets **3.6.0**, PyArrow **20.0.0**, PyAV **15.1.0**. All actual runtime packages are pinned in `requirements-loader.txt`; install LeRobot separately with `--no-deps` to keep this a dataset-loader runtime subset. No dependency shims or extracted loader functions substitute for execution. All Hugging Face checks ran offline; no remote dataset/model was fetched.

The official `LeRobotDataset` read all **347 frames**. State/action tensors were bit-identical to the official float32 conversion; maximum float64-to-float32 error was **2.370526415162999e-7** for state and **2.8950827513085642e-8** for action. Exact float64 source values remain in Parquet. All **1,041 camera frames** matched independent PyAV RGB decoding. Every camera presentation timestamp was within **1e-5 seconds** of the signal timestamp. Episode bounds were exactly **[0,347)**; first/final action-window padding masks passed. Done values were compared directly. No success inference.

Run commands are in the README. `coppertop.verify_loader` produces the machine-readable loader report. macOS sandbox emitted PyArrow CPU-cache sysctl warnings; checks completed. Pinned torchvision emits a video-API deprecation warning; this does not change the pinned loader result. The optional official runtime environment occupies about **692 MiB**. No GPU/model loading or large download occurred.

## Review

`/tighten` applied deslop, simplify and contextualize in sequence: kept existing source guards, removed unused copied `struct` import, separated Xiaomi state conversion from the core, removed duplicate split/quality values from episode records in favor of overlays, and retained why-comments for action order and structural LeRobot split ranges.

`/ship-review` covered the entire new repository against an empty base. Structure and correctness were skeptical local rereads, not independent agent reviews. Small modules keep object storage, import, export and verification separate; no surviving structural/correctness blockers. The scope is one episode/schema and a local store, not a platform.

Security used independent static finder and refuter agents. Trust boundary: operator CLI args trusted; imported files/manifests untrusted. Finder candidates concerned metadata-controlled output paths and compressed video/Parquet expansion. Fixes pinned acquisition authenticity, fixed export templates/camera allowlist, bounded signal metadata before decode, and streamed video statistics with frame/dimension guards. Refuter dropped the original three candidates; no confirmed vulnerability remains. Native codec internals are outside this source review. Imported/shared stores still require ordinary integrity/access control; this prototype is not an untrusted multi-tenant ingestion service.

Residual limits: no R2/compute integration, durable orchestration, model/token parity, training, simulator evaluation or robot-improvement evidence. Sidecars reference raw store objects; copying an export alone is not a complete Chords store migration. Object immutability is application-level; interrupted writes/export directories require explicit operator recovery. No official-loader blocker remains for this episode.

## Cloud transfer publication, 2026-10-06

The curated publication includes the streaming storage boundary, bounded native
checkpoint prerequisites and cloud transfer adapters. It excludes the separate
Cloudflare CPU-job experiments, secrets, local evidence and dataset/model bytes.
All 134 tests passed in the isolated publication checkout with the already
acquired official episode enabled. Without that local source, 132 pass and two
integration cases skip. This run uses the existing CPU environment and downloads
no model or dataset. It does not rerun the official LeRobot loader.

The transfer implementation passed independent structure, correctness and security
reviews after fixes for duplicate transfer, temporary path collisions, metadata
bounds, report hash binding and teardown error handling. Publication scope and
the new CPU workflow were independently checked; no remaining blocker was found.
GitHub CI pins action commits, grants read-only repository permissions, and runs
CPU tests under a 15-minute limit with offline model settings.

The previous native GPU update produced a verified immutable checkpoint in R2.
Its fresh verifier did not run because the shared GPU lifetime expired during
the Mac relay. The new adapter separates CPU transfers from GPU lifetimes and
requires independent stop checks. Mounted storage, updated image validation,
fresh-container exact velocity equality and live teardown remain unverified.
See [cloud transfer](cloud-transfer.md) and [native smoke](gpu-smoke.md) for scope
and evidence. No paid run is implied by these local tests or this publication.

## Executable reload-only preparation

The original 134-test publication includes every reviewed transfer file, both
Modal and Runpod adapters, and the complete required checkpoint/storage closure.
The prior 164-test working-directory suite additionally ran 30 unrelated cases:
seven CPU-job tests, four job-budget tests, twelve live-key helper tests, and
seven provider-helper tests. Those excluded cases contained the prior SDK skip;
no transfer implementation or transfer test was omitted.

The executable reload-only preparation adds 42 tests. The exact final checkout
ran 176 tests with the acquired source enabled: 175 passed and the optional
pinned-SDK case skipped in the older CPU environment. Eight image tests then
passed in the existing Modal 1.6.1 controller environment, including that case
and actual generated builder resource limits. Offline preparation printed the
plan/code hashes with zero env reads or cloud calls. CI installs the small
pinned R2 SDK and runs the same suite without downloading source or models.

Independent structure review rejected introducing a generic resource manager or
reusing the full training runner for this fixed reload. Correctness review fixed
the pinned CLI billing schema and durable retention of the new hold through
delayed billing/startup failure. Security review narrowed the actual temporary
JWT report grant to the approved `objects/` subtree; independent recheck found
no surviving blocker. Nested deadlines preserve the whole-attempt limit, and
owned cleanup handles partial App startup and refuses unsafe Volume adoption.
The live image build, scoped sessions, mounted barriers, fresh CUDA comparison
and provider teardown remain execution gates. See [reload-only](reload-only.md).
