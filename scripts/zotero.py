"""Sync a .bib with a Zotero library: fetch BibTeX, compare with the .bib text the editor holds, apply a chosen subset.

Stdlib-only (imports `bibfix` for parsing and splicing, `grammar` for the rate limiter). Network use is `get` and
nothing else; callers opt in (the owner's "Sync from Zotero" click), tests replace `get`/`_opener`.

The API key lives outside the project: `ZOTERO_API_KEY`, or the 0600 file `config_path()` in the user's config
dir. It is sent only in the `Zotero-API-Key` header (never in a URL) and `info()`/errors never contain it.
"""

from __future__ import annotations

import difflib
import json
import os
import re
import sys
import threading
import urllib.error
import urllib.request
from pathlib import Path

import bibfix
import grammar

API = "https://api.zotero.org"
LOCAL = "http://127.0.0.1:23119/better-bibtex/export/library?/1/library."  # Better BibTeX, Zotero desktop
USER_AGENT = "LatexPipeline (https://github.com/NotTahaAli/LatexPipeline)"
LIMITER = grammar.Limiter(requests=30, size=1, window=60.0)  # one hit per page
PAGE = 100  # Zotero's maximum for non-JSON formats
MAX_PAGES = 200
MAX_BODY = 8_000_000
MAX_TOTAL = 20_000_000
MAX_OPS = 5000
FORMATS = ("bibtex", "biblatex")
IGNORE = {"file"}  # Attachment paths on the Zotero machine
CACHE: dict = {}  # (mode, base, format) -> {"version", "text"}; small, in memory, never holds the key
CACHE_LOCK = threading.Lock()


class ZoteroError(Exception):
    def __init__(self, message: str, status: int = 502) -> None:
        super().__init__(message)
        self.status = status


# ---------------------------------------------------------------------------
# Settings: the key stays out of the project, out of replies, out of the bus
# ---------------------------------------------------------------------------

def config_path() -> Path:
    if sys.platform == "win32":
        base = Path(os.environ.get("APPDATA") or Path.home() / "AppData" / "Roaming")
    elif sys.platform == "darwin":
        base = Path.home() / "Library" / "Application Support"
    else:
        base = Path(os.environ.get("XDG_CONFIG_HOME") or Path.home() / ".config")
    return base / "latex-pipeline" / "zotero.json"


def _read() -> dict:
    try:
        data = json.loads(config_path().read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


def _key(cfg: dict) -> str:
    return os.environ.get("ZOTERO_API_KEY", "").strip() or str(cfg.get("key") or "")


def info() -> dict:
    """What the settings dialog may see. Never the key, only whether there is one."""
    cfg, env = _read(), bool(os.environ.get("ZOTERO_API_KEY", "").strip())
    mode = "local" if cfg.get("mode") == "local" else "web"
    out = {"mode": mode, "library_type": "groups" if cfg.get("library_type") == "groups" else "users",
           "library_id": str(cfg.get("library_id") or ""), "collection": str(cfg.get("collection") or ""),
           "format": cfg.get("format") if cfg.get("format") in FORMATS else "bibtex",
           "has_key": env or bool(cfg.get("key")), "key_from_env": env}
    out["configured"] = mode == "local" or bool(out["library_id"] and out["has_key"])
    return out


def save_settings(data: dict) -> dict:
    """Validate and write the settings file (mode 0600). `key` "" keeps the stored key; `clear_key` removes it."""
    cfg = _read()
    mode, kind = data.get("mode", "web"), data.get("library_type", "users")
    lib, coll = str(data.get("library_id") or "").strip(), str(data.get("collection") or "").strip()
    fmt = data.get("format", "bibtex")
    if mode not in ("web", "local") or kind not in ("users", "groups") or fmt not in FORMATS:
        raise ZoteroError("Unknown mode, library type or format.", 400)
    if lib and not re.fullmatch(r"\d{1,12}", lib):
        raise ZoteroError("The library ID is the number in your Zotero profile or group URL.", 400)
    if coll and not re.fullmatch(r"[A-Za-z0-9]{8}", coll):
        raise ZoteroError("A collection key is 8 letters or digits (the end of the collection's web address).", 400)
    key = data.get("key")
    if key not in (None, "") and not (isinstance(key, str) and re.fullmatch(r"[A-Za-z0-9]{10,64}", key.strip())):
        raise ZoteroError("That does not look like a Zotero API key (letters and digits).", 400)
    cfg.update(mode=mode, library_type=kind, library_id=lib, collection=coll, format=fmt)
    if data.get("clear_key"):
        cfg.pop("key", None)
    if key:
        cfg["key"] = key.strip()
    path = config_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tmp")
    try:
        tmp.unlink()
    except OSError:
        pass
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as handle:
        json.dump(cfg, handle)
    os.replace(tmp, path)
    with CACHE_LOCK:
        CACHE.clear()
    return info()


# ---------------------------------------------------------------------------
# HTTP
# ---------------------------------------------------------------------------

class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *args, **kwargs):  # A redirect would carry the key header to another host.
        return None


