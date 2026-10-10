import assert from "node:assert/strict";
import test from "node:test";
import {
  APIConnectionError,
  APIConnectionTimeoutError,
  APIUserAbortError,
} from "cloudflare";
import {
  plan,
  requireApproval,
  absent,
  execute,
  cleanup,
  ACCOUNT_ID,
  claimExecution,
  executionState,
  requireCleanupReceipt,
  auditOutput,
  ownedNamespaces,
  saveExecution,
  main,
} from "./controller.mjs";

import { mkdtemp, rm, mkdir, readFile } from "node:fs/promises";
import { tmpdir } from "node:os";
import { join } from "node:path";
import { acceptance } from "./report.ts";
import { MODES } from "./contracts.ts";

function passingOutput(p) {
  const snapshot = {
    calls: [],
    observations: [],
    fenced: [],
    artifacts: [],
    closed: false,
  };
  const ledger = { grant: { consumed: 6 }, quarantined: false, runs: {} };
  const statuses = {};
  for (const probe of MODES) {
    const id = `${p.params.suiteId}-${probe}`;
    const receipt = { runId: id, decisionGate: "passed" };
    snapshot.calls.push({
      runId: id,
      state: probe === "cancel" ? "cancelled" : "completed",
    });
    snapshot.fenced.push(id);
    ledger.runs[id] = {
      dispatchConsumed: true,
      bodyClaimant: `body-${probe}`,
      cleanup: "confirmed",
      accounting: "settled",
      held: 0,
      cost: 1,
      phase: "closed",
      outcome: probe === "cancel" ? "cancelled" : "succeeded",
    };
    if (probe !== "cancel") {
      ledger.runs[id].receipt = receipt;
      snapshot.artifacts.push(receipt);
    }
    statuses[probe] = probe === "cancel" ? "terminated" : "complete";
    const observe = (event, at, count = 1) =>
      snapshot.observations.push({ mode: probe, event, at, count });
    observe("body", 1_000_000);
    if (probe !== "cancel") observe("probe-complete", 1_020_000);
    if (probe === "timeout-retry") {
      observe("timeout-delay-start", 1_000_000);
      observe("first-attempt", 1_002_000, 2);
    }
    if (probe === "sleep") {
      observe("sleep-start", 1_000_000);
      observe("sleep-complete", 1_010_000);
    }
    if (["event", "event-timeout", "cancel"].includes(probe))
      observe("wait-start", 1_000_000);
    if (probe === "event") observe("event-received", 1_001_000);
    if (probe === "event-timeout") observe("event-timeout", 1_003_000);
    if (probe === "cancel") observe("cancel-observed", 1_001_000);
  }
  return {
    ...acceptance(p.params, snapshot, ledger, statuses),
    evidence: { payload: p.params, snapshot, ledger, statuses },
  };
}

const makePlan = (mode) =>
  plan("ct-test-canary", "a".repeat(64), 1_000_000, mode);
