"""Claude Code hook entry point:  python3 -m tokentrim.hook <event>

Every rule here fails open: any error, unknown tool or odd input means the
call goes through untouched. A hook must never be the reason a tool breaks.

  pre      PreToolUse    shrink screenshots, shrink images before Read,
                         outline-first for huge text files
  post     PostToolUse   head+tail long Bash output, minify/clip MCP JSON
                         (full original always spooled to ~/.cache/tokentrim/out)
  prompt   UserPromptSubmit  context meter (a message to YOU, costs Claude 0 tokens)
"""
import hashlib
import json
import os
import re
import subprocess
import sys
import time

from . import config, filters, outline

STATE = os.path.expanduser("~/.cache/tokentrim")
IMG_EXT = (".png", ".jpg", ".jpeg", ".webp", ".gif", ".heic", ".tiff", ".bmp")


def _out(obj):
    sys.stdout.write(json.dumps(obj))
    sys.exit(0)


def _rewrite(new_input, note):
    _log("rewrite", note)
    _out({"hookSpecificOutput": {"hookEventName": "PreToolUse", "updatedInput": new_input}})


def _deny(reason):
    _log("deny", reason[:120])
    _out({"hookSpecificOutput": {"hookEventName": "PreToolUse",
                                 "permissionDecision": "deny",
                                 "permissionDecisionReason": reason}})


SESSION = ""
SHOT_TOKENS = 1600  # a full-size browser screenshot (~1456x816 viewport), before any scaling


def _saving(rule, tool, before, after, est=False):
    """One line per action in savings.jsonl: what the result would have cost, and what it cost."""
    try:
        os.makedirs(STATE, exist_ok=True)
        with open(os.path.join(STATE, "savings.jsonl"), "a") as fh:
            fh.write(json.dumps({"ts": time.time(), "session": SESSION, "rule": rule, "tool": tool,
                                 "before": int(before), "after": int(after), "est": est}) + "\n")
    except OSError:
        pass


def _log(kind, note):
    try:
        os.makedirs(STATE, exist_ok=True)
        with open(os.path.join(STATE, "events.log"), "a") as fh:
            fh.write("%s\t%s\t%s\n" % (time.strftime("%Y-%m-%dT%H:%M:%S"), kind, note.replace("\n", " ")))
    except OSError:
        pass


# ---------------------------------------------------------------- screenshots

BROWSER_TOOLS = re.compile(r"^mcp__.*(Claude_Browser|claude-in-chrome|Claude_in_Chrome|Claude_Preview).*__(\w+)$")
# Browser tools that only look at the page; anything else may change what a screenshot would show.
READ_ONLY = {"find", "read_page", "get_page_text", "read_console_messages", "read_network_requests",
             "tabs_context", "tabs_context_mcp", "preview_list", "preview_logs", "list_connected_browsers",
             "shortcuts_list", "gif_creator"}


def _scale_screenshot(inp, cfg, scale=None):
    if inp.get("action") in ("screenshot", "zoom") and "scale" not in inp:
        if inp.get("save_to_disk"):
            return None  # an image meant for the user keeps full size
        new = dict(inp)
        new["scale"] = scale or (cfg["zoom_scale"] if inp.get("action") == "zoom" else cfg["screenshot_scale"])
        return new
    return None


def _page_state(session, value=None):
    """'shot' if the last page-touching browser call this session was a screenshot, else 'act'."""
    path = os.path.join(STATE, "browser", hashlib.sha1(session.encode()).hexdigest()[:16])
    if value is None:
        try:
            with open(path) as fh:
                return fh.read()
        except OSError:
            return "act"
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w") as fh:
        fh.write(value)


def _is_shot(inp):
    return isinstance(inp, dict) and inp.get("action") == "screenshot"


