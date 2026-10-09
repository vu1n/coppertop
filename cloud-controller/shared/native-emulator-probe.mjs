import assert from "node:assert/strict";
import { test } from "node:test";
import { mkdtemp, readdir, rm, readFile } from "node:fs/promises";
import { tmpdir } from "node:os";
import { join, relative } from "node:path";
import { DatabaseSync } from "node:sqlite";

const pause = (ms) => new Promise((resolve) => setTimeout(resolve, ms));

async function sqliteFiles(directory) {
  const found = [];
  for (const entry of await readdir(directory, { withFileTypes: true })) {
    const path = join(directory, entry.name);
    if (entry.isDirectory()) found.push(...await sqliteFiles(path));
    else if (entry.isFile() && entry.name.endsWith(".sqlite")) found.push(path);
  }
  return found;
}

/** Read only this fixture's generated target SQLite, before any restart. */
async function persistedSleep(directory) {
  for (const path of await sqliteFiles(directory)) {
    const db = new DatabaseSync(path, { readOnly: true });
    try {
      const tables = db.prepare("SELECT name FROM sqlite_master WHERE type='table'").all();
      if (!tables.some((row) => row.name === "states") ||
          !tables.some((row) => row.name === "priority_queue")) continue;
      const states = db.prepare("SELECT id,event,target,metadata,groupKey FROM states ORDER BY id").all();
      const before = states.find((row) => row.event === 6 && row.target === "raw-before-1");
      const sleep = states.find((row) => row.event === 8 && row.target === "raw-sleep-1");
      const active = new Map();
      for (const row of db.prepare("SELECT * FROM priority_queue ORDER BY id").all()) {
        const key = JSON.stringify([row.entryType, row.hash]);
        if (row.action === 0) active.delete(key); else active.set(key, row);
      }
      const due = sleep && [...active.values()].find((row) => row.hash === sleep.groupKey);
      if (before && sleep && due &&
          !states.some((row) => row.event === 9 && row.groupKey === sleep.groupKey)) {
        assert.equal(JSON.parse(sleep.metadata).durationMs, 10_000);
        return {
          database: relative(directory, path),
          before: { id: before.id, event: before.event, target: before.target },
          sleep: { id: sleep.id, event: sleep.event, target: sleep.target, durationMs: 10_000 },
          queuedSleep: { targetTimestamp: due.target_timestamp, action: due.action },
        };
      }
    } finally {
      db.close();
    }
  }
}