const approved = (p) => ({
  approved: true,
  suiteId: p.params.suiteId,
  buildDigest: p.params.buildDigest,
  deadline: p.params.deadline,
  cleanupAuthorized: true,
  incrementalDollarCap: 1,
  billingHeadroomVerified: true,
  subscriptionChangeAuthorized: false,
});
const creditApproved = (p) => ({
  ...approved(p),
  billingMode: "user-confirmed-credits",
  incrementalDollarCap: null,
  billingHeadroomVerified: false,
  creditAuthorization: {
    spendingScope: "this-bounded-synthetic-run-only",
    userConfirmedCreditsForPurpose: true,
    dollarOneCapUnverified: true,
    possibleDoOverageIncrementDollars: 12.5,
    overageRiskAccepted: true,
    creditBalanceVerified: false,
    creditApplicabilityVerified: false,
    evidence: {
      source: "user-message",
      userMessage: {
        messageId: "test-approval",
        at: new Date(999_000).toISOString(),
        text: "Run this bounded canary using credits.",
      },
      priorDisclosure: {
        at: new Date(998_000).toISOString(),
        text: "The $1 cap is unverified; DO overage can add $12.50.",
      },
    },
  },
});
function fake(p) {
  const resources = new Map(),
    trace = [],
    journals = [];
  let namespaces = [],
    creates = 0,
    clock = 1_000_000,
    claimed = false;
  const base = `/accounts/${ACCOUNT_ID}`;
  const notFound = () => Object.assign(new Error("not found"), { status: 404 });
  const refs = [
    { workflow: p.names.suite, id: `${p.params.suiteId}-controller` },
    ...[
      "control",
      "timeout-retry",
      "sleep",
      "event",
      "event-timeout",
      "cancel",
    ].map((mode) => ({
      workflow: p.names.probes,
      id: `${p.params.suiteId}-${mode}`,
    })),
    { workflow: p.names.observer, id: `${p.params.suiteId}-observer` },
  ];
  const api = {
    get: async (path) => {
      trace.push(["GET", path]);
      if (path.endsWith("/durable_objects/namespaces"))
        return { result: namespaces, result_info: { total_pages: 1 } };
      if (path.endsWith("/settings")) path = path.slice(0, -9);
      if (!resources.has(path)) throw notFound();
      return resources.get(path);
    },
    post: async (path, request) => {
      trace.push(["POST", path]);
      if (path.endsWith("/instances/batch/delete")) {
        for (const id of request.body.instances)
          resources.delete(`${path.slice(0, -13)}/${id}`);
        return {
          result: {
            deleted: request.body.instances.map((id) => ({ id })),
            errors: [],
          },
        };
      }
      creates++;
      assert.equal(request.body.instance_id, `${p.params.suiteId}-controller`);
      for (const ref of refs)
        resources.set(`${base}/workflows/${ref.workflow}/instances/${ref.id}`, {
          result: {
            status: "complete",
            output: passingOutput(p),
          },
        });
      return { result: {} };
    },
    delete: async (path) => {
      trace.push(["DELETE", path]);
      resources.delete(path);
      return { success: true };
    },
  };
  const deploy = async (name) => {
    trace.push(["DEPLOY", name]);
    if (name === "retire") {
      namespaces = [];
      return;
    }
    resources.set(`${base}/workers/scripts/${p.names[name]}`, {});
    if (name === "provider") {
      namespaces = [
        {
          id: "synthetic-owned",
          script: p.names.provider,
          class: "CanaryStore",
        },
      ];
      resources.set(`${base}/workflows/${p.names.observer}`, {});
    } else
      for (const flow of [p.names.suite, p.names.probes])
        resources.set(`${base}/workflows/${flow}`, {});
  };
  return {
    api,
    claim: async () => {
      if (claimed) throw new Error("Execution already claimed");
      claimed = true;
    },
    deploy,
    trace,
    journals,
    resources,
    creates: () => creates,
    now: () => clock,
    sleep: async (ms) => {
      clock += ms;
    },
    save: async (record) => {
      journals.push(structuredClone(record));
    },
  };
}
test("plan has two private Workers, eight instances, one namespace and no real providers", () => {
  const p = makePlan();
  assert.equal(p.resources.workers, 2);
  assert.equal(p.resources.workflowInstances, 8);
  assert.equal(p.resources.sqliteObjects, 3);
  for (const config of [p.provider, p.controller, p.retire]) {
    assert.equal(config.workers_dev, false);
    assert.equal(config.preview_urls, false);
    assert.deepEqual(config.routes, []);
    assert.equal(config.observability.enabled, false);
    assert.equal(config.limits.cpu_ms, 1000);
    assert.equal(config.account_id, ACCOUNT_ID);
  }
  assert.equal(p.controller.services[0].entrypoint, "SyntheticProvider");
  assert.equal(p.provider.durable_objects.bindings.length, 1);
  assert.equal(p.hostProcessLoss, "untested");
  assert.equal(p.realProviders, false);
  assert.equal(p.billingGate.verification, "operator-attestation-required");
  assert.throws(() => plan("../unrelated", "a".repeat(64)));
  assert.throws(() => plan("ct-bad", "not-a-build"));
});
test("implementation permission never substitutes for deployment/spend/headroom approval", async () => {
  const p = makePlan();
  for (const approval of [
    undefined,
    { ...approved(p), approved: false },
    { ...approved(p), billingHeadroomVerified: false },
    { ...approved(p), subscriptionChangeAuthorized: true },
    { ...approved(p), incrementalDollarCap: 2 },
    { ...approved(p), buildDigest: "b".repeat(64) },
    { ...approved(p), deadline: p.params.deadline + 1 },
    { ...approved(p), cleanupAuthorized: false },
  ])
    assert.throws(() => requireApproval(p, approval, 1_000_000));
  assert.throws(() => requireApproval(p, approved(p), p.params.deadline));
  let contacted = false;
  await assert.rejects(
    execute(
      {
        get: async () => {
          contacted = true;
        },
      },
      p,
      undefined,
      { now: () => 1_000_000 },
    ),
  );
  assert.equal(contacted, false);
});
test("credit authorization preserves verified-headroom mode and fixed resources", () => {
  const original = makePlan(),
    p = makePlan("user-confirmed-credits");
  requireApproval(original, approved(original), 1_000_000);
  requireApproval(p, creditApproved(p), 1_000_000);
  assert.deepEqual(p.resources, original.resources);
  for (const key of ["provider", "controller", "retire", "params", "names"])
    assert.deepEqual(p[key], original[key]);
  assert.equal(p.billingGate.dollarCap, null);
  assert.equal(p.billingGate.creditBalanceVerified, false);
  assert.equal(p.billingGate.creditApplicabilityVerified, false);
  assert.throws(() => requireApproval(p, approved(p), 1_000_000));
  assert.throws(() =>
    requireApproval(original, creditApproved(original), 1_000_000),
  );
  assert.throws(() => makePlan("unknown-mode"));
});
test("missing or ambiguous credit authorization fails before claim and API contact", async () => {
  const changes = [
    (a) => {
      delete a.billingMode;
    },
    (a) => {
      a.billingMode = null;
    },
    (a) => {
      delete a.creditAuthorization;
    },
    (a) => {
      a.approved = false;
    },
    (a) => {
      a.cleanupAuthorized = false;
    },
    (a) => {
      a.subscriptionChangeAuthorized = true;
    },
    (a) => {
      a.incrementalDollarCap = 1;
    },
    (a) => {
      delete a.incrementalDollarCap;
    },
    (a) => {
      a.billingHeadroomVerified = true;
    },
    (a) => {
      a.deadline++;
    },
    (a) => {
      a.buildDigest = "b".repeat(64);
    },
    (a) => {
      a.suiteId = "ct-other";
    },
    (a) => {
      a.creditAuthorization.spendingScope = "unlimited";
    },
    (a) => {
      a.creditAuthorization.userConfirmedCreditsForPurpose = false;
    },
    (a) => {
      a.creditAuthorization.dollarOneCapUnverified = false;
    },
    (a) => {
      a.creditAuthorization.possibleDoOverageIncrementDollars = 1;
    },
    (a) => {
      a.creditAuthorization.overageRiskAccepted = false;
    },
    (a) => {
      a.creditAuthorization.creditBalanceVerified = true;
    },
    (a) => {
      a.creditAuthorization.creditApplicabilityVerified = true;
    },
    (a) => {
      delete a.creditAuthorization.evidence;
    },
    (a) => {
      a.creditAuthorization.evidence.source = "operator-assumption";
    },
    (a) => {
      a.creditAuthorization.evidence.userMessage.messageId = " ";
    },
    (a) => {
      a.creditAuthorization.evidence.userMessage.text = "x".repeat(4097);
    },
    (a) => {
      a.creditAuthorization.evidence.priorDisclosure.text = "";
    },
    (a) => {
      a.creditAuthorization.evidence.priorDisclosure.at = new Date(
        999_001,
      ).toISOString();
    },
    (a) => {
      a.creditAuthorization.evidence.userMessage.at = new Date(
        1_000_001,
      ).toISOString();
    },
    (a) => {
      a.creditAuthorization.evidence.priorDisclosure.at =
        "1969-02-30T00:00:00Z";
    },
  ];
  const p = makePlan("user-confirmed-credits");
  for (const change of [undefined, ...changes]) {
    const a = change ? creditApproved(p) : undefined;
    if (change) change(a);
    const f = fake(p);
    let claims = 0;
    await assert.rejects(
      execute(f.api, p, a, {
        ...f,
        claim: async () => {
          claims++;
        },
      }),
    );
    assert.equal(claims, 0);
    assert.equal(f.trace.length, 0);
    assert.equal(f.journals.length, 0);
  }
});
test("both approval modes reject expanded plans before API contact", async () => {
  const changes = [
    ...Object.keys(makePlan().resources).map((key) => (p) => {
      p.resources[key]++;
    }),
    (p) => {
      p.provider.routes.push("example.com/*");
    },
    (p) => {
      p.provider.workers_dev = true;
    },
    (p) => {
      p.controller.preview_urls = true;
    },
    (p) => {
      p.controller.limits.cpu_ms++;
    },
    (p) => {
      p.controller.services[0].service = "unrelated";
    },
    (p) => {
      p.realProviders = true;
    },
    (p) => {
      p.provider.durable_objects.bindings.push({
        name: "OTHER",
        class_name: "Other",
      });
    },
  ];
  for (const mode of ["verified-headroom", "user-confirmed-credits"])
    for (const change of changes) {
      const p = makePlan(mode),
        a = mode === "verified-headroom" ? approved(p) : creditApproved(p);
      change(p);
      const f = fake(p);
      await assert.rejects(execute(f.api, p, a, f), /fixed canary scope/);
      assert.equal(f.trace.length, 0);
    }
});
test("credit approval remains one-use, deadline-bound and independently cleanable", async () => {
  const p = makePlan("user-confirmed-credits"),
    f = fake(p),
    a = creditApproved(p);
  await execute(f.api, p, a, f);
  assert.equal(f.creates(), 1);
  assert.equal(f.resources.size, 0);
  const before = f.trace.length;
  await assert.rejects(execute(f.api, p, a, f), /already claimed/);
  assert.equal(f.trace.length, before);
  assert.throws(() => requireApproval(p, a, p.params.deadline));
  assert.throws(() => requireApproval(p, a, 999_999));
  assert.deepEqual(
    requireCleanupReceipt(p, f.journals.at(-1)),
    f.journals.at(-1).state,
  );
});
function cleanupFixture() {
  const p = makePlan(),
    f = fake(p),
    id = `${p.params.suiteId}-controller`;
  const path = `/accounts/${ACCOUNT_ID}/workflows/${p.names.suite}/instances/${id}`;
  f.resources.set(path, { result: { status: "complete" } });
  return {
    p,
    f,
    id,
    path,
    state: { providerAttempted: false, controllerAttempted: true },
  };
}
test("cleanup distinguishes malformed, rejected and nonmatching deletion replies", async () => {
  for (const [response, reason] of [
    [{ result: { deleted: "unsupported", errors: [] } }, "malformed"],
    [
      {
        result: {
          deleted: [],
          errors: [{ id: "other", message: "private response" }],
        },
      },
      "rejected",
    ],
    [{ result: { deleted: [], errors: [] } }, "missingId"],
  ]) {
    const { p, f, state } = cleanupFixture();
    f.api.post = async () => response;
    const r = await cleanup(f.api, p, state, f.deploy);
    assert.equal(r.confirmed, false);
    assert.equal(r.failures[0].reason, reason);
    assert.equal(r.failures[0].phase, "validate-delete-response");
    assert.equal(r.failures[0].errorClass, "CleanupConfirmationError");
    assert.equal(r.instanceChecks[0].finalAbsent, false);
    assert.ok(!JSON.stringify(r).includes("private response"));
  }
});
test("acknowledged deletion still requires absence; later absence is retained without a causal claim", async () => {
  const { p, f, id, path, state } = cleanupFixture();
  f.api.post = async () => ({ result: { deleted: [{ id }], errors: [] } });
  const first = await cleanup(f.api, p, state, f.deploy);
  assert.equal(first.confirmed, false);
  assert.equal(first.failures[0].reason, "visible");
  assert.equal(first.failures[0].phase, "confirm-instance-absence");
  assert.equal(first.instanceChecks[0].reply.requestedIdReportedDeleted, true);
  f.resources.delete(path);
  const later = await cleanup(f.api, p, state, f.deploy);
  assert.equal(later.confirmed, true);
  assert.equal(later.instanceChecks[0].initialAbsent, true);
  assert.equal(later.instanceChecks[0].deletionAttempted, false);
  assert.equal(later.instanceChecks[0].finalAbsent, true);
});
test("cleanup retains safe transport diagnostics and lost-reply absence reconciliation", async () => {
  for (const lostReply of [false, true]) {
    const { p, f, path, state } = cleanupFixture();
    f.api.post = async () => {
      if (lostReply) f.resources.delete(path);
      throw new APIConnectionTimeoutError({
        message: "Bearer FAKE-PRIVATE-TOKEN",
      });
    };
    const r = await cleanup(f.api, p, state, f.deploy);
    assert.equal(r.confirmed, lostReply);
    assert.equal(
      r.instanceChecks[0].deleteError.errorClass,
      "APIConnectionTimeoutError",
    );
    assert.equal(
      r.instanceChecks[0].deleteError.message,
      "Cloudflare API connection failed",
    );
    assert.ok(!JSON.stringify(r).includes("FAKE-PRIVATE-TOKEN"));
    if (!lostReply) assert.equal(r.failures[0].phase, "batch-delete");
  }
});
test("cleanup omits raw auth/messages/causes and does not treat a denial as absence", async () => {
  const { p, f, state } = cleanupFixture();
  f.api.post = async () => {
    throw Object.assign(new Error("Authorization: Bearer FAKE_SECRET"), {
      status: 403,
      error: { errors: [{ code: 10000, message: "token=FAKE_SECRET" }] },
      headers: { authorization: "FAKE_SECRET" },
      cause: { url: "https://user:FAKE_SECRET@example.com" },
    });
  };
  const r = await cleanup(f.api, p, state, f.deploy);
  assert.equal(r.confirmed, false);
  assert.equal(r.failures[0].status, 403);
  assert.deepEqual(r.failures[0].codes, [10000]);
  assert.equal(r.failures[0].message, "Cloudflare API request failed");
  assert.ok(!JSON.stringify(r).includes("FAKE_SECRET"));
  assert.equal(r.instanceChecks[0].finalAbsent, false);
});
test("403 and network failures do not imply missing resources or permission to overwrite", async () => {
  await assert.rejects(
    absent(
      {
        get: async () => {
          throw Object.assign(new Error("denied"), { status: 403 });
        },
      },
      "/owned",
    ),
  );
  await assert.rejects(
    absent(
      {
        get: async () => {
          throw new Error("network denied");
        },
      },
      "/owned",
    ),
  );
  const p = makePlan(),
    f = fake(p);
  f.resources.set(
    `/accounts/${ACCOUNT_ID}/workers/scripts/${p.names.provider}`,
    {},
  );
  await assert.rejects(execute(f.api, p, approved(p), f), /already exists/);
  assert.ok(!f.trace.some((row) => row[0] === "DEPLOY"));
});
test("lost controller launch reply is reconciled once; cleanup removes only exact owned resources", async () => {
  const p = makePlan(),
    f = fake(p),
    post = f.api.post;
  f.api.post = async (...args) => {
    await post(...args);
    throw new Error("reply lost after creation");
  };
  const output = await execute(f.api, p, approved(p), f);
  assert.equal(output.supportedGatesPass, true);
  assert.equal(f.creates(), 1);
  assert.equal(f.resources.size, 0);
  assert.equal(f.journals.at(-1).cleanup.confirmed, true);
  assert.equal(
    f.trace.filter(
      (row) => row[0] === "POST" && row[1].endsWith("/instances/batch/delete"),
    ).length,
    8,
  );
  assert.ok(
    f.trace.findIndex((row) => row[0] === "DEPLOY" && row[1] === "retire") <
      f.trace.findIndex(
        (row) =>
          row[0] === "DELETE" &&
          row[1].endsWith(`/scripts/${p.names.provider}`),
      ),
  );
});
test("ambiguous partial deployment is cleaned while preserving source scope", async () => {
  const p = makePlan(),
    f = fake(p),
    deploy = f.deploy;
  f.deploy = async (name) => {
    await deploy(name);
    if (name === "provider") throw new Error("deployment acknowledgement lost");
  };
  await assert.rejects(execute(f.api, p, approved(p), f), /incomplete/);
  assert.equal(f.creates(), 0);
  assert.equal(f.resources.size, 0);
  assert.equal(f.journals.at(-1).cleanup.confirmed, true);
});
test("namespace cleanup denial preserves Worker and durable cleanup authority", async () => {
  const p = makePlan(),
    f = fake(p);
  await f.deploy("provider");
  const get = f.api.get;
  f.api.get = async (path) => {
    if (path.endsWith("/durable_objects/namespaces"))
      throw Object.assign(new Error("denied"), { status: 403 });
    return get(path);
  };
  const result = await cleanup(
    f.api,
    p,
    { providerAttempted: true, controllerAttempted: false },
    f.deploy,
  );
  assert.equal(result.confirmed, false);
  assert.equal(result.failures[0].status, 403);
  assert.ok(
    f.resources.has(
      `/accounts/${ACCOUNT_ID}/workers/scripts/${p.names.provider}`,
    ),
  );
});
test("cleanup with no attempted deployment performs no deletion", async () => {
  const p = makePlan(),
    f = fake(p);
  const result = await cleanup(
    f.api,
    p,
    { providerAttempted: false, controllerAttempted: false },
    f.deploy,
  );
  assert.equal(result.confirmed, true);
  assert.deepEqual(f.trace, []);
});
test("finite runner polling budget expires and still attempts cleanup", async () => {
  const p = makePlan(),
    f = fake(p),
    post = f.api.post;
  f.api.post = async (...args) => {
    const response = await post(...args);
    if (args[0].endsWith("/batch/delete")) return response;
    f.resources.set(
      `/accounts/${ACCOUNT_ID}/workflows/${p.names.suite}/instances/${p.params.suiteId}-controller`,
      { result: { status: "running" } },
    );
    return {};
  };
  await assert.rejects(execute(f.api, p, approved(p), f), /incomplete/);
  assert.equal(f.journals.at(-1).cleanup.confirmed, true);
  assert.equal(f.creates(), 1);
  assert.ok(
    f.trace.filter(
      (row) =>
        row[0] === "GET" && row[1].endsWith(`${p.params.suiteId}-controller`),
    ).length <= 902,
  );
});

