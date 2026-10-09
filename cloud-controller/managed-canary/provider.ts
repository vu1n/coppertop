import {
  DurableObject,
  WorkerEntrypoint,
  WorkflowEntrypoint,
  type WorkflowEvent,
  type WorkflowStep,
} from "cloudflare:workers";
import * as Effect from "effect/Effect";
import * as ManagedRuntime from "effect/ManagedRuntime";
import * as SqliteClient from "@effect/sql-sqlite-do/SqliteClient";
import * as SqlClient from "effect/sql/SqlClient";
import { initialLedger, transition } from "../shared/domain.ts";
import {
  jsonResponse,
  isRecord,
  type Command,
  type Decision,
  type Ledger,
  type ProviderCall,
  type ArtifactReceipt,
} from "../shared/contracts.ts";
import * as Codec from "../lane-a/schema.ts";
import {
  MODES,
  canonical,
  envelope,
  validate,
  mode,
  runId,
  initial,
  spec,
  hash,
  post,
  type CanaryEnv,
  type Envelope,
  type Mode,
  type Observation,
} from "./contracts.ts";

type Snapshot = {
  calls: ProviderCall[];
  observations: Observation[];
  fenced: string[];
  artifacts: ArtifactReceipt[];
  closed: boolean;
};
const namespaceNames = ["ledger", "provider", "observer"] as const;
const object = (env: CanaryEnv, name: (typeof namespaceNames)[number]) =>
  env.STATE.get(env.STATE.idFromName(name));
const generation = (env: CanaryEnv, probe: Mode) =>
  `${runId(env.CANARY_SUITE, probe)}-e1`;

