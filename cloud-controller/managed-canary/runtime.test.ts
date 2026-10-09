import assert from "node:assert/strict";
import test from "node:test";
import { build } from "esbuild";
import { Miniflare, convertV4MiniflareOptions } from "miniflare";
import { mkdtemp, rm, readFile } from "node:fs/promises";
import { tmpdir } from "node:os";
import { join, resolve } from "node:path";
import { createHash } from "node:crypto";
import type { Ledger, Decision } from "../shared/contracts.ts";
import { MODES, type Envelope } from "./contracts.ts";
import { acceptance, type IndependentSnapshot } from "./report.ts";
import { auditOutput } from "./controller.mjs";

const bundles = Promise.all(
  ["workflow.ts", "provider.ts"].map((file) =>
    build({
      entryPoints: [new URL(file, import.meta.url).pathname],
      bundle: true,
      format: "esm",
      write: false,
      external: ["cloudflare:workers", "cloudflare:workflows", "node:*"],
      conditions: ["workerd", "worker", "browser"],
    }),
  ),
);
const driver = `export default {async fetch(request,env) {
 const path=new URL(request.url).pathname;
 if(path==='/start'){const params=await request.json();return Response.json({id:(await env.CONTROL.create({id:params.suiteId+'-controller',params})).id});}
 if(path==='/start-probe'){const params=await request.json();return Response.json({id:(await env.PROBES.create({id:params.suiteId+'-'+params.mode,params})).id});}
 if(path==='/status'){const body=await request.json();return Response.json(await (await env[body.binding].get(body.id)).status());}
 return env.PROVIDER.fetch(request);
}}`;
async function fixture(milliseconds = 60_000) {
  const dir = await mkdtemp(join(tmpdir(), "ct-managed-canary-"));
  const [control, provider] = await bundles;
  const payload: Envelope = {
    version: 1,
    suiteId: "ct-local-managed",
    buildDigest: "a".repeat(64),
    deadline: Date.now() + milliseconds,
  };
  const bindings = {
    CANARY_SUITE: payload.suiteId,
    CANARY_BUILD: payload.buildDigest,
    CANARY_DEADLINE: String(payload.deadline),
  };
  const module = (path: string, contents: string) => [
    { type: "ESModule" as const, path, contents },
  ];
  const options = convertV4MiniflareOptions({
    workers: [
      {
        name: "driver",
        modules: module("driver.mjs", driver),
        compatibilityDate: "2026-09-26",
        serviceBindings: {
          PROVIDER: { name: "provider", entrypoint: "SyntheticProvider" },
        },
        workflows: {
          CONTROL: {
            name: "ct-suite",
            className: "CanaryController",
            scriptName: "controller",
          },
          PROBES: {
            name: "ct-probes",
            className: "CanaryProbe",
            scriptName: "controller",
          },
          OBSERVER: {
            name: "ct-observe",
            className: "CanaryObserver",
            scriptName: "provider",
          },
        },
      },
      {
        name: "provider",
        modules: module("provider.mjs", provider.outputFiles[0]!.text),
        compatibilityDate: "2026-09-26",
        compatibilityFlags: ["nodejs_compat"],
        bindings,
        durableObjects: {
          STATE: { className: "CanaryStore", useSQLite: true },
        },
        workflows: {
          OBSERVER: { name: "ct-observe", className: "CanaryObserver" },
        },
      },
      {
        name: "controller",
        modules: module("controller.mjs", control.outputFiles[0]!.text),
        compatibilityDate: "2026-09-26",
        compatibilityFlags: ["nodejs_compat"],
        bindings,
        serviceBindings: {
          PROVIDER: { name: "provider", entrypoint: "SyntheticProvider" },
        },
        workflows: {
          CONTROL: { name: "ct-suite", className: "CanaryController" },
          PROBES: { name: "ct-probes", className: "CanaryProbe" },
          OBSERVER: {
            name: "ct-observe",
            className: "CanaryObserver",
            scriptName: "provider",
          },
        },
      },
    ],
  });
  const mf = new Miniflare({ ...options, resourcePersistencePath: dir });
  await mf.ready;
  const request = (path: string, body: object = {}) =>
    mf.dispatchFetch(`https://driver${path}`, {
      method: "POST",
      body: JSON.stringify({ ...body, ...payload }),
    });
  const call = async <T>(path: string, body: object = {}) => {
    const response = await request(path, body);
    assert.equal(
      response.status,
      200,
      `${path}: ${await response.clone().text()}`,
    );
    return (await response.json()) as T;
  };
  return {
    mf,
    payload,
    request,
    call,
    close: async () => {
      await mf.dispose();
      await rm(dir, { recursive: true, force: true });
    },
  };
}
const pause = (ms: number) => new Promise((resolve) => setTimeout(resolve, ms));

