"""Text shrinkers. Pure functions: str in, str out, never longer than the input."""
import hashlib
import json
import os
import re

ANSI = re.compile(r"\x1b\[[0-9;?]*[ -/]*[@-~]|\x1b\][^\x07]*\x07")
SPOOL = os.path.expanduser("~/.cache/tokentrim/out")


def strip_noise(text):
    """ANSI colours, carriage-return progress bars, trailing spaces."""
    text = ANSI.sub("", text)
    if "\r" in text:
        # a progress bar redraws one line with \r: keep only its final state
        text = "\n".join(line.rsplit("\r", 1)[-1] for line in text.split("\n"))
    return "\n".join(line.rstrip() for line in text.split("\n"))


def collapse_repeats(text, min_run=4):
    """Runs of identical lines -> first, a count, last. Exact matches only: data rows survive."""
    lines = text.split("\n")
    out, i = [], 0
    while i < len(lines):
        j = i + 1
        while j < len(lines) and lines[j] == lines[i]:
            j += 1
        run = j - i
        if run >= min_run:
            out.append(lines[i])
            out.append("  … %d identical lines …" % (run - 2))
            out.append(lines[j - 1])
        else:
            out.extend(lines[i:j])
        i = j
    return "\n".join(out)


def spool(text):
    """Keep the full original on disk so nothing is ever truly lost."""
    os.makedirs(SPOOL, exist_ok=True)
    path = os.path.join(SPOOL, hashlib.sha1(text.encode("utf-8", "replace")).hexdigest()[:16] + ".txt")
    if not os.path.exists(path):
        with open(path, "w") as fh:
            fh.write(text)
    return path


def clip(text, limit, head_frac=0.4):
    """Head + tail with a pointer to the full text in the middle."""
    if len(text) <= limit:
        return text
    path = spool(text)
    head = int(limit * head_frac)
    tail = limit - head
    h = text[:head].rsplit("\n", 1)[0]
    t = text[-tail:].split("\n", 1)[-1]
    dropped = text[len(h):len(text) - len(t)]
    return "%s\n\n[tokentrim: %d lines / %s chars cut. Full output: %s — grep or sed -n it if you need more]\n\n%s" % (
        h, dropped.count("\n"), "{:,}".format(len(dropped)), path, t)


def shrink_command_output(text, limit):
    if not text:
        return text
    small = collapse_repeats(strip_noise(text))
    small = clip(small, limit)
    return small if len(small) < len(text) else text


# ---------------------------------------------------------------- JSON (MCP results)

def _prune(o, max_str):
    if isinstance(o, dict):
        out = {}
        for k, v in o.items():
            v = _prune(v, max_str)
            if v is None or v == "" or v == [] or v == {}:
                continue
            out[k] = v
        return out
    if isinstance(o, list):
        return [_prune(v, max_str) for v in o]  # never drops list items: lossless-leaning
    if isinstance(o, str):
        if len(o) > 200 and re.fullmatch(r"[A-Za-z0-9+/=\s]+", o[:4000] or "x") and len(o) > 1000:
            return "[base64 %d chars removed]" % len(o)
        if len(o) > max_str:
            return o[:max_str] + "…[+%d chars]" % (len(o) - max_str)
    return o


def shrink_json_text(text, max_str=4000):
    """Minify JSON, drop null/empty fields, strip base64 blobs. Non-JSON passes through."""
    s = text.strip()
    if not s or s[0] not in "{[":
        return text
    try:
        obj = json.loads(s)
    except ValueError:
        return text
    small = json.dumps(_prune(obj, max_str), ensure_ascii=False, separators=(",", ":"))
    return small if len(small) < len(text) else text
