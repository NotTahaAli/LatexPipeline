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
from urllib.parse import parse_qs, unquote, urlsplit

import build
import hints

PDFJS = "https://cdnjs.cloudflare.com/ajax/libs/pdf.js/4.4.168"
LOOPBACK_HOSTS = {"localhost", "127.0.0.1", "[::1]"}
UI_DIR = Path(__file__).resolve().parent / "serve_ui"
STARTED = time.time()

STOP = threading.Event()
LOCK = threading.Lock()
STATE: dict[str, dict] = {}  # doc name -> status dict sent to browsers
DOCS: dict[str, Path] = {}  # doc name -> main.tex
FORCE: set[str] = set()  # documents the browser asked to rebuild from scratch
SETTINGS = {"editor": "vscode", "check_host": True}


# ---------------------------------------------------------------------------
# Message bus (transport-independent: WebSocket, long-poll and SSE all read it)
# ---------------------------------------------------------------------------

class Bus:
    """
    Message log with a revision counter; readers resume from a revision. Durable messages (state, edits) sit in a
    long log; ephemeral ones (cursors, presence) in a short one and are never the reason for a resync.
    """

    def __init__(self, keep: int = 2000, ephemeral: int = 300) -> None:
        self.cond = threading.Condition()
        self.rev = 0
        self.floor = 0  # newest revision dropped from the durable log
        self.log: collections.deque = collections.deque(maxlen=keep)
        self.eph: collections.deque = collections.deque(maxlen=ephemeral)

    def publish(self, type_: str, data, topic: str = "doc", ephemeral: bool = False) -> int:
        with self.cond:
            self.rev += 1
            message = {"rev": self.rev, "topic": topic, "type": type_, "data": data}
            if ephemeral:
                self.eph.append(message)
            else:
                if len(self.log) == self.log.maxlen:
                    self.floor = self.log[0]["rev"]
                self.log.append(message)
            self.cond.notify_all()
            return self.rev

    def since(self, rev: int | None) -> list[dict]:
        """Messages after rev. None, or a rev older than the log keeps, yields one fresh state snapshot."""
        with self.cond:
            if rev is None or rev > self.rev or rev < self.floor:
                return [{"rev": self.rev, "topic": "doc", "type": "state", "data": snapshot(), "resync": True}]
            return sorted((m for m in itertools.chain(self.log, self.eph) if m["rev"] > rev), key=lambda m: m["rev"])

    def wait(self, rev: int, timeout: float) -> list[dict]:
        """Block until something newer than rev exists (or timeout); return it."""
        deadline = time.monotonic() + timeout
        with self.cond:
            while self.rev <= rev and not STOP.is_set():
                left = deadline - time.monotonic()
                if left <= 0:
                    break
                self.cond.wait(min(left, 1.0))
        return self.since(rev) if rev <= self.rev else self.since(None)


BUS = Bus()
HANDLERS: dict = {}  # client message type -> fn(message, client_id, role) -> reply dict | None


def handle_client_message(message: dict, client: str, role: str = "owner") -> dict | None:
    kind = message.get("type")
    if not bind_client(client, role):
        return {"type": "error", "topic": "sys", "data": {"error": "Client id belongs to another session."}}
    if kind == "ping":
        return {"type": "pong", "topic": "sys", "data": {"t": message.get("data"), "rev": BUS.rev}}
    handler = HANDLERS.get(kind)
    return handler(message, client, role) if handler else None


def visible(messages: list[dict], role: str) -> list[dict]:
    """What a role may see on the bus: shared sessions only hear about the shared document."""
    if role == "owner":
        return messages
    shared = SHARE["doc"]
    out = []
    for message in messages:
        kind, data = message["type"], message["data"]
        if kind == "state":
            message = {**message, "data": {"docs": [d for d in data["docs"] if d["name"] == shared]}}
        elif kind in ("fs", "forward") and data.get("doc") != shared:
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


