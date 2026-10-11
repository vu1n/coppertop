# Modal CPU run — 2026-10-11

The single approved synthetic CPU seam **passed** in workspace `vuluan`, environment `main`. The completion returned the exact 32-byte payload; the second input started in the same container and was cancelled. Independent cleanup and a final read-only check confirmed the App stopped with zero containers and zero Sandboxes.

The [machine-readable summary](modal-cpu-run-2026-10-11.json) preserves the sanitized identities, source hashes, recorded timestamps, and original evidence hashes.

| Binding | Verified value |
| --- | --- |
| Executed commit | `46701a30b31713d0393d069df7427daff875217d` |
| Executed tree | `4ceba9cab6646eecca59ded6275f075c5b49e001` |
| Plan SHA-256 | `49b4b66c7fc46504ddc151256d8e7f7b2a9eba35183d94152b0f0d3aa094057a` |
| Modal SDK | `1.6.1` |
| App | `ap-ghuh8owVffINshUVBnVctG` |
| Prebuilt image | `im-GYYHaMJMKETlbgCvh62lOr` |
| Shared container | `ta-01M4MHYGFCG99V7M7ZZJ42P7QR` |
| Synthetic payload SHA-256 | `630dcd2966c4336691125448bbb25b4ff412a49c732db2c8abc1b8581bd710dd` |

The image was a verified global `debian_slim` cache hit with Python `3.12.10` and builder `2025.06`. Its immutable ID was journaled and independently checked before either input. No custom build, cache bypass, force build, persistent deployment, GPU, Volume, or Sandbox was requested. The App ID was journaled before image lookup.

The fixed scope was one App, one Function, at most one concurrent container, CPU request/limit `0.125`, memory request/limit `256 MiB`, zero retries, and two sequential inputs. The controller enforced a 180-second execution deadline and 60-second cleanup bound under the approved $0.25 operational allowance. Authentication used verified TLS with certificate and hostname checks. Prior inventory showed no Coppertop Apps or running containers.

| Recorded event (UTC) | Timestamp |
| --- | --- |
| Readiness check | `2026-10-11T04:00:29Z` |
| Execution start | `2026-10-11T04:01:10.659827Z` |
| Execution deadline | `2026-10-11T04:04:10.659827Z` |
| Cleanup receipt recorded | `2026-10-11T04:01:32Z` |
| Final verification started | `2026-10-11T04:02:00Z` |

The first call, `fc-01M4MHYG8K20NKXN7SQCN1W472`, returned the payload `bytes(range(32))`; its identity, hash, length, source hash, and local artifact readback were validated. The second call, `fc-01M4MHYTAV97ADYSSW9NN035Y7`, emitted a matching start marker in the same container. Cancellation was acknowledged, and that container was absent before App stop and within the 20-second hold bound. Native terminal state was not asserted.

All **34 focused local tests** in [test_modal_seam.py](../tests/test_modal_seam.py) passed for this exact source. They cover the actual pinned SDK global-image guard and App journal, source/approval refusal, exact receipt validation, cancellation, and independent cleanup. These offline tests are separate from the successful live observations above. The [controller instructions](../cloud-controller/modal-seam/README.md) describe the fixed scope and recovery path.

Actual billed cost and the provider invoice ceiling remain unverified. Exact per-input timestamps were not recorded. The container concurrency cap does not guarantee one lifetime allocation, although both observed inputs used the same container. A whole-executor loss between App creation and identity journaling remains a general cleanup limitation; no unknown App identity occurred here. This run does not establish orchestration, Cloudflare/R2 integration, managed recovery, GPU execution, or durable remote payload storage.

The original detailed evidence remains unchanged in the execution workspace. This report authorizes no additional execution.
