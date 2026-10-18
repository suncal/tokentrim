// tokentrim Pro cloud: the hosted real-time dashboard.
//
// Your Mac (`tokentrim live --cloud`) pushes a totals-only snapshot with your Pro license key; any browser
// opens it with a view link (or by signing in with the key). Each license gets its own Durable Object,
// keyed by a hash of the key, so the key itself is never stored. Licenses are checked against Polar and
// re-checked every 6 hours; if Polar is unreachable a good license keeps working for 3 days.
import html from "./live.html";

const RECHECK = 6 * 3600e3, GRACE = 3 * 86400e3, KEEP = 30 * 86400e3, WATCH = 120e3;
const MAX = 512 * 1024;

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
  return h.startsWith("Bearer ") ? h.slice(7).trim() : "";
}

function same(a, b) {   // constant time
  if (!a || !b || a.length !== b.length) return false;
  let d = 0;
  for (let i = 0; i < a.length; i++) d |= a.charCodeAt(i) ^ b.charCodeAt(i);
  return d === 0;
}

async function tenantOf(key) {
  const h = await crypto.subtle.digest("SHA-256", new TextEncoder().encode("tokentrim:" + key));
  return [...new Uint8Array(h)].slice(0, 16).map(b => b.toString(16).padStart(2, "0")).join("");
}

