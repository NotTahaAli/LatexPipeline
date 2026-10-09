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
import base64
import collections
import hashlib
import json
import os
import posixpath
import re
import shutil
import struct
import subprocess
import sys
import tempfile
import threading
import time
import webbrowser
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
    """Append-only message log with a revision counter; readers resume from a revision."""

    def __init__(self, keep: int = 500) -> None:
        self.cond = threading.Condition()
        self.rev = 0
        self.log: collections.deque = collections.deque(maxlen=keep)

    def publish(self, type_: str, data, topic: str = "doc") -> int:
        with self.cond:
            self.rev += 1
            self.log.append({"rev": self.rev, "topic": topic, "type": type_, "data": data})
            self.cond.notify_all()
            return self.rev

    def since(self, rev: int | None) -> list[dict]:
        """Messages after rev. None, or a rev older than the log keeps, yields one fresh state snapshot."""
        with self.cond:
            gap = rev is None or rev > self.rev or (self.log and self.log[0]["rev"] > rev + 1)
            if gap:
                return [{"rev": self.rev, "topic": "doc", "type": "state", "data": snapshot(), "resync": True}]
            return [m for m in self.log if m["rev"] > rev]

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
HANDLERS: dict = {}  # client message type -> fn(message, client_id) -> reply dict | None (phase 2 plugs in here)


def handle_client_message(message: dict, client: str) -> dict | None:
    kind = message.get("type")
    if kind == "ping":
        return {"type": "pong", "topic": "sys", "data": {"t": message.get("data"), "rev": BUS.rev}}
    handler = HANDLERS.get(kind)
    return handler(message, client) if handler else None


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
# HTTP
# ---------------------------------------------------------------------------

STATIC_TYPES = {
    ".html": "text/html; charset=utf-8", ".js": "text/javascript; charset=utf-8", ".css": "text/css; charset=utf-8",
}
MAX_BODY = 8 * 1024 * 1024
POLL_HOLD = 25.0


def health() -> dict:
    return {
        "ok": True, "rev": BUS.rev, "uptime": round(time.time() - STARTED, 1), "docs": sorted(DOCS),
        "synctex": shutil.which("synctex") is not None, "texcount": shutil.which("texcount") is not None,
    }


class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.0"

    def log_message(self, *args) -> None:  # Quiet: build output is the interesting part.
        pass

    def reply(self, status: int, body: bytes, kind: str, extra: dict | None = None) -> None:
        self.send_response(status)
        self.send_header("Content-Type", kind)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        for key, value in (extra or {}).items():
            self.send_header(key, value)
        self.end_headers()
        self.wfile.write(body)

    def json(self, data, status: int = 200) -> None:
        self.reply(status, json.dumps(data).encode(), "application/json")

    def allowed(self) -> bool:
        """Refuse foreign Host headers (DNS rebinding) while bound to loopback."""
        host = self.headers.get("Host", "")
        host = host if host.endswith("]") else host.rsplit(":", 1)[0]
        if SETTINGS["check_host"] and host not in LOOPBACK_HOSTS:
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
                reply = handle_client_message(message, client) if isinstance(message, dict) else None
                if reply:
                    replies.append(reply)
            self.json({"replies": replies, "rev": BUS.rev})
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
            self.json(health())
        elif path == "/api/config":
            self.json({"pdfjs": PDFJS, "editor": SETTINGS["editor"]})
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
        events = BUS.wait(since, POLL_HOLD) if since is not None else BUS.since(None)
        self.json({"rev": BUS.rev, "events": events})

    def websocket(self, query: dict) -> None:
        key = self.headers.get("Sec-WebSocket-Key")
        if "websocket" not in self.headers.get("Upgrade", "").lower() or not key or not self.same_origin():
            self.reply(400, b"Expected a WebSocket upgrade", "text/plain")
            return
        self.send_response(101)
        self.send_header("Upgrade", "websocket")
        self.send_header("Connection", "Upgrade")
        self.send_header("Sec-WebSocket-Accept", ws_accept(key))
        self.end_headers()
        self.wfile.flush()

        client = str(query.get("cid", ["?"])[0])[:64]
        try:
            since = int(query["since"][0]) if "since" in query else None
        except ValueError:
            since = None
        send_lock = threading.Lock()
        alive = threading.Event()

        def send(opcode: int, payload: bytes) -> None:
            with send_lock:
                self.wfile.write(ws_encode(opcode, payload))
                self.wfile.flush()

        def pump() -> None:  # Bus -> socket.
            cursor = since
            try:
                while not alive.is_set() and not STOP.is_set():
                    messages = BUS.since(cursor) if cursor is None else BUS.wait(cursor, 5.0)
                    for message in messages:
                        send(0x1, json.dumps(message).encode())
                        cursor = message["rev"]
                    if cursor is None:
                        cursor = BUS.rev
            except OSError:
                pass
            finally:
                alive.set()

        threading.Thread(target=pump, daemon=True).start()
        partial: dict = {}
        try:
            while not alive.is_set():
                frame = ws_read(self.rfile.read, partial)  # Socket -> bus handlers.
                if frame is None or frame[0] == 0x8:
                    break
                opcode, payload = frame
                if opcode == 0x9:
                    send(0xA, payload)
                elif opcode in (0x1, 0x2):
                    try:
                        message = json.loads(payload)
                    except ValueError:
                        continue
                    reply = handle_client_message(message, client) if isinstance(message, dict) else None
                    if reply:
                        send(0x1, json.dumps(reply).encode())
        except (OSError, ValueError, struct.error):
            pass
        finally:
            alive.set()
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
    args = parser.parse_args()

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
    address = f"http://{'localhost' if args.host == '127.0.0.1' else args.host}:{port}/"
    first = build.select_documents(documents, args.docs)
    if first:
        address += "#" + build.doc_name(first[0]).replace(" ", "%20")
    build.info(f"Serving {address}  (Ctrl+C to stop)")

    threading.Thread(target=watcher, args=(latexmk, args.docs), daemon=True).start()
    threading.Thread(target=fs_watcher, daemon=True).start()
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