def _opener(local: bool):
    handlers = [_NoRedirect]
    if local:
        handlers.append(urllib.request.ProxyHandler({}))  # 127.0.0.1 must never go through a proxy
    return urllib.request.build_opener(*handlers)


def get(url: str, headers: dict, local: bool = False, timeout: float = 20.0) -> tuple:
    """(status, lower-case headers, body bytes); 304 is a normal answer, other failures raise ZoteroError.

    Throttled. Errors name the status only: never the URL or a header."""
    try:
        LIMITER.acquire(0, 0.0)
    except grammar.GrammarError:
        raise ZoteroError("Too many Zotero requests; wait a moment.", 429)
    request = urllib.request.Request(url, headers={"User-Agent": USER_AGENT, **headers})
    try:
        with _opener(local).open(request, timeout=timeout) as reply:
            return reply.status, {k.lower(): v for k, v in reply.headers.items()}, reply.read(MAX_BODY + 1)
    except urllib.error.HTTPError as error:
        head = {k.lower(): v for k, v in error.headers.items()} if error.headers else {}
        if error.code == 304:
            return 304, head, b""
        wait = head.get("retry-after") or head.get("backoff")
        raise ZoteroError({
            403: "Zotero refused the API key (is it valid, with read access to this library?).",
            404: "Zotero does not know this library or collection (check the ID and type).",
            429: f"Zotero asks to wait {wait} s before the next request." if wait else "Zotero is rate limiting; wait.",
            503: f"Zotero is busy; try again in {wait} s." if wait else "Zotero is busy; try again shortly.",
        }.get(error.code, f"Zotero answered HTTP {error.code}."), 429 if error.code in (429, 503) else 502)
    except (OSError, ValueError):
        raise ZoteroError("Better BibTeX is not answering on 127.0.0.1:23119 (is Zotero open?)." if local
                          else "Zotero is not reachable.")


