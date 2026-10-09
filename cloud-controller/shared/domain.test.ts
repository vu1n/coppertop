import { test } from "node:test";
import assert from "node:assert/strict";
import { initialLedger, transition } from "./domain.ts";
import type { Command, Ledger, RunSpec } from "./contracts.ts";

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
