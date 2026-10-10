#!/usr/bin/env python3
"""
Local LaTeX editor with live PDF preview.

    python scripts/serve.py [DOC ...] [--port 8000] [--no-open]

Serves a CodeMirror 6 editor (source plus an optional visual mode), a file tree, an
outline with word counts, build errors with hints, and a PDF.js preview with SyncTeX
in both directions. Saving a file triggers the usual debounced rebuild.

Stdlib only. Binds 127.0.0.1 unless --host says otherwise. The UI is static files in
serve_ui/. The browser talks to the server over one message bus (WebSocket, with a
long-poll fallback for tunnels); /events (SSE) stays as a legacy read-only endpoint.
"""

from __future__ import annotations

import argparse
import atexit
import base64
import bisect
import collections
import hashlib
import hmac
import http.client
import itertools
import json
import os
import posixpath
import re
import secrets
import shutil
import signal
import socket
import ssl
import struct
import subprocess
import sys
import tempfile
import threading
import time
import webbrowser
from http.cookies import SimpleCookie
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, quote, unquote, urlsplit

import bibfix
import build
import grammar
import hints

LOOPBACK_HOSTS = {"localhost", "127.0.0.1", "[::1]"}
UI_DIR = Path(__file__).resolve().parent / "serve_ui"
VENDOR_DIR = UI_DIR / "vendor"  # made by scripts/vendor_ui.py; when present the editor needs no CDN
STARTED = time.time()

STOP = threading.Event()
LOCK = threading.Lock()
STATE: dict[str, dict] = {}  # doc name -> status dict sent to browsers
DOCS: dict[str, Path] = {}  # doc name -> main.tex
FORCE: set[str] = set()  # documents the browser asked to rebuild from scratch
SETTINGS = {"editor": "vscode", "check_host": True, "latexmk": "latexmk"}
BUILD_LOCK = threading.RLock()  # One LaTeX run at a time: the watcher's builds and chapter previews share the cache.
FOCUS_BUSY: set[str] = set()  # documents with a chapter preview in flight
BUS_MAX_BYTES = 16 * 1024 * 1024  # JSON size of the durable log; the ephemeral one gets a quarter


# ---------------------------------------------------------------------------
# Message bus (transport-independent: WebSocket, long-poll and SSE all read it)
# ---------------------------------------------------------------------------

class Bus:
    """
    Message log with a revision counter; readers resume from a revision. Durable messages (state, edits) sit in a
    long log; ephemeral ones (cursors, presence) in a short one and are never the reason for a resync.
    """

    def __init__(self, keep: int = 2000, ephemeral: int = 300, max_bytes: int = BUS_MAX_BYTES) -> None:
        self.cond = threading.Condition()
        self.rev = 0
        self.floor = 0  # newest revision dropped from the durable log
        self.keep, self.keep_eph, self.max_bytes = keep, ephemeral, max_bytes
        self.log: collections.deque = collections.deque()  # (message, size)
        self.eph: collections.deque = collections.deque()
        self.bytes = {"log": 0, "eph": 0}

    def publish(self, type_: str, data, topic: str = "doc", ephemeral: bool = False) -> int:
        with self.cond:
            self.rev += 1
            message = {"rev": self.rev, "topic": topic, "type": type_, "data": data}
            size = len(json.dumps(data)) + 64
            which, queue, limit = ("eph", self.eph, self.keep_eph) if ephemeral else ("log", self.log, self.keep)
            queue.append((message, size))
            self.bytes[which] += size
            cap = self.max_bytes // 4 if ephemeral else self.max_bytes  # Memory is bounded by bytes, not only count.
            while len(queue) > 1 and (len(queue) > limit or self.bytes[which] > cap):
                old, old_size = queue.popleft()
                self.bytes[which] -= old_size
                if not ephemeral:
                    self.floor = old["rev"]
            self.cond.notify_all()
            return self.rev

    def since(self, rev: int | None) -> list[dict]:
        """Messages after rev. None, or a rev older than the log keeps, yields one fresh state snapshot."""
        with self.cond:
            if rev is None or rev > self.rev or rev < self.floor:
                return [{"rev": self.rev, "topic": "doc", "type": "state", "data": snapshot(), "resync": True}]
            return sorted(
                (m for m, _ in itertools.chain(self.log, self.eph) if m["rev"] > rev), key=lambda m: m["rev"],
            )

    def wait(self, rev: int, timeout: float) -> list[dict]:
        """Block until something newer than rev exists (or timeout); return it."""
        deadline = time.monotonic() + timeout
        with self.cond:
            if rev > self.rev:  # From a previous server run: resync now instead of holding the poll open.
                return self.since(None)
            while self.rev <= rev and not STOP.is_set():
                left = deadline - time.monotonic()
                if left <= 0:
                    break
                self.cond.wait(min(left, 1.0))
        return self.since(rev) if rev <= self.rev else self.since(None)


BUS = Bus()
HANDLERS: dict = {}  # client message type -> fn(message, client_id, role) -> reply dict | None


def handle_client_message(message: dict, client: str, role: str = "owner", user: str | None = None) -> dict | None:
    kind = message.get("type")
    if not bind_client(client, role, user):
        return {"type": "error", "topic": "sys", "data": {"error": "Client id belongs to another session."}}
    if kind == "ping":
        return {"type": "pong", "topic": "sys", "data": {"t": message.get("data"), "rev": BUS.rev}}
    handler = HANDLERS.get(kind)
    return handler(message, client, role) if handler else None


Y_KINDS = ("y-update", "y-aware", "y-leader", "y-gone", "y-closed")  # data["room"] is "<doc>\n<path>"


def visible(messages: list[dict], role: str) -> list[dict]:
    """
    What a role may see on the bus: shared sessions only hear about the shared document. A whitelist: a message
    type not handled here (or one naming another document or a build-config file) is dropped.
    """
    if role == "owner":
        return messages
    shared = SHARE["doc"]
    out = []
    for message in messages:
        kind, data = message["type"], message["data"]
        if kind == "state":
            message = {**message, "data": {"docs": [d for d in data["docs"] if d["name"] == shared]}}
        elif kind in ("fs", "forward"):
            if data.get("doc") != shared:
                continue
        elif kind in Y_KINDS:
            doc, _, path = str(data.get("room")).partition("\n")
            if doc != shared or is_rc(path):
                continue
        elif kind == "presence":
            message = {**message, "data": {"users": presence_for(data["users"], role)}}
        else:
            continue
        out.append(message)
    return out


# --- RFC 6455, just enough: handshake, text/binary frames, ping/pong, close ---

WS_GUID = "258EAFA5-E914-47DA-95CA-C5AB0DC85B11"
WS_MAX = 8 * 1024 * 1024


def ws_accept(key: str) -> str:
    return base64.b64encode(hashlib.sha1((key + WS_GUID).encode()).digest()).decode()


def ws_encode(opcode: int, payload: bytes, mask_key: bytes | None = None) -> bytes:
    """One unfragmented frame. Servers send unmasked; mask_key is for tests and clients."""
    size = len(payload)
    head = bytes([0x80 | opcode])
    flag = 0x80 if mask_key else 0
    if size < 126:
        head += bytes([flag | size])
    elif size < 65536:
        head += bytes([flag | 126]) + struct.pack(">H", size)
    else:
        head += bytes([flag | 127]) + struct.pack(">Q", size)
    if mask_key:
        payload = bytes(b ^ mask_key[i % 4] for i, b in enumerate(payload))
        head += mask_key
    return head + payload


def ws_read(read, partial: dict | None = None) -> tuple[int, bytes] | None:
    """
    Next complete message as (opcode, payload), joining fragments; control frames pass through (a ping may arrive
    between fragments, so pass the same `partial` dict on every call of one connection). None at EOF.
    """
    partial = {"op": 0, "buf": b""} if partial is None else partial
    partial.setdefault("op", 0)
    partial.setdefault("buf", b"")
    while True:
        head = read(2)
        if len(head) < 2:
            return None
        fin, opcode = head[0] & 0x80, head[0] & 0x0F
        masked, size = head[1] & 0x80, head[1] & 0x7F
        if size == 126:
            size = struct.unpack(">H", read(2))[0]
        elif size == 127:
            size = struct.unpack(">Q", read(8))[0]
        if size > WS_MAX:
            raise ValueError("frame too large")
        key = read(4) if masked else b""
        payload = read(size)
        if len(payload) < size:
            return None
        if masked:
            payload = bytes(b ^ key[i % 4] for i, b in enumerate(payload))
        if opcode >= 8:  # Control frames are never fragmented and may interleave.
            return opcode, payload
        if opcode:
            partial["op"] = opcode
        partial["buf"] += payload
        if len(partial["buf"]) > WS_MAX:
            raise ValueError("message too large")
        if fin:
            done = (partial["op"], partial["buf"])
            partial["op"], partial["buf"] = 0, b""
            return done


def broadcast(event: str, data) -> None:
    BUS.publish(event, data)


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


def publish(name: str, **changes) -> None:
    with LOCK:
        STATE[name].update(changes)
    broadcast("state", snapshot())


def snapshot() -> dict:
    with LOCK:
        return {"docs": list(STATE.values())}


def latex_section(text: str) -> str:
    """The LaTeX log inside out/<name>.log (the part before any BibTeX log); the whole text if it has no sections."""
    start = text.find("===== LaTeX log")
    if start < 0:
        return text
    end = text.find("\n===== BibTeX log", start)
    return text[start:end if end > 0 else len(text)]


ENGINES = {"pdfTeX": "pdflatex", "XeTeX": "xelatex", "LuaTeX": "lualatex", "LuaHBTeX": "lualatex"}


def saved_result(name: str, main_tex: Path) -> dict:
    """
    What out/ remembers of the last successful build (pages, warnings, engine, time), so "Up to date" after a
    restart still says how big the PDF is. Nothing when there is no PDF, or the log says the build failed.
    """
    try:
        text = build.log_path_for(main_tex).read_text(encoding="utf-8", errors="replace")
        finished = build.log_path_for(main_tex).stat().st_mtime
    except (OSError, ValueError):  # ValueError: a document outside the repository has no out/ path.
        return {}
    if not build.output_path_for(main_tex).is_file() or not text.startswith("Build of ") \
            or not text.split("\n", 1)[0].endswith(": SUCCESS"):
        return {}
    latex = latex_section(text)
    pages = build.LATEX_PAGES.findall(latex)
    engine = re.search(r"^This is (\w+)", latex, re.M)
    result = {
        "pages": int(pages[-1]) if pages else None, "warnings": len(build.LATEX_WARNING.findall(latex)),
        "engine": ENGINES.get(engine.group(1)) if engine else None, "finished": finished,
    }
    try:  # The build summary has the build time; the log does not.
        for entry in json.loads((build.OUT_DIR / "build-report.json").read_text("utf-8"))["documents"]:
            if entry.get("name") == name and entry.get("ok"):
                result.update(seconds=entry.get("seconds"), engine=entry.get("engine") or result["engine"])
    except (OSError, ValueError, KeyError, TypeError, AttributeError):
        pass
    return result


def remember_build(entry: dict) -> None:
    """Keep this build's summary in out/build-report.json beside the other documents', for saved_result()."""
    with build.REPORT_LOCK:
        try:
            old = json.loads((build.OUT_DIR / "build-report.json").read_text("utf-8"))["documents"]
            entries = [e for e in old if isinstance(e, dict) and e.get("name") != entry["name"]]
        except (OSError, ValueError, KeyError, TypeError):
            entries = []
        try:
            build.write_report([*entries, entry])
        except OSError:
            pass


def fresh_state(name: str, main_tex: Path) -> dict:
    return {
        "name": name, "status": "idle", "ok": None, "seconds": None, "pages": None, "warnings": 0,
        "engine": None, "error": None, "errors": [], "finished": None, "started": None,
        "version": pdf_version(main_tex), "focus": None, **saved_result(name, main_tex),
    }


def synctex_file(main_tex: Path) -> Path:
    return build.cache_dir_for(main_tex) / f"{main_tex.stem}.synctex.gz"


def log_excerpt(log_text: str, found: dict, lines: int = 8) -> str:
    """TeX's own report for an error: from its '!' line through the 'l.<n>' source context."""
    rows = log_text.splitlines()
    for index, row in enumerate(rows):
        if re.match(rf"l\.{found['line']}\b", row):
            start = next((i for i in range(index, max(index - 8, -1), -1) if rows[i].startswith("!")), index)
            return "\n".join(rows[start:index + 3])
    for index, row in enumerate(rows):  # No l.<n> context (e.g. -file-line-error only): start at the message.
        if found["message"] in row:
            return "\n".join(rows[index:index + lines])
    return ""


# One LaTeX warning or box report (it ends at the first blank line), or the start/end of a file in the log.
_WARNING_TOKEN = re.compile(
    r"(?P<warn>(?:(?:LaTeX(?: Font)?|Package [\w.-]+|Class [\w.-]+|Module [\w.-]+) Warning:"
    r"|(?:Overfull|Underfull) \\[hv]box)[^\n]*(?:\n(?!\n)[^\n]*)*)"
    r"|\((?P<open>[^\s()]*)|\)"
)
MAX_WARNINGS = 400


def doc_relative(root: Path, name: str | None) -> str | None:
    """A file name from the log as a path inside the document, or None (a system package, a missing file)."""
    if not name:
        return None
    candidate = Path(name)
    try:
        rel = candidate.resolve().relative_to(root.resolve()).as_posix() if candidate.is_absolute() else \
            posixpath.normpath(name)
        return rel if resolve_in_doc(root, rel).is_file() else None
    except (ValueError, OSError, ApiError):
        return None


def parse_warnings(log_text: str, root: Path | None = None) -> list[dict]:
    """
    LaTeX warnings and box reports from an out/<name>.log, in log order, plus BibTeX's "Warning--" lines:
    {kind, level, message, file, line, source, hint, excerpt}. file is a path inside `root` when TeX named one
    (the innermost .tex being read), else None. kind is undefined | overfull | underfull | rerun | package |
    latex | bibtex.
    """
    found: list[dict] = []
    seen: set[tuple] = set()
    stack: list[str | None] = []
    closed: str | None = None  # the .tex file TeX finished reading last
    for match in _WARNING_TOKEN.finditer(latex_section(log_text)):
        if match.group("open") is not None:
            name = match.group("open")
            stack.append(name if name.endswith(".tex") else None)
            continue
        if match.group("warn") is None:
            if stack:
                closed = stack.pop() or closed
            continue
        rows = match.group("warn").split("\n")
        first = rows[0]
        box = first.startswith(("Overfull", "Underfull"))
        if box:
            message, source = first, "TeX"
            kind = first.split()[0].lower()
        else:
            head, _, rest = first.partition(" Warning:")
            source = head.replace("Package ", "").replace("Class ", "").replace("Module ", "")
            joined = " ".join([rest.strip(), *(re.sub(r"^\([\w.-]+\)\s*", "", row).strip() for row in rows[1:])])
            message = re.sub(r"\s+", " ", joined).strip()
            if re.search(r"(?:Reference|Citation) .*? undefined|There were undefined", message):
                kind = "undefined"
            elif re.search(r"Rerun to get|Label\(s\) may have changed", message):
                kind = "rerun"
            else:
                kind = "package" if head.startswith(("Package", "Class", "Module")) else "latex"
        at = re.search(r"\blines? (\d+)" if box else r"on input line (\d+)", first if box else message)
        file = stack[-1] if stack else None  # A warning raised while a package file is open names that file's lines.
        span = re.search(r"\blines (\d+)--(\d+)", first) if box else None
        if span and int(span.group(2)) < int(span.group(1)):
            file = closed  # The paragraph began in the file that just ended and was closed by the next one.
        file = doc_relative(root, file[2:] if file and file.startswith("./") else file) if root else None
        line = int(at.group(1)) if at and file else None
        file = file if line else None  # No line: nothing to jump to.
        key = (kind, message, file, line)
        if key in seen:
            continue
        seen.add(key)
        excerpt = "\n".join(rows[:5])
        found.append({
            "kind": kind, "level": "info" if kind in ("underfull", "rerun") else "warning", "message": message,
            "file": file, "line": line, "source": source, "hint": hints.explain(message, excerpt), "excerpt": excerpt,
        })
        if len(found) >= MAX_WARNINGS:
            return found
    bib = log_text.find("\n===== BibTeX log")
    for row in log_text[bib:].splitlines() if bib >= 0 else []:
        if row.startswith("Warning--") and len(found) < MAX_WARNINGS:
            message = row[len("Warning--"):].strip()
            found.append({
                "kind": "bibtex", "level": "warning", "message": message, "file": None, "line": None,
                "source": "BibTeX", "hint": hints.explain(message), "excerpt": row,
            })
    return found


