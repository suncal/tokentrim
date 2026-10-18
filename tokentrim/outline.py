"""A cheap structural outline of a text file: definitions and headings with line numbers."""
import os
import re

PATTERNS = {
    ".py": r"^\s*(async\s+def|def|class)\s+\w+",
    ".js": r"^\s*(export\s+)?(default\s+)?(async\s+)?(function\*?\s+\w+|class\s+\w+|const\s+\w+\s*=\s*(async\s*)?(\(|function))",
    ".ts": r"^\s*(export\s+)?(default\s+)?(async\s+)?(function\*?\s+\w+|class\s+\w+|interface\s+\w+|type\s+\w+\s*=|const\s+\w+\s*=\s*(async\s*)?(\(|function))",
    ".go": r"^(func|type)\s+",
    ".rs": r"^\s*(pub\s+)?(fn|struct|enum|trait|impl|mod)\s+",
    ".rb": r"^\s*(def|class|module)\s+",
    ".java": r"^\s*(public|private|protected).*(class|interface|\(.*\)\s*\{)",
    ".swift": r"^\s*(func|class|struct|enum|protocol|extension)\s+",
    ".md": r"^#{1,4}\s",
    ".html": r"<(h[1-3]|section|script|style|template)\b|^\s*(function\s+\w+|class\s+\w+)",
    ".css": r"^[^\s{][^{]*\{",
    ".sql": r"^\s*(create|alter)\s+(table|index|view|trigger)",
    ".sh": r"^\s*(function\s+)?\w+\s*\(\)\s*\{",
    ".yaml": r"^\S[^:]*:",
    ".json": None,
}
for _a, _b in ((".jsx", ".js"), (".mjs", ".js"), (".cjs", ".js"), (".tsx", ".ts"), (".htm", ".html"),
               (".kt", ".java"), (".zsh", ".sh"), (".yml", ".yaml"), (".markdown", ".md")):
    PATTERNS[_a] = PATTERNS[_b]


def outline(path, max_lines=120):
    ext = os.path.splitext(path)[1].lower()
    pat = PATTERNS.get(ext)
    try:
        with open(path, errors="replace") as fh:
            lines = fh.read().split("\n")
    except OSError:
        return ""
    hits = []
    if pat:
        rx = re.compile(pat, re.I)
        hits = [(i + 1, l.strip()[:110]) for i, l in enumerate(lines) if rx.search(l)]
    if not hits:  # unknown type: a sparse sample so Claude can still aim
        step = max(len(lines) // 40, 1)
        hits = [(i + 1, lines[i].strip()[:110]) for i in range(0, len(lines), step) if lines[i].strip()]
    if len(hits) > max_lines:
        step = len(hits) / max_lines
        hits = [hits[int(k * step)] for k in range(max_lines)]
    body = "\n".join("%6d  %s" % h for h in hits)
    return "%d lines total.\n%s" % (len(lines), body)