def rule_screenshot(tool, inp, cfg, session=""):
    m = BROWSER_TOOLS.match(tool)
    if not m:
        return
    name = m.group(2)
    if name in READ_ONLY:
        return
    if name == "computer":
        if not _is_shot(inp):
            if inp.get("action") != "zoom":
                _page_state(session, "act")
        else:
            explicit = "scale" in inp or inp.get("save_to_disk")
            if cfg["dedupe_screenshots"] and not explicit and session and _page_state(session) == "shot":
                _saving("screenshot_dedupe", tool, SHOT_TOKENS, 60, est=True)
                _deny("tokentrim: nothing changed since your last screenshot (no click, typing, scroll, "
                      "navigation or wait in between). Use the image you already have, or read_page / "
                      "get_page_text for text. If you really need a fresh one, call again with scale set.")
            _page_state(session, "shot")
        new = _scale_screenshot(inp, cfg) if cfg["screenshot_scale"] < 1 else None
        if new:
            _saving("screenshot_scale", tool, SHOT_TOKENS, SHOT_TOKENS * new["scale"] ** 2, est=True)
            _rewrite(new, "%s %s scale=%s" % (tool.split("__")[1], inp.get("action"), new["scale"]))
    elif name == "browser_batch":
        items = [i for i in (inp.get("actions") or []) if isinstance(i, dict)]
        shots = [k for k, i in enumerate(items) if i.get("name") == "computer" and _is_shot(i.get("input"))]
        changed, actions = False, []
        for k, item in enumerate(inp.get("actions") or []):
            if k in shots and cfg["screenshot_scale"] < 1:
                # only the batch's last screenshot is what Claude acts on; earlier ones are progress checks
                scale = cfg["screenshot_scale"] if k == shots[-1] else cfg["batch_extra_scale"]
                new = _scale_screenshot(item["input"], cfg, scale)
                if new:
                    item = dict(item, input=new)
                    changed = True
                    _saving("screenshot_scale", tool, SHOT_TOKENS, SHOT_TOKENS * scale ** 2, est=True)
            actions.append(item)
        last = items[-1] if items else {}
        if session:
            _page_state(session, "shot" if last.get("name") == "computer" and _is_shot(last.get("input")) else "act")
        if changed:
            _rewrite(dict(inp, actions=actions), "browser_batch %d screenshots scaled" % len(shots))
    elif session:
        _page_state(session, "act")


# ---------------------------------------------------------------- images via Read

def _image_dims(path):
    try:
        r = subprocess.run(["sips", "-g", "pixelWidth", "-g", "pixelHeight", path],
                           capture_output=True, text=True, timeout=5)
        w = re.search(r"pixelWidth: (\d+)", r.stdout)
        h = re.search(r"pixelHeight: (\d+)", r.stdout)
        return (int(w.group(1)), int(h.group(1))) if w and h else None
    except (OSError, subprocess.SubprocessError):
        return None


def _img_tokens(w, h):
    k = min(1.0, 1568.0 / max(w, h), (1150000.0 / (w * h)) ** 0.5)
    return w * k * h * k / 750


def rule_read_image(tool, inp, cfg):
    path = inp.get("file_path") or ""
    if tool != "Read" or not path.lower().endswith(IMG_EXT) or not os.path.isfile(path):
        return
    max_px = cfg["image_max_px"]
    dims = _image_dims(path)
    if not dims or max(dims) <= max_px:
        return
    st = os.stat(path)
    key = hashlib.sha1(("%s|%d|%d|%d" % (path, st.st_size, int(st.st_mtime), max_px)).encode()).hexdigest()[:16]
    os.makedirs(os.path.join(STATE, "img"), exist_ok=True)
    small = os.path.join(STATE, "img", key + ".jpg")
    if not os.path.exists(small):
        r = subprocess.run(["sips", "-Z", str(max_px), "-s", "format", "jpeg",
                            "-s", "formatOptions", "80", path, "--out", small],
                           capture_output=True, timeout=20)
        if r.returncode != 0 or not os.path.exists(small):
            return
    new = dict(inp, file_path=small)
    k = max_px / float(max(dims))
    _saving("read_image", tool, _img_tokens(*dims), _img_tokens(dims[0] * k, dims[1] * k))
    _rewrite(new, "Read image %s %dx%d -> max %dpx" % (os.path.basename(path), dims[0], dims[1], max_px))


