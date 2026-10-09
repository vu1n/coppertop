import assert from "node:assert/strict"
import test from "node:test"
import * as fs from "node:fs/promises"
import * as path from "node:path"
import * as os from "node:os"
import * as zlib from "node:zlib"
import { createHash } from "node:crypto"
import * as esbuild from "esbuild"
import { Miniflare } from "miniflare"

const shared = process.env.COPPERTOP_SHARED
assert.ok(shared, "COPPERTOP_SHARED must point at the consumer-local shared sources")
const bundleFor = version => esbuild.build({
  entryPoints: [path.join(import.meta.dirname, "worker.ts")], bundle: true, format: "esm", write: false,
  external: ["cloudflare:workers"], define: { COPPERTOP_BUILD_VERSION: JSON.stringify(version) }, alias: {
    "@effect/platform-cloudflare": path.resolve(import.meta.dirname, "../../../src"),
    "@coppertop/domain": path.join(shared, "domain.ts"),
    "@coppertop/contracts": path.join(shared, "contracts.ts")
  }
}).then(bundle => bundle.outputFiles[0].text)
const scripts = { v1: await bundleFor("v1"), v2: await bundleFor("v2") }
assert.notEqual(scripts.v1, scripts.v2, "Upgrade fixture must change actual Worker module bytes")
const providerScript = (await esbuild.build({
  entryPoints: [path.join(shared, "provider-worker.ts")], bundle: true, format: "esm", write: false,
  external: ["cloudflare:workers"]
})).outputFiles[0].text
console.log(JSON.stringify({ workerBytes: Buffer.byteLength(scripts.v1), workerGzipBytes: zlib.gzipSync(scripts.v1).byteLength,
  upgradeChangesModuleBytes: scripts.v1 !== scripts.v2 }))
const pause = (ms) => new Promise(resolve => setTimeout(resolve, ms))
const make = async () => {
  const root = await fs.mkdtemp(path.join(os.tmpdir(), "coppertop-effect-do-"))
  const provider = new Miniflare({
    modules: [{ type: "ESModule", path: "provider.mjs", contents: providerScript }],
    compatibilityDate: "2026-08-01", durableObjectsPersist: path.join(root, "provider"),
    durableObjects: { PROVIDER: { className: "FakeProvider", useSQLite: true } }
  })
  await provider.ready
  let version = "v1", dropWake = false
  const options = () => ({
    modules: [{ type: "ESModule", path: "workflow.mjs", contents: scripts[version] }], compatibilityDate: "2026-08-01",
    bindings: { DROP_PARENT_WAKE: String(dropWake) },
    durableObjectsPersist: path.join(root, "workflow"),
    durableObjects: {
      CLUSTER_WORKFLOW: { className: "ClusterWorkflow", useSQLite: true },
      LEDGER: { className: "Claims", useSQLite: true }
    },
    serviceBindings: { OBSERVER: request => provider.dispatchFetch(request) }
  })
  const target = new Miniflare(options())
  await target.ready
  const fetch = async (endpoint, init) => {
    const response = await target.dispatchFetch(`http://target${endpoint}`, init)
    const body = await response.text()
    assert.equal(response.status, 200, `${endpoint}: ${body}`)
    return JSON.parse(body)
  }
  const observe = async () => (await provider.dispatchFetch("http://provider/observe")).json()
  const count = async (id, event) => (await observe()).find(row => row.kind === "observer" && row.subjectId === id && row.event === event)?.value ?? 0
  const waitObserver = async (id, event, expected = 1, timeout = 5000) => {
    const stop = Date.now() + timeout
    while (Date.now() < stop) {
      if (await count(id, event) >= expected) return
      await pause(50)
    }
    assert.fail(`Independent observer did not see ${id}/${event} count ${expected}; rows: ${JSON.stringify(await observe())}`)
  }
  const waitTarget = async (endpoint, predicate) => {
    for (let i = 0; i < 100; i++) {
      const result = await fetch(endpoint)
      if (predicate(result)) return result
      await pause(50)
    }
    assert.fail(`Target did not settle ${endpoint}`)
  }
  return {
    fetch, observe, count, waitObserver, waitTarget,
    providerPost: async (endpoint, body) => {
      const response = await provider.dispatchFetch(`http://provider${endpoint}`, { method: "POST", body: JSON.stringify(body) })
      return { status: response.status, body: await response.json() }
    },
    restart: async (next = {}) => {
      version = next.version ?? version
      dropWake = next.dropWake ?? dropWake
      await target.setOptions(options())
    },
    setDropWake: async () => { dropWake = true; await target.setOptions(options()) },
    close: async () => { await target.dispose(); await provider.dispose(); await fs.rm(root, { recursive: true, force: true }) }
  }
}
const withRuntime = (body) => async () => {
  const fixture = await make()
  try { await body(fixture) } finally { await fixture.close() }
}
const gate = (desired, knownGap, evidence) => {
  console.log(JSON.stringify({ engineGate: evidence, passed: desired }))
  if (process.env.COPPERTOP_STRICT_GATES === "1") assert.ok(desired, evidence)
  else assert.ok(knownGap, `Known-gap characterization changed: ${evidence}; investigate the source pin`)
}

