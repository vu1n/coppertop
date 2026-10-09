import { build } from "esbuild";
import { Miniflare } from "miniflare";
import { createRequire } from "node:module";
import { dirname, join, relative } from "node:path";
import { pathToFileURL } from "node:url";

// run.sh copies this wrapper into the pinned upstream workspace; imports resolve there.
const shared = process.env.COPPERTOP_SHARED;
if (!shared) throw new Error("COPPERTOP_SHARED must point to the approved Coppertop shared fixture");
const root = dirname(dirname(shared));
const { evidence } = await import(pathToFileURL(join(shared, "evidence.ts")).href);
const { nativeEmulatorControls } = await import(pathToFileURL(join(shared, "native-emulator-probe.mjs")).href);
const require = createRequire(import.meta.url);
const miniflareRoot = dirname(require.resolve("miniflare/package.json"));
const sourceFile = join(miniflareRoot, "dist/src/workers/workflows/binding.worker.js");
const miniflareRequire = createRequire(join(miniflareRoot, "package.json"));
nativeEmulatorControls({
  Miniflare, build, lane: "lane-b", shared, compatibilityDate: "2026-08-01", sourceFile,
  options: (base, persistence) => ({ ...base, durableObjectsPersist: persistence }),
  provenance: { ...evidence(root, [
    "cloud-controller/shared/native-emulator-worker.ts",
    "cloud-controller/shared/native-emulator-probe.mjs",
    "cloud-controller/shared/provider-worker.ts",
    "cloud-controller/shared/contracts.ts",
    "cloud-controller/shared/evidence.ts",
    "cloud-controller/lane-b/raw-native.test.mjs",
    ".local/upstream/effect/pnpm-lock.yaml",
    ".local/upstream/effect/packages/platform/cloudflare/package.json",
    relative(root, join(miniflareRoot, "package.json")),
    relative(root, sourceFile), relative(root, miniflareRequire.resolve("workerd/package.json")),
  ], process.env.COPPERTOP_TEST_COMMAND ?? "cloud-controller/lane-b/run.sh --raw-native"),
  emulatorPins: {
    miniflare: require("miniflare/package.json").version,
    workerd: miniflareRequire("workerd/package.json").version,
    compatibilityDate: "2026-08-01",
  } },
});
