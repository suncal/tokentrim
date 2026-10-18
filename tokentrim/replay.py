"""Re-run your past sessions through tokentrim's rules and count what would have changed.

Uses the real tool results stored in your transcripts. Each saving is weighted
by how many later API calls would have re-read it, exactly like the bill.
Then it simulates an earlier compaction window on the trimmed sessions.

What it cannot see: answers Claude would have given differently because it saw
less, and the extra targeted reads after an outline. Those are modelled with
stated assumptions (READ_RETRY, READ_FOLLOWUP) rather than ignored.
"""
import base64
import glob
import json
import os
import struct
from collections import defaultdict

from . import config, filters
from .audit import PROJECTS, _bucket, est_tokens

READ_RETRY = 0.35      # share of outline-first reads where Claude still pulls the whole file
READ_FOLLOWUP = 0.25   # otherwise it reads ~this fraction of the file in ranges
OUTLINE_TOKENS = 900
TRIGGER = 0.75         # measured: auto-compaction fires at ~73-83% of CLAUDE_CODE_AUTO_COMPACT_WINDOW


def image_tokens(w, h):
    # Claude downsizes images so the long edge is <= 1568 and area <= ~1.15MP
    scale = min(1.0, 1568 / max(w, h), (1_150_000 / float(w * h)) ** 0.5)
    w, h = w * scale, h * scale
    return int(w * h / 750)


def image_dims(b64):
    try:
        raw = base64.b64decode(b64[:4000] + "=" * (-len(b64[:4000]) % 4))
    except (ValueError, TypeError):
        return None
    if raw[:8] == b"\x89PNG\r\n\x1a\n":
        return struct.unpack(">II", raw[16:24])
    if raw[:2] == b"\xff\xd8":
        i = 2
        while i < len(raw) - 9:
            if raw[i] != 0xFF:
                i += 1
                continue
            marker = raw[i + 1]
            if marker in (0xC0, 0xC1, 0xC2):
                h, w = struct.unpack(">HH", raw[i + 5:i + 9])
                return w, h
            i += 2 + struct.unpack(">H", raw[i + 2:i + 4])[0]
    return None


def _result_saving(name, inp, content, cfg):
    """(original_tokens, trimmed_tokens) for one tool result."""
    parts = content if isinstance(content, list) else [{"type": "text", "text": content or ""}]
    orig = new = 0
    for p in parts:
        if not isinstance(p, dict):
            continue
        if p.get("type") == "text":
            t = p.get("text") or ""
            o = est_tokens(len(t))
            if name == "Bash":
                n = est_tokens(len(filters.shrink_command_output(t, cfg["bash_max_chars"])))
            elif name.startswith("mcp__"):
                s = filters.shrink_json_text(t)
                if not cfg["mcp_lossless_only"]:
                    s = filters.clip(s, cfg["mcp_max_chars"]) if len(s) > cfg["mcp_max_chars"] else s
                n = est_tokens(len(s))
            elif name == "Read" and len(t) * 0.8 > cfg["read_outline_bytes"] and not (inp or {}).get("limit"):
                n = int(READ_RETRY * (o + OUTLINE_TOKENS) + (1 - READ_RETRY) * (OUTLINE_TOKENS + READ_FOLLOWUP * o))
            else:
                n = o
            orig += o
            new += min(n, o)
        elif p.get("type") == "image":
            dims = image_dims(((p.get("source") or {}).get("data")) or "")
            if not dims:
                orig += 1600
                new += 1600
                continue
            w, h = dims
            o = image_tokens(w, h)
            if name.startswith("mcp__") and name.endswith("__computer") or name.endswith("__browser_batch"):
                if (inp or {}).get("scale") is None:
                    k = cfg["screenshot_scale"]
                    n = image_tokens(max(int(w * k), 1), max(int(h * k), 1))
                else:
                    n = o
            elif name == "Read" and max(w, h) > cfg["image_max_px"]:
                k = cfg["image_max_px"] / float(max(w, h))
                n = image_tokens(int(w * k), int(h * k))
            else:
                n = o
            orig += o
            new += n
    return orig, new


