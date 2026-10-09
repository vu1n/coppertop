import { test } from "node:test";
import assert from "node:assert/strict";
import { build } from "esbuild";
import { Miniflare, convertV4MiniflareOptions } from "miniflare";
import { mkdtemp, rm } from "node:fs/promises";
import { tmpdir } from "node:os";
import { join } from "node:path";
import { createHash } from "node:crypto";
import { isRecord } from "./contracts.ts";

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
    const content = JSON.stringify({result:"synthetic-valid"});
    const digest = createHash("sha256").update(content).digest("hex");
    const artifact = { key:`runs/one/${digest}.json`,digest,content };
    const put = (value: unknown) => provider.dispatchFetch("http://provider/artifact/put",{method:"POST",body:JSON.stringify(value)});
    assert.equal((await put(artifact)).status,200);
    assert.equal((await put({...artifact,content:"replaced"})).status,409);
    assert.equal((await put({...artifact,key:"../escape.json"})).status,400);
    const record = (id:string,event:string) => provider.dispatchFetch("http://provider/record",{method:"POST",body:JSON.stringify({id,event})});
    await record("a-b","c"); await record("a-b","c"); await record("a","b-c");
    await provider.dispose(); provider = new Miniflare(options);
    const records = await (await provider.dispatchFetch("http://provider/observe")).json() as Array<{kind:string;subjectId?:string;event?:string;value:unknown}>;
    assert.equal(records.filter(r=>r.kind==="call" && isRecord(r.value) && r.value.runId==="one").length,1);
    assert.equal(records.find(row=>row.subjectId==="a-b" && row.event==="c")?.value,2);
    assert.equal(records.find(row=>row.subjectId==="a" && row.event==="b-c")?.value,1);
    const persisted = await (await provider.dispatchFetch("http://provider/artifact/read",{method:"POST",body:JSON.stringify({key:artifact.key})})).json() as {content:string;digest:string};
    assert.equal(persisted.content,content); assert.equal(persisted.digest,digest);
    const disabled = await provider.dispatchFetch("http://provider/disable", {method:"POST",body:JSON.stringify({generation:"one"})});
    assert.equal(disabled.status,200);
    const late = await provider.dispatchFetch("http://provider/spawn", {method:"POST",body:JSON.stringify({runId:"one",generation:"one"})});
    assert.equal(late.status,409);
  } finally { await provider.dispose(); await rm(dir,{recursive:true,force:true}); }
});
