"""Where do your Claude Code tokens actually go?

Reads the transcripts Claude Code keeps in ~/.claude/projects/*/*.jsonl and
attributes spend to its sources. Nothing leaves the machine.

The key fact this surfaces: in an agent loop almost all input tokens are
*cache reads* — the whole context is re-sent on every API call. A 10k-token
tool result that lands at call 5 of a 200-call session is paid for ~195 more
times. So we weight each tool result by how many later calls re-read it.
"""
import glob
import json
import os
from collections import defaultdict

IMG_TOKENS = 1600  # a ~1280x800 screenshot is ~1.4-1.6k tokens
PROJECTS = os.path.expanduser("~/.claude/projects")


def est_tokens(n_chars):
    return n_chars // 4


def _text_of(content):
    """Return (chars, n_images) for a tool_result content field."""
    if isinstance(content, str):
        return len(content), 0
    chars, imgs = 0, 0
    for part in content or []:
        if not isinstance(part, dict):
            continue
        if part.get("type") == "text":
            chars += len(part.get("text", ""))
        elif part.get("type") == "image":
            imgs += 1
    return chars, imgs


def _bucket(name, inp):
    if name == "Bash":
        cmd = (inp or {}).get("command", "").strip().split()
        return "Bash:" + (os.path.basename(cmd[0]) if cmd else "?")
    if name.startswith("mcp__"):
        parts = name.split("__")
        server = parts[1] if len(parts) > 1 else "?"
        return "MCP:" + (server[:12] if len(server) > 30 else server)
    return name


def scan_session(path):
    """One pass over a transcript. Returns a dict of per-session stats."""
    usage_seen = set()
    calls = []          # ordered list of API-call usage dicts
    results = []        # (call_index_at_arrival, bucket, tokens, images)
    tool_names = {}
    compactions = []    # call indexes where the context was compacted
    hook_ctx = 0
    with open(path, errors="replace") as fh:
        for line in fh:
            try:
                d = json.loads(line)
            except ValueError:
                continue
            t = d.get("type")
            if t == "assistant":
                msg = d.get("message") or {}
                for part in msg.get("content") or []:
                    if not isinstance(part, dict):
                        continue
                    if part.get("type") == "tool_use":
                        tool_names[part.get("id")] = (part.get("name", "?"), part.get("input"))
                        size = len(json.dumps(part.get("input"), ensure_ascii=False))
                        results.append((len(calls) + 1, "(your tool calls: args)", est_tokens(size), 0))
                    elif part.get("type") == "text":
                        results.append((len(calls) + 1, "(assistant text)", est_tokens(len(part.get("text", ""))), 0))
                mid = msg.get("id")
                u = msg.get("usage")
                if u and mid not in usage_seen:
                    usage_seen.add(mid)
                    calls.append(u)
            elif t == "user":
                msg = d.get("message") or {}
                content = msg.get("content")
                if isinstance(content, str):
                    results.append((len(calls), "(user messages)", est_tokens(len(content)), 0))
                elif isinstance(content, list):
                    for part in content:
                        if isinstance(part, dict) and part.get("type") == "text":
                            results.append((len(calls), "(user messages)", est_tokens(len(part.get("text", ""))), 0))
                        if isinstance(part, dict) and part.get("type") == "tool_result":
                            name, inp = tool_names.get(part.get("tool_use_id"), ("?", None))
                            chars, imgs = _text_of(part.get("content"))
                            results.append((len(calls), _bucket(name, inp), est_tokens(chars), imgs))
                if d.get("isCompactSummary"):
                    compactions.append(len(calls))
            elif t == "attachment":
                a = d.get("attachment") or {}
                if a.get("type") == "hook_success" and a.get("hookEvent") == "SessionStart":
                    hook_ctx += est_tokens(len(a.get("content") or ""))
            elif t == "system" and d.get("subtype") == "compact_boundary":
                compactions.append(len(calls))
    return {"calls": calls, "results": results, "compactions": sorted(compactions), "hook_ctx": hook_ctx}


def rereads(arrival, n_calls, compactions):
    """How many API calls re-read something that entered context at `arrival`."""
    end = n_calls
    for c in compactions:
        if c > arrival:
            end = c
            break
    return max(end - arrival, 0)


