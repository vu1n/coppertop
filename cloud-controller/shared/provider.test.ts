import { test } from "node:test";
import assert from "node:assert/strict";
import { build } from "esbuild";
import { Miniflare, convertV4MiniflareOptions } from "miniflare";
import { mkdtemp, rm } from "node:fs/promises";
import { tmpdir } from "node:os";
import { join } from "node:path";

test("independent provider persists an ambiguous spawn through a workerd restart", { timeout: 30_000 }, async () => {
  const dir = await mkdtemp(join(tmpdir(), "coppertop-provider-"));
  const bundle = await build({ entryPoints: [new URL("./provider-worker.ts", import.meta.url).pathname], bundle: true, format: "esm", write: false, external: ["cloudflare:workers"] });
  const options = { ...convertV4MiniflareOptions({
    modules: [{type: "ESModule" as const, path: "provider.mjs", contents: bundle.outputFiles[0]!.text}],
    compatibilityDate: "2026-09-26",
    durableObjects: { PROVIDER: { className: "FakeProvider", useSQLite: true } },
  }), resourcePersistencePath: dir };
  let provider = new Miniflare(options);
  try {
    const lost = await provider.dispatchFetch("http://provider/spawn", {method:"POST",body:JSON.stringify({runId:"one",generation:"one",loseReply:true})});
    assert.equal(lost.status, 503);
    await provider.dispose(); provider = new Miniflare(options);
    const records = await (await provider.dispatchFetch("http://provider/observe")).json() as Array<{kind:string;value:{runId:string}}>;
    assert.equal(records.filter(r=>r.kind==="call" && r.value.runId==="one").length,1);
    const disabled = await provider.dispatchFetch("http://provider/disable", {method:"POST",body:JSON.stringify({generation:"one"})});
    assert.equal(disabled.status,200);
    const late = await provider.dispatchFetch("http://provider/spawn", {method:"POST",body:JSON.stringify({runId:"one",generation:"one"})});
    assert.equal(late.status,409);
  } finally { await provider.dispose(); await rm(dir,{recursive:true,force:true}); }
});
