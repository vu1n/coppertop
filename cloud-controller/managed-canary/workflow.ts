import * as Context from "effect/Context";
import * as Effect from "effect/Effect";
import * as Layer from "effect/Layer";
import { Workflow, WorkerEnvironment } from "effect-cf";
import type { Decision, Ledger } from "../shared/contracts.ts";
import {
  MODES,
  validate,
  mode,
  runId,
  post,
  type CanaryEnv,
  type Envelope,
  type Observation,
} from "./contracts.ts";
import { acceptance, type IndependentSnapshot } from "./report.ts";

interface Env extends CanaryEnv {
  PROVIDER: Fetcher;
  PROBES: globalThis.Workflow;
  OBSERVER: globalThis.Workflow;
}
declare global {
  namespace Cloudflare {
    interface Env extends Record<string, unknown> {
      STATE: DurableObjectNamespace;
      PROVIDER: Fetcher;
      PROBES: globalThis.Workflow;
      OBSERVER: globalThis.Workflow;
      CANARY_SUITE: string;
      CANARY_DEADLINE: string;
      CANARY_BUILD: string;
    }
  }
}
class SyntheticClient extends Context.Service<
  SyntheticClient,
  { call: <T>(path: string, payload: Envelope, data?: object) => Promise<T> }
>()("coppertop/SyntheticClient") {}
const services = Layer.effect(
  SyntheticClient,
  Effect.map(WorkerEnvironment, (env) => ({
    call: <T>(path: string, payload: Envelope, data: object = {}) =>
      post<T>(env.PROVIDER, path, payload, data),
  })),
);
const options = {
  retries: {
    limit: 1,
    delay: "1 second" as const,
    backoff: "constant" as const,
  },
  timeout: "10 seconds" as const,
};
const checked = (raw: unknown, env: CanaryEnv) =>
  Effect.try({
    try: () => validate(raw, env),
    catch: () =>
      new Workflow.WorkflowStepNonRetryableError({
        cause: "canary identity/lifetime rejected",
      }),
  });

