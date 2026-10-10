import { isRecord, type Ledger, type RunSpec } from "../shared/contracts.ts";

export const MODES = [
  "control",
  "timeout-retry",
  "sleep",
  "event",
  "event-timeout",
  "cancel",
] as const;
export type Mode = (typeof MODES)[number];
export interface Envelope {
  version: 1;
  suiteId: string;
  deadline: number;
  buildDigest: string;
}
export interface CanaryEnv {
  STATE: DurableObjectNamespace;
  CANARY_SUITE: string;
  CANARY_DEADLINE: string;
  CANARY_BUILD: string;
}
export interface Observation {
  mode: Mode;
  event: string;
  count: number;
  at: number;
}
export const runId = (suite: string, mode: Mode) => `${suite}-${mode}`;
export function envelope(env: CanaryEnv): Envelope {
  return {
    version: 1,
    suiteId: env.CANARY_SUITE,
    deadline: Number(env.CANARY_DEADLINE),
    buildDigest: env.CANARY_BUILD,
  };
}
export function validate(
  raw: unknown,
  env: CanaryEnv,
  allowExpired = false,
): Envelope {
  const wanted = envelope(env);
  if (
    !isRecord(raw) ||
    raw.version !== 1 ||
    raw.suiteId !== wanted.suiteId ||
    raw.deadline !== wanted.deadline ||
    raw.buildDigest !== wanted.buildDigest ||
    !/^ct-[a-z0-9-]{1,35}$/.test(wanted.suiteId) ||
    !/^[a-f0-9]{64}$/.test(wanted.buildDigest) ||
    !Number.isSafeInteger(wanted.deadline) ||
    (!allowExpired &&
      (wanted.deadline <= Date.now() ||
        wanted.deadline > Date.now() + 1_800_000))
  ) {
    throw new Error("Canary configuration or lifetime fence rejected");
  }
  return wanted;
}
export function mode(raw: unknown): Mode {
  if (!MODES.includes(raw as Mode))
    throw new Error("Unsupported synthetic mode");
  return raw as Mode;
}
export const initial = (env: CanaryEnv, base: Ledger): Ledger => ({
  ...base,
  grant: {
    limit: 60,
    maxRuns: 6,
    concurrency: 6,
    consumed: 0,
    revoked: false,
    expires: Number(env.CANARY_DEADLINE),
  },
});
export const spec = (payload: Envelope, probe: Mode): RunSpec => ({
  schemaVersion: 1,
  definitionVersion: 1,
  runId: runId(payload.suiteId, probe),
  commandId: `cmd-${probe}`,
  idempotencyKey: `key-${probe}`,
  principal: "synthetic-operator",
  grantId: "synthetic-grant",
  preset: "fake-v1",
  deployment: "fake-deployment-v1",
  sourceDigest: payload.buildDigest,
  inputDigest: payload.buildDigest,
  engine: "native-cf",
  reservation: 10,
  deadline: payload.deadline,
});
export async function hash(value: string): Promise<string> {
  const bytes = await crypto.subtle.digest(
    "SHA-256",
    new TextEncoder().encode(value),
  );
  return Array.from(new Uint8Array(bytes), (b) =>
    b.toString(16).padStart(2, "0"),
  ).join("");
}
export async function post<T>(
  binding: Fetcher,
  path: string,
  payload: Envelope,
  data: object = {},
): Promise<T> {
  const response = await binding.fetch(`https://synthetic.invalid${path}`, {
    method: "POST",
    body: JSON.stringify({ ...data, ...payload }),
  });
  if (!response.ok)
    throw new Error(`Synthetic service ${path} rejected (${response.status})`);
  return (await response.json()) as T;
}

export const canonical = (value: object): string =>
  JSON.stringify(
    Object.fromEntries(
      Object.entries(value).sort(([a], [b]) => a.localeCompare(b)),
    ),
  );