/** Three fixed, separately persisted actors in one SQLite namespace; no production gateway. */
export class CanaryStore extends DurableObject<CanaryEnv> {
  private runtime: ManagedRuntime.ManagedRuntime<
    SqlClient.SqlClient | SqliteClient.SqliteClient,
    never
  >;
  constructor(ctx: DurableObjectState, env: CanaryEnv) {
    super(ctx, env);
    this.runtime = ManagedRuntime.make(
      SqliteClient.layer({ storage: ctx.storage }),
    );
    ctx.storage.sql.exec(
      "CREATE TABLE IF NOT EXISTS records (kind TEXT, id TEXT, value TEXT NOT NULL, PRIMARY KEY(kind,id))",
    );
  }
  private read<T>(kind: string, id = "state"): T | undefined {
    const row = this.ctx.storage.sql
      .exec<{
        value: string;
      }>("SELECT value FROM records WHERE kind=? AND id=?", kind, id)
      .toArray()[0];
    return row ? (JSON.parse(row.value) as T) : undefined;
  }
  private put(kind: string, id: string, value: unknown): void {
    const encoded = JSON.stringify(value);
    if (new TextEncoder().encode(encoded).length > 64 * 1024)
      throw new Error("Synthetic row size bound");
    this.ctx.storage.sql.exec(
      "INSERT OR REPLACE INTO records VALUES(?,?,?)",
      kind,
      id,
      encoded,
    );
  }
  private rows<T>(kind: string): T[] {
    return this.ctx.storage.sql
      .exec<{ value: string }>(
        "SELECT value FROM records WHERE kind=? ORDER BY id",
        kind,
      )
      .toArray()
      .map((row) => JSON.parse(row.value) as T);
  }
  private ledger(): Ledger {
    const raw = this.read<unknown>("ledger");
    const ledger = raw
      ? (Codec.decode(Codec.StoredLedger, raw) as Ledger)
      : initial(this.env, initialLedger(1));
    if (
      ledger.epoch !== 1 ||
      ledger.grant.expires !== Number(this.env.CANARY_DEADLINE) ||
      Object.values(ledger.runs).some(
        (run) => run.spec.sourceDigest !== this.env.CANARY_BUILD,
      )
    )
      throw new Error("Persisted deployment fence rejected");
    if (this.read("closed")) ledger.grant.revoked = true;
    return ledger;
  }
  private async mutate(command: Command): Promise<Decision> {
    const self = this;
    return this.runtime.runPromise(
      Effect.gen(function* () {
        const sql = yield* SqlClient.SqlClient;
        return yield* sql.withTransaction(
          Effect.gen(function* () {
            const current = self.ledger();
            const result = transition(current, {
              epoch: 1,
              now: Date.now(),
              command,
            });
            if (result.failure) throw new Error(result.failure);
            yield* sql`INSERT OR REPLACE INTO records VALUES('ledger','state',${JSON.stringify(result.ledger)})`;
            if (
              Date.now() < Number(self.env.CANARY_DEADLINE) &&
              Object.values(result.ledger.runs).some(
                (run) => run.cleanup !== "confirmed",
              )
            )
              yield* Effect.promise(() =>
                self.ctx.storage.setAlarm(Number(self.env.CANARY_DEADLINE)),
              );
            else yield* Effect.promise(() => self.ctx.storage.deleteAlarm());
            return result;
          }),
        );
      }),
    );
  }
  private async reconcile(probe: Mode): Promise<Decision> {
    const payload = envelope(this.env),
      id = runId(payload.suiteId, probe);
    let snapshot = await post<Snapshot>(
      object(this.env, "provider"),
      "/snapshot",
      payload,
      { role: "provider" },
    );
    for (const call of snapshot.calls.filter((call) => call.runId === id))
      await this.mutate({ type: "register", runId: id, callId: call.id });
    let run = this.ledger().runs[id];
    if (!run) throw new Error("UnknownRun");
    if (run.cancelRequested || run.receipt || Date.now() >= payload.deadline) {
      if (Date.now() >= payload.deadline)
        await this.mutate({ type: "timeout", runId: id });
      await post(object(this.env, "provider"), "/fence", payload, {
        role: "provider",
        mode: probe,
      });
      // Fence first, then enumerate again. Unknown/lost launches remain owned and charged.
      snapshot = await post<Snapshot>(
        object(this.env, "provider"),
        "/snapshot",
        payload,
        { role: "provider" },
      );
      const calls = snapshot.calls.filter((call) => call.runId === id);
      for (const call of calls)
        await this.mutate({ type: "register", runId: id, callId: call.id });
      await this.mutate({
        type: "cleanup",
        runId: id,
        stoppedCallIds: calls
          .filter((call) => ["completed", "cancelled"].includes(call.state))
          .map((call) => call.id),
        generationDisabled: snapshot.fenced.includes(
          generation(this.env, probe),
        ),
      });
      run = this.ledger().runs[id]!;
      if (run.cleanup === "confirmed" && run.outcome !== "pending")
        await this.mutate({
          type: "settle",
          runId: id,
          cost: calls.reduce((sum, call) => sum + call.cost, 0),
        });
    }
    return { ledger: this.ledger(), run: this.ledger().runs[id] };
  }
  async alarm(): Promise<void> {
    // Deadline containment lives independently of the target Workflow scheduler.
    this.put("closed", "state", true);
    const meta = this.read<{ role: string }>("meta");
    if (meta?.role === "provider") {
      for (const probe of MODES)
        this.put(
          "fence",
          generation(this.env, probe),
          generation(this.env, probe),
        );
      for (const call of this.rows<ProviderCall>("call"))
        if (!["completed", "cancelled"].includes(call.state))
          this.put("call", call.id, { ...call, state: "cancelled" });
    } else if (meta?.role === "ledger") {
      // Persisted alarm retry is provided by Cloudflare. A failed cleanup never releases a hold.
      for (const probe of MODES)
        if (this.ledger().runs[runId(this.env.CANARY_SUITE, probe)])
          await this.reconcile(probe);
    }
  }
  async fetch(request: Request): Promise<Response> {
    try {
      if (request.method !== "POST")
        return jsonResponse({ error: "POST required" }, 405);
      const text = await request.text();
      if (
        new TextEncoder().encode(text).length >
        (new URL(request.url).pathname === "/report" ? 32 * 1024 : 4096)
      )
        return jsonResponse({ error: "envelope size cap" }, 413);
      const body: unknown = JSON.parse(text);
      if (
        !isRecord(body) ||
        !namespaceNames.includes(body.role as (typeof namespaceNames)[number])
      )
        throw new Error("Invalid private envelope");
      const path = new URL(request.url).pathname;
      const payload = validate(
        body,
        this.env,
        [
          "/snapshot",
          "/reconcile",
          "/cancel",
          "/fence",
          "/cleanup",
          "/report",
        ].includes(path),
      );
      const meta = this.read<{
        role: string;
        buildDigest: string;
        suiteId: string;
      }>("meta");
      if (
        meta &&
        (meta.role !== body.role ||
          meta.buildDigest !== payload.buildDigest ||
          meta.suiteId !== payload.suiteId)
      )
        throw new Error("Persisted actor identity fence");
      if (!meta) {
        this.put("meta", "state", {
          role: body.role,
          buildDigest: payload.buildDigest,
          suiteId: payload.suiteId,
        });
        await this.ctx.storage.setAlarm(payload.deadline);
      }
      if (body.role === "observer") {
        if (path === "/report" && body.report !== undefined) {
          this.put("report", "state", body.report);
          return jsonResponse({ recorded: true });
        }
        if (path === "/snapshot")
          return jsonResponse(this.read("report") ?? null);
        throw new Error("Unsupported observer operation");
      }
      if (body.role === "provider") {
        if (path === "/snapshot")
          return jsonResponse({
            calls: this.rows("call"),
            observations: this.rows("observation"),
            fenced: this.rows("fence"),
            artifacts: this.rows("artifact"),
            closed: this.read("closed") ?? false,
          });
        if (path === "/cleanup") {
          await this.alarm();
          return jsonResponse({ contained: true });
        }
        const probe = mode(body.mode),
          id = runId(payload.suiteId, probe);
        if (path === "/fence") {
          this.put(
            "fence",
            generation(this.env, probe),
            generation(this.env, probe),
          );
          for (const call of this.rows<ProviderCall>("call").filter(
            (call) =>
              call.runId === id &&
              !["completed", "cancelled"].includes(call.state),
          ))
            this.put("call", call.id, { ...call, state: "cancelled" });
          return jsonResponse({ fenced: true });
        }
        if (path === "/artifact-read")
          return jsonResponse(this.read("bytes", id));
        if (this.read("closed")) throw new Error("Provider retired");
        if (path === "/record") {
          if (
            typeof body.event !== "string" ||
            !/^[a-z-]{1,40}$/.test(body.event)
          )
            throw new Error("Unsupported observation");
          const key = `${probe}:${body.event}`,
            old = this.read<Observation>("observation", key);
          if (!old && this.rows("observation").length >= 32)
            throw new Error("Observation count cap");
          const observation = {
            mode: probe,
            event: body.event,
            count: (old?.count ?? 0) + 1,
            at: Date.now(),
          };
          if (observation.count > 30) throw new Error("Observation retry cap");
          this.put("observation", key, observation);
          return jsonResponse(observation);
        }
        if (path === "/spawn") {
          if (this.read("fence", generation(this.env, probe)))
            throw new Error("Provider generation fenced");
          if (this.rows("call").length >= 6)
            throw new Error("Synthetic provider call cap");
          const call: ProviderCall = {
            id: `call-${crypto.randomUUID()}`,
            runId: id,
            generation: generation(this.env, probe),
            state: "queued",
            cost: 1,
          };
          this.put("call", call.id, call);
          return probe === "control"
            ? jsonResponse(
                { error: "synthetic reply lost after durable launch" },
                503,
              )
            : jsonResponse(call);
        }
        const call = this.rows<ProviderCall>("call").find(
          (call) => call.runId === id,
        );
        if (!call) throw new Error("Unknown synthetic call");
        if (path === "/finish") {
          if (
            this.read("fence", generation(this.env, probe)) ||
            call.state === "cancelled"
          )
            throw new Error("Late completion fenced");
          this.put("call", call.id, {
            ...call,
            state: "completed",
            bodyId: `body-${probe}`,
          });
          return jsonResponse({ finished: true });
        }
        if (path === "/artifact") {
          if (call.state !== "completed" || call.bodyId !== `body-${probe}`)
            throw new Error("Unverified synthetic body completion");
          const content = JSON.stringify({
            schemaVersion: 1,
            runId: id,
            specDigest: await hash(JSON.stringify(spec(payload, probe))),
            bodyId: `body-${probe}`,
            decisionGate: "passed",
            validity: "valid",
            result: "synthetic-valid",
          });
          const digest = await hash(content);
          const receipt: ArtifactReceipt = {
            schemaVersion: 1,
            runId: id,
            specDigest: await hash(JSON.stringify(spec(payload, probe))),
            bodyId: `body-${probe}`,
            key: `runs/${id}/${digest}.json`,
            digest,
            bytes: new TextEncoder().encode(content).length,
            decisionGate: "passed",
            validity: "valid",
          };
          // Hashing yields; recheck containment and immutable content before CAS.
          if (this.read("fence", generation(this.env, probe)))
            throw new Error("Artifact generation fenced");
          const old = this.read<{ content: string; receipt: ArtifactReceipt }>(
            "bytes",
            id,
          );
          if (old && old.content !== content)
            throw new Error("Immutable synthetic artifact conflict");
          this.put("bytes", id, { content, receipt });
          this.put("artifact", id, receipt);
          return jsonResponse(receipt);
        }
        throw new Error("Unsupported provider operation");
      }
      if (path === "/snapshot") return jsonResponse(this.ledger());
      if (path === "/cleanup") {
        this.put("closed", "state", true);
        for (const probe of MODES)
          if (this.ledger().runs[runId(payload.suiteId, probe)]) {
            await this.mutate({
              type: "cancel",
              runId: runId(payload.suiteId, probe),
              commandId: `cleanup-${probe}`,
            });
            await this.reconcile(probe);
          }
        return jsonResponse(this.ledger());
      }
      const probe = mode(body.mode),
        id = runId(payload.suiteId, probe);
      if (path === "/admit")
        return jsonResponse(
          await this.mutate({
            type: "submit",
            spec: spec(payload, probe),
            digest: await hash(JSON.stringify(spec(payload, probe))),
          }),
        );
      if (path === "/dispatch") {
        const claim = await this.mutate({ type: "dispatch", runId: id });
        if (claim.newPermission) {
          const response = await object(this.env, "provider").fetch(
            "https://synthetic.invalid/spawn",
            {
              method: "POST",
              body: JSON.stringify({
                ...payload,
                role: "provider",
                mode: probe,
              }),
            },
          );
          if (response.ok) {
            const call = Codec.decode(
              Codec.ProviderCall,
              await response.json(),
            );
            await this.mutate({ type: "register", runId: id, callId: call.id });
          } else await this.mutate({ type: "uncertain", runId: id });
        }
        return jsonResponse(await this.reconcile(probe));
      }
      if (path === "/execute") {
        const run = (await this.reconcile(probe)).run!;
        const callId = run.callIds[0];
        if (!callId)
          throw new Error(
            "Ambiguous launch unresolved; no second dispatch allowed",
          );
        return jsonResponse(
          await this.mutate({
            type: "execute",
            runId: id,
            callId,
            bodyId: `body-${probe}`,
          }),
        );
      }
      if (path === "/complete") {
        const prior = this.ledger().runs[id]?.receipt;
        const receipt =
          prior ??
          (await post<ArtifactReceipt>(
            object(this.env, "provider"),
            "/artifact",
            payload,
            { role: "provider", mode: probe },
          ));
        const physical = await post<{
          content: string;
          receipt: ArtifactReceipt;
        }>(object(this.env, "provider"), "/artifact-read", payload, {
          role: "provider",
          mode: probe,
        });
        const content = Codec.decode(
          Codec.ArtifactContent,
          JSON.parse(physical.content),
        );
        const run = this.ledger().runs[id]!;
        if (
          canonical(physical.receipt) !== canonical(receipt) ||
          (await hash(physical.content)) !== receipt.digest ||
          new TextEncoder().encode(physical.content).length !== receipt.bytes ||
          content.runId !== id ||
          content.specDigest !== run.digest ||
          content.bodyId !== run.bodyClaimant ||
          content.decisionGate !== "passed" ||
          content.validity !== "valid"
        )
          throw new Error("Physical immutable receipt verification failed");
        if (!run.receipt)
          await this.mutate({
            type: "artifact",
            runId: id,
            revision: run.revision,
            receipt,
          });
        else if (canonical(run.receipt) !== canonical(receipt))
          throw new Error("Receipt CAS conflict");
        return jsonResponse(await this.reconcile(probe));
      }
      if (path === "/cancel") {
        await this.mutate({
          type: "cancel",
          runId: id,
          commandId: `cancel-${probe}`,
        });
        return jsonResponse(await this.reconcile(probe));
      }
      if (path === "/reconcile")
        return jsonResponse(await this.reconcile(probe));
      throw new Error("Unsupported ledger operation");
    } catch (cause) {
      return jsonResponse(
        {
          error: "Synthetic envelope, storage, or permission rejected",
          reason: cause instanceof Error ? cause.message : "rejected",
        },
        409,
      );
    }
  }
}

