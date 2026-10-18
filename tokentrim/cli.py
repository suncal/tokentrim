"""tokentrim — cut what Claude Code re-reads on every step.

  tokentrim audit                 where your tokens went (from local transcripts)
  tokentrim replay [--window N]   what tokentrim would have saved on those sessions
  tokentrim fixed                 what loads into every session before you type
  tokentrim install [--profile P] add hooks + compaction window to ~/.claude/settings.json
  tokentrim uninstall             remove exactly what install added
  tokentrim status                what is installed, and recent hook activity
  tokentrim dashboard [--no-open] full usage + savings dashboard (dashboard.html)
  tokentrim serve [--port N]      live dashboard at http://localhost:4747 (auto-refreshing)
  tokentrim serve --install       keep it running at login; --uninstall removes that
  tokentrim live                  real-time savings page (http://localhost:4747/live)
  tokentrim live --cloud          Pro: hosted real-time dashboard, sign in with your license key
                                  (--titles to include chat titles, --no-control: page can't change settings,
                                   --rotate: new link, old one stops working, `--cloud off` to stop)
  tokentrim live --deploy         also put it on your Cloudflare account, to open from anywhere
  tokentrim live --link           print the private link to the remote page
  tokentrim share [--out DIR]     savings card to post (totals only, no prompts or paths)

Pro (https://github.com/suncal/tokentrim#pro):
  tokentrim pro [activate KEY | deactivate]   license status / activate / free this seat
  tokentrim digest [--install]    plain-English weekly report (+ Slack); --install: digest Mondays,
                                  budget check every day at 9am
  tokentrim budget 2B|off         weekly token budget; notification at 80% (sets up the daily check)
  tokentrim slack URL|off         post weekly totals to a Slack incoming webhook (test message first)
"""
import json
import os
import shutil
import sys
import time

SETTINGS = os.path.expanduser("~/.claude/settings.json")
CLAUDE_MD = os.path.expanduser("~/.claude/CLAUDE.md")
ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
TAG = "tokentrim"

# Compaction window (tokens). Compaction fires at ~75% of it (measured).
# Smaller = cheaper sessions, but Claude forgets detail sooner, and below ~150k
# a session with big file reads can compact over and over.
PROFILES = {"balanced": 300_000, "strong": 200_000, "max": 150_000}

GUIDANCE_START = "<!-- tokentrim:start -->"
GUIDANCE_END = "<!-- tokentrim:end -->"
GUIDANCE = """%s
# Token discipline (tokentrim)
- Browser: read pages with get_page_text / read_page / find; take a screenshot only to check visuals.
- Change files with Edit, not by rewriting them with Write; generate big files with a script.
- Read large files in ranges (offset/limit) after looking at their outline; don't cat whole files.
- Pipe long command output through head/tail/grep; long output is trimmed and spooled to disk anyway.
- One task per session: suggest a fresh session when the topic changes.
%s""" % (GUIDANCE_START, GUIDANCE_END)


def _hook_cmd(event):
    return '/usr/bin/env python3 "%s" %s' % (os.path.join(ROOT, "tokentrim_hook.py"), event)


def _entries():
    return {
        "PreToolUse": {"matcher": "Read|mcp__.*(Claude_Browser|claude-in-chrome|Claude_in_Chrome|Claude_Preview).*",
                       "hooks": [{"type": "command", "command": _hook_cmd("pre"), "timeout": 20}]},
        "PostToolUse": {"matcher": "Bash|mcp__.*",
                        "hooks": [{"type": "command", "command": _hook_cmd("post"), "timeout": 10}]},
        "UserPromptSubmit": {"matcher": "",
                             "hooks": [{"type": "command", "command": _hook_cmd("prompt"), "timeout": 5}]},
    }


def _is_ours(entry):
    return any("tokentrim_hook.py" in (h.get("command") or "") for h in entry.get("hooks", []))


def _plugin_enabled(s):
    """The tokentrim Claude Code plugin is enabled in user settings."""
    return any(k.split("@")[0] == TAG and v for k, v in (s.get("enabledPlugins") or {}).items())


def _load_settings():
    try:
        with open(SETTINGS) as fh:
            return json.load(fh)
    except FileNotFoundError:
        return {}


def _save_settings(s, rolling=False):
    """rolling: dashboard clicks keep one backup that each change replaces, instead of a new file per click."""
    if os.path.exists(SETTINGS):
        if rolling:
            shutil.copy2(SETTINGS, SETTINGS + ".bak-tokentrim-dashboard")
        else:
            backup = "%s.bak-tokentrim-%s" % (SETTINGS, time.strftime("%Y%m%d-%H%M%S"))
            shutil.copy2(SETTINGS, backup)
            print("  backup     ->", backup)
    os.makedirs(os.path.dirname(SETTINGS), exist_ok=True)
    tmp = SETTINGS + ".tmp"
    with open(tmp, "w") as fh:
        json.dump(s, fh, indent=2)
        fh.write("\n")
    os.replace(tmp, SETTINGS)


