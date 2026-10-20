#!/usr/bin/env python3
"""Render JARVIS pages: content/jarvis/<slug>.json -> /<slug>/index.html in this site's own shell.

The sales-ops publisher commits one JSON per approved page (path, title, h1, description, html, ld). This turns each
into a page that borrows the home page's head styles, header and footer, lists the pages in the sitemap, and links
them from the home page. It never overwrites a page it did not write, removes pages whose JSON is gone, and is safe to
run any number of times — the site generator calls it last, so regenerating the site keeps the pages.
"""
import html, json, re, shutil
from datetime import date
from pathlib import Path

HERE = Path(__file__).resolve().parent
ROOT = HERE / "docs"  # the folder Pages serves
BASE = "https://suncal.github.io/tokentrim"  # public URL of ROOT, no trailing slash
HOME = ROOT / "index.html"
SITEMAP = ROOT / "sitemap.xml"
SRC = HERE / "content" / "jarvis"
MARK = '<meta name="generator" content="jarvis-publisher">'
PATH = re.compile(r"^/[a-z0-9][a-z0-9-]{0,90}/$")
LOCAL = re.compile(r'((?:href|src)=")(?!https?:|//|/|#|mailto:|tel:|data:|javascript:)([^"]*")')


def _first(pat, text):
    m = re.search(pat, text, re.S | re.I)
    return m.group(0) if m else ""


def shell():
    """(head extras, header, footer) lifted from the home page, with its relative links pointed one folder up."""
    home = re.sub(r"<!-- jarvis:links -->.*?<!-- /jarvis:links -->\n?", "", HOME.read_text(encoding="utf-8"), flags=re.S)
    head = _first(r"<head\b.*?</head>", home)
    keep = re.findall(r'<link\b[^>]*rel="(?:stylesheet|preconnect|icon|apple-touch-icon|shortcut icon)"[^>]*>'
                      r'|<link\b[^>]*href="[^"]*"[^>]*rel="(?:stylesheet|preconnect|icon)"[^>]*>'
                      r'|<style\b.*?</style>'
                      r'|<script\b(?![^>]*ld\+json).*?</script>', head, re.S | re.I)
    # The site chrome is whatever opens the body up to the last </nav> or </header> before the content starts
    # (some sites call their hero a <header>, so that one does not count).
    body = home[home.find(">", home.find("<body")) + 1:]
    content = re.search(r'<(?:main|section|article)\b|<(?:header|div)\b[^>]*class="[^"]*\bhero', body)
    top = body[:content.start() if content else 0]
    ends = [m.end() for m in re.finditer(r"</(?:nav|header)>", top)]
    header = top[:ends[-1]] if ends else ""
    footers = list(re.finditer(r"<footer\b.*?</footer>", body, re.S | re.I))
    footer = footers[-1].group(0) if footers else ""
    if footers:   # the site-wide scripts after the footer (menus, analytics); inline ones are page-specific
        footer += "\n" + "\n".join(re.findall(r"<script\b[^>]*\bsrc=[^>]*>\s*</script>", body[footers[-1].end():], re.I))
    up = lambda s: LOCAL.sub(lambda m: m.group(1) + "../" + m.group(2), s)
    return up("\n".join(keep)), up(header), up(footer)


def docs():
    for f in sorted(SRC.glob("*.json")) if SRC.is_dir() else []:
        try:
            d = json.loads(f.read_text(encoding="utf-8"))
        except ValueError:
            continue
        if PATH.match(d.get("path", "")) and d.get("html"):
            yield d


def ours(p):
    return p.exists() and MARK in p.read_text(encoding="utf-8", errors="ignore")


