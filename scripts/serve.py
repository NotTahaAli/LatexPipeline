#!/usr/bin/env python3
# ruff: noqa: E501  (the embedded page has long CSS/JS lines)
"""
Live PDF preview: rebuilds documents on save and shows them in the browser
(PDF.js) with build status, an error overlay and SyncTeX forward/inverse search.

    python scripts/serve.py [DOC ...] [--port 8000] [--no-open]

Stdlib only. Binds 127.0.0.1 unless --host says otherwise.
"""

from __future__ import annotations

import argparse
import json
import posixpath
import queue
import subprocess
import sys
import threading
import time
import webbrowser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, unquote, urlsplit

import build

PDFJS = "https://cdnjs.cloudflare.com/ajax/libs/pdf.js/4.4.168"
LOOPBACK_HOSTS = {"localhost", "127.0.0.1", "[::1]"}

STOP = threading.Event()
LOCK = threading.Lock()
STATE: dict[str, dict] = {}  # doc name -> status dict sent to browsers
DOCS: dict[str, Path] = {}  # doc name -> main.tex
FORCE: set[str] = set()  # documents the browser asked to rebuild from scratch
CLIENTS: list[queue.Queue] = []
SETTINGS = {"editor": "vscode", "check_host": True}


# ---------------------------------------------------------------------------
# State and events
# ---------------------------------------------------------------------------

def editor_link(path: Path, line: int) -> str:
    """vscode://file/<abs path>:<line>. Windows C:\\x\\y becomes C:/x/y."""
    posix = path.resolve().as_posix()
    return f"{SETTINGS['editor']}://file{'' if posix.startswith('/') else '/'}{posix}:{line}"


def pdf_version(main_tex: Path) -> str | None:
    try:
        return str(build.output_path_for(main_tex).stat().st_mtime_ns)
    except OSError:
        return None


def broadcast(event: str, data: dict) -> None:
    message = f"event: {event}\ndata: {json.dumps(data)}\n\n"
    with LOCK:
        for client in CLIENTS:
            client.put(message)


def publish(name: str, **changes) -> None:
    with LOCK:
        STATE[name].update(changes)
    broadcast("state", snapshot())


def snapshot() -> dict:
    with LOCK:
        return {"docs": list(STATE.values())}


def fresh_state(name: str, main_tex: Path) -> dict:
    return {
        "name": name, "status": "idle", "ok": None, "seconds": None, "pages": None, "warnings": 0,
        "engine": None, "error": None, "errors": [], "finished": None, "version": pdf_version(main_tex),
    }


def run_build(main_tex: Path, latexmk: str, force: bool) -> None:
    name = build.doc_name(main_tex)
    publish(name, status="building")
    entry, _ = build.build_safely(main_tex, latexmk, False, force)
    errors = [
        {**found, "link": editor_link(main_tex.parent / found["file"], found["line"])}
        for found in entry["errors"]
    ]
    publish(
        name, status="ok" if entry["ok"] else "failed", ok=entry["ok"], seconds=entry["seconds"],
        pages=entry["pages"], warnings=entry["warnings"], engine=entry["engine"], error=entry["error"],
        errors=errors, finished=time.time(), version=pdf_version(main_tex),
    )
    build.info(f"{'ok    ' if entry['ok'] else 'FAILED'} {name} ({entry['seconds']}s)")


def watcher(latexmk: str, patterns: list[str], interval: float = 0.5) -> None:
    """build.watch() logic, per document, publishing state instead of printing."""
    # ponytail: polling and one build at a time; fine for a handful of documents.
    failed: dict[Path, float] = {}  # inputs timestamp of the last failed build
    seen: dict[Path, float] = {}  # inputs timestamp of the previous poll (debounce)

    while not STOP.is_set():
        for main_tex in build.select_documents(build.find_documents(), patterns):
            name = build.doc_name(main_tex)
            with LOCK:
                new = name not in STATE
                if new:
                    DOCS[name] = main_tex
                    STATE[name] = fresh_state(name, main_tex)
                forced = name in FORCE
                FORCE.discard(name)
            if new:
                broadcast("state", snapshot())

            if forced:
                run_build(main_tex, latexmk, True)
                continue
            if not build.is_stale(main_tex):
                seen.pop(main_tex, None)
                continue

            newest = build.newest_input(main_tex)
            if failed.get(main_tex) == newest:
                continue
            if seen.get(main_tex) != newest:
                seen[main_tex] = newest
                continue

            del seen[main_tex]
            run_build(main_tex, latexmk, False)
            if STATE[name]["ok"]:
                failed.pop(main_tex, None)
            else:
                failed[main_tex] = newest

        STOP.wait(interval)