def fresh_state(name: str, main_tex: Path) -> dict:
    return {
        "name": name, "status": "idle", "ok": None, "seconds": None, "pages": None, "warnings": 0,
        "engine": None, "error": None, "errors": [], "finished": None, "version": pdf_version(main_tex),
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


def run_build(main_tex: Path, latexmk: str, force: bool) -> None:
    name = build.doc_name(main_tex)
    publish(name, status="building")
    entry, _ = build.build_safely(main_tex, latexmk, False, force)
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
                current = None
            if current is not None and current != base:
                raise ApiError("The file changed on disk.", 409, current=current)
        atomic_write(path, data)
        return {"path": rel, "version": version_of(path.stat())}


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


def forward(query: dict) -> tuple[str, dict]:
    """Returns (doc name, {page, x, y, w, h}) in PDF points from the page's top-left."""
    raw = Path(query.get("file", [""])[0])
    name = query.get("doc", [""])[0]
    here = DOCS[name].parent / raw if name in DOCS else raw
    candidates = [raw] if raw.is_absolute() else [here, build.ROOT_DIR / raw]
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
# Sharing: token links, roles, tunnels
# ---------------------------------------------------------------------------
#
# While sharing is on EVERY request needs a token, loopback included. A tunnel client connects to us from
# 127.0.0.1, so the peer address says nothing about who is asking, and the Host header is whatever the tunnel
# forwards (some rewrite it to localhost). Requiring the token for everyone is the only rule that cannot be
# bypassed through the tunnel; the owner's browser gets a third, private token as a cookie when sharing starts.

SHARE: dict = {
    "on": False, "tokens": {}, "doc": None, "provider": None, "public": None, "hosts": set(), "status": "off",
    "error": None, "tunnel": None, "port": 0,
}
SHARE_LOCK = threading.Lock()
ROLES = ("owner", "edit", "view")
SHELL_TOKENS = ("shell-escape", "enable-write18", "shell-restricted")
COMMAND_KEYS = {
    "pdflatex", "xelatex", "lualatex", "latex", "bibtex", "biber", "makeindex", "makeglossaries", "dvips", "dvipdf",
    "ps2pdf", "pdf_previewer", "dvi_previewer", "ps_previewer", "print_pdf_command", "e", "r",
}  # -key=value forms that name a program to run
RC_NAMES = {".latexmkrc", "latexmkrc", "build.toml"}  # configuration that can run programs
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
    """Flags that enable shell escape or let a document pick the programs latexmk runs."""
    low = arg.lower()
    if any(token in low for token in SHELL_TOKENS) or low in ("-e", "--e", "-r", "--r"):
        return True
    key = re.match(r"^--?([a-z0-9_]+)=", low)
    return bool(key) and key.group(1) in COMMAND_KEYS


_READ_SETTINGS = build.read_settings


def guarded_read_settings(main_tex: Path) -> dict:
    """build.read_settings, but while sharing: no shell escape, and no latexmk_args that run commands."""
    settings = _READ_SETTINGS(main_tex)
    if SHARE["on"]:
        bad = [arg for arg in settings["latexmk_args"] if unsafe_latexmk_arg(arg)]
        if bad:
            raise build.ConfigError(f"{main_tex.parent.name}/build.toml: {bad[0]!r} is not allowed while sharing")
        settings["shell_escape"] = False
    return settings


build.read_settings = guarded_read_settings


def share_enable(doc: str | None, provider: str, port: int) -> None:
    with SHARE_LOCK:
        SHARE.update(
            on=True, doc=doc, provider=provider, status="starting", error=None, public=None, port=port,
            tokens={"owner": new_token(), "edit": new_token(), "view": new_token()},
        )


def share_disable() -> None:
    tunnel = None
    with SHARE_LOCK:
        tunnel, SHARE["tunnel"] = SHARE["tunnel"], None
        SHARE.update(on=False, tokens={}, public=None, hosts=set(), status="off", error=None, provider=None)
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
        if self.proc:
            kill_tree(self.proc)


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
    share_enable(doc, chosen, port)

    def run() -> None:
        try:
            if chosen == "local":
                SHARE["status"] = "ready"
                return
            tunnel = Tunnel(chosen, port)
            SHARE["tunnel"] = tunnel
            url = tunnel.start()
            host = (urlsplit(url).hostname or "").lower()
            with SHARE_LOCK:
                if SHARE["tunnel"] is tunnel:
                    SHARE.update(public=url, status="ready")
                    SHARE["hosts"].add(host)
        except RuntimeError as exc:
            if SHARE["tunnel"] is not tunnel:
                return  # Sharing was stopped meanwhile.
            build.error(f"Share failed: {exc}")
            share_disable()
            SHARE["error"] = str(exc)

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
# ponytail: logs grow with the session and die with the room (no compaction); a leader-made snapshot would bound it.

ROOMS: dict[str, dict] = {}
CLIENTS: dict[str, dict] = {}
COLLAB_LOCK = threading.RLock()
ROOM_GRACE = float(os.environ.get("LP_ROOM_GRACE", "60"))  # an emptied room (and its log) lingers this long
CLIENT_TIMEOUT = 60.0
MAX_UPDATE = 2 * 1024 * 1024


def room_id(doc: str, path: str) -> str:
    return f"{doc}\n{path}"


def bind_client(client: str, role: str) -> bool:
    """Remember who a client id belongs to. False if the id was already taken by another role."""
    with COLLAB_LOCK:
        record = CLIENTS.setdefault(client, {"role": role, "rooms": set(), "name": None, "color": None, "path": None})
        record["seen"] = time.monotonic()
        return record["role"] == role


def room_leader(room: dict) -> str | None:
    return next((cid for cid, m in room["members"].items() if m["role"] in ("owner", "edit")), None)


def publish_leader(rid: str, room: dict) -> None:
    leader = room_leader(room)
    if leader != room.get("leader"):
        room["leader"] = leader
        BUS.publish("y-leader", {"room": rid, "leader": leader})


def presence() -> dict:
    with COLLAB_LOCK:
        users = [
            {"cid": cid, "name": c["name"], "color": c["color"], "role": c["role"], "path": c["path"]}
            for cid, c in CLIENTS.items() if c["name"]
        ]
    return {"users": sorted(users, key=lambda u: (u["name"] or "", u["cid"]))}


def publish_presence() -> None:
    BUS.publish("presence", presence(), topic="sys", ephemeral=True)


def check_room(doc, path, role: str) -> tuple[str, Path]:
    """Validate a room request: a served document, and for shared sessions the shared one."""
    if not isinstance(doc, str) or doc not in DOCS or (role != "owner" and doc != SHARE["doc"]):
        raise ApiError("Unknown document.", 404)
    if not isinstance(path, str) or file_kind(path) != "text":
        raise ApiError("Not a text file.", 415)
    return room_id(doc, path), resolve_in_doc(DOCS[doc].parent, path)


def reply_error(exc: ApiError) -> dict:
    return {"type": "error", "topic": "sys", "data": {"error": str(exc), "status": exc.status}}


def on_hello(message: dict, client: str, role: str) -> dict:
    data = message.get("data") or {}
    with COLLAB_LOCK:
        record = CLIENTS[client]
        record["name"] = str(data.get("name") or "Guest")[:40]
        color = str(data.get("color") or "")
        record["color"] = color if re.fullmatch(r"#[0-9a-fA-F]{6}", color) else "#0969da"
        path = data.get("path")
        record["path"] = path[:300] if isinstance(path, str) else None
    publish_presence()
    return {"type": "presence", "topic": "sys", "data": presence()}


def on_join(message: dict, client: str, role: str) -> dict:
    data = message.get("data") or {}
    try:
        rid, path = check_room(data.get("doc"), data.get("path"), role)
    except ApiError as exc:
        return reply_error(exc)
    epoch = data.get("epoch") if isinstance(data.get("epoch"), str) and role != "view" else None
    with COLLAB_LOCK:
        room = ROOMS.get(rid)
        conflict = False
        if room is None:
            room = ROOMS[rid] = {
                "epoch": epoch or secrets.token_hex(8), "log": [], "aware": {}, "members": {}, "gone_at": None,
                "claimed": bool(epoch), "text": "", "eol": "\n", "leader": None,
            }
            if not epoch:  # The seed every first joiner builds identically; kept in the room so it stays consistent.
                try:
                    found = read_text_file(DOCS[data["doc"]].parent, data["path"])
                    room["text"], room["eol"] = found["text"], found["eol"]
                except ApiError:
                    pass
        elif epoch and epoch != room["epoch"]:
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
        }
    return {"type": "y-state", "topic": "sys", "data": reply}