test("failed supported evidence never returns success even when cleanup succeeds", async () => {
  const p = makePlan(),
    f = fake(p),
    post = f.api.post;
  f.api.post = async (...args) => {
    const response = await post(...args);
    if (!args[0].endsWith("/batch/delete"))
      f.resources.set(
        `/accounts/${ACCOUNT_ID}/workflows/${p.names.suite}/instances/${p.params.suiteId}-controller`,
        {
          result: { status: "complete", output: { supportedGatesPass: false } },
        },
      );
    return response;
  };
  await assert.rejects(execute(f.api, p, approved(p), f), /incomplete/);
  assert.equal(f.journals.at(-1).cleanup.confirmed, true);
});
test("namespace pagination ambiguity blocks deployment before any mutation", async () => {
  const p = makePlan(),
    f = fake(p),
    get = f.api.get;
  f.api.get = async (path) =>
    path.endsWith("/namespaces")
      ? { result: Array(100).fill({ script: "other", class: "Other" }) }
      : get(path);
  await assert.rejects(execute(f.api, p, approved(p), f), /pagination/);
  assert.ok(!f.trace.some((row) => row[0] === "DEPLOY"));
});

test("approval cannot run the same suite twice even after complete teardown", async () => {
  const p = makePlan(),
    f = fake(p);
  await execute(f.api, p, approved(p), f);
  assert.equal(f.resources.size, 0);
  const before = f.trace.length;
  await assert.rejects(execute(f.api, p, approved(p), f), /already claimed/);
  assert.equal(f.trace.length, before);
});
test("exclusive durable claims survive re-planning, directory changes and concurrent attempts", async () => {
  const dir = await mkdtemp(join(tmpdir(), "ct-claim-"));
  try {
    const p = makePlan(),
      registry = join(dir, "registry"),
      first = join(dir, "first"),
      second = join(dir, "second");
    await mkdir(first);
    await mkdir(second);
    const claims = await Promise.allSettled([
      claimExecution(p, first, registry),
      claimExecution(p, second, registry),
    ]);
    assert.equal(claims.filter((row) => row.status === "fulfilled").length, 1);
    await assert.rejects(
      claimExecution(
        plan(p.params.suiteId, p.params.buildDigest, 1_000_001),
        second,
        registry,
      ),
      { code: "EEXIST" },
    );
    await assert.rejects(
      claimExecution(
        plan(
          p.params.suiteId,
          p.params.buildDigest,
          1_000_001,
          "user-confirmed-credits",
        ),
        second,
        registry,
      ),
      { code: "EEXIST" },
    );
    const winner = claims[0].status === "fulfilled" ? first : second;
    const receipt = JSON.parse(
      await readFile(join(winner, "execution.json"), "utf8"),
    );
    assert.deepEqual(requireCleanupReceipt(p, receipt), executionState(p));
    // Cleanup authority is retained without the original spending/headroom file.
    assert.throws(() =>
      requireCleanupReceipt(p, {
        state: { ...receipt.state, deadline: p.params.deadline + 1 },
      }),
    );
    assert.throws(() =>
      requireCleanupReceipt(p, {
        state: { ...receipt.state, cleanupAuthorized: false },
      }),
    );
  } finally {
    await rm(dir, { recursive: true, force: true });
  }
});
test("local acceptance rejects missing/raw contradictory evidence and altered verdicts", () => {
  const p = makePlan(),
    output = passingOutput(p);
  assert.equal(auditOutput(p, output).supportedGatesPass, true);
  for (const change of [
    (value) => {
      delete value.evidence;
    },
    (value) => {
      value.evidence.snapshot.calls.pop();
    },
    (value) => {
      value.evidence.ledger.grant.consumed = 7;
    },
    (value) => {
      value.gates.control = false;
    },
    (value) => {
      value.evidence.payload.deadline++;
    },
  ]) {
    const bad = structuredClone(output);
    change(bad);
    assert.throws(() => auditOutput(p, bad));
  }
});
test("raw failed evidence survives teardown in the local receipt", async () => {
  const p = makePlan(),
    f = fake(p),
    post = f.api.post;
  f.api.post = async (...args) => {
    const reply = await post(...args);
    if (!args[0].endsWith("/batch/delete")) {
      const record = f.resources.get(
        `/accounts/${ACCOUNT_ID}/workflows/${p.names.suite}/instances/${p.params.suiteId}-controller`,
      );
      record.result.output.evidence.snapshot.calls.pop();
    }
    return reply;
  };
  await assert.rejects(execute(f.api, p, approved(p), f), /incomplete/);
  assert.equal(f.journals.at(-1).output.evidence.snapshot.calls.length, 5);
  assert.equal(f.journals.at(-1).cleanup.confirmed, true);
});
test("namespace pagination without total_pages exhausts via empty page and keeps owned rows", async () => {
  const pages = [];
  const result = await ownedNamespaces(
    {
      get: async (_path, request) => {
        pages.push(request.query.page);
        return {
          result:
            request.query.page === 1
              ? [{ script: "ct-provider", class: "CanaryStore" }]
              : [],
        };
      },
    },
    "ct-provider",
  );
  assert.deepEqual(pages, [1, 2]);
  assert.equal(result.length, 1);
  for (const total_pages of ["bad", -1])
    await assert.rejects(
      ownedNamespaces(
        { get: async () => ({ result: [], result_info: { total_pages } }) },
        "ct-provider",
      ),
      /pagination/,
    );
});
test("a refused controller Worker deletion never force-deletes or skips namespace confirmation", async () => {
  const p = makePlan(),
    f = fake(p),
    remove = f.api.delete;
  f.api.delete = async (path) => {
    if (path.endsWith(`/scripts/${p.names.controller}`)) {
      f.trace.push(["REFUSED", path]);
      throw Object.assign(new Error("binding conflict"), { status: 409 });
    }
    return remove(path);
  };
  await assert.rejects(execute(f.api, p, approved(p), f), /incomplete/);
  assert.equal(f.journals.at(-1).cleanup.failures[0].status, 409);
  const retire = f.trace.findIndex(
    (row) => row[0] === "DEPLOY" && row[1] === "retire",
  );
  const providerDelete = f.trace.findIndex(
    (row) =>
      row[0] === "DELETE" && row[1].endsWith(`/scripts/${p.names.provider}`),
  );
  assert.ok(retire >= 0 && providerDelete > retire);
  assert.ok(f.trace.every((row) => !row[1].includes("force=")));
});
for (const failure of [429, 503, APIConnectionError, APIConnectionTimeoutError])
  test(`bounded transient status reads recover: ${typeof failure === "number" ? failure : failure.name}`, async () => {
    const p = makePlan(),
      f = fake(p),
      get = f.api.get;
    let failures = 0;
    f.api.get = async (path, ...args) => {
      if (
        f.creates() &&
        path.endsWith(`${p.params.suiteId}-controller`) &&
        failures++ < 3
      ) {
        const error =
          typeof failure === "number"
            ? Object.assign(new Error("transient read"), { status: failure })
            : new failure({});
        if (typeof failure !== "number") assert.equal(error.name, "Error");
        throw error;
      }
      return get(path, ...args);
    };
    assert.equal(
      (await execute(f.api, p, approved(p), f)).supportedGatesPass,
      true,
    );
    assert.equal(f.creates(), 1);
    assert.equal(f.journals.at(-1).cleanup.confirmed, true);
  });
