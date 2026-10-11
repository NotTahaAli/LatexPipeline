"""
Personal reference library: one store of BibTeX entries per person, shared by all their documents (stdlib only,
Python 3.9; imported by serve.py and host.py, never by build.py or CI).

Locally the owner's library is the 0600 file LIBRARY_FILE in the user's config folder (outside every project, like
ai.json); hosted, each account's library is one row of host.db (table library). Both keep the same JSON
({"version": 1, "entries": [{"id", "type", "key", "fields", "added"}]}) and go through apply() and listing(), so the
rules are the same: entries are checked like the References panel's (bibfix.check_entry), BibTeX and RIS are parsed
here (bibfix.parse, bibfix.from_ris), a DOI is resolved by the editor through the document's /api/bib/import first
(Crossref only from there), duplicates (same key, DOI, or title and year) are flagged, never merged silently. Nothing
here touches a document: the References panel inserts an entry into a .bib through /api/bib/edit and its own editor.
"""

from __future__ import annotations

import json
import os
import re
import secrets
import threading
import time
from pathlib import Path

import bibfix

LIBRARY_FILE = Path(os.environ.get("XDG_CONFIG_HOME") or Path.home() / ".config") / "latex-pipeline" / "library.json"
MAX_ENTRIES = 2000
MAX_STORE = 3_000_000  # characters of the stored JSON
MAX_ADD = 1_000_000  # characters of BibTeX or RIS in one add
MAX_IDS = 500  # entries named in one request
LOCK = threading.Lock()  # one writer at a time in this process (serve.py); host.py uses a database transaction


class LibraryError(Exception):
    def __init__(self, message: str, status: int = 400) -> None:
        super().__init__(message)
        self.status = status


def empty() -> dict:
    return {"version": 1, "entries": []}


def loads(text: str | None) -> dict:
    """The stored JSON, keeping only well-formed entries (a hand-edited or damaged store never breaks a listing)."""
    try:
        found = json.loads(text) if text else empty()
    except ValueError:
        found = empty()
    entries = found.get("entries") if isinstance(found, dict) and isinstance(found.get("entries"), list) else []
    out = []
    for item in entries[:MAX_ENTRIES]:
        if not (isinstance(item, dict) and re.fullmatch(r"[0-9a-f]{16}", str(item.get("id")))):
            continue
        try:
            out.append({"id": item["id"], **clean(item), "added": float(item.get("added") or 0)})
        except (LibraryError, TypeError, ValueError):
            continue
    return {"version": 1, "entries": out}


def dumps(lib: dict) -> str:
    text = json.dumps(lib, ensure_ascii=False)
    if len(text) > MAX_STORE:
        raise LibraryError(f"Your library is full ({MAX_STORE // 1_000_000} MB). Delete some entries first.", 413)
    return text


def clean(entry) -> dict:
    """{"type", "key", "fields"} that format into a valid entry; raises LibraryError."""
    if not isinstance(entry, dict):
        raise LibraryError("Bad entry.")
    try:
        fields = bibfix.check_entry(entry.get("type"), entry.get("key"), entry.get("fields"))
    except ValueError as exc:
        raise LibraryError(str(exc))
    if not fields:
        raise LibraryError(f"{entry.get('key')}: the entry has no fields.")
    return {"type": entry["type"].lower(), "key": entry["key"], "fields": fields}


def parse_text(text) -> list[dict]:
    """Entries from pasted BibTeX or RIS (Mendeley, EndNote, publishers' exports)."""
    if not isinstance(text, str) or len(text) > MAX_ADD:
        raise LibraryError(f"Paste BibTeX or RIS (up to {MAX_ADD // 1_000_000} MB).", 413)
    if not re.search(r"@[ \t]*\w+[ \t\r\n]*[({]", text) and re.search(r"^\s*TY  ?-", text, re.M):
        return bibfix.from_ris(text)
    out = []
    for entry in bibfix.parse(text):
        fields: dict[str, str] = {}
        for field in entry.fields:
            fields.setdefault(field.name, field.value)
        out.append({"type": entry.kind, "key": entry.key, "fields": fields})
    return out


