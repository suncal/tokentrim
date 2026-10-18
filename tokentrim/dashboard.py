"""tokentrim dashboard: one self-contained HTML page with everything about your token use.

Reads the same local transcripts as `audit` (plus sub-agent transcripts, which
the audit skips), the hook's savings log, and a replay of your sessions. Writes
dashboard.html next to this repo and opens it. Nothing leaves the machine.
"""
import bisect
import datetime as dt
import glob
import json
import os
import re
import statistics
import time
import webbrowser
from collections import defaultdict

from . import audit as audit_mod
from . import cli, config, replay as replay_mod

PROJECTS = audit_mod.PROJECTS
STATE = os.path.expanduser("~/.cache/tokentrim")
TEMPLATE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "dashboard_template.html")
SERIES_MAX = 1500  # context points kept per session for the chart


def _ts(s):
    try:
        return dt.datetime.fromisoformat(s.replace("Z", "+00:00")).timestamp()
    except (AttributeError, ValueError):
        return None


def _project(dirname):
    name = dirname.lstrip("-")
    if "scratchpad" in name or name.startswith("private-tmp"):
        return "(scratch / headless)"
    name = re.sub(r"^Users-[^-]+-?", "", name) or "~"
    return name.replace("-", "/")


def _scan(path):
    """Per-call usage with timestamps and model, plus compactions and a title."""
    calls, seen, compactions = [], set(), []
    title = first_prompt = None
    with open(path, errors="replace") as fh:
        for line in fh:
            try:
                d = json.loads(line)
            except ValueError:
                continue
            t = d.get("type")
            if t == "assistant":
                m = d.get("message") or {}
                u, mid = m.get("usage"), m.get("id")
                if u and mid not in seen:
                    seen.add(mid)
                    calls.append((_ts(d.get("timestamp")), m.get("model") or "?",
                                  u.get("cache_read_input_tokens", 0) or 0,
                                  u.get("cache_creation_input_tokens", 0) or 0,
                                  u.get("input_tokens", 0) or 0,
                                  u.get("output_tokens", 0) or 0))
            elif t == "user":
                if d.get("isCompactSummary"):
                    compactions.append(len(calls))
                elif first_prompt is None and not d.get("isMeta"):
                    c = (d.get("message") or {}).get("content")
                    if isinstance(c, list):
                        c = " ".join(p.get("text", "") for p in c if isinstance(p, dict) and p.get("type") == "text")
                    if isinstance(c, str) and c.strip() and not c.lstrip().startswith("<"):
                        first_prompt = c.strip()[:120]
            elif t == "custom-title":
                title = d.get("customTitle") or title
            elif t == "system" and d.get("subtype") == "compact_boundary":
                compactions.append(len(calls))
    return calls, sorted(set(compactions)), title or first_prompt or ""


def _install_ts(settings):
    marker = settings.get("tokentrim") or {}
    if marker.get("installed_ts"):
        return marker["installed_ts"]
    backups = sorted(glob.glob(cli.SETTINGS + ".bak-tokentrim-*"))
    for b in backups:
        m = re.search(r"(\d{8}-\d{6})$", b)
        if m:
            return time.mktime(time.strptime(m.group(1), "%Y%m%d-%H%M%S"))
    if marker.get("installed"):
        return time.mktime(time.strptime(marker["installed"], "%Y-%m-%d"))
    return None


def _load_savings():
    rows = []
    try:
        with open(os.path.join(STATE, "savings.jsonl")) as fh:
            for line in fh:
                try:
                    rows.append(json.loads(line))
                except ValueError:
                    pass
    except OSError:
        pass
    return rows


def _day(ts):
    return time.strftime("%Y-%m-%d", time.localtime(ts)) if ts else "unknown"


