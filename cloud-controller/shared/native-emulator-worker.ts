import { WorkflowEntrypoint, type WorkflowEvent, type WorkflowStep } from "cloudflare:workers";

/** Plain native control: deliberately no Effect or effect-cf dependency. */
export class RawNativeWorkflow extends WorkflowEntrypoint<{ OBSERVER: Fetcher }, { id: string }> {
  async run(event: WorkflowEvent<{ id: string }>, step: WorkflowStep) {
    const id = event.payload.id;
    const record = async (name: string) => {
      const response = await this.env.OBSERVER.fetch("http://observer/record", {
        method: "POST",
        body: JSON.stringify({ id, event: name }),
      });
      if (!response.ok) throw new Error(`observer ${response.status}`);
      return { id, event: name };
    };
    await step.do("raw-before", () => record("before"));
    await step.sleep("raw-sleep", "3 seconds");
    await step.do("raw-after", () => record("complete"));
    return { id };
  }
}

export default {
  async fetch(request: Request, env: { RAW: Workflow }) {
    if (new URL(request.url).pathname !== "/create" || request.method !== "POST")
      return Response.json({ error: "local synthetic create only" }, { status: 404 });
    const { id } = (await request.json()) as { id: string };
    if (!/^raw-[a-z-]+$/.test(id))
      return Response.json({ error: "invalid synthetic ID" }, { status: 400 });
    const instance = await env.RAW.create({ id, params: { id } });
    return Response.json({ id: instance.id });
  },
};