function token() {
  const b = crypto.getRandomValues(new Uint8Array(24));
  return btoa(String.fromCharCode(...b)).replace(/\+/g, "-").replace(/\//g, "_").replace(/=+$/, "");
}

export class Tenant {
  constructor(state, env) {
    this.state = state; this.env = env;
    this.snap = null; this.pushedAt = 0; this.savedAt = 0; this.lastRead = 0; this.loaded = false;
  }

  async load() {
    if (this.loaded) return;
    const s = await this.state.storage.get(["snap", "pushedAt"]);
    this.snap = s.get("snap") || null; this.pushedAt = s.get("pushedAt") || 0; this.loaded = true;
  }

  // -> {ok, plan, error}
  async license(key) {
    const comp = (this.env.COMP_KEYS || "").split(",").map(s => s.trim()).filter(Boolean);
    if (comp.some(c => same(c, key))) return { ok: true, plan: "comp" };
    const now = Date.now();
    const lic = (await this.state.storage.get("license")) || {};
    if (lic.status === "granted" && now - lic.checked < RECHECK) return { ok: true, plan: "pro" };
    if (lic.status && lic.status !== "granted" && now - lic.checked < 10 * 60e3)  // don't hammer Polar with a bad key
      return { ok: false, error: lic.status };
    let r, j = {};
    try {
      r = await fetch("https://api.polar.sh/v1/customer-portal/license-keys/validate", {
        method: "POST", headers: { "content-type": "application/json", "accept": "application/json" },
        body: JSON.stringify({ key, organization_id: this.env.POLAR_ORG }) });
      j = await r.json().catch(() => ({}));
    } catch (e) { r = null; }
    if (!r || r.status >= 500) {   // Polar down: trust a recent good check
      return lic.status === "granted" && now - lic.checked < GRACE ? { ok: true, plan: "pro" } : { ok: false, error: "license check unavailable" };
    }
    const live = r.ok && j.status === "granted" && j.organization_id === this.env.POLAR_ORG &&
                 (!j.expires_at || Date.parse(j.expires_at) > now);
    const status = live ? "granted" : (j.status || (r.status === 404 ? "unknown key" : "not granted"));
    await this.state.storage.put("license", { status, checked: now });
    return live ? { ok: true, plan: "pro" } : { ok: false, error: status };
  }

  async fetch(req) {
    const op = new URL(req.url).pathname;
    const now = Date.now();
    if (op === "/live" || op === "/settings") {
      const view = (await this.state.storage.get("view")) || "";
      if (!same(view, req.headers.get("x-view") || "")) return json({ error: "this link was replaced; sign in again" }, 401);
      if (op === "/settings") return queueSetting(this.state.storage, await req.text());
      const lic = (await this.state.storage.get("license")) || {};
      if (lic.status && lic.status !== "granted" && !lic.comp) return json({ error: "Pro is not active for this license (" + lic.status + ")", buy: this.env.BUY_URL }, 402);
      await this.load();
      this.lastRead = now;
      if (!this.snap) return new Response(null, { status: 204, headers: { "cache-control": "no-store" } });
      const results = (await this.state.storage.get("results")) || [];
      const pending = ((await this.state.storage.get("cmds")) || []).map(c => c.id);
      return json(this.snap, 200, { "x-pushed-at": String(this.pushedAt), "x-settings": JSON.stringify({ results, pending }) });
    }

    const key = req.headers.get("x-key") || "";
    const lic = await this.license(key);
    if (!lic.ok) return json({ error: lic.error, buy: this.env.BUY_URL }, 402);
    if (lic.plan === "comp") await this.state.storage.put("license", { status: "granted", checked: now, comp: true });

    if (op === "/link" || op === "/rotate") {
      let view = await this.state.storage.get("view");
      if (!view || op === "/rotate") { view = token(); await this.state.storage.put("view", view); }
      return json({ view: req.headers.get("x-tenant") + "." + view, plan: lic.plan });
    }
    if (op === "/push") {
      await this.load();
      if (now - this.pushedAt < 1500) return json({ error: "too fast" }, 429);
      const body = await req.text();
      if (body.length > MAX) return json({ error: "too big" }, 413);
      this.snap = body; this.pushedAt = now;
      if (now - this.savedAt > 15e3) {   // memory first; storage at most every 15 s
        this.savedAt = now;
        await this.state.storage.put({ snap: body, pushedAt: now });
        await this.state.storage.setAlarm(now + KEEP);
      }
      const settings = await takeAcks(this.state.storage, req.headers.get("x-ack"));
      return json({ ok: true, watching: now - this.lastRead < WATCH, plan: lic.plan, settings });
    }
    return json({ error: "not found" }, 404);
  }

  async alarm() {   // nothing pushed for 30 days: forget everything
    await this.state.storage.deleteAll();
    this.snap = null; this.pushedAt = 0;
  }
}

const PAGE_HEADERS = { "content-type": "text/html; charset=utf-8", "cache-control": "no-store", "x-robots-tag": "noindex",
  "referrer-policy": "no-referrer", "x-frame-options": "DENY", "x-content-type-options": "nosniff" };

export default {
  async fetch(req, env) {
    const url = new URL(req.url);
    const p = url.pathname;
    if (p === "/" || p === "/index.html")
      return new Response(html.replace("<!--TT_MODE-->",
        `<script>window.TT_MODE="cloud";window.TT_BUY=${JSON.stringify(env.BUY_URL)};</script>`), { headers: PAGE_HEADERS });
    if (p === "/api/health") return json({ ok: true });

    if ((p === "/api/live" && req.method === "GET") || (p === "/api/settings" && req.method === "POST")) {
      const [tenant, view] = bearer(req).split(".");
      if (!/^[0-9a-f]{32}$/.test(tenant || "") || !view) return json({ error: "sign in with your Pro license key" }, 401);
      const body = p === "/api/settings" ? (await req.text()).slice(0, 4096) : null;
      return env.TENANT.get(env.TENANT.idFromName(tenant)).fetch(new Request("https://t" + p.slice(4),
        { method: body === null ? "GET" : "POST", body, headers: { "x-view": view } }));
    }

    if (["/api/push", "/api/link", "/api/rotate"].includes(p) && req.method === "POST") {
      const key = bearer(req);
      if (key.length < 8 || key.length > 200) return json({ error: "missing license key" }, 401);
      const tenant = await tenantOf(key);
      const body = p === "/api/push" ? await req.text() : "";
      if (body.length > MAX) return json({ error: "too big" }, 413);
      return env.TENANT.get(env.TENANT.idFromName(tenant)).fetch(new Request("https://t" + p.slice(4), {
        method: "POST", body: body || null, headers: { "x-key": key, "x-tenant": tenant, "x-ack": req.headers.get("x-ack") || "" } }));
    }
    return new Response("not found", { status: 404 });
  },
};
