# Coppertop

RunPod's local full-rollout controller, portable runtime export recipe, evidence
handoff and remaining approval/validation gates are in [RunPod execution](docs/runpod-execution.md).

`from coppertop import chords`

Chords is the composable robotics data layer: one native record, many reproducible model views. This first milestone imports one already acquired NVIDIA human demonstration into a local immutable object store and exports LeRobot v2.1 for the official `lerobot==0.3.3` loader. No cloud infrastructure is provisioned.

## Reproduce locally

Python 3.11 is used. Core export dependencies are pinned in `pyproject.toml`. The optional official-loader verification environment uses `requirements-loader.txt`; install that list, then `lerobot==0.3.3 --no-deps`. The runtime subset intentionally excludes policy/training, device-control and dashboard dependencies. It executes the installed, unmodified official dataset loader, with the PyAV backend. It is not a complete LeRobot training installation. The measured environment is about 692 MiB; no model weights or extra datasets are needed.

```sh
python -m venv .venv
.venv/bin/python -m pip install -r requirements-loader.txt
.venv/bin/python -m pip install --no-deps lerobot==0.3.3
export COPPERTOP_SOURCE=/path/to/training-data
.venv/bin/python -m unittest discover -s tests -v
.venv/bin/python -m coppertop import "$COPPERTOP_SOURCE"
# Substitute the printed immutable release digest:
.venv/bin/python -m coppertop export RELEASE_DIGEST .local/lerobot-v21
HF_HOME=.local/huggingface .venv/bin/python -m coppertop.verify_loader .local/lerobot-v21
```

The importer reads only `sources/official-files.json` and its 13 explicitly listed objects, verifying checksums and lengths. Originals remain unchanged. Dataset bytes, generated exports and environments are ignored by Git. Exports refuse an existing output directory. An interrupted export should be discarded by the operator and recreated in a fresh directory.

See [data contract](docs/data-contract.md), [source attribution](docs/source.md), [secret management](docs/secrets.md), and [verification](docs/verification.md). This is data plumbing, with no training, token/model parity, simulator replay or robot-improvement claim.

The [cloud transfer adapters](docs/cloud-transfer.md) stage immutable objects on
CPU, run bounded GPU work, and publish verified checkpoints to R2. The
[native Xiaomi smoke](docs/gpu-smoke.md) records the completed bounded training
check and the remaining fresh-container reload check. Provider execution needs
separate explicit cost, storage and temporary-access approval.

CPU CI uses fixtures without model or dataset downloads. To include the acquired
real episode locally, set `COPPERTOP_SOURCE` when running the tests above.

The [reload-only launcher](docs/reload-only.md) reuses the saved checkpoint with
one disposable input Volume. Preparation is offline; execution requires the
exact approved plan and current code hashes.

The [paired performance pilot](docs/performance-eval.md) compares the official
model and saved one-update head on two matched CoffeeSetupMug seeds. Its bounded
cloud controller validates cached inputs on CPU before GPU admission. Offline
preparation is available; paid execution requires the exact budget-bound approval.
