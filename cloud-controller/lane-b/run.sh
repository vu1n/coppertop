#!/usr/bin/env bash
set -euo pipefail
repo_root=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/../.." && pwd)
upstream="$repo_root/.local/upstream/effect"
[[ $(git -C "$upstream" rev-parse HEAD) == aa48e3fac02d43bf7825d7005968586d26a79ae3 ]] || exit 1
git -C "$upstream" diff --quiet && git -C "$upstream" diff --cached --quiet || {
  echo 'Upstream tracked source, lockfile or patches differ from the pinned commit' >&2
  exit 1
}
destination="$upstream/packages/platform/cloudflare/test/integration/coppertop"
mkdir -p "$destination"
cp "$repo_root/cloud-controller/lane-b/worker.ts" "$destination/worker.ts"
cp "$repo_root/cloud-controller/lane-b/runtime.mjs" "$destination/runtime.mjs"
cp "$repo_root/cloud-controller/lane-b/tsconfig.json" "$destination/tsconfig.json"
mkdir -p "$destination/shared"
cp "$repo_root/cloud-controller/shared/contracts.ts" "$destination/shared/contracts.ts"
cp "$repo_root/cloud-controller/shared/domain.ts" "$destination/shared/domain.ts"
if [[ ${1:-} == --prepare-only ]]; then exit 0; fi
cd "$upstream"
export COPPERTOP_SHARED="$repo_root/cloud-controller/shared"
if [[ ${1:-} == --typecheck ]]; then
  node node_modules/typescript/lib/tsc.js -b packages/platform/cloudflare/tsconfig.json
  node node_modules/typescript/lib/tsc.js -p "$destination/tsconfig.json" --noEmit
  exit 0
fi
if [[ ${1:-} == --strict ]]; then export COPPERTOP_STRICT_GATES=1; shift; fi
node --test --test-concurrency=1 "$@" "$destination/runtime.mjs"
