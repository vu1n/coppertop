import { test } from "node:test";
import assert from "node:assert/strict";
import { build } from "esbuild";
import { Miniflare, convertV4MiniflareOptions } from "miniflare";
import { mkdtemp, rm } from "node:fs/promises";
import { tmpdir } from "node:os";
import { join } from "node:path";
import { createServer, type Server } from "node:http";
import { createHash } from "node:crypto";
import { createRequire } from "node:module";
import { dirname, relative } from "node:path";
import { fileURLToPath } from "node:url";
import { evidence } from "../shared/evidence.ts";
import type { Decision, Ledger, RunSpec } from "../shared/contracts.ts";
import type { Controls } from "./schema.ts";

const characterize = process.env.COPPERTOP_CHARACTERIZE_RUNTIME_GAPS === "1";
const repoRoot = fileURLToPath(new URL("../../", import.meta.url));
const require = createRequire(import.meta.url);
const miniflareRoot = dirname(require.resolve("miniflare/package.json"));
console.log(JSON.stringify({ runtimeProvenance: evidence(repoRoot, [
  "cloud-controller/lane-a/worker.ts", "cloud-controller/lane-a/schema.ts",
  "cloud-controller/lane-a/runtime.test.ts", "cloud-controller/lane-a/package.json",
  "cloud-controller/lane-a/package-lock.json", "cloud-controller/shared/contracts.ts",
  "cloud-controller/shared/domain.ts", "cloud-controller/shared/provider-worker.ts",
  "cloud-controller/shared/evidence.ts",
  relative(repoRoot, join(miniflareRoot, "package.json")),
  relative(repoRoot, join(miniflareRoot, "dist/src/workers/workflows/binding.worker.js")),
  relative(repoRoot, require.resolve("workerd/package.json")),
], process.env.COPPERTOP_TEST_COMMAND ?? `npm run test:${characterize ? "characterize" : "acceptance"}`),
  mode: characterize ? "negative characterization" : "strict acceptance" }));
const gapLabel = characterize
  ? "KNOWN GAP CHARACTERIZATION (not acceptance): "
  : "";
const pause = (ms: number) =>
  new Promise<void>((resolve) => setTimeout(resolve, ms));
const digest = "a".repeat(64);
const spec = (runId: string): RunSpec => ({
  schemaVersion: 1,
  runId,
  commandId: `cmd-${runId}`,
  idempotencyKey: `key-${runId}`,
  principal: "synthetic-operator",
  grantId: "synthetic-grant",
  preset: "fake-v1",
  sourceDigest: digest,
  inputDigest: digest,
  deployment: "fake-deployment-v1",
  reservation: 10,
  deadline: Date.now() + 60_000,
  engine: "native-cf",
  definitionVersion: 1,
});
type Observation = {
  kind: string;
  id: string;
  subjectId?: string;
  event?: string;
  value: unknown;
};
const summarizeStatus = (value: unknown) => {
  const record = value as {
    status: string;
    output: unknown;
    __LOCAL_DEV_STEP_OUTPUTS?: unknown[];
  };
  return {
    status: record.status,
    output: record.output,
    persistedStepCount: record.__LOCAL_DEV_STEP_OUTPUTS?.length,
  };
};
const laneBuild = (epoch: number) =>
  build({
    entryPoints: [new URL("./worker.ts", import.meta.url).pathname],
    bundle: true,
    format: "esm",
    write: false,
    external: ["cloudflare:workers", "cloudflare:workflows", "node:*"],
    conditions: ["workerd", "worker", "browser"],
    define: { COPPERTOP_DEPLOYMENT_EPOCH: String(epoch) },
  });
