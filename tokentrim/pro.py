"""tokentrim Pro: license key, weekly digest, usage budget.

The free tool (hooks, profiles, audit, replay, dashboard, share) stays free and open.
Pro adds the parts that keep the savings going without you checking a dashboard.

License keys are sold through Polar (polar.sh) and checked against its public license-key API.
Only the key and a machine label are sent; nothing about your usage ever leaves the machine.
Slack posting is opt-in and sends totals only.
"""
import datetime
import json
import os
import platform
import subprocess
import sys
import time
import urllib.error
import urllib.request

LICENSE = os.path.expanduser("~/.config/tokentrim/license.json")
BUY_URL = "https://buy.polar.sh/polar_cl_f3ynkXOeRekKTm9C62vqBI2C3UGMRz1LgBNAp2GIvva"
ORG_ID = "d3cd7f3b-ea6d-4453-af1b-dc308458c629"  # Polar organization "tokentrim"
API = "https://api.polar.sh/v1/customer-portal/license-keys/"
RECHECK = 7 * 86400         # re-validate weekly
GRACE = 30 * 86400          # keep working offline for this long after the last good check
AGENT = os.path.expanduser("~/Library/LaunchAgents/com.tokentrim.digest.plist")


# ---------- license

def _post(action, fields):
    req = urllib.request.Request(API + action, data=json.dumps(dict(fields, organization_id=ORG_ID)).encode(),
                                 headers={"Accept": "application/json", "Content-Type": "application/json",
                                          "User-Agent": "tokentrim (+https://github.com/suncal/tokentrim)"})
    try:
        with urllib.request.urlopen(req, timeout=15) as r:
            return json.loads(r.read() or b"{}")
    except urllib.error.HTTPError as e:  # the API answers 4xx with a JSON body
        try:
            body = json.load(e)
        except ValueError:
            body = {}
        detail = body.get("detail")
        if isinstance(detail, list):  # validation errors come back as a list
            detail = "; ".join(d.get("msg", "") for d in detail if isinstance(d, dict))
        return {"error": detail or body.get("error") or "HTTP %d" % e.code}


def _save(lic):
    os.makedirs(os.path.dirname(LICENSE), exist_ok=True)
    with open(LICENSE, "w") as fh:
        json.dump(lic, fh, indent=2)
    os.chmod(LICENSE, 0o600)


def _load():
    try:
        with open(LICENSE) as fh:
            return json.load(fh)
    except (OSError, ValueError):
        return None


def activate(key):
    if ORG_ID is None:
        sys.exit("Pro checkout isn't open yet. Watch %s for launch." % BUY_URL)
    resp = _post("activate", {"key": key, "label": "tokentrim@" + platform.node()})
    if not resp.get("id"):
        sys.exit("Activation failed: %s" % (resp.get("error") or "this key isn't for tokentrim"))
    _save({"key": key, "instance": resp["id"], "checked": time.time()})
    print("tokentrim Pro activated. Thank you!")


def deactivate():
    lic = _load()
    if not lic:
        return print("No Pro license on this machine.")
    _post("deactivate", {"key": lic["key"], "activation_id": lic["instance"]})
    os.remove(LICENSE)
    print("Pro license removed from this machine (the seat is free to use elsewhere).")


def is_pro():
    lic = _load()
    if not lic:
        return False
    if time.time() - lic.get("checked", 0) < RECHECK:
        return True
    try:
        resp = _post("validate", {"key": lic["key"], "activation_id": lic["instance"]})
    except OSError:  # offline: trust the last good check for a while
        return time.time() - lic.get("checked", 0) < GRACE
    if resp.get("status") == "granted" and resp.get("organization_id") == ORG_ID:
        lic["checked"] = time.time()
        _save(lic)
        return True
    return False


def require():
    if not is_pro():
        sys.exit("This is a tokentrim Pro feature ($49 lifetime early-bird, then $9/mo).\n"
                 "Get a key: %s\nThen: tokentrim pro activate <key>" % BUY_URL)


def status():
    lic = _load()
    if not lic:
        print("tokentrim Free. Pro adds the weekly digest, budget alerts and Slack reports: %s" % BUY_URL)
    else:
        ok = is_pro()
        print("tokentrim Pro: %s (last checked %s)" % ("active" if ok else "NOT valid",
              datetime.datetime.fromtimestamp(lic.get("checked", 0)).strftime("%Y-%m-%d")))


# ---------- config + Slack

