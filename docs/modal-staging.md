# Modal cloud staging adapter

`coppertop.modal_staging.create_functions` registers six local Function definitions.
It does not run an App, deploy, create Volumes or Secrets, or submit inputs.
Use Modal SDK **1.6.1**, existing dedicated input/output Volumes, a reviewed
cached image containing this package/native runtime, and explicit region/run ID.
The unrelated existing `truesight-clm-cache` Volume is not a Coppertop cache.

The factory owns a new App with empty inherited secrets/volumes. CPU Functions
receive only caller-supplied **existing short-lived object-scoped transfer Secret
handles**, supplied as `{'input': [...], 'output': [...]}`. The stage Function
receives only input scope; publish/restage/report Functions receive output scope.
`store_factory(role)` selects the corresponding source or output CAS prefix. The store factory reads those CPU environment variables; never capture
parent keys in callbacks, put them in images, or send them as Function arguments.
GPU Functions have no secrets, block outbound networking, and mount inputs
read-only. Mounts are restricted to the run's subdirectory. Model weights may
remain in the already validated cached image; no fresh model download is implied.

Call order is `stage(input_manifest)`, `train(input_manifest)`,
`publish(output_manifest)`, `restage(checkpoint_key, output_manifest)`, and
`reload(input_manifest, output_manifest, training_container_id)`, then
`publish_reload(report_manifest, verifier_container_id)` after verified GPU stop.
Only manifests, receipt keys, native result reports and container IDs cross the
controller boundary. R2 payload streams run on CPU Functions. Train and reload
use separate single-use GPU Functions; neither waits for the CPU transfer phase.
The fixed reviewed command builders provide native CLI argv without shell use.

Shared staging verifies byte counts/SHA256 and writes readiness markers. Generated
and fetched directories rename locally into their immutable manifest-key location;
`commit_directory` seals them without copying the checkpoint again. The mounted
Function calls `Volume.commit()` before returning a committed receipt. CPU publish
checks the committed stage and publishes the existing native-head receipt last,
with R2 read-back verification; CPU restage checks the receipt's exact artifacts
before committing separate inputs. GPU Functions use the fresh container mount
snapshot; they do not reload read-only inputs. Mounted Volume commit requires
platform access, so `restrict_modal_access` is deliberately not enabled without
a documented exemption for commit RPCs. No parent/R2 credential is supplied. Partial/failed directories remain diagnostic
artifacts and are not silently reused or retried.

Every Function is single-use with zero retries, zero warm minimum/buffer and one
maximum container. CPU transfer timeout defaults to 1,800 seconds; GPU Function
runtime defaults to 600 seconds with native subprocess limited to 480 seconds.
Fresh reload has a separate 120-second subprocess limit. Startup has a separate
300-second limit. Limits do not replace a fresh billing
admission or storage/network budget. Volume bytes (including diagnostic partials
and the extra verified restaging copy), startup/runtime and possible Modal outbound
network charges for publishing to R2 must be included in the next live approval. Modal Volumes are
region-distributed, but requested compute regions still require availability and
approval; incompatible location restrictions must stop before allocation.

A Function return establishes commit acknowledgement, **not stopped-resource
verification**. The caller must read-only verify the returned `MODAL_TASK_ID` has
stopped before advancing from a GPU stage to CPU transfer, and independently
check final teardown. The verifier rejects reuse of the training container ID;
PID inequality is not evidence across containers. The fresh report is sealed and
committed in the output Volume before the verifier exits; its CPU publisher
checks identity, uploads the bounded report and verifies R2 read-back. Metadata identity and reports
must still be checked by the shared orchestration contract.

CPU contract tests use fake SDK/Volumes and small local files. Actual mounted
commit/reload, single-use lifecycle, network restriction, scoped session access,
GPU processor execution, and provider teardown have **not** run for this adapter.
The factory provides no credential generation or automatic live retry path.

References: [Modal Volumes](https://modal.com/docs/guide/volumes),
[Function lifecycle](https://modal.com/docs/guide/container-lifecycle),
[network egress billing](https://modal.com/docs/guide/network-egress-billing).
Function commit semantics deliberately replace the old Sandbox relay path;
[Sandbox files](https://modal.com/docs/guide/sandbox-files) require different
commit handling.

After an approved App starts, bind `stop_verifier(app_id, environment='main',
command=('/path/to/controller-python', '-m', 'modal'))` to the shared runner's
stop-confirmation hook. This runs only read-only `container list --app-id ...
--env ... --json` queries, bounded to 60 seconds, and fails closed on malformed,
wrong-scope, unsuccessful or timed-out responses. Pass only container IDs from
that App's validated phase receipts. Single-use Functions own shutdown; this
callback confirms their absence and never requests allocation or termination.
A separate final app-scoped container listing is still required for residual
resources after failures; individual phase absence is not the final audit.
Pinned CLI JSON field names (`container_id`, `app_id`, `app_name`, `start_time`)
were checked in SDK 1.6.1's `modal/cli/container.py` and the snake-case conversion
in `modal/cli/utils.py`.
No actual container-list command ran in local tests.