const builds = Promise.all([
  laneBuild(1),
  build({
    entryPoints: [
      new URL("../shared/provider-worker.ts", import.meta.url).pathname,
    ],
    bundle: true,
    format: "esm",
    write: false,
    external: ["cloudflare:workers"],
  }),
  laneBuild(2),
]);
async function fixture(firstCancelUnavailable = false, spawnAtFence = false) {
  const dir = await mkdtemp(join(tmpdir(), "coppertop-native-"));
  const [laneBundle, providerBundle, upgradedBundle] = await builds;
  const providerOptions = {
    ...convertV4MiniflareOptions({
      modules: [
        {
          type: "ESModule" as const,
          path: "provider.mjs",
          contents: providerBundle.outputFiles[0]!.text,
        },
      ],
      compatibilityDate: "2026-09-26",
      durableObjects: {
        PROVIDER: { className: "FakeProvider", useSQLite: true },
      },
    }),
    resourcePersistencePath: join(dir, "provider"),
  };
  const provider = new Miniflare(providerOptions);
  let address = (await provider.ready).host;
  let proxy: Server | undefined;
  let injectedCancels = 0;
  let injectedSpawns = 0;
  if (firstCancelUnavailable || spawnAtFence) {
    proxy = createServer(async (request, response) => {
      if (
        firstCancelUnavailable &&
        request.url === "/cancel" &&
        injectedCancels++ === 0
      ) {
        await provider.dispatchFetch("http://provider/record", {
          method: "POST",
          body: JSON.stringify({ id: "cleanup-retry", event: "cancel-outage" }),
        });
        response.writeHead(503, { "content-type": "application/json" });
        response.end(
          JSON.stringify({ error: "synthetic first cancel unavailable" }),
        );
        return;
      }
      const chunks: Buffer[] = [];
      for await (const chunk of request) chunks.push(Buffer.from(chunk));
      const body = Buffer.concat(chunks);
      if (
        spawnAtFence &&
        request.url === "/disable" &&
        injectedSpawns++ === 0
      ) {
        const fence = JSON.parse(body.toString()) as { generation: string };
        const late = await provider.dispatchFetch("http://provider/spawn", {
          method: "POST",
          body: JSON.stringify({
            runId: "fence-race",
            generation: fence.generation,
          }),
        });
        assert.equal(late.status, 200);
        await provider.dispatchFetch("http://provider/record", {
          method: "POST",
          body: JSON.stringify({ id: "fence-race", event: "late-at-fence" }),
        });
      }
      const upstream = await provider.dispatchFetch(
        `http://provider${request.url}`,
        { method: request.method, body: body.length ? body : undefined },
      );
      response.writeHead(upstream.status, {
        "content-type": "application/json",
      });
      response.end(Buffer.from(await upstream.arrayBuffer()));
    });
    await new Promise<void>((resolve) =>
      proxy!.listen(0, "127.0.0.1", resolve),
    );
    const socket = proxy.address();
    assert(socket && typeof socket !== "string");
    address = `127.0.0.1:${socket.port}`;
  }
  const makeOptions = (contents: string) => ({
    ...convertV4MiniflareOptions({
      modules: [
        {
          type: "ESModule" as const,
          path: "lane.mjs",
          contents,
        },
      ],
      compatibilityDate: "2026-09-26",
      compatibilityFlags: ["nodejs_compat"],
      durableObjects: {
        COORDINATOR: { className: "Coordinator", useSQLite: true },
      },
      workflows: { RUNS: { name: "synthetic-runs", className: "NativeRun" } },
      bindings: { SPIKE_LOCAL_ONLY: "true", EXPECTED_EPOCH: "1" },
      serviceBindings: { PROVIDER: { external: { address, http: {} } } },
    }),
    resourcePersistencePath: join(dir, "lane"),
  });
  const options = makeOptions(laneBundle.outputFiles[0]!.text);
  const upgradedOptions = makeOptions(upgradedBundle.outputFiles[0]!.text);
  let lane = new Miniflare(options);
  await lane.ready;
  return {
    post: async (path: string, body: unknown) =>
      lane.dispatchFetch(`http://lane${path}`, {
        method: "POST",
        body: JSON.stringify(body),
      }),
    providerPost: async (path: string, body: unknown) =>
      provider.dispatchFetch(`http://provider${path}`, {
        method: "POST",
        body: JSON.stringify(body),
      }),
    ledger: async () =>
      (await (await lane.dispatchFetch("http://lane/ledger")).json()) as Ledger,
    observe: async () =>
      (await (
        await provider.dispatchFetch("http://provider/observe")
      ).json()) as Observation[],
    restart: async (upgrade = false) => {
      await lane.dispose();
      lane = new Miniflare(upgrade ? upgradedOptions : options);
      await lane.ready;
    },
    stop: async () => lane.dispose(),
    start: async () => {
      lane = new Miniflare(options);
      await lane.ready;
    },
    submit: async (runId: string, controls: Controls = {}) =>
      lane.dispatchFetch("http://lane/submit", {
        method: "POST",
        body: JSON.stringify({ epoch: 1, spec: spec(runId), controls }),
      }),
    cleanup: async () => {
      await lane.dispose();
      await provider.dispose();
      if (proxy)
        await new Promise<void>((resolve, reject) =>
          proxy!.close((error) => (error ? reject(error) : resolve())),
        );
      await rm(dir, { recursive: true, force: true });
    },
  };
}
async function observed(
  f: Awaited<ReturnType<typeof fixture>>,
  id: string,
  event: string,
  timeout = 10_000,
): Promise<Observation[]> {
  const started = performance.now();
  const expires = Date.now() + timeout;
  let records: Observation[] = [];
  while (Date.now() < expires) {
    records = await f.observe();
    if (
      records.some(
        (r) => r.kind === "observer" && r.subjectId === id && r.event === event,
      )
    ) {
      console.log(
        JSON.stringify({
          observer: `${id}-${event}`,
          observerWaitMs: Math.round(performance.now() - started),
        }),
      );
      return records;
    }
    await pause(100);
  }
  assert.fail(
    `independent observer missing ${id}-${event}; ${JSON.stringify(records)}`,
  );
}
async function closed(
  f: Awaited<ReturnType<typeof fixture>>,
  id: string,
): Promise<Ledger> {
  for (let i = 0; i < 50; i++) {
    const ledger = await f.ledger();
    if (ledger.runs[id]?.phase === "closed") return ledger;
    await pause(100);
  }
  assert.fail(`run ${id} not closed`);
}

