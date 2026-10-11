# Clef source recovery — draft integration input

This directory packages weight-free provenance for 16 files recovered from the
existing Coppertop Mac checkout. `source-provenance.json` records their exact
SHA256 values, sizes, old checkout HEAD, and the current remote base used here.
The recovered files were uncommitted; the old HEAD alone does not identify them.
The original checkout was not changed. This is a source migration, not a runtime
validation, deployment, model evaluation, or merge-ready integration.

`model-release.json` pins the actual public Cloudflare Clef Flash release and
its official joint-schema head. The generic smoke head is not a substitute.
Weights and upstream Python reference code are not vendored. The two unmodified
upstream configuration files are supplied with `LICENSE.upstream` (Apache-2.0).
Retrieve any future model assets from the pinned revision and verify its recorded
hashes. The cached image ID is historical evidence; its current availability was
not queried during recovery. No model or data downloads ran during migration.

`dev-archive-identities.json` records receipt/archive identities only. Both later
DEV1150 archives were successfully published and their existing local tar bytes
were rehashed during inventory. This does not constitute a new R2 readback.
The earlier failed attempt retained only 9,194 bytes of metadata; its lost archive
must not be confused with either later successful execute16/execute8 archive.
No recordings, tar files, raw input JSON/NPZ, runtime ZIP, consumed launcher,
credentials, or assistant/session material are included here.

## Integration boundaries

- The recovered docs are exact historical files. Their first completed-cache
  result describes the latest successful benchmark; lower chronological sections
  retain obsolete "not executed"/approval statements. Reconcile those sections
  in the integration lane rather than treating them as current run instructions.
- No Clef simulator intervention has run. The completed same-GPU benchmark used
  saved DEV inputs and the official joint head. It established model coexistence
  and latency, not intervention benefit, learned utility, or held-out quality.
- The benchmark accepts official four-decimal probabilities explicitly;
  `GatePolicy` still uses strict default validation. Resolve this boundary before
  using self-hosted Clef responses in the live gate.
- Cloud evaluation owns dependency/runtime parity, runnable input/configuration
  plumbing, and test execution. The recovered cache builder still expects its
  explicit `/opt/clef-build` inputs; this PR does not include a launch controller.
- `mechanism_policy.serve` has only a bounded optional lifetime parameter added:
  default 240 seconds is preserved; the recovered benchmark selects 450 seconds.
  No other existing cloud/evaluator modules were overwritten.
- DEV seeds 8, 9, 12, 15, 23 and 1150 are not held out. Existing cadence notes also
  exclude 1151. Keep initialization groups together when deriving examples.

Review this draft against current private main. Do not restore the old checkout
wholesale or merge before the integration lane resolves its runtime/test gates.