# ---------------------------------------------------------------------------
# SyncTeX
# ---------------------------------------------------------------------------

class SynctexError(Exception):
    pass


def synctex(main_tex: Path, kind: str, spec: str) -> dict[str, str]:
    """Run the synctex CLI against the cached PDF (its .synctex.gz sits beside it)."""
    # ponytail: the cache PDF can be newer than the out/ PDF after a failed build; line-level error is small.
    pdf = build.cache_dir_for(main_tex) / f"{main_tex.stem}.pdf"
    if not pdf.with_name(f"{main_tex.stem}.synctex.gz").exists():
        raise SynctexError("No .synctex.gz: build with latexmk_args -synctex=1.")
    argv = ["edit", "-o", f"{spec}:{pdf}"] if kind == "edit" else ["view", "-i", spec, "-o", str(pdf)]
    try:
        result = subprocess.run(
            ["synctex", *argv], capture_output=True, text=True, errors="replace", timeout=15, cwd=main_tex.parent,
        )
    except FileNotFoundError:
        raise SynctexError("The synctex command was not found on PATH (it ships with TeX Live).")
    except subprocess.TimeoutExpired:
        raise SynctexError("synctex timed out.")

    fields: dict[str, str] = {}
    for line in result.stdout.splitlines():
        key, sep, value = line.partition(":")
        if sep and key not in fields:
            fields[key] = value.strip()
    if "Page" not in fields and "Input" not in fields:
        raise SynctexError("No match at that position.")
    return fields


def number(query: dict, key: str, default: str | None = None) -> float:
    try:
        return float(query.get(key, [default])[0])
    except (TypeError, ValueError):
        raise SynctexError(f"Bad or missing parameter: {key}")


def inverse(main_tex: Path, query: dict) -> dict:
    page, x, y = number(query, "page"), number(query, "x"), number(query, "y")
    fields = synctex(main_tex, "edit", f"{int(page)}:{x:.2f}:{y:.2f}")
    source = Path(fields["Input"])
    if not source.is_absolute():
        source = main_tex.parent / source
    source = Path(posixpath.normpath(source.as_posix()))
    line = max(1, int(fields.get("Line", "1")))
    return {"file": source.as_posix(), "line": line, "link": editor_link(source, line)}


def forward(query: dict) -> tuple[str, dict]:
    """Returns (doc name, {page, x, y, w, h}) in PDF points from the page's top-left."""
    raw = Path(query.get("file", [""])[0])
    name = query.get("doc", [""])[0]
    candidates = [raw] if raw.is_absolute() else [build.ROOT_DIR / raw, DOCS[name].parent / raw if name in DOCS else raw]
    source = next((c.resolve() for c in candidates if c.is_file()), None)
    if source is None:
        raise SynctexError(f"No such file: {raw}")
    owner = max((n for n, d in DOCS.items() if d.parent.resolve() in source.parents), key=len, default=None)
    if owner is None:
        raise SynctexError(f"{raw} is not inside a served document's directory.")
    main_tex = DOCS[owner]
    line = int(number(query, "line", "1"))
    column = int(number(query, "col", "-1"))
    fields = synctex(main_tex, "view", f"{line}:{column}:{source}")
    height = float(fields.get("H", 0))
    return owner, {
        "page": int(fields["Page"]), "x": float(fields["h"]), "y": float(fields["v"]) - height,
        "w": float(fields["W"]), "h": height + float(fields.get("D", 0)),
    }


# ---------------------------------------------------------------------------
# HTTP
# ---------------------------------------------------------------------------

