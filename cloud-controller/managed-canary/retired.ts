/** Temporary namespace deletion migration only; never handles application requests. */
export default {
  fetch: () => new Response("Temporary canary retired", { status: 410 }),
};