def page(d, head, header, footer):
    e = lambda s: html.escape(s or "", quote=True)
    url = BASE + d["path"]
    ld = "".join('<script type="application/ld+json">' + json.dumps(j, ensure_ascii=False).replace("</", "<\\/") + "</script>\n"
                 for j in d.get("ld") or [])
    return f"""<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
{MARK}
<title>{e(d.get("title") or d.get("h1"))}</title>
<meta name="description" content="{e(d.get("description"))}">
<link rel="canonical" href="{url}">
<meta property="og:type" content="article">
<meta property="og:title" content="{e(d.get("title") or d.get("h1"))}">
<meta property="og:description" content="{e(d.get("description"))}">
<meta property="og:url" content="{url}">
{head}
{ld}<style>.jarvis-page{{max-width:760px;margin:0 auto;padding:40px 20px 64px;line-height:1.65}}.jarvis-page h1{{line-height:1.2;margin:0 0 18px}}.jarvis-page h2{{margin:32px 0 10px}}.jarvis-page li{{margin:6px 0}}.jarvis-page .upd{{opacity:.65;font-size:.9em;margin-top:36px}}</style>
</head>
<body>
{header}
<main class="jarvis-page">
{d["html"]}
<p class="upd">Updated {e(d.get("updated"))}</p>
</main>
{footer}
</body>
</html>
"""


def links_block(live):
    items = "".join(f'<a href="{d["path"].strip("/")}/" style="margin:0 14px 6px 0;display:inline-block">{html.escape(d.get("h1") or d.get("title"))}</a>'
                    for d in live)
    return ('<!-- jarvis:links -->\n<section class="jarvis-links" style="max-width:1100px;margin:0 auto;padding:28px 20px;font-size:15px">'
            f'<h2 style="font-size:17px;margin:0 0 10px">Guides</h2><nav>{items}</nav></section>\n<!-- /jarvis:links -->\n')


def link_home(live):
    t = HOME.read_text(encoding="utf-8")
    t2 = re.sub(r"<!-- jarvis:links -->.*?<!-- /jarvis:links -->\n?", "", t, flags=re.S)
    if live:
        i = t2.rfind("<footer")
        i = i if i >= 0 else t2.rfind("</body>")
        t2 = t2[:i] + links_block(live) + t2[i:]
    if t2 != t:
        HOME.write_text(t2, encoding="utf-8")


def sitemap(live):
    t = SITEMAP.read_text(encoding="utf-8")
    t2 = re.sub(r"\s*<!-- jarvis -->.*?<!-- /jarvis -->", "", t, flags=re.S)
    if "<sitemapindex" in t2:   # an index: the pages get their own sitemap file
        part = ROOT / "sitemap-jarvis.xml"
        if live:
            part.write_text('<?xml version="1.0" encoding="UTF-8"?>\n<urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">\n'
                            + "".join(f"  <url><loc>{BASE}{d['path']}</loc><lastmod>{d.get('updated') or date.today()}</lastmod></url>\n" for d in live)
                            + "</urlset>\n", encoding="utf-8")
            entry = f"<sitemap><loc>{BASE}/sitemap-jarvis.xml</loc><lastmod>{max(str(d.get('updated') or date.today()) for d in live)}</lastmod></sitemap>"
            t2 = t2.replace("</sitemapindex>", f"  <!-- jarvis -->{entry}<!-- /jarvis -->\n</sitemapindex>")
        elif part.exists():
            part.unlink()
    elif live:
        urls = "".join(f"<url><loc>{BASE}{d['path']}</loc><lastmod>{d.get('updated') or date.today()}</lastmod></url>" for d in live)
        t2 = t2.replace("</urlset>", f"  <!-- jarvis -->{urls}<!-- /jarvis -->\n</urlset>")
    if t2 != t:
        SITEMAP.write_text(t2, encoding="utf-8")


def main():
    head, header, footer = shell()
    live, want = [], set()
    for d in docs():
        out = ROOT / d["path"].strip("/") / "index.html"
        if out.exists() and not ours(out):
            print(f"jarvis: skip {d['path']} (an existing page owns it)")
            continue
        if (ROOT / (d["path"].strip("/") + ".html")).exists():
            print(f"jarvis: skip {d['path']} (an existing .html page owns it)")
            continue
        out.parent.mkdir(exist_ok=True)
        out.write_text(page(d, head, header, footer), encoding="utf-8")
        live.append(d); want.add(out.parent.name)
    for idx in ROOT.glob("*/index.html"):    # pages whose JSON was removed
        if idx.parent.name not in want and ours(idx):
            shutil.rmtree(idx.parent)
            print(f"jarvis: removed /{idx.parent.name}/")
    live.sort(key=lambda d: d["path"])
    link_home(live); sitemap(live)
    print(f"jarvis: {len(live)} page(s) live")


if __name__ == "__main__":
    main()
