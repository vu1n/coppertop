import {
  MODES,
  runId,
  canonical,
  type Envelope,
  type Observation,
} from "./contracts.ts";
import type {
  ArtifactReceipt,
  Ledger,
  ProviderCall,
} from "../shared/contracts.ts";
export interface IndependentSnapshot {
  calls: ProviderCall[];
  observations: Observation[];
  fenced: string[];
  artifacts: ArtifactReceipt[];
  closed: boolean;
}
export function acceptance(
  payload: Envelope,
  snapshot: IndependentSnapshot,
  ledger: Ledger,
  statuses: Record<string, string>,
) {
  const count = (mode: string, event: string) =>
    snapshot.observations.find(
      (row) => row.mode === mode && row.event === event,
    )?.count ?? 0;
  const gates = {
    control: count("control", "probe-complete") === 1,
    timeoutRetry:
      count("timeout-retry", "first-attempt") >= 2 &&
      count("timeout-retry", "probe-complete") === 1,
    durableSleep:
      count("sleep", "sleep-start") === 1 &&
      count("sleep", "sleep-complete") === 1,
    durableEvent:
      count("event", "wait-start") === 1 &&
      count("event", "event-received") === 1,
    eventTimeout:
      count("event-timeout", "wait-start") === 1 &&
      count("event-timeout", "event-timeout") === 1,
    cancellation:
      count("cancel", "wait-start") === 1 &&
      count("cancel", "cancel-observed") === 1 &&
      count("cancel", "probe-complete") === 0 &&
      statuses.cancel === "terminated",
    oneShot:
      snapshot.calls.length === 6 &&
      MODES.every(
        (probe) =>
          snapshot.calls.filter(
            (call) => call.runId === runId(payload.suiteId, probe),
          ).length === 1 && count(probe, "body") === 1,
      ),
    accounting:
      ledger.grant.consumed === 6 &&
      !ledger.quarantined &&
      MODES.every((probe) => {
        const run = ledger.runs[runId(payload.suiteId, probe)];
        return (
          run?.dispatchConsumed &&
          run.bodyClaimant === `body-${probe}` &&
          run.cleanup === "confirmed" &&
          run.accounting === "settled" &&
          run.held === 0 &&
          run.cost === 1 &&
          run.phase === "closed" &&
          run.outcome === (probe === "cancel" ? "cancelled" : "succeeded")
        );
      }),
    receipts:
      snapshot.artifacts.length === 5 &&
      MODES.filter((probe) => probe !== "cancel").every((probe) => {
        const run = ledger.runs[runId(payload.suiteId, probe)];
        return (
          run?.receipt?.decisionGate === "passed" &&
          snapshot.artifacts.some(
            (receipt) => canonical(receipt) === canonical(run.receipt!),
          )
        );
      }),
    containment:
      snapshot.calls.every((call) =>
        ["completed", "cancelled"].includes(call.state),
      ) && snapshot.fenced.length === 6,
    nativeTerminal: MODES.every(
      (probe) =>
        statuses[probe] === (probe === "cancel" ? "terminated" : "complete"),
    ),
  };
  return {
    version: 1,
    suiteId: payload.suiteId,
    buildDigest: payload.buildDigest,
    gates,
    supportedGatesPass: Object.values(gates).every(Boolean),
    hostProcessLoss: "untested",
    realProviderReady: false,
  };
}
