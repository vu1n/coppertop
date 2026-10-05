# User-managed secrets

The local milestone needs no cloud credentials. `.env.example` contains empty placeholders; `.env` and variants, private keys, credentials directories and local artifacts are ignored. Copy the example locally only if needed. Never put keys in Git or paste them into chat.

GitHub repository/environment **Secrets** can hold credentials for a future authorized workflow; **Variables** are for non-sensitive values. Use the GitHub settings UI and provider dashboards directly. A private repository is not a secret vault and does not make committing keys safe. This project creates no collaborators, workflow, credentials or secret configuration.

For future R2 access, create scoped bucket credentials yourself in Cloudflare and store them in your compute provider's secret store (for example Modal Secrets or RunPod's supported secret mechanism), injecting at worker startup. Prefer narrow permissions and short-lived credentials when available. Store non-secret endpoint/bucket identifiers as variables. Keep actual values user-managed, and rotate in the provider if exposed. R2/compute integrations remain unimplemented and unprovisioned here.
