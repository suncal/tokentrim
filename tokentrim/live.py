"""Real-time savings: `tokentrim serve` → http://localhost:4747/live

Tails today's transcripts and the hook's savings log every couple of seconds and
keeps, per conversation, the context Claude actually re-read on each step next to
the context it would have re-read without tokentrim:

  without = actual + every trimmed tool result still in the conversation
            + the history an earlier compaction removed (until Claude Code's own,
              much later compaction would have fired)

The difference, summed over steps, is the live saving. Same model as `replay`,
just running forward. Nothing leaves the machine unless you run `tokentrim live
--deploy`, and then only totals and project folder names (no prompts, no titles).
"""
import datetime as dt
import glob
import json
import os
import sys
import time
from collections import defaultdict, deque

from . import cli, config
from .audit import PROJECTS
from .share import PRICES, _fam

STATE = os.path.expanduser("~/.cache/tokentrim")
SAVINGS = os.path.join(STATE, "savings.jsonl")
ACTIVE = 20 * 60          # a conversation is "live" if it was written to in the last 20 min
TRIGGER = 0.75            # compaction fires at ~75% of CLAUDE_CODE_AUTO_COMPACT_WINDOW
SERIES = 160              # context points kept per conversation for its sparkline
RULES = {"screenshot": "Screenshots shrunk", "screenshot_dedupe": "Repeat screenshots skipped",
         "read_image": "Images downsized", "read_outline": "Big files outlined first",
         "bash_trim": "Long command output trimmed", "mcp_shrink": "Connector output shrunk"}


def _ts(s):
    try:
        return dt.datetime.fromisoformat(s.replace("Z", "+00:00")).timestamp()
    except (AttributeError, ValueError):
        return None


def _project(dirname):
    from .dashboard import _project as p
    return p(dirname)


def _midnight():
    return time.mktime(dt.date.today().timetuple())


def _windows():
    """(tokentrim's compaction point, where Claude Code would compact on its own)."""
    env = cli._load_settings().get("env") or {}
    try:
        ours = int(env.get("CLAUDE_CODE_AUTO_COMPACT_WINDOW") or 0)
    except ValueError:
        ours = 0
    cfg = config.load()
    native = int(cfg.get("native_compact_tokens", 850_000))
    return int(ours * TRIGGER) if ours else native, native


class Conv:
    """One conversation (its sub-agents fold into it)."""

    def __init__(self, sid, project):
        self.sid, self.project = sid, project
        self.title = ""
        self.steps = 0
        self.agents = set()
        self.ctx = self.cf = 0          # last step: actual / without-tokentrim context
        self.prev = None
        self.pending = 0                # trimmed tokens not yet carried into a step
        self.carried = 0                # trimmed tokens still sitting in the conversation
        self.saved = 0                  # tokens not re-read, summed over steps
        self.saved_today = 0
        self.compactions = 0
        self.last = 0.0
        self.model = "?"
        self.deltas = deque(maxlen=12)
        self.series = deque(maxlen=SERIES)

    def step(self, ts, ctx, native):
        if self.prev is None:
            self.cf = ctx
        elif ctx < self.prev * 0.6:      # compaction: the actual context dropped, without-tokentrim keeps it
            self.compactions += 1
            self.carried = 0
            delta = 0
        else:
            delta = ctx - self.prev
            self.deltas.append(delta)
        if self.prev is not None:
            self.cf += delta + self.pending
            self.carried += self.pending
        self.pending = 0
        if self.cf > native:             # Claude Code's own compaction would have fired by now
            self.cf = ctx + self.carried
        self.cf = max(self.cf, ctx)
        self.prev, self.ctx = ctx, ctx
        self.steps += 1
        self.last = max(self.last, ts)
        saved = self.cf - ctx
        self.saved += saved
        self.series.append((int(ctx), int(self.cf)))
        return saved


