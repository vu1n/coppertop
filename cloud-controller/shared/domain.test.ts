import { test } from "node:test";
import assert from "node:assert/strict";
import { initialLedger, transition } from "./domain.ts";
import type { ArtifactReceipt, Command, Ledger, RunSpec } from "./contracts.ts";
import { fileURLToPath } from "node:url";
import { evidence } from "./evidence.ts";

console.log(JSON.stringify({ sharedProvenance: evidence(fileURLToPath(new URL("../../", import.meta.url)), [
  "cloud-controller/shared/contracts.ts", "cloud-controller/shared/domain.ts",
  "cloud-controller/shared/domain.test.ts", "cloud-controller/shared/provider-worker.ts",
  "cloud-controller/shared/provider.test.ts", "cloud-controller/shared/evidence.ts",
  "cloud-controller/shared/package.json", "cloud-controller/shared/package-lock.json",
], process.env.COPPERTOP_TEST_COMMAND ?? "npm test (cloud-controller/shared)") }));

const now = 1_000_000;
const digest = "a".repeat(64);
export const spec = (runId = "run-one", reservation = 100): RunSpec => ({
  schemaVersion: 1, runId, commandId: `submit-${runId}`, idempotencyKey: runId,
  principal: "synthetic-operator", grantId: "synthetic-grant", preset: "fake-v1",
  sourceDigest: digest, inputDigest: digest, deployment: "fake-deployment-v1", reservation,
  deadline: now + 60_000, engine: "native-cf", definitionVersion: 1,
});
const mutate = (ledger: Ledger, command: Command, epoch = 1, time = now) => transition(ledger, { epoch, now: time, command });
const admitted = () => mutate(initialLedger(1), { type: "submit", spec: spec(), digest }).ledger;

test("idempotency consumes grant and reservation once and rejects changed spec", () => {
  const ledger = admitted();
  const again = mutate(ledger, { type: "submit", spec: spec(), digest });
  assert.equal(again.ledger.grant.consumed, 1);
  assert.equal(Object.keys(again.ledger.outbox).length, 1);
  assert.equal(again.run?.held, 100);
  assert.equal(mutate(ledger, { type: "submit", spec: spec(), digest: "b".repeat(64) }).failure, "SpecConflict");
  assert.equal(mutate(ledger, { type: "submit", spec: spec(), digest }, 1, now + 120_000).run?.spec.runId, "run-one");
  assert.equal(mutate(initialLedger(1), { type: "submit", spec: spec(), digest }, 1, now + 120_000).failure, "SchemaRejected");
});

test("budget and concurrency preserve last-slot admission", () => {
  const ledger = initialLedger(1); ledger.grant.concurrency = 1;
  const first = mutate(ledger, { type: "submit", spec: spec(), digest }).ledger;
  assert.equal(mutate(first, { type: "submit", spec: spec("second"), digest }).failure, "BudgetDenied");
  const budget = initialLedger(1); budget.grant.limit = 100;
  const used = mutate(budget, { type: "submit", spec: spec(), digest }).ledger;
  assert.equal(mutate(used, { type: "submit", spec: spec("second", 1), digest }).failure, "BudgetDenied");
});

test("dispatch and body grants are never returned twice after ambiguous reply", () => {
  let ledger = admitted();
  const claim = mutate(ledger, { type: "dispatch", runId: "run-one" });
  assert.equal(claim.newPermission, true); ledger = claim.ledger;
  assert.equal(mutate(ledger, { type: "dispatch", runId: "run-one" }).newPermission, false);
  ledger = mutate(ledger, { type: "register", runId: "run-one", callId: "call-one" }).ledger;
  const body = { type: "execute", runId: "run-one", callId: "call-one", bodyId: "body-one" } as const;
  const granted = mutate(ledger, body); assert.equal(granted.newPermission, true);
  assert.equal(mutate(granted.ledger, body).newPermission, false);
  assert.equal(mutate(granted.ledger, { ...body, bodyId: "body-two" }).newPermission, false);
});

