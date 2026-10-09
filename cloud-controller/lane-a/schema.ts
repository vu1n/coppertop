import * as Schema from "effect/Schema";

const Id = Schema.String.check(Schema.isPattern(/^[a-zA-Z0-9_-]{1,80}$/));
const Digest = Schema.String.check(Schema.isPattern(/^[a-f0-9]{64}$/));
const Integer = Schema.Number.check(
  Schema.isInt(),
  Schema.isBetween({ minimum: 0, maximum: Number.MAX_SAFE_INTEGER }),
);
export const Spec = Schema.Struct({
  schemaVersion: Schema.Literal(1),
  runId: Id,
  commandId: Id,
  idempotencyKey: Id,
  principal: Schema.Literal("synthetic-operator"),
  grantId: Schema.Literal("synthetic-grant"),
  preset: Schema.Literal("fake-v1"),
  sourceDigest: Digest,
  inputDigest: Digest,
  deployment: Schema.Literal("fake-deployment-v1"),
  reservation: Integer,
  deadline: Integer,
  engine: Schema.Literal("native-cf"),
  definitionVersion: Schema.Literal(1),
});
export const Receipt = Schema.Struct({
  schemaVersion: Schema.Literal(1),
  runId: Id,
  specDigest: Digest,
  bodyId: Id,
  key: Schema.String,
  digest: Digest,
  bytes: Integer,
  validity: Schema.Literal("valid"),
  decisionGate: Schema.Literals(["passed", "failed"]),
});
export const Controls = Schema.Struct({
  firstDelayMs: Schema.optionalKey(Integer),
  finishExternally: Schema.optionalKey(Schema.Boolean),
  loseReply: Schema.optionalKey(Schema.Boolean),
  dropWake: Schema.optionalKey(Schema.Boolean),
  deferWorkflowMs: Schema.optionalKey(Integer),
});
export type Controls = typeof Controls.Type;
export const Submit = Schema.Struct({
  epoch: Integer,
  spec: Spec,
  controls: Schema.optionalKey(Controls),
});
export const RunId = Schema.Struct({ epoch: Integer, runId: Id });
export const Cancel = Schema.Struct({
  epoch: Integer,
  runId: Id,
  commandId: Id,
});
export const Claim = Schema.Struct({
  epoch: Integer,
  runId: Id,
  callId: Id,
  bodyId: Id,
});
export const Artifact = Schema.Struct({
  epoch: Integer,
  runId: Id,
  revision: Integer,
  receipt: Receipt,
});
export const WorkflowPayload = Schema.Struct({
  epoch: Integer,
  runId: Id,
  definitionVersion: Schema.Literal(1),
  schemaVersion: Schema.Literal(1),
});
export const ProviderCall = Schema.Struct({
  id: Id,
  runId: Id,
  generation: Schema.String,
  state: Schema.Literals(["queued", "running", "completed", "cancelled"]),
  cost: Integer,
  bodyId: Schema.optionalKey(Id),
});
export const ProviderCalls = Schema.Array(ProviderCall);
export function decode<S extends Schema.ConstraintDecoder<unknown>>(
  schema: S,
  value: unknown,
): S["Type"] {
  return Schema.decodeUnknownSync(schema, { onExcessProperty: "error" })(value);
}
export const StoredRun = Schema.Struct({
  spec: Spec,
  digest: Digest,
  epoch: Integer,
  revision: Integer,
  phase: Schema.Literals([
    "accepted",
    "dispatch_claimed",
    "launch_unknown",
    "running",
    "cleanup_pending",
    "closed",
    "recovery_required",
  ]),
  cancelRequested: Schema.Boolean,
  dispatchConsumed: Schema.Boolean,
  bodyClaimant: Schema.optionalKey(Id),
  callIds: Schema.Array(Id),
  outcome: Schema.Literals([
    "pending",
    "succeeded",
    "failed",
    "cancelled",
    "timed_out",
  ]),
  cleanup: Schema.Literals(["pending", "confirmed", "unconfirmed"]),
  accounting: Schema.Literals(["reserved", "provisional", "settled"]),
  held: Integer,
  cost: Integer,
  receipt: Schema.optionalKey(Receipt),
  events: Schema.Record(Schema.String, Digest),
});
export const StoredLedger = Schema.Struct({
  schemaVersion: Schema.Literal(1),
  epoch: Integer,
  quarantined: Schema.Boolean,
  grant: Schema.Struct({
    limit: Integer,
    maxRuns: Integer,
    concurrency: Integer,
    consumed: Integer,
    revoked: Schema.Boolean,
    expires: Integer,
  }),
  runs: Schema.Record(Schema.String, StoredRun),
  idempotency: Schema.Record(Schema.String, Id),
  outbox: Schema.Record(
    Schema.String,
    Schema.Struct({
      id: Schema.String,
      runId: Id,
      kind: Schema.Literals(["ensure_workflow", "cleanup", "wake"]),
      delivered: Schema.Boolean,
    }),
  ),
});
export const ArtifactContent = Schema.Struct({
  schemaVersion: Schema.Literal(1),
  runId: Id,
  specDigest: Digest,
  bodyId: Id,
  decisionGate: Schema.Literals(["passed", "failed"]),
  validity: Schema.Literal("valid"),
  result: Schema.Literal("synthetic-valid"),
});
export const FakeArtifact = Schema.Struct({
  key: Schema.String,
  digest: Digest,
  content: Schema.String,
  bytes: Integer,
});
export const RunResponse = Schema.Struct({
  run: StoredRun,
  controls: Controls,
});
export const Decision = Schema.Struct({
  ledger: StoredLedger,
  run: Schema.optionalKey(StoredRun),
  newPermission: Schema.optionalKey(Schema.Boolean),
  failure: Schema.optionalKey(
    Schema.Literals([
      "SchemaRejected",
      "EpochMismatch",
      "Quarantined",
      "BudgetDenied",
      "AuthorizationExpired",
      "SpecConflict",
      "UnknownRun",
      "StaleRevision",
      "InvalidManifest",
      "CleanupUnconfirmed",
    ]),
  ),
});

export const FenceAck = Schema.Struct({ disabled: Schema.Literal(true) });