test("completed Activity is deduplicated and survives restart/code version change", withRuntime(async f => {
  const endpoint = "/execute?name=Completed&id=completed"
  const [a, b] = await Promise.all([f.fetch(endpoint), f.fetch(endpoint)])
  assert.equal(a.result, "v1"); assert.equal(b.result, "v1")
  assert.equal(await f.count("completed", "activity"), 1)
  await f.restart({ version: "v2" })
  assert.equal((await f.fetch(endpoint)).result, "v1")
  assert.equal(await f.count("completed", "activity"), 1)
}))

test("REGRESSION: initial Activity autonomous recovery without target contact", withRuntime(async f => {
  await f.fetch("/execute?name=Initial&id=initial&discard=true")
  await f.waitObserver("initial", "entered")
  const before = await f.fetch("/snapshot?name=Initial&id=initial")
  assert.equal(before.activities.length, 0); assert.equal(before.deferreds.length, 0)
  assert.equal(before.clocks.length, 0); assert.equal(before.alarm, null)
  await f.restart()
  // Only the separate, still-running observer is contacted during this window.
  await pause(2500)
  const entered = await f.count("initial", "entered")
  console.log(JSON.stringify({ regression: "initial", observationMillis: 2500, observerEntries: entered,
    initialActivityRows: before.activities.length, initialDeferredRows: before.deferreds.length,
    initialClockRows: before.clocks.length, initialAlarm: before.alarm }))
  gate(entered >= 2, entered === 1, "initial-activity-autonomous-recovery")
  await f.fetch("/resume?name=Initial&id=initial") // diagnostic intervention after gate
  await f.waitObserver("initial", "entered", 2)
}))

test("durable clock resumes autonomously after restart", withRuntime(async f => {
  const deadline = Date.now() + 10_000
  await f.fetch(`/execute?name=Clock&id=clock&deadline=${deadline}&discard=true`)
  await f.waitTarget("/snapshot?name=Clock&id=clock", row => row.clocks.length === 1 && row.alarm !== null)
  await f.restart()
  // Rule out completion in the old isolate while setOptions prepared the restart.
  assert.equal(await f.count("clock", "done"), 0)
  await f.waitObserver("clock", "done", 1, 15_000) // no target contact after restart
  assert.equal(await f.count("clock", "done"), 1)
}))

test("deferred-only suspension stays suspended until legitimate external completion", withRuntime(async f => {
  await f.fetch("/execute?name=Suspended&id=suspended&discard=true")
  await f.waitTarget("/poll?name=Suspended&id=suspended", row => row._tag === "Suspended")
  await f.restart()
  await pause(500)
  assert.equal(await f.count("suspended", "done"), 0)
  assert.equal(await f.count("suspended", "entered"), 1)
  await f.fetch("/complete?name=Suspended&id=suspended")
  await f.waitObserver("suspended", "done")
}))