test("cancellation fences dispatch, body and stale artifact publication", () => {
  let ledger = admitted();
  ledger = mutate(ledger, { type: "cancel", runId: "run-one", commandId: "cancel-one" }).ledger;
  assert.equal(mutate(ledger, { type: "dispatch", runId: "run-one" }).newPermission, false);
  assert.equal(ledger.runs["run-one"]?.cancelRequested, true);
  assert.ok(Object.values(ledger.outbox).some(item => item.kind === "cleanup"));
  assert.equal(mutate(ledger, { type: "artifact", runId: "run-one", revision: 1, receipt: {
    schemaVersion: 1, runId: "run-one", specDigest: digest, bodyId: "body-one",
    key: `runs/run-one/${digest}.json`, digest, bytes: 20, validity: "valid", decisionGate: "passed",
  } }).failure, "StaleRevision");
});

test("unknown calls retain liability; closure requires generation and every call stopped", () => {
  let ledger = admitted();
  ledger = mutate(ledger, { type: "dispatch", runId: "run-one" }).ledger;
  ledger = mutate(ledger, { type: "uncertain", runId: "run-one" }).ledger;
  assert.equal(ledger.runs["run-one"]?.held, 100);
  ledger = mutate(ledger, { type: "register", runId: "run-one", callId: "call-one" }).ledger;
  ledger = mutate(ledger, { type: "cancel", runId: "run-one", commandId: "cancel-one" }).ledger;
  ledger = mutate(ledger, { type: "cleanup", runId: "run-one", stoppedCallIds: [], generationDisabled: true }).ledger;
  assert.equal(ledger.runs["run-one"]?.cleanup, "unconfirmed");
  assert.equal(mutate(ledger, { type: "settle", runId: "run-one", cost: 1 }).failure, "CleanupUnconfirmed");
  ledger = mutate(ledger, { type: "cleanup", runId: "run-one", stoppedCallIds: ["call-one"], generationDisabled: true }).ledger;
  assert.equal(ledger.runs["run-one"]?.held, 100, "confirmed cleanup retains provisional billing liability");
  ledger = mutate(ledger, { type: "settle", runId: "run-one", cost: 1 }).ledger;
  assert.equal(ledger.runs["run-one"]?.held, 0);
  ledger = mutate(ledger, { type: "cleanup", runId: "run-one", stoppedCallIds: ["call-one"], generationDisabled: true }).ledger;
  assert.equal(ledger.runs["run-one"]?.accounting, "settled", "cleanup replay cannot reopen settled accounting");
  const replayedSettlement = mutate(ledger, { type: "settle", runId: "run-one", cost: 1 });
  assert.equal(replayedSettlement.ledger.quarantined, false);
  assert.equal(mutate(ledger, { type: "settle", runId: "run-one", cost: 2 }).failure, "SpecConflict");
  ledger = mutate(ledger, { type: "register", runId: "run-one", callId: "late-call" }).ledger;
  assert.equal(ledger.quarantined, true); assert.equal(ledger.runs["run-one"]?.cleanup, "unconfirmed");
});

test("callback identity conflicts and restored epochs fail closed", () => {
  let ledger = admitted();
  assert.equal(mutate(ledger, { type: "dispatch", runId: "run-one" }, 2).failure, "EpochMismatch");
  ledger = mutate(ledger, { type: "register", runId: "run-one", callId: "call-one" }).ledger;
  const event = { type: "event", runId: "run-one", eventId: "event-one", hash: digest, callId: "call-one" } as const;
  ledger = mutate(ledger, event).ledger;
  assert.equal(mutate(ledger, { ...event, hash: "b".repeat(64) }).failure, "SpecConflict");
  assert.equal(mutate(ledger, { ...event, callId: "unowned" }).failure, "SchemaRejected");
  ledger.quarantined = true;
  assert.equal(mutate(ledger, { type: "dispatch", runId: "run-one" }).failure, "Quarantined");
});

test("invalid specs, safe-integer money and expired/revoked grants cannot admit", () => {
  for (const invalid of [spec("bad/path"), spec("x", -1), spec("x", Number.MAX_SAFE_INTEGER + 1)]) {
    assert.equal(mutate(initialLedger(1), { type: "submit", spec: invalid, digest }).failure, "SchemaRejected");
  }
  const expired = initialLedger(1, now);
  assert.equal(mutate(expired, { type: "submit", spec: spec(), digest }).failure, "AuthorizationExpired");
  const revoked = initialLedger(1); revoked.grant.revoked = true;
  assert.equal(mutate(revoked, { type: "submit", spec: spec(), digest }).failure, "AuthorizationExpired");
});

