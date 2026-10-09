/** Engine-neutral persisted data. Effect codecs belong to each isolated lane. */
export type EngineKind = "native-cf" | "effect-do";
export interface RunSpec {
  schemaVersion: 1;
  runId: string;
  commandId: string;
  idempotencyKey: string;
  principal: "synthetic-operator";
  grantId: "synthetic-grant";
  preset: "fake-v1";
  sourceDigest: string;
  inputDigest: string;
  deployment: "fake-deployment-v1";
  reservation: number;
  deadline: number;
  engine: EngineKind;
  definitionVersion: 1;
}
export type Phase = "accepted" | "dispatch_claimed" | "launch_unknown" | "running" | "cleanup_pending" | "closed" | "recovery_required";
export interface ArtifactReceipt {
  schemaVersion: 1;
  runId: string;
  specDigest: string;
  bodyId: string;
  key: string;
  digest: string;
  bytes: number;
  validity: "valid";
  decisionGate: "passed" | "failed";
}
export interface Run {
  spec: RunSpec;
  digest: string;
  epoch: number;
  revision: number;
  phase: Phase;
  cancelRequested: boolean;
  dispatchConsumed: boolean;
  bodyClaimant?: string;
  callIds: string[];
  outcome: "pending" | "succeeded" | "failed" | "cancelled" | "timed_out";
  cleanup: "pending" | "confirmed" | "unconfirmed";
  accounting: "reserved" | "provisional" | "settled";
  held: number;
  cost: number;
  receipt?: ArtifactReceipt;
  events: Record<string, string>;
}
export interface OutboxCommand {
  id: string;
  runId: string;
  kind: "ensure_workflow" | "cleanup" | "wake";
  delivered: boolean;
}
export interface Ledger {
  schemaVersion: 1;
  epoch: number;
  quarantined: boolean;
  grant: { limit: number; maxRuns: number; concurrency: number; consumed: number; revoked: boolean; expires: number };
  runs: Record<string, Run>;
  idempotency: Record<string, string>;
  outbox: Record<string, OutboxCommand>;
}
export type Command =
  | { type: "submit"; spec: RunSpec; digest: string }
  | { type: "dispatch"; runId: string }
  | { type: "register"; runId: string; callId: string }
  | { type: "execute"; runId: string; callId: string; bodyId: string }
  | { type: "uncertain"; runId: string }
  | { type: "cancel"; runId: string; commandId: string }
  | { type: "event"; runId: string; eventId: string; hash: string; callId: string }
  | { type: "artifact"; runId: string; revision: number; receipt: ArtifactReceipt }
  | { type: "cleanup"; runId: string; stoppedCallIds: string[]; generationDisabled: boolean }
  | { type: "settle"; runId: string; cost: number }
  | { type: "delivered"; outboxId: string };
export interface Mutation { epoch: number; now: number; command: Command }
export type Failure = "SchemaRejected" | "EpochMismatch" | "Quarantined" | "BudgetDenied" | "AuthorizationExpired" | "SpecConflict" | "UnknownRun" | "StaleRevision" | "InvalidManifest" | "CleanupUnconfirmed";
export interface Decision { ledger: Ledger; run?: Run; newPermission?: boolean; failure?: Failure }

export interface ProviderCall {
  id: string;
  runId: string;
  generation: string;
  state: "queued" | "running" | "completed" | "cancelled";
  bodyId?: string;
  cost: number;
}

export interface FakeArtifact { key: string; digest: string; content: string; bytes: number }

export const jsonResponse = (value: unknown, status = 200): Response =>
  new Response(JSON.stringify(value), { status, headers: { "content-type": "application/json" } });

export function isRecord(value: unknown): value is Record<string, unknown> {
  return typeof value === "object" && value !== null && !Array.isArray(value);
}
