#!/usr/bin/env python3
"""Download the editor's pinned third-party code into scripts/serve_ui/vendor/ so the editor works offline.

The URLs come from the import map in serve_ui/index.html (the single place they are pinned). Modules are
fetched with the modules they import, their imports rewritten to local paths, and manifest.json records
url -> file + sha256. serve.py serves exactly the files in that manifest and drops the CDNs from its CSP.
Delete the vendor directory to go back to the CDNs. Stdlib only.
"""
from __future__ import annotations

import hashlib
import json
import re
import shutil
import sys
import urllib.request
from pathlib import Path
from urllib.parse import urljoin, urlsplit

UI_DIR = Path(__file__).resolve().parent / "serve_ui"
VENDOR = UI_DIR / "vendor"
IMPORT = re.compile(r"""((?:\bfrom|\bimport)\s*\(?\s*)(["'])((?:https?:)?[/.][^"']*)\2""")
CSS_URL = re.compile(r"url\(([^)]+)\)")
# Only woff2 is kept: every browser the editor supports prefers it, so woff and ttf are dead weight.
CSS_FALLBACK = re.compile(r",\s*url\([^)]*\)\s*format\(\"(?:woff|truetype)\"\)")


def local_name(url: str) -> str:
    base = re.sub(r"[^\w.-]", "_", urlsplit(url).path.rsplit("/", 1)[-1])
    ext = next((e for e in (".woff2", ".css") if base.endswith(e)), ".js")  # the server picks the MIME type from this
    return f"{hashlib.sha1(url.encode()).hexdigest()[:8]}-{base}" + ("" if base.endswith(ext) else ext)


def fetch(url: str) -> bytes:
    req = urllib.request.Request(url, headers={"User-Agent": "latex-pipeline-vendor"})
    with urllib.request.urlopen(req, timeout=60) as res:
        return res.read()


def rewrite(url: str, body: bytes) -> tuple[bytes, list[str]]:
    """Point a module's or stylesheet's references at local files; return the new body and the URLs it needs."""
    needed: list[str] = []
    text = body.decode("utf-8")

    def ref(spec: str) -> str:
        target = urljoin(url, spec)
        if target not in needed:
            needed.append(target)
        return f"./{local_name(target)}"  # all files sit in one folder: relative works under any path prefix

    if url.endswith(".css"):
        text = CSS_FALLBACK.sub("", text)
        text = CSS_URL.sub(lambda m: f"url({ref(m.group(1).strip(chr(34) + chr(39)))})", text)
    else:
        text = IMPORT.sub(lambda m: f"{m.group(1)}{m.group(2)}{ref(m.group(3))}{m.group(2)}", text)
    return text.encode("utf-8"), needed


def main(out: Path = VENDOR, getter=fetch) -> int:
    page = (UI_DIR / "index.html").read_text("utf-8")
    block = re.search(r'<script type="importmap">(.*?)</script>', page, re.S).group(1)
    roots = list(json.loads(block)["imports"].values())
    if out.exists():
        shutil.rmtree(out)
    out.mkdir(parents=True)
    files: dict[str, dict] = {}
    queue = list(roots)
    while queue:
        url = queue.pop()
        if url in files:
            continue
        body = getter(url)
        if not url.endswith(".woff2"):
            body, needed = rewrite(url, body)
            queue.extend(needed)
        name = local_name(url)
        (out / name).write_bytes(body)
        files[url] = {"file": name, "sha256": hashlib.sha256(body).hexdigest()}
        print(f"{len(body) // 1024:6d} KB  {url[:100]}")
    (out / "manifest.json").write_text(json.dumps({"files": files}, indent=1, sort_keys=True), "utf-8")
    total = sum(f.stat().st_size for f in out.iterdir())
    print(f"{len(files)} files, {total / 1e6:.2f} MB in {out}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