def doc_warnings(name: str) -> list[dict]:
    main_tex = DOCS[name]
    try:
        text = build.log_path_for(main_tex).read_text(encoding="utf-8", errors="replace")
    except (OSError, ValueError):
        return []
    return parse_warnings(text, main_tex.parent)


def run_build(main_tex: Path, latexmk: str, force: bool, record: bool = False) -> None:
    name = build.doc_name(main_tex)
    publish(name, status="building", started=time.time())
    with BUILD_LOCK:
        started = time.time()
        over = quota_after_build(started)  # Already over quota: do not build at all.
        if over is None:
            entry, _ = build.build_safely(main_tex, latexmk, False, force, record=record, validate=False)
            over = quota_after_build(started)
    if over is not None:
        publish(name, status="failed", ok=False, error=over, error_hint=None, errors=[], finished=time.time(),
                version=pdf_version(main_tex))
        build.error(f"{name}: {over}")
        return
    remember_build(entry)
    try:
        log_text = build.log_path_for(main_tex).read_text(encoding="utf-8", errors="replace")
    except OSError:
        log_text = ""
    errors = []
    for found in entry["errors"]:
        excerpt = log_excerpt(log_text, found)
        errors.append({
            **found, "link": editor_link(main_tex.parent / found["file"], found["line"]),
            "hint": found.get("hint") or hints.explain(found["message"], excerpt), "excerpt": excerpt,
        })
    publish(
        name, status="ok" if entry["ok"] else "failed", ok=entry["ok"], seconds=entry["seconds"],
        pages=entry["pages"], warnings=entry["warnings"], engine=entry["engine"], error=entry["error"],
        error_hint=hints.explain(entry["error"] or "") if entry["error"] else None,
        errors=errors, finished=time.time(), version=pdf_version(main_tex),
    )
    build.info(f"{'ok    ' if entry['ok'] else 'FAILED'} {name} ({entry['seconds']}s)")


# --- Chapter preview: build.build_focus for the chapter that holds a file, shown as out/<name>.focus.pdf ---------

def focus_map(main_tex: Path) -> Path:
    return build.cache_dir_for(main_tex) / f"{main_tex.stem}.focusmap"


def focus_target(main_tex: Path, rel: str) -> str | None:
    """The top-level file (read by main.tex) that holds rel, from the last full build's recorded input tree."""
    if not focus_map(main_tex).exists():
        return None
    return build.accel.top_unit(build.accel.read_focusmap(focus_map(main_tex)), rel)


def focus_error(main_tex: Path) -> str:
    """Why the last chapter preview failed: its first error line, else a pointer to the log."""
    try:
        rows = build.focus_paths(main_tex)[1].read_text(encoding="utf-8", errors="replace").split("\n")
        stop = rows.index("===== LaTeX output =====") if "===== LaTeX output =====" in rows else len(rows)
        first = next((r for r in rows[1:stop] if r.strip()), "")
        return first[:300] or "The chapter preview failed."
    except (OSError, ValueError):
        return "The chapter preview failed."


def run_focus(name: str, rel: str, began: float | None = None) -> None:
    main_tex = DOCS[name]
    began = began or time.time()
    result: dict = {"status": "failed", "path": rel, "target": None, "started": began, "error": None}
    try:
        with BUILD_LOCK:
            if not focus_map(main_tex).exists():
                run_build(main_tex, SETTINGS["latexmk"], True, record=True)  # First time: the input tree comes first.
            target = focus_target(main_tex, rel)
            result["target"] = target
            if target is None:
                result["error"] = f"{rel} is not read by main.tex with \\input or \\include, so it has no chapter."
            else:
                started = time.time()
                built = build.build_focus(main_tex, SETTINGS["latexmk"], target)
                over = quota_after_build(started)
                if over:
                    result["error"] = over
                elif built:
                    result.update(status="ok", version=str(build.focus_paths(main_tex)[0].stat().st_mtime_ns))
                else:
                    result["error"] = focus_error(main_tex)
    except Exception as exc:  # noqa: BLE001 - a failed preview must not take the server down.
        result["error"] = f"{type(exc).__name__}: {exc}"
    finally:
        FOCUS_BUSY.discard(name)
        publish(name, focus={**result, "finished": time.time()})


def start_focus(name: str, rel) -> float:
    """
    Start a chapter preview in the background; returns its start time, which the "focus" state carries so a
    browser can tell this preview from an older one. Raises ApiError for a bad file, or one already running.
    """
    main_tex = DOCS[name]
    if not isinstance(rel, str) or not rel.endswith(".tex"):
        raise ApiError("Open a .tex file of a chapter to preview it.", 400)
    if not resolve_in_doc(main_tex.parent, rel).is_file():
        raise ApiError("No such file.", 404)
    if rel == "main.tex" or (focus_map(main_tex).exists() and focus_target(main_tex, rel) is None):
        raise ApiError("This file is not a chapter: main.tex does not \\input or \\include it.", 409)
    with LOCK:
        if name in FOCUS_BUSY:
            raise ApiError("A chapter preview is already building.", 409)
        FOCUS_BUSY.add(name)
    began = time.time()
    if name in STATE:
        publish(name, focus={"status": "building", "path": rel, "target": None, "started": began})
    threading.Thread(target=run_focus, args=(name, rel, began), daemon=True).start()
    return began


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
                    if not synctex_file(main_tex).exists() and pdf_version(main_tex):
                        FORCE.add(name)  # Built before SyncTeX was on: rebuild once so both searches work.
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


def fs_watcher(interval: float = 1.0) -> None:
    """Tell browsers when a file in a served document changes on disk (editors, git, our own saves)."""
    known: dict[str, dict[str, str]] = {}
    while not STOP.is_set():
        for name, main_tex in list(DOCS.items()):
            try:
                now = {f["path"]: f["version"] for f in list_files(main_tex.parent)}
                now.update({d + "/": "dir" for d in empty_dirs(main_tex.parent)})  # A new empty folder is news too.
            except OSError:
                continue
            before = known.get(name)
            known[name] = now
            if before is None or before == now:
                continue
            changed = [p for p in now if before.get(p) != now[p]]
            removed = [p for p in before if p not in now]
            broadcast("fs", {"doc": name, "changed": changed, "removed": removed})
        STOP.wait(interval)


# ---------------------------------------------------------------------------
# Files: every path is checked against the document directory
# ---------------------------------------------------------------------------

class ApiError(Exception):
    def __init__(self, message: str, status: int = 400, **extra) -> None:
        super().__init__(message)
        self.status, self.extra = status, extra


TEXT_EXT = {
    ".tex", ".bib", ".cls", ".sty", ".bst", ".txt", ".md", ".def", ".cfg", ".ltx", ".clo", ".bbx", ".cbx",
    ".lbx", ".dtx", ".ins", ".latexmkrc", ".toml", ".csv", ".tikz", ".pgf",
}
IMAGE_TYPES = {
    ".png": "image/png", ".jpg": "image/jpeg", ".jpeg": "image/jpeg", ".gif": "image/gif",
    ".svg": "image/svg+xml", ".webp": "image/webp",
}
MAX_TEXT = 4 * 1024 * 1024
HIDDEN_DIRS = {".git", "__pycache__", "node_modules"}


def file_kind(name: str) -> str:
    suffix = Path(name).suffix.lower()
    if suffix in TEXT_EXT or name.lower() == ".latexmkrc":
        return "text"
    return "image" if suffix in IMAGE_TYPES else "other"


def resolve_in_doc(root: Path, rel: str) -> Path:
    """The real path of rel inside root. Rejects traversal, absolute paths and symlinks that leave root."""
    if not isinstance(rel, str) or not rel or "\0" in rel or "\\" in rel or rel.startswith("/"):
        raise ApiError("Bad path.", 400)
    if re.match(r"[A-Za-z]:", rel):
        raise ApiError("Bad path.", 400)
    if any(part in ("", ".", "..") for part in rel.split("/")):
        raise ApiError("Bad path.", 400)
    base = root.resolve()
    target = (base / rel).resolve()
    if base not in target.parents:
        raise ApiError("Path is outside the document directory.", 403)
    return target


def version_of(stat: os.stat_result) -> str:
    return f"{stat.st_mtime_ns:x}-{stat.st_size:x}"


def list_files(root: Path, limit: int = 5000) -> list[dict]:
    base = root.resolve()
    found: list[dict] = []
    for current, dirs, names in os.walk(base):
        dirs[:] = sorted(d for d in dirs if d not in HIDDEN_DIRS and not d.startswith("."))
        for name in sorted(names):
            if name.startswith(".") and name != ".latexmkrc":
                continue
            full = Path(current, name)
            try:
                real = full.resolve()
                if base not in real.parents:
                    continue  # Symlink out of the document.
                stat = real.stat()
            except OSError:
                continue
            found.append({
                "path": full.relative_to(base).as_posix(), "size": stat.st_size,
                "kind": file_kind(name), "version": version_of(stat),
            })
            if len(found) >= limit:
                return found
    return found


def empty_dirs(root: Path, limit: int = 500) -> list[str]:
    """Folders with nothing visible in them (list_files shows files only), so a new folder appears in the tree."""
    base = root.resolve()
    found: list[str] = []
    for current, dirs, names in os.walk(base):
        dirs[:] = sorted(d for d in dirs if d not in HIDDEN_DIRS and not d.startswith(".")
                         and not os.path.islink(os.path.join(current, d)))
        if not dirs and not [n for n in names if not n.startswith(".") or n == ".latexmkrc"] and Path(current) != base:
            found.append(Path(current).relative_to(base).as_posix())
            if len(found) >= limit:
                break
    return found


def read_text_file(root: Path, rel: str) -> dict:
    path = resolve_in_doc(root, rel)
    if file_kind(rel) != "text":
        raise ApiError("Not a text file.", 415)
    try:
        stat = path.stat()
        if stat.st_size > MAX_TEXT:
            raise ApiError("File is too large to edit.", 413)
        raw = path.read_bytes()
    except OSError:
        raise ApiError("No such file.", 404)
    if b"\0" in raw:
        raise ApiError("Not a text file.", 415)
    try:
        text = raw.decode("utf-8")
    except UnicodeDecodeError:
        raise ApiError("File is not valid UTF-8.", 415)
    eol = "\r\n" if "\r\n" in text else "\n"
    return {"path": rel, "text": text.replace("\r\n", "\n"), "eol": eol, "version": version_of(stat)}


def atomic_write(path: Path, data: bytes) -> None:
    """Temp file in the same directory, fsync, rename over the target."""
    handle, tmp = tempfile.mkstemp(dir=path.parent, prefix=f".{path.name}.", suffix=".tmp")
    try:
        with os.fdopen(handle, "wb") as out:
            out.write(data)
            out.flush()
            os.fsync(out.fileno())
        if path.exists():
            shutil.copymode(path, tmp)
        os.replace(tmp, path)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


WRITE_LOCK = threading.Lock()
# --gateway: bytes the whole project area (the document plus .out, .cache and .home beside it) may use; 0 = none.
# host.py passes it in LP_QUOTA_BYTES. A worker serves one project, so WRITE_LOCK is the per-project lock.
QUOTA: dict = {"bytes": 0, "area": None}


def folder_bytes(root: Path) -> int:
    """Apparent size of every file below root, symlinks not followed. Same rule as host.py's."""
    total = 0
    for current, dirs, names in os.walk(root):
        dirs[:] = [d for d in dirs if not os.path.islink(os.path.join(current, d))]
        for name in names:
            try:
                total += os.lstat(os.path.join(current, name)).st_size
            except OSError:
                pass
    return total


def quota_check(adding: int) -> None:
    """Refuse (507) a write of `adding` more bytes that would take the project over its quota; shrinking is fine.
    Measured fresh every time, under WRITE_LOCK, so parallel writes cannot all pass on one old number."""
    if QUOTA["bytes"] and adding >= 0 and folder_bytes(QUOTA["area"]) + adding > QUOTA["bytes"]:
        raise ApiError(f"This project is over its {QUOTA['bytes'] // 1048576} MB quota (files plus build output). "
                       "Delete files to make room.", 507)


def quota_after_build(started: float) -> str | None:
    """
    After a LaTeX run: if the project is over quota, delete what the run wrote (files in .out, .cache and .home
    changed since `started`) and return the error to report; None when within quota.
    """
    area = QUOTA["area"]
    if not QUOTA["bytes"] or folder_bytes(area) <= QUOTA["bytes"]:
        return None
    for sub in (".out", ".cache", ".home"):
        for current, dirs, names in os.walk(area / sub):
            dirs[:] = [d for d in dirs if not os.path.islink(os.path.join(current, d))]
            for name in names:
                path = os.path.join(current, name)
                try:
                    if os.lstat(path).st_mtime >= started:
                        os.unlink(path)
                except OSError:
                    pass
    return (f"The build went over the project's {QUOTA['bytes'] // 1048576} MB quota (files plus build output), "
            "so its output was deleted. Delete files or make the document smaller.")


def write_text_file(root: Path, rel: str, text: str, base: str | None, eol: str = "\n") -> dict:
    """Save text. If base (the version the editor loaded) is given and the file moved on, refuse with 409."""
    path = resolve_in_doc(root, rel)
    if file_kind(rel) != "text":
        raise ApiError("Not an editable text file.", 415)
    if not isinstance(text, str) or len(text) > MAX_TEXT or "\0" in text:
        raise ApiError("Bad file content.", 400)
    if not path.parent.is_dir():
        raise ApiError("Directory does not exist.", 404)
    data = (text.replace("\n", "\r\n") if eol == "\r\n" else text).encode("utf-8")
    with WRITE_LOCK:  # ponytail: one lock for all documents; the check-then-replace race is the only thing it closes.
        if base is not None:
            try:
                current = version_of(path.stat())
            except OSError:
                raise ApiError("The file was deleted or renamed on disk.", 409, deleted=True)
            if current != base:
                raise ApiError("The file changed on disk.", 409, current=current)
        try:
            old = path.stat().st_size
        except OSError:
            old = 0
        quota_check(len(data) - old)
        atomic_write(path, data)
        return {"path": rel, "version": version_of(path.stat())}


# --- File tree operations: new file, new folder, rename, delete -------------------------------------------------

BAD_NAME = re.compile(r'[\x00-\x1f<>:"|?*\\]|[ .]$')
MAX_REL = 240
MAX_DEPTH_NEW = 8


def fs_path(root: Path, rel, what: str = "path") -> Path:
    """
    A path the file tree may create, rename or delete: inside the document (resolve_in_doc), no dotfiles or hidden
    folders, no characters Windows refuses. Returns the lexical path, so a symlink is renamed or deleted itself,
    never the file it points to.
    """
    if not isinstance(rel, str) or not rel or len(rel) > MAX_REL:
        raise ApiError(f"Bad {what}.", 400)
    parts = rel.split("/")
    if len(parts) > MAX_DEPTH_NEW or any(p.startswith(".") or p in HIDDEN_DIRS or BAD_NAME.search(p) for p in parts):
        raise ApiError(f"Bad {what}: names cannot start with a dot or contain special characters.", 400)
    resolve_in_doc(root, rel)  # Traversal, absolute paths and symlinks that leave the document.
    return root.resolve() / rel


