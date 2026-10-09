import { DurableObject } from "cloudflare:workers"
import * as CloudflareCluster from "@effect/platform-cloudflare/CloudflareCluster"
import * as CloudflareDurableObjects from "@effect/platform-cloudflare/CloudflareDurableObjects"
import * as CloudflareWorkflowEngine from "@effect/platform-cloudflare/CloudflareWorkflowEngine"
import { Cause, Effect, Exit, Layer, Option, Schema, Scope } from "effect"
import type { Context } from "effect"
import { Activity, DurableClock, DurableDeferred, Workflow, type WorkflowEngine } from "effect/workflow"
import { initialLedger, transition } from "@coppertop/domain"
import type { Decision, Ledger, Mutation } from "@coppertop/contracts"

declare const COPPERTOP_BUILD_VERSION: string
interface Env {
  CLUSTER_WORKFLOW: DurableObjectNamespace
  LEDGER: DurableObjectNamespace
  OBSERVER: Fetcher
  DROP_PARENT_WAKE: string
}
let env: Env
const post = (path: string, value: unknown) => Effect.promise(async () => {
  const response = await env.OBSERVER.fetch(`http://provider${path}`, {
    method: "POST", headers: { "content-type": "application/json" }, body: JSON.stringify(value)
  })
  return { status: response.status, body: await response.json() as unknown }
})
const record = (id: string, event: string) => post("/record", { id, event }).pipe(Effect.asVoid)
const payload = { id: Schema.String, version: Schema.String, deadline: Schema.Number }
const define = (name: string) => Workflow.make(name, {
  payload, success: Schema.String, error: Schema.String, idempotencyKey: ({ id }) => id
})
const Initial = define("Initial")
const Completed = define("Completed")
const Clock = define("Clock")
const Suspended = define("Suspended")
const Defect = define("Defect")
const Child = define("Child")
const Parent = define("Parent")
const Race = define("Race")
const Guarded = define("Guarded")
const Launch = define("Launch")
const Door = DurableDeferred.make("Door", { success: Schema.String })
const Completion = DurableDeferred.make("Completion", { success: Schema.String })
const activity = <R>(name: string, execute: Effect.Effect<string, string, R>) =>
  Activity.make({ name, success: Schema.String, error: Schema.String, execute })

const mutate = (command: Mutation["command"]) => Effect.promise(async () => {
  const response = await env.LEDGER.getByName("claims").fetch("http://ledger/mutate", {
    method: "POST", body: JSON.stringify({ now: Date.now(), epoch: 1, command })
  })
  return await response.json() as Decision
})

const layers = Layer.mergeAll(
  Initial.toLayer(({ id }) => activity("first", record(id, "entered").pipe(Effect.andThen(Effect.never)))),
  Completed.toLayer(({ id }) => activity("completed", record(id, "activity").pipe(Effect.as(COPPERTOP_BUILD_VERSION)))),
  Clock.toLayer(({ id }) => Effect.gen(function*() {
    yield* DurableClock.sleep({ name: "wake", duration: "800 millis" })
    yield* record(id, "done")
    return "clock-done"
  })),
  Suspended.toLayer(({ id }) => Effect.gen(function*() {
    yield* record(id, "entered")
    const value = yield* DurableDeferred.await(Door)
    yield* record(id, "done")
    return value
  })),
  Defect.toLayer(({ id }) => activity("defect", record(id, "entered").pipe(Effect.andThen(Effect.die("deterministic defect"))))),
  Child.toLayer(({ id }) => Effect.gen(function*() {
    const value = yield* DurableDeferred.await(Door)
    yield* record(id, "child-done")
    return value
  })),
  Parent.toLayer((data) => Effect.gen(function*() {
    yield* record(data.id, "parent-entered")
    const value = yield* Child.execute(data)
    yield* record(data.id, "parent-done")
    return value
  })),
  Race.toLayer(({ id, deadline }) => Effect.gen(function*() {
    yield* activity("announce", record(id, "activity").pipe(Effect.as("announced")))
    const winner = yield* DurableDeferred.raceAll({
      name: "absolute-deadline", success: Schema.String, error: Schema.Never,
      effects: [
        DurableDeferred.await(Completion),
        DurableClock.sleep({ name: "deadline", duration: Math.max(0, deadline - Date.now()) }).pipe(Effect.as("deadline"))
      ]
    })
    yield* record(id, `winner-${winner}`)
    return winner
  })),
  Guarded.toLayer(({ id, version }) => Effect.gen(function*() {
    if (version !== COPPERTOP_BUILD_VERSION) return yield* Effect.fail("definition-version-quarantined")
    yield* activity("guarded-first", record(id, "activity").pipe(Effect.as(COPPERTOP_BUILD_VERSION)))
    return yield* DurableDeferred.await(Door)
  })),
  Launch.toLayer(({ id }) => activity("launch", Effect.gen(function*() {
    const claim = yield* mutate({ type: "dispatch", runId: id })
    if (!claim.newPermission) return "dispatch-not-permitted"
    yield* post("/spawn", { runId: id, generation: id, loseReply: true })
    yield* mutate({ type: "uncertain", runId: id })
    yield* record(id, "ambiguous")
    // Side effect occurred; reply was lost; checkpoint intentionally never committed.
    return yield* Effect.never
  })))
)
let app: Promise<Context.Context<WorkflowEngine.WorkflowEngine>> | undefined
const initialize = CloudflareDurableObjects.setInitializer(async (environment) => {
  env = environment as Env
  app ??= Effect.runPromise(Effect.flatMap(Scope.make(), (scope) =>
    Layer.buildWithScope(layers.pipe(Layer.provideMerge(CloudflareWorkflowEngine.layer({
      workflowNamespace: env.CLUSTER_WORKFLOW
    }))), scope)))
  return app
})

