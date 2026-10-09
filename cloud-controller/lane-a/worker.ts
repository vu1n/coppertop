import { DurableObject } from "cloudflare:workers";
import * as Effect from "effect/Effect";
import * as Layer from "effect/Layer";
import * as ManagedRuntime from "effect/ManagedRuntime";
import * as SqliteClient from "@effect/sql-sqlite-do/SqliteClient";
import * as SqlClient from "effect/sql/SqlClient";
import { Workflow, WorkerEnvironment } from "effect-cf";
import { initialLedger, transition } from "../shared/domain.ts";
import {
  jsonResponse,
  type ArtifactReceipt,
  type Command,
  type Decision,
  type Ledger,
  type ProviderCall,
  type Run,
} from "../shared/contracts.ts";
import * as Codec from "./schema.ts";

// Defined by the test bundler: changing this constant changes deployed module bytes.
declare const COPPERTOP_DEPLOYMENT_EPOCH: number;
interface Env {
  COORDINATOR: DurableObjectNamespace;
  RUNS: Workflow;
  PROVIDER: Fetcher;
  SPIKE_LOCAL_ONLY: string;
  EXPECTED_EPOCH: string;
}
declare global {
  namespace Cloudflare {
    interface Env extends Record<string, unknown> {
      COORDINATOR: DurableObjectNamespace;
      RUNS: Workflow;
      PROVIDER: Fetcher;
      SPIKE_LOCAL_ONLY: string;
      EXPECTED_EPOCH: string;
    }
  }
}
const generation = (run: Run) =>
  `${run.spec.runId}-e${run.epoch}-v${run.spec.definitionVersion}`;
const canonicalSpec = (spec: Run["spec"]) =>
  JSON.stringify(
    Object.fromEntries(
      Object.entries(spec).sort(([a], [b]) => (a < b ? -1 : a > b ? 1 : 0)),
    ),
  );
class StorageRejected extends Error {
  constructor(readonly failure: "SchemaRejected" | "EpochMismatch") {
    super(failure);
  }
}
const instanceId = (runId: string, epoch: number) =>
  `run-${runId}-e${epoch}-v1`;
const post = (binding: Fetcher, path: string, body: unknown) =>
  binding.fetch(`http://synthetic${path}`, {
    method: "POST",
    body: JSON.stringify(body),
  });
async function hash(value: string): Promise<string> {
  return Array.from(
    new Uint8Array(
      await crypto.subtle.digest("SHA-256", new TextEncoder().encode(value)),
    ),
  )
    .map((b) => b.toString(16).padStart(2, "0"))
    .join("");
}