def close_rooms(doc: str, rel: str, below: bool = True) -> None:
    """
    Drop the co-editing rooms of a path (and everything below it): their update log would replay the old text into
    whatever is created there next. Members are told with y-closed and stop writing.
    """
    with COLLAB_LOCK:
        for rid in [r for r, room in ROOMS.items() if room["doc"] == doc and (
                room["path"] == rel or (below and room["path"].startswith(rel + "/")))]:
            room = ROOMS.pop(rid)
            for cid in room["members"]:
                if cid in CLIENTS:
                    CLIENTS[cid]["rooms"].discard(rid)
            BUS.publish("y-closed", {"room": rid})


def _same_file(a: Path, b: Path) -> bool:
    try:
        return os.path.samefile(a, b)
    except OSError:
        return False


def fs_operation(doc: str, op, rel, to=None) -> dict:
    """Run one file tree operation in document `doc` and tell the browsers; raises ApiError."""
    root = DOCS[doc].parent
    changed: list[str] = []
    removed: list[str] = []
    with WRITE_LOCK:
        source = fs_path(root, rel)
        if op in ("newfile", "mkdir"):
            if source.exists() or source.is_symlink():
                raise ApiError("Something with that name already exists.", 409)
            if op == "newfile" and file_kind(rel) != "text":
                raise ApiError("Only text files (.tex, .bib, ...) can be created here; drop images on the editor.", 415)
            if any(parent.is_file() for parent in source.parents if root.resolve() in parent.parents):
                raise ApiError("A file is in the way of that folder.", 409)
            quota_check(0)  # Empty, but not once the project is over quota.
            source.parent.mkdir(parents=True, exist_ok=True)
            if op == "mkdir":
                source.mkdir()
            else:
                os.close(os.open(source, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o666))
                changed.append(rel)
            close_rooms(doc, rel)
        elif op in ("rename", "delete"):
            if not (source.exists() or source.is_symlink()):
                raise ApiError("No such file or folder.", 404)
            others = [d.parent.resolve() for n, d in DOCS.items() if n != doc]
            if _same_file(source, root / "main.tex") or _same_file(source, root):
                raise ApiError("main.tex is the document itself and cannot be renamed or deleted.", 409)
            if any(o == source.resolve() or source.resolve() in o.parents for o in others):
                raise ApiError("That is, or contains, another document.", 409)
            folder = source.is_dir() and not source.is_symlink()
            moved = [f["path"] for f in list_files(source)] if folder else [rel]
            moved = [rel + "/" + m if folder else m for m in moved]
            gone = False
            try:
                if op == "delete":
                    gone = True
                    if folder:
                        shutil.rmtree(source)
                    else:
                        source.unlink()
                else:
                    target = fs_path(root, to, "new name")
                    if target == source or target.is_relative_to(source):
                        raise ApiError("A folder cannot be moved into itself.", 400)
                    if target.exists() or target.is_symlink():
                        raise ApiError("Something with that name already exists.", 409)
                    if not target.parent.is_dir():
                        raise ApiError("The folder you are moving into does not exist.", 404)
                    tp = target.parent.resolve()
                    if any(o == tp or o in tp.parents for o in others):
                        raise ApiError("That folder belongs to another document.", 409)
                    os.rename(source, target)
                    gone = True
                    changed += [to + m[len(rel):] for m in moved]
                    close_rooms(doc, to)
            except OSError as exc:
                raise ApiError(f"Could not {op} {rel}: {exc.strerror or exc}", 500)
            finally:
                if gone:  # Also after a half-finished rmtree: report what may be gone.
                    removed += [m for m in moved if not (root / m).exists()] if op == "delete" else moved
                    close_rooms(doc, rel)
        else:
            raise ApiError("Unknown operation.", 400)
    broadcast("fs", {"doc": doc, "changed": changed, "removed": removed})
    return {"ok": True, "path": to if op == "rename" else rel}


# --- Figure upload: drag and drop or paste into the editor ----------------------------------------------------

# png, jpg and pdf only: svg can carry script and TeX cannot read it anyway. The bytes must match the type.
UPLOAD_TYPES = {".png": b"\x89PNG\r\n\x1a\n", ".jpg": b"\xff\xd8\xff", ".jpeg": b"\xff\xd8\xff", ".pdf": b"%PDF-"}
MAX_UPLOAD = 8 * 1024 * 1024


def save_upload(doc: str, name, data: bytes) -> dict:
    """Store an image in the document (in Figures/ when it exists) under a safe, unused name."""
    root = DOCS[doc].parent
    base = re.sub(r"[^A-Za-z0-9._-]+", "_", posixpath.basename(str(name or "").replace("\\", "/")))
    stem, ext = posixpath.splitext(base)
    stem, ext = stem.strip("._-") or "image", ext.lower()
    if ext not in UPLOAD_TYPES:
        raise ApiError("Only png, jpg and pdf images can be added.", 415)
    if not data or len(data) > MAX_UPLOAD:
        raise ApiError(f"Images can be up to {MAX_UPLOAD // (1024 * 1024)} MB.", 413)
    if not data.startswith(UPLOAD_TYPES[ext]):
        raise ApiError("That file is not really a png, jpg or pdf.", 415)
    folder = "Figures" if (root / "Figures").is_dir() and not (root / "Figures").is_symlink() else ""
    with WRITE_LOCK:
        quota_check(len(data))
        for n in range(100):
            rel = posixpath.join(folder, f"{stem[:80]}{'' if n == 0 else '-' + str(n)}{ext}")
            target = fs_path(root, rel)
            try:
                handle = os.open(target, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o666)
            except FileExistsError:
                continue
            with os.fdopen(handle, "wb") as out:
                out.write(data)
            break
        else:
            raise ApiError("Too many files with that name.", 409)
    close_rooms(doc, rel, below=False)
    broadcast("fs", {"doc": doc, "changed": [rel], "removed": []})
    return {"ok": True, "path": rel}


def find_image(root: Path, name: str, origin: str = "") -> Path | None:
    """Resolve an \\includegraphics name like LaTeX would: as given, with extensions, in \\graphicspath dirs."""
    paths = [""]
    main = root / "main.tex"
    try:
        for group in re.finditer(r"\\graphicspath\s*\{((?:\s*\{[^}]*\}\s*)+)\}", main.read_text("utf-8", "replace")):
            paths += [p for p in re.findall(r"\{([^}]*)\}", group.group(1))]
    except OSError:
        pass
    origin_dir = posixpath.dirname(origin)
    for prefix in dict.fromkeys(paths + [origin_dir] if origin_dir else paths):
        for ext in ("", *IMAGE_TYPES):
            rel = posixpath.normpath(posixpath.join(prefix, name + ext))
            try:
                path = resolve_in_doc(root, rel)
            except ApiError:
                continue
            if path.is_file() and path.suffix.lower() in IMAGE_TYPES:
                return path
    return None


# ---------------------------------------------------------------------------
# Outline, word counts, references
# ---------------------------------------------------------------------------

LEVELS = {"part": 0, "chapter": 1, "section": 2, "subsection": 3, "subsubsection": 4}
# Macros whose braces never hold a path to a .tex file even if a same-named file exists.
NOT_PATHS = {
    "label", "ref", "pageref", "eqref", "autoref", "cref", "Cref", "nameref", "vref", "hyperref", "hypertarget",
    "cite", "citep", "citet", "citealp", "citeauthor", "citeyear", "nocite", "caption", "textbf", "emph",
    "textit", "usepackage", "documentclass", "bibliography", "bibliographystyle", "includegraphics",
    "begin", "end", "newcommand", "renewcommand", "def", "newenvironment", "graphicspath", "title", "author",
    "date", "footnote", "texttt", "url", "href", "newlabel", "setcounter", "input@path",
} | set(LEVELS)
COMMAND = re.compile(r"\\([A-Za-z]+)\*?")
MAX_DEPTH = 12


def strip_comments(text: str) -> str:
    """Drop % comments (keeping line structure) except escaped \\%."""
    return re.sub(r"(?<!\\)((?:\\\\)*)%[^\n]*", r"\1", text)


def brace_arg(text: str, start: int) -> tuple[str, int] | None:
    """text[start] == '{' -> (content, index after the matching '}'), or None if unbalanced."""
    depth = 0
    for i in range(start, len(text)):
        c = text[i]
        if c == "\\":
            continue
        if c == "{" and text[i - 1:i] != "\\":
            depth += 1
        elif c == "}" and text[i - 1:i] != "\\":
            depth -= 1
            if depth == 0:
                return text[start + 1:i], i + 1
    return None


def clean_title(title: str) -> str:
    title = re.sub(r"\\(?:label|footnote)\{[^{}]*\}", "", title)
    title = re.sub(r"\\[A-Za-z]+\*?\s*", "", title)
    return re.sub(r"[{}]", "", title).strip() or "(untitled)"


def tex_candidate(root: Path, current_dir: str, arg: str) -> str | None:
    """arg as a path to an existing .tex file inside root (relative to root or to the including file), else None."""
    arg = arg.strip()
    if not arg or len(arg) > 200 or any(c in arg for c in "\\{}$%#&^\n\t,"):
        return None
    for prefix in dict.fromkeys(["", current_dir]):
        rel = posixpath.normpath(posixpath.join(prefix, arg if arg.endswith(".tex") else arg + ".tex"))
        try:
            path = resolve_in_doc(root, rel)
        except ApiError:
            continue
        if path.is_file():
            return rel
    return None


def parse_document(main_tex: Path) -> dict:
    """
    Walk main.tex and everything it includes, in order. Any macro whose braced argument names an
    existing .tex file counts as an include (\\input, \\include, \\fypinput{unit}{path}, ...).
    Returns headings (with flat-text offsets), the flattened text and the visited files.
    """
    root = main_tex.parent
    pieces: list[str] = []
    headings: list[dict] = []
    files: list[str] = []
    size = 0

    def emit(text: str) -> None:
        nonlocal size
        pieces.append(text)
        size += len(text)

    def walk(rel: str, stack: tuple[str, ...]) -> None:
        if rel in stack or len(stack) >= MAX_DEPTH:
            return
        if rel not in files:
            files.append(rel)
        try:
            text = strip_comments(resolve_in_doc(root, rel).read_text("utf-8", "replace"))
        except (OSError, ApiError):
            return
        line_starts = [0] + [m.end() for m in re.finditer("\n", text)]
        from bisect import bisect_right
        here = posixpath.dirname(rel)
        pos = 0
        skip_to = 0
        for m in COMMAND.finditer(text):
            if m.start() < skip_to:
                continue
            name = m.group(1)
            if name in LEVELS:
                i = m.end()
                while text[i:i + 1].isspace():
                    i += 1
                if text[i:i + 1] == "[":
                    close = text.find("]", i)
                    i = close + 1 if close > 0 else i
                    while text[i:i + 1].isspace():
                        i += 1
                arg = brace_arg(text, i) if text[i:i + 1] == "{" else None
                if arg:
                    headings.append({
                        "level": LEVELS[name], "kind": name, "title": clean_title(arg[0]), "file": rel,
                        "line": bisect_right(line_starts, m.start()), "offset": size + (m.start() - pos),
                        "starred": m.group(0).endswith("*"),
                    })
                continue
            if name in NOT_PATHS:
                continue
            # Collect up to four consecutive {args} (and a bare word for \input foo).
            i, args, end = m.end(), [], m.end()
            for _ in range(4):
                while text[i:i + 1] in (" ", "\t"):
                    i += 1
                if text[i:i + 1] == "[":
                    close = text.find("]", i)
                    if close < 0:
                        break
                    i = close + 1
                    continue
                if text[i:i + 1] != "{":
                    break
                arg = brace_arg(text, i)
                if not arg:
                    break
                args.append(arg[0])
                i = end = arg[1]
            if not args and name in ("input", "include"):
                word = re.match(r"\s+([^\s\\{}]+)", text[m.end():m.end() + 200])
                if word:
                    args, end = [word.group(1)], m.end() + word.end()
            target = next((t for t in (tex_candidate(root, here, a) for a in args) if t), None)
            if target:
                emit(text[pos:m.start()] + "\n")
                walk(target, (*stack, rel))
                emit("\n")
                pos = skip_to = end
        emit(text[pos:])

    walk("main.tex", ())
    return {"headings": headings, "flat": "".join(pieces), "files": files}


SUBCOUNT = re.compile(r"^\s*(\d+)\+(\d+)\+(\d+) \([^)]*\) (.*)$")


MATH_ENVS = r"\\begin\{(equation|align|gather|eqnarray|multline|displaymath)\*?\}.*?\\end\{\1\*?\}"
NO_WORD_CMDS = (r"\\(?:begin|end|label|ref|eqref|cite[a-z]*|usepackage|documentclass|includegraphics|input|include)"
                r"\*?(?:\[[^\]]*\])?\{[^}]*\}")


def rough_words(text: str) -> int:
    """Fallback counter: drops math, environments' names, commands and comments; counts word-like tokens."""
    text = re.sub(r"\$\$.*?\$\$|\$[^$]*\$|\\\[.*?\\\]|\\\(.*?\\\)", " ", text, flags=re.S)
    text = re.sub(MATH_ENVS, " ", text, flags=re.S)
    text = re.sub(NO_WORD_CMDS, " ", text)
    text = re.sub(r"\\[A-Za-z@]+\*?", " ", text)
    return len(re.findall(r"[^\W_]+(?:['’.\-][^\W_]+)*", text))


def texcount_sections(flat: str) -> list[int] | None:
    """Words per [_top_, heading, heading, ...] from `texcount -sub=subsubsection` (None if unavailable)."""
    if shutil.which("texcount") is None:
        return None
    try:
        result = subprocess.run(
            ["texcount", "-sub=subsubsection", "-q", "-"], input=flat, capture_output=True, text=True,
            errors="replace", timeout=30,
        )
    except (OSError, subprocess.TimeoutExpired):
        return None
    counts, labels, seen = [], [], False
    for line in result.stdout.splitlines():
        if line.startswith("Subcounts"):
            seen = True
            continue
        match = SUBCOUNT.match(line) if seen else None
        if match:
            counts.append(sum(int(match.group(i)) for i in (1, 2, 3)))
            labels.append(match.group(4))
    if counts and labels[0] != "_top_":
        counts.insert(0, 0)  # texcount omits _top_ when nothing precedes the first heading.
    return counts or None


def outline(main_tex: Path) -> dict:
    parsed = parse_document(main_tex)
    heads, flat = parsed["headings"], parsed["flat"]
    own = [rough_words(flat[:heads[0]["offset"]] if heads else flat)]
    for i, head in enumerate(heads):
        own.append(rough_words(flat[head["offset"]:heads[i + 1]["offset"] if i + 1 < len(heads) else len(flat)]))
    counted = texcount_sections(flat)
    source = "texcount" if counted and len(counted) == len(heads) + 1 else "builtin"
    if source == "texcount":
        own = counted
    items = []
    for i, head in enumerate(heads):
        items.append({k: head[k] for k in ("level", "kind", "title", "file", "line", "starred")} | {"own": own[i + 1]})
    for i, item in enumerate(items):  # Rollup: a heading's words include everything under it.
        total = item["own"]
        for later in items[i + 1:]:
            if later["level"] <= item["level"]:
                break
            total += later["own"]
        item["words"] = total
    return {
        "items": items, "top_words": own[0], "total_words": sum(own), "files": parsed["files"], "counter": source,
    }


BIB_ENTRY = re.compile(r"@(\w+)\s*\{\s*([^,\s]+)\s*,")
LABEL = re.compile(r"\\label\{([^}]+)\}")