/** Test-only seam: raw SQL diagnostics and deterministic transport failure. */
export class ClusterWorkflow extends CloudflareDurableObjects.ClusterWorkflow {
  private readonly state: DurableObjectState
  private readonly environment: Env
  constructor(ctx: DurableObjectState, environment: Env) {
    super(ctx, environment)
    this.state = ctx
    this.environment = environment
  }
  async snapshot() {
    const sql = this.state.storage.sql
    return {
      execution: sql.exec("SELECT * FROM workflow_execution").toArray(),
      activities: sql.exec("SELECT * FROM workflow_activities").toArray(),
      deferreds: sql.exec("SELECT * FROM workflow_deferreds").toArray(),
      clocks: sql.exec("SELECT * FROM workflow_clocks").toArray(),
      alarm: await this.state.storage.getAlarm()
    }
  }
  override async resume(): Promise<void> {
    if (this.environment.DROP_PARENT_WAKE === "true" && this.state.id.name?.includes("Parent")) {
      await this.environment.OBSERVER.fetch("http://provider/record", {
        method: "POST", body: JSON.stringify({ id: "wake", event: "dropped" })
      })
      throw new Error("synthetic child-to-parent wake lost")
    }
    return super.resume()
  }
}

/** Test-only safety ledger for trusted mutations. One synchronous SQL decision; no scheduler or production API. */
export class Claims extends DurableObject {
  override async fetch(request: Request): Promise<Response> {
    const snapshot = new URL(request.url).pathname === "/snapshot"
    // Decode before reading state. An await between read and write would let
    // concurrent fetches decide against the same stale admission/claim state.
    const mutation = snapshot ? undefined : await request.json() as Mutation
    this.ctx.storage.sql.exec("CREATE TABLE IF NOT EXISTS ledger(id INTEGER PRIMARY KEY CHECK(id=0), value TEXT NOT NULL)")
    const row = this.ctx.storage.sql.exec<{ value: string }>("SELECT value FROM ledger WHERE id=0").toArray()[0]
    const ledger: Ledger = row ? JSON.parse(row.value) : initialLedger(1)
    if (snapshot) return Response.json(ledger)
    const decision = transition(ledger, mutation!)
    this.ctx.storage.sql.exec("INSERT OR REPLACE INTO ledger(id,value) VALUES(0,?)", JSON.stringify(decision.ledger))
    return Response.json(decision)
  }
}
const workflows = { Initial, Completed, Clock, Suspended, Defect, Child, Parent, Race, Guarded, Launch }
const serialize = (exit: Exit.Exit<unknown, unknown>) => Exit.isSuccess(exit)
  ? { _tag: "Success", value: exit.value } : { _tag: "Failure", cause: Cause.pretty(exit.cause) }
export default {
  async fetch(request: Request, environment: Env): Promise<Response> {
    const context = await initialize(environment)
    const url = new URL(request.url)
    const id = url.searchParams.get("id") ?? "default"
    const name = url.searchParams.get("name") as keyof typeof workflows
    const workflow = workflows[name]
    const data = { id, version: url.searchParams.get("version") ?? "v1", deadline: Number(url.searchParams.get("deadline") ?? Date.now() + 60_000) }
    const program = Effect.gen(function*() {
      if (url.pathname === "/ledger") return yield* Effect.promise(async () => {
        const response = await environment.LEDGER.getByName("claims").fetch(`http://ledger/${request.method === "POST" ? "mutate" : "snapshot"}`, request.method === "POST" ? { method: "POST", body: await request.text() } : {})
        return await response.json()
      })
      const executionId = yield* workflow.executionId(data)
      switch (url.pathname) {
        case "/execute":
          if (url.searchParams.get("discard") === "true") {
            yield* workflow.execute(data, { discard: true })
            return { executionId }
          }
          return { executionId, result: yield* workflow.execute(data) }
        case "/poll": {
          const result = yield* workflow.poll(executionId)
          return Option.match(result, {
            onNone: () => ({ _tag: "None" }),
            onSome: (value) => value._tag === "Complete" ? { _tag: "Complete", exit: serialize(value.exit) } : { _tag: value._tag }
          })
        }
        case "/resume":
          yield* workflow.resume(executionId)
          return { resumed: true }
        case "/interrupt":
          yield* workflow.interrupt(executionId)
          return { interrupted: true }
        case "/complete": {
          const deferred = name === "Race" ? Completion : Door
          const token = DurableDeferred.tokenFromExecutionId(deferred, { workflow, executionId })
          yield* DurableDeferred.succeed(deferred, { token, value: url.searchParams.get("value") ?? "completed" })
          return { completed: true }
        }
        case "/snapshot":
          return yield* Effect.promise(() => (environment.CLUSTER_WORKFLOW.getByName(CloudflareCluster.encodeName(workflow._tag, executionId)) as unknown as ClusterWorkflow).snapshot())
        default: return { error: "unknown path" }
      }
    })
    try { return Response.json(await Effect.runPromise(Effect.provideContext(program, context))) }
    catch (error) { return new Response(String(error), { status: 599 }) }
  }
}