async function cleanupAcknowledged(
  f: Awaited<ReturnType<typeof fixture>>,
  runId: string,
): Promise<Ledger> {
  for (let i = 0; i < 50; i++) {
    const ledger = await f.ledger();
    const intents = Object.values(ledger.outbox).filter(
      (c) => c.runId === runId && c.kind === "cleanup",
    );
    if (intents.length && intents.every((c) => c.delivered)) return ledger;
    await pause(100);
  }
  assert.fail(`cleanup intent for ${runId} remains unacknowledged`);
}

test(
  "native ledger: concurrent last-slot admission, strict schemas, atomic official SQL adapter",
  { timeout: 30_000 },
  async () => {
    const f = await fixture();
    try {
      const responses = await Promise.all(
        ["admit-one", "admit-two", "admit-three"].map((id) =>
          f.submit(id, { deferWorkflowMs: 60_000 }),
        ),
      );
      assert.deepEqual(responses.map((r) => r.status).sort(), [200, 200, 409]);
      const ledger = await f.ledger();
      assert.equal(Object.keys(ledger.runs).length, 2);
      assert.equal(ledger.grant.consumed, 2);
      assert.equal(
        Object.values(ledger.runs).reduce((sum, r) => sum + r.held, 0),
        20,
      );
      assert.equal(Object.keys(ledger.outbox).length, 2);
      const saved = Object.values(ledger.runs)[0]!;
      const reordered = Object.fromEntries(
        Object.entries(saved.spec).reverse(),
      );
      const replay = await f.post("/submit", {
        epoch: 1,
        spec: reordered,
        controls: { deferWorkflowMs: 60_000 },
      });
      assert.equal(replay.status, 200);
      assert.equal(
        ((await replay.json()) as Decision).run!.digest,
        saved.digest,
      );
      assert.notEqual(saved.digest, digest);
      assert.equal((await f.ledger()).grant.consumed, 2);
      assert.equal(
        (
          await f.post("/submit", {
            epoch: 1,
            spec: { ...saved.spec, reservation: 11 },
          })
        ).status,
        409,
      );
      const invalid = await f.post("/submit", {
        epoch: 1,
        spec: { ...spec("bad"), secret: "unexpected" },
      });
      assert.equal(invalid.status, 400);
      const epoch = await f.post("/submit", { epoch: 2, spec: spec("epoch") });
      assert.equal(epoch.status, 409);
      const diag = await f.post("/diagnostic", {});
      assert.equal(diag.status, 200);
      const diagnostic = (await diag.json()) as {
        rows: Array<{ id: string }>;
        alarmRolledBack: boolean;
      };
      assert.deepEqual(
        diagnostic.rows.map((r) => r.id),
        ["after-yield", "outer"],
      );
      assert.equal(diagnostic.alarmRolledBack, true);
      await f.restart();
      assert.equal((await f.ledger()).grant.consumed, 2);
    } finally {
      await f.cleanup();
    }
  },
);

