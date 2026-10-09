import { DurableObject } from "cloudflare:workers";
import { isRecord, jsonResponse, type ProviderCall } from "./contracts.ts";

/** Synthetic provider. Run in a separate Miniflare instance with separate SQLite persistence. */
export class FakeProvider extends DurableObject {
  async fetch(request: Request): Promise<Response> {
    const url = new URL(request.url);
    const sql = this.ctx.storage.sql;
    sql.exec("CREATE TABLE IF NOT EXISTS records (kind TEXT, id TEXT, value TEXT NOT NULL, PRIMARY KEY(kind,id))");
    const read = <T>(kind: string, id: string): T | undefined => {
      const row = sql.exec<{ value: string }>("SELECT value FROM records WHERE kind=? AND id=?", kind, id).toArray()[0];
      return row ? JSON.parse(row.value) as T : undefined;
    };
    const put = (kind: string, id: string, value: unknown): void => {
      sql.exec("INSERT OR REPLACE INTO records(kind,id,value) VALUES(?,?,?)", kind, id, JSON.stringify(value));
    };
    if (url.pathname === "/observe") return jsonResponse(sql.exec<{kind: string; id: string; value: string}>("SELECT * FROM records ORDER BY kind,id").toArray().map(row => ({ kind: row.kind, id: row.id, value: JSON.parse(row.value) as unknown })));
    if (request.method !== "POST") return jsonResponse({ error: "synthetic POST required" }, 405);
    const body: unknown = await request.json();
    if (!isRecord(body)) return jsonResponse({ error: "invalid envelope" }, 400);
    if (url.pathname === "/record") {
      if (typeof body.id !== "string" || typeof body.event !== "string") return jsonResponse({error:"invalid observer event"},400);
      const previous = read<number>("observer", `${body.id}-${body.event}`) ?? 0;
      put("observer", `${body.id}-${body.event}`, previous + 1);
      return jsonResponse({ count: previous + 1 });
    }
    if (url.pathname === "/disable") {
      if (typeof body.generation !== "string") return jsonResponse({error:"invalid generation"},400);
      put("disabled", body.generation, true); return jsonResponse({ disabled: true });
    }
    if (url.pathname === "/spawn") {
      if (typeof body.runId !== "string" || typeof body.generation !== "string") return jsonResponse({error:"invalid launch"},400);
      if (read("disabled", body.generation)) return jsonResponse({error:"generation fenced"},409);
      const id = `call-${crypto.randomUUID()}`;
      const call: ProviderCall = { id, runId: body.runId, generation: body.generation, state: "queued", cost: 1 };
      put("call", id, call);
      // An ambiguous acknowledgement is injected after the durable external side effect.
      return body.loseReply ? jsonResponse({error:"injected lost acknowledgement"},503) : jsonResponse(call);
    }
    if (url.pathname === "/lookup") {
      return jsonResponse(sql.exec<{value:string}>("SELECT value FROM records WHERE kind='call'").toArray().map(row => JSON.parse(row.value) as ProviderCall).filter(call => call.runId === body.runId));
    }
    if (url.pathname === "/finish" || url.pathname === "/cancel") {
      if (typeof body.callId !== "string") return jsonResponse({error:"invalid call"},400);
      const call = read<ProviderCall>("call", body.callId);
      if (!call) return jsonResponse({error:"unknown call"},404);
      call.state = url.pathname === "/cancel" ? "cancelled" : "completed";
      put("call", call.id, call); return jsonResponse(call);
    }
    return jsonResponse({error:"unknown synthetic endpoint"},404);
  }
}

export default {
  fetch(request: Request, env: { PROVIDER: DurableObjectNamespace }): Promise<Response> {
    return env.PROVIDER.get(env.PROVIDER.idFromName("independent-provider")).fetch(request);
  },
};