test("opaque IDs survive JSON persistence even when matching object property names", () => {
  for (const key of ["constructor", "__proto__", "toString"]) {
    const request = spec(key);
    let ledger = mutate(initialLedger(1), { type: "submit", spec: request, digest }).ledger;
    ledger = JSON.parse(JSON.stringify(ledger)) as Ledger;
    assert.equal(mutate(ledger, { type: "submit", spec: request, digest }).run?.spec.runId, key);
    ledger = mutate(ledger, { type: "register", runId: key, callId: "call-one" }).ledger;
    ledger = mutate(ledger, { type: "event", runId: key, callId: "call-one", eventId: key, hash: digest }).ledger;
    assert.equal(ledger.runs[key]?.events[key], digest);
  }
});

test("compound outbox identity cannot collide across valid hyphenated IDs", () => {
  const first = { ...spec("a-ensure_workflow-b"), commandId: "c" };
  const second = { ...spec("a"), commandId: "b-ensure_workflow-c" };
  let ledger = mutate(initialLedger(1), { type: "submit", spec: first, digest }).ledger;
  ledger = mutate(ledger, { type: "submit", spec: second, digest }).ledger;
  assert.equal(Object.keys(ledger.outbox).length, 2);
  assert.deepEqual(new Set(Object.values(ledger.outbox).map(item => item.runId)), new Set([first.runId, second.runId]));
});

test("cleanup retires launch and execution permissions even without a terminal report", () => {
  let ledger = admitted();
  const cleaned = mutate(ledger, { type: "cleanup", runId: "run-one", stoppedCallIds: [], generationDisabled: true }).ledger;
  assert.equal(mutate(cleaned, { type: "dispatch", runId: "run-one" }).newPermission, false);
  ledger = mutate(ledger, { type: "dispatch", runId: "run-one" }).ledger;
  ledger = mutate(ledger, { type: "register", runId: "run-one", callId: "call-one" }).ledger;
  for (const generationDisabled of [true, false]) {
    const retired = mutate(ledger, { type: "cleanup", runId: "run-one", stoppedCallIds: ["call-one"], generationDisabled }).ledger;
    assert.equal(mutate(retired, { type: "execute", runId: "run-one", callId: "call-one", bodyId: "late-body" }).newPermission, false);
  }
});

test("quarantine denies new work while permitting recorded containment and accounting", () => {
  let ledger = admitted(); ledger.quarantined = true;
  assert.equal(mutate(ledger, { type: "dispatch", runId: "run-one" }).failure, "Quarantined");
  const cancelled = mutate(ledger, { type: "cancel", runId: "run-one", commandId: "contain-one" });
  assert.equal(cancelled.failure, undefined); ledger = cancelled.ledger;
  ledger = mutate(ledger, { type: "register", runId: "run-one", callId: "late-call" }).ledger;
  ledger = mutate(ledger, { type: "cleanup", runId: "run-one", stoppedCallIds: ["late-call"], generationDisabled: true }).ledger;
  const settled = mutate(ledger, { type: "settle", runId: "run-one", cost: 1 });
  assert.equal(settled.failure, undefined);
  assert.equal(settled.ledger.quarantined, true);
  assert.equal(settled.run?.accounting, "settled");
});

test("artifact publication binds claimant/spec and preserves scientific gate separately", () => {
  let ledger = admitted();
  ledger = mutate(ledger, { type: "dispatch", runId: "run-one" }).ledger;
  ledger = mutate(ledger, { type: "register", runId: "run-one", callId: "call-one" }).ledger;
  ledger = mutate(ledger, { type: "execute", runId: "run-one", callId: "call-one", bodyId: "body-one" }).ledger;
  const receipt: ArtifactReceipt = {
    schemaVersion: 1, runId: "run-one", specDigest: digest, bodyId: "body-one",
    key: `runs/run-one/${digest}.json`, digest, bytes: 100, validity: "valid", decisionGate: "failed",
  };
  const revision = ledger.runs["run-one"]!.revision;
  for (const invalid of [{...receipt,bodyId:"other"},{...receipt,specDigest:"b".repeat(64)},
    {...receipt,key:"../escape.json"},{...receipt,bytes:4097}]) {
    assert.equal(mutate(ledger, { type:"artifact",runId:"run-one",revision,receipt:invalid }).failure,"InvalidManifest");
  }
  const published = mutate(ledger, { type:"artifact",runId:"run-one",revision,receipt });
  assert.equal(published.run?.outcome,"failed");
  assert.equal(published.run?.receipt?.validity,"valid");
  assert.equal(published.run?.cleanup,"pending");
  assert.equal(published.run?.held,100);
  assert.equal(mutate(published.ledger, {type:"artifact",runId:"run-one",revision:published.run!.revision,
    receipt:{...receipt,bytes:101} }).failure,"SpecConflict");
});