test(
  "native workflow: ambiguous dispatch, lost provider wake, immutable receipt and completed replay",
  { timeout: 35_000 },
  async () => {
    const f = await fixture();
    try {
      assert.equal(
        (await f.submit("ambiguous", { loseReply: true, dropWake: true }))
          .status,
        200,
      );
      const records = await observed(f, "ambiguous", "parent-complete");
      assert.equal(records.filter((r) => r.kind === "call").length, 1);
      assert.equal(records.find((r) => r.id === "ambiguous-body")?.value, 1);
      const ledger = await closed(f, "ambiguous");
      const run = ledger.runs.ambiguous!;
      assert.equal(run.dispatchConsumed, true);
      assert.equal(run.cleanup, "confirmed");
      assert.equal(run.accounting, "settled");
      assert.equal(run.held, 0);
      assert.equal(run.cost, 1);
      assert.equal(run.receipt?.validity, "valid");
      const duplicate = await f.post("/launch", {
        epoch: 1,
        runId: "ambiguous",
      });
      assert.equal(((await duplicate.json()) as Decision).newPermission, false);
      const artifact = await f.post("/artifact", {
        epoch: 1,
        runId: "ambiguous",
        revision: run.revision,
        receipt: { ...run.receipt, digest: "b".repeat(64) },
      });
      assert.equal(artifact.status, 409);
      await f.restart();
      const native = (await (
        await f.post("/native-status", { epoch: 1, runId: "ambiguous" })
      ).json()) as { status: string; output: unknown };
      assert.equal(native.status, "complete");
      assert.deepEqual(native.output, { runId: "ambiguous" });
      const replay = (await (
        await f.post("/native-ensure", { epoch: 1, runId: "ambiguous" })
      ).json()) as { status: string; output: unknown };
      assert.deepEqual(replay, native);
      await pause(500);
      const after = await f.observe();
      assert.equal(
        after.find((r) => r.id === "ambiguous-first-start")?.value,
        1,
      );
      assert.equal(after.find((r) => r.id === "ambiguous-first")?.value, 1);
      assert.equal(after.find((r) => r.id === "ambiguous-body")?.value, 1);
      assert.equal(
        after.find((r) => r.id === "ambiguous-parent-complete")?.value,
        1,
      );
      assert.deepEqual((await f.ledger()).runs.ambiguous!.receipt, run.receipt);
    } finally {
      await f.cleanup();
    }
  },
);

test(
  "native claims: duplicates cannot launch or execute twice; cancellation fences late launch",
  { timeout: 30_000 },
  async () => {
    const f = await fixture();
    try {
      assert.equal(
        (await f.submit("cancelled", { deferWorkflowMs: 60_000 })).status,
        200,
      );
      const launches = await Promise.all(
        Array.from({ length: 8 }, () =>
          f
            .post("/launch", { epoch: 1, runId: "cancelled" })
            .then(async (r) => (await r.json()) as Decision),
        ),
      );
      assert.equal(launches.filter((r) => r.newPermission).length, 1);
      const run = (await f.ledger()).runs.cancelled!;
      assert.equal(run.callIds.length, 1);
      const claims = await Promise.all(
        Array.from({ length: 8 }, (_, i) =>
          f
            .post("/claim", {
              epoch: 1,
              runId: "cancelled",
              callId: run.callIds[0],
              bodyId: `claim-${i}`,
            })
            .then(async (r) => (await r.json()) as Decision),
        ),
      );
      assert.equal(claims.filter((r) => r.newPermission).length, 1);
      // A provider launch arrives after cancellation but before the gateway generation fence.
      await f.post("/cancel", {
        epoch: 1,
        runId: "cancelled",
        commandId: "cancel-now",
      });
      const late = await f.providerPost("/spawn", {
        runId: "cancelled",
        generation: "cancelled-e1-v1",
      });
      assert.equal(late.status, 200);
      await f.post("/reconcile", { epoch: 1, runId: "cancelled" });
      const final = (await f.ledger()).runs.cancelled!;
      assert.equal(final.callIds.length, 2);
      assert.equal(final.outcome, "cancelled");
      assert.equal(final.cleanup, "confirmed");
      assert.equal(final.accounting, "settled");
      assert.equal(final.cost, 2);
      assert.equal(final.held, 0);
      assert.equal(
        (
          await f.providerPost("/spawn", {
            runId: "cancelled",
            generation: "cancelled-e1-v1",
          })
        ).status,
        409,
      );
      assert.equal(
        (
          await f
            .post("/claim", {
              epoch: 1,
              runId: "cancelled",
              callId: final.callIds[1],
              bodyId: "late-body",
            })
            .then(async (r) => (await r.json()) as Decision)
        ).newPermission,
        false,
      );
      assert.equal(
        (await f.observe()).filter(
          (r) =>
            r.kind === "call" &&
            (r.value as { state: string }).state !== "cancelled",
        ).length,
        0,
      );
    } finally {
      await f.cleanup();
    }
  },
);