class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.0"

    def log_message(self, *args) -> None:  # Quiet: build output is the interesting part.
        pass

    def reply(self, status: int, body: bytes, kind: str) -> None:
        self.send_response(status)
        self.send_header("Content-Type", kind)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def json(self, data: dict, status: int = 200) -> None:
        self.reply(status, json.dumps(data).encode(), "application/json")

    def allowed(self) -> bool:
        """Refuse foreign Host headers (DNS rebinding) while bound to loopback."""
        host = self.headers.get("Host", "")
        host = host if host.endswith("]") else host.rsplit(":", 1)[0]
        if SETTINGS["check_host"] and host not in LOOPBACK_HOSTS:
            self.reply(403, b"Forbidden host", "text/plain")
            return False
        return True

    def do_POST(self) -> None:
        if not self.allowed():
            return
        url = urlsplit(self.path)
        name = parse_qs(url.query).get("doc", [""])[0]
        if url.path == "/rebuild" and name in DOCS:
            with LOCK:
                FORCE.add(name)
            self.json({"ok": True})
        else:
            self.reply(404, b"Not found", "text/plain")

    def do_GET(self) -> None:
        if not self.allowed():
            return
        url = urlsplit(self.path)
        query = parse_qs(url.query)
        path = unquote(url.path)

        try:
            if path == "/":
                self.reply(200, PAGE.replace("__PDFJS__", PDFJS).encode(), "text/html; charset=utf-8")
            elif path == "/events":
                self.events()
            elif path.startswith(("/pdf/", "/log/")):
                self.file(path)
            elif path == "/synctex/edit":
                name = query.get("doc", [""])[0]
                if name not in DOCS:
                    raise SynctexError("Unknown document.")
                self.json(inverse(DOCS[name], query))
            elif path == "/forward":
                name, box = forward(query)
                box["doc"] = name
                if "quiet" not in query:
                    broadcast("forward", box)
                self.json(box)
            else:
                self.reply(404, b"Not found", "text/plain")
        except SynctexError as exc:
            self.json({"error": str(exc)}, 400)
        except (BrokenPipeError, ConnectionError):
            pass

    def file(self, path: str) -> None:
        kind, _, name = path[1:].partition("/")
        if name not in DOCS:
            self.reply(404, b"Unknown document", "text/plain")
            return
        main_tex = DOCS[name]
        target = build.output_path_for(main_tex) if kind == "pdf" else build.log_path_for(main_tex)
        try:
            body = target.read_bytes()
        except OSError:
            self.reply(404, b"Not built yet", "text/plain")
            return
        self.reply(200, body, "application/pdf" if kind == "pdf" else "text/plain; charset=utf-8")

    def events(self) -> None:
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream")
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        mine: queue.Queue = queue.Queue()
        mine.put(f"event: state\ndata: {json.dumps(snapshot())}\n\n")
        with LOCK:
            CLIENTS.append(mine)
        try:
            while not STOP.is_set():
                try:
                    message = mine.get(timeout=10)
                except queue.Empty:
                    message = ": keepalive\n\n"  # Also detects closed tabs.
                self.wfile.write(message.encode())
                self.wfile.flush()
        except OSError:
            pass
        finally:
            with LOCK:
                CLIENTS.remove(mine)


