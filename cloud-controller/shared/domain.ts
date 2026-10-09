import type { ArtifactReceipt, Decision, Failure, Ledger, Mutation, Run, RunSpec } from "./contracts.ts";

const digestPattern = /^[a-f0-9]{64}$/;
const idPattern = /^[a-zA-Z0-9_-]{1,80}$/;
const money = (value: number): boolean => Number.isSafeInteger(value) && value >= 0;
const dictionary = <T>(): Record<string, T> => Object.create(null) as Record<string, T>;
export const initialLedger = (epoch: number, expires = Number.MAX_SAFE_INTEGER): Ledger => ({
  schemaVersion: 1, epoch, quarantined: false,
  grant: { limit: 1000, maxRuns: 20, concurrency: 2, consumed: 0, revoked: false, expires },
  runs: dictionary(), idempotency: dictionary(), outbox: dictionary(),
});

export function validSpec(spec: RunSpec, now: number): boolean {
  return spec.schemaVersion === 1 && spec.definitionVersion === 1 &&
    [spec.runId, spec.commandId, spec.idempotencyKey].every(id => typeof id === "string" && idPattern.test(id)) &&
    spec.principal === "synthetic-operator" && spec.grantId === "synthetic-grant" &&
    spec.preset === "fake-v1" && spec.deployment === "fake-deployment-v1" &&
    digestPattern.test(spec.sourceDigest) && digestPattern.test(spec.inputDigest) &&
    ["native-cf", "effect-do"].includes(spec.engine) && money(spec.reservation) && spec.reservation > 0 &&
    Number.isSafeInteger(spec.deadline) && spec.deadline > 0 && spec.deadline <= now + 3600_000;
}

export function validReceipt(run: Run, receipt: ArtifactReceipt): boolean {
  return receipt.schemaVersion === 1 && receipt.runId === run.spec.runId &&
    receipt.specDigest === run.digest && typeof run.bodyClaimant === "string" &&
    receipt.bodyId === run.bodyClaimant && idPattern.test(receipt.bodyId) &&
    receipt.validity === "valid" && ["passed", "failed"].includes(receipt.decisionGate) &&
    receipt.key === `runs/${run.spec.runId}/${receipt.digest}.json` && digestPattern.test(receipt.digest) &&
    Number.isSafeInteger(receipt.bytes) && receipt.bytes > 0 && receipt.bytes <= 4096;
}