def fetch(cfg: dict, local_ok: bool) -> tuple:
    """(bibtex text, version, from_cache). An unchanged library answers 304 and the cached text is reused."""
    if cfg["mode"] == "local":
        if not local_ok:
            raise ZoteroError("Better BibTeX sync is off while sharing: use the web API.", 409)
        status, _, body = get(LOCAL + cfg["format"], {}, local=True)
        if len(body) > MAX_TOTAL:
            raise ZoteroError("The export is too large.", 413)
        return body.decode("utf-8", "replace"), "", False
    if not cfg["library_id"] or not cfg["key"]:
        raise ZoteroError("Set the library ID and API key first (Zotero settings).", 400)
    scope = f"collections/{cfg['collection']}/" if cfg["collection"] else ""
    base = f"{API}/{cfg['library_type']}/{cfg['library_id']}/{scope}items/top"
    cache_key = ("web", base, cfg["format"])
    with CACHE_LOCK:
        cached = CACHE.get(cache_key)
    headers = {"Zotero-API-Version": "3", "Zotero-API-Key": cfg["key"]}
    parts, version, start = [], None, 0
    for _ in range(MAX_PAGES):
        page_headers = dict(headers)
        if start == 0 and cached:
            page_headers["If-Modified-Since-Version"] = cached["version"]
        url = f"{base}?format={cfg['format']}&itemType=-attachment&limit={PAGE}&start={start}"
        status, head, body = get(url, page_headers)
        if status == 304 and cached:
            return cached["text"], cached["version"], True
        if len(body) > MAX_BODY:
            raise ZoteroError("Zotero sent an oversized page.", 413)
        seen = head.get("last-modified-version", "")
        if version is not None and seen != version:
            raise ZoteroError("The library changed while it was being read; sync again.", 409)
        version = seen
        parts.append(body.decode("utf-8", "replace"))
        start += PAGE
        total = head.get("total-results", "")
        more = start < int(total) if total.isdigit() else 'rel="next"' in head.get("link", "")
        if not more or not body.strip():
            break
        if sum(map(len, parts)) > MAX_TOTAL:
            raise ZoteroError("The library is too large to sync in one go; pick a collection.", 413)
    else:
        raise ZoteroError("The library is too large to sync in one go; pick a collection.", 413)
    text = "\n".join(parts)
    with CACHE_LOCK:
        if version:
            CACHE[cache_key] = {"version": version, "text": text}
        while len(CACHE) > 4:
            CACHE.pop(next(iter(CACHE)))
    return text, version or "", False


# ---------------------------------------------------------------------------
# Comparing
# ---------------------------------------------------------------------------

def _entries(text: str) -> list[dict]:
    out = []
    for entry in bibfix.parse(text):
        fields: dict[str, str] = {}
        for field in entry.fields:
            fields.setdefault(field.name, field.value)
        out.append({"type": entry.kind, "key": entry.key, "fields": fields})
    return out


def _flat(value: str) -> str:
    return re.sub(r"\s+", " ", value.replace("{", "").replace("}", "")).strip().lower()


def _same_work(a: dict, b: dict) -> bool | None:
    """True/False when DOIs or titles decide, None when neither entry has either."""
    doi_a, doi_b = bibfix.clean_doi(a.get("doi", "")).lower(), bibfix.clean_doi(b.get("doi", "")).lower()
    if doi_a and doi_b:
        return doi_a == doi_b
    title_a, title_b = bibfix._norm(a.get("title", "")), bibfix._norm(b.get("title", ""))
    if title_a and title_b:
        return difflib.SequenceMatcher(None, title_a, title_b).ratio() >= bibfix.MIN_SIMILARITY
    return None


def _free_key(key: str, taken: set) -> str:
    for suffix in [""] + [chr(97 + i) for i in range(26)] + [str(i) for i in range(2, 1000)]:
        if key + suffix not in taken:
            return key + suffix
    return key


