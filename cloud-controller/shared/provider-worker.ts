import { DurableObject } from "cloudflare:workers";
import { isRecord, jsonResponse, type FakeArtifact, type ProviderCall } from "./contracts.ts";

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
    if (url.pathname === "/observe") return jsonResponse(sql.exec<{kind: string; id: string; value: string}>("SELECT * FROM records ORDER BY kind,id").toArray().map(row => {
      const value = JSON.parse(row.value) as unknown;
      if (row.kind !== "observer") return { kind: row.kind, id: row.id, value };
      const [subjectId, event] = JSON.parse(row.id) as [string, string];
      return { kind: row.kind, id: `${subjectId}-${event}`, subjectId, event, value };
    }));
    if (request.method !== "POST") return jsonResponse({ error: "synthetic POST required" }, 405);
    const body: unknown = await request.json();
    if (!isRecord(body)) return jsonResponse({ error: "invalid envelope" }, 400);
    if (url.pathname === "/artifact/put" || url.pathname === "/artifact/read") {
      if (typeof body.key !== "string" || !/^runs\/[a-zA-Z0-9_-]{1,80}\/[a-f0-9]{64}\.json$/.test(body.key)) return jsonResponse({error:"invalid artifact key"},400);
      const previous = read<FakeArtifact>("artifact", body.key);
      if (url.pathname === "/artifact/read") return previous ? jsonResponse(previous) : jsonResponse({error:"missing artifact"},404);
      if (typeof body.content !== "string" || typeof body.digest !== "string") return jsonResponse({error:"invalid content"},400);
      const bytes = new TextEncoder().encode(body.content);
      if (bytes.byteLength < 1 || bytes.byteLength > 4096) return jsonResponse({error:"artifact size cap"},400);
      const digest = Array.from(new Uint8Array(await crypto.subtle.digest("SHA-256", bytes))).map(byte => byte.toString(16).padStart(2,"0")).join("");
      if (digest !== body.digest || !body.key.endsWith(`/${digest}.json`)) return jsonResponse({error:"content digest mismatch"},409);
      if (previous) return previous.digest === digest && previous.content === body.content ? jsonResponse(previous) : jsonResponse({error:"immutable artifact conflict"},409);
      const artifact: FakeArtifact = {key:body.key,digest,content:body.content,bytes:bytes.byteLength};
      // Awaiting hashing yields the actor: re-read before conditional insertion.
      const raced = read<FakeArtifact>("artifact", body.key);
      if (raced) return raced.digest === digest && raced.content === body.content ? jsonResponse(raced) : jsonResponse({error:"immutable artifact conflict"},409);
      sql.exec("INSERT INTO records(kind,id,value) VALUES('artifact',?,?)",body.key,JSON.stringify(artifact));
      return jsonResponse(artifact);
    }
    if (url.pathname === "/record") {
      if (typeof body.id !== "string" || typeof body.event !== "string") return jsonResponse({error:"invalid observer event"},400);
      const key = JSON.stringify([body.id, body.event]);
      const previous = read<number>("observer", key) ?? 0;
      put("observer", key, previous + 1);
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