test("deterministic defect does not hot-loop through restart", withRuntime(async f => {
  await f.fetch("/execute?name=Defect&id=defect&discard=true")
  await f.waitObserver("defect", "entered")
  await pause(150)
  await f.restart()
  await pause(700)
  assert.equal(await f.count("defect", "entered"), 1)
}))

test("REGRESSION: exhausted child-to-parent wake retries survive isolate loss", withRuntime(async f => {
  await f.setDropWake()
  await f.fetch("/execute?name=Parent&id=parent&discard=true")
  await f.waitTarget("/poll?name=Parent&id=parent", row => row._tag === "Suspended")
  await f.fetch("/complete?name=Child&id=parent")
  await f.waitObserver("parent", "child-done")
  await f.waitObserver("wake", "dropped", 6, 10_000) // first attempt + five retries
  const parentState = await f.fetch("/snapshot?name=Parent&id=parent")
  const childState = await f.fetch("/snapshot?name=Child&id=parent")
  assert.equal(parentState.alarm, null)
  assert.equal(parentState.execution[0].resume_pending, 0)
  assert.equal(JSON.parse(parentState.execution[0].result)._tag, "Suspended")
  assert.equal(JSON.parse(childState.execution[0].result)._tag, "Complete")
  await f.restart({ dropWake: false })
  await pause(2500) // parent is not contacted or polled
  const done = await f.count("parent", "parent-done")
  console.log(JSON.stringify({ regression: "child-parent", observationMillis: 2500, observerParentCompletions: done,
    droppedWakeAttempts: await f.count("wake", "dropped"), parentAlarm: parentState.alarm,
    parentResumePending: parentState.execution[0].resume_pending, childResult: "Complete", parentResult: "Suspended" }))
  gate(done === 1, done === 0, "child-parent-wake-autonomous-recovery")
  await f.fetch("/resume?name=Parent&id=parent")
  await f.waitObserver("parent", "parent-done")
  assert.equal(await f.count("parent", "child-done"), 1)
}))

test("typed completion races a durable absolute deadline with replayed Activity", withRuntime(async f => {
  const deadline = Date.now() + 60_000
  await f.fetch(`/execute?name=Race&id=race&deadline=${deadline}&discard=true`)
  await f.waitTarget("/snapshot?name=Race&id=race", row => row.clocks.length === 1)
  await f.restart()
  await f.fetch("/complete?name=Race&id=race&value=provider-complete")
  await f.waitObserver("race", "winner-provider-complete")
  assert.equal(await f.count("race", "activity"), 1)
  await f.restart()
  assert.equal((await f.fetch("/poll?name=Race&id=race")).exit.value, "provider-complete")
  assert.equal(await f.count("race", "winner-deadline"), 0)
  const secondDeadline = Date.now() + 10_000
  await f.fetch(`/execute?name=Race&id=timeout&deadline=${secondDeadline}&discard=true`)
  await f.waitTarget("/snapshot?name=Race&id=timeout", row => row.clocks.length === 1)
  await f.restart()
  assert.equal(await f.count("timeout", "winner-deadline"), 0)
  await f.waitObserver("timeout", "winner-deadline", 1, 15_000)
  await f.fetch("/complete?name=Race&id=timeout&value=late-completion")
  assert.equal((await f.fetch("/poll?name=Race&id=timeout")).exit.value, "deadline")
}))

test("application definition guard quarantines suspended work on upgrade", withRuntime(async f => {
  await f.fetch("/execute?name=Guarded&id=guarded&discard=true")
  await f.waitTarget("/poll?name=Guarded&id=guarded", row => row._tag === "Suspended")
  await f.restart({ version: "v2" })
  await f.fetch("/complete?name=Guarded&id=guarded")
  const result = await f.waitTarget("/poll?name=Guarded&id=guarded", row => row._tag === "Complete")
  assert.equal(result.exit._tag, "Failure")
  assert.match(result.exit.cause, /definition-version-quarantined/)
  assert.equal(await f.count("guarded", "activity"), 1)
}))