/** Domain ledger only: native Cloudflare Workflows own the durability engine. */
export class Coordinator extends DurableObject<Env> {
  private runtime: ManagedRuntime.ManagedRuntime<
    SqlClient.SqlClient | SqliteClient.SqliteClient,
    never
  >;
  constructor(ctx: DurableObjectState, env: Env) {
    super(ctx, env);
    this.runtime = ManagedRuntime.make(
      SqliteClient.layer({ storage: ctx.storage }),
    );
    ctx.storage.sql.exec(
      "CREATE TABLE IF NOT EXISTS ledger (id INTEGER PRIMARY KEY CHECK(id=1), value TEXT NOT NULL)",
    );
    ctx.storage.sql.exec(
      "CREATE TABLE IF NOT EXISTS controls (id TEXT PRIMARY KEY, value TEXT NOT NULL)",
    );
    ctx.storage.sql.exec(
      "CREATE TABLE IF NOT EXISTS diagnostic (id TEXT PRIMARY KEY, value INTEGER)",
    );
    ctx.storage.sql.exec(
      "CREATE TABLE IF NOT EXISTS workflow_due (run_id TEXT PRIMARY KEY, not_before INTEGER NOT NULL)",
    );
    ctx.storage.sql.exec(
      "CREATE TABLE IF NOT EXISTS workflow_instances (run_id TEXT PRIMARY KEY, instance_id TEXT NOT NULL)",
    );
  }
  private read(): Ledger {
    const row = this.ctx.storage.sql
      .exec<{ value: string }>("SELECT value FROM ledger WHERE id=1")
      .toArray()[0];
    return row
      ? this.decodeLedger(row.value)
      : initialLedger(Number(this.env.EXPECTED_EPOCH));
  }
  private decodeLedger(value: string): Ledger {
    try {
      const ledger = structuredClone(
        Codec.decode(Codec.StoredLedger, JSON.parse(value)),
      ) as unknown as Ledger;
      for (const [id, run] of Object.entries(ledger.runs))
        if (id !== run.spec.runId || run.epoch !== ledger.epoch)
          throw new Error("stored identity mismatch");
      for (const [key, id] of Object.entries(ledger.idempotency))
        if (ledger.runs[id]?.spec.idempotencyKey !== key)
          throw new Error("stored idempotency mismatch");
      for (const [id, item] of Object.entries(ledger.outbox))
        if (id !== item.id || !ledger.runs[item.runId])
          throw new Error("stored outbox mismatch");
      return ledger;
    } catch {
      throw new StorageRejected("SchemaRejected");
    }
  }
  private controls(id: string): Codec.Controls {
    const row = this.ctx.storage.sql
      .exec<{ value: string }>("SELECT value FROM controls WHERE id=?", id)
      .toArray()[0];
    return row ? Codec.decode(Codec.Controls, JSON.parse(row.value)) : {};
  }
  private workflowNotBefore(runId: string): number {
    return (
      this.ctx.storage.sql
        .exec<{
          not_before: number;
        }>("SELECT not_before FROM workflow_due WHERE run_id=?", runId)
        .toArray()[0]?.not_before ?? 0
    );
  }
  private knownInstance(runId: string): string | undefined {
    return this.ctx.storage.sql
      .exec<{
        instance_id: string;
      }>("SELECT instance_id FROM workflow_instances WHERE run_id=?", runId)
      .toArray()[0]?.instance_id;
  }
  private rememberInstance(runId: string, id: string): void {
    this.ctx.storage.sql.exec(
      "INSERT OR IGNORE INTO workflow_instances(run_id,instance_id) VALUES(?,?)",
      runId,
      id,
    );
  }
  private nextDue(
    ledger: Ledger,
    now: number,
    retryMs: number,
  ): number | undefined {
    const deadlines = Object.values(ledger.runs)
      .filter((r) => r.phase !== "closed" && !r.cancelRequested)
      .map((r) => r.spec.deadline);
    const outbox = Object.values(ledger.outbox)
      .filter((c) => !c.delivered)
      .map((c) => {
        const run = ledger.runs[c.runId]!;
        return c.kind === "ensure_workflow" &&
          !run.cancelRequested &&
          !ledger.quarantined
          ? Math.max(now + retryMs, this.workflowNotBefore(c.runId))
          : now + retryMs;
      });
    const due = [...deadlines, ...outbox];
    return due.length ? Math.min(...due) : undefined;
  }
  private async mutate(
    command: Command,
    epoch: number,
    controls?: Codec.Controls,
  ): Promise<Decision> {
    const self = this;
    return this.runtime.runPromise(
      Effect.gen(function* () {
        const sql = yield* SqlClient.SqlClient;
        return yield* sql.withTransaction(
          Effect.gen(function* () {
            const rows = yield* sql<{
              value: string;
            }>`SELECT value FROM ledger WHERE id=1`;
            const current: Ledger = rows[0]
              ? self.decodeLedger(rows[0].value)
              : initialLedger(Number(self.env.EXPECTED_EPOCH));
            if (
              current.epoch !== Number(self.env.EXPECTED_EPOCH) ||
              current.epoch !== COPPERTOP_DEPLOYMENT_EPOCH
            )
              throw new StorageRejected("EpochMismatch");
            const result = transition(current, {
              epoch,
              now: Date.now(),
              command,
            });
            if (!result.failure) {
              yield* sql`INSERT OR REPLACE INTO ledger(id,value) VALUES(1,${JSON.stringify(result.ledger)})`;
              if (command.type === "submit" && controls)
                yield* sql`INSERT OR IGNORE INTO controls(id,value) VALUES(${command.spec.runId},${JSON.stringify(controls)})`;
              if (command.type === "submit")
                yield* sql`INSERT OR IGNORE INTO workflow_due(run_id,not_before) VALUES(${command.spec.runId},${Date.now() + (controls?.deferWorkflowMs ?? 100)})`;
              // Per-run synthetic launch delay never delays another run's safety deadline or cleanup.
              // Persist SQL/outbox/due intent and preserve the already-armed alarm atomically.
              const now = Date.now();
              const due = self.nextDue(result.ledger, now, 100);
              const armed = yield* Effect.promise(() =>
                self.ctx.storage.getAlarm(),
              );
              if (due !== undefined)
                yield* Effect.promise(() =>
                  self.ctx.storage.setAlarm(
                    Math.max(now, Math.min(due, armed ?? Infinity)),
                  ),
                );
            }
            return result;
          }),
        );
      }),
    );
  }
  private async providerCalls(runId: string): Promise<ProviderCall[]> {
    const response = await post(this.env.PROVIDER, "/lookup", { runId });
    if (!response.ok) throw new Error(`provider lookup ${response.status}`);
    return [...Codec.decode(Codec.ProviderCalls, await response.json())];
  }
  private async launch(runId: string, epoch: number): Promise<Decision> {
    const claim = await this.mutate({ type: "dispatch", runId }, epoch);
    if (claim.newPermission) {
      const response = await post(this.env.PROVIDER, "/spawn", {
        runId,
        generation: generation(claim.run!),
        loseReply: this.controls(runId).loseReply,
      });
      if (response.ok) {
        const call = Codec.decode(Codec.ProviderCall, await response.json());
        await this.mutate({ type: "register", runId, callId: call.id }, epoch);
      } else await this.mutate({ type: "uncertain", runId }, epoch);
    }
    return {
      ledger: this.read(),
      run: this.read().runs[runId],
      newPermission: claim.newPermission,
      ...(claim.failure ? { failure: claim.failure } : {}),
    };
  }
  private async reconcile(runId: string, epoch: number): Promise<void> {
    let run = this.read().runs[runId];
    if (!run || this.read().epoch !== epoch) return;
    const calls = await this.providerCalls(runId);
    for (const call of calls) {
      await this.mutate({ type: "register", runId, callId: call.id }, epoch);
    }
    run = this.read().runs[runId]!;
    for (const call of calls.filter((c) => c.state === "completed")) {
      await this.mutate(
        {
          type: "event",
          runId,
          eventId: `done-${call.id}`,
          hash: await hash(JSON.stringify(call)),
          callId: call.id,
        },
        epoch,
      );
    }
    if (run.cancelRequested || run.receipt || run.outcome !== "pending") {
      // The fake provider offers a durable generation fence; a real gateway must supply an equivalent contract.
      const disabled = await post(this.env.PROVIDER, "/disable", {
        generation: generation(run),
      });
      if (!disabled.ok) throw new Error(`provider disable ${disabled.status}`);
      Codec.decode(Codec.FenceAck, await disabled.json());
      // Once fenced, enumerate again: a late launch can commit after the initial lookup but before disable.
      const fenced = await this.providerCalls(runId);
      for (const call of fenced)
        await this.mutate({ type: "register", runId, callId: call.id }, epoch);
      for (const call of fenced.filter(
        (c) => !["completed", "cancelled"].includes(c.state),
      ))
        await post(this.env.PROVIDER, "/cancel", { callId: call.id });
      const stopped = await this.providerCalls(runId);
      // Every final observed call enters ownership before cleanup proves all owned calls terminal.
      for (const call of stopped)
        await this.mutate({ type: "register", runId, callId: call.id }, epoch);
      const result = await this.mutate(
        {
          type: "cleanup",
          runId,
          stoppedCallIds: stopped
            .filter((c) => ["completed", "cancelled"].includes(c.state))
            .map((c) => c.id),
          generationDisabled: true,
        },
        epoch,
      );
      if (
        result.run?.cleanup === "confirmed" &&
        result.run.outcome !== "pending"
      )
        await this.mutate(
          {
            type: "settle",
            runId,
            cost: stopped.reduce((sum, c) => sum + c.cost, 0),
          },
          epoch,
        );
    }
  }
  async alarm(): Promise<void> {
    let ledger: Ledger;
    try {
      ledger = this.read();
    } catch (cause) {
      if (cause instanceof StorageRejected) return;
      throw cause;
    }
    if (
      ledger.epoch !== Number(this.env.EXPECTED_EPOCH) ||
      ledger.epoch !== COPPERTOP_DEPLOYMENT_EPOCH
    )
      return;
    // Alarm repairs control outbox and safety cleanup. Native Workflow owns normal status reads.
    for (const run of Object.values(ledger.runs).filter(
      (r) => r.phase !== "closed",
    )) {
      if (Date.now() >= run.spec.deadline && !run.cancelRequested)
        await this.mutate(
          { type: "timeout", runId: run.spec.runId },
          ledger.epoch,
        );
    }
    ledger = this.read();
    for (const item of Object.values(ledger.outbox).filter(
      (c) => !c.delivered,
    )) {
      const run = ledger.runs[item.runId]!;
      if (
        item.kind === "ensure_workflow" &&
        !run.cancelRequested &&
        !ledger.quarantined
      ) {
        if (Date.now() < this.workflowNotBefore(item.runId)) continue;
        try {
          await this.env.RUNS.create({
            id: instanceId(item.runId, ledger.epoch),
            params: {
              runId: item.runId,
              epoch: ledger.epoch,
              definitionVersion: 1,
              schemaVersion: 1,
            },
          });
        } catch (cause) {
          const existing = await this.env.RUNS.get(
            instanceId(item.runId, ledger.epoch),
          );
          const status = await existing.status();
          if (status.status === "unknown") throw cause;
        }
        // Retiring an outbox item does not prove native creation. Record only a successful create/existing-instance observation.
        this.rememberInstance(item.runId, instanceId(item.runId, ledger.epoch));
      } else if (item.kind === "wake" && !run.cancelRequested) {
        if (!this.knownInstance(item.runId)) continue;
        // Fault injection drops one notification while retaining its durable outbox record.
        const controls = this.controls(item.runId);
        if (controls.dropWake) {
          this.ctx.storage.sql.exec(
            "UPDATE controls SET value=? WHERE id=?",
            JSON.stringify({ ...controls, dropWake: false }),
            item.runId,
          );
          continue;
        }
        const instance = await this.env.RUNS.get(
          instanceId(item.runId, ledger.epoch),
        );
        await instance.sendEvent({
          type: "provider-done",
          payload: { runId: item.runId },
        });
      } else if (item.kind === "cleanup") {
        await this.reconcile(item.runId, ledger.epoch);
        const contained = this.read().runs[item.runId]!;
        // Retain durable cleanup authority until independently observed containment succeeds.
        if (contained.cleanup !== "confirmed") continue;
        const createdId = this.knownInstance(item.runId);
        if (run.cancelRequested && createdId) {
          const instance = await this.env.RUNS.get(createdId);
          const status = await instance.status();
          if (!["complete", "terminated", "errored"].includes(status.status))
            await instance.terminate();
          // Unclassified engine failures propagate, keeping cleanup intent undelivered for native alarm retry.
        }
      }
      await this.mutate({ type: "delivered", outboxId: item.id }, ledger.epoch);
    }
    ledger = this.read();
    const now = Date.now();
    const due = this.nextDue(ledger, now, 250);
    const armed = await this.ctx.storage.getAlarm();
    if (due !== undefined)
      await this.ctx.storage.setAlarm(
        Math.max(now, Math.min(due, armed ?? Infinity)),
      );
  }
  private async diagnostic(): Promise<unknown> {
    const self = this;
    return this.runtime.runPromise(
      Effect.gen(function* () {
        const sql = yield* SqlClient.SqlClient;
        yield* sql`DELETE FROM diagnostic`;
        yield* Effect.exit(
          sql.withTransaction(
            Effect.gen(function* () {
              yield* sql`INSERT INTO diagnostic VALUES('rollback',1)`;
              yield* Effect.fail("injected");
            }),
          ),
        );
        yield* Effect.exit(
          sql.withTransaction(
            Effect.gen(function* () {
              yield* sql`INSERT INTO diagnostic VALUES('interrupted',1)`;
              yield* Effect.interrupt;
            }),
          ),
        );
        yield* sql.withTransaction(
          Effect.gen(function* () {
            yield* sql`INSERT INTO diagnostic VALUES('outer',1)`;
            yield* Effect.exit(
              sql.withTransaction(
                Effect.gen(function* () {
                  yield* sql`INSERT INTO diagnostic VALUES('inner-rollback',1)`;
                  yield* Effect.fail("nested");
                }),
              ),
            );
            yield* Effect.yieldNow;
            yield* sql`INSERT INTO diagnostic VALUES('after-yield',1)`;
            yield* Effect.promise(() =>
              self.ctx.storage.setAlarm(Date.now() + 60_000),
            );
          }),
        );
        // Roll back both SQL and alarm update; previous committed alarm must survive.
        const committedAlarm = yield* Effect.promise(() =>
          self.ctx.storage.getAlarm(),
        );
        yield* Effect.exit(
          sql.withTransaction(
            Effect.gen(function* () {
              yield* sql`INSERT INTO diagnostic VALUES('alarm-rollback',1)`;
              yield* Effect.promise(() =>
                self.ctx.storage.setAlarm(Date.now() + 120_000),
              );
              yield* Effect.fail("alarm");
            }),
          ),
        );
        const alarm = yield* Effect.promise(() => self.ctx.storage.getAlarm());
        return {
          rows: yield* sql<{
            id: string;
            value: number;
          }>`SELECT * FROM diagnostic ORDER BY id`,
          alarmRolledBack: alarm === committedAlarm,
        };
      }),
    );
  }
  async fetch(request: Request): Promise<Response> {
    if (this.env.SPIKE_LOCAL_ONLY !== "true")
      return jsonResponse({ error: "local synthetic spike disabled" }, 403);
    const path = new URL(request.url).pathname;
    try {
      if (path === "/ledger") return jsonResponse(this.read());
      if (
        path === "/native-status" ||
        path === "/native-ensure" ||
        path === "/native-wait-start"
      ) {
        const b = Codec.decode(Codec.RunId, await request.json());
        const ledger = this.read();
        if (
          b.epoch !== ledger.epoch ||
          ledger.epoch !== Number(this.env.EXPECTED_EPOCH) ||
          ledger.epoch !== COPPERTOP_DEPLOYMENT_EPOCH
        )
          return jsonResponse({ failure: "EpochMismatch" }, 409);
        if (!ledger.runs[b.runId])
          return jsonResponse({ failure: "UnknownRun" }, 409);
        let createFailure: unknown;
        if (path === "/native-ensure") {
          try {
            await this.env.RUNS.create({
              id: instanceId(b.runId, b.epoch),
              params: { ...b, definitionVersion: 1, schemaVersion: 1 },
            });
          } catch (cause) {
            createFailure = cause;
          }
        }
        const instance = await this.env.RUNS.get(instanceId(b.runId, b.epoch));
        const status = await instance.status();
        if (createFailure && status.status === "unknown") throw createFailure;
        if (path === "/native-ensure" && status.status !== "unknown")
          this.rememberInstance(b.runId, instance.id);
        if (path === "/native-wait-start") {
          const subscription = await instance.subscribe({
            cursor: 0,
            filter: ["wait_started"],
          });
          let timer: ReturnType<typeof setTimeout> | undefined;
          try {
            const event = await Promise.race([
              subscription.next(),
              new Promise<never>((_, reject) => {
                timer = setTimeout(
                  () =>
                    reject(new Error("native WAIT_START diagnostic timed out")),
                  1500,
                );
              }),
            ]);
            if (event.done || event.value.type !== "wait_started")
              throw new Error("native WAIT_START absent");
            return jsonResponse({
              event: event.value,
              status: await instance.status(),
            });
          } finally {
            if (timer) clearTimeout(timer);
            subscription[Symbol.dispose]();
          }
        }
        return jsonResponse(status);
      }
      if (path === "/diagnostic") return jsonResponse(await this.diagnostic());
      // Test-only corruption simulates an incompatible persisted deployment; no migration permission inferred.
      if (path === "/corrupt") {
        const body = (await request.json()) as {
          schemaVersion: number;
          epoch: number;
        };
        const row = this.ctx.storage.sql
          .exec<{ value: string }>("SELECT value FROM ledger WHERE id=1")
          .toArray()[0];
        const ledger = row ? JSON.parse(row.value) : initialLedger(1);
        this.ctx.storage.sql.exec(
          "INSERT OR REPLACE INTO ledger(id,value) VALUES(1,?)",
          JSON.stringify({ ...ledger, ...body }),
        );
        return jsonResponse({ injected: true });
      }
      const body: unknown = await request.json();
      let result: Decision;
      if (path === "/submit") {
        const b = Codec.decode(Codec.Submit, body);
        result = await this.mutate(
          {
            type: "submit",
            spec: b.spec,
            digest: await hash(canonicalSpec(b.spec)),
          },
          b.epoch,
          b.controls,
        );
      } else if (path === "/cancel") {
        const b = Codec.decode(Codec.Cancel, body);
        result = await this.mutate(
          { type: "cancel", runId: b.runId, commandId: b.commandId },
          b.epoch,
        );
      } else if (path === "/launch") {
        const b = Codec.decode(Codec.RunId, body);
        result = await this.launch(b.runId, b.epoch);
      } else if (path === "/claim") {
        const b = Codec.decode(Codec.Claim, body);
        result = await this.mutate(
          {
            type: "execute",
            runId: b.runId,
            callId: b.callId,
            bodyId: b.bodyId,
          },
          b.epoch,
        );
      } else if (path === "/artifact") {
        const b = Codec.decode(Codec.Artifact, body);
        const response = await post(this.env.PROVIDER, "/artifact/read", {
          key: b.receipt.key,
        });
        if (!response.ok)
          return jsonResponse({ failure: "InvalidManifest" }, 409);
        const artifact = Codec.decode(
          Codec.FakeArtifact,
          await response.json(),
        );
        const content = Codec.decode(
          Codec.ArtifactContent,
          JSON.parse(artifact.content),
        );
        if (
          artifact.digest !== b.receipt.digest ||
          (await hash(artifact.content)) !== b.receipt.digest ||
          artifact.bytes !==
            new TextEncoder().encode(artifact.content).byteLength ||
          artifact.bytes !== b.receipt.bytes ||
          artifact.key !== b.receipt.key ||
          content.runId !== b.runId ||
          content.specDigest !== b.receipt.specDigest ||
          content.bodyId !== b.receipt.bodyId ||
          content.decisionGate !== b.receipt.decisionGate ||
          content.validity !== b.receipt.validity
        )
          return jsonResponse({ failure: "InvalidManifest" }, 409);
        result = await this.mutate(
          {
            type: "artifact",
            runId: b.runId,
            revision: b.revision,
            receipt: b.receipt,
          },
          b.epoch,
        );
      } else if (path === "/reconcile") {
        const b = Codec.decode(Codec.RunId, body);
        await this.reconcile(b.runId, b.epoch);
        result = { ledger: this.read(), run: this.read().runs[b.runId] };
      } else if (path === "/run") {
        const b = Codec.decode(Codec.RunId, body);
        const ledger = this.read();
        if (
          ledger.schemaVersion !== 1 ||
          ledger.epoch !== b.epoch ||
          ledger.epoch !== Number(this.env.EXPECTED_EPOCH) ||
          ledger.epoch !== COPPERTOP_DEPLOYMENT_EPOCH
        )
          return jsonResponse({ failure: "EpochMismatch" }, 409);
        return jsonResponse({
          run: ledger.runs[b.runId],
          controls: this.controls(b.runId),
        });
      } else return jsonResponse({ error: "unknown synthetic endpoint" }, 404);
      return jsonResponse(result, result.failure ? 409 : 200);
    } catch (cause) {
      if (cause instanceof StorageRejected)
        return jsonResponse({ failure: cause.failure }, 409);
      return jsonResponse(
        { error: "SchemaOrStorageRejected", detail: String(cause) },
        400,
      );
    }
  }
}

