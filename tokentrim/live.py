"""Real-time savings: `tokentrim serve` → http://localhost:4747/live

Tails the last week of transcripts and the hook's savings log every couple of
seconds and keeps, per conversation, the context Claude actually re-read on each
step next to the context it would have re-read without tokentrim:

  without = actual + every trimmed tool result still in the conversation
            + the history an earlier compaction removed (until Claude Code's own,
              much later compaction would have fired)

The difference, summed over steps, is the live saving. Same model as `replay`,
just running forward. On top of that it keeps a step-by-step log, the 5-hour
usage window Claude's plan limits reset on, a 7-day history and plain-English
insights. Restart-safe: the week is re-read from the transcripts on start.

Nothing leaves the machine unless you run `tokentrim live --deploy` (your own
Cloudflare) or `tokentrim live --cloud` (Pro, hosted), and then only totals and
project folder names: no prompts, no titles, no code.
"""
import datetime as dt
import glob
import json
import os
import sys
import threading
import time
import urllib.error
import urllib.request
from collections import defaultdict, deque

from . import cli, config, controls
from .audit import PROJECTS
from .share import PRICES, _fam

STATE = os.path.expanduser("~/.cache/tokentrim")
SAVINGS = os.path.join(STATE, "savings.jsonl")
TODAY = os.path.join(STATE, "today.json")      # read by the hook's budget and cold-cache guards
ACTIVE = 20 * 60          # a conversation is "live" if it was written to in the last 20 min
TRIGGER = 0.75            # compaction fires at ~75% of CLAUDE_CODE_AUTO_COMPACT_WINDOW
SERIES = 160              # context points kept per conversation for its sparkline
HISTORY = 7               # days re-read on start (about 2-3 s for a heavy week)
WINDOW = 5 * 3600         # Claude's plan limits reset on 5-hour windows
LOG = 400                 # log rows kept for today (the page shows them all, remote gets 200)
REBUILD = 30_000          # a cache write this big after a pause = the cache expired and was rebuilt
IDLE = 5 * 60             # prompt cache lifetime
PULSE = 5                 # live graph: 5-second buckets...
PULSE_SPAN = 30 * 60      # ...for the last 30 minutes
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


def _midnight(day=None):
    return time.mktime((day or dt.date.today()).timetuple())


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


def _cost(fam, cr, cw, inp, out):
    """API-price equivalent in $: cache reads 0.1x input, cache writes 1.25x."""
    i, o = PRICES[fam]
    return (cr * 0.1 * i + cw * 1.25 * i + inp * i + out * o) / 1e6


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
        self.cost_today = 0.0
        self.compactions = 0
        self.last = 0.0
        self.model = "?"
        self.deltas = deque(maxlen=12)
        self.series = deque(maxlen=SERIES)

    def step(self, ts, ctx, native, counted=True):
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
        if not counted:                  # before tokentrim was installed nothing was trimmed or compacted early
            self.cf, self.carried = ctx, 0
        self.prev, self.ctx = ctx, ctx
        self.steps += 1
        self.last = max(self.last, ts)
        saved = self.cf - ctx
        self.saved += saved
        self.series.append((int(ctx), int(self.cf)))
        return saved

    def label(self, titles):
        if titles and self.title:
            return "%s · %s" % (self.project, self.title[:28] + "…" if len(self.title) > 28 else self.title)
        return "%s · #%s" % (self.project, self.sid[:4])


