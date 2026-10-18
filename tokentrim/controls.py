"""Settings the live dashboard can change: which rules run, the compaction profile, the CLAUDE.md guidance.

Only the keys in SPEC can be changed, each with a fixed type, so a dashboard (local, or the cloud relaying a
click through your Mac) can never write anything else. Rule switches take effect on the next tool call;
profile and guidance changes apply to new Claude Code sessions. Every change is logged to CHANGES.
"""
import glob
import json
import os
import re
import subprocess
import time

from . import cli, config

CHANGES = os.path.expanduser("~/.cache/tokentrim/controls.jsonl")

# key -> (kind, label, what it does). Rule keys are read by hook.py.
SPEC = {
    "enabled": ("bool", "tokentrim", "Master switch for every hook. Off = Claude Code runs untouched."),
    "rule_screenshots": ("bool", "Smaller screenshots", "Browser screenshots at 40% scale (~1/6 the tokens); repeats with no action in between are skipped."),
    "rule_images": ("bool", "Downscale images", "Images opened with Read are shrunk to 1024px first."),
    "rule_outline": ("bool", "Outline big files", "Whole-file reads over 40 KB return an outline first; asking again gets everything."),
    "rule_bash": ("bool", "Trim long command output", "Over 6k chars keeps head + tail; the full output is saved to disk."),
    "rule_mcp": ("bool", "Shrink MCP results", "Minify JSON, drop empty fields and base64 blobs."),
    "mcp_lossless_only": ("bool", "MCP: never clip", "Only minify MCP results, never shorten them."),
    "rule_meter": ("bool", "Context meter", "Tells you (not Claude) when a conversation passes 120k tokens."),
    "profile": ("choice", "Summary point", "When Claude Code summarises old turns. Earlier = fewer tokens re-read every step."),
    "guidance": ("bool", "CLAUDE.md guidance", "~110 tokens of habits: page text over screenshots, Edit over Write, ranged reads."),
    # smart features: Claude Code settings tokentrim manages for you (your own values come back when you switch off)
    "model_mode": ("choice", "Smart model", "Auto = Opus while planning, Sonnet while doing the work (Claude Code's opusplan). "
                   "Sonnet = everything on Sonnet. Off = your own model choice."),
    "subagent_model": ("choice", "Sub-agent model", "Sub-agents (Explore, Task) run on a cheaper model; your main conversation keeps its own."),
    "effort": ("choice", "Thinking effort", "Lower effort spends fewer thinking tokens on routine work. Off = Claude Code's default."),
    "cache_ttl": ("choice", "Cache lifetime", "1 hour keeps your context cached through breaks (cache writes cost 2x instead of 1.25x). "
                  "For API-key users who pause 5-60 min; a Claude subscription already gets 1 hour."),
    # guards: run in tokentrim's prompt hook
    "budget_guard": ("bool", "Daily budget guard", "Tells you at 80% of your daily budget, and again when you pass it."),
    "budget_usd": ("choice", "Daily budget", "Usage per day at API prices, across every session."),
    "budget_block": ("bool", "Hard stop at budget", "Hold new prompts once you pass the budget. Start a message with ++ to send it anyway."),
    "cache_guard": ("bool", "Cold-cache alert", "Back after the cache expired? Tells you what your next message will re-write and when /clear is cheaper."),
    "model_advisor": ("bool", "Model advisor", "Spots quick jobs (renames, formatting, git chores) running on Opus and tells you Sonnet does them cheaper."),
}
CHOICES = {"profile": list(cli.PROFILES), "model_mode": ["off", "auto", "sonnet"], "subagent_model": ["off", "sonnet", "haiku"],
           "effort": ["off", "medium", "low"], "cache_ttl": ["off", "1h"], "budget_usd": [5, 10, 20, 30, 50, 100, 200]}
LIVE = {"enabled", "rule_screenshots", "rule_images", "rule_outline", "rule_bash", "rule_mcp", "mcp_lossless_only", "rule_meter",
        "budget_guard", "budget_usd", "budget_block", "cache_guard", "model_advisor"}
