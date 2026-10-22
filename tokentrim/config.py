"""Settings live in ~/.config/tokentrim/config.json; every key is optional."""
import json
import os

PATH = os.path.expanduser("~/.config/tokentrim/config.json")

DEFAULTS = {
    "enabled": True,
    "rule_screenshots": True,        # per-rule switches; the live dashboard's Controls panel flips these
    "rule_images": True,
    "rule_outline": True,
    "rule_bash": True,
    "rule_mcp": True,
    "rule_meter": True,
    "budget_guard": False,           # daily budget: off until you pick one in the dashboard
    "budget_usd": 20,
    "budget_block": False,
    "cache_guard": True,             # tell the user when a message will re-write an expired cache
    "model_advisor": True,           # point out quick jobs running on Opus
    "screenshot_scale": 0.4,         # browser screenshots: 0.4 = ~16% of the tokens; UI text stays legible
    "zoom_scale": 0.6,               # zoom is for detail, so it keeps more resolution
    "batch_extra_scale": 0.25,       # screenshots in a browser_batch other than the last one
    "batch_last_shot_only": False,   # True: drop a browser_batch's earlier screenshots, keep only the last
    "screenshot_scale_cap": None,    # e.g. 0.4: lower a scale Claude picked itself (0.5, 0.6...) to this; zoom untouched
    "dedupe_screenshots": True,      # deny a screenshot when nothing happened since the last one
    "image_max_px": 1024,            # images opened with Read are downscaled to this long edge
    "read_outline_bytes": 40_000,    # whole-file Reads above this get an outline first
    "outline_max_lines": 120,
    "bash_max_chars": 6_000,         # Bash output above this keeps head + tail, full copy on disk
    "mcp_max_chars": 12_000,         # MCP text results above this are clipped after JSON shrinking
    "mcp_lossless_only": False,      # True: only minify JSON, never clip
    "context_warn_tokens": 120_000,  # the context meter speaks up past this
    "baseline_tokens": 60_000,       # rough size of a fresh session
}


def load():
    cfg = dict(DEFAULTS)
    try:
        with open(PATH) as fh:
            cfg.update(json.load(fh))
    except (OSError, ValueError):
        pass
    return cfg