# ---------------------------------------------------------------- huge text files

def _seen(session, path):
    """True the second time Claude asks for the same whole file this session."""
    os.makedirs(os.path.join(STATE, "seen"), exist_ok=True)
    marker = os.path.join(STATE, "seen", hashlib.sha1(("%s|%s" % (session, path)).encode()).hexdigest())
    if os.path.exists(marker):
        return True
    open(marker, "w").close()
    return False


def rule_read_text(tool, inp, cfg, session):
    path = inp.get("file_path") or ""
    if tool != "Read" or inp.get("offset") or inp.get("limit") or not os.path.isfile(path):
        return
    if path.lower().endswith(IMG_EXT + (".pdf", ".ipynb")):
        return
    size = os.path.getsize(path)
    if size < cfg["read_outline_bytes"]:
        return
    if _seen(session, path):
        # Claude asked again: it really wants the whole file. The outline it saw was extra cost.
        _saving("read_outline_retry", tool, 0, size / 4.0)
        return
    ol = outline.outline(path, max_lines=cfg["outline_max_lines"])
    if not ol:
        return
    _saving("read_outline", tool, size / 4.0, len(ol) / 4.0 + 80)
    _deny(
        "tokentrim: %s is %s KB (~%s tokens). Here is its outline instead. "
        "Read just the part you need with offset/limit. If you genuinely need the whole "
        "file, call Read again with the same arguments and it will go through.\n\n%s"
        % (os.path.basename(path), size // 1024, "{:,}".format(size // 4), ol))


# ---------------------------------------------------------------- PostToolUse

def _post(new_response, note):
    _log("trim", note)
    _out({"hookSpecificOutput": {"hookEventName": "PostToolUse", "updatedToolOutput": new_response}})


def rule_post_bash(tool, resp, cfg):
    if tool != "Bash" or not isinstance(resp, dict) or resp.get("isImage"):
        return
    out = resp.get("stdout") or ""
    err = resp.get("stderr") or ""
    limit = cfg["bash_max_chars"]
    if len(out) + len(err) <= limit:
        return
    new = dict(resp)
    new["stdout"] = filters.shrink_command_output(out, limit)
    new["stderr"] = filters.shrink_command_output(err, max(limit // 3, 1500))
    before, after = len(out) + len(err), len(new["stdout"]) + len(new["stderr"])
    if after < before:
        _saving("bash_trim", tool, before / 4.0, after / 4.0)
        _post(new, "Bash %d -> %d chars" % (before, after))


def _shrink_parts(parts, cfg):
    changed, out = False, []
    for part in parts:
        if isinstance(part, dict) and part.get("type") == "text" and isinstance(part.get("text"), str):
            t = part["text"]
            small = filters.shrink_json_text(t)
            if not cfg["mcp_lossless_only"]:
                small = filters.clip(small, cfg["mcp_max_chars"])
            if len(small) < len(t):
                part = dict(part, text=small)
                changed = True
        out.append(part)
    return changed, out


def rule_post_mcp(tool, resp, cfg):
    if not tool.startswith("mcp__"):
        return
    before = len(json.dumps(resp, ensure_ascii=False))
    if before < 2000:
        return
    if isinstance(resp, list):
        changed, parts = _shrink_parts(resp, cfg)
        new = parts
    elif isinstance(resp, dict) and isinstance(resp.get("content"), list):
        changed, parts = _shrink_parts(resp["content"], cfg)
        new = dict(resp, content=parts)
    elif isinstance(resp, str):
        new = filters.shrink_json_text(resp)
        if not cfg["mcp_lossless_only"]:
            new = filters.clip(new, cfg["mcp_max_chars"])
        changed = len(new) < len(resp)
    else:
        return
    if changed:
        after = len(json.dumps(new, ensure_ascii=False))
        _saving("mcp_shrink", tool, before / 4.0, after / 4.0)
        _post(new, "%s %d -> %d chars" % (tool, before, after))


# ---------------------------------------------------------------- context meter

def last_context(transcript):
    """Tokens the most recent API call had in context, from the transcript tail."""
    try:
        with open(transcript, "rb") as fh:
            fh.seek(0, 2)
            fh.seek(max(fh.tell() - 400_000, 0))
            tail = fh.read().decode("utf-8", "replace").splitlines()
    except OSError:
        return 0
    for line in reversed(tail):
        if '"usage"' not in line:
            continue
        try:
            u = json.loads(line)["message"]["usage"]
        except (ValueError, KeyError, TypeError):
            continue
        return (u.get("input_tokens", 0) or 0) + (u.get("cache_read_input_tokens", 0) or 0) \
            + (u.get("cache_creation_input_tokens", 0) or 0)
    return 0


def on_prompt(event, cfg):
    ctx = last_context(event.get("transcript_path") or "")
    warn = cfg["context_warn_tokens"]
    if not ctx or ctx < warn:
        return
    # speak once per 50k of growth, not on every prompt
    step = ctx // 50_000
    os.makedirs(os.path.join(STATE, "meter"), exist_ok=True)
    mark = os.path.join(STATE, "meter", hashlib.sha1((event.get("session_id") or "").encode()).hexdigest())
    try:
        with open(mark) as fh:
            if int(fh.read() or 0) >= step:
                return
    except (OSError, ValueError):
        pass
    with open(mark, "w") as fh:
        fh.write(str(step))
    _log("meter", "%d tokens" % ctx)
    times = ctx / max(cfg["baseline_tokens"], 1)
    # systemMessage is shown to the user only; it never enters Claude's context.
    _out({"systemMessage":
          "tokentrim: this session is carrying %sk tokens of context, re-sent on every step "
          "(~%.0fx a fresh session). Run /compact, or start a new session for the next task."
          % ("{:,}".format(ctx // 1000), times)})


def settings_hooks_installed():
    """True when `tokentrim install` put our hooks in ~/.claude/settings.json."""
    try:
        with open(os.path.expanduser("~/.claude/settings.json")) as fh:
            hooks = json.load(fh).get("hooks") or {}
    except (OSError, ValueError, AttributeError):
        return False
    return any("tokentrim_hook.py" in (h.get("command") or "")
               for lst in hooks.values() for e in lst for h in e.get("hooks", []))


def main():
    event_name = sys.argv[1] if len(sys.argv) > 1 else "pre"
    # Plugin hooks stand down when the settings.json install is present, so a
    # user with both never gets each rule applied twice.
    if "--plugin" in sys.argv[2:] and settings_hooks_installed():
        return
    try:
        event = json.load(sys.stdin)
        global SESSION
        SESSION = event.get("session_id") or ""
        cfg = config.load()
        if not cfg.get("enabled", True):
            return
        if event_name == "pre":
            tool = event.get("tool_name") or ""
            inp = event.get("tool_input") or {}
            if not isinstance(inp, dict):
                return
            rule_screenshot(tool, inp, cfg, event.get("session_id") or "")
            rule_read_image(tool, inp, cfg)
            rule_read_text(tool, inp, cfg, event.get("session_id") or "")
        elif event_name == "post":
            tool = event.get("tool_name") or ""
            resp = event.get("tool_response")
            rule_post_bash(tool, resp, cfg)
            rule_post_mcp(tool, resp, cfg)
        elif event_name == "prompt":
            on_prompt(event, cfg)
    except SystemExit:
        raise
    except Exception as e:  # fail open, always
        _log("error", "%s: %s" % (type(e).__name__, e))


if __name__ == "__main__":
    main()
