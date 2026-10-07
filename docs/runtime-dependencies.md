# Runtime dependency admission

The CPU retest `diag-a69177a10d6fe977` passed pinned checkout checks and
failed importing `idna` through Transformers → Hugging Face Hub → Requests.
No GPU, model loading, R2 access or Volume was started. Its exact App
`ap-7rcNwkAk2s32xOFvzEaKs2` was independently verified stopped with zero
active containers and sandboxes. The reported cost was not finalized; retain
the entire $0.10 hold. Conservative cumulative holds are $48.06459248 against
the approved $48.07 ceiling.

## Verified recipe defect and remaining uncertainty

The historical recipe created `/work/sim` separately, installed editable
RoboCasa/RoboSuite and their transitive dependencies with unlocked resolution,
and never ran its full dependency check. Later alignment installed exact
overlay pins into `/opt/conda` only. Thus a passing native image gate did not
establish simulator dependency completeness. Why `idna` disappeared from the
current simulator environment is still unknown; an old freeze listing it is
not current installation evidence.

`requirements-simulator.lock` records the entire historical simulator closure:
128 freeze pins plus historically observed `setuptools==65.5.0`, needed by
TensorBoard. Source editables are checked separately at the pinned revisions
in `performance_eval.SOURCE_PINS`. Native overlay and R2 locks retain all
working model/CUDA versions. They are overlays on the immutable GPU image,
not a complete reinstallable GPU environment.

`containers/simulator-dependency-audit.json` records official release metadata
hashes, active requirements and recursively activated extras for Python 3.11
Linux x86_64. Official CPU Torch/Torchvision wheel metadata sidecars were read;
no wheels or models were downloaded. Every declared simulator dependency is
present and compatible in the proposed lock. Both pinned official source
requirements matched the historical cached files.
Tests validate the recorded active edges against the lock; they do not
independently refresh official metadata or prove the extraction omitted none.

The native audit checks 75 known exact historical/overlay versions with no
known active constraint conflicts. Some Conda freeze entries omit versions;
wheel/setuptools are omitted by freeze. A complete exact native base lock
cannot be established from those records. Current installed inventory and
native `pip check` remain live validation gates.

## Admission before GPU

Both native and simulator gates run `pip check` in their actual interpreters,
offline with arbitrary manager output suppressed. Simulator admission checks
all 129 pins before importing libraries. It then imports runtime modules,
including Requests/HF/Transformers, CPU Torch, MuJoCo, video/image libraries
and pinned simulator packages. Metadata cannot prove module files or shared
library ABI are intact. Both OpenCV distributions supply `cv2`; metadata
compatibility does not prove their shared-file ownership is correct.

Every missing/different simulator pin is returned together as bounded public
package/version metadata. Import failures keep module name and sanitized
trace locations. Raw subprocess output and exception messages remain omitted.
Reviewed code and the simulator lock are mounted and hashed in approval
identity; cached source cannot silently keep the earlier 64-pin parser.
Full native model/CUDA admission still uses the existing processor, snapshot
and ABI gates. CPU tests and metadata audits are not remote loader or render
execution.

The Docker and Modal native overlay recipes now both use exact native+R2
pins with `--no-deps`, binary wheels and an explicit package index, followed
by `pip check`. `--no-deps` is safe only alongside the verified inherited
base and the full live closure checks; it is not a dependency resolver.

## Executable CPU repair and validation proposal

`tools/run_cpu_image_repair.py` prepares the exact approval plan offline. Its
`--execute --approved-plan-sha256` mode is for a separately approved single
attempt; preparation, tests and publication do not authorize that mode.
No repair/build/retry has run. Retained holds are $48.06459248 against the
currently approved $48.07 ceiling, leaving $0.00540752.

The tool first runs the cached native interpreter's active dependency closure,
30 overlay pins, `pip check`, imports and CUDA ABI checks on CPU. It then checks
both source editables' revisions/cleanliness and all 129 simulator versions.
It rejects any changed version or missing protected/unrelated package before
network or installation. The 124-package repair allowlist follows audited
runtime dependencies; five unrelated historical service packages are excluded.
Missing approved wheels plus `idna==3.10` are installed from exact official
wheel filenames, sizes and SHA-256 hashes. Official PyPI JSON resolves only
the already bound artifact; redirects, proxies and alternate hosts are rejected.
This is a small repair, not a complete reinstall of the historical freeze.

`gym==0.26.2`, `evdev==2.0.0`, CPU Torch/Torchvision and source editables must
remain inherited. Downloads are limited to 32 MiB compressed wheels and 64 MiB
unpacked wheel contents. A larger missing closure fails before download.
Temporary wheels are removed before capture; seven hash-bound reviewed runtime
files are delivered by SDK entrypoint mount and serialized bytes, with no
additional COPY build. The original image, model/assets and native stack stay
intact. A new immutable image is recorded only after validation; the GPU image
selector is never changed automatically.

The single build and validation function use hard limits of two CPU cores and
4 GiB RAM, no GPU, secrets, R2, Volumes, model/asset downloads or automatic
retries. Build execution/startup are each limited to 600 seconds; validation
execution/startup to 900/300 seconds. The controller has a 3,000-second deadline,
then at most 60 seconds context exit and 180 seconds exact-App stop/audit.
Allowing another 60 seconds builder drain overlap gives a conservative
3,300-second compute envelope. Outbound interface counters stop build stages
above 768 MiB; the 1 GiB network allowance includes 128 MiB estimated new image
layer traffic and 128 MiB bootstrap/protocol/overrun allowance. Image capture
traffic is provider-managed, so this is an estimated allowance, not a provider
dollar cap or a finalized bill.

At [Modal's published pricing](https://modal.com/pricing), using the maximum
1.75 [regional multiplier](https://modal.com/docs/guide/region-selection),
compute is estimated at $0.202587 plus $0.04 for 1 GiB
[network egress](https://modal.com/docs/guide/network-egress-billing): $0.242587.
Propose a **$0.25 additional reservation and $48.32 cumulative ceiling**;
retaining every previous hold gives $48.31459248. This replaces the earlier
incomplete $0.20/$48.27 proposal by accounting for the full controller, cleanup,
image/network allowance and margin. Neither proposal has been approved.
No credits or release of existing holds are assumed.

Validation reruns the native gate and the existing complete simulator pin,
`pip check`, import, source and cached-assets CPU gate with network blocked.
A strict receipt binds those checks to the plan and new image. It does not
instantiate the model, execute CUDA or reset/render a simulator. Metadata and
local tests remain insufficient to establish live import/ABI integrity.
Any unexpected native base, source, simulator version or footprint stops the
attempt without broader repair. The owned App is stopped and audited even if
context exit fails; the full reservation remains held.

The first remaining gate is new explicit approval of the exact prepared hash
and budget, followed by live native/simulator validation. GPU evaluation needs
its own later approval and a passing simulator gate. Paired real RoboCasa
rollouts remain **0/4**.