test("interrupt completion persists through restart", withRuntime(async f => {
  await f.fetch("/execute?name=Suspended&id=cancelled&discard=true")
  await f.waitTarget("/poll?name=Suspended&id=cancelled", row => row._tag === "Suspended")
  await f.fetch("/interrupt?name=Suspended&id=cancelled")
  const before = await f.waitTarget("/poll?name=Suspended&id=cancelled", row => row._tag === "Complete")
  assert.equal(before.exit._tag, "Failure")
  await f.restart()
  assert.deepEqual(await f.fetch("/poll?name=Suspended&id=cancelled"), before)
  assert.equal(await f.count("cancelled", "done"), 0)
}))

test("shared one-shot ledger stops ambiguous spawn replay; cancellation reconciles cleanup/accounting", withRuntime(async f => {
  const id = "ambiguous"
  const spec = { schemaVersion: 1, definitionVersion: 1, runId: id, commandId: "submit", idempotencyKey: id,
    principal: "synthetic-operator", grantId: "synthetic-grant", preset: "fake-v1", deployment: "fake-deployment-v1",
    sourceDigest: "a".repeat(64), inputDigest: "b".repeat(64), engine: "effect-do", reservation: 100, deadline: Date.now() + 60_000 }
  const mutation = async command => f.fetch("/ledger", { method: "POST", body: JSON.stringify({ epoch: 1, now: Date.now(), command }) })
  assert.ok((await mutation({ type: "submit", spec, digest: "c".repeat(64) })).run)
  await Promise.all([f.fetch(`/execute?name=Launch&id=${id}&discard=true`), f.fetch(`/execute?name=Launch&id=${id}&discard=true`)])
  await f.waitObserver(id, "ambiguous")
  assert.equal((await f.observe()).filter(row => row.kind === "call").length, 1)
  await f.restart()
  await f.fetch(`/resume?name=Launch&id=${id}`) // explicit replay diagnostics, no autonomy claim
  await f.waitTarget(`/poll?name=Launch&id=${id}`, row => row._tag === "Complete")
  assert.equal((await f.observe()).filter(row => row.kind === "call").length, 1)
  const call = (await f.providerPost("/lookup", { runId: id })).body[0]
  await mutation({ type: "register", runId: id, callId: call.id })
  const [claimA, claimB] = await Promise.all([
    mutation({ type: "execute", runId: id, callId: call.id, bodyId: "body-a" }),
    mutation({ type: "execute", runId: id, callId: call.id, bodyId: "body-b" })
  ])
  assert.equal(Number(claimA.newPermission) + Number(claimB.newPermission), 1)
  await mutation({ type: "cancel", runId: id, commandId: "cancel" })
  const uncertainCleanup = await mutation({ type: "cleanup", runId: id, generationDisabled: false, stoppedCallIds: [call.id] })
  assert.equal(uncertainCleanup.run.held, 100)
  assert.equal((await mutation({ type: "settle", runId: id, cost: 1 })).failure, "CleanupUnconfirmed")
  await f.providerPost("/disable", { generation: id })
  await f.providerPost("/cancel", { callId: call.id })
  assert.equal((await f.providerPost("/spawn", { generation: id, runId: id })).status, 409)
  await mutation({ type: "cleanup", runId: id, generationDisabled: true, stoppedCallIds: [call.id] })
  const settled = await mutation({ type: "settle", runId: id, cost: 1 })
  assert.equal(settled.run.held, 0); assert.equal(settled.run.accounting, "settled")
  await f.restart()
  assert.equal((await f.fetch("/ledger")).runs[id].accounting, "settled")
}))