def build():
    t0 = time.time()
    settings = cli._load_settings()
    install_ts = _install_ts(settings)
    window = int((settings.get("env") or {}).get("CLAUDE_CODE_AUTO_COMPACT_WINDOW") or 0) or None

    savings = _load_savings()
    sav_by_session = defaultdict(list)
    for r in savings:
        sav_by_session[r.get("session") or ""].append(r)

    sessions, daily = [], defaultdict(lambda: defaultdict(lambda: [0, 0, 0, 0, 0]))
    phase = {k: {"calls": 0, "ctx": [], "cr": 0, "cw": 0, "inp": 0, "out": 0, "compactions": 0, "sessions": set()}
             for k in ("before", "after")}

    for path in glob.glob(os.path.join(PROJECTS, "*", "*.jsonl")):
        sid = os.path.basename(path)[:-6]
        calls, comps, title = _scan(path)
        sub = [0, 0, 0, 0, 0]  # calls, cr, cw, inp, out
        sub_files = glob.glob(os.path.join(os.path.dirname(path), sid, "**", "*.jsonl"), recursive=True)
        sub_calls = []
        for sp in sub_files:
            sc, _, _ = _scan(sp)
            sub_calls += sc
        for c in sub_calls:
            sub[0] += 1
            for i in range(4):
                sub[i + 1] += c[2 + i]
        if not calls and not sub_calls:
            continue
        ctx = [c[2] + c[3] + c[4] for c in calls]
        times = [c[0] or 0 for c in calls]
        for c in calls + sub_calls:
            row = daily[_day(c[0])][c[1]]
            row[0] += c[2]; row[1] += c[3]; row[2] += c[4]; row[3] += c[5]; row[4] += 1
        for i, c in enumerate(calls):
            k = "after" if install_ts and c[0] and c[0] >= install_ts else "before"
            p = phase[k]
            p["calls"] += 1
            p["ctx"].append(ctx[i])
            p["cr"] += c[2]; p["cw"] += c[3]; p["inp"] += c[4]; p["out"] += c[5]
            p["sessions"].add(sid)
            if i in comps:
                p["compactions"] += 1

        # tokentrim savings in this session, weighted by how many later calls would have re-read them
        direct = life = 0
        for r in sav_by_session.get(sid, []):
            saved = (r.get("before") or 0) - (r.get("after") or 0)
            idx = bisect.bisect_left(times, r.get("ts") or 0)
            rr = audit_mod.rereads(idx, len(calls), comps) if calls else 0
            r["life"] = saved * max(rr, 1)
            direct += saved
            life += r["life"]

        step = max(1, len(ctx) // SERIES_MAX)
        models = defaultdict(int)
        for c in calls:
            models[c[1]] += 1
        sessions.append({
            "id": sid, "title": title, "project": _project(os.path.basename(os.path.dirname(path))),
            "start": times[0] if calls else (sub_calls[0][0] or 0),
            "end": max(times) if calls else 0,
            "model": max(models, key=models.get) if models else "?",
            "calls": len(calls), "cr": sum(c[2] for c in calls), "cw": sum(c[3] for c in calls),
            "inp": sum(c[4] for c in calls), "out": sum(c[5] for c in calls),
            "peak": max(ctx) if ctx else 0, "avg": int(sum(ctx) / len(ctx)) if ctx else 0,
            "first": ctx[0] if ctx else 0, "compactions": len(comps), "sub": sub, "subagents": len(sub_files),
            "saved": int(direct), "saved_life": int(life),
            "after": bool(install_ts and calls and (times[-1] or 0) >= install_ts),
            "series": ctx[::step], "series_step": step,
            "comps": [i // step for i in comps],
        })

    # where context comes from (the audit's lifetime-weighted attribution)
    tot, by_src = audit_mod.audit()
    sources = sorted(({"name": k, "count": v["count"], "raw": v["tokens"], "images": v["images"],
                       "weighted": v["weighted"]} for k, v in by_src.items()), key=lambda s: -s["weighted"])
    cr_all = max(tot["cache_read"], 1)
    listed = sum(s["weighted"] for s in sources)
    attribution = {"fixed": tot["fixed_weighted"] / cr_all, "listed": listed / cr_all,
                   "other": max(0.0, 1 - (listed + tot["fixed_weighted"]) / cr_all)}

    # replay once, simulate every profile
    cfg = config.load()
    rp = {"orig": 0, "trimmed": 0}
    for name in cli.PROFILES:
        rp[name] = 0
    for p in glob.glob(os.path.join(PROJECTS, "*", "*.jsonl")):
        calls, events = replay_mod.replay_session(p, cfg)
        if not calls:
            continue
        a, b, _ = replay_mod.simulate(calls, events, None)
        rp["orig"] += a
        rp["trimmed"] += b
        for name, w in cli.PROFILES.items():
            rp[name] += replay_mod.simulate(calls, events, w)[2]

    by_rule = defaultdict(lambda: {"count": 0, "before": 0, "after": 0, "life": 0, "est": False})
    sav_daily = defaultdict(lambda: [0, 0])
    for r in savings:
        b = by_rule[r.get("rule") or "?"]
        b["count"] += 1
        b["before"] += r.get("before") or 0
        b["after"] += r.get("after") or 0
        b["life"] += r.get("life", (r.get("before") or 0) - (r.get("after") or 0))
        b["est"] = b["est"] or bool(r.get("est"))
        d = sav_daily[_day(r.get("ts"))]
        d[0] += (r.get("before") or 0) - (r.get("after") or 0)
        d[1] += r.get("life", 0)

    def summarise(p):
        c = sorted(p["ctx"])
        n = max(p["calls"], 1)
        return {"calls": p["calls"], "sessions": len(p["sessions"]),
                "avg_ctx": int(sum(c) / n) if c else 0, "median_ctx": int(statistics.median(c)) if c else 0,
                "p90_ctx": c[int(len(c) * 0.9)] if c else 0,
                "over_150k": sum(1 for x in c if x > 150_000) / n,
                "cr": p["cr"], "cw": p["cw"], "inp": p["inp"], "out": p["out"],
                "compactions_per_100": 100.0 * p["compactions"] / n,
                "per_call": (p["cr"] + p["cw"] + p["inp"] + p["out"]) / n}

    fixed_rows = []
    for path, label in ((cli.CLAUDE_MD, "~/.claude/CLAUDE.md"),):
        if os.path.exists(path):
            fixed_rows.append([label, os.path.getsize(path) // 4])
    for p in glob.glob(os.path.join(PROJECTS, "*", "memory", "MEMORY.md")):
        fixed_rows.append(["memory index (" + _project(p.split(os.sep)[-3]) + ")", os.path.getsize(p) // 4])
    sk = os.path.expanduser("~/.claude/skills")
    if os.path.isdir(sk):
        n = 0
        for name in os.listdir(sk):
            f = os.path.join(sk, name, "SKILL.md")
            if os.path.exists(f):
                with open(f, errors="replace") as fh:
                    head = fh.read(3000)
                desc = head.split("description:", 1)[-1].split("\n---", 1)[0] if "description:" in head else ""
                n += len(desc) // 4 + 10
        fixed_rows.append(["skill descriptions (~/.claude/skills)", n])

    sessions.sort(key=lambda s: -(s["start"] or 0))
    return {
        "generated": time.time(), "build_seconds": 0, "install_ts": install_ts,
        "profile": (settings.get("tokentrim") or {}).get("profile"), "window": window,
        "sessions": sessions,
        "daily": {d: dict(v) for d, v in sorted(daily.items())},
        "sources": sources[:40], "attribution": attribution,
        "baseline_median": tot["baseline_median"], "fixed_rows": fixed_rows,
        "replay": rp, "profiles": cli.PROFILES,
        "phase": {k: summarise(v) for k, v in phase.items()},
        "savings": {"by_rule": by_rule, "daily": dict(sorted(sav_daily.items())),
                    "recent": sorted(savings, key=lambda r: -(r.get("ts") or 0))[:300],
                    "total_events": len(savings)},
        "build_seconds_": round(time.time() - t0, 1),
    }


def write(out=None, open_it=True):
    data = build()
    out = out or os.path.join(cli.ROOT, "dashboard.html")
    with open(TEMPLATE) as fh:
        html = fh.read()
    blob = json.dumps(data, separators=(",", ":")).replace("</", "<\\/")
    with open(out, "w") as fh:
        fh.write(html.replace("/*__DATA__*/null", blob))
    print("dashboard -> %s  (%d sessions, built in %.0fs)" % (out, len(data["sessions"]), data["build_seconds_"]))
    if open_it:
        webbrowser.open("file://" + out)
    return out