test(
  "native upgrade: unknown schema and epoch fail closed after real workerd restart",
  { timeout: 30_000 },
  async () => {
    const f = await fixture();
    try {
      await f.submit("upgrade", { deferWorkflowMs: 60_000 });
      await f.post("/corrupt", { schemaVersion: 2 });
      await f.restart();
      const r = await f.post("/launch", { epoch: 1, runId: "upgrade" });
      assert.equal(r.status, 409);
      assert.equal(((await r.json()) as Decision).failure, "SchemaRejected");
      assert.equal(
        (await f.observe()).filter((r) => r.kind === "call").length,
        0,
      );
      await f.post("/corrupt", { schemaVersion: 1, epoch: 2 });
      await f.restart();
      assert.equal(
        (await f.post("/launch", { epoch: 1, runId: "upgrade" })).status,
        409,
      );
      const invalid = await f.post("/submit", {
        epoch: 2,
        spec: { ...spec("definition"), definitionVersion: 2 },
      });
      assert.equal(invalid.status, 400);
    } finally {
      await f.cleanup();
    }
  },
);

test(
  gapLabel +
    "native first-activity autonomous recovery: restart without waking or polling target",
  { timeout: 65_000 },
  async () => {
    const f = await fixture();
    try {
      await f.submit("first-restart", { firstDelayMs: 6000 });
      // Independent proof that the FIRST step callback entered, before any step completion.
      await observed(f, "first-restart", "first-start");
      assert.equal(
        (await f.observe()).some((r) => r.id === "first-restart-first"),
        false,
      );
      await f.restart();
      // Only /observe on the separately alive provider is touched after restart.
      let records: Observation[];
      try {
        records = await observed(f, "first-restart", "parent-complete", 25_000);
        assert.equal(
          characterize,
          false,
          "known gap recovered autonomously; update strict evidence",
        );
      } catch (gateFailure) {
        const before = await f.observe();
        const intervention = await (
          await f.post("/native-status", { epoch: 1, runId: "first-restart" })
        ).json();
        let recovered = false;
        try {
          await observed(f, "first-restart", "parent-complete", 12_000);
          recovered = true;
        } catch {}
        console.log(
          JSON.stringify({
            diagnostic:
              "AFTER failed autonomous first-activity gate: native status intervention",
            before,
            intervention: summarizeStatus(intervention),
            recovered,
          }),
        );
        if (characterize) {
          assert.equal(
            recovered,
            false,
            "known runtime gap unexpectedly recovered after intervention",
          );
          assert.equal((intervention as { status: string }).status, "running");
          assert.equal(
            before.find((r) => r.id === "first-restart-first-start")?.value,
            1,
          );
          assert.equal(
            before.some((r) => r.id === "first-restart-first"),
            false,
          );
          assert.equal(
            before.some((r) => r.kind === "call"),
            false,
          );
          return;
        }
        throw gateFailure;
      }
      assert.equal(
        records.find((r) => r.id === "first-restart-first")?.value,
        1,
      );
      assert.equal(
        records.find((r) => r.id === "first-restart-body")?.value,
        1,
      );
    } finally {
      await f.cleanup();
    }
  },
);

test(
  gapLabel +
    "native suspended control: lost provider callback repaired by bounded native reconciliation after workerd restart",
  { timeout: 65_000 },
  async () => {
    const f = await fixture();
    try {
      await f.submit("suspended", { finishExternally: true, dropWake: true });
      let records = await observed(f, "suspended", "body");
      const call = records.find((r) => r.kind === "call")!.value as {
        id: string;
      };
      const waitResponse = await f.post("/native-wait-start", {
        epoch: 1,
        runId: "suspended",
      });
      const wait = (await waitResponse.json()) as {
        event: { type: string; stepName: string };
        status: unknown;
      };
      assert.equal(waitResponse.status, 200, JSON.stringify(wait));
      assert.equal(wait.event.type, "wait_started");
      assert.equal(wait.event.stepName, "provider-terminal-0-1");
      assert.equal(
        (wait.status as { __LOCAL_DEV_STEP_OUTPUTS: unknown[] })
          .__LOCAL_DEV_STEP_OUTPUTS.length,
        4,
      );
      console.log(
        JSON.stringify({
          preRestartNativeWait: {
            event: wait.event,
            status: summarizeStatus(wait.status),
          },
        }),
      );
      await f.stop();
      await f.providerPost("/finish", { callId: call.id });
      await f.start();
      // Independent provider reads only; no native Workflow.status()/get()/resume() from test.
      try {
        records = await observed(f, "suspended", "parent-complete", 25_000);
        assert.equal(
          characterize,
          false,
          "known gap recovered autonomously; update strict evidence",
        );
      } catch (gateFailure) {
        const before = await f.observe();
        const intervention = await (
          await f.post("/native-status", { epoch: 1, runId: "suspended" })
        ).json();
        let recovered = false;
        try {
          await observed(f, "suspended", "parent-complete", 12_000);
          recovered = true;
        } catch {}
        console.log(
          JSON.stringify({
            diagnostic:
              "AFTER failed suspended gate: native status intervention",
            before,
            intervention: summarizeStatus(intervention),
            recovered,
          }),
        );
        if (characterize) {
          assert.equal(
            recovered,
            false,
            "known runtime gap unexpectedly recovered after intervention",
          );
          assert.equal((intervention as { status: string }).status, "running");
          assert.equal(before.find((r) => r.id === "suspended-body")?.value, 1);
          assert.equal(
            before.some((r) => r.id === "suspended-parent-complete"),
            false,
          );
          assert.equal(
            before.filter(
              (r) =>
                r.kind === "call" &&
                (r.value as { state: string }).state === "completed",
            ).length,
            1,
          );
          return;
        }
        throw gateFailure;
      }
      assert.equal(records.find((r) => r.id === "suspended-body")?.value, 1);
    } finally {
      await f.cleanup();
    }
  },
);

