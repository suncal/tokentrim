// tokentrim live, remote view: your Mac pushes a totals-only snapshot, any browser with the key reads it.
// One Durable Object holds the latest snapshot. Deployed by `tokentrim live --deploy`.
import html from "./live.html";

const json = (body, status = 200) =>
  new Response(typeof body === "string" ? body : JSON.stringify(body), {
    status, headers: { "content-type": "application/json", "cache-control": "no-store" } });

function bearer(req) {
  const h = req.headers.get("authorization") || "";
  return h.startsWith("Bearer ") ? h.slice(7) : "";
}

// constant-time compare so the key can't be guessed byte by byte
function same(a, b) {
  if (!a || !b || a.length !== b.length) return false;
  let d = 0;
  for (let i = 0; i < a.length; i++) d |= a.charCodeAt(i) ^ b.charCodeAt(i);
  return d === 0;
}

export class Live {
  constructor(state) { this.state = state; this.snap = null; }
  async fetch(req) {
    if (req.method === "POST") {
      this.snap = await req.text();
      await this.state.storage.put("snap", this.snap);
      return json({ ok: true });
    }
    if (this.snap === null) this.snap = (await this.state.storage.get("snap")) || "";
    return this.snap ? json(this.snap) : new Response(null, { status: 204 });
  }
}

export default {
  async fetch(req, env) {
    const url = new URL(req.url);
    const stub = env.LIVE.get(env.LIVE.idFromName("me"));
    if (url.pathname === "/" || url.pathname === "/index.html")
      return new Response(html, { headers: { "content-type": "text/html; charset=utf-8", "cache-control": "no-store",
        "x-robots-tag": "noindex", "referrer-policy": "no-referrer", "x-frame-options": "DENY" } });
    if (url.pathname === "/live.json" && req.method === "GET") {
      if (!same(bearer(req), env.READ_KEY)) return json({ error: "unauthorized" }, 401);
      return stub.fetch(req);
    }
    if (url.pathname === "/push" && req.method === "POST") {
      if (!same(bearer(req), env.WRITE_KEY)) return json({ error: "unauthorized" }, 401);
      const body = await req.text();
      if (body.length > 512 * 1024) return json({ error: "too big" }, 413);
      return stub.fetch(new Request(req.url, { method: "POST", body }));
    }
    return new Response("not found", { status: 404 });
  },
};