def _signatures(entry: dict) -> set:
    fields = entry["fields"]
    found = {"key:" + entry["key"].casefold()}
    doi = bibfix.clean_doi(fields.get("doi", "")).lower()
    if doi:
        found.add("doi:" + doi)
    title = bibfix._norm(fields.get("title", ""))
    if len(title) >= 12:
        found.add("title:" + title + ":" + (re.search(r"\d{4}", fields.get("year", fields.get("date", ""))) or [""])[0])
    return found


def listing(lib: dict) -> dict:
    """{"entries": [... with "bibtex" and "duplicates" (ids of entries with the same key, DOI, or title and year)]}."""
    seen: dict[str, list[str]] = {}
    for entry in lib["entries"]:
        for sig in _signatures(entry):
            seen.setdefault(sig, []).append(entry["id"])
    out = []
    for entry in lib["entries"]:
        twins = {other for sig in _signatures(entry) for other in seen[sig] if other != entry["id"]}
        out.append({**entry, "bibtex": bibfix.format_entry(entry["type"], entry["key"], entry["fields"]),
                    "duplicates": sorted(twins)})
    return {"entries": out, "max": MAX_ENTRIES}


def _ids(data: dict) -> set:
    ids = data.get("ids")
    if not (isinstance(ids, list) and len(ids) <= MAX_IDS and all(isinstance(i, str) for i in ids)):
        raise LibraryError("Name the entries by id.")
    return set(ids)


def apply(lib: dict, data: dict) -> tuple[dict, dict]:
    """(new library, reply) for one change: {"op": "add", "entries": [...]} or {"op": "add", "text": BibTeX/RIS},
    {"op": "delete", "ids": [...]}. The reply lists what was added (ids) and what was skipped and why."""
    op = data.get("op")
    if op == "add":
        raw = parse_text(data["text"]) if "text" in data else data.get("entries")
        if not isinstance(raw, list) or not raw:
            raise LibraryError("No complete entry found (check braces, quotes and commas).")
        if len(raw) > MAX_IDS:
            raise LibraryError(f"Add at most {MAX_IDS} entries at once.", 413)
        entries, added, skipped = list(lib["entries"]), [], []
        for item in raw:
            try:
                entry = clean(item)
            except LibraryError as exc:
                skipped.append(str(exc))
                continue
            if len(entries) >= MAX_ENTRIES:
                skipped.append(f"{entry['key']}: your library holds at most {MAX_ENTRIES} entries.")
                continue
            entries.append({"id": secrets.token_hex(8), **entry, "added": time.time()})
            added.append(entries[-1]["id"])
        new = {"version": 1, "entries": entries}
        dumps(new)  # the size cap, before anything is stored
        return new, {"added": added, "skipped": skipped}
    if op == "delete":
        ids = _ids(data)
        return {"version": 1, "entries": [e for e in lib["entries"] if e["id"] not in ids]}, {"deleted": len(ids)}
    raise LibraryError("Unknown operation.")


# ---------------------------------------------------------------------------
# Local store (serve.py): the owner's file, readable by this user only
# ---------------------------------------------------------------------------

def read(path: Path | None = None) -> dict:
    try:
        return loads((path or LIBRARY_FILE).read_text(encoding="utf-8"))
    except OSError:
        return empty()


def write(lib: dict, path: Path | None = None) -> None:
    """0600 in a 0700 folder, replaced atomically."""
    path = path or LIBRARY_FILE
    text = dumps(lib)
    path.parent.mkdir(parents=True, exist_ok=True)
    if os.name != "nt":
        os.chmod(path.parent, 0o700)
    tmp = path.with_name(path.name + ".tmp")
    fd = os.open(str(tmp), os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as out:
        out.write(text)
    if os.name != "nt":
        os.chmod(tmp, 0o600)
    os.replace(tmp, path)


def change(data: dict, path: Path | None = None) -> dict:
    """One change to the local library, then its listing with the reply's fields."""
    with LOCK:
        lib, reply = apply(read(path), data)
        write(lib, path)
    return {**listing(lib), **reply}