def parse_bib(text: str) -> dict[str, dict]:
    entries: dict[str, dict] = {}
    matches = list(BIB_ENTRY.finditer(text))
    for index, match in enumerate(matches):
        body = text[match.end():matches[index + 1].start() if index + 1 < len(matches) else len(text)]
        fields = {"type": match.group(1).lower()}
        for field in re.finditer(r"(\w+)\s*=\s*", body):
            i = field.end()
            if body[i:i + 1] == "{":
                arg = brace_arg(body, i)
                value = arg[0] if arg else ""
            elif body[i:i + 1] == '"':
                close = body.find('"', i + 1)
                value = body[i + 1:close] if close > 0 else ""
            else:
                value = re.match(r"[^,\n}]*", body[i:]).group(0)
            fields.setdefault(field.group(1).lower(), re.sub(r"\s+", " ", re.sub(r"[{}]", "", value)).strip())
        entries[match.group(2)] = fields
    return entries


def references(root: Path) -> dict:
    labels: dict[str, dict] = {}
    bib: dict[str, dict] = {}
    for item in list_files(root):
        path = item["path"]
        if item["kind"] != "text" or not path.endswith((".tex", ".bib")):
            continue
        try:
            text = resolve_in_doc(root, path).read_text("utf-8", "replace")
        except (OSError, ApiError):
            continue
        if path.endswith(".bib"):
            for key, fields in parse_bib(text).items():
                bib.setdefault(key, {**fields, "file": path})
            continue
        lines = strip_comments(text).split("\n")
        for number, line in enumerate(lines, 1):
            for match in LABEL.finditer(line):
                near = next((lines[n].strip() for n in range(number - 1, max(number - 8, -1), -1)
                             if re.search(r"\\(?:caption|(?:sub)*section|chapter)\b", lines[n])), line.strip())
                labels.setdefault(match.group(1), {"file": path, "line": number, "context": near[:200]})
    return {"labels": labels, "bib": bib}


def lint(doc_name: str) -> list[dict]:
    import ci_report
    main_tex = DOCS[doc_name]
    findings = ci_report.lint_document(main_tex.parent, build.log_path_for(main_tex), ci_report.DEFAULT_OVERFULL_PT)
    return [finding.as_dict() for finding in findings]


# ---------------------------------------------------------------------------
# Grammar (LanguageTool, see grammar.py)
# ---------------------------------------------------------------------------

# Owner settings, kept in memory (the owner's browser sends them again on every load). "auto" follows build.toml,
# and without that a local server if one answers. Public mode sends text to languagetool.org.
GRAMMAR: dict = {"mode": "auto", "url": None, "share_public": False}
GRAMMAR_MAX_CHARS = 400_000
GRAMMAR_MAX_SHARED_CHARS = 100_000  # while sharing: a non-owner must not tie up LanguageTool
_PROBES: dict = {}


def cached_probe(base: str) -> bool:
    now = time.monotonic()
    when, found = _PROBES.get(base, (-60.0, False))
    if now - when > (30 if found else 10):
        found = grammar.probe(base)
        _PROBES[base] = (now, found)
    return found


def grammar_plan(name: str) -> tuple[str, str | None, dict]:
    """(mode, endpoint, build.toml settings) for a document. Raises ApiError when public mode is not allowed."""
    try:
        cfg = build.read_settings(DOCS[name])
        grammar.validate_settings(cfg)
    except (build.ConfigError, grammar.GrammarError, OSError):
        cfg = {}
    mode = GRAMMAR["mode"] if GRAMMAR["mode"] != "auto" else cfg.get("grammar")
    try:
        mode, url = grammar.resolve(mode, GRAMMAR["url"] or cfg.get("grammar_url"), probe_fn=cached_probe)
    except grammar.GrammarError as exc:
        raise ApiError(str(exc), 400)
    if mode == "public" and SHARE["on"] and not GRAMMAR["share_public"]:
        raise ApiError("Public grammar checking is off while sharing. The owner can allow it in Settings.", 403)
    return mode, url, cfg


def grammar_check(name: str, text) -> dict:
    """Findings for the text of one open file, positioned in UTF-16 units like the editor counts."""
    if not isinstance(text, str) or len(text) > (GRAMMAR_MAX_SHARED_CHARS if SHARE["on"] else GRAMMAR_MAX_CHARS):
        raise ApiError("Bad or oversized text.", 413)
    mode, url, cfg = grammar_plan(name)
    if mode == "off":
        return {"mode": "off", "findings": [], "notice": "Grammar check is off: start a LanguageTool server "
                f"({grammar.DEFAULT_LOCAL}) or pick a mode in Settings."}
    try:
        found = grammar.check_source(text, "", url=url, public=mode == "public", lang=cfg.get("lang", "en-US"),
                                     disabled=cfg.get("disabled_rules", []), max_wait=5.0)
    except grammar.GrammarError as exc:
        raise ApiError(str(exc), 429 if "limit" in str(exc) else 502)
    u16 = utf16_offsets(text)
    return {"mode": mode, "notice": grammar.PUBLIC_NOTICE if mode == "public" else None, "findings": [
        {"from": u16(f.offset), "to": u16(f.offset + f.length), "line": f.line, "col": f.col, "rule": f.rule,
         "message": f.message, "replacements": list(f.replacements)} for f in found]}


def grammar_settings(data: dict) -> dict:
    mode, url = data.get("mode", "auto"), data.get("url") or None
    if mode not in grammar.MODES:
        raise ApiError("Unknown grammar mode.", 400)
    if url is not None:
        try:
            grammar.endpoint(url if isinstance(url, str) else "")
        except grammar.GrammarError as exc:
            raise ApiError(str(exc), 400)
    GRAMMAR.update(mode=mode, url=url, share_public=bool(data.get("share_public")))
    return grammar_info()


BIB_MAX_CHARS = 1_000_000


def bib_lookup(data: dict) -> dict:
    """Crossref suggestions for one entry of the .bib text the editor holds, with the edit that applies them.

    Only a DOI or a title leaves the machine (see bibfix.py). The editor applies the edit to its open document,
    so co-editing rooms stay consistent and nothing is written here."""
    text, key = data.get("text"), data.get("key")
    if not (isinstance(text, str) and isinstance(key, str)):
        raise ApiError("Send the .bib text and the entry key.", 400)
    if len(text) > BIB_MAX_CHARS:
        raise ApiError("This .bib file is too large to look up (1 MB limit).", 413)
    try:
        return bibfix.suggest_for(text, key)
    except bibfix.BibLookupError as exc:
        return {"fields": {}, "error": str(exc)}


def utf16_offsets(text: str):
    """index -> UTF-16 offset in `text` (what CodeMirror counts), O(log n) per call after one pass."""
    astral = [i for i, char in enumerate(text) if ord(char) > 0xFFFF]
    return lambda index: index + bisect.bisect_left(astral, index)


BIB_MAX_FILES = 50
BIB_MAX_TOTAL = 5_000_000


def bib_overview(name: str, texts) -> dict:
    """The References panel: every .bib entry in the document folder, every \\cite with its place, file problems.

    `texts` maps .bib paths to the text the editor holds (unsaved edits included); offsets are UTF-16 into it."""
    import ci_report
    root = DOCS[name].parent
    texts = texts if isinstance(texts, dict) else {}
    sources: dict[str, str] = {}
    for rel, text in ci_report.reachable_sources(root).items():
        try:
            resolve_in_doc(root, rel)
        except ApiError:
            continue  # \\input{../x} or a symlink out: not something a shared link may read.
        sources[rel] = text
    unused = {finding.subject for finding in ci_report.unused_bib_files(root, sources)}
    files: list[dict] = []
    entries: list[dict] = []
    problems: list[str] = []
    total = 0
    for rel in (item["path"] for item in list_files(root) if item["path"].endswith(".bib")):
        if len(files) >= BIB_MAX_FILES:
            problems.append(f"Only the first {BIB_MAX_FILES} .bib files are listed.")
            break
        text = texts.get(rel)
        if not isinstance(text, str):
            try:
                text = read_text_file(root, rel)["text"]
            except ApiError as exc:
                problems.append(f"{rel}: {exc}")
                continue
        if len(text) > BIB_MAX_CHARS or total + len(text) > BIB_MAX_TOTAL:
            problems.append(f"{rel} is too large to list (1 MB per file, 5 MB in all).")
            continue
        total += len(text)
        u16, line, at = utf16_offsets(text), 1, 0
        parsed = bibfix.parse(text)
        files.append({"path": rel, "used": rel not in unused, "entries": len(parsed)})
        for entry in parsed:
            line, at = line + text.count("\n", at, entry.start), entry.start
            fields: dict[str, str] = {}
            for field in entry.fields:
                fields.setdefault(field.name, field.value)
            entries.append({"key": entry.key, "type": entry.kind, "fields": fields, "file": rel, "line": line,
                            "start": u16(entry.start), "end": u16(entry.end),
                            "missing": ci_report.missing_bib_fields(entry.kind, fields)})
    citations: dict[str, list[dict]] = {}
    for rel, text in sources.items():
        newlines = [i for i, char in enumerate(text) if char == "\n"]
        u16 = utf16_offsets(text)
        for match in ci_report.CITE.finditer(text):
            for part in re.finditer(r"[^,]+", match.group(1)):
                key = part.group(0).strip()
                if not key or ci_report.is_macro_key(key):
                    continue
                pos = match.start(1) + part.start() + len(part.group(0)) - len(part.group(0).lstrip())
                line = bisect.bisect_left(newlines, pos)
                start = newlines[line - 1] + 1 if line else 0
                citations.setdefault(key, []).append({"file": rel, "line": line + 1, "col": u16(pos) - u16(start) + 1})
    return {"files": files, "entries": entries, "citations": citations, "problems": problems,
            "nocite_all": any(ci_report.NOCITE_ALL.search(text) for text in sources.values()),
            "required": {kind: [need.split("|")[0] for need in needs]
                         for kind, needs in ci_report.BIB_REQUIRED.items()}}


def bib_edit(data: dict) -> dict:
    """{from, to, insert} (UTF-16) that adds, changes or deletes one entry of the .bib text the editor holds.

    Like the lookup, nothing is written here: the editor applies the splice, so co-editing rooms stay consistent."""
    text, op, key, entry = data.get("text"), data.get("op"), data.get("key"), data.get("entry") or {}
    if not (isinstance(text, str) and isinstance(entry, dict)):
        raise ApiError("Send the .bib text and the entry.", 400)
    if len(text) > BIB_MAX_CHARS:
        raise ApiError("This .bib file is too large to edit here (1 MB limit).", 413)
    kind, new_key, fields = entry.get("type"), entry.get("key"), entry.get("fields")
    try:
        if op in ("add", "edit"):
            bibfix.check_entry(kind, new_key, fields)
            if new_key != key and any(other.key == new_key for other in bibfix.parse(text)):
                raise ApiError(f"'{new_key}' is already in this file.", 409)
        if op == "add":
            start, insert = bibfix.append_entry(text, kind, new_key, fields)
            end = start
        elif op == "edit":
            start, end, insert = bibfix.replace_entry(text, key, kind, new_key, fields)
        elif op == "delete":
            (start, end), insert = bibfix.delete_entry(text, key), ""
        else:
            raise ApiError("Unknown operation.", 400)
    except KeyError:
        raise ApiError(f"No entry '{key}' in this file any more. Refresh and try again.", 409)
    except ValueError as exc:
        raise ApiError(str(exc), 400)
    u16 = utf16_offsets(text)
    return {"from": u16(start), "to": u16(end), "insert": insert}


def bib_import(data: dict) -> dict:
    """Entries to add: parsed from pasted BibTeX, or from Crossref for a DOI (only the DOI leaves the machine)."""
    bibtex, doi = data.get("bibtex"), data.get("doi")
    if isinstance(doi, str) and doi.strip():
        try:
            return {"entries": [bibfix.entry_for_doi(doi)]}
        except bibfix.BibLookupError as exc:
            return {"entries": [], "error": str(exc)}
    if not isinstance(bibtex, str) or len(bibtex) > BIB_MAX_CHARS:
        raise ApiError("Paste BibTeX (up to 1 MB) or a DOI.", 400)
    found = []
    for entry in bibfix.parse(bibtex):
        fields: dict[str, str] = {}
        for field in entry.fields:
            fields.setdefault(field.name, field.value)
        found.append({"type": entry.kind, "key": entry.key, "fields": fields})
    heads = sum(kind.lower() not in ("comment", "string", "preamble")
                for kind in re.findall(r"@[ \t]*(\w+)[ \t\r\n]*[({]", bibtex))
    skipped = max(heads - len(found), 0)
    error = None if found and not skipped else (
        f"{skipped} entr{'y' if skipped == 1 else 'ies'} could not be read (check braces, quotes and commas)."
        if found else "No complete BibTeX entry found (check braces, quotes and commas).")
    return {"entries": found, "error": error}


def grammar_info(role: str = "owner") -> dict:
    info = {"mode": GRAMMAR["mode"], "share_public": GRAMMAR["share_public"]}
    return {**info, "url": GRAMMAR["url"]} if role == "owner" else info


# ---------------------------------------------------------------------------
# SyncTeX
# ---------------------------------------------------------------------------

class SynctexError(Exception):
    pass


def synctex(main_tex: Path, kind: str, spec: str) -> dict[str, str]:
    """Run the synctex CLI against the cached PDF (its .synctex.gz sits beside it)."""
    # ponytail: the cache PDF can be newer than the out/ PDF after a failed build; line-level error is small.
    pdf = build.cache_dir_for(main_tex) / f"{main_tex.stem}.pdf"
    if not synctex_file(main_tex).exists():
        raise SynctexError("No SyncTeX data yet: use Rebuild from scratch.")
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
    try:
        rel = source.resolve().relative_to(main_tex.parent.resolve()).as_posix()
    except ValueError:
        rel = None
    return {"file": source.as_posix(), "rel": rel, "line": line, "link": editor_link(source, line)}


def forward(query: dict, only: str | None = None) -> tuple[str, dict]:
    """
    Returns (doc name, {page, x, y, w, h}) in PDF points from the page's top-left. With `only`, files of other
    documents answer exactly like missing ones, so the reply says nothing about what else is on this machine.
    """
    raw = Path(query.get("file", [""])[0])
    name = query.get("doc", [""])[0]
    here = DOCS[name].parent / raw if name in DOCS else raw
    candidates = [raw] if raw.is_absolute() else [here, build.ROOT_DIR / raw]
    source = next((c.resolve() for c in candidates if c.is_file()), None)
    owner = None
    if source is not None:
        owner = max((n for n, d in DOCS.items() if d.parent.resolve() in source.parents), key=len, default=None)
    if owner is None or (only is not None and owner != only):
        raise SynctexError(f"No such file in the document: {raw}")
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
# Sharing: token links, roles, tunnels
# ---------------------------------------------------------------------------
#
# While sharing is on EVERY request needs a token, loopback included. A tunnel client connects to us from
# 127.0.0.1, so the peer address says nothing about who is asking, and the Host header is whatever the tunnel
# forwards (some rewrite it to localhost). Requiring the token for everyone is the only rule that cannot be
# bypassed through the tunnel; the owner's browser gets a third, private token as a cookie when sharing starts.

SHARE: dict = {
    "on": False, "tokens": {}, "doc": None, "provider": None, "public": None, "hosts": set(), "status": "off",
    "error": None, "tunnel": None, "port": 0, "gen": 0,
}
SHARE_LOCK = threading.Lock()
ROLES = ("owner", "edit", "view")
# --gateway (scripts/host.py): one worker per project, reached only through the gateway, which authenticates the
# user and says who they are in X-Host-* headers signed with a per-worker secret. Every request is a shared
# non-owner (edit or view): cookies, share tokens and owner features do not exist here.
GATEWAY: dict = {"secret": b""}
GATEWAY_ROLES = ("edit", "view")
SHELL_TOKENS = ("shell-escape", "enable-write18", "shell-restricted")
COMMAND_KEYS = {
    "pdflatex", "xelatex", "lualatex", "latex", "bibtex", "biber", "makeindex", "makeglossaries", "dvips", "dvipdf",
    "ps2pdf", "pdf_previewer", "dvi_previewer", "ps_previewer", "print_pdf_command", "e", "r",
}  # -key=value forms that name a program to run
RC_NAMES = {".latexmkrc", "latexmkrc", "build.toml"}  # configuration that can run programs
# Builds while sharing: no shell escape, and TeX may only read and write below the document's directory
# (openin_any=p also refuses "..", absolute paths and dotfiles). Environment variables beat texmf.cnf.
SHARE_ENV = {"shell_escape": "f", "openin_any": "p", "openout_any": "p"}
SHARE_TIMEOUT = 300
_SAVED_ENV: dict[str, str | None] = {}
# latexmk options that take a program, a file or code, or move the output; refused in every spelling.
VALUE_KEYS = {
    "e", "r", "latexoption", "pretex", "usepretex", "cnf-line", "outdir", "output-directory", "auxdir",
    "aux-directory", "jobname",
}
# The owner chose to allow LuaLaTeX while sharing: Lua can still write files (io.open), so an edit
# link to a LuaLaTeX document can run code. The Share dialog (app.js) says so.