test(
  "six supported native probes plus controller and independent observer execute with one-shot accounting",
  { timeout: 70_000 },
  async () => {
    const f = await fixture();
    try {
      await f.call("/start");
      let result:
        | {
            status: string;
            output?: {
              supportedGatesPass: boolean;
              gates: Record<string, boolean>;
              hostProcessLoss: string;
              realProviderReady: boolean;
              evidence: {
                payload: Envelope;
                snapshot: IndependentSnapshot;
                ledger: Ledger;
                statuses: Record<string, string>;
              };
            };
          }
        | undefined;
      for (let attempt = 0; attempt < 55; attempt++) {
        result = await f.call<NonNullable<typeof result>>("/status", {
          binding: "CONTROL",
          id: `${f.payload.suiteId}-controller`,
        });
        if (["complete", "errored", "terminated"].includes(result.status))
          break;
        await pause(1000);
      }
      assert.ok(result);
      assert.equal(result.status, "complete", JSON.stringify(result));
      assert.equal(
        result.output?.supportedGatesPass,
        true,
        JSON.stringify(result.output),
      );
      assert.equal(result.output?.hostProcessLoss, "untested");
      assert.equal(result.output?.realProviderReady, false);
      assert.ok(result.output);
      assert.equal(
        auditOutput({ params: f.payload }, result.output).supportedGatesPass,
        true,
      );
      const evidence = result.output.evidence;
      for (const [probe, event, gate] of [
        ["sleep", "sleep-complete", "durableSleep"],
        ["event-timeout", "event-timeout", "eventTimeout"],
      ] as const) {
        const broken = structuredClone(evidence.snapshot);
        broken.observations.find(
          (row) => row.mode === probe && row.event === event,
        )!.at = 0;
        assert.equal(
          acceptance(f.payload, broken, evidence.ledger, evidence.statuses)
            .gates[gate],
          false,
        );
      }
      const missingDelay = structuredClone(evidence.snapshot);
      missingDelay.observations = missingDelay.observations.filter(
        (row) => row.event !== "timeout-delay-start",
      );
      assert.equal(
        acceptance(f.payload, missingDelay, evidence.ledger, evidence.statuses)
          .gates.timeoutRetry,
        false,
      );
      const snapshot = await f.call<IndependentSnapshot>("/provider/snapshot");
      const ledger = await f.call<Ledger>("/ledger/snapshot");
      assert.equal(snapshot.calls.length, 6);
      assert.equal(snapshot.closed, true);
      assert.equal(ledger.grant.consumed, 6);
      assert.ok(
        Object.values(ledger.runs).every(
          (run) =>
            run.held === 0 && run.cost === 1 && run.accounting === "settled",
        ),
      );
      let applicationBytes = Buffer.byteLength(
        JSON.stringify({ snapshot, ledger, controller: result }),
      );
      for (const probe of MODES) {
        const status = await f.call<{
          status: string;
          __LOCAL_DEV_STEP_OUTPUTS?: unknown[];
        }>("/status", {
          binding: "PROBES",
          id: `${f.payload.suiteId}-${probe}`,
        });
        assert.equal(
          status.status,
          probe === "cancel" ? "terminated" : "complete",
        );
        applicationBytes += Buffer.byteLength(JSON.stringify(status));
      }
      const observer = await f.call<{
        output: { snapshot: IndependentSnapshot; hostProcessLoss: string };
      }>("/status", {
        binding: "OBSERVER",
        id: `${f.payload.suiteId}-observer`,
      });
      assert.equal(observer.output.hostProcessLoss, "untested");
      applicationBytes += Buffer.byteLength(JSON.stringify(observer));
      assert.ok(
        applicationBytes < 1_048_576,
        `Application evidence exceeded synthetic bound: ${applicationBytes}`,
      );
      console.log(
        JSON.stringify({
          evidence: "local native execution, not managed recovery",
          applicationBytes,
          report: result.output,
        }),
      );
    } finally {
      await f.close();
    }
  },
);

test(
  "transactional duplicate admission, ambiguous dispatch and body claims preserve lifetime budget",
  { timeout: 20_000 },
  async () => {
    const f = await fixture();
    try {
      const admissions = await Promise.all(
        Array.from({ length: 12 }, () =>
          f.call<Decision>("/ledger/admit", { mode: "control" }),
        ),
      );
      assert.ok(admissions.every((item) => item.ledger.grant.consumed === 1));
      await Promise.all(
        Array.from({ length: 12 }, () =>
          f.call("/ledger/dispatch", { mode: "control" }),
        ),
      );
      const snapshot = await f.call<IndependentSnapshot>("/provider/snapshot");
      assert.equal(
        snapshot.calls.length,
        1,
        "Lost provider reply must never reopen dispatch permission",
      );
      const bodies = await Promise.all(
        Array.from({ length: 12 }, () =>
          f.call<Decision>("/ledger/execute", { mode: "control" }),
        ),
      );
      assert.equal(bodies.filter((item) => item.newPermission).length, 1);
      await f.call("/ledger/cancel", { mode: "control" });
      const denied = await f.call<Decision>("/ledger/dispatch", {
        mode: "control",
      });
      assert.equal(denied.run?.cleanup, "confirmed");
      assert.equal(denied.run?.held, 0);
      assert.equal(
        (await f.call<IndependentSnapshot>("/provider/snapshot")).calls.length,
        1,
      );
      const defaults = await f.mf.getWorker("provider");
      assert.equal(
        (
          await defaults.fetch("https://private.invalid/provider/spawn", {
            method: "POST",
            body: "{}",
          })
        ).status,
        404,
      );
      const bad = await f.mf.dispatchFetch("https://driver/ledger/admit", {
        method: "POST",
        body: JSON.stringify({
          ...f.payload,
          buildDigest: "b".repeat(64),
          mode: "sleep",
        }),
      });
      assert.equal(bad.status, 409);
      assert.equal(
        (await f.request("/ledger/admit", { mode: "arbitrary-provider" }))
          .status,
        409,
      );
    } finally {
      await f.close();
    }
  },
);