test("operational histories preserve money, immutable specs, and one-shot permissions", () => {
  let random = 42;
  const choose = (max: number): number => { random = (Math.imul(random,1664525)+1013904223)>>>0; return random%max; };
  for (let history = 0; history < 200; history++) {
    let ledger = admitted(); let dispatches = 0; let bodies = 0;
    const acceptedSpec = JSON.stringify(ledger.runs["run-one"]!.spec);
    for (let step = 0; step < 40; step++) {
      const commands: Command[] = [
        {type:"dispatch",runId:"run-one"},
        {type:"register",runId:"run-one",callId:"call-one"},
        {type:"execute",runId:"run-one",callId:"call-one",bodyId:"body-one"},
        {type:"uncertain",runId:"run-one"},
        {type:"cancel",runId:"run-one",commandId:"cancel-one"},
        {type:"cleanup",runId:"run-one",stoppedCallIds:choose(2)?["call-one"]:[],generationDisabled:choose(2)===1},
        {type:"settle",runId:"run-one",cost:1},
      ];
      const command = commands[choose(commands.length)]!;
      const decision = mutate(ledger,command); ledger = decision.ledger;
      if (decision.newPermission && command.type==="dispatch") dispatches++;
      if (decision.newPermission && command.type==="execute") bodies++;
      const run = ledger.runs["run-one"]!;
      assert.ok(dispatches<=1 && bodies<=1);
      assert.ok(run.held>=0 && run.cost>=0 && run.held+run.cost<=run.spec.reservation);
      assert.equal(JSON.stringify(run.spec),acceptedSpec);
      assert.equal(ledger.grant.consumed,1);
      if (run.accounting==="settled") assert.equal(run.held,0);
    }
  }
});

test("deadline expiry requests cleanup, retains liability, and preserves prior cancellation", () => {
  const ledger = admitted();
  assert.equal(mutate(ledger,{type:"timeout",runId:"run-one"}).failure,"SchemaRejected");
  const expired = mutate(ledger,{type:"timeout",runId:"run-one"},1,now+60_000);
  assert.equal(expired.run?.outcome,"timed_out"); assert.equal(expired.run?.held,100);
  assert.equal(expired.run?.cleanup,"pending"); assert.equal(expired.run?.cancelRequested,true);
  const cancelled = mutate(ledger,{type:"cancel",runId:"run-one",commandId:"cancel-one"}).ledger;
  assert.equal(mutate(cancelled,{type:"timeout",runId:"run-one"},1,now+60_000).run?.outcome,"cancelled");
});

test("contradictory cleanup after settlement cannot admit spending around a late call", () => {
  let ledger = admitted();
  ledger = mutate(ledger,{type:"cancel",runId:"run-one",commandId:"cancel-one"}).ledger;
  ledger = mutate(ledger,{type:"cleanup",runId:"run-one",stoppedCallIds:[],generationDisabled:true}).ledger;
  ledger = mutate(ledger,{type:"settle",runId:"run-one",cost:1}).ledger;
  ledger = mutate(ledger,{type:"cleanup",runId:"run-one",stoppedCallIds:[],generationDisabled:false}).ledger;
  ledger = mutate(ledger,{type:"register",runId:"run-one",callId:"new-late-call"}).ledger;
  assert.equal(ledger.quarantined,true);
  assert.equal(ledger.runs["run-one"]?.cleanup,"unconfirmed");
  assert.equal(mutate(ledger,{type:"submit",spec:spec("second",999),digest}).failure,"Quarantined");
});
