# R2 archive qualification — 2026-10-11

The approved synthetic qualification **passed** in **20.2 seconds**: one streamed single PUT, full R2 SHA-256 readback, completion receipt verification, and exact-key cleanup. Both owned objects returned HTTP 204 on deletion and HTTP 404 on subsequent HEAD. A fresh local process then verified the archive, all 1,939 archived payloads, and preserved source files.

The [sanitized JSON closeout](r2-qualification-2026-10-11.json) records source bindings, limits, observations, SDK pins, and hashes of the unchanged original private evidence. It contains no credentials, raw archive inputs, or executor configuration.

| Binding | Value |
| --- | --- |
| Executed base commit | `9b842bdf181b13019407bc5f798b74e874ee7e28` |
| Commit capturing exact tested/executed runtime | `75361c1d49d6d89de2ff315f57b3d1ffdc09e00c` |
| Runtime implementation SHA-256 | `ca1fa4f6af7d2ed0eea20c5247112a8ef524708a70748a619cdb0c761b11b7ec` |
| Original approved plan SHA-256 | `cac76796ec80789393050ec5fc69a5dfa22f7794e3413eeda8cbe71ecc2b5046` |
| Fresh executed plan SHA-256 | `b8e4a3081f4b79ce37f607a25350ae7bec636737ce23a907d025080af95a7f1e` |
| Archive SHA-256 | `973e055acb9f5680c90447c3b501a5b161399079622cb52aa88bfd583c04379e` |
| Archive size / source files | 156,098,560 bytes / 1,939 |
| Completion receipt SHA-256 | `29fe1f51ba4ada1856f4d8053d0baa192f2dbaf67a9f23ebdba21f955b51602a` |

Only the existing harness was changed: preparation accepts positive integer limits at or below the original 600-second and 32-request ceilings, binds them to the plan hash, and execution enforces the plan's timeout and request cap. Boolean, fractional, zero, and above-ceiling values are rejected. Cleanup retains its separate 300-second / 16-request bounds. **23 focused tests passed** against the identical source captured in the harness commit, including timeout propagation, request dispatch refusal, approval validation, and failure cleanup. The full repository suite was not run for this change; independent code review remains pending.

| Operational limit | Approved remaining cap | This run |
| --- | ---: | ---: |
| Publication requests | 28 | 12 |
| Publication deadline | 596 seconds | completed within limit |
| Cleanup requests | 16 | 4 |
| Cleanup deadline | 300 seconds | completed within limit |
| New remote writes | 157,351,936 bytes | 156,099,359 bytes |
| Automatic retries | 0 | 0 |
| Cloud compute | $0 | $0 |

The 28-request publication cap accounts for four previously consumed request slots, including the latest HTTPS reachability HEAD. Aggregate publication budget use is 16 of 32 slots; cleanup used 4 of 16. The $5 R2 amount is an operational allowance. **Actual billed cost and a provider-enforced invoice ceiling remain unknown.**

For local reproduction, check out the captured harness commit and run these commands from the repository root. Dependency installation uses the official registry and an isolated environment; preparation and tests make no provider calls.

```sh
python -m venv .local/r2-qualification-venv
.local/r2-qualification-venv/bin/python -m pip --isolated --disable-pip-version-check install --index-url https://pypi.org/simple --only-binary=:all: --no-cache-dir -r requirements-r2.txt
.local/r2-qualification-venv/bin/python -m unittest discover -s tests -p 'test_archive*.py' -v
.local/r2-qualification-venv/bin/python -m coppertop.archive_validation --prepare .local/archive-validation-fresh --execution-seconds 596 --publication-requests-max 28
```

Preparation creates a fresh path, prefix, interpreter binding, and plan hash. The completed attempt and its approval cannot be resumed or reused. A further live invocation requires fresh authorization bound to its new plan and current remaining budget.

The completed invocation used the existing entrypoint with private stdin as its supported configuration file. The following command form is recorded for reproducibility; it is **not authorization to execute again**:

```sh
.local/r2-qualification-venv/bin/python -m coppertop.archive_validation --execute .local/archive-validation-fresh --approval .local/archive-validation-fresh/approval.json --env-file /dev/stdin
```

Its stdin was supplied privately from the already injected `CLOUDFLARE_API_TOKEN`, `CLOUDFLARE_ACCOUNT_ID`, and `CLOUDFLARE_R2_URL` variables using an in-memory subprocess pipe. Values were never echoed or stored in a credential file. The publisher received only temporary scoped credentials; cleanup received a separate capability for the two exact journaled keys.

The unchanged original archive, source, approval, journals, telemetry, and detailed outcome remain in ignored private local storage. No provider call followed cleanup. The verification establishes observed absence; it cannot rule out a late accepted write. It does not establish large multipart completion, durable retained remote payload storage, or cloud-compute qualification. The draft closeout grants no additional execution or merge authority.