def install(profile="strong", guidance=True):
    s = _load_settings()
    hooks = s.setdefault("hooks", {})
    for event, entry in _entries().items():
        lst = [e for e in hooks.get(event, []) if not _is_ours(e)]
        lst.append(entry)
        hooks[event] = lst
    env = s.setdefault("env", {})
    first_install = "tokentrim" not in s
    marker = s.setdefault("tokentrim", {})
    if first_install and "CLAUDE_CODE_AUTO_COMPACT_WINDOW" in env:
        marker["prev_window"] = env["CLAUDE_CODE_AUTO_COMPACT_WINDOW"]
    env["CLAUDE_CODE_AUTO_COMPACT_WINDOW"] = str(PROFILES[profile])
    marker["profile"] = profile
    marker["installed"] = time.strftime("%Y-%m-%d")
    marker.setdefault("installed_ts", time.time())
    _save_settings(s)
    print("  hooks      -> PreToolUse, PostToolUse, UserPromptSubmit")
    print("  compaction -> CLAUDE_CODE_AUTO_COMPACT_WINDOW=%d (%s)" % (PROFILES[profile], profile))
    if guidance:
        _write_guidance(True)
        print("  guidance   ->", CLAUDE_MD, "(~110 tokens)")
    if _plugin_enabled(s):
        print("  plugin     -> tokentrim plugin is also enabled; its hooks stand down while these are installed")
    print("\nNew sessions pick this up. Already-open sessions keep their old settings.")


def _write_guidance(on):
    text = ""
    if os.path.exists(CLAUDE_MD):
        with open(CLAUDE_MD) as fh:
            text = fh.read()
    if GUIDANCE_START in text:
        a = text.index(GUIDANCE_START)
        b = text.index(GUIDANCE_END) + len(GUIDANCE_END)
        text = (text[:a] + text[b:]).strip("\n")
    if on:
        text = (text.rstrip("\n") + "\n\n" + GUIDANCE + "\n").lstrip("\n")
    with open(CLAUDE_MD, "w") as fh:
        fh.write(text + ("\n" if text and not text.endswith("\n") else ""))


def uninstall():
    s = _load_settings()
    for event in list((s.get("hooks") or {}).keys()):
        s["hooks"][event] = [e for e in s["hooks"][event] if not _is_ours(e)]
        if not s["hooks"][event]:
            del s["hooks"][event]
    marker = s.pop("tokentrim", {}) or {}
    for where, value in (marker.get("prev") or {}).items():   # smart settings: put the user's own values back
        if where.startswith("env."):
            if value is None:
                (s.get("env") or {}).pop(where[4:], None)
            else:
                s.setdefault("env", {})[where[4:]] = value
        elif value is None:
            s.pop(where, None)
        else:
            s[where] = value
    env = s.get("env") or {}
    if "prev_window" in marker:
        env["CLAUDE_CODE_AUTO_COMPACT_WINDOW"] = marker["prev_window"]
    else:
        env.pop("CLAUDE_CODE_AUTO_COMPACT_WINDOW", None)
    if not env:
        s.pop("env", None)
    _save_settings(s)
    _write_guidance(False)
    print("tokentrim removed. (Spooled outputs remain in ~/.cache/tokentrim; delete them if you like.)")


def status():
    s = _load_settings()
    ours = [ev for ev, lst in (s.get("hooks") or {}).items() if any(_is_ours(e) for e in lst)]
    print("installed hooks:", ", ".join(ours) or "none")
    if _plugin_enabled(s):
        print("plugin: enabled" + (" (standing down: settings hooks above run instead)" if ours else " (its hooks are active)"))
    print("compaction window:", (s.get("env") or {}).get("CLAUDE_CODE_AUTO_COMPACT_WINDOW", "default"))
    print("profile:", (s.get("tokentrim") or {}).get("profile", "-"))
    log = os.path.expanduser("~/.cache/tokentrim/events.log")
    if os.path.exists(log):
        with open(log) as fh:
            lines = fh.read().splitlines()
        print("\nlast hook events (%d total):" % len(lines))
        for l in lines[-10:]:
            print("  " + l[:150])