test(
  "native controls: incomplete first activity and lost provider callback complete without restart",
  { timeout: 30_000 },
  async () => {
    const f = await fixture();
    try {
      await f.submit("first-control", { firstDelayMs: 1000 });
      await observed(f, "first-control", "parent-complete");
      await f.submit("wait-control", {
        finishExternally: true,
        dropWake: true,
      });
      const records = await observed(f, "wait-control", "body");
      const call = records.find(
        (r) =>
          r.kind === "call" &&
          (r.value as { runId: string }).runId === "wait-control",
      )!.value as { id: string };
      await f.providerPost("/finish", { callId: call.id });
      await observed(f, "wait-control", "parent-complete");
      const ledger = await f.ledger();
      assert.equal(ledger.runs["first-control"]!.accounting, "settled");
      assert.equal(ledger.runs["wait-control"]!.accounting, "settled");
    } finally {
      await f.cleanup();
    }
  },
);

test(
  "native interruption: cancelling a suspended workflow stops owned calls and preserves settled accounting",
  { timeout: 30_000 },
  async () => {
    const f = await fixture();
    try {
      await f.submit("interrupt", { finishExternally: true });
      await observed(f, "interrupt", "body");
      assert.equal(
        (
          await f.post("/cancel", {
            epoch: 1,
            runId: "interrupt",
            commandId: "interrupt-now",
          })
        ).status,
        200,
      );
      const ledger = await closed(f, "interrupt");
      const run = ledger.runs.interrupt!;
      assert.equal(run.outcome, "cancelled");
      assert.equal(run.cleanup, "confirmed");
      assert.equal(run.accounting, "settled");
      assert.equal(run.held, 0);
      assert.equal(run.cost, 1);
      assert.equal(run.receipt, undefined);
      let nativeState = "running";
      for (let i = 0; i < 20; i++) {
        nativeState = (
          (await (
            await f.post("/native-status", { epoch: 1, runId: "interrupt" })
          ).json()) as { status: string }
        ).status;
        if (nativeState === "terminated") break;
        await pause(100);
      }
      assert.equal(nativeState, "terminated");
      await cleanupAcknowledged(f, "interrupt");
      await f.post("/reconcile", { epoch: 1, runId: "interrupt" });
      assert.equal((await f.ledger()).runs.interrupt!.accounting, "settled");
      await f.restart();
      await pause(500);
      const after = await f.observe();
      assert.equal(after.find((r) => r.id === "interrupt-body")?.value, 1);
      assert.equal(
        after.some((r) => r.id === "interrupt-parent-complete"),
        false,
      );
      assert.equal(
        after.filter(
          (r) =>
            r.kind === "call" &&
            (r.value as { state: string }).state !== "cancelled",
        ).length,
        0,
      );
    } finally {
      await f.cleanup();
    }
  },
);

