import { createHash } from "node:crypto";
import { readFile, writeFile, mkdir } from "node:fs/promises";
import { resolve, dirname, join } from "node:path";
import { fileURLToPath, pathToFileURL } from "node:url";
import { spawnSync } from "node:child_process";
import { acceptance } from "./report.ts";
import { canonical } from "./contracts.ts";

export const ACCOUNT_ID = "a06ff95ff66e0f58f269da6d2fb99021"; // Previously returned by Cloudflare, not a credential.
const root = dirname(fileURLToPath(import.meta.url));
const sources = [
  "contracts.ts",
  "provider.ts",
  "workflow.ts",
  "report.ts",
  "controller.mjs",
  "retired.ts",
  "package.json",
  "tsconfig.json",
  "package-lock.json",
  "../shared/domain.ts",
  "../shared/contracts.ts",
  "../lane-a/schema.ts",
  "../lane-a/package.json",
  "../lane-a/package-lock.json",
];
export async function buildDigest() {
  const hash = createHash("sha256");
  for (const source of sources) {
    hash.update(source);
    hash.update(await readFile(join(root, source)));
  }
  return hash.digest("hex");
}
export function plan(suiteId, buildIdentity, now = Date.now()) {
  if (
    !/^ct-[a-z0-9-]{1,35}$/.test(suiteId) ||
    !/^[a-f0-9]{64}$/.test(buildIdentity)
  )
    throw new Error("Invalid fixed canary identity");
  const params = {
    version: 1,
    suiteId,
    buildDigest: buildIdentity,
    deadline: now + 1_800_000,
  };
  const names = {
    provider: `${suiteId}-provider`,
    controller: `${suiteId}-control`,
    observer: `${suiteId}-observe`,
    probes: `${suiteId}-probes`,
    suite: `${suiteId}-suite`,
  };
  const base = {
    account_id: ACCOUNT_ID,
    compatibility_date: "2026-09-26",
    compatibility_flags: ["nodejs_compat"],
    workers_dev: false,
    preview_urls: false,
    routes: [],
    send_metrics: false,
    observability: { enabled: false },
    limits: { cpu_ms: 1000 },
    vars: {
      CANARY_SUITE: suiteId,
      CANARY_DEADLINE: String(params.deadline),
      CANARY_BUILD: buildIdentity,
    },
  };
  const retention = { success_retention: "1 hour", error_retention: "1 hour" };
  const provider = {
    ...base,
    name: names.provider,
    main: join(root, "provider.ts"),
    durable_objects: {
      bindings: [{ name: "STATE", class_name: "CanaryStore" }],
    },
    migrations: [{ tag: "canary-v1", new_sqlite_classes: ["CanaryStore"] }],
    workflows: [
      {
        binding: "OBSERVER",
        name: names.observer,
        class_name: "CanaryObserver",
        default_retention: retention,
      },
    ],
  };
  const controller = {
    ...base,
    name: names.controller,
    main: join(root, "workflow.ts"),
    services: [
      {
        binding: "PROVIDER",
        service: names.provider,
        entrypoint: "SyntheticProvider",
      },
    ],
    workflows: [
      {
        binding: "CONTROL",
        name: names.suite,
        class_name: "CanaryController",
        default_retention: retention,
      },
      {
        binding: "PROBES",
        name: names.probes,
        class_name: "CanaryProbe",
        default_retention: retention,
      },
      {
        binding: "OBSERVER",
        name: names.observer,
        class_name: "CanaryObserver",
        script_name: names.provider,
      },
    ],
  };
  const retire = {
    ...base,
    name: names.provider,
    main: join(root, "retired.ts"),
    migrations: [
      { tag: "canary-v1", new_sqlite_classes: ["CanaryStore"] },
      { tag: "retired-v1", deleted_classes: ["CanaryStore"] },
    ],
  };
  return {
    version: 1,
    accountId: ACCOUNT_ID,
    params,
    names,
    provider,
    controller,
    retire,
    resources: {
      workers: 2,
      workflowDefinitions: 3,
      workflowInstances: 8,
      sqliteNamespaces: 1,
      sqliteObjects: 3,
      applicationPayloadBytes: 1_048_576,
      executionMilliseconds: 1_800_000,
    },
    billingGate: {
      verification: "operator-attestation-required",
      dollarCap: 1,
      subscriptionChangeAuthorized: false,
      reason:
        "Subscription/quota reads denied. Verify free plan or sufficient included allowance before execution; DO duration overage can add a $12.50 rounded unit.",
    },
    hostProcessLoss: "untested",
    realProviders: false,
  };
}
export function requireApproval(p, approval, now = Date.now()) {
  if (
    approval?.approved !== true ||
    approval?.suiteId !== p.params.suiteId ||
    approval?.buildDigest !== p.params.buildDigest ||
    approval?.deadline !== p.params.deadline ||
    approval?.cleanupAuthorized !== true ||
    approval?.incrementalDollarCap !== 1 ||
    approval?.billingHeadroomVerified !== true ||
    approval?.subscriptionChangeAuthorized !== false
  )
    throw new Error(
      "Separate deployment/$1 approval and verified billing headroom required",
    );
  if (
    p.params.deadline <= now ||
    p.params.deadline > now + 1_800_000 ||
    p.accountId !== ACCOUNT_ID
  )
    throw new Error("Expired or mismatched deployment scope");
}
export function executionState(p) {
  return {
    suiteId: p.params.suiteId,
    buildDigest: p.params.buildDigest,
    deadline: p.params.deadline,
    cleanupAuthorized: true,
    providerAttempted: false,
    controllerAttempted: false,
  };
}
export async function claimExecution(
  p,
  dir,
  registry = join(root, "../../.local/managed-canary-executions"),
) {
  await mkdir(registry, { recursive: true });
  const record = JSON.stringify({ state: executionState(p) }, null, 2) + "\n";
  // Exclusive, permanent per-suite claim survives teardown and re-planning.
  // It is local protection, not a Cloudflare/account-wide replay barrier.
  await writeFile(join(registry, `${p.params.suiteId}.json`), record, {
    flag: "wx",
  });
  await writeFile(join(dir, "execution.json"), record, { flag: "wx" });
}
export function requireCleanupReceipt(p, receipt) {
  const state = receipt?.state;
  if (
    state?.suiteId !== p.params.suiteId ||
    state?.buildDigest !== p.params.buildDigest ||
    state?.deadline !== p.params.deadline ||
    state?.cleanupAuthorized !== true ||
    typeof state.providerAttempted !== "boolean" ||
    typeof state.controllerAttempted !== "boolean"
  )
    throw new Error("Cleanup receipt scope/authority mismatch");
  return state;
}
export function auditOutput(p, output) {
  if (Buffer.byteLength(JSON.stringify(output) ?? "") > 1_048_576)
    throw new Error("Evidence size bound exceeded");
  const evidence = output?.evidence;
  if (canonical(evidence?.payload ?? {}) !== canonical(p.params))
    throw new Error("Evidence plan identity mismatch");
  const local = acceptance(
    p.params,
    evidence.snapshot,
    evidence.ledger,
    evidence.statuses,
  );
  if (
    local.supportedGatesPass !== true ||
    output.supportedGatesPass !== true ||
    output.version !== local.version ||
    output.suiteId !== local.suiteId ||
    output.buildDigest !== local.buildDigest ||
    output.hostProcessLoss !== "untested" ||
    output.realProviderReady !== false ||
    canonical(output.gates ?? {}) !== canonical(local.gates)
  )
    throw new Error("Local evidence acceptance differs from reported verdict");
  return local;
}
export async function materialize(p, dir) {
  await mkdir(dir, { recursive: true });
  await writeFile(join(dir, "plan.json"), JSON.stringify(p, null, 2) + "\n");
  for (const key of ["provider", "controller", "retire"])
    await writeFile(
      join(dir, `${key}.json`),
      JSON.stringify(p[key], null, 2) + "\n",
    );
}
export function wrangler(args) {
  const result = spawnSync(
    "env",
    [
      "WRANGLER_SEND_METRICS=false",
      "WRANGLER_LOG=error",
      `XDG_CONFIG_HOME=${join(root, "../../.local/managed-wrangler-config")}`,
      process.execPath,
      join(root, "node_modules/wrangler/bin/wrangler.js"),
      ...args,
    ],
    { cwd: root, stdio: "pipe", encoding: "utf8", timeout: 120_000 },
  );
  if (result.status !== 0)
    throw new Error(
      `Wrangler ${args[0]} failed (${result.status ?? "process error"})`,
    );
  // Wrangler's default output contains resource summaries, never request headers.
  return result.stdout;
}
const refList = (p) => [
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
const missing = (error) => error?.status === 404;
export async function absent(api, path) {
  try {
    await api.get(path);
    return false;
  } catch (error) {
    if (missing(error)) return true;
    throw error;
  }
}
export async function ownedNamespaces(api, providerName) {
  const owned = [];
  for (let page = 1; page <= 10; page++) {
    const response = await api.get(
      `/accounts/${ACCOUNT_ID}/workers/durable_objects/namespaces`,
      { query: { page, per_page: 100 } },
    );
    if (!Array.isArray(response.result))
      throw new Error("Namespace listing schema unsupported");
    owned.push(
      ...response.result.filter(
        (row) => row.script === providerName && row.class === "CanaryStore",
      ),
    );
    const total = response.result_info?.total_pages;
    if (total !== undefined) {
      if (
        !Number.isSafeInteger(total) ||
        total < 0 ||
        (total < page && response.result.length)
      )
        throw new Error("Namespace pagination cannot establish absence");
      if (page >= total) return owned;
    }
    // The pinned SDK also establishes exhaustion from an empty page.
    if (response.result.length === 0) return owned;
  }
  throw new Error("Namespace pagination safety limit exceeded");
}
export async function cleanup(api, p, state, deploy) {
  const base = `/accounts/${ACCOUNT_ID}`;
  const failures = [];
  const attempt = async (label, action) => {
    try {
      await action();
    } catch (error) {
      failures.push({
        action: label,
        status: error?.status ?? "local failure",
      });
    }
  };
  // Delete only resources proven absent at preflight and attempted by this execution.
  if (state.controllerAttempted) {
    for (const ref of refList(p))
      await attempt(`instance:${ref.id}`, async () => {
        const path = `${base}/workflows/${ref.workflow}/instances/${ref.id}`;
        if (!(await absent(api, path))) {
          let response;
          try {
            response = await api.post(
              `${base}/workflows/${ref.workflow}/instances/batch/delete`,
              { body: { instances: [ref.id] } },
            );
          } catch (error) {
            if (!(await absent(api, path))) throw error;
          }
          if (
            response &&
            (!Array.isArray(response.result?.deleted) ||
              !response.result.deleted.some((row) => row.id === ref.id) ||
              response.result.errors?.length)
          )
            throw new Error("Instance batch deletion unconfirmed");
        }
        if (!(await absent(api, path)))
          throw new Error("Instance deletion unconfirmed");
      });
  }
  if (state.providerAttempted) {
    for (const name of [p.names.suite, p.names.probes, p.names.observer])
      await attempt(`workflow:${name}`, async () => {
        const path = `${base}/workflows/${name}`;
        if (!(await absent(api, path))) await api.delete(path);
        if (!(await absent(api, path)))
          throw new Error("Workflow deletion unconfirmed");
      });
  }
  if (state.controllerAttempted)
    await attempt(`worker:${p.names.controller}`, async () => {
      // Deliberately non-forced SDK script deletion: bindings may refuse it.
      // Never implicitly force-delete a Durable Object namespace.
      const path = `${base}/workers/scripts/${p.names.controller}`;
      if (!(await absent(api, `${path}/settings`))) await api.delete(path);
      if (!(await absent(api, `${path}/settings`)))
        throw new Error("Worker deletion unconfirmed");
    });
  if (state.providerAttempted) {
    // Official deletion migration removes the namespace and its owned SQLite data.
    await attempt("namespace:CanaryStore", async () => {
      const owned = await ownedNamespaces(api, p.names.provider);
      if (owned.length > 1) throw new Error("Unexpected namespace ownership");
      if (owned.length) await deploy("retire");
      if ((await ownedNamespaces(api, p.names.provider)).length)
        throw new Error("Namespace deletion unconfirmed");
    });
    // Retain the Worker if namespace cleanup failed: preserve retry authority.
    if (!failures.some((row) => row.action === "namespace:CanaryStore"))
      await attempt(`worker:${p.names.provider}`, async () => {
        const path = `${base}/workers/scripts/${p.names.provider}`;
        if (!(await absent(api, `${path}/settings`))) await api.delete(path);
        if (!(await absent(api, `${path}/settings`)))
          throw new Error("Worker deletion unconfirmed");
      });
  }
  return { confirmed: failures.length === 0, failures };
}
export async function execute(
  api,
  p,
  approval,
  {
    deploy,
    save,
    claim,
    sleep = (ms) => new Promise((resolve) => setTimeout(resolve, ms)),
    now = Date.now,
  } = {},
) {
  requireApproval(p, approval, now());
  if (typeof claim !== "function")
    throw new Error("Durable one-use execution claim required");
  await claim();
  const base = `/accounts/${ACCOUNT_ID}`;
  for (const name of [p.names.provider, p.names.controller])
    if (!(await absent(api, `${base}/workers/scripts/${name}/settings`)))
      throw new Error(
        "Temporary Worker name already exists; no overwrite permitted",
      );
  for (const name of [p.names.suite, p.names.probes, p.names.observer])
    if (!(await absent(api, `${base}/workflows/${name}`)))
      throw new Error(
        "Temporary Workflow name already exists; no overwrite permitted",
      );
  if ((await ownedNamespaces(api, p.names.provider)).length)
    throw new Error("Temporary namespace already exists; no reuse permitted");
  const state = executionState(p);
  let output, localAcceptance, runFailure;
  try {
    state.providerAttempted = true;
    await save({ state });
    await deploy("provider");
    state.controllerAttempted = true;
    await save({ state });
    await deploy("controller");
    const path = `${base}/workflows/${p.names.suite}/instances`;
    // One create attempt. A lost reply is reconciled by exact ID, never resubmitted.
    try {
      await api.post(path, {
        body: {
          instance_id: `${p.params.suiteId}-controller`,
          params: p.params,
        },
      });
    } catch (error) {
      if (await absent(api, `${path}/${p.params.suiteId}-controller`))
        throw error;
    }
    let readFailures = 0;
    for (let sample = 0; sample < 900 && now() < p.params.deadline; sample++) {
      let response;
      try {
        response = await api.get(`${path}/${p.params.suiteId}-controller`);
        readFailures = 0;
      } catch (error) {
        const transient =
          error?.status === 429 ||
          (error?.status >= 500 && error?.status <= 599) ||
          ["APIConnectionError", "APIConnectionTimeoutError"].includes(
            error?.name,
          );
        if (!transient || ++readFailures > 3) throw error;
        await sleep(2000);
        continue;
      }
      const result = response?.result;
      if (
        !result ||
        ![
          "queued",
          "running",
          "paused",
          "waiting",
          "waitingForPause",
          "complete",
          "errored",
          "terminated",
        ].includes(result.status)
      )
        throw new Error("Controller status schema unsupported");
      if (result.status === "complete") {
        const raw =
          typeof result.output === "string"
            ? result.output
            : JSON.stringify(result.output);
        if (typeof raw !== "string" || Buffer.byteLength(raw) > 1_048_576)
          throw new Error("Controller evidence missing or exceeds size bound");
        output =
          typeof result.output === "string"
            ? JSON.parse(result.output)
            : result.output;
        break;
      }
      if (["errored", "terminated"].includes(result.status))
        throw new Error("Canary terminated without supported acceptance");
      await sleep(2000);
    }
    if (output === undefined)
      throw new Error("Canary deadline or status budget exhausted");
    await save({ state, output }); // Preserve bounded raw evidence before auditing/deletion.
    localAcceptance = auditOutput(p, output);
    await save({ state, output, localAcceptance });
  } catch (error) {
    runFailure = {
      name: error?.name ?? "Error",
      status: error?.status ?? null,
    };
  } finally {
    const stopped = await cleanup(api, p, state, deploy);
    await save({
      state,
      output,
      localAcceptance,
      runFailure,
      cleanup: stopped,
    });
    if (runFailure || !stopped.confirmed)
      throw new Error(
        "Canary execution/cleanup incomplete; retain its local receipt and report blockers",
      );
  }
  return output;
}
async function client() {
  const [{ default: Cloudflare }, { EnvHttpProxyAgent, fetch }] =
    await Promise.all([import("cloudflare"), import("undici")]);
  const dispatcher = new EnvHttpProxyAgent();
  // The token is used only for supported bearer authentication, never inspected/logged/saved.
  const api = new Cloudflare({
    apiToken: process.env.CLOUDFLARE_API_TOKEN,
    apiKey: null,
    apiEmail: null,
    maxRetries: 0,
    timeout: 15_000,
    fetch: (input, init) => fetch(input, { ...init, dispatcher }),
  });
  return { api, close: () => dispatcher.close() };
}
export async function main(args) {
  const [
    command = "plan",
    suite = "ct-native-canary-01",
    directory = "../../.local/managed-canary",
    approvalPath,
  ] = args;
  if (!["plan", "package", "execute", "cleanup"].includes(command))
    throw new Error("Expected plan, package, execute, or cleanup");
  const dir = resolve(root, directory);
  if (command === "plan" || command === "package") {
    try {
      await readFile(join(dir, "execution.json"));
      throw new Error(
        "Execution receipt exists; preserve its original plan for cleanup",
      );
    } catch (error) {
      if (error?.code !== "ENOENT") throw error;
    }
    const p = plan(suite, await buildDigest());
    await materialize(p, dir);
    if (command === "package")
      for (const name of ["provider", "controller", "retire"]) {
        const output = wrangler([
          "deploy",
          "--dry-run",
          "--config",
          join(dir, `${name}.json`),
          "--outdir",
          join(dir, `${name}-bundle`),
        ]);
        await writeFile(join(dir, `${name}-package.txt`), output);
      }
    console.log(
      JSON.stringify({
        plan: join(dir, "plan.json"),
        resources: p.resources,
        names: p.names,
        billingGate: p.billingGate,
        hostProcessLoss: p.hostProcessLoss,
      }),
    );
    return;
  }
  const p = JSON.parse(await readFile(join(dir, "plan.json"), "utf8"));
  const expected = plan(
    suite,
    await buildDigest(),
    p.params.deadline - 1_800_000,
  );
  if (JSON.stringify(p) !== JSON.stringify(expected))
    throw new Error("Plan differs from current source or fixed scope");
  const approval =
    command === "execute" && approvalPath
      ? JSON.parse(await readFile(resolve(approvalPath), "utf8"))
      : undefined;
  let receipt;
  if (command === "execute") requireApproval(p, approval);
  else {
    receipt = JSON.parse(await readFile(join(dir, "execution.json"), "utf8"));
    requireCleanupReceipt(p, receipt);
  }
  await materialize(p, dir); // Recreate configs from the validated scope before any mutation.
  const deploy = (name) =>
    wrangler(["deploy", "--config", join(dir, `${name}.json`)]);
  const save = (data) =>
    writeFile(
      join(dir, "execution.json"),
      JSON.stringify(data, null, 2) + "\n",
    );
  const { api, close } = await client();
  try {
    if (command === "execute")
      console.log(
        JSON.stringify(
          await execute(api, p, approval, {
            deploy,
            save,
            claim: () => claimExecution(p, dir),
          }),
        ),
      );
    else {
      const result = await cleanup(api, p, receipt.state, deploy);
      await save({ ...receipt, cleanup: result });
      console.log(JSON.stringify(result));
    }
  } finally {
    await close();
  }
}
if (
  process.argv[1] &&
  import.meta.url === pathToFileURL(resolve(process.argv[1])).href
)
  main(process.argv.slice(2)).catch((error) => {
    console.error(
      `Canary controller stopped: ${error?.name ?? "Error"}: ${error?.status ?? error?.message ?? "unknown failure"}`,
    );
    process.exitCode = 1;
  });