def replay_session(path, cfg):
    calls, events, names = [], [], {}
    seen = set()
    with open(path, errors="replace") as fh:
        for line in fh:
            try:
                d = json.loads(line)
            except ValueError:
                continue
            if d.get("type") == "assistant":
                m = d.get("message") or {}
                for p in m.get("content") or []:
                    if isinstance(p, dict) and p.get("type") == "tool_use":
                        names[p.get("id")] = (p.get("name", "?"), p.get("input"))
                u, mid = m.get("usage"), m.get("id")
                if u and mid not in seen:
                    seen.add(mid)
                    calls.append((u.get("input_tokens", 0) or 0) + (u.get("cache_read_input_tokens", 0) or 0)
                                 + (u.get("cache_creation_input_tokens", 0) or 0))
            elif d.get("type") == "user":
                c = (d.get("message") or {}).get("content")
                if isinstance(c, list):
                    for p in c:
                        if isinstance(p, dict) and p.get("type") == "tool_result":
                            name, inp = names.get(p.get("tool_use_id"), ("?", None))
                            o, n = _result_saving(name, inp, p.get("content"), cfg)
                            if o:
                                events.append((len(calls), _bucket(name, inp), o, n))
    return calls, events


def simulate(calls, events, window):
    """Context series after trimming, then with compaction at `window` tokens.
    Returns (original_sum, trimmed_sum, trimmed_and_capped_sum)."""
    if not calls:
        return 0, 0, 0
    saved_at = defaultdict(int)
    for idx, _b, o, n in events:
        saved_at[idx] += o - n
    trimmed, cum = [], 0
    prev = calls[0]
    for i, c in enumerate(calls):
        if c < prev * 0.6:      # a real compaction happened: earlier savings left the context
            cum = 0
        prev = c
        cum += saved_at.get(i, 0)
        trimmed.append(max(c - cum, calls[0]))
    if not window:
        return sum(calls), sum(trimmed), sum(trimmed)
    base = calls[0]
    window = int(window * TRIGGER)
    capped, ctx, last = 0, base, trimmed[0]
    for t in trimmed:
        d = t - last if t >= last * 0.6 else 0
        last = t
        ctx += max(d, 0)
        if ctx > window:
            capped += ctx + 4000          # the compaction call itself reads everything once
            ctx = base + 12000            # fresh context + the summary
        capped += ctx
    return sum(calls), sum(trimmed), capped


def replay(window=None, paths=None):
    cfg = config.load()
    paths = paths or glob.glob(os.path.join(PROJECTS, "*", "*.jsonl"))
    by = defaultdict(lambda: [0, 0, 0])
    T = [0, 0, 0]
    for p in paths:
        calls, events = replay_session(p, cfg)
        if not calls:
            continue
        a, b, c = simulate(calls, events, window)
        T[0] += a
        T[1] += b
        T[2] += c
        for idx, bucket, o, n in events:
            by[bucket][0] += 1
            by[bucket][1] += o
            by[bucket][2] += n
    return T, by


def report(T, by, window, top=12):
    out = []
    w = out.append
    orig, trimmed, capped = T
    w("tokentrim replay — your real sessions, re-run through the rules")
    w("")
    w("Context tokens re-sent across all calls (what you are billed for, ~98% of it):")
    w("  as it happened              %16s" % "{:,}".format(orig))
    w("  with output trimming        %16s   -%.1f%%" % ("{:,}".format(trimmed), 100 - 100.0 * trimmed / max(orig, 1)))
    if window:
        w("  + compaction window %4dk    %16s   -%.1f%%" % (window // 1000, "{:,}".format(capped),
                                                          100 - 100.0 * capped / max(orig, 1)))
    w("")
    w("Per-result trimming (raw tokens, before re-reads):")
    w("  %-26s %7s %12s %12s %6s" % ("source", "results", "before", "after", "cut"))
    for name, (k, o, n) in sorted(by.items(), key=lambda kv: -(kv[1][1] - kv[1][2]))[:top]:
        w("  %-26s %7d %12s %12s %5.0f%%" % (name[:26], k, "{:,}".format(o), "{:,}".format(n), 100 - 100.0 * n / max(o, 1)))
    return "\n".join(out)