# dashboard key -> (where in settings.json, choice -> value written). "env." = inside the env block.
CLAUDE = {"model_mode": ("model", {"auto": "opusplan", "sonnet": "sonnet"}),
          "subagent_model": ("env.CLAUDE_CODE_SUBAGENT_MODEL", {"sonnet": "sonnet", "haiku": "haiku"}),
          "effort": ("effortLevel", {"medium": "medium", "low": "low"}),
          "cache_ttl": ("promptCacheTtl", {"1h": "1h"})}
# oldest Claude Code that reads each setting (from its changelog); older versions silently ignore it
MIN_VERSION = {"model_mode": "1.0.77", "subagent_model": "2.1.147", "effort": "2.1.203", "cache_ttl": "2.1.243"}


def _ver(v):
    return tuple(int(x) for x in re.findall(r"\d+", v or "")[:3])


_versions = {"at": 0.0, "v": {}}


def claude_versions():
    """{"app": newest Claude Code bundled with the desktop app, "cli": the `claude` on PATH}; cached 10 min."""
    if time.time() - _versions["at"] < 600:
        return _versions["v"]
    v = {}
    app = sorted(glob.glob(os.path.expanduser("~/Library/Application Support/Claude/claude-code/[0-9]*")), key=lambda p: _ver(os.path.basename(p)))
    if app:
        v["app"] = os.path.basename(app[-1])
    for exe in ("claude", os.path.expanduser("~/.local/bin/claude"), os.path.expanduser("~/.claude/local/claude")):
        try:
            out = subprocess.run([exe, "--version"], capture_output=True, text=True, timeout=5).stdout
        except (OSError, subprocess.SubprocessError):
            continue
        m = re.search(r"\d+\.\d+\.\d+", out)
        if m:
            v["cli"] = m.group(0)
            break
    _versions.update(at=time.time(), v=v)
    return v


def support(versions):
    """setting -> None (every Claude Code here reads it) or a note naming what's too old."""
    out = {}
    for k, need in MIN_VERSION.items():
        old = [("the desktop app" if w == "app" else "your terminal `claude`") + " (" + v + ")"
               for w, v in sorted(versions.items()) if _ver(v) < _ver(need)]
        if not versions:
            out[k] = None
        elif len(old) == len(versions):
            out[k] = "Needs Claude Code %s or newer; %s ignores it. Run `claude update`." % (need, " and ".join(old))
        elif old:
            ok = [("the desktop app" if w == "app" else "your terminal `claude`") + " (" + v + ")"
                  for w, v in sorted(versions.items()) if _ver(v) >= _ver(need)]
            out[k] = "Works in %s; %s ignores it until you run `claude update`." % (ok[0], old[0])
        else:
            out[k] = None
    return out


def _get(s, where):
    if where.startswith("env."):
        return (s.get("env") or {}).get(where[4:])
    return s.get(where)


def _put(s, where, value):   # value None removes the key
    box, name = (s.setdefault("env", {}), where[4:]) if where.startswith("env.") else (s, where)
    if value is None:
        box.pop(name, None)
    else:
        box[name] = value
    if where.startswith("env.") and not s["env"]:
        s.pop("env")


def _claude_set(s, key, choice):
    """Write one smart setting into settings.json, remembering the user's own value the first time."""
    where, values = CLAUDE[key]
    prev = s.setdefault("tokentrim", {}).setdefault("prev", {})
    if choice == "off":
        mine = _get(s, where) in values.values()
        if where in prev:
            back = prev.pop(where)
            if mine:   # only undo our own value: a model picked later with /model stays
                _put(s, where, back)
        elif mine:
            _put(s, where, None)
        if not prev:
            s["tokentrim"].pop("prev")
        return
    if where not in prev:
        prev[where] = _get(s, where)
    _put(s, where, values[choice])


def _guidance_on():
    try:
        with open(cli.CLAUDE_MD) as fh:
            return cli.GUIDANCE_START in fh.read()
    except OSError:
        return False