def is_rc(path: str) -> bool:
    return posixpath.basename(path).lower() in RC_NAMES


def share_env(on: bool) -> None:
    """Put the build restrictions into this process's environment (builds inherit it) or take them out again."""
    if on and not _SAVED_ENV:
        for key, value in SHARE_ENV.items():
            _SAVED_ENV[key] = os.environ.get(key)
            os.environ[key] = value
    elif not on and _SAVED_ENV:
        for key, old in _SAVED_ENV.items():
            if old is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = old
        _SAVED_ENV.clear()
REBUILD_LIMIT = (6, 60.0)  # builds per window (seconds) an editor may trigger
RATE: dict[str, collections.deque] = {}


def new_token() -> str:
    return secrets.token_urlsafe(32)


def role_for_token(token: str | None) -> str | None:
    """owner when sharing is off; otherwise the role whose token matches. Compares every token, in constant time."""
    if not SHARE["on"]:
        return "owner"
    found = None
    probe = (token or "").encode("utf-8", "replace")
    for role, good in list(SHARE["tokens"].items()):
        if hmac.compare_digest(probe, good.encode()) and token:
            found = role
    return found


def gateway_enable(secret: str, doc: str, timeout: int | None = None) -> None:
    """Serve one document to the gateway only: sharing restrictions on for good, no tokens, no Host check."""
    global SHARE_TIMEOUT
    GATEWAY["secret"] = secret.encode()
    SETTINGS["check_host"] = False  # Loopback only, and every request must carry the secret instead.
    SHARE.update(on=True, doc=doc, provider="gateway", status="ready", tokens={}, public=None, hosts=set())
    share_env(True)
    if timeout:
        SHARE_TIMEOUT = timeout


def gateway_identity(headers) -> tuple[str | None, str | None]:
    """(role, "id;name") from the gateway's headers, or (None, None) unless X-Host-Secret matches."""
    probe = (headers.get("X-Host-Secret") or "").encode("utf-8", "replace")
    if not GATEWAY["secret"] or not hmac.compare_digest(probe, GATEWAY["secret"]):
        return None, None
    role = headers.get("X-Host-Role")
    user = unquote(headers.get("X-Host-User") or "")
    if role not in GATEWAY_ROLES or not re.fullmatch(r"[A-Za-z0-9_-]{1,64};[^\x00-\x1f]{0,80}", user):
        return None, None
    return role, user


def cookie_name() -> str:
    return f"lp_{SHARE['port']}"  # Cookies ignore ports; keep two local servers apart.


def rate_ok(key: str, limit: int, window: float) -> bool:
    now = time.monotonic()
    hits = RATE.setdefault(key, collections.deque())
    while hits and now - hits[0] > window:
        hits.popleft()
    if len(hits) >= limit:
        return False
    hits.append(now)
    return True


def unsafe_latexmk_arg(arg: str) -> bool:
    """Flags that enable shell escape, load rc files or code, move the output, or pick the programs latexmk runs."""
    low = arg.lower().strip()
    if any(token in low for token in (*SHELL_TOKENS, "shell_escape", "write18", "lua")):
        return True
    key = re.match(r"^--?([a-z0-9_-]+)(=|$)", low)
    if not key:
        return False
    return key.group(1) in VALUE_KEYS or (key.group(2) == "=" and key.group(1) in COMMAND_KEYS)


_READ_SETTINGS = build.read_settings


def guarded_read_settings(main_tex: Path) -> dict:
    """build.read_settings, but while sharing: no shell escape, rc files or latexmk_args that run code."""
    settings = _READ_SETTINGS(main_tex)
    if SHARE["on"]:
        bad = [arg for arg in settings["latexmk_args"] if unsafe_latexmk_arg(arg)]
        if bad:
            raise build.ConfigError(f"{main_tex.parent.name}/build.toml: {bad[0]!r} is not allowed while sharing")
        settings["shell_escape"] = False
        settings["timeout"] = min(settings["timeout"], SHARE_TIMEOUT)  # A looping document must not hold BUILD_LOCK.
        settings["latexmk_args"] = ["-norc", *settings["latexmk_args"]]  # A latexmkrc in the document would run code.
    return settings


build.read_settings = guarded_read_settings


def share_enable(doc: str | None, provider: str, port: int) -> int:
    """Turn sharing on; returns this share's generation, so a late tunnel thread can tell it was stopped."""
    with SHARE_LOCK:
        share_env(True)
        SHARE["gen"] = SHARE.get("gen", 0) + 1
        SHARE.update(
            on=True, doc=doc, provider=provider, status="starting", error=None, public=None, port=port,
            tokens={"owner": new_token(), "edit": new_token(), "view": new_token()},
        )
        return SHARE["gen"]


def share_disable(error: str | None = None) -> None:
    with SHARE_LOCK:
        tunnel, SHARE["tunnel"] = SHARE["tunnel"], None
        SHARE["gen"] = SHARE.get("gen", 0) + 1  # Whatever is still starting belongs to a share that no longer exists.
        SHARE.update(on=False, tokens={}, public=None, hosts=set(), status="off", error=error, provider=None)
        share_env(False)
    if tunnel:
        tunnel.stop()


def share_regenerate() -> None:
    """New view and edit tokens: every link handed out so far stops working, connections included."""
    with SHARE_LOCK:
        if SHARE["on"]:
            SHARE["tokens"].update(edit=new_token(), view=new_token())


def share_links() -> dict:
    base = SHARE["public"] or f"http://localhost:{SHARE['port']}"
    frag = "#" + SHARE["doc"].replace(" ", "%20") if SHARE["doc"] else ""
    tokens = SHARE["tokens"]
    return {role: f"{base.rstrip('/')}/?token={tokens[role]}{frag}" for role in ("view", "edit") if role in tokens}


def share_info() -> dict:
    return {
        "on": SHARE["on"], "status": SHARE["status"], "error": SHARE["error"], "provider": SHARE["provider"],
        "url": SHARE["public"], "doc": SHARE["doc"], "links": share_links() if SHARE["on"] else {},
        "providers": [{"name": n, "available": a, "hint": TUNNELS[n]["hint"]} for n, a in tunnel_status().items()],
    }


# --- Tunnels: one subprocess each, public URL read from its output --------------------------------------------

def _cmd(name: str) -> list[str] | None:
    path = shutil.which(name)
    return [path] if path else None


SSH_OPTS = ["-o", "StrictHostKeyChecking=no", "-o", "ServerAliveInterval=30", "-o", "ExitOnForwardFailure=yes"]


def _ssh_pinggy(port: int) -> list[str]:
    return ["-p", "443", f"-R0:localhost:{port}", *SSH_OPTS, "a.pinggy.io"]


def _ssh_lhr(port: int) -> list[str]:
    return ["-R", f"80:localhost:{port}", *SSH_OPTS, "nokey@localhost.run"]


# argv(port) builds the command after the binary; pattern finds the public https URL in the tool's output.
TUNNELS: dict[str, dict] = {
    "cloudflared": {
        "cmd": lambda: _cmd("cloudflared"), "args": lambda p: ["tunnel", "--no-autoupdate", "--url", f"http://127.0.0.1:{p}"],
        "pattern": re.compile(r"https://(?!api\.)[a-z0-9-]+\.trycloudflare\.com"),
        "hint": "Install cloudflared (no account needed): https://developers.cloudflare.com/cloudflare-one/networks/"
        "connectors/cloudflare-tunnel/downloads/",
    },
    "ngrok": {
        "cmd": lambda: _cmd("ngrok"), "args": lambda p: ["http", str(p), "--log", "stdout", "--log-format", "logfmt"],
        "pattern": re.compile(r"url=(https://[a-z0-9.-]+\.ngrok[a-z.-]*\.(?:app|io|dev))"), "api": "http://127.0.0.1:4040/api/tunnels",
        "hint": "Install ngrok and run `ngrok config add-authtoken <token>` (free account): https://ngrok.com/download",
    },
    "localtunnel": {
        "cmd": lambda: _cmd("lt") or (_cmd("npx") and [*_cmd("npx"), "--yes", "localtunnel"]),
        "args": lambda p: ["--port", str(p)],
        "pattern": re.compile(r"your url is:\s*(https://[a-z0-9.-]+)"),
        "hint": "Install Node.js (provides npx); localtunnel is fetched on first use.",
    },
    "pinggy": {
        "cmd": lambda: _cmd("ssh"), "args": _ssh_pinggy,
        "pattern": re.compile(r"https://(?:[a-z0-9-]+\.)+(?:pinggy-free\.link|pinggy\.link)"),
        "hint": "Install an OpenSSH client (ssh); pinggy needs no account.",
    },
    "localhost.run": {
        "cmd": lambda: _cmd("ssh"), "args": _ssh_lhr,
        "pattern": re.compile(r"https://(?!admin\.)[a-z0-9]+\.(?:lhr\.life|localhost\.run)\b"),
        "hint": "Install an OpenSSH client (ssh); localhost.run needs no account.",
    },
}
AUTO_ORDER = ("cloudflared", "ngrok", "localtunnel", "pinggy", "localhost.run")
PROVIDER_CHOICES = ("auto", "local", *TUNNELS)


def tunnel_status() -> dict[str, bool]:
    return {name: bool(spec["cmd"]()) for name, spec in TUNNELS.items()}


def pick_provider(name: str) -> str:
    """Resolve 'auto' to the best installed tool; raise ApiError with an install hint when nothing fits."""
    if name == "local":
        return name
    if name == "auto":
        found = next((n for n in AUTO_ORDER if TUNNELS[n]["cmd"]()), None)
        if found:
            return found
        hints_ = "; ".join(f"{n}: {TUNNELS[n]['hint']}" for n in AUTO_ORDER[:3])
        raise ApiError(f"No tunnel tool found (looked for cloudflared, ngrok, npx, ssh). {hints_}", 409)
    if name not in TUNNELS:
        raise ApiError(f"Unknown provider {name!r}.", 400)
    if not TUNNELS[name]["cmd"]():
        raise ApiError(f"{name} is not installed. {TUNNELS[name]['hint']}", 409)
    return name


def find_public_url(provider: str, text: str) -> str | None:
    """The tunnel's public https URL in a chunk of its output, or None."""
    match = TUNNELS[provider]["pattern"].search(text)
    if not match:
        return None
    return match.group(1) if match.groups() else match.group(0)


def ngrok_public_url(api_json: str) -> str | None:
    """https public_url from ngrok's local API (http://127.0.0.1:4040/api/tunnels)."""
    try:
        tunnels = json.loads(api_json).get("tunnels", [])
    except (ValueError, AttributeError):
        return None
    urls = [t.get("public_url", "") for t in tunnels if isinstance(t, dict)]
    return next((u for u in urls if u.startswith("https://")), None)


def kill_tree(proc: subprocess.Popen) -> None:
    if proc.poll() is not None:
        return
    try:
        if os.name == "nt":
            subprocess.run(["taskkill", "/T", "/F", "/PID", str(proc.pid)], capture_output=True, timeout=10)
        else:
            os.killpg(proc.pid, signal.SIGTERM)
        proc.wait(5)
    except (OSError, subprocess.TimeoutExpired):
        proc.kill()


class Tunnel:
    def __init__(self, provider: str, port: int) -> None:
        self.provider, self.port = provider, port
        self.proc: subprocess.Popen | None = None
        self.url: str | None = None
        self.lines: collections.deque = collections.deque(maxlen=40)
        self.lock = threading.Lock()
        self.stopped = False  # Set by stop(); start() checks it under the lock, so no process outlives a stop.

    def _read(self) -> None:
        for line in self.proc.stdout:
            self.lines.append(line.rstrip())
            if not self.url:
                self.url = find_public_url(self.provider, line)
        self.proc.stdout.close()

    def start(self, timeout: float = 60.0) -> str:
        spec = TUNNELS[self.provider]
        argv = [*spec["cmd"](), *spec["args"](self.port)]
        windows = os.name == "nt"
        group = {"creationflags": subprocess.CREATE_NEW_PROCESS_GROUP} if windows else {"start_new_session": True}
        with self.lock:
            if self.stopped:
                raise RuntimeError("Sharing was stopped.")
            self.proc = subprocess.Popen(
                argv, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True,
                errors="replace", **group,
            )
        threading.Thread(target=self._read, daemon=True).start()
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline and not STOP.is_set():
            if self.url:
                return self.url
            if "api" in spec:
                self.url = self.url or ngrok_api_url(spec["api"])
            if self.proc.poll() is not None and not self.url:
                time.sleep(0.2)  # let the reader drain
                if self.url:
                    return self.url
                raise RuntimeError(f"{self.provider} exited ({self.proc.returncode}): {self.tail()}")
            time.sleep(0.25)
        self.stop()
        raise RuntimeError(f"{self.provider} gave no public URL within {int(timeout)} s: {self.tail()}")

    def tail(self) -> str:
        return " | ".join(list(self.lines)[-4:])

    def stop(self) -> None:
        with self.lock:
            self.stopped = True
            proc = self.proc
        if proc:
            kill_tree(proc)


def ngrok_api_url(api: str) -> str | None:
    try:
        with http.client.HTTPConnection("127.0.0.1", 4040, timeout=1) as conn:
            conn.request("GET", api.split("4040", 1)[1])
            return ngrok_public_url(conn.getresponse().read().decode("utf-8", "replace"))
    except (OSError, http.client.HTTPException):
        return None


def share_start(provider: str, doc: str | None, port: int, wait: bool = False) -> None:
    """Turn sharing on and bring the tunnel up in the background (or here with wait=True). Raises ApiError up front."""
    chosen = pick_provider(provider)
    with SHARE_LOCK:
        if SHARE["on"]:
            raise ApiError("Already sharing. Stop sharing first.", 409)
    gen = share_enable(doc, chosen, port)

    def run() -> None:
        tunnel = None
        try:
            if chosen == "local":
                with SHARE_LOCK:
                    if SHARE["gen"] == gen:
                        SHARE["status"] = "ready"
                return
            tunnel = Tunnel(chosen, port)
            with SHARE_LOCK:
                if SHARE["gen"] != gen:
                    return  # Stopped before the tunnel was even created.
                SHARE["tunnel"] = tunnel
            url = tunnel.start()
            host = (urlsplit(url).hostname or "").lower()
            with SHARE_LOCK:
                if SHARE["gen"] == gen and SHARE["on"]:
                    SHARE.update(public=url, status="ready")
                    SHARE["hosts"].add(host)
                    return
            tunnel.stop()  # Sharing was stopped while the tunnel came up.
        except (RuntimeError, OSError) as exc:
            if tunnel:
                tunnel.stop()
            with SHARE_LOCK:
                current = SHARE["gen"] == gen
            if current:
                build.error(f"Share failed: {exc}")
                share_disable(str(exc))

    if wait:
        run()
    else:
        threading.Thread(target=run, daemon=True).start()


atexit.register(lambda: SHARE["tunnel"] and SHARE["tunnel"].stop())