def _cfg_load():
    from .config import PATH
    try:
        with open(PATH) as fh:
            cfg = json.load(fh)
        return cfg if isinstance(cfg, dict) else {}
    except (OSError, ValueError):
        return {}


def _cfg_save(cfg):
    from .config import PATH
    os.makedirs(os.path.dirname(PATH), exist_ok=True)
    with open(PATH, "w") as fh:
        json.dump(cfg, fh, indent=2)


def _slack_post(hook, text):
    """-> None if Slack took it, else the reason it didn't."""
    try:
        with urllib.request.urlopen(urllib.request.Request(hook, data=json.dumps({"text": text}).encode(),
                                    headers={"Content-Type": "application/json"}), timeout=15) as r:
            body = r.read().decode("utf-8", "replace").strip()
        return None if body in ("ok", "") else body
    except urllib.error.HTTPError as e:
        return "HTTP %d %s" % (e.code, e.read().decode("utf-8", "replace")[:100])
    except (OSError, ValueError) as e:
        return str(e)


def slack(value):
    """`tokentrim slack <webhook URL>` posts weekly totals there; `off` stops; no argument shows it."""
    require()
    cfg = _cfg_load()
    if value is None:
        return print("Slack: %s" % ("on (weekly totals only)" if cfg.get("slack_webhook") else
                     "off. Create an incoming webhook (https://api.slack.com/messaging/webhooks), then: tokentrim slack <URL>"))
    if value == "off":
        cfg.pop("slack_webhook", None)
        _cfg_save(cfg)
        return print("Slack: off")
    if not value.startswith("https://hooks.slack.com/"):
        sys.exit("That isn't a Slack incoming-webhook URL (it starts with https://hooks.slack.com/).")
    err = _slack_post(value, "tokentrim is connected. Weekly totals will be posted here on Mondays.")
    if err:
        sys.exit("Slack didn't accept the test message (%s). Nothing was saved." % err)
    cfg["slack_webhook"] = value
    _cfg_save(cfg)
    print("Slack: on. A test message was posted; weekly totals follow every Monday (run `tokentrim digest --install`).")


# ---------- weekly digest

def _week(data, days=7):
    from .share import PRICES, _fam
    today = datetime.date.today()
    keep = {(today - datetime.timedelta(d)).isoformat() for d in range(days)}
    tok = usd = 0.0
    for day, models in data["daily"].items():
        if day in keep:
            for m, r in models.items():
                i, o = PRICES[_fam(m)]
                tok += r[0] + r[1] + r[2] + r[3]
                usd += (r[0] * 0.1 * i + r[1] * 1.25 * i + r[2] * i + r[3] * o) / 1e6
    saved = sum(v[1] for d, v in data["savings"]["daily"].items() if d in keep)
    cut = time.time() - days * 86400
    recent = [s for s in data["sessions"] if (s.get("end") or 0) >= cut]
    heavy = sorted(recent, key=lambda s: s["cr"] + s["sub"][1], reverse=True)[:3]
    return {"tokens": tok, "usd": usd, "saved": saved, "sessions": len(recent), "heavy": heavy,
            "long": sum(1 for s in recent if s["calls"] >= 300)}


def digest(post=True):
    """Last 7 days in plain English. Writes Markdown; optionally posts totals to Slack and warns on budget."""
    require()
    from .config import load as cfg_load
    from .share import load, summary, _fmt
    data = load(rebuild=True)
    w, s, cfg = _week(data), summary(data), cfg_load()
    lines = ["# tokentrim weekly digest, %s" % datetime.date.today().isoformat(), "",
             "- **This week:** %s tokens across %d conversations (~$%s at API prices)."
             % (_fmt(w["tokens"]), w["sessions"], "{:,.0f}".format(w["usd"])),
             "- **Trimmed live:** %s tokens kept out of your conversations." % _fmt(w["saved"]),
             "- **Overall:** %d%% less history re-read; limits last %.1fx longer." % (round(s["share"] * 100), s["times"])]
    budget = cfg.get("weekly_budget_tokens")
    if budget:
        lines.append("- **Budget:** %d%% of your %s weekly budget used." % (round(100 * w["tokens"] / budget), _fmt(budget)))
    if w["heavy"]:
        lines += ["", "Heaviest conversations:"] + ["- %s (%s calls, %s re-read)" % (h["title"][:60], h["calls"], _fmt(h["cr"] + h["sub"][1])) for h in w["heavy"]]
    if w["long"]:
        lines += ["", "Tip: %d conversation(s) ran past 300 steps. A fresh conversation per task is the biggest free saving." % w["long"]]
    text = "\n".join(lines) + "\n"
    out = os.path.expanduser("~/.cache/tokentrim/digest-%s.md" % datetime.date.today().isoformat())
    with open(out, "w") as fh:
        fh.write(text)
    print(text + "\nsaved -> " + out)
    hook = cfg.get("slack_webhook") or os.environ.get("TOKENTRIM_SLACK_WEBHOOK")
    if post and hook:  # totals only: conversation titles stay on this machine
        err = _slack_post(hook, "\n".join(lines[2:5 + bool(budget)]))
        print("posted to Slack" if not err else "Slack post failed: %s" % err)
    _budget_alert(w, budget)
    return out