class Tracker:
    def __init__(self):
        self.files = {}                 # path -> [offset, sid, is_agent, seen message ids]
        self.convs = {}
        self.sav_off = 0
        self.ours, self.native = _windows()
        self.day = None
        self.version = 0
        self._reset_day()
        self.last_glob = 0.0

    def _reset_day(self):
        self.day = dt.date.today()
        from .dashboard import _install_ts
        self.start = max(_midnight(), _install_ts(cli._load_settings()) or 0)  # savings only count once installed
        self.today = defaultdict(float)  # used, would, saved, usd, trims, trimmed, steps
        self.minutes = defaultdict(lambda: [0, 0])   # minute -> [used, would]
        self.hours = defaultdict(lambda: [0, 0])
        self.rules = defaultdict(lambda: [0, 0])
        self.feed = deque(maxlen=60)
        for c in self.convs.values():
            c.saved_today = 0

    # ---------- reading

    def _discover(self):
        cutoff = self.start if not self.files else time.time() - ACTIVE
        pats = [os.path.join(PROJECTS, "*", "*.jsonl"), os.path.join(PROJECTS, "*", "*", "subagents", "*.jsonl")]
        for p in (q for pat in pats for q in glob.glob(pat)):
            if p in self.files:
                continue
            try:
                if os.path.getmtime(p) < cutoff:
                    continue
            except OSError:
                continue
            agent = "/subagents/" in p
            sid = os.path.basename(os.path.dirname(os.path.dirname(p))) if agent else os.path.basename(p)[:-6]
            self.files[p] = [0, sid, agent, set()]
            if sid not in self.convs:
                proj_dir = p.split(PROJECTS.rstrip("/") + "/", 1)[1].split("/", 1)[0]
                self.convs[sid] = Conv(sid, _project(proj_dir))
            if agent:
                self.convs[sid].agents.add(os.path.basename(p))

    def _read_savings(self):
        try:
            size = os.path.getsize(SAVINGS)
        except OSError:
            return
        if size < self.sav_off:
            self.sav_off = 0
        if size == self.sav_off:
            return
        with open(SAVINGS, errors="replace") as fh:
            fh.seek(self.sav_off)
            chunk = fh.read()
        end = chunk.rfind("\n") + 1
        self.sav_off += len(chunk[:end].encode())
        for line in chunk[:end].splitlines():
            try:
                r = json.loads(line)
            except ValueError:
                continue
            ts, cut = r.get("ts") or 0, max(0, (r.get("before") or 0) - (r.get("after") or 0))
            if ts < self.start:
                continue
            c = self.convs.get(r.get("session"))
            if c is not None:
                c.pending += cut
            self.today["trims"] += 1
            self.today["trimmed"] += cut
            rule = r.get("rule", "?")
            self.rules[rule][0] += 1
            self.rules[rule][1] += cut
            if cut >= 300:
                self.feed.append({"ts": ts, "kind": "trim", "tokens": cut,
                                  "project": c.project if c else "",
                                  "text": "%s: %s → %s" % (RULES.get(rule, rule), _k(r.get("before")), _k(r.get("after")))})
            self.version += 1

    def _read_file(self, p, st):
        try:
            size = os.path.getsize(p)
        except OSError:
            return
        if size <= st[0]:
            return
        with open(p, "rb") as fh:
            fh.seek(st[0])
            chunk = fh.read()
        end = chunk.rfind(b"\n") + 1
        st[0] += end
        c = self.convs[st[1]]
        for raw in chunk[:end].splitlines():
            if b'"usage"' not in raw and b"isCompactSummary" not in raw and (st[2] or c.title or b'"user"' not in raw):
                continue
            try:
                d = json.loads(raw)
            except ValueError:
                continue
            t = d.get("type")
            if t == "user" and not st[2] and not c.title and not d.get("isMeta") and not d.get("isCompactSummary"):
                m = (d.get("message") or {}).get("content")
                txt = m if isinstance(m, str) else next((x.get("text", "") for x in m or [] if isinstance(x, dict) and x.get("type") == "text"), "")
                if txt and not txt.startswith("<"):
                    c.title = " ".join(txt.split())[:70]
                continue
            if t != "assistant":
                continue
            m = d.get("message") or {}
            u, mid = m.get("usage"), m.get("id")
            if not u or mid in st[3]:
                continue
            st[3].add(mid)
            ts = _ts(d.get("timestamp")) or time.time()
            ctx = (u.get("cache_read_input_tokens") or 0) + (u.get("cache_creation_input_tokens") or 0) + (u.get("input_tokens") or 0)
            if not ctx:
                continue
            out = u.get("output_tokens") or 0
            model = m.get("model") or "?"
            if st[2]:                      # sub-agent: its own context, counted as used, no counterfactual
                saved, would = 0, ctx
            else:
                before = c.compactions
                saved = c.step(ts, ctx, self.native)
                would = c.cf
                c.model = model
                if c.compactions > before and ts >= self.start:
                    self.feed.append({"ts": ts, "kind": "compact", "project": c.project, "tokens": int(c.cf - ctx),
                                      "text": "Summarised early: context %s → %s" % (_k(c.series[-2][0] if len(c.series) > 1 else 0), _k(ctx))})
                elif c.deltas and c.deltas[-1] > 25_000 and ts >= self.start:
                    self.feed.append({"ts": ts, "kind": "jump", "project": c.project, "tokens": c.deltas[-1],
                                      "text": "Big jump: +%s added in one step" % _k(c.deltas[-1])})
            c.last = max(c.last, ts)
            if ts < self.start:
                continue
            c.saved_today += saved
            i, _o = PRICES[_fam(model)]
            self.today["used"] += ctx + out
            self.today["would"] += would + out
            self.today["saved"] += saved
            self.today["usd"] += saved * 0.1 * i / 1e6
            self.today["usd_used"] += (ctx * 0.1 * i + out * _o) / 1e6
            self.today["steps"] += 1
            mi = int(ts // 60) * 60
            self.minutes[mi][0] += ctx + out
            self.minutes[mi][1] += would + out
            h = time.localtime(ts).tm_hour
            self.hours[h][0] += ctx + out
            self.hours[h][1] += would + out
            self.version += 1

    def tick(self):
        if dt.date.today() != self.day:
            self._reset_day()
        now = time.time()
        if now - self.last_glob > 8:
            self._discover()
            self.last_glob = now
        self._read_savings()
        for p, st in list(self.files.items()):
            self._read_file(p, st)
        cut = now - 3 * 3600
        for m in [m for m in self.minutes if m < cut]:
            del self.minutes[m]

    # ---------- the picture

    def snapshot(self, titles=True):
        now = time.time()
        T = self.today
        live = sorted((c for c in self.convs.values() if now - c.last < ACTIVE and c.steps),
                      key=lambda c: c.last, reverse=True)[:8]
        m0 = int(now // 60) * 60
        minutes = [[m, *self.minutes.get(m, [0, 0])] for m in range(m0 - 59 * 60, m0 + 60, 60)]
        last5 = minutes[-6:-1] if now - m0 < 20 else minutes[-5:]
        used_pm = sum(r[1] for r in last5) / 5.0
        saved_pm = sum(r[2] - r[1] for r in last5) / 5.0
        convs = []
        for c in live:
            growth = (sum(c.deltas) / len(c.deltas)) if c.deltas else 0
            room = self.ours - c.ctx
            convs.append({
                "id": c.sid[:8], "project": c.project, "title": c.title if titles else "",
                "label": "%s · %s" % (c.project, (c.title[:28] + "…" if len(c.title) > 28 else c.title) if titles and c.title else "#" + c.sid[:4]),
                "steps": c.steps, "agents": len(c.agents), "ctx": int(c.ctx), "cf": int(c.cf),
                "saved": int(c.saved_today), "last": c.last, "model": _fam(c.model),
                "compactions": c.compactions, "growth": int(growth),
                "to_compact": int(room / growth) if growth > 500 and room > 0 else None,
                "series": list(c.series)[-SERIES:],
            })
        return {
            "now": now, "version": self.version, "trigger": self.ours, "native": self.native,
            "today": {k: (round(v, 2) if k.startswith("usd") else int(v)) for k, v in T.items()},
            "rate": {"used_pm": int(used_pm), "saved_pm": int(saved_pm)},
            "minutes": minutes,
            "hours": [[h, *self.hours.get(h, [0, 0])] for h in range(24)],
            "convs": convs,
            "rules": sorted(([RULES.get(r, r), n, int(t)] for r, (n, t) in self.rules.items()), key=lambda x: -x[2])[:6],
            "feed": sorted(self.feed, key=lambda f: -f["ts"])[:30],
            "insights": self._insights(convs, used_pm, saved_pm),
        }

    def _insights(self, convs, used_pm, saved_pm):
        T, out = self.today, []
        if T["would"]:
            share = 1 - T["used"] / T["would"]
            out.append(("good", "Today Claude re-read %d%% less than it would have without tokentrim, so your plan limits last %.1f× longer."
                        % (round(share * 100), T["would"] / max(T["used"], 1))))
        for c in convs:
            if c["to_compact"] is not None and c["to_compact"] <= 15:
                when = "any step now" if c["to_compact"] < 2 else "in about %d steps" % c["to_compact"]
                out.append(("warn", "%s will summarise %s (%s of %s). Wrap up the current thought first."
                            % (c["label"], when, _k(c["ctx"]), _k(self.ours))))
            if c["steps"] >= 300:
                out.append(("tip", "%s is at %d steps. A fresh conversation would start near %s per step instead of %s."
                            % (c["label"], c["steps"], _k(60_000), _k(c["ctx"]))))
            if c["growth"] > 15_000:
                out.append(("warn", "%s is growing ~%s per step: likely big tool results. Ask for summaries or ranges."
                            % (c["label"], _k(c["growth"]))))
        if self.rules:
            r, (n, t) = max(self.rules.items(), key=lambda x: x[1][1])
            out.append(("info", "Biggest live saver today: %s (%d times, %s kept out of the chat)." % (RULES.get(r, r).lower(), n, _k(t))))
        if used_pm > 0:
            out.append(("info", "Right now: %s/min re-read, %s/min saved. At this pace, %s this hour."
                        % (_k(used_pm), _k(max(saved_pm, 0)), _k(used_pm * 60))))
        elif not convs:
            out.append(("info", "Nothing running right now. Start a conversation and this page fills in live."))
        return [{"level": l, "text": t} for l, t in out[:7]]

def _k(n):
    n = float(n or 0)
    for unit, d in (("B", 1e9), ("M", 1e6), ("k", 1e3)):
        if abs(n) >= d:
            return ("%.1f" % (n / d)).rstrip("0").rstrip(".") + unit
    return str(int(n))


# ---------- remote: push totals to your private Cloudflare page

LIVE_CFG = os.path.expanduser("~/.config/tokentrim/live.json")


def remote_cfg():
    try:
        with open(LIVE_CFG) as fh:
            return json.load(fh)
    except (OSError, ValueError):
        return None


def push(cfg, snap):
    import urllib.request
    req = urllib.request.Request(cfg["url"].rstrip("/") + "/push", data=json.dumps(snap, separators=(",", ":")).encode(),
                                 headers={"Content-Type": "application/json", "Authorization": "Bearer " + cfg["write"],
                                          "User-Agent": "tokentrim-live"})
    with urllib.request.urlopen(req, timeout=10) as r:
        r.read()


def run(tracker, holder, interval=2.0):
    """Background loop used by `tokentrim serve`: tick, keep the latest snapshot, push when it changed."""
    last_v, last_push = -1, 0.0
    while True:
        try:
            tracker.tick()
            holder["snap"] = tracker.snapshot()
            cfg = remote_cfg()
            now = time.time()
            if cfg and (tracker.version != last_v and now - last_push >= 3 or now - last_push >= 60):
                push(cfg, tracker.snapshot(titles=cfg.get("titles", False)))
                last_v, last_push = tracker.version, now
        except Exception as e:  # keep the loop alive; the page shows how stale the data is
            print("live:", e, flush=True)
            time.sleep(10)
        time.sleep(interval)


def deploy(titles=False):
    """Put the live page on your own Cloudflare account (free plan is enough) and start pushing to it."""
    import re
    import secrets
    import shutil
    import subprocess
    import tempfile
    here = os.path.dirname(os.path.abspath(__file__))
    cfg = remote_cfg() or {"read": secrets.token_urlsafe(24), "write": secrets.token_urlsafe(32)}
    build = tempfile.mkdtemp(prefix="tokentrim-live-")
    for f in ("worker.js", "wrangler.toml"):
        shutil.copy(os.path.join(here, "remote", f), build)
    shutil.copy(os.path.join(here, "live.html"), build)
    wr = ["npx", "--yes", "wrangler@4"]
    out = subprocess.run(wr + ["deploy"], cwd=build, capture_output=True, text=True)
    if out.returncode:
        sys.exit("wrangler deploy failed (run `npx wrangler login` first?):\n" + out.stderr[-1500:])
    m = re.search(r"https://[\w.-]+\.workers\.dev", out.stdout + out.stderr)
    for name, val in (("READ_KEY", cfg["read"]), ("WRITE_KEY", cfg["write"])):
        r = subprocess.run(wr + ["secret", "put", name], cwd=build, input=val, capture_output=True, text=True)
        if r.returncode:
            sys.exit("setting %s failed:\n%s" % (name, r.stderr[-800:]))
    shutil.rmtree(build, ignore_errors=True)
    cfg.update(url=m.group(0) if m else cfg.get("url"), titles=titles)
    os.makedirs(os.path.dirname(LIVE_CFG), exist_ok=True)
    with open(LIVE_CFG, "w") as fh:
        json.dump(cfg, fh, indent=2)
    os.chmod(LIVE_CFG, 0o600)
    print("deployed. `tokentrim serve` (already running at login if installed) now pushes totals every few seconds.")
    link()


def link():
    cfg = remote_cfg()
    if not cfg or not cfg.get("url"):
        sys.exit("not deployed yet: tokentrim live --deploy")
    print("private link (open once per device, then bookmark):\n  %s/#k=%s" % (cfg["url"], cfg["read"]))
