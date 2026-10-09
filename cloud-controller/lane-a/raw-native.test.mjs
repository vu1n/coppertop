import { build } from "esbuild";
import { Miniflare, convertV4MiniflareOptions } from "miniflare";
import { createRequire } from "node:module";
import { dirname, join, relative } from "node:path";
import { fileURLToPath } from "node:url";
import { evidence } from "../shared/evidence.ts";
import { nativeEmulatorControls } from "../shared/native-emulator-probe.mjs";

const require = createRequire(import.meta.url);
const root = fileURLToPath(new URL("../../", import.meta.url));
const miniflareRoot = dirname(require.resolve("miniflare/package.json"));
const sourceFile = join(miniflareRoot, "dist/src/workers/workflows/binding.worker.js");
const miniflareRequire = createRequire(join(miniflareRoot, "package.json"));
const workerdPackage = miniflareRequire.resolve("workerd/package.json");
nativeEmulatorControls({
  Miniflare, build, lane: "lane-a", shared: join(root, "cloud-controller/shared"),
  compatibilityDate: "2026-09-26", sourceFile,
  options: (base, persistence) => ({
    ...convertV4MiniflareOptions(base), resourcePersistencePath: persistence,
  }),
  provenance: { ...evidence(root, [
    "cloud-controller/shared/native-emulator-worker.ts",
    "cloud-controller/shared/native-emulator-probe.mjs",
    "cloud-controller/shared/provider-worker.ts",
    "cloud-controller/shared/contracts.ts",
    "cloud-controller/shared/evidence.ts",
    "cloud-controller/lane-a/raw-native.test.mjs",
    "cloud-controller/lane-a/package-lock.json",
    relative(root, join(miniflareRoot, "package.json")),
    relative(root, sourceFile), relative(root, workerdPackage),
  ], process.env.COPPERTOP_TEST_COMMAND ?? "node --test --test-concurrency=1 cloud-controller/lane-a/raw-native.test.mjs"),
  emulatorPins: {
    miniflare: require("miniflare/package.json").version,
    workerd: miniflareRequire("workerd/package.json").version,
    compatibilityDate: "2026-09-26",
  } },
});