test(
  "native uncertainty: ambiguous launch and terminal provider status retain liabilities until confirmed cleanup",
  { timeout: 30_000 },
  async () => {
    const f = await fixture();
    try {
      await f.submit("liability", { deferWorkflowMs: 60_000, loseReply: true });
      const launch = await f.post("/launch", { epoch: 1, runId: "liability" });
      assert.equal(launch.status, 200);
      let run = (await f.ledger()).runs.liability!;
      assert.equal(run.phase, "launch_unknown");
      assert.equal(run.dispatchConsumed, true);
      assert.equal(run.callIds.length, 0);
      assert.equal(run.held, 10);
      assert.equal(run.accounting, "reserved");
      const records = await f.observe();
      const call = records.find((r) => r.kind === "call")!.value as {
        id: string;
      };
      assert.equal(records.filter((r) => r.kind === "call").length, 1);
      await f.providerPost("/finish", { callId: call.id });
      await f.post("/reconcile", { epoch: 1, runId: "liability" });
      run = (await f.ledger()).runs.liability!;
      assert.equal(run.outcome, "pending");
      assert.equal(run.receipt, undefined);
      assert.equal(run.held, 10);
      assert.equal(run.accounting, "reserved");
      await f.post("/cancel", {
        epoch: 1,
        runId: "liability",
        commandId: "contain-unknown",
      });
      await f.post("/reconcile", { epoch: 1, runId: "liability" });
      run = (await f.ledger()).runs.liability!;
      assert.equal(run.cleanup, "confirmed");
      assert.equal(run.accounting, "settled");
      assert.equal(run.cost, 1);
      assert.equal(run.held, 0);
    } finally {
      await f.cleanup();
    }
  },
);

test(
  "native deadline: expiry contains resources and keeps timeout separate from cancellation",
  { timeout: 30_000 },
  async () => {
    const f = await fixture();
    try {
      const deadlineSpec = { ...spec("deadline"), deadline: Date.now() + 1000 };
      const submit = await f.post("/submit", {
        epoch: 1,
        spec: deadlineSpec,
        controls: { deferWorkflowMs: 60_000, finishExternally: true },
      });
      assert.equal(submit.status, 200);
      await f.post("/launch", { epoch: 1, runId: "deadline" });
      const ledger = await closed(f, "deadline");
      const run = ledger.runs.deadline!;
      assert.equal(run.outcome, "timed_out");
      assert.equal(run.cancelRequested, true);
      assert.equal(run.cleanup, "confirmed");
      assert.equal(run.accounting, "settled");
      assert.equal(run.cost, 1);
      assert.equal(run.held, 0);
      const claim = await f.post("/claim", {
        epoch: 1,
        runId: "deadline",
        callId: run.callIds[0],
        bodyId: "after-deadline",
      });
      assert.equal(((await claim.json()) as Decision).newPermission, false);
    } finally {
      await f.cleanup();
    }
  },
);

test(
  "native code upgrade: incompatible module bytes fence an incomplete v1 workflow",
  { timeout: 30_000 },
  async () => {
    const f = await fixture();
    try {
      const [v1, , v2] = await builds;
      assert.notEqual(v1.outputFiles[0]!.text, v2.outputFiles[0]!.text);
      await f.submit("code-upgrade", { firstDelayMs: 6000 });
      await observed(f, "code-upgrade", "first-start");
      await f.restart(true);
      // A real different Worker module is loaded; no manual persisted-state mutation in this test.
      const launch = await f.post("/launch", {
        epoch: 1,
        runId: "code-upgrade",
      });
      assert.equal(launch.status, 409);
      assert.equal(
        ((await launch.json()) as Decision).failure,
        "EpochMismatch",
      );
      const read = await f.post("/run", { epoch: 1, runId: "code-upgrade" });
      assert.equal(read.status, 409);
      const records = await f.observe();
      assert.equal(records.filter((r) => r.kind === "call").length, 0);
      assert.equal(
        records.find((r) => r.id === "code-upgrade-first-start")?.value,
        1,
      );
      assert.equal((await f.ledger()).runs["code-upgrade"]!.held, 10);
    } finally {
      await f.cleanup();
    }
  },
);

test(
  "native cleanup retry: first unavailable cancel retains durable containment intent and liability",
  { timeout: 30_000 },
  async () => {
    const f = await fixture(true);
    try {
      await f.submit("cleanup-retry", {
        deferWorkflowMs: 60_000,
        finishExternally: true,
      });
      await f.post("/launch", { epoch: 1, runId: "cleanup-retry" });
      await f.post("/cancel", {
        epoch: 1,
        runId: "cleanup-retry",
        commandId: "cleanup-unavailable",
      });
      await f.post("/reconcile", { epoch: 1, runId: "cleanup-retry" });
      const pending = await f.ledger();
      const run = pending.runs["cleanup-retry"]!;
      assert.equal(run.cleanup, "unconfirmed");
      assert.equal(run.held, 10);
      assert.equal(run.accounting, "reserved");
      assert(
        Object.values(pending.outbox).some(
          (c) => c.kind === "cleanup" && !c.delivered,
        ),
      );
      await closed(f, "cleanup-retry");
      const final = await cleanupAcknowledged(f, "cleanup-retry");
      assert.equal(
        (await f.observe()).find(
          (r) => r.subjectId === "cleanup-retry" && r.event === "cancel-outage",
        )?.value,
        1,
      );
      assert.equal(final.runs["cleanup-retry"]!.cleanup, "confirmed");
      assert.equal(final.runs["cleanup-retry"]!.accounting, "settled");
      assert.equal(final.runs["cleanup-retry"]!.cost, 1);
      assert.equal(final.runs["cleanup-retry"]!.held, 0);
      assert.equal(
        (await f.observe()).filter(
          (r) =>
            r.kind === "call" &&
            (r.value as { state: string }).state === "cancelled",
        ).length,
        1,
      );
    } finally {
      await f.cleanup();
    }
  },
);