# ---------------------------------------------------------------------------
# Co-editing: a relay for Yjs updates (the server never decodes them)
# ---------------------------------------------------------------------------
#
# One room per (document, file). The server keeps the room's update log, base64 as sent, so a late joiner replays
# it; Yjs updates are idempotent and commutative, so duplicates and reordering are harmless. Disk writes are done
# by ONE client per room, the leader (the earliest-joined member who may edit), through the ordinary PUT API, so
# they stay atomic and serialized by WRITE_LOCK. The leader also folds external disk edits into the Yjs document.
# ponytail: logs die with the room and are capped at MAX_ROOM_BYTES (further edits are refused); a leader-made
# snapshot would allow compaction.
# A room remembers the disk version and text it last matched (room["disk"], room["saved"]); a leader who takes over
# after the file changed behind the room's back is told to merge that change instead of overwriting it.

ROOMS: dict[str, dict] = {}
CLIENTS: dict[str, dict] = {}
COLLAB_LOCK = threading.RLock()
ROOM_GRACE = float(os.environ.get("LP_ROOM_GRACE", "60"))  # an emptied room (and its log) lingers this long
CLIENT_TIMEOUT = 60.0
MAX_UPDATE = 2 * 1024 * 1024
MAX_ROOM_BYTES = 8 * 1024 * 1024  # base64 update log per room; the server cannot compact what it never decodes
MAX_ROOMS_PER_CLIENT = 40
MAX_ROOMS = 300  # server-wide; the owner is exempt, so a shared link can never lock the owner out
MAX_ROOMS_PER_ROLE = 40  # rooms a shared role (edit/view) may have opened, however many client ids it uses
MAX_BYTES_PER_ROLE = 32 * 1024 * 1024  # update-log bytes a shared role may have put into rooms


def room_id(doc: str, path: str) -> str:
    return f"{doc}\n{path}"


def bind_client(client: str, role: str, user: str | None = None) -> bool:
    """
    Remember who a client id belongs to. False if the id was already taken by another role, or (behind the
    gateway) by another user: presence shows client ids, so a client id alone must not be enough to act as someone.
    """
    with COLLAB_LOCK:
        record = CLIENTS.setdefault(client, {
            "role": role, "rooms": set(), "name": None, "color": None, "path": None, "doc": None, "user": user,
        })
        record["seen"] = time.monotonic()
        return record["role"] == role and record.get("user") == user


def room_leader(room: dict) -> str | None:
    return next((cid for cid, m in room["members"].items() if m["role"] in ("owner", "edit")), None)


def room_drift(room: dict) -> dict:
    """
    Empty when the file is as the room last saw it. Otherwise it changed on disk behind the room's back (the
    leader was away), and the next leader must merge room["saved"] -> disk before it saves anything.
    """
    try:
        current = version_of(resolve_in_doc(DOCS[room["doc"]].parent, room["path"]).stat())
    except (OSError, ApiError, KeyError):
        current = None
    if current == room["disk"]:
        return {}
    return {"stale": True, "base": room["saved"], "gone": current is None and room["disk"] is not None}


def publish_leader(rid: str, room: dict) -> None:
    leader = room_leader(room)
    if leader != room.get("leader"):
        room["leader"] = leader
        BUS.publish("y-leader", {"room": rid, "leader": leader, **(room_drift(room) if leader else {})})


def note_write(doc: str, path: str, text: str, version: str, client: str | None = None) -> None:
    """
    A save went through. Only the room's leader writes the room's text; a save from anywhere else (a tab outside
    the room) leaves room["disk"] stale, so the next leader sees drift and merges it instead of overwriting it.
    """
    with COLLAB_LOCK:
        room = ROOMS.get(room_id(doc, path))
        if room and client is not None and client == room_leader(room):
            room["disk"], room["saved"] = version, text.replace("\r\n", "\n")


def presence(role: str = "owner") -> dict:
    with COLLAB_LOCK:
        users = [
            {"cid": cid, "name": c["name"], "color": c["color"], "role": c["role"], "path": c["path"], "doc": c["doc"]}
            for cid, c in CLIENTS.items() if c["name"]
        ]
    users.sort(key=lambda u: (u["name"] or "", u["cid"]))
    return {"users": users if role == "owner" else presence_for(users, role)}


def presence_for(users: list[dict], role: str) -> list[dict]:
    """Shared sessions only see who is in the shared document, and not which build-config file they have open."""
    return [
        {**u, "path": None if is_rc(u.get("path") or "") else u["path"]}
        for u in users if role == "owner" or u.get("doc") == SHARE["doc"]
    ]


def publish_presence() -> None:
    BUS.publish("presence", presence(), topic="sys", ephemeral=True)


def check_room(doc, path, role: str) -> tuple[str, Path]:
    """Validate a room request: a served document, and for shared sessions the shared one."""
    if not isinstance(doc, str) or doc not in DOCS or (role != "owner" and doc != SHARE["doc"]):
        raise ApiError("Unknown document.", 404)
    if role != "owner" and isinstance(path, str) and is_rc(path):  # The PUT API refuses these too.
        raise ApiError("This file configures the build and cannot be edited through a shared link.", 403)
    if not isinstance(path, str) or file_kind(path) != "text":
        raise ApiError("Not a text file.", 415)
    target = resolve_in_doc(DOCS[doc].parent, path)
    if role == "view" and not target.is_file():
        raise ApiError("No such file.", 404)
    return room_id(doc, path), target


def reply_error(exc: ApiError) -> dict:
    return {"type": "error", "topic": "sys", "data": {"error": str(exc), "status": exc.status}}


def on_hello(message: dict, client: str, role: str) -> dict:
    data = message.get("data") or {}
    with COLLAB_LOCK:
        record = CLIENTS[client]
        # Behind the gateway the name is the account's, not whatever the browser claims.
        record["name"] = (record.get("user") or "").partition(";")[2][:40] or str(data.get("name") or "Guest")[:40]
        color = str(data.get("color") or "")
        record["color"] = color if re.fullmatch(r"#[0-9a-fA-F]{6}", color) else "#0969da"
        path = data.get("path")
        record["path"] = path[:300] if isinstance(path, str) else None
        doc = data.get("doc")
        record["doc"] = doc if isinstance(doc, str) and doc in DOCS else None
    publish_presence()
    return {"type": "presence", "topic": "sys", "data": presence(role)}


def on_join(message: dict, client: str, role: str) -> dict:
    data = message.get("data") or {}
    try:
        rid, path = check_room(data.get("doc"), data.get("path"), role)
    except ApiError as exc:
        return reply_error(exc)
    seen = data.get("epoch") if isinstance(data.get("epoch"), str) else None
    epoch = seen if role != "view" else None
    with COLLAB_LOCK:
        room = ROOMS.get(rid)
        conflict = False
        if rid not in CLIENTS[client]["rooms"] and len(CLIENTS[client]["rooms"]) >= MAX_ROOMS_PER_CLIENT:
            return reply_error(ApiError("Too many files open in shared editing.", 429))
        if room is None:
            if role != "owner":
                if not path.is_file():  # A shared edit link creates files through PUT only, never rooms.
                    return reply_error(ApiError("No such file.", 404))
                if len(ROOMS) >= MAX_ROOMS:
                    return reply_error(ApiError("Too many shared files open on this server.", 503))
                if sum(1 for r in ROOMS.values() if r["opener"] == role) >= MAX_ROOMS_PER_ROLE:
                    return reply_error(ApiError("Too many files open through this link.", 429))
            room = ROOMS[rid] = {
                "epoch": epoch or secrets.token_hex(8), "log": [], "aware": {}, "members": {}, "gone_at": None,
                "claimed": bool(epoch), "opener": role, "text": "", "eol": "\n", "leader": None, "bytes": 0,
                "by_role": {},
                "doc": data["doc"], "path": data["path"], "disk": None, "saved": "",
            }
            try:  # The seed every first joiner builds identically; kept in the room so it stays consistent.
                found = read_text_file(DOCS[data["doc"]].parent, data["path"])
                room["disk"], room["saved"] = found["version"], found["text"]
                if not epoch:
                    room["text"], room["eol"] = found["text"], found["eol"]
            except ApiError:
                pass
        elif seen and seen != room["epoch"]:
            conflict = True
        room["gone_at"] = None
        aid = data.get("aid") if isinstance(data.get("aid"), int) else None
        member = room["members"].setdefault(client, {"aid": aid, "role": role})
        member["aid"] = aid
        CLIENTS[client]["rooms"].add(rid)
        publish_leader(rid, room)
        seed = room["text"] if not room["log"] and not room["claimed"] else None
        reply = {
            "room": rid, "epoch": room["epoch"], "updates": list(room["log"]), "aware": dict(room["aware"]),
            "leader": room["leader"], "seed": seed, "eol": room["eol"], "conflict": conflict,
            **(room_drift(room) if room["leader"] is None or room["leader"] == client else {}),
        }
    return {"type": "y-state", "topic": "sys", "data": reply}


def on_update(message: dict, client: str, role: str) -> dict | None:
    data = message.get("data") or {}
    rid, update = data.get("room"), data.get("u")
    if role == "view":
        return reply_error(ApiError("This link is view-only.", 403))
    if not isinstance(update, str) or len(update) > MAX_UPDATE:
        return reply_error(ApiError("Bad update.", 413))
    if role != "owner":
        doc, _, path = str(rid).partition("\n")
        if doc != SHARE["doc"] or is_rc(path):
            return reply_error(ApiError("This file cannot be edited through a shared link.", 403))
    with COLLAB_LOCK:
        room = ROOMS.get(rid)
        if room is None or client not in room["members"]:
            return reply_error(ApiError("Join the room first.", 409))
        if len(room["log"]) <= 3 and update in room["log"]:
            return None  # Two clients seeding the same file produce the same bytes.
        if room["bytes"] + len(update) > MAX_ROOM_BYTES:
            return reply_error(ApiError("The shared editing history of this file is full; save and reopen it.", 413))
        used = sum(r["by_role"].get(role, 0) for r in ROOMS.values())
        if role != "owner" and used + len(update) > MAX_BYTES_PER_ROLE:
            return reply_error(ApiError("This link has used up its shared editing budget.", 413))
        room["by_role"][role] = room["by_role"].get(role, 0) + len(update)
        room["bytes"] += len(update)
        room["log"].append(update)
        BUS.publish("y-update", {"room": rid, "u": update, "cid": client})
    return None


def on_aware(message: dict, client: str, role: str) -> None:
    data = message.get("data") or {}
    rid, update = data.get("room"), data.get("u")
    with COLLAB_LOCK:
        room = ROOMS.get(rid)
        if room is None or client not in room["members"] or not isinstance(update, str) or len(update) > 64 * 1024:
            return
        room["aware"][client] = update
    BUS.publish("y-aware", {"room": rid, "u": update, "cid": client}, topic="sys", ephemeral=True)


def leave_room(client: str, rid: str) -> None:
    with COLLAB_LOCK:
        room = ROOMS.get(rid)
        if CLIENTS.get(client):
            CLIENTS[client]["rooms"].discard(rid)
        if room is None or client not in room["members"]:
            return
        member = room["members"].pop(client)
        room["aware"].pop(client, None)
        BUS.publish("y-gone", {"room": rid, "cid": client, "aid": member["aid"]}, topic="sys", ephemeral=True)
        publish_leader(rid, room)
        if not room["members"]:
            room["gone_at"] = time.monotonic()


def on_bye(message: dict, client: str, role: str) -> None:
    client_gone(client)  # The tab is closing (sent as a beacon): no need to wait for the timeout.


def on_leave(message: dict, client: str, role: str) -> None:
    leave_room(client, (message.get("data") or {}).get("room"))


def client_gone(client: str) -> None:
    with COLLAB_LOCK:
        record = CLIENTS.get(client)
        for rid in list(record["rooms"]) if record else []:
            leave_room(client, rid)
        CLIENTS.pop(client, None)
    publish_presence()


def ws_closed(client: str) -> None:
    """A socket died. The client may be switching transports, so give it a few seconds before it counts as gone."""
    with COLLAB_LOCK:
        if client in CLIENTS:
            CLIENTS[client]["seen"] = min(CLIENTS[client]["seen"], time.monotonic() - CLIENT_TIMEOUT + 8)


def reap(now: float | None = None) -> None:
    """Drop silent clients and rooms that stayed empty past the grace period."""
    now = time.monotonic() if now is None else now
    with COLLAB_LOCK:
        stale = [cid for cid, c in CLIENTS.items() if now - c["seen"] > CLIENT_TIMEOUT]
        old = [
            rid for rid, r in ROOMS.items()
            if not r["members"] and r["gone_at"] is not None and now - r["gone_at"] > ROOM_GRACE
        ]
        for rid in old:
            del ROOMS[rid]
    for cid in stale:
        client_gone(cid)


def housekeeping() -> None:
    while not STOP.wait(3.0):
        reap()


HANDLERS.update({
    "hello": on_hello, "y-join": on_join, "y-update": on_update, "y-aware": on_aware, "y-leave": on_leave,
    "bye": on_bye,
})


# ---------------------------------------------------------------------------
# HTTP
# ---------------------------------------------------------------------------

STATIC_TYPES = {
    ".html": "text/html; charset=utf-8", ".js": "text/javascript; charset=utf-8", ".css": "text/css; charset=utf-8",
    ".mjs": "text/javascript; charset=utf-8", ".woff2": "font/woff2",
}
MAX_BODY = 8 * 1024 * 1024
POLL_HOLD = 25.0


CDNS = "https://esm.sh https://cdnjs.cloudflare.com https://cdn.jsdelivr.net"
DOCX_TYPE = "application/vnd.openxmlformats-officedocument.wordprocessingml.document"
READ_API = {
    "/api/files", "/api/file", "/api/raw", "/api/image", "/api/outline", "/api/refs", "/api/lint", "/api/warnings",
    "/synctex/edit",
}


def attachment_header(filename: str) -> str:
    """Content-Disposition with an ASCII fallback name and the real one as RFC 5987 filename*."""
    ascii_name = re.sub(r'[^A-Za-z0-9._ -]', "_", filename)
    return f"attachment; filename=\"{ascii_name}\"; filename*=UTF-8''{quote(filename, safe='')}"


_VENDOR_WARNED = [False]


def vendor_files() -> dict:
    """url -> file name of the offline copies, or {} when vendor/ is missing or does not cover the import map."""
    try:
        files = json.loads((VENDOR_DIR / "manifest.json").read_text("utf-8"))["files"]
        local = {url: info["file"] for url, info in files.items()}
        page = (UI_DIR / "index.html").read_text("utf-8")
        block = re.search(r'<script type="importmap">(.*?)</script>', page, re.S).group(1)
        wanted = list(json.loads(block)["imports"].values())
    except (OSError, ValueError, KeyError, TypeError, AttributeError):
        return {}
    if all(url in local for url in wanted):
        return local
    if not _VENDOR_WARNED[0]:
        _VENDOR_WARNED[0] = True
        print("vendor/ is out of date; run scripts/vendor_ui.py", file=sys.stderr)
    return {}


def localize(page: bytes) -> bytes:
    """With vendored libraries, point the import map at the local copies."""
    local = vendor_files()
    if not local:
        return page
    prefix = b"./ui/vendor/" if GATEWAY["secret"] else b"/ui/vendor/"  # Behind the gateway the page is at /p/<id>/.
    return re.sub(rb'"(https://[^"]+)"', lambda m: b'"%s%s"' % (prefix, local[m.group(1).decode()].encode())
                  if m.group(1).decode() in local else m.group(0), page)


def csp(page: bytes, host: str) -> str:
    """Only our own files, the pinned CDNs (none when vendored), this origin's sockets; import map by hash."""
    cdns = "" if vendor_files() else " " + CDNS
    block = re.search(rb'<script type="importmap">(.*?)</script>', page, re.S)
    inline = f" 'sha256-{base64.b64encode(hashlib.sha256(block.group(1)).digest()).decode()}'" if block else ""
    return "; ".join([
        "default-src 'none'", f"script-src 'self'{inline}{cdns}", f"style-src 'self' 'unsafe-inline'{cdns}",
        "img-src 'self' data: blob:", f"font-src 'self' data:{cdns}", f"worker-src 'self' blob:{cdns}",
        f"connect-src 'self' ws://{host} wss://{host}{cdns}", "base-uri 'none'", "form-action 'self'",
        "frame-ancestors 'none'", "object-src 'none'",
    ])


