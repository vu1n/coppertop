#!/usr/bin/env bash
set -euo pipefail
repo_root=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/../.." && pwd)
upstream="$repo_root/.local/upstream/effect"
source_commit=aa48e3fac02d43bf7825d7005968586d26a79ae3
mkdir -p "$repo_root/.local/upstream"
if [[ ! -d "$upstream/.git" ]]; then
  git clone --no-checkout https://github.com/Effect-TS/effect.git "$upstream"
  git -C "$upstream" fetch origin "$source_commit"
  git -C "$upstream" checkout --detach "$source_commit"
fi
[[ $(git -C "$upstream" rev-parse HEAD) == "$source_commit" ]] || { echo 'Wrong upstream source commit' >&2; exit 1; }
cd "$upstream"
npm exec --cache "$repo_root/.local/npm-cache" --yes --package=pnpm@11.20.0 -- pnpm \
  --filter @effect/platform-cloudflare... --filter @effect/vitest... install --frozen-lockfile \
  --store-dir "$repo_root/.local/pnpm-store"
# Leave upstream manifests, lockfile, source and patches untouched.
bash "$repo_root/cloud-controller/lane-b/run.sh" --prepare-only