test(
  "native alarm priority: second deferred admission cannot postpone first run deadline",
  { timeout: 30_000 },
  async () => {
    const f = await fixture();
    try {
      const first = { ...spec("early-deadline"), deadline: Date.now() + 1000 };
      assert.equal(
        (
          await f.post("/submit", {
            epoch: 1,
            spec: first,
            controls: { deferWorkflowMs: 60_000 },
          })
        ).status,
        200,
      );
      assert.equal(
        (await f.submit("deferred-second", { deferWorkflowMs: 60_000 })).status,
        200,
      );
      await closed(f, "early-deadline");
      const final = await cleanupAcknowledged(f, "early-deadline");
      assert(
        Object.values(final.outbox).some(
          (c) =>
            c.runId === "early-deadline" &&
            c.kind === "ensure_workflow" &&
            c.delivered,
        ),
      );
      assert.equal(final.runs["early-deadline"]!.outcome, "timed_out");
      assert.equal(final.runs["early-deadline"]!.cleanup, "confirmed");
      assert.equal(final.runs["deferred-second"]!.phase, "accepted");
      assert.equal(
        (await f.observe()).filter((r) => r.kind === "call").length,
        0,
      );
    } finally {
      await f.cleanup();
    }
  },
);

test(
  "native opaque ID: accepted leading hyphen survives native creation and completed history",
  { timeout: 30_000 },
  async () => {
    const f = await fixture();
    try {
      assert.equal((await f.submit("-opaque")).status, 200);
      await observed(f, "-opaque", "parent-complete");
      const status = (await (
        await f.post("/native-status", { epoch: 1, runId: "-opaque" })
      ).json()) as { status: string; output: unknown };
      assert.equal(status.status, "complete");
      assert.deepEqual(status.output, { runId: "-opaque" });
      assert.equal(
        (await closed(f, "-opaque")).runs["-opaque"]!.accounting,
        "settled",
      );
    } finally {
      await f.cleanup();
    }
  },
);

test(
  "native fenced cleanup: late launch between lookup and disable is owned and cancelled before acknowledgement",
  { timeout: 30_000 },
  async () => {
    const f = await fixture(false, true);
    try {
      await f.submit("fence-race", { deferWorkflowMs: 60_000 });
      await f.post("/launch", { epoch: 1, runId: "fence-race" });
      await f.post("/cancel", {
        epoch: 1,
        runId: "fence-race",
        commandId: "cancel-fence-race",
      });
      await f.post("/reconcile", { epoch: 1, runId: "fence-race" });
      const ledger = await cleanupAcknowledged(f, "fence-race");
      const run = ledger.runs["fence-race"]!;
      assert.equal(run.callIds.length, 2);
      assert.equal(run.cleanup, "confirmed");
      assert.equal(run.accounting, "settled");
      assert.equal(run.cost, 2);
      assert.equal(run.held, 0);
      const records = await f.observe();
      assert.equal(
        records.find(
          (r) => r.subjectId === "fence-race" && r.event === "late-at-fence",
        )?.value,
        1,
      );
      const calls = records.filter((r) => r.kind === "call");
      assert.equal(calls.length, 2);
      assert(
        calls.every(
          (r) => (r.value as { state: string }).state === "cancelled",
        ),
      );
    } finally {
      await f.cleanup();
    }
  },
);

test("bundle metrics", async () => {
  const [lane] = await builds;
  console.log(
    JSON.stringify({
      laneBundleBytes: lane.outputFiles[0]!.contents.byteLength,
      laneBundleSha256: createHash("sha256").update(lane.outputFiles[0]!.contents).digest("hex"),
      node: process.version,
      testRunner: "node:test",
      timestamp: new Date().toISOString(),
    }),
  );
});
