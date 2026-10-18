"""Settings live in ~/.config/tokentrim/config.json; every key is optional."""
import json
import os

PATH = os.path.expanduser("~/.config/tokentrim/config.json")

DEFAULTS = {
    "enabled": True,
    "screenshot_scale": 0.5,         # browser screenshots: 0.5 = a quarter of the tokens
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
