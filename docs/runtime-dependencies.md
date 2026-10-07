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

## Smallest proposed CPU repair and validation bundle

No repair/build/retry has been executed or authorized by this change. Current
remaining headroom is only $0.00540752, so it cannot fund another $0.10 hold.

Prepare one CPU image repair and one CPU validation on the exact cached image,
with no GPU, secrets, R2, Volumes, model/asset downloads or automatic retries.
Retain the native CUDA stack, cached model/assets, source editables and CPU
Torch/Torchvision. Compare the entire simulator inventory against the lock.
Fail on version differences until reviewed; install only missing exact pins
plus reinstall the known broken `idna==3.10` wheel. Validate the entire lock,
active metadata closure, both interpreter `pip check` results and imports,
then the existing simulator source/assets CPU gate.

`gym==0.26.2` and `evdev==2.0.0` have no compatible official binary wheels.
Their inherited installations must remain intact. If either, CPU Torch, or
an editable source is absent, stop instead of building/downloading a fallback.
The other selected compatible PyPI wheels total 574,678,742 bytes, excluding
CPU Torch, source-only packages, source editables and extraction overhead.
This is a minimum selected-wheel footprint, not a bound on arbitrary pip
selection. A live repair recipe must bind those exact wheel hashes and sizes
and enforce its aggregate download/disk bounds before execution.

Proposed hard resources: 2 physical CPU cores, 4 GiB RAM, one 600-second repair
build and one 900-second gate, each at most 300 seconds startup plus 60 seconds
drain; no implicit unbounded copy builds. At [Modal's published pricing](https://modal.com/pricing),
using the maximum 1.75 region multiplier, 2,220 seconds costs at most an
estimated $0.1362858 compute plus $0.0000390625 for 1 MiB metadata egress.
Reserve $0.20 additional and preserve every previous hold: $48.26459248 total,
requiring a proposed $48.27 cumulative ceiling. No credits are assumed.
This is a proposal, not permission or a finalized bill.

The executable live bundle remains blocked on binding the exact selected
wheels, proving bounded source delivery without an extra image build, and
new budget authorization. If current inventory reveals a changed native
base or conflicting simulator versions, return the complete findings before
choosing a repair. Successful repair must yield a new immutable image identity
and gate receipt before any separately approved GPU evaluation.