def state():
    cfg = config.load()
    s = cli._load_settings()
    marker = s.get("tokentrim")
    window = (s.get("env") or {}).get("CLAUDE_CODE_AUTO_COMPACT_WINDOW")
    values = {k: bool(cfg.get(k, True)) for k in LIVE}
    values["mcp_lossless_only"] = bool(cfg.get("mcp_lossless_only", False))
    values["profile"] = (marker or {}).get("profile")
    values["guidance"] = _guidance_on()
    values["budget_usd"] = cfg.get("budget_usd", 20)
    for k, (where, vals) in CLAUDE.items():
        cur = _get(s, where)
        values[k] = next((c for c, v in vals.items() if v == cur), "off")
    return {
        "installed": marker is not None,
        "window": int(window) if str(window or "").isdigit() else None,
        "profiles": {k: v for k, v in cli.PROFILES.items()},
        "values": values,
        "claude": {"model": s.get("model"), "effort": s.get("effortLevel"), "versions": claude_versions()},
        "support": support(claude_versions()),
        "spec": {k: dict({"kind": v[0], "label": v[1], "help": v[2], "live": k in LIVE}, **({"choices": CHOICES[k]} if k in CHOICES else {}))
                 for k, v in SPEC.items()},
    }


def validate(changes):
    """-> clean dict of allowed changes; raises ValueError on anything else."""
    if not isinstance(changes, dict) or not changes:
        raise ValueError("nothing to change")
    clean = {}
    for k, v in changes.items():
        if k not in SPEC:
            raise ValueError("unknown setting: %s" % k)
        kind = SPEC[k][0]
        if kind == "bool" and isinstance(v, bool):
            clean[k] = v
        elif kind == "choice" and v in CHOICES[k] and not isinstance(v, bool):
            clean[k] = v
        else:
            raise ValueError("bad value for %s" % k)
    return clean


def _save_config(updates):
    try:
        with open(config.PATH) as fh:
            cur = json.load(fh)
        if not isinstance(cur, dict):
            cur = {}
    except (OSError, ValueError):
        cur = {}
    cur.update(updates)
    os.makedirs(os.path.dirname(config.PATH), exist_ok=True)
    tmp = config.PATH + ".tmp"
    with open(tmp, "w") as fh:
        json.dump(cur, fh, indent=2)
    os.replace(tmp, config.PATH)


def apply(changes, source="local"):
    """Apply validated changes -> (new state, list of {key, old, new})."""
    clean = validate(changes)
    before = state()
    old = before["values"]
    if ("profile" in clean or set(clean) & set(CLAUDE)) and not before["installed"]:
        raise ValueError("tokentrim isn't installed; run `tokentrim install` first")
    done = []
    live = {k: v for k, v in clean.items() if k in LIVE and v != old.get(k)}
    if live:
        _save_config(live)
    if "profile" in clean and clean["profile"] != old.get("profile"):
        s = cli._load_settings()
        s.setdefault("env", {})["CLAUDE_CODE_AUTO_COMPACT_WINDOW"] = str(cli.PROFILES[clean["profile"]])
        s.setdefault("tokentrim", {})["profile"] = clean["profile"]
        cli._save_settings(s, rolling=True)
    smart = {k: v for k, v in clean.items() if k in CLAUDE and v != old.get(k)}
    if smart:
        s = cli._load_settings()
        for k, v in smart.items():
            _claude_set(s, k, v)
        cli._save_settings(s, rolling=True)
    if "guidance" in clean and clean["guidance"] != old.get("guidance"):
        cli._write_guidance(clean["guidance"])
    now = time.time()
    for k, v in clean.items():
        if v != old.get(k):
            done.append({"key": k, "old": old.get(k), "new": v})
    if done:
        os.makedirs(os.path.dirname(CHANGES), exist_ok=True)
        with open(CHANGES, "a") as fh:
            for d in done:
                fh.write(json.dumps(dict(d, ts=now, source=source)) + "\n")
    return state(), done
