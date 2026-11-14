#!/usr/bin/env python3
"""Ping IndexNow (Bing, Yandex, Naver, Seznam...) with every URL in docs/sitemap.xml.

Run after a deploy: python3 scripts/indexnow.py
The key is the name of the docs/<32 hex>.txt file whose body equals its name.
"""
import json, pathlib, re, sys, urllib.request

HOST = "tokentrim.app"
docs = pathlib.Path(__file__).resolve().parent.parent / "docs"
key = next(p.stem for p in docs.glob("*.txt")
           if re.fullmatch(r"[0-9a-f]{32}", p.stem) and p.read_text().strip() == p.stem)
urls = re.findall(r"<loc>(.*?)</loc>", (docs / "sitemap.xml").read_text())
body = json.dumps({"host": HOST, "key": key,
                   "keyLocation": f"https://{HOST}/{key}.txt", "urlList": urls}).encode()
req = urllib.request.Request("https://api.indexnow.org/indexnow", data=body,
                             headers={"Content-Type": "application/json; charset=utf-8"})
try:
    with urllib.request.urlopen(req, timeout=30) as r:
        print(f"IndexNow: {r.status} for {len(urls)} URLs")
except urllib.error.HTTPError as e:
    print(f"IndexNow: HTTP {e.code} {e.read()[:200]!r}")
    sys.exit(0 if e.code in (200, 202) else 1)