PAGE = r"""<!doctype html>
<html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>LaTeX Preview</title>
<style>
:root{--bg:#f3f3f5;--bar:#fff;--fg:#1d1d1f;--mute:#6b6b73;--line:#d8d8de;--ok:#1a7f37;--bad:#cf222e;--busy:#9a6700;--acc:#0969da}
@media(prefers-color-scheme:dark){:root{--bg:#1c1c1f;--bar:#26262a;--fg:#e8e8ea;--mute:#9a9aa3;--line:#3a3a40;--ok:#3fb950;--bad:#ff7b72;--busy:#d29922;--acc:#58a6ff}}
*{box-sizing:border-box}
body{margin:0;background:var(--bg);color:var(--fg);font:14px system-ui,sans-serif;height:100vh;display:flex;flex-direction:column}
header{display:flex;flex-wrap:wrap;gap:8px;align-items:center;padding:6px 16px;background:var(--bar);border-bottom:1px solid var(--line)}
header>*{flex:none} select,input,button{font:inherit;color:inherit;background:var(--bg);border:1px solid var(--line);border-radius:6px;padding:3px 8px}
button{cursor:pointer} input{width:12em}
#status{display:flex;gap:8px;align-items:center;flex:1 1 auto;min-width:12em}
.dot{width:10px;height:10px;border-radius:50%;background:var(--mute)}
.ok .dot{background:var(--ok)}.failed .dot{background:var(--bad)}.building .dot{background:var(--busy);animation:p 1s infinite}
@keyframes p{50%{opacity:.25}}
#info{color:var(--mute)} a{color:var(--acc)}
#stage{flex:1;min-height:0;position:relative}#viewer{height:100%;overflow:auto}
#pages{padding:16px;display:flex;flex-direction:column;align-items:center;gap:12px;position:relative}
.page{position:relative;background:#fff;box-shadow:0 1px 4px #0005;flex:none}
.page canvas{width:100%;height:100%;display:block}
.hit{position:absolute;background:#ffe60080;outline:2px solid #ffb000;animation:f 3s forwards;pointer-events:none}
@keyframes f{70%{opacity:1}to{opacity:0}}
#empty{padding:48px;text-align:center;color:var(--mute)}
#errors{position:absolute;left:16px;right:16px;bottom:16px;max-height:45%;overflow:auto;background:var(--bar);border:1px solid var(--bad);border-radius:8px;padding:8px 12px;box-shadow:0 4px 16px #0006}
#errors h4{margin:0 0 6px;color:var(--bad);display:flex;justify-content:space-between}
#errors ul{margin:0;padding:0;list-style:none} #errors li{padding:3px 0;font:13px ui-monospace,monospace;word-break:break-word}
#toast{position:fixed;top:56px;right:16px;background:var(--bar);border:1px solid var(--line);border-radius:6px;padding:6px 10px;box-shadow:0 4px 16px #0005}
[hidden]{display:none!important}
</style></head><body>
<header>
 <select id="doc" title="Document"></select>
 <div id="status"><span class="dot"></span><b id="label">Connecting</b><span id="info"></span></div>
 <button id="out" title="Zoom out">-</button><button id="in" title="Zoom in">+</button><button id="fit">Fit</button>
 <input id="fwd" placeholder="main.tex:12 (forward)" title="SyncTeX forward search: file:line">
 <button id="rebuild" title="Rebuild from scratch">Rebuild</button><a id="log" target="_blank">Log</a>
</header>
<div id="stage"><div id="viewer"><div id="pages"></div><div id="empty" hidden></div></div>
 <div id="errors" hidden><h4><span id="etitle"></span><button id="close">x</button></h4><ul id="elist"></ul></div></div>
<div id="toast" hidden></div>
<script type="module">
import * as pdfjs from "__PDFJS__/pdf.min.mjs";
pdfjs.GlobalWorkerOptions.workerSrc = "__PDFJS__/pdf.worker.min.mjs";
const $ = id => document.getElementById(id);
const store = { get(k) { try { return localStorage.getItem(k); } catch { return null; } },
                set(k, v) { try { localStorage.setItem(k, v); } catch {} } };
let docs = {}, cur = decodeURIComponent(location.hash.slice(1)), pdf = null, loaded = null, seq = 0;
let zoom = parseFloat(store.get("zoom")) || 1.33, sizes = [], els = [], dismissed = null;
const viewer = $("viewer"), pagesEl = $("pages");
const url = (kind, name) => `${kind}/${name.split("/").map(encodeURIComponent).join("/")}`;

const io = new IntersectionObserver(es => es.forEach(e => e.isIntersecting && render(+e.target.dataset.i)),
                                    { root: viewer, rootMargin: "800px 0px" });

async function render(i) {
  const el = els[i], doc = pdf; if (!el || !doc) return;
  const token = el.token = (el.token || 0) + 1;
  const page = await doc.getPage(i + 1);
  const vp = page.getViewport({ scale: zoom * (window.devicePixelRatio || 1) });
  const canvas = document.createElement("canvas");
  canvas.width = vp.width; canvas.height = vp.height;
  await page.render({ canvasContext: canvas.getContext("2d"), viewport: vp }).promise;
  if (token !== el.token) return;
  el.querySelector("canvas")?.remove();   // Swap in one step: the old render stays visible until now.
  el.prepend(canvas);
}

function layout() {
  sizes.forEach(([w, h], i) => {
    let el = els[i];
    if (!el) { el = els[i] = document.createElement("div"); el.className = "page"; el.dataset.i = i; pagesEl.append(el); }
    el.style.width = w * zoom + "px"; el.style.height = h * zoom + "px";
    io.unobserve(el); io.observe(el);    // Re-observe: fires again for pages already on screen.
  });
  while (els.length > sizes.length) { const el = els.pop(); io.unobserve(el); el.remove(); }
}

async function load() {
  const d = docs[cur];
  if (!d || !d.version) { pdf = null; loaded = null; sizes = []; layout(); showEmpty(d ? "Not built yet. " + (d.status === "failed" ? "The build failed." : "Building...") : "No such document."); return; }
  $("empty").hidden = true;
  const mine = ++seq, version = d.version;
  const next = await pdfjs.getDocument(url("pdf", cur) + "?v=" + version).promise;
  if (mine !== seq) return;
  const pages = await Promise.all(Array.from({ length: next.numPages }, (_, i) => next.getPage(i + 1)));
  if (mine !== seq) return;
  const top = viewer.scrollTop, before = pagesEl.scrollHeight;
  pdf = next; loaded = version;
  sizes = pages.map(p => { const v = p.getViewport({ scale: 1 }); return [v.width, v.height]; });
  layout();
  viewer.scrollTop = top;  // Scroll and zoom survive: pages keep their boxes until the new canvas is ready.
}

function showEmpty(text) { $("empty").textContent = text; $("empty").hidden = false; }

function status() {
  const d = docs[cur]; if (!d) return;
  $("status").className = d.status;
  $("label").textContent = { idle: "Up to date", building: "Building...", ok: "Built", failed: "Build failed" }[d.status];
  const bits = [];
  if (d.seconds != null) bits.push(d.seconds + "s");
  if (d.pages != null) bits.push(d.pages + (d.pages === 1 ? " page" : " pages"));
  if (d.warnings) bits.push(d.warnings + " warning" + (d.warnings === 1 ? "" : "s"));
  if (d.finished) bits.push(new Date(d.finished * 1000).toLocaleTimeString());
  if (d.status === "failed" && d.version) bits.push("showing last good PDF");
  $("info").textContent = bits.join(" · ");
  $("log").href = url("log", cur);
  const key = d.finished;
  const bad = d.status === "failed";
  if (!bad) dismissed = null;
  $("errors").hidden = !bad || dismissed === key;
  if (bad) {
    $("etitle").textContent = `${d.errors.length || 1} error${d.errors.length === 1 ? "" : "s"}`;
    $("elist").replaceChildren(...(d.errors.length ? d.errors : [{ message: d.error || "See the log." }]).map(e => {
      const li = document.createElement("li");
      if (e.link) { const a = document.createElement("a"); a.href = e.link; a.textContent = `${e.file}:${e.line}`; li.append(a, ": " + e.message); }
      else li.textContent = e.message;
      return li;
    }));
    if (d.error && d.errors.length) { const li = document.createElement("li"); li.textContent = d.error; $("elist").prepend(li); }
  }
}
$("close").onclick = () => { dismissed = docs[cur]?.finished; $("errors").hidden = true; };

function pick(name) {
  cur = name; location.hash = encodeURIComponent(name); store.set("doc", name);
  $("doc").value = name; loaded = null; status(); load();
}

const events = new EventSource("events");
events.addEventListener("state", e => {
  const list = JSON.parse(e.data).docs;
  docs = Object.fromEntries(list.map(d => [d.name, d]));
  const names = list.map(d => d.name);
  if ([...$("doc").options].map(o => o.value).join("\n") !== names.join("\n"))
    $("doc").replaceChildren(...names.map(n => new Option(n, n)));
  if (!docs[cur]) { pick(docs[store.get("doc")] ? store.get("doc") : names[0]); return; }
  $("doc").value = cur; status();
  if (docs[cur].version !== loaded) load();
});
events.addEventListener("forward", e => show(JSON.parse(e.data)));
events.onerror = () => { $("label").textContent = "Disconnected"; };
$("doc").onchange = e => pick(e.target.value);
$("rebuild").onclick = () => fetch("rebuild?doc=" + encodeURIComponent(cur), { method: "POST" });

function setZoom(z) {
  const ratio = z / zoom; zoom = Math.max(0.3, Math.min(5, z)); store.set("zoom", zoom);
  const top = viewer.scrollTop * ratio; layout(); viewer.scrollTop = top;
}
$("in").onclick = () => setZoom(zoom * 1.2);
$("out").onclick = () => setZoom(zoom / 1.2);
$("fit").onclick = () => sizes.length && setZoom((viewer.clientWidth - 32) / Math.max(...sizes.map(s => s[0])));

function toast(html) { const t = $("toast"); t.replaceChildren(html); t.hidden = false; clearTimeout(t.t); t.t = setTimeout(() => t.hidden = true, 6000); }
const text = s => document.createTextNode(s);

async function inverse(e) {
  const el = e.target.closest(".page"); if (!el) return;
  const r = el.getBoundingClientRect();
  const q = new URLSearchParams({ doc: cur, page: +el.dataset.i + 1, x: (e.clientX - r.left) / zoom, y: (e.clientY - r.top) / zoom });
  const res = await (await fetch("synctex/edit?" + q)).json();
  if (res.error) return toast(text(res.error));
  const a = document.createElement("a"); a.href = res.link; a.textContent = `${res.file}:${res.line}`;
  toast(a); location.href = res.link;
}
viewer.addEventListener("dblclick", inverse);
viewer.addEventListener("click", e => (e.ctrlKey || e.metaKey) && inverse(e));

function show(b) {   // Scroll to a forward-search result and highlight its box.
  if (b.doc !== cur) pick(b.doc);
  const el = els[b.page - 1]; if (!el) return;
  const hit = document.createElement("div"); hit.className = "hit";
  Object.assign(hit.style, { left: b.x * zoom + "px", top: b.y * zoom + "px", width: Math.max(b.w, 4) * zoom + "px", height: Math.max(b.h, 4) * zoom + "px" });
  el.append(hit); setTimeout(() => hit.remove(), 3000);
  viewer.scrollTo({ top: el.offsetTop + b.y * zoom - viewer.clientHeight / 3, behavior: "smooth" });
}
$("fwd").addEventListener("keydown", async e => {
  if (e.key !== "Enter") return;
  const v = $("fwd").value.trim(), at = v.lastIndexOf(":");
  const file = at < 0 ? "main.tex" : v.slice(0, at), line = at < 0 ? v : v.slice(at + 1);
  const res = await (await fetch("forward?" + new URLSearchParams({ doc: cur, file, line, quiet: 1 }))).json();
  res.error ? toast(text(res.error)) : show(res);
});
</script></body></html>
"""


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def main() -> int:
    parser = argparse.ArgumentParser(description="Live PDF preview with SyncTeX.")
    parser.add_argument("docs", nargs="*", metavar="DOC", help="Documents to serve (name or glob). Default: all.")
    parser.add_argument("--port", type=int, default=8000)
    parser.add_argument("--host", default="127.0.0.1", help="Bind address (default: 127.0.0.1, this machine only).")
    parser.add_argument("--no-open", action="store_true", help="Do not open a browser.")
    parser.add_argument("--editor", default="vscode", help="URL scheme for source links (vscode, vscode-insiders, cursor).")
    args = parser.parse_args()

    documents = build.find_documents()
    unknown = build.unknown_patterns(documents, args.docs)
    if unknown:
        build.error(f"No document matches: {', '.join(unknown)}")
        return 2

    latexmk = build.check_latex()
    SETTINGS["editor"] = args.editor
    SETTINGS["check_host"] = args.host in LOOPBACK_HOSTS | {"::1"}
    if not SETTINGS["check_host"]:
        build.error(f"Listening on {args.host}: anyone who can reach this port can read your PDFs and run synctex.")

    try:
        server = ThreadingHTTPServer((args.host, args.port), Handler)
    except OSError as exc:
        build.error(f"Cannot listen on {args.host}:{args.port}: {exc}")
        return 1
    server.daemon_threads = True

    port = server.server_address[1]
    address = f"http://{'localhost' if args.host == '127.0.0.1' else args.host}:{port}/"
    first = build.select_documents(documents, args.docs)
    if first:
        address += "#" + build.doc_name(first[0]).replace(" ", "%20")
    build.info(f"Serving {address}  (Ctrl+C to stop)")

    threading.Thread(target=watcher, args=(latexmk, args.docs), daemon=True).start()
    if not args.no_open:
        threading.Timer(0.3, webbrowser.open, (address,)).start()

    try:
        server.serve_forever()
    except KeyboardInterrupt:
        build.info("\nStopping.")
    finally:
        STOP.set()
        server.server_close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