export const NativeRun = Workflow.make(Layer.empty, {
  run: (raw: unknown) =>
    Effect.gen(function* () {
      const payload = Codec.decode(Codec.WorkflowPayload, raw);
      if (payload.epoch !== COPPERTOP_DEPLOYMENT_EPOCH)
        return yield* Effect.fail(
          new Workflow.WorkflowStepNonRetryableError({
            cause: "incompatible workflow deployment epoch",
          }),
        );
      const env = yield* WorkerEnvironment;
      const coordinator = env.COORDINATOR.get(
        env.COORDINATOR.idFromName("synthetic-grant"),
      );
      const request = { runId: payload.runId, epoch: payload.epoch };
      const read = async () => {
        const response = await post(coordinator, "/run", request);
        if (!response.ok) throw new Error(`ledger fence ${response.status}`);
        const parsed = Codec.decode(Codec.RunResponse, await response.json());
        return {
          run: structuredClone(parsed.run) as unknown as Run,
          controls: parsed.controls,
        };
      };
      const initial = yield* Effect.tryPromise(() => read());
      yield* Workflow.step(
        "first-activity",
        Effect.tryPromise(async () => {
          await post(env.PROVIDER, "/record", {
            id: payload.runId,
            event: "first-start",
          });
          // Test barrier is inside an incomplete first activity: no prior workflow step/timer checkpoint.
          if (initial.controls.firstDelayMs)
            await new Promise<void>((resolve) =>
              setTimeout(resolve, initial.controls.firstDelayMs),
            );
          await post(env.PROVIDER, "/record", {
            id: payload.runId,
            event: "first",
          });
          return "observed";
        }),
        {
          retries: { limit: 2, delay: "1 second", backoff: "constant" },
          timeout: "15 seconds",
        },
      );
      yield* Workflow.step(
        "one-shot-dispatch",
        Effect.tryPromise(async () => {
          const r = await post(coordinator, "/launch", request);
          if (!r.ok) throw new Error(`dispatch ${r.status}`);
          return await r.json();
        }),
      );
      const call = yield* Workflow.step(
        "reconcile-launch",
        Effect.tryPromise(async () => {
          await post(coordinator, "/reconcile", request);
          return (await read()).run.callIds[0] ?? null;
        }),
      );
      if (call !== null) {
        yield* Workflow.step(
          "one-shot-body",
          Effect.tryPromise(async () => {
            const r = await post(coordinator, "/claim", {
              ...request,
              callId: call,
              bodyId: `body-${(await hash(instanceId(payload.runId, payload.epoch))).slice(0, 48)}`,
            });
            const decision = Codec.decode(Codec.Decision, await r.json());
            if (!r.ok) throw new Error(`body claim ${r.status}`);
            if (decision.newPermission) {
              await post(env.PROVIDER, "/record", {
                id: payload.runId,
                event: "body",
              });
              if (!initial.controls.finishExternally) {
                await post(env.PROVIDER, "/finish", { callId: call });
                await post(coordinator, "/reconcile", request);
              }
            }
            return decision.newPermission ?? false;
          }),
        );
        // A child/provider notification may be lost; the durable ledger alarm repairs its outbox.
        let terminal = false;
        for (let attempt = 0; attempt < 3; attempt++) {
          yield* Workflow.waitForEvent(`provider-terminal-${attempt}`, {
            type: "provider-done",
            timeout: "2 seconds",
          }).pipe(Effect.catch(() => Effect.succeed(null)));
          terminal = yield* Workflow.step(
            `bounded-status-reconcile-${attempt}`,
            Effect.tryPromise(async () => {
              const response = await post(coordinator, "/reconcile", request);
              if (!response.ok) throw new Error(`status ${response.status}`);
              const { run } = await read();
              return (
                run.cancelRequested ||
                Object.keys(run.events).some((id) => id === `done-${call}`)
              );
            }),
            {
              retries: { limit: 2, delay: "1 second", backoff: "constant" },
              timeout: "5 seconds",
            },
          );
          if (terminal) break;
        }
        if (!terminal) return { runId: payload.runId, recoveryRequired: true };
        yield* Workflow.step(
          "immutable-receipt",
          Effect.tryPromise(async () => {
            const { run } = await read();
            if (run.cancelRequested || run.phase === "closed")
              return "cancelled";
            const content = JSON.stringify({
              schemaVersion: 1,
              runId: payload.runId,
              specDigest: run.digest,
              bodyId: run.bodyClaimant!,
              decisionGate: "passed",
              validity: "valid",
              result: "synthetic-valid",
            });
            const digest = await hash(content);
            const receipt: ArtifactReceipt = {
              schemaVersion: 1,
              runId: payload.runId,
              specDigest: run.digest,
              bodyId: run.bodyClaimant!,
              key: `runs/${payload.runId}/${digest}.json`,
              digest,
              bytes: new TextEncoder().encode(content).byteLength,
              validity: "valid",
              decisionGate: "passed",
            };
            const persisted = await post(env.PROVIDER, "/artifact/put", {
              key: receipt.key,
              digest,
              content,
            });
            if (!persisted.ok)
              throw new Error(`artifact write ${persisted.status}`);
            const r = await post(coordinator, "/artifact", {
              ...request,
              revision: run.revision,
              receipt,
            });
            if (!r.ok) throw new Error(`receipt ${r.status}`);
            await post(coordinator, "/reconcile", request);
            await post(env.PROVIDER, "/record", {
              id: payload.runId,
              event: "parent-complete",
            });
            return receipt;
          }),
        );
      }
      return { runId: payload.runId };
    }),
});
export default {
  async fetch(request: Request, env: Env): Promise<Response> {
    if (env.SPIKE_LOCAL_ONLY !== "true")
      return jsonResponse({ error: "local synthetic spike disabled" }, 403);
    return env.COORDINATOR.get(
      env.COORDINATOR.idFromName("synthetic-grant"),
    ).fetch(request);
  },
};