def compare(remote_text: str, local_text: str, taken=()) -> dict:
    """Which remote entries are new, changed for a local entry, identical; which local ones Zotero lacks.

    `taken`: keys of the project's other .bib files (a new entry must not reuse them)."""
    local = _entries(local_text)
    by_key = {e["key"]: e for e in local}
    taken = set(taken)
    taken_all = set(by_key) | taken
    out = {"new": [], "changed": [], "same": 0, "local_only": [], "skipped": 0}
    seen = set()
    remote = []
    for entry in _entries(remote_text):
        try:
            kept = {k: v for k, v in entry["fields"].items() if k not in IGNORE}
            fields = bibfix.check_entry(entry["type"], entry["key"], kept)
        except ValueError:
            out["skipped"] += 1
            continue
        if entry["key"] in seen:
            out["skipped"] += 1
            continue
        seen.add(entry["key"])
        remote.append({"type": entry["type"], "key": entry["key"], "fields": fields})
    taken_all |= seen
    twins: dict[str, tuple] = {}  # remote key -> (local entry, how matched); same key first, then DOI or title
    collided = set()
    for item in remote:
        twin = by_key.get(item["key"])
        if twin is None:
            continue
        if _same_work(item["fields"], twin["fields"]) is False:
            collided.add(item["key"])  # Same key, different work: not an update.
        else:
            twins[item["key"]] = (twin, "key")
    claimed = {twin["key"] for twin, _ in twins.values()}
    for item in remote:
        if item["key"] in twins:
            continue
        for other in local:
            if other["key"] in claimed or other["key"] == item["key"]:
                continue
            if _same_work(item["fields"], other["fields"]):
                twins[item["key"]] = (other, "doi/title")
                claimed.add(other["key"])
                break
    for item in remote:
        if item["key"] not in twins:
            key = item["key"]
            clash = key in collided or key in taken
            if clash:
                key = _free_key(key, taken_all)
                taken_all.add(key)
            out["new"].append({**item, "key": key, "zotero_key": item["key"], "collision": clash})
            continue
        twin, by = twins[item["key"]]
        diff = [{"field": name, "old": twin["fields"].get(name, ""), "new": value}
                for name, value in item["fields"].items() if _flat(twin["fields"].get(name, "")) != _flat(value)]
        if diff or item["type"] != twin["type"]:
            out["changed"].append({"key": twin["key"], "zotero_key": item["key"], "by": by, "type": item["type"],
                                   "old_type": twin["type"], "diff": diff})
        else:
            out["same"] += 1
    out["local_only"] = [e["key"] for e in local if e["key"] not in claimed]
    return out


def preview(text: str, taken, local_ok: bool) -> dict:
    cfg = {**info(), "key": _key(_read())}
    remote, version, cached = fetch(cfg, local_ok)
    return {**compare(remote, text, taken), "source": cfg["mode"], "version": version, "cached": cached}


def apply(text: str, ops) -> dict:
    """One UTF-16 splice {from, to, insert} that adds/updates the chosen entries of `text` and leaves every other byte.

    ops: [{"op": "add", "type", "key", "fields"} | {"op": "update", "key", "type"?, "fields": changed fields only}]."""
    if not isinstance(ops, list) or len(ops) > MAX_OPS:
        raise ZoteroError("Send the chosen entries.", 400)
    new = text
    try:
        for op in ops:
            if not isinstance(op, dict) or not isinstance(op.get("fields"), dict):
                raise ZoteroError("Bad entry.", 400)
            key = op.get("key")
            if op.get("op") == "add":
                if any(e.key == key for e in bibfix.parse(new)):
                    raise ZoteroError(f"'{key}' is already in this file.", 409)
                at, insert = bibfix.append_entry(new, op.get("type"), key, op["fields"])
                new = new[:at] + insert + new[at:]
            elif op.get("op") == "update":
                entry = bibfix.find_entry(new, key)
                if entry is None:
                    raise ZoteroError(f"No entry '{key}' in this file any more. Preview again.", 409)
                merged: dict[str, str] = {}
                for field in entry.fields:
                    merged.setdefault(field.name, field.value)
                merged.update(bibfix.check_entry(op.get("type") or entry.kind, key, op["fields"]))
                start, end, piece = bibfix.replace_entry(new, key, op.get("type") or entry.kind, key, merged)
                new = new[:start] + piece + new[end:]
            else:
                raise ZoteroError("Unknown operation.", 400)
    except ValueError as exc:
        raise ZoteroError(str(exc), 400)
    head = 0
    while head < min(len(text), len(new)) and text[head] == new[head]:
        head += 1
    tail = 0
    while tail < min(len(text), len(new)) - head and text[-1 - tail] == new[-1 - tail]:
        tail += 1
    return {"from": grammar.to_utf16(text, head), "to": grammar.to_utf16(text, len(text) - tail),
            "insert": new[head:len(new) - tail]}
