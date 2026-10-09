import assert from "node:assert/strict";
import test from "node:test";
import {
  plan,
  requireApproval,
  absent,
  execute,
  cleanup,
  ACCOUNT_ID,
} from "./controller.mjs";

const makePlan = () => plan("ct-test-canary", "a".repeat(64), 1_000_000);
const approved = (p) => ({
  approved: true,
  suiteId: p.params.suiteId,
  buildDigest: p.params.buildDigest,
  incrementalDollarCap: 1,
  billingHeadroomVerified: true,
  subscriptionChangeAuthorized: false,
});
function fake(p) {
  const resources = new Map(),
    trace = [],
    journals = [];
  let namespaces = [],
    creates = 0,
    clock = 1_000_000;
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
            output: {
              supportedGatesPass: true,
              suiteId: p.params.suiteId,
              buildDigest: p.params.buildDigest,
              hostProcessLoss: "untested",
              realProviderReady: false,
            },
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
  assert.equal(p.billingGate.verified, false);
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
    path.endsWith("/namespaces") ? { result: [] } : get(path);
  await assert.rejects(execute(f.api, p, approved(p), f), /pagination/);
  assert.ok(!f.trace.some((row) => row[0] === "DEPLOY"));
});