def check_permission(role: str, method: str, path: str, query: dict) -> None:
    """Raise ApiError unless this role may make this request. The owner may do anything."""
    if role == "owner":
        return
    doc = (query.get("doc") or [None])[0]

    def scoped(name) -> None:
        if name != SHARE["doc"]:
            raise ApiError("Not part of the shared document.", 403)

    def need_edit() -> None:
        if role != "edit":
            raise ApiError("This link is view-only.", 403)

    if method == "GET":
        if path == "/" or path.startswith("/ui/") or path in ("/api/config", "/api/health", "/ws", "/api/poll"):
            return
        if path.startswith("/docx/"):
            raise ApiError("Only the owner can download the DOCX export.", 403)
        if path.startswith(("/pdf/", "/log/")):
            scoped(path[1:].partition("/")[2])
        elif path in READ_API:
            scoped(doc)
        elif path == "/forward" and "quiet" in query:
            return  # Checked again once the target document is known.
        else:
            raise ApiError("Forbidden.", 403)
    elif method == "POST" and path == "/api/send":
        return
    elif method == "POST" and path == "/rebuild":
        need_edit()
        scoped(doc)
        if not rate_ok("rebuild", *REBUILD_LIMIT):
            raise ApiError("Too many rebuilds; wait a moment.", 429)
    elif method == "POST" and path == "/api/fs":
        need_edit()
        scoped(doc)
        if any(is_rc((query.get(key) or [""])[0]) for key in ("path", "to")):
            raise ApiError("This file configures the build and cannot be changed through a shared link.", 403)
    elif method == "POST" and path == "/api/upload":
        need_edit()
        scoped(doc)
        if not rate_ok("upload", 30, 60.0):
            raise ApiError("Too many uploads; wait a moment.", 429)
    elif method == "POST" and path == "/api/grammar":
        need_edit()  # Sends text to LanguageTool, so a view-only link cannot start it.
        scoped(doc)
        if not rate_ok("grammar", 30, 60.0):
            raise ApiError("Too many grammar checks; wait a moment.", 429)
    # POST /api/docx and GET /docx/ are owner only on purpose: pandoc reads any file a \input names, so an edit
    # link could otherwise put /etc/passwd into a download.
    elif method == "POST" and path == "/api/bib":
        scoped(doc)  # Read-only (the sources and .bib files a view link can already read), POST to carry unsaved text.
    elif method == "POST" and path == "/api/bib/edit":
        need_edit()  # Computes a splice; the editor applies it and the normal save path writes the file.
        scoped(doc)
    elif method == "POST" and path in ("/api/bib/lookup", "/api/bib/import"):
        need_edit()  # Sends one DOI or title to Crossref, so a view-only link cannot start it.
        scoped(doc)
        if not rate_ok("bib-guest", 10, 60.0):  # Own key and a third of bibfix.LIMITER: guests cannot starve the owner.
            raise ApiError("Too many lookups; wait a moment.", 429)
    elif method == "POST" and path == "/api/focus":
        need_edit()  # Starts LaTeX, so it counts like a rebuild.
        scoped(doc)
        if not rate_ok("rebuild", *REBUILD_LIMIT):
            raise ApiError("Too many rebuilds; wait a moment.", 429)
    elif method == "PUT" and path == "/api/file":
        need_edit()
        scoped(doc)
        if posixpath.basename((query.get("path") or [""])[0]).lower() in RC_NAMES:
            raise ApiError("This file configures the build and cannot be edited through a shared link.", 403)
    else:
        raise ApiError("Forbidden.", 403)


def health(role: str = "owner") -> dict:
    return {
        "ok": True, "rev": BUS.rev, "uptime": round(time.time() - STARTED, 1),
        "docs": sorted(DOCS) if role == "owner" else [d for d in sorted(DOCS) if d == SHARE["doc"]],
        "synctex": shutil.which("synctex") is not None, "texcount": shutil.which("texcount") is not None,
    }


class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.0"
    timeout = 75  # Socket timeout: a stalled client cannot hold a thread forever. The UI pings every 20 s.
    role = "owner"
    token = None
    user = None  # "id;display name" behind the gateway

    def log_message(self, *args) -> None:  # Quiet: build output is the interesting part.
        pass

    def reply(self, status: int, body: bytes, kind: str, extra: dict | None = None) -> None:
        self.send_response(status)
        self.send_header("Content-Type", kind)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Referrer-Policy", "no-referrer")
        self.send_header("X-Frame-Options", "DENY")
        if kind.startswith("text/html"):
            self.send_header("Content-Security-Policy", csp(body, self.headers.get("Host", "")))
        for key, value in (extra or {}).items():
            self.send_header(key, value)
        self.end_headers()
        self.wfile.write(body)

    def json(self, data, status: int = 200, extra: dict | None = None) -> None:
        self.reply(status, json.dumps(data).encode(), "application/json", extra)

    def cookie(self, token: str) -> dict:
        secure = SHARE["public"] and SHARE["public"].startswith("https") and self.hostname() in SHARE["hosts"]
        flags = "; Path=/; HttpOnly; SameSite=Lax" + ("; Secure" if secure else "")
        return {"Set-Cookie": f"{cookie_name()}={token}{flags}"}

    def hostname(self) -> str:
        host = self.headers.get("Host", "")
        return (host if host.endswith("]") else host.rsplit(":", 1)[0]).lower()

    def authenticate(self) -> str | None:
        if GATEWAY["secret"]:
            role, self.user = gateway_identity(self.headers)
            return role
        if not SHARE["on"]:
            return "owner"
        jar = SimpleCookie()
        try:
            jar.load(self.headers.get("Cookie", ""))
        except Exception:  # noqa: BLE001 - a malformed Cookie header is just "no token".
            pass
        morsel = jar.get(cookie_name())
        self.token = morsel.value if morsel else None
        return role_for_token(self.token)

    def allowed(self) -> bool:
        """Refuse foreign Host headers (DNS rebinding) while bound to loopback."""
        host = self.hostname()
        if SETTINGS["check_host"] and host not in LOOPBACK_HOSTS and host not in SHARE["hosts"]:
            self.reply(403, b"Forbidden host", "text/plain")
            return False
        return True

    def same_origin(self) -> bool:
        """State-changing requests must come from our own page (blocks cross-site writes)."""
        origin = self.headers.get("Origin")
        if origin is None:
            return True
        if urlsplit(origin).netloc != self.headers.get("Host", ""):
            self.reply(403, b"Forbidden origin", "text/plain")
            return False
        return True

    def body(self) -> dict:
        try:
            size = int(self.headers.get("Content-Length", "0"))
        except ValueError:
            size = -1
        if not 0 <= size <= MAX_BODY:
            raise ApiError("Bad or oversized body.", 413)
        if "json" not in self.headers.get("Content-Type", ""):
            raise ApiError("Expected application/json.", 415)
        try:
            data = json.loads(self.rfile.read(size) or b"{}")
        except ValueError:
            raise ApiError("Bad JSON.", 400)
        if not isinstance(data, dict):
            raise ApiError("Expected a JSON object.", 400)
        return data

    def doc_root(self, query: dict) -> Path:
        name = query.get("doc", [""])[0]
        if name not in DOCS:
            raise ApiError("Unknown document.", 404)
        return DOCS[name].parent

    def guarded(self, method) -> None:
        if not self.allowed():
            return
        try:
            url = urlsplit(self.path)
            query = parse_qs(url.query)
            if self.command == "GET" and url.path == "/" and SHARE["on"] and "token" in query and not GATEWAY["secret"]:
                if role_for_token(query["token"][0]):  # First visit through a link: keep the token in a cookie only.
                    self.reply(302, b"", "text/plain", {"Location": "/", **self.cookie(query["token"][0])})
                    return
            self.role = self.authenticate()
            if self.role is None:
                self.reply(401, b"Unauthorized: open the full share link you were sent.", "text/plain")
                return
            check_permission(self.role, self.command, unquote(url.path), query)
            method()
        except ApiError as exc:
            self.json({"error": str(exc), **exc.extra}, exc.status)
        except SynctexError as exc:
            self.json({"error": str(exc)}, 400)
        except (BrokenPipeError, ConnectionError):
            pass

    def do_GET(self) -> None:
        self.guarded(self.get)

    def do_POST(self) -> None:
        self.guarded(self.post)

    def do_PUT(self) -> None:
        self.guarded(self.put)

    def post(self) -> None:
        if not self.same_origin():
            return
        url = urlsplit(self.path)
        query = parse_qs(url.query)
        name = query.get("doc", [""])[0]
        if url.path == "/rebuild" and name in DOCS:
            with LOCK:
                FORCE.add(name)
            self.json({"ok": True})
        elif url.path == "/api/upload" and name in DOCS:
            try:
                size = int(self.headers.get("Content-Length", "0"))
            except ValueError:
                size = -1
            if not 0 < size <= MAX_UPLOAD:
                raise ApiError("Bad or oversized image.", 413)
            self.json(save_upload(name, query.get("name", [""])[0], self.rfile.read(size)))
        elif url.path == "/api/fs" and name in DOCS:
            op, rel, to = query.get("op", [""])[0], query.get("path", [""])[0], query.get("to", [None])[0]
            self.json(fs_operation(name, op, rel, to))
        elif url.path == "/api/grammar" and name in DOCS:
            self.json(grammar_check(name, self.body().get("text")))
        elif url.path == "/api/bib/lookup" and name in DOCS:
            self.json(bib_lookup(self.body()))
        elif url.path == "/api/bib" and name in DOCS:
            self.json(bib_overview(name, self.body().get("texts")))
        elif url.path == "/api/bib/edit" and name in DOCS:
            self.json(bib_edit(self.body()))
        elif url.path == "/api/bib/import" and name in DOCS:
            self.json(bib_import(self.body()))
        elif url.path == "/api/grammar/settings" and self.role == "owner":
            self.json(grammar_settings(self.body()))
        elif url.path == "/api/docx" and name in DOCS:
            if SHARE["on"]:
                raise ApiError("DOCX export is off while sharing: pandoc reads any file the source names.", 409)
            ok, message = build.export_docx(DOCS[name])
            if not ok:
                raise ApiError(message, 500)
            self.json({"ok": True})
        elif url.path == "/api/focus" and name in DOCS:
            self.json({"ok": True, "started": start_focus(name, query.get("path", [""])[0])})
        elif url.path == "/api/send":  # Long-poll transport: client -> server.
            data = self.body()
            client = str(query.get("cid", ["?"])[0])[:64]
            replies = []
            for message in data.get("messages", []):
                reply = handle_client_message(message, client, self.role, self.user) if isinstance(message, dict) \
                    else None
                if reply:
                    replies.append(reply)
            self.json({"replies": replies, "rev": BUS.rev})
        elif url.path == "/api/share":
            data = self.body()
            doc = data.get("doc") if data.get("doc") in DOCS else next(iter(sorted(DOCS)), None)
            share_start(str(data.get("provider", "auto")), doc, SHARE["port"])
            self.json(share_info(), extra=self.cookie(SHARE["tokens"]["owner"]))
        elif url.path == "/api/share/regenerate":
            share_regenerate()
            self.json(share_info())
        elif url.path == "/api/share/stop":
            share_disable()
            self.json(share_info())
        else:
            self.reply(404, b"Not found", "text/plain")

    def put(self) -> None:
        if not self.same_origin():
            return
        url = urlsplit(self.path)
        query = parse_qs(url.query)
        if url.path != "/api/file":
            self.reply(404, b"Not found", "text/plain")
            return
        root = self.doc_root(query)
        data = self.body()
        result = write_text_file(
            root, query.get("path", [""])[0], data.get("text"), data.get("base"), data.get("eol", "\n"),
        )
        note_write(query["doc"][0], query.get("path", [""])[0], data["text"], result["version"],
                   str(query.get("cid", [""])[0])[:64] or None)
        self.json(result)

    def get(self) -> None:
        url = urlsplit(self.path)
        query = parse_qs(url.query)
        path = unquote(url.path)

        if path == "/":
            self.static("index.html")
        elif path.startswith("/ui/"):
            self.static(path[4:])
        elif path == "/api/health":
            self.json(health(self.role))
        elif path == "/api/config":
            self.json({"editor": SETTINGS["editor"], "role": self.role, "collab": True,
                       "pandoc": shutil.which("pandoc") is not None, "hosted": bool(GATEWAY["secret"]),
                       "grammar": grammar_info(self.role)})
        elif path == "/api/share":
            self.json(share_info())
        elif path == "/ws":
            self.websocket(query)
        elif path == "/api/poll":
            self.poll(query)
        elif path == "/events":
            self.events()
        elif path.startswith(("/pdf/", "/log/", "/docx/")):
            self.file(path, "focus" in query)
        elif path == "/api/files":
            root = self.doc_root(query)
            self.json({"files": list_files(root), "dirs": empty_dirs(root)})
        elif path == "/api/file":
            self.json(read_text_file(self.doc_root(query), query.get("path", [""])[0]))
        elif path == "/api/raw":
            self.raw(query)
        elif path == "/api/image":
            found = find_image(self.doc_root(query), query.get("name", [""])[0], query.get("from", [""])[0])
            if found is None:
                raise ApiError("No such image.", 404)
            self.image(found)
        elif path == "/api/outline":
            name = query.get("doc", [""])[0]
            self.doc_root(query)
            data = outline(DOCS[name])
            data["pages"] = STATE.get(name, {}).get("pages")
            self.json(data)
        elif path == "/api/refs":
            self.json(references(self.doc_root(query)))
        elif path == "/api/lint":
            self.doc_root(query)
            self.json({"findings": lint(query["doc"][0])})
        elif path == "/api/warnings":
            self.doc_root(query)
            self.json({"warnings": doc_warnings(query["doc"][0])})
        elif path == "/synctex/edit":
            name = query.get("doc", [""])[0]
            if name not in DOCS:
                raise SynctexError("Unknown document.")
            self.json(inverse(DOCS[name], query))
        elif path == "/forward":
            name, box = forward(query, None if self.role == "owner" else SHARE["doc"])
            box["doc"] = name
            if "quiet" not in query:
                broadcast("forward", box)
            self.json(box)
        else:
            self.reply(404, b"Not found", "text/plain")

    def static(self, name: str) -> None:
        if name.startswith("vendor/"):  # only files the manifest lists, never a path from the URL
            name = name[7:]
            target = VENDOR_DIR / name
            if name not in vendor_files().values() or not target.is_file():
                self.reply(404, b"Not found", "text/plain")
                return
            self.reply(200, target.read_bytes(), STATIC_TYPES.get(target.suffix, "application/octet-stream"))
            return
        target = UI_DIR / name
        if "/" in name or "\\" in name or not target.is_file() or target.suffix not in STATIC_TYPES:
            self.reply(404, b"Not found", "text/plain")
            return
        body = target.read_bytes()
        self.reply(200, localize(body) if name == "index.html" else body, STATIC_TYPES[target.suffix])

    def image(self, path: Path) -> None:
        self.reply(200, path.read_bytes(), IMAGE_TYPES[path.suffix.lower()], {"Content-Security-Policy": "sandbox"})

    def raw(self, query: dict) -> None:
        rel = query.get("path", [""])[0]
        path = resolve_in_doc(self.doc_root(query), rel)
        if file_kind(rel) != "image" or not path.is_file():
            raise ApiError("Not an image.", 415)
        self.image(path)

    def file(self, path: str, focus: bool = False) -> None:
        kind, _, name = path[1:].partition("/")
        if name not in DOCS:
            self.reply(404, b"Unknown document", "text/plain")
            return
        main_tex = DOCS[name]
        if kind == "docx":
            target = build.docx_path_for(main_tex)
        elif focus:
            target = build.focus_paths(main_tex)[0 if kind == "pdf" else 1]
        else:
            target = build.output_path_for(main_tex) if kind == "pdf" else build.log_path_for(main_tex)
        try:
            body = target.read_bytes()
        except OSError:
            self.reply(404, b"Not built yet", "text/plain")
            return
        types = {"pdf": "application/pdf", "docx": DOCX_TYPE}
        self.reply(200, body, types.get(kind, "text/plain; charset=utf-8"),
                   {"Content-Disposition": attachment_header(posixpath.basename(name) + ".docx")}
                   if kind == "docx" else None)

    # --- transports -------------------------------------------------------

    def poll(self, query: dict) -> None:
        """Long-poll: hold up to POLL_HOLD seconds for messages newer than ?since=."""
        try:
            since = int(query["since"][0]) if "since" in query else None
        except ValueError:
            since = None
        if not bind_client(str(query.get("cid", ["?"])[0])[:64], self.role, self.user):
            raise ApiError("Client id belongs to another session.", 403)
        events = BUS.wait(since, POLL_HOLD) if since is not None else BUS.since(None)
        bind_client(str(query.get("cid", ["?"])[0])[:64], self.role, self.user)
        # rev is where the client resumes: filtered-out messages still advance it, so it cannot spin on them.
        self.json({"rev": events[-1]["rev"] if events else since, "events": visible(events, self.role)})

    def websocket(self, query: dict) -> None:
        key = self.headers.get("Sec-WebSocket-Key")
        if "websocket" not in self.headers.get("Upgrade", "").lower() or not key:
            self.reply(400, b"Expected a WebSocket upgrade", "text/plain")
            return
        if not self.same_origin():  # Has already answered 403.
            return
        client = str(query.get("cid", ["?"])[0])[:64]
        if not bind_client(client, self.role, self.user):
            raise ApiError("Client id belongs to another session.", 403)
        self.send_response(101)
        self.send_header("Upgrade", "websocket")
        self.send_header("Connection", "Upgrade")
        self.send_header("Sec-WebSocket-Accept", ws_accept(key))
        self.end_headers()
        self.wfile.flush()

        try:
            since = int(query["since"][0]) if "since" in query else None
        except ValueError:
            since = None
        role, token, user = self.role, self.token, self.user
        send_lock = threading.Lock()
        alive = threading.Event()

        def send(opcode: int, payload: bytes) -> None:
            with send_lock:
                self.wfile.write(ws_encode(opcode, payload))
                self.wfile.flush()

        def revoked() -> bool:  # Tokens regenerated or sharing stopped. The gateway closes its own sockets.
            return False if GATEWAY["secret"] else role_for_token(token) != role

        def pump() -> None:  # Bus -> socket.
            cursor = since
            try:
                while not alive.is_set() and not STOP.is_set() and not revoked():
                    messages = BUS.since(cursor) if cursor is None else BUS.wait(cursor, 5.0)
                    if alive.is_set():
                        break
                    bind_client(client, role, user)  # Keeps the client "seen" while the socket idles.
                    for message in visible(messages, role):
                        send(0x1, json.dumps(message).encode())
                    if messages:
                        cursor = messages[-1]["rev"]
                    elif cursor is None:
                        cursor = BUS.rev
            except OSError:
                pass
            finally:
                alive.set()
                try:
                    send(0x8, b"")  # Wakes the reader below if we are the one closing.
                except OSError:
                    pass

        threading.Thread(target=pump, daemon=True).start()
        partial: dict = {}
        try:
            while not alive.is_set():
                frame = ws_read(self.rfile.read, partial)  # Socket -> bus handlers.
                if frame is None or frame[0] == 0x8 or revoked():
                    break
                opcode, payload = frame
                if opcode == 0x9:
                    send(0xA, payload)
                elif opcode in (0x1, 0x2):
                    try:
                        message = json.loads(payload)
                    except ValueError:
                        continue
                    reply = handle_client_message(message, client, role, user) if isinstance(message, dict) else None
                    if reply:
                        send(0x1, json.dumps(reply).encode())
        except (OSError, ValueError, struct.error):
            pass
        finally:
            alive.set()
            ws_closed(client)
            try:
                send(0x8, b"")
            except OSError:
                pass

    def events(self) -> None:
        """Legacy SSE view of the bus; the UI does not use it."""
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream")
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        cursor = None
        try:
            while not STOP.is_set():
                messages = BUS.since(cursor) if cursor is None else BUS.wait(cursor, 10)
                if not messages:
                    self.wfile.write(b": keepalive\n\n")  # Also detects closed tabs.
                for message in messages:
                    self.wfile.write(f"event: {message['type']}\ndata: {json.dumps(message['data'])}\n\n".encode())
                    cursor = message["rev"]
                cursor = BUS.rev if cursor is None else cursor
                self.wfile.flush()
        except OSError:
            pass