/** Callable only through a named service binding. Default HTTP ingress always rejects. */
export class SyntheticProvider extends WorkerEntrypoint<CanaryEnv> {
  async fetch(request: Request): Promise<Response> {
    const path = new URL(request.url).pathname;
    const role = path.startsWith("/ledger/")
      ? "ledger"
      : path.startsWith("/observer/")
        ? "observer"
        : path.startsWith("/provider/")
          ? "provider"
          : undefined;
    if (!role || request.method !== "POST")
      return jsonResponse({ error: "private service operation required" }, 404);
    const text = await request.text();
    if (new TextEncoder().encode(text).length > 4096)
      return jsonResponse({ error: "size cap" }, 413);
    try {
      const body: unknown = JSON.parse(text);
      if (!isRecord(body))
        return jsonResponse({ error: "invalid envelope" }, 400);
      return object(this.env, role).fetch(
        `https://synthetic.invalid${path.slice(role.length + 1)}`,
        { method: "POST", body: JSON.stringify({ ...body, role }) },
      );
    } catch {
      return jsonResponse({ error: "invalid envelope" }, 400);
    }
  }
}

export class CanaryObserver extends WorkflowEntrypoint<CanaryEnv, Envelope> {
  async run(event: WorkflowEvent<Envelope>, step: WorkflowStep) {
    const payload = validate(event.payload, this.env);
    if (event.instanceId !== `${payload.suiteId}-observer`)
      throw new Error("Observer instance identity rejected");
    // This Workflow never has a binding to, or contacts, any target Workflow.
    const sample = async (): Promise<Snapshot> =>
      post(object(this.env, "provider"), "/snapshot", payload, {
        role: "provider",
      });
    let snapshot: Snapshot | undefined;
    for (let attempt = 0; attempt < 30; attempt++) {
      snapshot = await step.do(
        `independent-observation-${attempt}`,
        { retries: { limit: 1, delay: "1 second" }, timeout: "5 seconds" },
        sample,
      );
      const ended = MODES.every((probe) =>
        snapshot!.observations.some(
          (row) =>
            row.mode === probe &&
            row.event ===
              (probe === "cancel" ? "cancel-observed" : "probe-complete"),
        ),
      );
      if (ended) break;
      await step.sleep(`independent-interval-${attempt}`, "1 second");
    }
    const report = {
      version: 1,
      suiteId: payload.suiteId,
      buildDigest: payload.buildDigest,
      hostProcessLoss: "untested",
      observedAt: Date.now(),
      snapshot,
    };
    await step.do(
      "persist-independent-report",
      { retries: { limit: 1, delay: "1 second" }, timeout: "5 seconds" },
      async () =>
        post<{ recorded: boolean }>(
          object(this.env, "observer"),
          "/report",
          payload,
          { role: "observer", report },
        ),
    );
    return report;
  }
}
export default {
  fetch: () =>
    jsonResponse(
      { error: "No HTTP ingress; use authenticated Workflows API" },
      404,
    ),
};