class Tracker:
    def __init__(self, history=HISTORY):
        from .dashboard import _install_ts
        self.history = history
        self.files = {}                 # path -> [offset, sid, is_agent, seen message ids]
        self.convs = {}
        self.sav_off = 0
        self.trims = defaultdict(list)  # sid -> [(ts, tokens)] not yet folded into a step
        self.ours, self.native = _windows()
        self.install = _install_ts(cli._load_settings()) or 0
        self.events = []                # (ts, tokens, would, cost, would_cost) per step, for the history window
        self.daily = defaultdict(lambda: defaultdict(float))
        self.version = 0
        self.ready = False
        self.errors = deque(maxlen=5)
        self._an, self._an_at, self._an_v = {}, 0.0, -1
        self.pulse = defaultdict(lambda: [0, 0, 0.0])   # 5 s bucket -> [used, would, cost]
        self.ctl_off = 0
        self._ctl, self._ctl_at = None, 0.0
        self.ttl_votes = [0, 0]          # pauses of 5-60 min where the cache [expired, survived]: tells 5 min from 1 h caching
        self._today_at = 0.0
        self.day = None
        self._reset_day()
        self.last_glob = 0.0

    def _reset_day(self):
        self.day = dt.date.today()
        self.start = _midnight()
        self.t0 = _midnight(self.day - dt.timedelta(days=self.history - 1))
        self.today = defaultdict(float)
        self.minutes = defaultdict(lambda: [0, 0])   # minute -> [used, would]
        self.hours = defaultdict(lambda: [0, 0])
        self.rules = defaultdict(lambda: [0, 0])
        self.projects = defaultdict(lambda: [0, 0, 0.0, 0])   # used, saved, cost, steps
        self.models = defaultdict(lambda: [0, 0.0, 0])        # used, cost, steps
        self.feed = deque(maxlen=80)
        self.log = []
        self.events = [e for e in self.events if e[0] >= self.t0]
        for d in [d for d in self.daily if d < dt.date.fromtimestamp(self.t0).isoformat()]:
            del self.daily[d]
        for c in self.convs.values():
            c.saved_today, c.cost_today = 0, 0.0
        self.version += 1

    # ---------- reading

    def _discover(self):
        cutoff = self.t0 if not self.files else time.time() - ACTIVE
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
        if size < self.sav_off:          # rotated or truncated: start over
            self.sav_off = 0
        if size == self.sav_off:
            return
        with open(SAVINGS, "rb") as fh:
            fh.seek(self.sav_off)
            chunk = fh.read()
        end = chunk.rfind(b"\n") + 1     # only whole lines; a half-written one waits for the next tick
        self.sav_off += end
        for line in chunk[:end].splitlines():
            try:
                r = json.loads(line)
            except ValueError:
                continue
            ts, cut = r.get("ts") or 0, max(0, (r.get("before") or 0) - (r.get("after") or 0))
            if ts < self.t0:
                continue
            sid = r.get("session") or ""
            self.trims[sid].append((ts, cut))   # folded into the conversation at its next step
            if ts < self.start:
                continue
            c = self.convs.get(sid)
            rule = r.get("rule", "?")
            self.today["trims"] += 1
            self.today["trimmed"] += cut
            self.rules[rule][0] += 1
            self.rules[rule][1] += cut
            self.log.append({"ts": ts, "sid": sid, "kind": "trim", "rule": RULES.get(rule, rule), "tool": r.get("tool") or "",
                             "before": int(r.get("before") or 0), "after": int(r.get("after") or 0), "saved": int(cut)})
            if cut >= 300:
                self.feed.append({"ts": ts, "kind": "trim", "tokens": cut, "project": c.project if c else "",
                                  "text": "%s: %s → %s" % (RULES.get(rule, rule), _k(r.get("before")), _k(r.get("after")))})
            self.version += 1

    def _read_file(self, p, st):
        try:
            size = os.path.getsize(p)
        except OSError:
            return
        if size < st[0]:                 # rewritten in place: re-read; seen message ids stop double counting
            st[0] = 0
        if size == st[0]:
            return
        with open(p, "rb") as fh:
            fh.seek(st[0])
            chunk = fh.read()
        end = chunk.rfind(b"\n") + 1
        st[0] += end
        c = self.convs[st[1]]
        for raw in chunk[:end].splitlines():
            if b'"usage"' not in raw and (st[2] or c.title or b'"user"' not in raw):
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
            self._step(c, st[2], d, m, u)

    def _step(self, c, agent, d, m, u):
        ts = _ts(d.get("timestamp")) or time.time()
        cr, cw, inp = (u.get("cache_read_input_tokens") or 0), (u.get("cache_creation_input_tokens") or 0), (u.get("input_tokens") or 0)
        ctx = cr + cw + inp
        if not ctx:
            return
        out = u.get("output_tokens") or 0
        model = m.get("model") or "?"
        fam = _fam(model)
        i = PRICES[fam][0]
        gap = ts - c.last if c.last else 0
        kind = "agent" if agent else "step"
        if agent:                        # sub-agent: its own context, counted as used, no counterfactual
            saved, would = 0, ctx
        else:
            q = self.trims.get(c.sid)
            if q:
                c.pending += sum(n for t, n in q if t <= ts)
                q[:] = [x for x in q if x[0] > ts]
            before, prev = c.compactions, c.prev
            if IDLE < gap < 3600 and (prev or 0) > REBUILD:
                self.ttl_votes[cw < 0.3 * prev] += 1
            saved = c.step(ts, ctx, self.native, ts >= self.install)
            would = c.cf
            c.model = model
            if c.compactions > before:
                kind = "compact"
            elif cw >= REBUILD and gap >= IDLE and c.steps > 1:
                kind = "rebuild"
        c.last = max(c.last, ts)
        if agent:
            prev = None
        if ts < self.t0:
            return
        cost = _cost(fam, cr, cw, inp, out)
        save_usd = saved * 0.1 * i / 1e6
        self.events.append((ts, ctx + out, would + out, cost, cost + save_usd))
        D = self.daily[dt.date.fromtimestamp(ts).isoformat()]
        D["used"] += ctx + out
        D["would"] += would + out
        D["saved"] += saved
        D["cost"] += cost
        D["would_cost"] += cost + save_usd
        D["steps"] += 1
        self.version += 1
        if ts < self.start:
            return
        T = self.today
        T["used"] += ctx + out
        T["would"] += would + out
        T["saved"] += saved
        T["usd"] += save_usd
        T["cost"] += cost
        T["would_cost"] += cost + save_usd
        T["steps"] += 1
        T["out"] += out
        T["cr"] += cr
        T["cw"] += cw
        T["inp"] += inp
        if agent:
            T["agent_used"] += ctx + out
            T["agent_steps"] += 1
        c.saved_today += saved
        c.cost_today += cost
        if ts >= time.time() - PULSE_SPAN:
            B = self.pulse[int(ts // PULSE) * PULSE]
            B[0] += ctx + out
            B[1] += would + out
            B[2] += cost
        mi = int(ts // 60) * 60
        self.minutes[mi][0] += ctx + out
        self.minutes[mi][1] += would + out
        h = time.localtime(ts).tm_hour
        self.hours[h][0] += ctx + out
        self.hours[h][1] += would + out
        P = self.projects[c.project]
        P[0] += ctx + out
        P[1] += saved
        P[2] += cost
        P[3] += 1
        M = self.models[fam]
        M[0] += ctx + out
        M[1] += cost
        M[2] += 1
        row = {"ts": ts, "sid": c.sid, "kind": kind, "model": fam, "ctx": int(ctx), "cf": int(would), "saved": int(saved),
               "out": int(out), "cw": int(cw), "cr": int(cr), "inp": int(inp), "cost": round(cost, 5), "save_usd": round(save_usd, 5),
               "dctx": int(ctx - prev) if prev is not None else None, "since": int(gap) if gap else None}
        if kind == "compact":
            T["compactions"] += 1
            prev = c.series[-2][0] if len(c.series) > 1 else 0
            self.feed.append({"ts": ts, "kind": "compact", "project": c.project, "tokens": int(would - ctx),
                              "text": "Summarised early: context %s → %s" % (_k(prev), _k(ctx))})
        elif kind == "rebuild":
            extra = cw * 1.15 * i / 1e6
            T["rebuilds"] += 1
            T["rebuild_usd"] += extra
            row["gap"] = int(gap)
            self.feed.append({"ts": ts, "kind": "rebuild", "project": c.project, "tokens": int(cw),
                              "text": "Cache rebuilt after %d min idle: %s re-written (~$%.2f extra)" % (gap // 60, _k(cw), extra)})
        elif not agent and c.deltas and c.deltas[-1] > 25_000:
            self.feed.append({"ts": ts, "kind": "jump", "project": c.project, "tokens": c.deltas[-1],
                              "text": "Big jump: +%s added in one step" % _k(c.deltas[-1])})
        self.log.append(row)

    def _read_controls(self):
        """Settings changed from the dashboard show up in the log and the highlights."""
        try:
            size = os.path.getsize(controls.CHANGES)
        except OSError:
            return
        if size < self.ctl_off:
            self.ctl_off = 0
        if size == self.ctl_off:
            return
        with open(controls.CHANGES, "rb") as fh:
            fh.seek(self.ctl_off)
            chunk = fh.read()
        end = chunk.rfind(b"\n") + 1
        self.ctl_off += end
        for line in chunk[:end].splitlines():
            try:
                r = json.loads(line)
            except ValueError:
                continue
            if (r.get("ts") or 0) < self.start:
                continue
            label = controls.SPEC.get(r.get("key"), ("", r.get("key"), ""))[1]
            new = r.get("new")
            val = ("on" if new else "off") if isinstance(new, bool) else str(new)
            self.log.append({"ts": r["ts"], "sid": "", "kind": "setting", "rule": "%s → %s" % (label, val), "tool": r.get("source") or "",
                             "saved": 0})
            self.feed.append({"ts": r["ts"], "kind": "setting", "project": "", "text": "Setting changed from the %s dashboard: %s → %s"
                              % (r.get("source") or "local", label, val)})
            self._ctl_at = 0
            self.version += 1

    def controls(self):
        if time.time() - self._ctl_at > 5:
            try:
                self._ctl = controls.state()
                self.ours, self.native = _windows()   # follow a profile change made from the dashboard
            except Exception as e:     # a broken settings.json must not take the page down
                self._ctl = None
                self.errors.append("controls: %s" % e)
            self._ctl_at = time.time()
        return self._ctl

    def tick(self):
        if dt.date.today() != self.day:
            self._reset_day()
        now = time.time()
        if now - self.last_glob > 8:
            self._discover()
            self.last_glob = now
        self._read_savings()
        self._read_controls()
        if self.ready and now - self._today_at > 10:
            self._today_at = now
            self._write_today()
        for p, st in list(self.files.items()):
            try:
                self._read_file(p, st)
            except Exception as e:      # one bad file never stops the rest
                self.errors.append("%s: %s" % (os.path.basename(p), e))
        cut = now - 3 * 3600
        for m in [m for m in self.minutes if m < cut]:
            del self.minutes[m]
        for b in [b for b in self.pulse if b < now - PULSE_SPAN - 60]:
            del self.pulse[b]
        if len(self.log) > 2 * LOG:
            self.log.sort(key=lambda r: r["ts"])
            del self.log[:-LOG]
        old = now - ACTIVE * 6           # forget trims for conversations that never wrote a step
        for sid in [s for s, q in self.trims.items() if q and q[-1][0] < old]:
            del self.trims[sid]
        self.ready = True

    # ---------- analysis (recomputed at most every 5 s)

    def _analytics(self, now):
        if self._an and (self.version == self._an_v or now - self._an_at < 5):
            return self._an
        ev = sorted(self.events)
        wins, cur = [], None
        for e in ev:
            if cur is None or e[0] >= cur["start"] + WINDOW:
                cur = {"start": int(e[0] // 3600 * 3600), "used": 0, "would": 0, "cost": 0.0, "would_cost": 0.0, "steps": 0}
                wins.append(cur)
            cur["used"] += e[1]
            cur["would"] += e[2]
            cur["cost"] += e[3]
            cur["would_cost"] += e[4]
            cur["steps"] += 1
        active = wins[-1] if wins and now < wins[-1]["start"] + WINDOW else None
        past = wins[:-1] if active else wins
        peak = max((w["cost"] for w in past), default=0.0)
        window = None
        if active:
            end = active["start"] + WINDOW
            recent = sum(e[3] for e in ev if e[0] >= now - 1800) / 30.0
            window = dict(active, end=end, rate=round(recent, 4), peak=round(peak, 2), windows=len(past),
                          projected=round(active["cost"] + recent * max(0, end - now) / 60.0, 2))
            for k in ("cost", "would_cost"):
                window[k] = round(window[k], 2)
        # 7 days x 24 hours
        heat = [[0] * 24 for _ in range(self.history)]
        for e in ev:
            di = int((e[0] - self.t0) // 86400)
            if 0 <= di < self.history:
                heat[di][time.localtime(e[0]).tm_hour] += e[1]
        hours = [x for row in heat[:-1] for x in row if x]
        usual = sum(hours) / len(hours) if hours else 0
        last_hour = sum(e[1] for e in ev if e[0] >= now - 3600)
        y0 = self.start - 86400
        yday = [0, 0]
        for e in ev:
            if y0 <= e[0] < now - 86400:
                yday[0] += e[1]
                yday[1] += e[2] - e[1]
        week = []
        for n in range(self.history):
            day = (self.day - dt.timedelta(days=self.history - 1 - n))
            D = self.daily.get(day.isoformat(), {})
            week.append({"day": day.isoformat(), "label": day.strftime("%a"), "used": int(D.get("used", 0)),
                         "would": int(D.get("would", 0)), "saved": int(D.get("saved", 0)),
                         "cost": round(D.get("cost", 0), 2), "would_cost": round(D.get("would_cost", 0), 2),
                         "steps": int(D.get("steps", 0))})
        self._an = {"window": window, "heat": heat, "heat_days": [w["label"] for w in week], "week": week,
                    "compare": {"yday_used": int(yday[0]), "yday_saved": int(yday[1]), "last_hour": int(last_hour),
                                "usual_hour": int(usual), "pace": round(last_hour / usual, 2) if usual else None}}
        self._an_at, self._an_v = now, self.version
        return self._an

    # ---------- the picture

    def snapshot(self, titles=True, log_rows=LOG):
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
                "id": c.sid[:8], "project": c.project, "title": c.title if titles else "", "label": c.label(titles),
                "steps": c.steps, "agents": len(c.agents), "ctx": int(c.ctx), "cf": int(c.cf),
                "saved": int(c.saved_today), "cost": round(c.cost_today, 2), "last": c.last, "model": _fam(c.model),
                "compactions": c.compactions, "growth": int(growth),
                "to_compact": int(room / growth) if growth > 500 and room > 0 else None,
                "series": list(c.series)[-SERIES:],
            })
        labels = {}
        log = []
        for r in sorted(self.log, key=lambda r: r["ts"])[-log_rows:]:
            sid = r["sid"]
            if sid not in labels:
                c = self.convs.get(sid)
                labels[sid] = c.label(titles) if c else ("tokentrim" if not sid else "#" + sid[:4])
            row = dict(r, conv=labels[sid])
            row["sid"] = sid[:8]
            log.append(row)
        an = self._analytics(now)
        today = {k: (round(v, 2) if k.endswith("usd") or "cost" in k else int(v)) for k, v in T.items()}
        ctx_in = T["cr"] + T["cw"] + T["inp"]
        today["cache_hit"] = round(T["cr"] / ctx_in, 4) if ctx_in else None
        today["avg_ctx"] = int(ctx_in / T["steps"]) if T["steps"] else 0
        snap = {
            "now": now, "version": self.version, "ready": self.ready, "trigger": self.ours, "native": self.native,
            "install": self.install, "today": today,
            "rate": {"used_pm": int(used_pm), "saved_pm": int(saved_pm)},
            "minutes": minutes,
            "hours": [[h, *self.hours.get(h, [0, 0])] for h in range(24)],
            "convs": convs,
            "rules": sorted(([RULES.get(r, r), n, int(t)] for r, (n, t) in self.rules.items()), key=lambda x: -x[2])[:6],
            "projects": sorted(([p, int(v[0]), int(v[1]), round(v[2], 2), v[3]] for p, v in self.projects.items()), key=lambda x: -x[1])[:8],
            "models": sorted(([m, int(v[0]), round(v[1], 2), v[2]] for m, v in self.models.items()), key=lambda x: -x[2]),
            "feed": sorted(self.feed, key=lambda f: -f["ts"])[:30],
            "log": log,
        }
        p0 = int(now // PULSE) * PULSE - PULSE_SPAN + PULSE
        snap["pulse"] = {"t0": p0, "step": PULSE, "data": [self.pulse[b] if b in self.pulse else [0, 0, 0] for b in range(p0, p0 + PULSE_SPAN, PULSE)]}
        snap["pulse"]["data"] = [[int(u), int(w), round(c, 4)] for u, w, c in snap["pulse"]["data"]]
        snap.update(an)
        snap["controls"] = self.controls()
        if snap["controls"]:
            snap["controls"]["ttl"] = self.ttl()
        snap["insights"] = self._insights(snap, convs, used_pm, saved_pm)
        snap["recs"] = self._recommend(snap, convs)
        return snap

    def ttl(self):
        short, long_ = self.ttl_votes
        return 3600 if long_ > short else 300

    def _write_today(self):
        """Today's spend for the hook's budget and cold-cache guards (the hook itself must stay fast)."""
        T = self.today
        price = sum(PRICES.get(m, PRICES["other"])[0] * v[0] for m, v in self.models.items()) / max(1e-9, sum(v[0] for v in self.models.values()))
        try:
            os.makedirs(os.path.dirname(TODAY), exist_ok=True)
            with open(TODAY + ".tmp", "w") as fh:
                json.dump({"day": time.strftime("%Y-%m-%d"), "ts": time.time(), "cost": round(T["cost"], 4),
                           "ttl": self.ttl(), "price_in": round(price, 2) if self.models else 3}, fh)
            os.replace(TODAY + ".tmp", TODAY)
        except OSError:
            pass

    def _recommend(self, s, convs):
        """Concrete changes that would lower *this* user's usage, each with an estimate and, where the
        dashboard can do it, a one-click action. Estimates are from today's own steps."""
        T, recs = self.today, []
        ctl = s.get("controls") or {}
        vals = ctl.get("values") or {}
        price = {}
        for m, used, cost, steps in s.get("models") or []:
            price[m] = PRICES.get(m, PRICES["other"])[0]
        avg_in = (sum(price.values()) / len(price)) if price else 3

        def rd(tokens, inp=avg_in):     # tokens re-read from cache -> $
            return tokens * 0.1 * inp / 1e6

        if ctl and not ctl.get("installed"):
            recs.append({"id": "install", "level": "high", "title": "tokentrim's hooks aren't installed",
                         "detail": "Nothing is being trimmed. Run the install once; it backs up your settings first.",
                         "impact": None, "action": {"type": "copy", "text": "tokentrim install", "label": "Copy command"}})
        elif vals and not vals.get("enabled", True):
            recs.append({"id": "enabled", "level": "high", "title": "tokentrim is switched off",
                         "detail": "Every rule is paused, so Claude re-reads full screenshots, files and command output.",
                         "impact": None, "action": {"type": "set", "changes": {"enabled": True}, "label": "Turn on"}})
        off = [k for k in ("rule_screenshots", "rule_outline", "rule_bash", "rule_mcp", "rule_images") if vals and not vals.get(k, True)]
        if off and vals.get("enabled", True):
            names = ", ".join(controls.SPEC[k][1].lower() for k in off)
            recs.append({"id": "rules", "level": "med", "title": "%d saving rule%s off" % (len(off), "" if len(off) == 1 else "s"),
                         "detail": "Off right now: %s. Each one only shrinks what Claude reads back; full copies stay on disk." % names,
                         "impact": None, "action": {"type": "set", "changes": {k: True for k in off}, "label": "Turn all on"}})

        # A tighter summary point: replay today's steps against the next profile down.
        prof = vals.get("profile")
        order = sorted(cli.PROFILES, key=lambda p: -cli.PROFILES[p])
        if prof in order and order.index(prof) < len(order) - 1 and T["steps"] > 50:
            nxt = order[order.index(prof) + 1]
            trig = cli.PROFILES[nxt] * TRIGGER
            extra = 0.0
            for r in self.log:
                if r["kind"] in ("step", "compact", "rebuild") and r["ctx"] > trig:
                    extra += rd(r["ctx"] - trig, PRICES.get(r.get("model"), PRICES["other"])[0]) * 0.7
            if extra > max(0.5, 0.04 * T["cost"]):
                recs.append({"id": "profile", "level": "high" if extra > 0.15 * T["cost"] else "med",
                             "title": "Summarise earlier: %s → %s" % (prof, nxt),
                             "detail": "%d%% of today's steps carried more than %s of context. Summarising at %s instead would have cut "
                                       "about $%.2f today. Trade-off: Claude forgets older detail sooner. Applies to new sessions."
                                       % (round(100 * sum(1 for r in self.log if r["kind"] in ("step", "compact", "rebuild") and r["ctx"] > trig)
                                                / max(1, sum(1 for r in self.log if r["kind"] in ("step", "compact", "rebuild")))),
                                          _k(trig), _k(trig), extra),
                             "impact": round(extra, 2), "action": {"type": "set", "changes": {"profile": nxt}, "label": "Use %s" % nxt}})

        for c in convs:
            if c["ctx"] >= 0.6 * self.ours and c["steps"] >= 120:
                per50 = rd((c["ctx"] - 60_000) * 50, PRICES.get(c["model"], PRICES["other"])[0])
                recs.append({"id": "fresh-" + c["id"], "level": "med", "title": "Start fresh after this task: %s" % c["label"],
                             "detail": "%d steps in, every step re-reads %s. A new conversation starts near 60k, about $%.2f less every 50 steps. "
                                       "Or run /compact now to keep going with a summary." % (c["steps"], _k(c["ctx"]), per50),
                             "impact": round(per50, 2), "action": {"type": "copy", "text": "/compact", "label": "Copy /compact"}})
            elif c["growth"] > 15_000:
                recs.append({"id": "growth-" + c["id"], "level": "med", "title": "%s grows %s per step" % (c["label"], _k(c["growth"])),
                             "detail": "Big tool results are landing in the chat. Ask Claude to read files in ranges and summarise long output.",
                             "impact": None, "action": {"type": "copy", "text": "Read files in ranges and summarise long tool output instead of pasting it.",
                                                        "label": "Copy instruction"}})
        models = s.get("models") or []
        cost = sum(m[2] for m in models)
        opus = next((m for m in models if m[0] == "opus"), None)
        if opus and cost > 2 and opus[2] / cost > 0.5:
            o, so = PRICES["opus"][0], PRICES["sonnet"][0]
            sv = opus[2] * 0.4 * (1 - so / o)
            recs.append({"id": "model", "level": "med", "title": "Opus did %d%% of today's usage" % round(100 * opus[2] / cost),
                         "detail": "Moving routine steps (edits, tests, searches), say 40%%, to Sonnet would have used about $%.2f less today. "
                                   "Switch back with /model opus for hard problems." % sv,
                         "impact": round(sv, 2), "action": {"type": "set", "changes": {"model_mode": "auto"}, "label": "Use Smart model"}
                         if vals.get("model_mode") == "off" and ctl.get("installed") else {"type": "copy", "text": "/model sonnet", "label": "Copy /model sonnet"}})
        cw_usd = sum(r.get("cw", 0) * PRICES.get(r.get("model"), PRICES["other"])[0] * 1.25 / 1e6 for r in self.log if r["kind"] in ("step", "compact"))
        if T["rebuilds"] and T["rebuild_usd"] > 0.2 and self.ttl() == 300 and vals.get("cache_ttl") == "off" \
                and T["rebuild_usd"] > 0.6 * cw_usd and ctl.get("installed"):
            recs.append({"id": "ttl", "level": "med", "title": "Keep the cache for 1 hour",
                         "detail": "Your cache expired %d time%s today after breaks, re-writing whole conversations ($%.2f). A 1-hour cache "
                                   "costs about $%.2f more on today's writes and would have avoided most of that. New sessions."
                                   % (T["rebuilds"], "" if T["rebuilds"] == 1 else "s", T["rebuild_usd"], 0.6 * cw_usd),
                         "impact": round(max(0, T["rebuild_usd"] - 0.6 * cw_usd), 2), "action": {"type": "set", "changes": {"cache_ttl": "1h"}, "label": "Use 1-hour cache"}})
        elif T["rebuilds"] and T["rebuild_usd"] > 0.2:
            recs.append({"id": "rebuild", "level": "low", "title": "%d cache rebuild%s after breaks" % (T["rebuilds"], "" if T["rebuilds"] == 1 else "s"),
                         "detail": "Claude's cache expires after 5 idle minutes and the whole conversation is re-written at 1.25× price. "
                                   "Before a long break, /compact, or start a new conversation when you're back.",
                         "impact": round(T["rebuild_usd"], 2), "action": None})
        if T["used"] and T["agent_used"] / T["used"] > 0.25:
            recs.append({"id": "agents", "level": "low", "title": "Sub-agents: %d%% of today's tokens" % round(100 * T["agent_used"] / T["used"]),
                         "detail": "Each sub-agent builds its own context from scratch. Running them on Sonnet keeps your main model for the "
                                   "conversation; narrow tasks and short reports help too.",
                         "impact": None, "action": {"type": "set", "changes": {"subagent_model": "sonnet"}, "label": "Sub-agents on Sonnet"}
                         if vals.get("subagent_model") == "off" and ctl.get("installed") else None})
        week = [d for d in (s.get("week") or []) if d.get("cost")]
        if vals and not vals.get("budget_guard") and len(week) >= 3:
            avg = sum(d["cost"] for d in week) / len(week)
            cap = next((c for c in controls.CHOICES["budget_usd"] if c >= avg * 1.5), None)
            if cap and avg > 3:
                recs.append({"id": "budget", "level": "low", "title": "Set a daily budget of $%d" % cap,
                             "detail": "You average $%.2f a day. tokentrim tells you at 80%% and when you pass it, so a runaway session "
                                       "doesn't surprise you. Add a hard stop in Controls if you want one." % avg,
                             "impact": None, "action": {"type": "set", "changes": {"budget_guard": True, "budget_usd": cap}, "label": "Set $%d budget" % cap}})
        if vals and not vals.get("guidance", True):
            recs.append({"id": "guidance", "level": "low", "title": "Turn on the CLAUDE.md habits",
                         "detail": "~110 tokens that steer Claude to page text over screenshots, Edit over Write and ranged reads.",
                         "impact": None, "action": {"type": "set", "changes": {"guidance": True}, "label": "Turn on"}})
        if vals.get("mcp_lossless_only") and self.rules.get("mcp_shrink", [0])[0]:
            recs.append({"id": "mcp", "level": "low", "title": "Let tokentrim clip huge MCP results",
                         "detail": "MCP results are only minified now. Clipping over 12k chars keeps a full copy on disk.",
                         "impact": None, "action": {"type": "set", "changes": {"mcp_lossless_only": False}, "label": "Allow clipping"}})
        rank = {"high": 0, "med": 1, "low": 2}
        recs.sort(key=lambda r: (rank[r["level"]], -(r["impact"] or 0)))
        return recs[:8]

    def _insights(self, s, convs, used_pm, saved_pm):
        T, out = self.today, []
        if T["would"] > T["used"]:
            share = 1 - T["used"] / T["would"]
            out.append(("good", "Today Claude re-read %d%% less than it would have without tokentrim, so your plan limits last %.1f× longer."
                        % (round(share * 100), T["would"] / max(T["used"], 1))))
        w = s.get("window")
        if w:
            reset = time.strftime("%-I:%M %p", time.localtime(w["end"]))
            if w["peak"] and w["projected"] > w["peak"] * 0.9:
                out.append(("warn", "This 5-hour window is heading for $%.0f of usage, near or above your heaviest window this week ($%.0f). If you hit your limit, it resets at %s."
                            % (w["projected"], w["peak"], reset)))
            elif w["peak"]:
                out.append(("info", "This 5-hour window: $%.2f used, about $%.0f by %s at this pace (%d%% of your heaviest this week)."
                            % (w["cost"], w["projected"], reset, round(100 * w["projected"] / w["peak"]))))
            if w["would_cost"] > w["cost"] * 1.2:
                out.append(("good", "Without tokentrim this window would already be at $%.2f, %.1f× what you've used."
                            % (w["would_cost"], w["would_cost"] / max(w["cost"], 0.01))))
        cmp = s.get("compare") or {}
        if cmp.get("pace") and cmp["pace"] >= 2 and cmp["last_hour"] > 2_000_000:
            out.append(("warn", "You're going %.1f× your usual hourly pace (%s in the last hour vs %s usual)."
                        % (cmp["pace"], _k(cmp["last_hour"]), _k(cmp["usual_hour"]))))
        if cmp.get("yday_used") and T["used"]:
            d = T["used"] / cmp["yday_used"] - 1
            out.append(("info", "%s re-read so far today, %s%d%% vs this time yesterday."
                        % (_k(T["used"]), "+" if d >= 0 else "−", abs(round(d * 100)))))
        if T["rebuilds"]:
            out.append(("tip", "%d cache rebuild%s today after breaks over 5 min cost ~$%.2f extra. Finish the thought before stepping away, or start fresh after long breaks."
                        % (T["rebuilds"], "" if T["rebuilds"] == 1 else "s", T["rebuild_usd"])))
        if T["used"] and T["agent_used"] / T["used"] > 0.25:
            out.append(("tip", "Sub-agents used %d%% of today's tokens. Each one starts its own context, so keep their tasks narrow."
                        % round(100 * T["agent_used"] / T["used"])))
        ctx_in = T["cr"] + T["cw"] + T["inp"]
        if ctx_in > 5_000_000 and T["cr"] / ctx_in < 0.85:
            out.append(("warn", "Only %d%% of today's context came from cache. Editing CLAUDE.md, tools or MCP servers mid-session breaks the cache."
                        % round(100 * T["cr"] / ctx_in)))
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
        models = s.get("models") or []
        cost = sum(m[2] for m in models)
        if cost > 5 and models and models[0][0] == "opus" and models[0][2] / cost > 0.7:
            o, so = PRICES["opus"], PRICES["sonnet"]
            out.append(("tip", "Opus did %d%% of today's spend. Routine edits on Sonnet cost about %.1f× less per token."
                        % (round(100 * models[0][2] / cost), o[0] / so[0])))
        if s.get("projects") and len(s["projects"]) > 1:
            p = s["projects"][0]
            out.append(("info", "Busiest project today: %s, %s re-read across %d steps ($%.2f)." % (p[0], _k(p[1]), p[4], p[3])))
        if self.rules:
            r, (n, t) = max(self.rules.items(), key=lambda x: x[1][1])
            out.append(("info", "Biggest live saver today: %s (%d times, %s kept out of the chat)." % (RULES.get(r, r).lower(), n, _k(t))))
        if used_pm > 0:
            out.append(("info", "Right now: %s/min re-read, %s/min saved. At this pace, %s this hour."
                        % (_k(used_pm), _k(max(saved_pm, 0)), _k(used_pm * 60))))
        elif not convs:
            out.append(("info", "Nothing running right now. Start a conversation and this page fills in live."))
        rank = {"warn": 0, "good": 1, "tip": 2, "info": 3}
        out.sort(key=lambda x: rank[x[0]])
        return [{"level": l, "text": t} for l, t in out[:10]]


def _k(n):
    n = float(n or 0)
    for unit, d in (("B", 1e9), ("M", 1e6), ("k", 1e3)):
        if abs(n) >= d:
            return ("%.1f" % (n / d)).rstrip("0").rstrip(".") + unit
    return str(int(n))


# ---------- sync: your own Cloudflare page (--deploy) and the Pro cloud (--cloud)

LIVE_CFG = os.path.expanduser("~/.config/tokentrim/live.json")
CLOUD_CFG = os.path.expanduser("~/.config/tokentrim/cloud.json")
CLOUD_URL = "https://tokentrim-cloud.sunnyatlanta20.workers.dev"
REMOTE_LOG = 200


def _read(path):
    try:
        with open(path) as fh:
            return json.load(fh)
    except (OSError, ValueError):
        return None


def _write(path, cfg):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w") as fh:
        json.dump(cfg, fh, indent=2)
    os.chmod(path, 0o600)


def remote_cfg():
    return _read(LIVE_CFG)


def cloud_cfg():
    return _read(CLOUD_CFG)


def _targets():
    out = []
    cfg = remote_cfg()
    if cfg and cfg.get("url") and cfg.get("write"):
        out.append(("personal", cfg["url"].rstrip("/") + "/push", cfg["write"], bool(cfg.get("titles")), cfg.get("control", True)))
    cc = cloud_cfg()
    if cc and cc.get("key") and cc.get("enabled", True):
        out.append(("cloud", cc.get("url", CLOUD_URL).rstrip("/") + "/api/push", cc["key"], bool(cc.get("titles")), cc.get("control", True)))
    return out


def _http(url, key, body=None, timeout=10, headers=None):
    """(status, json): never raises for HTTP errors."""
    h = {"Content-Type": "application/json", "Authorization": "Bearer " + key, "User-Agent": "tokentrim-live"}
    h.update(headers or {})
    req = urllib.request.Request(url, data=body, method="POST" if body is not None else "GET", headers=h)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            raw = r.read()
            return r.status, (json.loads(raw) if raw else {})
    except urllib.error.HTTPError as e:
        try:
            return e.code, json.loads(e.read() or b"{}")
        except ValueError:
            return e.code, {}


def push(cfg, snap):
    """One-off push to your own page (kept for scripts)."""
    return _http(cfg["url"].rstrip("/") + "/push", cfg["write"], json.dumps(snap, separators=(",", ":")).encode())


def pusher(holder):
    """Sends the latest snapshot to each target: every 3 s while someone is watching, every 30 s otherwise,
    backing off on errors. Runs in its own thread so a slow network never stalls the live page."""
    state = {}
    while True:
        try:
            for name, url, key, titles, control in _targets():
                s = state.setdefault(name, {"v": -1, "at": 0.0, "next": 0.0, "fails": 0, "watching": True, "acks": []})
                now = time.time()
                snap = holder.get("snap_titles" if titles else "snap_public")
                if snap is None or now < s["next"]:
                    continue
                gap = 3 if s["watching"] else 30
                beat = 5 if s["watching"] else 60   # while watched, check in often so settings clicks land fast
                if not s["acks"] and (snap["version"] == s["v"] or now - s["at"] < gap) and now - s["at"] < beat:
                    continue
                try:
                    code, body = _http(url, key, json.dumps(snap, separators=(",", ":")).encode(),
                                       headers={"X-Ack": json.dumps(s["acks"])} if s["acks"] else None)
                except (OSError, ValueError) as e:
                    code, body = 0, {"error": str(e)[:120]}
                if code == 200:
                    s.update(v=snap["version"], at=now, fails=0, next=0, watching=body.get("watching", True), acks=[])
                    holder.setdefault("sync", {})[name] = {"ok": True, "at": now}
                    for cmd in (body.get("settings") or [])[:10]:   # clicks made on the hosted page
                        s["acks"].append(_apply_remote(cmd, name, control, holder))
                else:
                    s["fails"] += 1
                    # a lapsed license or wrong key won't fix itself in seconds
                    wait = 1800 if code in (401, 402, 403) else min(300, 5 * 2 ** min(s["fails"], 6))
                    s["next"] = now + wait
                    holder.setdefault("sync", {})[name] = {"ok": False, "at": s["at"], "code": code,
                                                           "error": body.get("error") or "HTTP %d" % code}
        except Exception as e:  # never die
            print("live sync:", e, flush=True)
        time.sleep(1)


def _apply_remote(cmd, source, control, holder):
    cid = str((cmd or {}).get("id") or "")[:16]
    if not control:
        return {"id": cid, "ok": False, "error": "remote control is off on this Mac"}
    try:
        _, done = controls.apply((cmd or {}).get("changes"), source=source)
        t = holder.get("tracker")
        if t is not None:
            t._ctl_at = 0
        return {"id": cid, "ok": True, "changed": len(done)}
    except (ValueError, OSError) as e:
        return {"id": cid, "ok": False, "error": str(e)[:150]}


def run(tracker, holder, interval=2.0):
    """Background loop used by `tokentrim serve`: tick, keep the latest snapshots for the page and for sync."""
    holder["tracker"] = tracker
    threading.Thread(target=pusher, args=(holder,), daemon=True).start()
    last_v, last_build = -1, 0.0
    while True:
        try:
            tracker.tick()
            snap = tracker.snapshot()
            snap["sync"] = holder.get("sync") or {}
            snap["errors"] = list(tracker.errors)
            holder["snap"] = snap
            targets = _targets()
            now = time.time()
            if targets and (tracker.version != last_v or now - last_build >= 50):
                if any(t[3] for t in targets):
                    holder["snap_titles"] = tracker.snapshot(titles=True, log_rows=REMOTE_LOG)
                if any(not t[3] for t in targets):
                    holder["snap_public"] = tracker.snapshot(titles=False, log_rows=REMOTE_LOG)
                last_v, last_build = tracker.version, now
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
    _write(LIVE_CFG, cfg)
    print("deployed. `tokentrim serve` (already running at login if installed) now pushes totals every few seconds.")
    link()


def cloud(key=None, titles=False, off=False, rotate=False, control=True):
    """Pro: the hosted real-time dashboard. Your license key is the login; nothing to deploy."""
    if off:
        if os.path.exists(CLOUD_CFG):
            os.remove(CLOUD_CFG)
        return print("Pro cloud sync is off for this Mac.")
    from . import pro
    if not key:
        key = ((pro._load() or {}).get("key")) or (cloud_cfg() or {}).get("key")
    if not key:
        sys.exit("The hosted dashboard is part of tokentrim Pro.\n  Get it: %s\n  Then: tokentrim pro activate <key> && tokentrim live --cloud" % pro.BUY_URL)
    url = (cloud_cfg() or {}).get("url") or config.load().get("cloud_url") or CLOUD_URL
    code, body = _http(url.rstrip("/") + ("/api/rotate" if rotate else "/api/link"), key, b"{}", timeout=20)
    if code == 402:
        sys.exit("That license isn't active (%s). Check it in your Polar receipt, or email support@boltak.com."
                 % (body.get("error") or "not granted"))
    if code != 200 or not body.get("view"):
        sys.exit("Couldn't reach the Pro cloud (%s). Try again in a minute." % (body.get("error") or "HTTP %d" % code))
    _write(CLOUD_CFG, {"url": url, "key": key, "view": body["view"], "titles": titles, "enabled": True, "control": control})
    print("Pro cloud on. `tokentrim serve` syncs totals every few seconds while you watch (titles %s; "
          "settings changes from the page %s)." % ("included" if titles else "never sent", "allowed" if control else "off"))
    link()


def link():
    cfg, cc = remote_cfg(), cloud_cfg()
    if not (cfg and cfg.get("url")) and not (cc and cc.get("view")):
        sys.exit("not set up yet: tokentrim live --cloud (Pro) or tokentrim live --deploy (your own Cloudflare)")
    if cc and cc.get("view"):
        print("Pro dashboard (open once per device, then bookmark; or sign in there with your license key):\n  %s/#v=%s"
              % (cc.get("url", CLOUD_URL).rstrip("/"), cc["view"]))
    if cfg and cfg.get("url"):
        print("your own page:\n  %s/#k=%s" % (cfg["url"], cfg["read"]))