/** Lane wrappers inject their own dependency universe and persistence conversion. */
export function nativeEmulatorControls({
  Miniflare, build, options, shared, lane, compatibilityDate,
  sourceFile, provenance,
}) {
  const bundles = Promise.all(["native-emulator-worker.ts", "provider-worker.ts"].map((file) =>
    build({
      entryPoints: [join(shared, file)], bundle: true, format: "esm", write: false,
      external: ["cloudflare:workers"],
    })));

  test(`${lane} plain native: pinned emulator source characterization`, async () => {
    const source = await readFile(sourceFile, "utf8");
    assert.match(source, /async handleNextAlarm\(\)\s*\{\s*this\.#heap\.peek\(\);\s*\}/);
    assert.doesNotMatch(source, /^\s*(?:async\s+)?alarm\s*\(/m);
    const statusStart = source.lastIndexOf("  async status() {");
    assert(statusStart >= 0, "installed binding must have its native status method");
    const statusEnd = source.indexOf("\n  async ", statusStart + 1);
    const status = source.slice(statusStart, statusEnd < 0 ? undefined : statusEnd);
    assert.match(status, /this\.stub\.getStatus\(\)/);
    assert.match(status, /this\.stub\.readLogs\(\)/);
    assert.doesNotMatch(status, /\.(?:init|run|resume|attemptRestart)\(/);
    console.log(JSON.stringify({
      rawNativeProvenance: provenance,
      sourceCharacterization: {
        handleNextAlarm: "only heap.peek(); no scheduling in this method",
        alarmMethodPresent: false,
        statusMethod: "reads getStatus and readLogs; no run/resume call in inspected method",
        inference: "local emulator implementation evidence, not managed Cloudflare evidence",
      },
    }));
  });

  async function fixture() {
    const dir = await mkdtemp(join(tmpdir(), `coppertop-raw-${lane}-`));
    const targetDirectory = join(dir, "target");
    const [raw, observer] = await bundles;
    const provider = new Miniflare(options({
      modules: [{ type: "ESModule", path: "observer.mjs", contents: observer.outputFiles[0].text }],
      compatibilityDate,
      durableObjects: { PROVIDER: { className: "FakeProvider", useSQLite: true } },
    }, join(dir, "observer")));
    const address = (await provider.ready).host;
    const targetOptions = options({
      modules: [{ type: "ESModule", path: "raw-native.mjs", contents: raw.outputFiles[0].text }],
      compatibilityDate,
      workflows: { RAW: { name: "plain-native-control", className: "RawNativeWorkflow" } },
      serviceBindings: { OBSERVER: { external: { address, http: {} } } },
    }, targetDirectory);
    let target = new Miniflare(targetOptions);
    await target.ready;
    return {
      targetDirectory,
      create: (id) => target.dispatchFetch("http://target/create", {
        method: "POST", body: JSON.stringify({ id }),
      }),
      observe: async () => {
        const response = await provider.dispatchFetch("http://observer/observe");
        assert.equal(response.status, 200);
        return response.json();
      },
      restart: async (method) => {
        if (method === "dispose-recreate") {
          await target.dispose();
          target = new Miniflare(targetOptions);
          await target.ready;
        } else {
          await target.setOptions(targetOptions);
          await target.ready;
        }
      },
      cleanup: async () => {
        await target.dispose();
        await provider.dispose();
        await rm(dir, { recursive: true, force: true });
      },
    };
  }
  const count = (records, id, event) => records.find((row) =>
    row.kind === "observer" && row.subjectId === id && row.event === event)?.value ?? 0;

  test(`${lane} plain native: no-restart do/sleep/do control completes`, { timeout: 45_000 }, async () => {
    const f = await fixture();
    const id = "raw-control";
    try {
      assert.equal((await f.create(id)).status, 200);
      const start = Date.now();
      let records;
      do {
        records = await f.observe();
        if (count(records, id, "complete") === 1) break;
        await pause(100);
      } while (Date.now() - start < 20_000);
      assert.equal(count(records, id, "before"), 1);
      assert.equal(count(records, id, "complete"), 1);
      console.log(JSON.stringify({ rawNative: lane, method: "no-restart", observerWaitMs: Date.now() - start, records }));
    } finally { await f.cleanup(); }
  });

  for (const method of ["dispose-recreate", "setOptions"]) {
    test(`${lane} plain native: EMULATOR CHARACTERIZATION (not acceptance) ${method} during durable sleep`,
      { timeout: 45_000 }, async () => {
        const f = await fixture();
        const id = `raw-${method === "setOptions" ? "set-options" : method}`;
        try {
          assert.equal((await f.create(id)).status, 200);
          const start = Date.now();
          let checkpoint;
          do {
            checkpoint = await persistedSleep(f.targetDirectory);
            if (checkpoint) break;
            await pause(50);
          } while (Date.now() - start < 2000);
          assert(checkpoint, "generated SQLite must prove first STEP_SUCCESS, SLEEP_START and queued sleep before restart");
          const before = await f.observe();
          assert.equal(count(before, id, "before"), 1);
          assert.equal(count(before, id, "complete"), 0);
          const originalDueMs = checkpoint.queuedSleep.targetTimestamp;
          const restartStartedMs = Date.now();
          const remainingAtRestartMs = originalDueMs - restartStartedMs;
          assert(remainingAtRestartMs > 8000,
            `invalid fault window: only ${remainingAtRestartMs}ms remain before sleep is due`);
          await f.restart(method);
          const restartedAtMs = Date.now();
          const remainingAfterRestartMs = originalDueMs - restartedAtMs;
          // All target inspection ends before restart. Only this independently alive observer is read below.
          const immediate = await f.observe();
          const remainingAtFirstObservationMs = originalDueMs - Date.now();
          const observationDeadlineMs = Math.max(restartedAtMs + 8000, originalDueMs + 3000);
          console.log(JSON.stringify({
            rawNativeRestartWindow: lane, method, originalDueMs, remainingAtRestartMs,
            restartDurationMs: restartedAtMs - restartStartedMs,
            remainingAfterRestartMs, remainingAtFirstObservationMs,
            immediateCompletion: count(immediate, id, "complete"), observationDeadlineMs,
          }));
          assert(remainingAfterRestartMs > 0 && remainingAtFirstObservationMs > 0,
            "invalid fault window: original sleep was due before the restarted observer check");
          assert.equal(count(immediate, id, "complete"), 0,
            "invalid fault window: completion must remain absent immediately after restart");
          let records = immediate;
          do {
            await pause(100);
            records = await f.observe();
            assert.equal(count(records, id, "complete"), 0, "characterized local stall changed; investigate rather than promote managed acceptance");
          } while (Date.now() < observationDeadlineMs);
          assert.equal(count(records, id, "before"), 1);
          console.log(JSON.stringify({
            rawNative: lane, method, checkpoint, immediateCompletion: 0,
            remainingAtRestartMs, remainingAfterRestartMs, observationDeadlineMs,
            observerOnlyWindowMs: Date.now() - restartedAtMs, records,
            inference: "this pinned local do/sleep/do program stalled; no universal or managed-runtime conclusion",
          }));
        } finally { await f.cleanup(); }
      });
  }
}