# ---------------------------------------------------------------------------
# Self-test: probe the tunnel from outside, the way a collaborator's browser would
# ---------------------------------------------------------------------------

def _public_connection(url: str, timeout: float):
    parts = urlsplit(url)
    if parts.scheme == "https":
        return http.client.HTTPSConnection(parts.hostname, parts.port or 443, timeout=timeout)
    return http.client.HTTPConnection(parts.hostname, parts.port or 80, timeout=timeout)


def probe_http(url: str, path: str, cookie: str | None, timeout: float = 60.0) -> tuple[int, bytes, float]:
    headers = {"ngrok-skip-browser-warning": "1", "bypass-tunnel-reminder": "1", "User-Agent": "serve-selftest"}
    if cookie:
        headers["Cookie"] = cookie
    began = time.monotonic()
    conn = _public_connection(url, timeout)
    try:
        conn.request("GET", path, headers=headers)
        res = conn.getresponse()
        return res.status, res.read(), time.monotonic() - began
    finally:
        conn.close()


def probe_ws(url: str, cookie: str) -> float:
    """Open a WebSocket through the tunnel, send a ping, wait for the pong. Returns seconds."""
    parts = urlsplit(url)
    tls = parts.scheme == "https"
    began = time.monotonic()
    raw = socket.create_connection((parts.hostname, parts.port or (443 if tls else 80)), timeout=20)
    sock = ssl.create_default_context().wrap_socket(raw, server_hostname=parts.hostname) if tls else raw
    try:
        sock.sendall((
            f"GET /ws?cid=selftest HTTP/1.1\r\nHost: {parts.netloc}\r\nOrigin: {parts.scheme}://{parts.netloc}\r\n"
            f"Upgrade: websocket\r\nConnection: Upgrade\r\nSec-WebSocket-Key: dGhlIHNhbXBsZSBub25jZQ==\r\n"
            f"Sec-WebSocket-Version: 13\r\nCookie: {cookie}\r\nngrok-skip-browser-warning: 1\r\n\r\n"
        ).encode())
        stream = sock.makefile("rb")
        status = stream.readline()
        if b" 101 " not in status:
            raise OSError(f"handshake refused: {status.decode(errors='replace').strip()}")
        while stream.readline() not in (b"\r\n", b""):
            pass
        sock.sendall(ws_encode(0x1, json.dumps({"type": "ping", "data": 1}).encode(), b"\x01\x02\x03\x04"))
        for _ in range(10):
            frame = ws_read(stream.read)
            if frame and frame[0] == 0x1 and json.loads(frame[1]).get("type") == "pong":
                return time.monotonic() - began
        raise OSError("no pong")
    finally:
        sock.close()


def selftest(provider: str, port: int) -> int:
    global POLL_HOLD
    POLL_HOLD = 30.0
    SETTINGS["check_host"] = True
    server = ThreadingHTTPServer(("127.0.0.1", port), Handler)
    server.daemon_threads = True
    SHARE["port"] = server.server_address[1]
    threading.Thread(target=server.serve_forever, daemon=True).start()
    threading.Thread(target=housekeeping, daemon=True).start()
    rows: list[tuple[str, bool, str]] = []
    try:
        try:
            share_start(provider, None, SHARE["port"], wait=True)
        except ApiError as exc:
            build.error(str(exc))
            return 1
        if SHARE["status"] != "ready":
            build.error(SHARE["error"] or "The tunnel did not come up.")
            return 1
        url = SHARE["public"] or f"http://127.0.0.1:{SHARE['port']}"
        cookie = f"{cookie_name()}={SHARE['tokens']['owner']}"
        build.info(f"Probing {url} through {SHARE['provider']} ...")

        def check(name: str, fn) -> None:
            try:
                ok, detail = fn()
            except (OSError, ValueError, http.client.HTTPException) as exc:
                ok, detail = False, f"{type(exc).__name__}: {exc}"
            rows.append((name, ok, detail))

        def health_check():
            status, body, took = probe_http(url, "/api/health", cookie)
            return status == 200 and b'"ok": true' in body, f"HTTP {status} in {took:.2f}s"

        def token_check():
            status, _, _ = probe_http(url, "/api/health", None)
            return status == 401, f"HTTP {status} without a token (want 401)"

        def ws_check():
            return True, f"ping/pong in {probe_ws(url, cookie):.2f}s"

        def poll_check():
            status, body, took = probe_http(url, f"/api/poll?since={BUS.rev}&cid=selftest", cookie, timeout=120)
            return status == 200 and took >= 28, f"HTTP {status} after {took:.1f}s (held 30s; want 200 after ~30s)"

        check("GET /api/health", health_check)
        check("token required", token_check)
        check("WebSocket echo", ws_check)
        check("30 s long-poll", poll_check)
    finally:
        share_disable()
        server.shutdown()
    width = max(len(r[0]) for r in rows)
    print(f"\n{'check'.ljust(width)}  result  detail")
    for name, ok, detail in rows:
        print(f"{name.ljust(width)}  {'PASS' if ok else 'FAIL':6}  {detail}")
    return 0 if all(r[1] for r in rows) else 1


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def main() -> int:
    parser = argparse.ArgumentParser(description="Local LaTeX editor with live PDF preview and SyncTeX.")
    parser.add_argument("docs", nargs="*", metavar="DOC", help="Documents to serve (name or glob). Default: all.")
    parser.add_argument("--port", type=int, default=8000)
    parser.add_argument("--host", default="127.0.0.1", help="Bind address (default: 127.0.0.1, this machine only).")
    parser.add_argument("--source", metavar="DIR", help="Directory holding the documents (default: files/).")
    parser.add_argument("--no-open", action="store_true", help="Do not open a browser.")
    parser.add_argument("--sandbox", action="store_true", help="Run LaTeX under bubblewrap (LATEX_SANDBOX=bwrap).")
    parser.add_argument("--editor", default="vscode", help="URL scheme for source links (vscode, cursor, ...).")
    parser.add_argument(
        "--share", nargs="?", const="auto", choices=PROVIDER_CHOICES, metavar="PROVIDER",
        help="Share the first document through a tunnel with token links (view and edit). PROVIDER: "
        f"{', '.join(PROVIDER_CHOICES)} (default auto; 'local' = token links without a tunnel).",
    )
    parser.add_argument(
        "--share-selftest", action="store_true",
        help="Start a tunnel (--share PROVIDER, default auto); check health, WebSocket and a 30 s long-poll.",
    )
    parser.add_argument(
        "--gateway", action="store_true",
        help="Worker for scripts/host.py: serve the one document in --source to the gateway only (secret in "
        "LP_HOST_SECRET; loopback; every request a shared editor or viewer).",
    )
    parser.add_argument("--build-timeout", type=int, default=None, help=argparse.SUPPRESS)
    args = parser.parse_args()
    signal.signal(signal.SIGTERM, lambda *_: STOP.set())  # Stop like Ctrl+C would: tunnels must not be orphaned.
    if args.share_selftest:
        return selftest(args.share or "auto", args.port if args.port != 8000 else 0)

    if args.gateway:
        secret = os.environ.pop("LP_HOST_SECRET", "")  # Popped: LaTeX and its children inherit os.environ.
        project = Path(args.source or "").resolve()
        if len(secret) < 32 or args.host not in ("127.0.0.1", "::1") or args.share or args.docs \
                or not (project / "main.tex").is_file():
            build.error("--gateway needs LP_HOST_SECRET (32+ chars), --source <dir with main.tex>, a loopback --host, "
                        "and no --share or DOC arguments.")
            return 2
        # The project's parent holds the document folder plus its out/ and cache (dot names: a document folder
        # never starts with a dot), so nothing is written next to the scripts and error messages show no host paths.
        build.ROOT_DIR = build.SOURCE_DIR = build.FILES_DIR = project.parent
        build.OUT_DIR, build.CACHE_DIR = project.parent / ".out", project.parent / ".cache"
        args.docs, args.source = [project.name], None
        gateway_enable(secret, project.name, args.build_timeout)
        try:
            QUOTA.update(bytes=max(0, int(os.environ.pop("LP_QUOTA_BYTES", "0"))), area=project.parent)
        except ValueError:
            build.error("LP_QUOTA_BYTES must be a whole number of bytes.")
            return 2

    if args.source:
        source = Path(args.source) if Path(args.source).is_absolute() else build.ROOT_DIR / args.source
        if not source.is_dir():
            build.error(f"--source {args.source}: not a directory")
            return 2
        build.SOURCE_DIR = source.resolve()

    documents = build.find_documents()
    unknown = build.unknown_patterns(documents, args.docs)
    if unknown:
        build.error(f"No document matches: {', '.join(unknown)}")
        return 2

    if args.sandbox:
        os.environ[build.sandbox.VARIABLE] = "bwrap"
    latexmk = build.check_latex()
    SETTINGS["editor"] = args.editor
    SETTINGS["latexmk"] = latexmk
    SETTINGS["check_host"] = args.host in LOOPBACK_HOSTS | {"::1"} and not args.gateway
    if not SETTINGS["check_host"] and not args.gateway:
        build.error(f"Listening on {args.host}: anyone who can reach this port can read and EDIT your documents.")

    try:
        server = ThreadingHTTPServer((args.host, args.port), Handler)
    except OSError as exc:
        build.error(f"Cannot listen on {args.host}:{args.port}: {exc}")
        return 1
    server.daemon_threads = True

    port = server.server_address[1]
    SHARE["port"] = port
    address = f"http://{'localhost' if args.host == '127.0.0.1' else args.host}:{port}/"
    first = build.select_documents(documents, args.docs)
    if first:
        address += "#" + build.doc_name(first[0]).replace(" ", "%20")
    if args.gateway:
        print(f"LP_GATEWAY_PORT={port}", flush=True)  # host.py reads this line.
    build.info(f"Serving {address}  (Ctrl+C to stop)")

    threading.Thread(target=watcher, args=(latexmk, args.docs), daemon=True).start()
    threading.Thread(target=fs_watcher, daemon=True).start()
    threading.Thread(target=housekeeping, daemon=True).start()
    threading.Thread(target=server.serve_forever, daemon=True).start()
    if args.share:
        shared = build.doc_name(first[0]) if first else None
        try:
            share_start(args.share, shared, port, wait=True)
        except ApiError as exc:
            build.error(str(exc))
            STOP.set()
            return 1
        if SHARE["status"] != "ready":
            build.error(SHARE["error"] or "The tunnel did not come up.")
            STOP.set()
            return 1
        links = share_links()
        print(f"\nSharing '{shared}' via {SHARE['provider']} until you press Ctrl+C. Anyone with a link can use it:")
        print(f"  view (read-only): {links['view']}\n  edit            : {links['edit']}")
        if SHARE["provider"] == "ngrok":
            print("  ngrok's free plan shows a warning page first; each visitor clicks 'Visit Site' once.")
        address = address.replace(f":{port}/", f":{port}/?token={SHARE['tokens']['owner']}", 1)
        print(f"  you (owner)     : {address}  (keep this one private)")
    if not args.no_open:
        threading.Timer(0.3, webbrowser.open, (address,)).start()

    try:
        while not STOP.wait(0.5):
            pass
    except KeyboardInterrupt:
        build.info("\nStopping.")
    finally:
        STOP.set()
        share_disable()
        server.shutdown()
        server.server_close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