export const CanaryProbe = Workflow.make(services, {
  run: (raw: unknown) =>
    Effect.gen(function* () {
      const env = yield* WorkerEnvironment;
      const payload = yield* checked(raw, env);
      const probe = mode((raw as Envelope & { mode: unknown }).mode);
      const event = yield* Workflow.WorkflowEvent;
      if (event.instanceId !== runId(payload.suiteId, probe))
        return yield* Effect.fail(
          new Workflow.WorkflowStepNonRetryableError({
            cause: "fixed instance identity rejected",
          }),
        );
      const client = yield* SyntheticClient;
      const call = <T>(path: string) =>
        client.call<T>(path, payload, { mode: probe });
      // This injects a supported activity timeout. It does not kill a host process.
      yield* Workflow.step(
        "first-activity",
        Effect.tryPromise(async () => {
          const attempt = await client.call<Observation>(
            "/provider/record",
            payload,
            { mode: probe, event: "first-attempt" },
          );
          if (probe === "timeout-retry" && attempt.count === 1)
            await new Promise<void>(() => {});
          return attempt.count;
        }),
        {
          retries: { limit: 2, delay: "1 second", backoff: "constant" },
          timeout: "1 second",
        },
      );
      yield* Workflow.step(
        "one-shot-launch-and-duplicate",
        Effect.tryPromise(async () => {
          await call("/ledger/dispatch");
          return await call("/ledger/dispatch");
        }),
        options,
      );
      yield* Workflow.step(
        "one-shot-body",
        Effect.tryPromise(async () => {
          const claim = await call<Decision>("/ledger/execute");
          if (claim.newPermission) {
            await client.call("/provider/record", payload, {
              mode: probe,
              event: "body",
            });
            if (probe !== "cancel") await call("/provider/finish");
          }
          return claim.newPermission ?? false;
        }),
        options,
      );
      if (probe === "sleep") {
        yield* Workflow.step(
          "observe-before-sleep",
          Effect.tryPromise(() =>
            client.call("/provider/record", payload, {
              mode: probe,
              event: "sleep-start",
            }),
          ),
          options,
        );
        yield* Workflow.sleep("durable-sleep", "10 seconds");
        yield* Workflow.step(
          "observe-after-sleep",
          Effect.tryPromise(() =>
            client.call("/provider/record", payload, {
              mode: probe,
              event: "sleep-complete",
            }),
          ),
          options,
        );
      }
      if (["event", "event-timeout", "cancel"].includes(probe)) {
        yield* Workflow.step(
          "observe-before-event",
          Effect.tryPromise(() =>
            client.call("/provider/record", payload, {
              mode: probe,
              event: "wait-start",
            }),
          ),
          options,
        );
        const received = yield* Workflow.waitForEvent<{ suiteId: string }>(
          "durable-event",
          {
            type: "canary-release",
            timeout: probe === "event-timeout" ? "3 seconds" : "20 seconds",
          },
        ).pipe(
          Effect.catch((error) =>
            probe === "event-timeout" &&
            /timeout|timed out/i.test(String(error.cause))
              ? Effect.succeed(null)
              : Effect.fail(error),
          ),
        );
        if (probe === "event" && received?.payload.suiteId !== payload.suiteId)
          return yield* Effect.fail(
            new Workflow.WorkflowStepNonRetryableError({
              cause: "synthetic event identity mismatch",
            }),
          );
        if (probe === "cancel")
          return yield* Effect.fail(
            new Workflow.WorkflowStepNonRetryableError({
              cause: "cancellation not delivered within its bound",
            }),
          );
        if (probe === "event-timeout" && received !== null)
          return yield* Effect.fail(
            new Workflow.WorkflowStepNonRetryableError({
              cause: "unexpected event invalidated timeout gate",
            }),
          );
        yield* Workflow.step(
          "observe-event-result",
          Effect.tryPromise(() =>
            client.call("/provider/record", payload, {
              mode: probe,
              event: probe === "event" ? "event-received" : "event-timeout",
            }),
          ),
          options,
        );
      }
      yield* Workflow.step(
        "verified-receipt-and-settlement",
        Effect.tryPromise(() => call("/ledger/complete")),
        options,
      );
      yield* Workflow.step(
        "independent-completion",
        Effect.tryPromise(() =>
          client.call("/provider/record", payload, {
            mode: probe,
            event: "probe-complete",
          }),
        ),
        options,
      );
      return { mode: probe, synthetic: true, hostProcessLoss: "untested" };
    }),
});
async function ensure(
  binding: globalThis.Workflow,
  id: string,
  params: object,
): Promise<void> {
  try {
    await binding.create({ id, params });
  } catch (cause) {
    const instance = await binding.get(id);
    if ((await instance.status()).status === "unknown") throw cause;
  }
}
export const CanaryController = Workflow.make(services, {
  run: (raw: unknown) =>
    Effect.gen(function* () {
      const env = (yield* WorkerEnvironment) as Env;
      const payload = yield* checked(raw, env);
      const event = yield* Workflow.WorkflowEvent;
      if (event.instanceId !== `${payload.suiteId}-controller`)
        return yield* Effect.fail(
          new Workflow.WorkflowStepNonRetryableError({
            cause: "controller instance identity rejected",
          }),
        );
      const client = yield* SyntheticClient;
      yield* Workflow.step(
        "admit-fixed-six-probes",
        Effect.tryPromise(async () => {
          for (const probe of MODES)
            await client.call("/ledger/admit", payload, { mode: probe });
          return { admitted: 6 };
        }),
        options,
      );
      yield* Workflow.step(
        "create-independent-observer",
        Effect.tryPromise(() =>
          ensure(env.OBSERVER, `${payload.suiteId}-observer`, payload),
        ),
        options,
      );
      yield* Workflow.step(
        "create-fixed-six-probes-and-duplicate",
        Effect.tryPromise(async () => {
          for (const probe of MODES)
            await ensure(env.PROBES, runId(payload.suiteId, probe), {
              ...payload,
              mode: probe,
            });
          await ensure(env.PROBES, runId(payload.suiteId, "control"), {
            ...payload,
            mode: "control",
          });
          return { uniqueProbeInstances: 6 };
        }),
        options,
      );
      let ready = false;
      for (let attempt = 0; attempt < 20; attempt++) {
        ready = yield* Workflow.step(
          `observe-event-cancel-readiness-${attempt}`,
          Effect.tryPromise(async () => {
            const snapshot = await client.call<IndependentSnapshot>(
              "/provider/snapshot",
              payload,
            );
            return ["event", "cancel"].every((probe) =>
              snapshot.observations.some(
                (row) => row.mode === probe && row.event === "wait-start",
              ),
            );
          }),
          options,
        );
        if (ready) break;
        yield* Workflow.sleep(`driver-interval-${attempt}`, "1 second");
      }
      if (!ready)
        return yield* Effect.fail(
          new Workflow.WorkflowStepNonRetryableError({
            cause:
              "supported event/cancel prerequisites not independently observed",
          }),
        );
      yield* Workflow.step(
        "supported-event-delivery",
        Effect.tryPromise(async () => {
          const instance = await env.PROBES.get(
            runId(payload.suiteId, "event"),
          );
          await instance.sendEvent({
            type: "canary-release",
            payload: { suiteId: payload.suiteId },
          });
          return { sent: true };
        }),
        options,
      );
      yield* Workflow.step(
        "independent-cancel-containment",
        Effect.tryPromise(async () => {
          const decision = await client.call<Decision>(
            "/ledger/cancel",
            payload,
            { mode: "cancel" },
          );
          if (decision.run?.cleanup !== "confirmed")
            throw new Error("Cancellation liability remains open");
          const instance = await env.PROBES.get(
            runId(payload.suiteId, "cancel"),
          );
          if ((await instance.status()).status !== "terminated")
            await instance.terminate();
          return await client.call("/provider/record", payload, {
            mode: "cancel",
            event: "cancel-observed",
          });
        }),
        options,
      );
      let independent: { snapshot: IndependentSnapshot } | undefined;
      for (let attempt = 0; attempt < 45; attempt++) {
        independent = yield* Workflow.step(
          `read-independent-report-${attempt}`,
          Effect.tryPromise(async () => {
            const instance = await env.OBSERVER.get(
              `${payload.suiteId}-observer`,
            );
            const status = await instance.status();
            if (status.status === "errored" || status.status === "terminated")
              throw new Error("Independent observer unavailable");
            return status.status === "complete"
              ? (status.output as { snapshot: IndependentSnapshot })
              : undefined;
          }),
          options,
        );
        if (independent) break;
        yield* Workflow.sleep(`report-interval-${attempt}`, "1 second");
      }
      if (!independent)
        return yield* Effect.fail(
          new Workflow.WorkflowStepNonRetryableError({
            cause: "independent observation window exhausted",
          }),
        );
      const snapshot = independent.snapshot;
      return yield* Workflow.step(
        "acceptance-and-independent-cleanup",
        Effect.tryPromise(async () => {
          const statuses: Record<string, string> = {};
          for (const probe of MODES)
            statuses[probe] = (
              await (
                await env.PROBES.get(runId(payload.suiteId, probe))
              ).status()
            ).status;
          const ledger = await client.call<Ledger>("/ledger/snapshot", payload);
          const report = acceptance(payload, snapshot, ledger, statuses);
          // Completion/output alone never implies receipt validity, accounting, or containment.
          await client.call("/provider/cleanup", payload);
          await client.call("/ledger/cleanup", payload);
          return {
            ...report,
            cleanup: "synthetic-contained",
            deploymentCleanup: "controller-must-delete-temporary-resources",
          };
        }),
        options,
      );
    }),
});
export default {
  fetch: () => new Response("Use authenticated Workflows API", { status: 404 }),
};