/** Pure transaction decision; the caller persists ledger and due wake atomically. */
export function transition(current: Ledger, mutation: Mutation): Decision {
  const reject = (failure: Failure): Decision => ({ ledger: current, failure });
  if (current.schemaVersion !== 1 || !Number.isSafeInteger(mutation.now)) return reject("SchemaRejected");
  if (current.epoch !== mutation.epoch) return reject("EpochMismatch");
  if (current.quarantined && ["submit", "dispatch", "execute", "artifact"].includes(mutation.command.type)) return reject("Quarantined");
  const command = mutation.command;
  const ledger = structuredClone(current);
  // JSON and structuredClone rebuild ordinary objects; IDs must remain data keys.
  for (const map of [ledger.runs, ledger.idempotency, ledger.outbox]) Object.setPrototypeOf(map, null);
  for (const run of Object.values(ledger.runs)) Object.setPrototypeOf(run.events, null);
  if (command.type === "submit") {
    if (!validSpec(command.spec, mutation.now) || !digestPattern.test(command.digest)) return reject("SchemaRejected");
    const previous = ledger.idempotency[command.spec.idempotencyKey];
    if (previous) {
      const run = ledger.runs[previous]!;
      return run.digest === command.digest ? { ledger: current, run } : reject("SpecConflict");
    }
    if (command.spec.deadline <= mutation.now) return reject("SchemaRejected");
    if (ledger.runs[command.spec.runId]) return reject("SpecConflict");
    const grant = ledger.grant;
    if (grant.revoked || grant.expires <= mutation.now) return reject("AuthorizationExpired");
    const runs = Object.values(ledger.runs);
    const liability = runs.reduce((sum, run) => sum + run.held + run.cost, 0);
    if (grant.consumed >= grant.maxRuns || runs.filter(run => run.cleanup !== "confirmed").length >= grant.concurrency ||
      liability + command.spec.reservation > grant.limit) return reject("BudgetDenied");
    const run: Run = {
      spec: structuredClone(command.spec), digest: command.digest, epoch: ledger.epoch, revision: 1,
      phase: "accepted", cancelRequested: false, dispatchConsumed: false, callIds: [],
      outcome: "pending", cleanup: "pending", accounting: "reserved", held: command.spec.reservation,
      cost: 0, events: dictionary(),
    };
    ledger.runs[run.spec.runId] = run;
    ledger.idempotency[run.spec.idempotencyKey] = run.spec.runId;
    ledger.grant.consumed++;
    enqueue(ledger, run, "ensure_workflow", run.spec.commandId);
    return { ledger, run };
  }
  if (command.type === "delivered") {
    const item = ledger.outbox[command.outboxId];
    if (item) item.delivered = true;
    return { ledger };
  }
  const run = ledger.runs[command.runId];
  if (!run) return reject("UnknownRun");
  if (run.epoch !== mutation.epoch) return reject("EpochMismatch");
  let newPermission: boolean | undefined;
  switch (command.type) {
    case "dispatch":
    case "execute": {
      newPermission = false;
      if (run.cancelRequested || run.cleanup !== "pending" ||
        ["closed", "cleanup_pending", "recovery_required"].includes(run.phase) || mutation.now >= run.spec.deadline ||
        ledger.grant.revoked || mutation.now >= ledger.grant.expires) break;
      if (command.type === "dispatch" && !run.dispatchConsumed) {
        run.dispatchConsumed = true; run.phase = "dispatch_claimed"; newPermission = true;
      } else if (command.type === "execute" && run.dispatchConsumed && !run.bodyClaimant &&
        run.callIds.includes(command.callId) && idPattern.test(command.bodyId)) {
        run.bodyClaimant = command.bodyId; run.phase = "running"; newPermission = true;
      }
      break;
    }
    case "register":
      if (!idPattern.test(command.callId)) return reject("SchemaRejected");
      if (!run.callIds.includes(command.callId)) {
        run.callIds.push(command.callId);
        // Late/duplicate starts remain owned even after a previous cleanup observation.
        if (run.cleanup === "confirmed" || run.accounting === "settled") {
          run.cleanup = "unconfirmed"; run.phase = "recovery_required"; ledger.quarantined = true;
        }
        if (run.cancelRequested) enqueue(ledger, run, "cleanup", `late-${command.callId}`);
      }
      break;
    case "uncertain":
      if (run.dispatchConsumed && run.phase !== "closed" && run.phase !== "running") run.phase = "launch_unknown";
      break;
    case "cancel":
      if (!idPattern.test(command.commandId)) return reject("SchemaRejected");
      run.cancelRequested = true;
      if (run.phase !== "closed") { run.outcome = "cancelled"; run.phase = "cleanup_pending"; }
      enqueue(ledger, run, "cleanup", command.commandId);
      break;
    case "timeout":
      if (mutation.now < run.spec.deadline) return reject("SchemaRejected");
      if (run.phase !== "closed") {
        run.cancelRequested = true;
        if (run.outcome === "pending") run.outcome = "timed_out";
        run.phase = "cleanup_pending";
        enqueue(ledger, run, "cleanup", "deadline-expired");
      }
      break;
    case "event":
      if (!idPattern.test(command.eventId) || !digestPattern.test(command.hash) || !run.callIds.includes(command.callId)) return reject("SchemaRejected");
      if (run.events[command.eventId] && run.events[command.eventId] !== command.hash) return reject("SpecConflict");
      if (!run.events[command.eventId]) { run.events[command.eventId] = command.hash; enqueue(ledger, run, "wake", command.eventId); }
      break;
    case "artifact":
      if (run.revision !== command.revision || run.cancelRequested || run.phase === "closed") return reject("StaleRevision");
      if (!validReceipt(run, command.receipt)) return reject("InvalidManifest");
      if (run.receipt && JSON.stringify(run.receipt) !== JSON.stringify(command.receipt)) return reject("SpecConflict");
      run.receipt = structuredClone(command.receipt); run.phase = "cleanup_pending";
      run.outcome = command.receipt.decisionGate === "passed" ? "succeeded" : "failed";
      break;
    case "cleanup":
      if (!command.generationDisabled || !run.callIds.every(id => command.stoppedCallIds.includes(id))) {
        if (run.cleanup === "confirmed" || run.accounting === "settled") ledger.quarantined = true;
        run.cleanup = "unconfirmed"; run.phase = "recovery_required";
      } else {
        run.cleanup = "confirmed";
        if (run.outcome !== "pending") {
          run.phase = "closed";
          if (run.accounting !== "settled") run.accounting = "provisional";
        }
      }
      break;
    case "settle":
      if (run.cleanup !== "confirmed" || run.outcome === "pending") return reject("CleanupUnconfirmed");
      if (!money(command.cost)) return reject("SchemaRejected");
      if (run.accounting === "settled") return run.cost === command.cost ? { ledger: current, run } : reject("SpecConflict");
      if (command.cost > run.held) ledger.quarantined = true;
      run.cost = command.cost; run.held = 0; run.accounting = "settled";
      break;
  }
  run.revision++;
  return { ledger, run, ...(newPermission === undefined ? {} : { newPermission }) };
}

function enqueue(ledger: Ledger, run: Run, kind: "ensure_workflow" | "cleanup" | "wake", suffix: string): void {
  const id = `${run.spec.runId}:${kind}:${suffix}`;
  ledger.outbox[id] ??= { id, runId: run.spec.runId, kind, delivered: false };
}