def _budget_alert(w, budget):
    """Notify once a week when the last 7 days pass 80% (and again at 100%) of the budget."""
    if not budget or w["tokens"] < 0.8 * budget:
        return False
    level = 100 if w["tokens"] >= budget else 80
    mark = os.path.expanduser("~/.cache/tokentrim/budget-alert-%s-%d" % (datetime.date.today().strftime("%G-W%V"), level))
    if os.path.exists(mark):
        return False
    os.makedirs(os.path.dirname(mark), exist_ok=True)
    open(mark, "w").close()
    _notify("tokentrim: %d%% of your weekly budget used (%s of %s tokens in the last 7 days)"
            % (round(100 * w["tokens"] / budget), _fmt_tokens(w["tokens"]), _fmt_tokens(budget)))
    return True


def _fmt_tokens(n):
    from .share import _fmt
    return _fmt(n)


def check():
    """Daily run (LaunchAgent): the full digest on Mondays, otherwise just the budget check."""
    if datetime.date.today().weekday() == 0:
        return digest()
    require()
    from .share import load
    budget = _cfg_load().get("weekly_budget_tokens")
    if not budget:
        return print("no weekly budget set")
    w = _week(load(rebuild=True))
    if not _budget_alert(w, budget):
        print("budget: %d%% used in the last 7 days" % round(100 * w["tokens"] / budget))


def _notify(msg):
    if sys.platform == "darwin":
        subprocess.run(["osascript", "-e", 'display notification "%s" with title "tokentrim"' % msg.replace('"', "'")],
                       capture_output=True)
    print("!! " + msg)


def budget(value):
    """`tokentrim budget 2B` sets a weekly token budget; `tokentrim budget off` clears it."""
    require()
    cfg = _cfg_load()
    if value == "off":
        cfg.pop("weekly_budget_tokens", None)
    else:
        mult = {"k": 1e3, "m": 1e6, "b": 1e9}.get(value[-1].lower(), 1)
        try:
            cfg["weekly_budget_tokens"] = int(float(value.rstrip("kKmMbB")) * mult)
        except ValueError:
            sys.exit("usage: tokentrim budget 2B|500M|off")
    _cfg_save(cfg)
    if value == "off":
        return print("weekly budget: off")
    print("weekly budget: %s tokens over the last 7 days, checked every morning at 9:00; you get a notification at 80%%."
          % _fmt_tokens(cfg["weekly_budget_tokens"]))
    if sys.platform == "darwin" and not os.path.exists(AGENT):
        install_digest()


def install_digest():
    """Every day at 09:00 (macOS LaunchAgent): budget check, plus the full digest on Mondays."""
    require()
    from . import cli
    plist = """<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0"><dict>
<key>Label</key><string>com.tokentrim.digest</string>
<key>ProgramArguments</key><array><string>/usr/bin/env</string><string>python3</string><string>%s</string><string>digest</string><string>--auto</string></array>
<key>StartCalendarInterval</key><dict><key>Hour</key><integer>9</integer><key>Minute</key><integer>0</integer></dict>
<key>StandardOutPath</key><string>%s</string><key>StandardErrorPath</key><string>%s</string>
<key>Nice</key><integer>10</integer>
</dict></plist>
""" % (os.path.join(cli.ROOT, "bin", "tokentrim"), os.path.expanduser("~/.cache/tokentrim/digest.log"),
       os.path.expanduser("~/.cache/tokentrim/digest.log"))
    os.makedirs(os.path.dirname(AGENT), exist_ok=True)
    with open(AGENT, "w") as fh:
        fh.write(plist)
    subprocess.run(["launchctl", "unload", AGENT], capture_output=True)
    subprocess.run(["launchctl", "load", AGENT], capture_output=True)
    print("scheduled: budget check daily at 09:00, digest on Mondays ->", AGENT)