for (const Failure of [APIConnectionError, APIConnectionTimeoutError])
  test(`fourth real SDK connection failure stops polling and still cleans up: ${Failure.name}`, async () => {
    const p = makePlan(),
      f = fake(p),
      get = f.api.get;
    let failures = 0;
    f.api.get = async (path, ...args) => {
      if (
        f.creates() &&
        path.endsWith(`${p.params.suiteId}-controller`) &&
        failures < 4
      ) {
        failures++;
        throw new Failure({});
      }
      return get(path, ...args);
    };
    await assert.rejects(execute(f.api, p, approved(p), f), /incomplete/);
    assert.equal(failures, 4);
    assert.equal(f.journals.at(-1).cleanup.confirmed, true);
    assert.equal(f.creates(), 1);
  });
test("abort and lookalike error names never grant connection retries", async () => {
  for (const error of [
    new APIUserAbortError({}),
    Object.assign(new Error("lookalike"), { name: "APIConnectionError" }),
  ]) {
    const p = makePlan(),
      f = fake(p),
      get = f.api.get;
    let failures = 0;
    f.api.get = async (path, ...args) => {
      if (
        f.creates() &&
        path.endsWith(`${p.params.suiteId}-controller`) &&
        failures < 1
      ) {
        failures++;
        throw error;
      }
      return get(path, ...args);
    };
    await assert.rejects(execute(f.api, p, approved(p), f), /incomplete/);
    assert.equal(failures, 1);
    assert.ok(f.journals.at(-1).runFailure);
    assert.equal(f.journals.at(-1).cleanup.confirmed, true);
    assert.equal(f.creates(), 1);
  }
});
for (const failure of ["fourth-transient", "denial", "malformed-result"])
  test(`status read failure remains bounded and fails closed: ${failure}`, async () => {
    const p = makePlan(),
      f = fake(p),
      get = f.api.get;
    let reads = 0;
    f.api.get = async (path, ...args) => {
      // Inject only the polling reads, allowing later teardown reconciliation.
      if (
        f.creates() &&
        path.endsWith(`${p.params.suiteId}-controller`) &&
        reads++ < (failure === "fourth-transient" ? 4 : 1)
      ) {
        if (failure === "malformed-result") return {};
        throw Object.assign(new Error("read failed"), {
          status: failure === "denial" ? 403 : 503,
        });
      }
      return get(path, ...args);
    };
    await assert.rejects(execute(f.api, p, approved(p), f), /incomplete/);
    assert.equal(f.journals.at(-1).cleanup.confirmed, true);
    assert.equal(f.creates(), 1);
  });