test("atomic admission and immutable receipt survive workerd replay", withRuntime(async f => {
  const specFor = id => ({ schemaVersion: 1, definitionVersion: 1, runId: id, commandId: `submit-${id}`, idempotencyKey: id,
    principal: "synthetic-operator", grantId: "synthetic-grant", preset: "fake-v1", deployment: "fake-deployment-v1",
    sourceDigest: "a".repeat(64), inputDigest: "b".repeat(64), engine: "effect-do", reservation: 100, deadline: Date.now() + 60_000 })
  const mutation = command => f.fetch("/ledger", { method: "POST", body: JSON.stringify({ epoch: 1, now: Date.now(), command }) })
  const admitted = await Promise.all(["receipt", "late", "denied"].map(id => mutation({ type: "submit", spec: specFor(id), digest: "c".repeat(64) })))
  assert.equal(admitted.filter(row => row.run).length, 2)
  assert.equal(admitted.filter(row => row.failure === "BudgetDenied").length, 1)
  const id = admitted.find(row => row.run).run.spec.runId
  await mutation({ type: "dispatch", runId: id })
  const call = (await f.providerPost("/spawn", { runId: id, generation: id })).body
  await mutation({ type: "register", runId: id, callId: call.id })
  const running = await mutation({ type: "execute", runId: id, callId: call.id, bodyId: "receipt-body" })
  assert.equal(running.newPermission, true)
  const content = JSON.stringify({ schemaVersion: 1, runId: id, source: "synthetic", decisionGate: "passed" })
  const digest = createHash("sha256").update(content).digest("hex")
  const key = `runs/${id}/${digest}.json`
  assert.equal((await f.providerPost("/artifact/put", { key, digest, content })).status, 200)
  assert.equal((await f.providerPost("/artifact/put", { key, digest, content: "conflicting bytes" })).status, 409)
  const receipt = { schemaVersion: 1, runId: id, specDigest: "c".repeat(64), bodyId: "receipt-body", validity: "valid",
    decisionGate: "passed", key, digest, bytes: Buffer.byteLength(content) }
  const stored = await mutation({ type: "artifact", runId: id, revision: running.run.revision, receipt })
  assert.deepEqual(stored.run.receipt, receipt)
  assert.equal((await mutation({ type: "artifact", runId: id, revision: stored.run.revision, receipt: { ...receipt, bytes: receipt.bytes + 1 } })).failure, "SpecConflict")
  await f.restart()
  assert.deepEqual((await f.fetch("/ledger")).runs[id].receipt, receipt)
  const reread = (await f.providerPost("/artifact/read", { key })).body
  assert.equal(reread.content, content)
  assert.equal(createHash("sha256").update(reread.content).digest("hex"), receipt.digest)
  assert.equal(reread.bytes, receipt.bytes)
  const lateId = admitted.filter(row => row.run)[1].run.spec.runId
  await mutation({ type: "dispatch", runId: lateId })
  await mutation({ type: "cancel", runId: lateId, commandId: "cancel-late" })
  // Provider dispatch was already in flight when cancellation was committed.
  const lateCall = (await f.providerPost("/spawn", { runId: lateId, generation: lateId })).body
  const registered = await mutation({ type: "register", runId: lateId, callId: lateCall.id })
  assert.ok(registered.run.callIds.includes(lateCall.id))
  assert.equal((await mutation({ type: "execute", runId: lateId, callId: lateCall.id, bodyId: "late-body" })).newPermission, false)
  assert.ok(Object.values(registered.ledger.outbox).some(item => item.runId === lateId && item.kind === "cleanup"))
  await f.providerPost("/disable", { generation: lateId })
  await f.providerPost("/cancel", { callId: lateCall.id })
  await mutation({ type: "cleanup", runId: lateId, generationDisabled: true, stoppedCallIds: [lateCall.id] })
  assert.equal((await mutation({ type: "settle", runId: lateId, cost: 1 })).run.held, 0)
  assert.equal((await f.providerPost("/spawn", { runId: lateId, generation: lateId })).status, 409)
}))