def fixed():
    """Rough size of what every session loads that you control."""
    tok = lambda n: n // 4
    rows = []
    for path in (CLAUDE_MD, os.path.join(os.getcwd(), "CLAUDE.md")):
        if os.path.exists(path):
            rows.append(("CLAUDE.md " + path.replace(os.path.expanduser("~"), "~"), tok(os.path.getsize(path))))
    mem_root = os.path.expanduser("~/.claude/projects")
    if os.path.isdir(mem_root):
        for d in os.listdir(mem_root):
            p = os.path.join(mem_root, d, "memory", "MEMORY.md")
            if os.path.exists(p):
                rows.append(("memory index " + d[:40], tok(os.path.getsize(p))))
    sk = os.path.expanduser("~/.claude/skills")
    if os.path.isdir(sk):
        total = 0
        for name in os.listdir(sk):
            f = os.path.join(sk, name, "SKILL.md")
            if os.path.exists(f):
                with open(f, errors="replace") as fh:
                    head = fh.read(3000)
                desc = head.split("description:", 1)[-1].split("\n---", 1)[0] if "description:" in head else ""
                total += tok(len(desc)) + 10
        rows.append(("skill descriptions (~/.claude/skills, %d)" % len(os.listdir(sk)), total))
    s = _load_settings()
    for ev in ("SessionStart", "UserPromptSubmit"):
        for e in (s.get("hooks") or {}).get(ev, []):
            for h in e.get("hooks", []):
                rows.append(("%s hook: %s" % (ev, (h.get("command") or "")[:50]), "output varies"))
    print("Loaded into every session (that you control):")
    for name, n in rows:
        print("  %-70s %s" % (name, n if isinstance(n, str) else "~{:,} tokens".format(n)))
    print("\nThe rest (system prompt, tool schemas, app instructions) is set by Claude Code itself.")


def main(argv=None):
    argv = list(sys.argv[1:] if argv is None else argv)
    cmd = argv.pop(0) if argv else "help"
    if cmd == "audit":
        from .audit import audit, report
        print(report(*audit()))
    elif cmd == "replay":
        from .replay import replay, report
        window = int(argv[argv.index("--window") + 1]) if "--window" in argv else PROFILES["strong"]
        T, by = replay(window)
        print(report(T, by, window))
    elif cmd == "install":
        profile = argv[argv.index("--profile") + 1] if "--profile" in argv else "strong"
        if profile not in PROFILES:
            sys.exit("profile must be one of: " + ", ".join(PROFILES))
        install(profile, guidance="--no-guidance" not in argv)
    elif cmd == "uninstall":
        uninstall()
    elif cmd == "status":
        status()
    elif cmd == "dashboard":
        from .dashboard import write
        write(open_it="--no-open" not in argv)
    elif cmd == "serve":
        from . import dashboard
        port = int(argv[argv.index("--port") + 1]) if "--port" in argv else dashboard.PORT
        if "--install" in argv:
            dashboard.install_agent(port)
        elif "--uninstall" in argv:
            dashboard.uninstall_agent()
        else:
            dashboard.serve(port, open_it="--no-open" not in argv)
    elif cmd == "live":
        from . import live
        if "--cloud" in argv:
            key = argv[argv.index("--key") + 1] if "--key" in argv else None
            live.cloud(key, titles="--titles" in argv, off="off" in argv, rotate="--rotate" in argv,
                       control="--no-control" not in argv)
        elif "--deploy" in argv:
            live.deploy(titles="--titles" in argv)
        elif "--link" in argv:
            live.link()
        else:
            import webbrowser
            webbrowser.open("http://localhost:4747/live")
            print("http://localhost:4747/live  (needs `tokentrim serve` running; `tokentrim serve --install` keeps it on)")
    elif cmd == "share":
        from .share import write as share
        share(argv[argv.index("--out") + 1] if "--out" in argv else None, open_it="--no-open" not in argv)
    elif cmd == "pro":
        from . import pro
        if argv[:1] == ["activate"] and len(argv) > 1:
            pro.activate(argv[1])
        elif argv[:1] == ["deactivate"]:
            pro.deactivate()
        else:
            pro.status()
    elif cmd == "digest":
        from . import pro
        if "--install" in argv:
            pro.install_digest()
        elif "--auto" in argv:
            pro.check()
        else:
            pro.digest(post="--no-post" not in argv)
    elif cmd == "budget":
        from . import pro
        pro.budget(argv[0] if argv else sys.exit("usage: tokentrim budget 2B|off"))
    elif cmd == "slack":
        from . import pro
        pro.slack(argv[0] if argv else None)
    elif cmd == "fixed":
        fixed()
    else:
        print(__doc__)


if __name__ == "__main__":
    main()
