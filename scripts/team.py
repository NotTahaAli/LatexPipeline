"""
Team features of the editor (serve.py): document locks, linked files, version zips and access requests. Stdlib only,
Python 3.9; imported by serve.py only. Locks and links are small JSON files in the document's history folder (beside
review.json, outside the document directory); access requests live in memory and end with the server.
"""

from __future__ import annotations

import hashlib
import io
import json
import os
import secrets
import tempfile
import threading
import time
import urllib.error
import urllib.request
import zipfile
from pathlib import Path
from urllib.parse import urlsplit

LOCK_FILE = "lock.json"
LINKS_FILE = "linked.json"
MAX_LINKS = 100  # linked files per document
FETCH_TIMEOUT = 20.0
MAX_REQUESTS = 50  # open access requests at once
MAX_NOTE = 300
REQUEST_TTL = 7 * 86400.0
_LOCK = threading.Lock()


class TeamError(Exception):
    def __init__(self, message: str, status: int = 400) -> None:
        super().__init__(message)
        self.status = status


def read_json(path: Path, default):
    try:
        data = json.loads(path.read_text("utf-8"))
    except FileNotFoundError:
        return default
    except (OSError, ValueError) as exc:
        raise TeamError(f"Could not read {path.name}: {exc}", 500)
    return data if isinstance(data, type(default)) else default


def write_json(path: Path, data) -> None:
    """Atomically; None deletes the file."""
    if data is None:
        try:
            path.unlink()
        except FileNotFoundError:
            pass
        return
    path.parent.mkdir(parents=True, exist_ok=True)
    handle, tmp = tempfile.mkstemp(dir=path.parent, prefix=f".{path.name}.")
    try:
        with os.fdopen(handle, "w", encoding="utf-8") as out:
            json.dump(data, out)
        os.replace(tmp, path)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


# --- Locks ------------------------------------------------------------------------------------------------------

def read_lock(folder: Path) -> dict | None:
    """{"by": name, "time": t} while the document is locked, else None. An unreadable lock counts as locked."""
    try:
        data = read_json(folder / LOCK_FILE, {})
    except TeamError:
        return {"by": "?", "time": 0}
    return data or None


def write_lock(folder: Path, by: str | None) -> dict | None:
    info = {"by": by, "time": time.time()} if by else None
    with _LOCK:
        write_json(folder / LOCK_FILE, info)
    return info


# --- Linked files -----------------------------------------------------------------------------------------------

def read_links(folder: Path) -> dict:
    """{path: {"url": https URL} or {"doc": name, "path": rel}, plus "fetched" and "by"}."""
    return read_json(folder / LINKS_FILE, {})


def write_links(folder: Path, links: dict) -> None:
    with _LOCK:
        write_json(folder / LINKS_FILE, links or None)


def check_url(url) -> str:
    if not isinstance(url, str) or len(url) > 2000:
        raise TeamError("Give an https:// address.")
    parts = urlsplit(url.strip())
    if parts.scheme != "https" or not parts.hostname or parts.username or parts.password:
        raise TeamError("Linked files come from https:// addresses only (no user name or password in them).")
    return url.strip()


class _HttpsRedirects(urllib.request.HTTPRedirectHandler):
    """Follow a redirect only to another https:// address."""

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        if urlsplit(newurl).scheme != "https":
            raise urllib.error.HTTPError(newurl, code, "redirected away from https", headers, fp)
        return super().redirect_request(req, fp, code, msg, headers, newurl)


def http_get(url: str, cap: int) -> bytes:
    """GET an https:// URL, at most cap bytes. The only network call here (tests mock it)."""
    opener = urllib.request.build_opener(_HttpsRedirects)
    request = urllib.request.Request(check_url(url), headers={"User-Agent": "LaTeX-Studio linked file"})
    try:
        with opener.open(request, timeout=FETCH_TIMEOUT) as response:
            data = response.read(cap + 1)
    except urllib.error.HTTPError as exc:
        raise TeamError(f"The server answered {exc.code}." if exc.code else "Could not fetch the file.", 502)
    except (urllib.error.URLError, OSError, ValueError) as exc:
        raise TeamError(f"Could not fetch the file: {getattr(exc, 'reason', exc)}", 502)
    if len(data) > cap:
        raise TeamError(f"The file is larger than {cap // (1024 * 1024)} MB.", 413)
    return data


# --- Version zips -----------------------------------------------------------------------------------------------

def zip_bytes(files: dict) -> bytes:
    """{path: bytes} as a zip archive, in path order."""
    out = io.BytesIO()
    with zipfile.ZipFile(out, "w", zipfile.ZIP_DEFLATED) as archive:
        for path in sorted(files):
            archive.writestr(path, files[path])
    return out.getvalue()


# --- Access requests (local sharing) ---------------------------------------------------------------------------

def ticket_hash(ticket: str) -> str:
    return hashlib.sha256(ticket.encode("utf-8", "replace")).hexdigest()


class Requests:
    """Open requests for more access from people on a view or review link. The requester keeps a ticket (a secret
    only its browser knows) and asks with it whether it was granted; the owner sees name, role and note."""

    def __init__(self) -> None:
        self.items: list = []

    def _prune(self, now: float) -> None:
        self.items = [r for r in self.items if now - r["time"] < REQUEST_TTL]

    def add(self, name: str, have: str, want, note, now: float | None = None) -> str:
        now = time.time() if now is None else now
        if want not in ("edit", "review") or want == have:
            raise TeamError("Ask for the edit or the review role.")
        if not isinstance(note, str) or len(note) > MAX_NOTE or "\0" in note:
            raise TeamError(f"Keep the note under {MAX_NOTE} characters.")
        with _LOCK:
            self._prune(now)
            if sum(r["state"] == "pending" for r in self.items) >= MAX_REQUESTS:
                raise TeamError("The owner has many open requests already; try again later.", 429)
            ticket = secrets.token_urlsafe(24)
            self.items.append({"id": secrets.token_hex(6), "ticket": ticket_hash(ticket), "name": name, "have": have,
                               "want": want, "note": note.strip(), "time": now, "state": "pending", "url": None})
        return ticket

    def status(self, ticket) -> dict:
        found = next((r for r in self.items if isinstance(ticket, str) and r["ticket"] == ticket_hash(ticket)), None)
        if found is None:
            raise TeamError("No such request.", 404)
        return {"state": found["state"], "url": found["url"], "want": found["want"]}

    def pending(self) -> list:
        with _LOCK:
            self._prune(time.time())
            return [{k: r[k] for k in ("id", "name", "have", "want", "note", "time")}
                    for r in self.items if r["state"] == "pending"]

    def take(self, rid) -> dict:
        """The pending request rid, for the owner to decide."""
        found = next((r for r in self.items if r["id"] == rid and r["state"] == "pending"), None)
        if found is None:
            raise TeamError("That request was already decided or is gone.", 404)
        return found

    def decide(self, rid, url: str | None) -> None:
        with _LOCK:
            found = self.take(rid)
            found["state"], found["url"] = ("granted", url) if url else ("denied", None)