def on_update(message: dict, client: str, role: str) -> dict | None:
    data = message.get("data") or {}
    rid, update = data.get("room"), data.get("u")
    if role == "view":
        return reply_error(ApiError("This link is view-only.", 403))
    if not isinstance(update, str) or len(update) > MAX_UPDATE:
        return reply_error(ApiError("Bad update.", 413))
    with COLLAB_LOCK:
        room = ROOMS.get(rid)
        if room is None or client not in room["members"]:
            return reply_error(ApiError("Join the room first.", 409))
        if len(room["log"]) <= 3 and update in room["log"]:
            return None  # Two clients seeding the same file produce the same bytes.
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
}
MAX_BODY = 8 * 1024 * 1024
POLL_HOLD = 25.0


CDNS = "https://esm.sh https://cdnjs.cloudflare.com https://cdn.jsdelivr.net"
READ_API = {
    "/api/files", "/api/file", "/api/raw", "/api/image", "/api/outline", "/api/refs", "/api/lint", "/synctex/edit",
}


def csp(page: bytes, host: str) -> str:
    """Only our own files, the pinned CDNs and this origin's sockets; the inline import map is allowed by hash."""
    block = re.search(rb'<script type="importmap">(.*?)</script>', page, re.S)
    inline = f" 'sha256-{base64.b64encode(hashlib.sha256(block.group(1)).digest()).decode()}'" if block else ""
    return "; ".join([
        "default-src 'none'", f"script-src 'self'{inline} {CDNS}", f"style-src 'self' 'unsafe-inline' {CDNS}",
        "img-src 'self' data: blob:", f"font-src 'self' data: {CDNS}", f"worker-src blob: {CDNS}",
        f"connect-src 'self' ws://{host} wss://{host} {CDNS}", "base-uri 'none'", "form-action 'self'",
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
    role = "owner"
    token = None

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
            if self.command == "GET" and url.path == "/" and SHARE["on"] and "token" in query:
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
        elif url.path == "/api/send":  # Long-poll transport: client -> server.
            data = self.body()
            client = str(query.get("cid", ["?"])[0])[:64]
            replies = []
            for message in data.get("messages", []):
                reply = handle_client_message(message, client, self.role) if isinstance(message, dict) else None
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
            self.json({"pdfjs": PDFJS, "editor": SETTINGS["editor"], "role": self.role, "collab": True})
        elif path == "/api/share":
            self.json(share_info())
        elif path == "/ws":
            self.websocket(query)
        elif path == "/api/poll":
            self.poll(query)
        elif path == "/events":
            self.events()
        elif path.startswith(("/pdf/", "/log/")):
            self.file(path)
        elif path == "/api/files":
            self.json({"files": list_files(self.doc_root(query))})
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
        elif path == "/synctex/edit":
            name = query.get("doc", [""])[0]
            if name not in DOCS:
                raise SynctexError("Unknown document.")
            self.json(inverse(DOCS[name], query))
        elif path == "/forward":
            name, box = forward(query)
            if self.role != "owner" and name != SHARE["doc"]:
                raise ApiError("Not part of the shared document.", 403)
            box["doc"] = name
            if "quiet" not in query:
                broadcast("forward", box)
            self.json(box)
        else:
            self.reply(404, b"Not found", "text/plain")

    def static(self, name: str) -> None:
        target = UI_DIR / name
        if "/" in name or "\\" in name or not target.is_file() or target.suffix not in STATIC_TYPES:
            self.reply(404, b"Not found", "text/plain")
            return
        self.reply(200, target.read_bytes(), STATIC_TYPES[target.suffix])

    def image(self, path: Path) -> None:
        self.reply(200, path.read_bytes(), IMAGE_TYPES[path.suffix.lower()], {"Content-Security-Policy": "sandbox"})

    def raw(self, query: dict) -> None:
        rel = query.get("path", [""])[0]
        path = resolve_in_doc(self.doc_root(query), rel)
        if file_kind(rel) != "image" or not path.is_file():
            raise ApiError("Not an image.", 415)
        self.image(path)

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

    # --- transports -------------------------------------------------------

    def poll(self, query: dict) -> None:
        """Long-poll: hold up to POLL_HOLD seconds for messages newer than ?since=."""
        try:
            since = int(query["since"][0]) if "since" in query else None
        except ValueError:
            since = None
        if not bind_client(str(query.get("cid", ["?"])[0])[:64], self.role):
            raise ApiError("Client id belongs to another session.", 403)
        events = BUS.wait(since, POLL_HOLD) if since is not None else BUS.since(None)
        bind_client(str(query.get("cid", ["?"])[0])[:64], self.role)
        # rev is where the client resumes: filtered-out messages still advance it, so it cannot spin on them.
        self.json({"rev": events[-1]["rev"] if events else since, "events": visible(events, self.role)})

    def websocket(self, query: dict) -> None:
        key = self.headers.get("Sec-WebSocket-Key")
        if "websocket" not in self.headers.get("Upgrade", "").lower() or not key or not self.same_origin():
            self.reply(400, b"Expected a WebSocket upgrade", "text/plain")
            return
        client = str(query.get("cid", ["?"])[0])[:64]
        if not bind_client(client, self.role):
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
        role, token = self.role, self.token
        send_lock = threading.Lock()
        alive = threading.Event()

        def send(opcode: int, payload: bytes) -> None:
            with send_lock:
                self.wfile.write(ws_encode(opcode, payload))
                self.wfile.flush()

        def revoked() -> bool:
            return role_for_token(token) != role  # Tokens regenerated or sharing stopped.

        def pump() -> None:  # Bus -> socket.
            cursor = since
            try:
                while not alive.is_set() and not STOP.is_set() and not revoked():
                    messages = BUS.since(cursor) if cursor is None else BUS.wait(cursor, 5.0)
                    if alive.is_set():
                        break
                    bind_client(client, role)  # Keeps the client "seen" while the socket idles.
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
                    reply = handle_client_message(message, client, role) if isinstance(message, dict) else None
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
    args = parser.parse_args()
    signal.signal(signal.SIGTERM, lambda *_: STOP.set())  # Stop like Ctrl+C would: tunnels must not be orphaned.
    if args.share_selftest:
        return selftest(args.share or "auto", args.port if args.port != 8000 else 0)

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

    latexmk = build.check_latex()
    SETTINGS["editor"] = args.editor
    SETTINGS["check_host"] = args.host in LOOPBACK_HOSTS | {"::1"}
    if not SETTINGS["check_host"]:
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
