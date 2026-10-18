"""tokentrim Pro: license key, weekly digest, usage budget.

The free tool (hooks, profiles, audit, replay, dashboard, share) stays free and open.
Pro adds the parts that keep the savings going without you checking a dashboard.

License keys are sold through Lemon Squeezy and checked against its public license API.
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
import urllib.parse
import urllib.request

LICENSE = os.path.expanduser("~/.config/tokentrim/license.json")
BUY_URL = "https://github.com/suncal/tokentrim#pro"
STORE_ID = None             # Lemon Squeezy store id; set at launch. None = checkout not open yet.
API = "https://api.lemonsqueezy.com/v1/licenses/"
RECHECK = 7 * 86400         # re-validate weekly
GRACE = 30 * 86400          # keep working offline for this long after the last good check
AGENT = os.path.expanduser("~/Library/LaunchAgents/com.tokentrim.digest.plist")


# ---------- license

def _post(action, fields):
    req = urllib.request.Request(API + action, data=urllib.parse.urlencode(fields).encode(),
                                 headers={"Accept": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=15) as r:
            return json.load(r)
    except urllib.error.HTTPError as e:  # the API answers 4xx with a JSON body
        try:
            return json.load(e)
        except ValueError:
            return {"error": "HTTP %d" % e.code}


def _ours(resp):
    meta = resp.get("meta") or {}
    return STORE_ID is None or meta.get("store_id") == STORE_ID


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
    if STORE_ID is None:
        sys.exit("Pro checkout isn't open yet. Watch %s for launch." % BUY_URL)
    resp = _post("activate", {"license_key": key, "instance_name": "tokentrim@" + platform.node()})
    if not resp.get("activated") or not _ours(resp):
        sys.exit("Activation failed: %s" % (resp.get("error") or "this key isn't for tokentrim"))
    _save({"key": key, "instance": resp["instance"]["id"], "checked": time.time(),
           "email": (resp.get("meta") or {}).get("customer_email", "")})
    print("tokentrim Pro activated. Thank you!")


def deactivate():
    lic = _load()
    if not lic:
        return print("No Pro license on this machine.")
    _post("deactivate", {"license_key": lic["key"], "instance_id": lic["instance"]})
    os.remove(LICENSE)
    print("Pro license removed from this machine (the seat is free to use elsewhere).")


def is_pro():
    lic = _load()
    if not lic:
        return False
    if time.time() - lic.get("checked", 0) < RECHECK:
        return True
    try:
        resp = _post("validate", {"license_key": lic["key"], "instance_id": lic["instance"]})
    except OSError:  # offline: trust the last good check for a while
        return time.time() - lic.get("checked", 0) < GRACE
    if resp.get("valid") and _ours(resp):
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
        body = {"text": "\n".join(l for l in lines[2:5 + bool(budget)])}
        urllib.request.urlopen(urllib.request.Request(hook, data=json.dumps(body).encode(),
                               headers={"Content-Type": "application/json"}), timeout=15)
        print("posted to Slack")
    if budget and w["tokens"] >= 0.8 * budget:
        _notify("tokentrim: %d%% of weekly budget used" % round(100 * w["tokens"] / budget))
    return out


def _notify(msg):
    if sys.platform == "darwin":
        subprocess.run(["osascript", "-e", 'display notification "%s" with title "tokentrim"' % msg.replace('"', "'")],
                       capture_output=True)
    print("!! " + msg)


def budget(value):
    """`tokentrim budget 2B` sets a weekly token budget; `tokentrim budget off` clears it."""
    require()
    from .config import PATH
    try:
        with open(PATH) as fh:
            cfg = json.load(fh)
    except (OSError, ValueError):
        cfg = {}
    if value == "off":
        cfg.pop("weekly_budget_tokens", None)
    else:
        mult = {"k": 1e3, "m": 1e6, "b": 1e9}.get(value[-1].lower(), 1)
        cfg["weekly_budget_tokens"] = int(float(value.rstrip("kKmMbB")) * mult)
    os.makedirs(os.path.dirname(PATH), exist_ok=True)
    with open(PATH, "w") as fh:
        json.dump(cfg, fh, indent=2)
    print("weekly budget:", cfg.get("weekly_budget_tokens", "off"))


def install_digest():
    """Run the digest every Monday 09:00 (macOS LaunchAgent)."""
    require()
    from . import cli
    plist = """<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0"><dict>
<key>Label</key><string>com.tokentrim.digest</string>
<key>ProgramArguments</key><array><string>/usr/bin/env</string><string>python3</string><string>%s</string><string>digest</string></array>
<key>StartCalendarInterval</key><dict><key>Weekday</key><integer>1</integer><key>Hour</key><integer>9</integer><key>Minute</key><integer>0</integer></dict>
<key>StandardOutPath</key><string>%s</string><key>StandardErrorPath</key><string>%s</string>
<key>Nice</key><integer>10</integer>
</dict></plist>
""" % (os.path.join(cli.ROOT, "bin", "tokentrim"), os.path.expanduser("~/.cache/tokentrim/digest.log"),
       os.path.expanduser("~/.cache/tokentrim/digest.log"))
    with open(AGENT, "w") as fh:
        fh.write(plist)
    subprocess.run(["launchctl", "unload", AGENT], capture_output=True)
    subprocess.run(["launchctl", "load", AGENT], capture_output=True)
    print("weekly digest scheduled: Mondays 09:00 ->", AGENT)