test(
  "independent deadline alarm contains synthetic calls without target workflow contact",
  { timeout: 20_000 },
  async () => {
    const f = await fixture(10_000);
    try {
      await f.call("/ledger/admit", { mode: "cancel" });
      await f.call("/ledger/dispatch", { mode: "cancel" });
      await f.call("/ledger/execute", { mode: "cancel" });
      await pause(Math.max(0, f.payload.deadline - Date.now()) + 1000);
      const snapshot = await f.call<IndependentSnapshot>("/provider/snapshot");
      assert.equal(snapshot.closed, true);
      assert.equal(snapshot.calls[0]?.state, "cancelled");
      const ledger = await f.call<Ledger>("/ledger/snapshot");
      assert.equal(
        ledger.runs[`${f.payload.suiteId}-cancel`]?.cleanup,
        "confirmed",
      );
      assert.equal(
        ledger.runs[`${f.payload.suiteId}-cancel`]?.outcome,
        "timed_out",
      );
      assert.equal(
        (await f.request("/ledger/admit", { mode: "control" })).status,
        409,
      );
    } finally {
      await f.close();
    }
  },
);

test("print partial source hashes (provenance record, no runtime behaviour)", async () => {
  const sourceFiles = [
    "contracts.ts",
    "provider.ts",
    "workflow.ts",
    "report.ts",
    "runtime.test.ts",
    "package-lock.json",
  ];
  const records = [];
  for (const file of sourceFiles)
    records.push({
      file,
      sha256: createHash("sha256")
        .update(
          await readFile(resolve(new URL(".", import.meta.url).pathname, file)),
        )
        .digest("hex"),
    });
  console.log(
    JSON.stringify({
      source: records,
      node: process.version,
      mode: "local controls; managed gates not run; host loss untested",
    }),
  );
});

test(
  "receipt replay survives generation fencing and partial cleanup retires admission",
  { timeout: 20_000 },
  async () => {
    const f = await fixture();
    try {
      await f.call("/ledger/admit", { mode: "control" });
      await f.call("/ledger/dispatch", { mode: "control" });
      await f.call("/ledger/execute", { mode: "control" });
      await f.call("/provider/finish", { mode: "control" });
      const first = await f.call<Decision>("/ledger/complete", {
        mode: "control",
      });
      const replay = await f.call<Decision>("/ledger/complete", {
        mode: "control",
      });
      assert.deepEqual(replay.run?.receipt, first.run?.receipt);
      assert.equal(replay.run?.cost, 1);
      assert.equal(replay.run?.held, 0);
      await f.call("/ledger/cleanup");
      assert.equal(
        (await f.request("/ledger/admit", { mode: "sleep" })).status,
        409,
      );
      assert.equal(
        (await f.call<Ledger>("/ledger/snapshot")).grant.revoked,
        true,
      );
    } finally {
      await f.close();
    }
  },
);

test(
  "cancel probe remains waiting beyond the old 20-second race window",
  { timeout: 40_000 },
  async () => {
    const f = await fixture();
    try {
      await f.call("/ledger/admit", { mode: "cancel" });
      await f.call("/start-probe", { mode: "cancel" });
      let ready = false;
      for (let attempt = 0; attempt < 10; attempt++) {
        const snapshot =
          await f.call<IndependentSnapshot>("/provider/snapshot");
        ready = snapshot.observations.some(
          (row) => row.mode === "cancel" && row.event === "wait-start",
        );
        if (ready) break;
        await pause(250);
      }
      assert.equal(ready, true);
      await pause(21_000);
      const status = await f.call<{ status: string }>("/status", {
        binding: "PROBES",
        id: `${f.payload.suiteId}-cancel`,
      });
      // The pinned emulator labels event waits running; managed status may
      // expose waiting. Either must remain live, rather than timing out.
      assert.ok(["running", "waiting"].includes(status.status));
      const snapshot = await f.call<IndependentSnapshot>("/provider/snapshot");
      assert.equal(
        snapshot.observations.some(
          (row) => row.mode === "cancel" && row.event === "probe-complete",
        ),
        false,
      );
      const stopped = await f.call<Decision>("/ledger/cancel", {
        mode: "cancel",
      });
      assert.equal(stopped.run?.cleanup, "confirmed");
    } finally {
      await f.close();
    }
  },
);