test("local audit accepts JSON field reordering without changing plan identity", () => {
  const p = makePlan(),
    output = passingOutput(p);
  output.evidence.payload = Object.fromEntries(
    Object.entries(output.evidence.payload).reverse(),
  );
  output.gates = Object.fromEntries(Object.entries(output.gates).reverse());
  assert.equal(auditOutput(p, output).supportedGatesPass, true);
});
test("local re-planning cannot overwrite a consumed execution's cleanup plan", async () => {
  const dir = await mkdtemp(join(tmpdir(), "ct-preserve-plan-"));
  try {
    const p = makePlan();
    await claimExecution(p, dir, join(dir, "registry"));
    await assert.rejects(
      main(["plan", p.params.suiteId, dir]),
      /preserve its original plan/,
    );
    await assert.rejects(
      main(["package", p.params.suiteId, dir]),
      /preserve its original plan/,
    );
  } finally {
    await rm(dir, { recursive: true, force: true });
  }
});
test("CLI preparation selects credit mode explicitly and rejects unknown modes", async () => {
  const dir = await mkdtemp(join(tmpdir(), "ct-credit-plan-"));
  try {
    await main(["plan", "ct-credit-plan", dir, "user-confirmed-credits"]);
    const p = JSON.parse(await readFile(join(dir, "plan.json"), "utf8"));
    assert.equal(p.billingGate.mode, "user-confirmed-credits");
    assert.equal(p.billingGate.dollarCap, null);
    await assert.rejects(
      main(["plan", "ct-credit-plan", dir, "unknown"]),
      /Invalid/,
    );
    assert.deepEqual(
      JSON.parse(await readFile(join(dir, "plan.json"), "utf8")),
      p,
    );
  } finally {
    await rm(dir, { recursive: true, force: true });
  }
});
test("cleanup history preserves the first failure when a later attempt succeeds", async () => {
  const dir = await mkdtemp(join(tmpdir(), "ct-cleanup-history-"));
  try {
    const p = makePlan(),
      state = executionState(p);
    const f = fake(p);
    state.controllerAttempted = true;
    f.resources.set(
      `/accounts/${ACCOUNT_ID}/workflows/${p.names.suite}/instances/${p.params.suiteId}-controller`,
      { result: { status: "complete" } },
    );
    f.api.post = async () => ({ result: { deleted: [], errors: [] } });
    const first = await cleanup(f.api, p, state, f.deploy);
    assert.equal(first.confirmed, false);
    await saveExecution(
      dir,
      { state, output: { privateEvidence: "not-in-history" }, cleanup: first },
      1,
    );
    f.api.get = async () => {
      throw Object.assign(new Error("gone"), { status: 404 });
    };
    const second = await cleanup(f.api, p, state, f.deploy);
    assert.equal(second.confirmed, true);
    await saveExecution(dir, { state, cleanup: second }, 2);
    const history = (await readFile(join(dir, "cleanup-history.jsonl"), "utf8"))
      .trim()
      .split("\n")
      .map(JSON.parse);
    assert.deepEqual(history, [
      { at: 1, state, cleanup: first },
      { at: 2, state, cleanup: second },
    ]);
    assert.equal(
      JSON.parse(await readFile(join(dir, "execution.json"), "utf8")).cleanup
        .confirmed,
      true,
    );
    await rm(join(dir, "cleanup-history.jsonl"));
    await mkdir(join(dir, "cleanup-history.jsonl"));
    await assert.rejects(
      saveExecution(dir, { state, cleanup: first }, 3),
      /directory|EISDIR/,
    );
    assert.equal(
      JSON.parse(await readFile(join(dir, "execution.json"), "utf8")).cleanup
        .confirmed,
      true,
    );
  } finally {
    await rm(dir, { recursive: true, force: true });
  }
});
