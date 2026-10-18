// tokentrim live, remote view: your Mac pushes a totals-only snapshot, any browser with the key reads it.
// One Durable Object holds the latest snapshot. Deployed by `tokentrim live --deploy`.
import html from "./live.html";

const json = (body, status = 200, extra = {}) =>
  new Response(typeof body === "string" ? body : JSON.stringify(body), {
    status, headers: { "content-type": "application/json", "cache-control": "no-store", ...extra } });

// Settings changed in the browser wait here until the Mac's next push picks them up; the Mac applies only
// these keys (tokentrim/controls.py checks again) and acknowledges each one on the push after.
const SETTINGS = { enabled: "b", rule_screenshots: "b", rule_images: "b", rule_outline: "b", rule_bash: "b", rule_mcp: "b",
  mcp_lossless_only: "b", rule_meter: "b", guidance: "b", profile: ["balanced", "strong", "max"],
  model_mode: ["off", "auto", "sonnet"], subagent_model: ["off", "sonnet", "haiku"], effort: ["off", "medium", "low"],
  cache_ttl: ["off", "1h"], budget_usd: [5, 10, 20, 30, 50, 100, 200],
  budget_guard: "b", budget_block: "b", cache_guard: "b", model_advisor: "b" };
function cleanChanges(c) {
  if (!c || typeof c !== "object" || Array.isArray(c)) return null;
  const out = {};
  for (const [k, v] of Object.entries(c)) {
    const t = SETTINGS[k];
    if (t === "b" && typeof v === "boolean") out[k] = v;
    else if (Array.isArray(t) && t.includes(v)) out[k] = v;
    else return null;
  }
  return Object.keys(out).length ? out : null;
}
async function queueSetting(storage, body) {
  let j; try { j = JSON.parse(body); } catch (e) { return json({ error: "bad json" }, 400); }
  const changes = cleanChanges(j && j.changes);
  if (!changes) return json({ error: "unknown setting" }, 400);
  const cmds = ((await storage.get("cmds")) || []).filter(c => Date.now() - c.at < 600e3).slice(-9);
  const id = crypto.randomUUID().slice(0, 8);
  cmds.push({ id, changes, at: Date.now() });
  await storage.put("cmds", cmds);
  return json({ id, queued: true });
}
async function takeAcks(storage, header) {   // -> pending commands for the Mac
  let acks = []; try { acks = JSON.parse(header || "[]"); } catch (e) {}
  let cmds = ((await storage.get("cmds")) || []).filter(c => Date.now() - c.at < 600e3);
  if (Array.isArray(acks) && acks.length) {
    const done = new Set(acks.map(a => a && a.id));
    const results = ((await storage.get("results")) || []).concat(acks.filter(a => a && a.id).map(a =>
      ({ id: String(a.id).slice(0, 16), ok: !!a.ok, error: String(a.error || "").slice(0, 160), at: Date.now() }))).slice(-20);
    cmds = cmds.filter(c => !done.has(c.id));
    await storage.put({ cmds, results });
  }
  return cmds.map(c => ({ id: c.id, changes: c.changes }));
}

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
  constructor(state) { this.state = state; this.snap = null; this.pushedAt = 0; this.savedAt = 0; this.lastRead = 0; }
  async fetch(req) {
    const now = Date.now();
    const op = new URL(req.url).pathname;
    if (op === "/settings") return queueSetting(this.state.storage, await req.text());
    if (req.method === "POST") {
      this.snap = await req.text();
      this.pushedAt = now;
      if (now - this.savedAt > 15e3) {   // memory first; storage at most every 15 s
        this.savedAt = now;
        await this.state.storage.put({ snap: this.snap, pushedAt: now });
      }
      const settings = await takeAcks(this.state.storage, req.headers.get("x-ack"));
      return json({ ok: true, watching: now - this.lastRead < 120e3, settings });
    }
    if (this.snap === null) {
      const s = await this.state.storage.get(["snap", "pushedAt"]);
      this.snap = s.get("snap") || ""; this.pushedAt = s.get("pushedAt") || 0;
    }
    this.lastRead = now;
    if (!this.snap) return new Response(null, { status: 204 });
    const results = (await this.state.storage.get("results")) || [];
    const pending = ((await this.state.storage.get("cmds")) || []).map(c => c.id);
    return json(this.snap, 200, { "x-pushed-at": String(this.pushedAt), "x-settings": JSON.stringify({ results, pending }) });
  }
}

// Bump when the Live class changes: an object that is pushed to every few seconds never goes idle, and
// Cloudflare can keep it on the old code after a deploy. A new name starts a fresh one; the Mac re-pushes within seconds.
const OBJECT = "me-2";

export default {
  async fetch(req, env) {
    const url = new URL(req.url);
    const stub = env.LIVE.get(env.LIVE.idFromName(OBJECT));
    if (url.pathname === "/" || url.pathname === "/index.html")
      return new Response(html.replace("<!--TT_MODE-->", '<script>window.TT_MODE="remote";</script>'), { headers: { "content-type": "text/html; charset=utf-8", "cache-control": "no-store",
        "x-robots-tag": "noindex", "referrer-policy": "no-referrer", "x-frame-options": "DENY" } });
    if (url.pathname === "/live.json" && req.method === "GET") {
      if (!same(bearer(req), env.READ_KEY)) return json({ error: "unauthorized" }, 401);
      return stub.fetch(req);
    }
    if (url.pathname === "/push" && req.method === "POST") {
      if (!same(bearer(req), env.WRITE_KEY)) return json({ error: "unauthorized" }, 401);
      const body = await req.text();
      if (body.length > 512 * 1024) return json({ error: "too big" }, 413);
      return stub.fetch(new Request(req.url, { method: "POST", body, headers: { "x-ack": req.headers.get("x-ack") || "" } }));
    }
    if (url.pathname === "/settings" && req.method === "POST") {   // same key as reading: whoever can see the page can tune it
      if (!same(bearer(req), env.READ_KEY)) return json({ error: "unauthorized" }, 401);
      return stub.fetch(new Request(req.url, { method: "POST", body: (await req.text()).slice(0, 4096) }));
    }
    return new Response("not found", { status: 404 });
  },
};
