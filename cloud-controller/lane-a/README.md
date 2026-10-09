# Native Cloudflare lane

Local synthetic spike using effect-cf 0.53.0, native Cloudflare Workflows and the official Effect 4.0.2 SQLite Durable Object adapter. Dependencies and lockfile are isolated from the shared contracts and upstream lane.

```sh
npm ci
npm run typecheck
npm run test:acceptance
```

Two strict autonomous workerd restart gates currently fail. `npm run test:characterize` verifies those known local failures explicitly; its green result is not durability acceptance. See [EVIDENCE.md](EVIDENCE.md) for the passed/failed/not-run matrix and integration boundaries. No deployment configuration, credentials, paid provider access or Python changes are included.
