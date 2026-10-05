# Working constraints

Cloud data is foundational; compute is disposable. Preserve immutable raw source bytes, licenses, revision/checksum provenance, native state/action semantics, clocks and coordinates. Chords holds signals/indexes in Parquet and compressed per-camera videos. Schema/encoding compatibility governs physical shards; tasks, splits and quality are metadata/overlays. Never infer success from done.

Version annotations separately; releases and forks are immutable content-addressed manifests. Forks reuse objects. History/crop/reorder/padding/normalization for a model belongs in views. Keep the local ObjectStore seam portable to R2 without provisioning it. Add durable orchestration only when necessary; prefer Absurd/DBOS then. No bespoke platform.

Use `.venv/bin/python -m unittest discover -s tests -v`. Set `COPPERTOP_SOURCE` to the existing bounded official source to enable integration tests; do not download full datasets or model weights. `coppertop.verify_loader` must execute the pinned installed official loader offline. Do not label source inspection/fixtures loader execution. No GPU training here.

Only placeholders in `.env.example`; do not read/copy secret values. Keep data, environments and generated exports in `.local/` or other ignored paths. No paid services, deployments, public publishing, collaborators, or keys without explicit authorization. Preserve original dotspace work.