def audit(paths=None):
    paths = paths or glob.glob(os.path.join(PROJECTS, "*", "*.jsonl"))
    tot = defaultdict(int)
    by_src = defaultdict(lambda: defaultdict(int))
    baseline = []
    sessions = []
    for p in paths:
        s = scan_session(p)
        calls = s["calls"]
        if not calls:
            continue
        tot["sessions"] += 1
        tot["calls"] += len(calls)
        for u in calls:
            tot["input"] += u.get("input_tokens", 0) or 0
            tot["cache_read"] += u.get("cache_read_input_tokens", 0) or 0
            tot["cache_write"] += u.get("cache_creation_input_tokens", 0) or 0
            tot["output"] += u.get("output_tokens", 0) or 0
        first = calls[0]
        baseline.append((first.get("input_tokens", 0) or 0)
                        + (first.get("cache_read_input_tokens", 0) or 0)
                        + (first.get("cache_creation_input_tokens", 0) or 0))
        for arrival, bucket, toks, imgs in s["results"]:
            b = by_src[bucket]
            b["count"] += 1
            b["tokens"] += toks
            b["images"] += imgs
            # images cost ~1.6k tokens each at typical screenshot sizes
            b["weighted"] += (toks + imgs * IMG_TOKENS) * rereads(arrival, len(calls), s["compactions"])
        n = len(calls)
        reads = sum(u.get("cache_read_input_tokens", 0) or 0 for u in calls)
        sessions.append((n, reads))
        tot["hook_ctx"] += s["hook_ctx"]
    baseline.sort()
    tot["baseline_median"] = baseline[len(baseline) // 2] if baseline else 0
    tot["fixed_weighted"] = tot["baseline_median"] * tot["calls"]
    sessions.sort()
    tot["sessions_list"] = sessions
    return tot, by_src


def report(tot, by_src, top=20):
    all_in = tot["input"] + tot["cache_read"] + tot["cache_write"]
    out = []
    w = out.append
    w("tokentrim audit — %d sessions, %d API calls" % (tot["sessions"], tot["calls"]))
    w("")
    w("Tokens billed (from your transcripts):")
    w("  cache reads   %15s  (%4.1f%% of input)" % (f"{tot['cache_read']:,}", 100 * tot["cache_read"] / max(all_in, 1)))
    w("  cache writes  %15s" % f"{tot['cache_write']:,}")
    w("  fresh input   %15s" % f"{tot['input']:,}")
    w("  output        %15s  (%4.1f%% of all)" % (f"{tot['output']:,}", 100 * tot["output"] / max(all_in + tot["output"], 1)))
    w("")
    w("Fixed context at the first call of a session (median): %s tokens" % f"{tot['baseline_median']:,}")
    w("  = system prompt + tool schemas + CLAUDE.md + memory + skills + hooks, re-read on every call")
    w("")
    wsum = sum(b["weighted"] for b in by_src.values()) or 1
    w("Tool results, ranked by tokens they cost over their lifetime in context:")
    w("  %-28s %7s %12s %15s %6s" % ("source", "calls", "raw tok", "re-read tok", "share"))
    rows = sorted(by_src.items(), key=lambda kv: -kv[1]["weighted"])
    for name, b in rows[:top]:
        w("  %-28s %7d %12s %15s %5.1f%%" % (name[:28], b["count"], f"{b['tokens']:,}",
                                           f"{b['weighted']:,}", 100 * b["weighted"] / wsum))
    w("")
    cr = max(tot["cache_read"], 1)
    w("Attribution of cache reads (approximate):")
    w("  fixed context x calls       %5.1f%%" % (100 * tot["fixed_weighted"] / cr))
    w("  everything listed above     %5.1f%%" % (100 * wsum / cr))
    w("  unattributed (thinking, overhead, reminders) %5.1f%%" % max(0, 100 - 100 * (wsum + tot["fixed_weighted"]) / cr))
    w("")
    ss = tot["sessions_list"]
    long_ = [r for n, r in ss if n >= 300]
    w("Session length: %d of %d sessions ran 300+ API calls and they account for %.0f%% of all cache reads."
      % (len(long_), len(ss), 100 * sum(long_) / cr))
    return "\n".join(out)
