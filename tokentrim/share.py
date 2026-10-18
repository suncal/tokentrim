"""`tokentrim share`: a savings card you can post (SVG, plus PNG on macOS).

Only totals go on the card: no prompts, paths, project names or session titles.
"""
import json
import os
import shutil
import subprocess
import time

from . import cli

STATE = os.path.expanduser("~/.cache/tokentrim")
# $ per million tokens: input, output (cache read = 0.1x input)
PRICES = {"opus": (5, 25), "sonnet": (3, 15), "haiku": (1, 5), "other": (3, 15)}


def _fam(model):
    m = model.lower()
    return next((f for f in ("opus", "sonnet", "haiku") if f in m), "other")


def load(rebuild=False):
    """Dashboard data: the live server's cached copy if there is one, else a fresh build."""
    disk = os.path.join(STATE, "dashboard.json")
    if not rebuild and os.path.exists(disk) and time.time() - os.path.getmtime(disk) < 86400:
        with open(disk) as fh:
            return json.load(fh)
    from .dashboard import build
    return build()


def summary(data):
    """With vs without tokentrim, from the replay of the user's own sessions."""
    rp = data["replay"]
    prof = data.get("profile") or "strong"
    without, with_ = rp["orig"], rp.get(prof) or rp["strong"]
    cr_tok = cr_usd = 0.0
    for models in data["daily"].values():
        for m, r in models.items():
            cr_tok += r[0]
            cr_usd += r[0] * 0.1 * PRICES[_fam(m)][0] / 1e6
    per_tok = cr_usd / max(cr_tok, 1)
    saved = max(without - with_, 0)
    sessions = data["sessions"]
    starts = [s["start"] for s in sessions if s.get("start")]
    ends = [s["end"] for s in sessions if s.get("end")]
    months = max((max(ends) - min(starts)) / (30 * 86400), 0.25) if starts and ends else 1
    return {
        "profile": prof, "sessions": len(sessions), "without": without, "with": with_,
        "saved": saved, "share": saved / max(without, 1), "times": without / max(with_, 1),
        "usd": saved * per_tok, "usd_month": saved * per_tok / months,
    }


def _fmt(n):
    for div, suf in ((1e9, "B"), (1e6, "M"), (1e3, "k")):
        if n >= div:
            return ("%.2f" % (n / div)).rstrip("0").rstrip(".") + suf
    return str(int(n))


def svg(s):
    w_with = max(int(820 * s["with"] / max(s["without"], 1)), 6)
    return """<svg xmlns="http://www.w3.org/2000/svg" width="1200" height="630" viewBox="0 0 1200 630" font-family="-apple-system,Segoe UI,Helvetica,Arial,sans-serif">
<rect width="1200" height="630" fill="#0f1115"/>
<text x="70" y="110" font-size="34" fill="#9aa3b2">My Claude Code, with tokentrim</text>
<text x="70" y="230" font-size="120" font-weight="800" fill="#4ade80">%(pct)s less</text>
<text x="70" y="290" font-size="34" fill="#e6e9ef">history re-read, on my own %(n)s conversations</text>
<text x="70" y="370" font-size="24" fill="#9aa3b2">Without</text>
<rect x="210" y="345" width="820" height="36" rx="8" fill="#f08a5d"/>
<text x="1045" y="372" font-size="26" font-weight="700" fill="#e6e9ef">%(without)s</text>
<text x="70" y="430" font-size="24" fill="#9aa3b2">With</text>
<rect x="210" y="405" width="%(ww)d" height="36" rx="8" fill="#4ade80"/>
<text x="%(wx)d" y="432" font-size="26" font-weight="700" fill="#e6e9ef">%(with)s</text>
<text x="70" y="520" font-size="30" fill="#e6e9ef">Usage limits last <tspan font-weight="800">%(x)s&#215;</tspan> longer · ~%(usd)s at API prices</text>
<text x="70" y="585" font-size="24" fill="#6b7280">github.com/suncal/tokentrim · measured by replaying real transcripts, not a benchmark</text>
</svg>""" % {
        "pct": "%d%%" % round(s["share"] * 100), "n": "{:,}".format(s["sessions"]),
        "without": _fmt(s["without"]), "with": _fmt(s["with"]), "ww": w_with, "wx": 210 + w_with + 15,
        "x": "%.1f" % s["times"], "usd": "${:,.0f}".format(s["usd"]),
    }


CHROMES = ("/Applications/Google Chrome.app/Contents/MacOS/Google Chrome",
           "/Applications/Chromium.app/Contents/MacOS/Chromium",
           "google-chrome", "chromium", "chromium-browser")


def _png(svg_path):
    """Render the SVG to a 1200x630 PNG with headless Chrome, if installed. None otherwise."""
    exe = next((c for c in CHROMES if os.path.exists(c) or shutil.which(c)), None)
    if not exe:
        return None
    png = svg_path[:-4] + ".png"
    profile = os.path.join(STATE, "share-chrome")
    try:
        subprocess.run([exe, "--headless=new", "--disable-gpu", "--hide-scrollbars", "--user-data-dir=" + profile,
                        "--window-size=1200,630", "--screenshot=" + png, "file://" + svg_path],
                       capture_output=True, timeout=60)
    except (OSError, subprocess.TimeoutExpired):
        return None
    return png if os.path.exists(png) else None


def write(out_dir=None, open_it=True):
    s = summary(load())
    out_dir = out_dir or os.path.expanduser("~/Desktop")
    if not os.path.isdir(out_dir):
        out_dir = os.getcwd()
    path = os.path.join(out_dir, "tokentrim-savings.svg")
    with open(path, "w") as fh:
        fh.write(svg(s))
    png = _png(path)
    print("%d%% less history re-read · limits last %.1fx longer · ~$%s at API prices"
          % (round(s["share"] * 100), s["times"], "{:,.0f}".format(s["usd"])))
    print("card ->", png or path)
    if open_it and cli.sys.platform == "darwin":
        subprocess.run(["open", png or path])
    return png or path
