"""Sync a .bib with a Zotero library: fetch BibTeX, compare with the .bib text the editor holds, apply a chosen subset.

Stdlib-only (imports `bibfix` for parsing and splicing, `grammar` for the rate limiter). Network use is `get` and
nothing else; callers opt in (the owner's "Sync from Zotero" click), tests replace `get`/`_opener`.

The API key lives outside the project: `ZOTERO_API_KEY`, or the 0600 file `config_path()` in the user's config
dir. It is sent only in the `Zotero-API-Key` header (never in a URL) and `info()`/errors never contain it.
"""

from __future__ import annotations

import collections
import difflib
import hashlib
import itertools
import json
import os
import re
import sys
import threading
import urllib.error
import urllib.request
from pathlib import Path
from urllib.parse import quote

import bibfix
import grammar

API = "https://api.zotero.org"
LOCAL = "http://127.0.0.1:23119/better-bibtex/export/"  # Better BibTeX pull export, Zotero desktop
USER_AGENT = "LatexPipeline (https://github.com/NotTahaAli/LatexPipeline)"
LIMITER = grammar.Limiter(requests=120, size=1, window=60.0)  # one hit per page; a full sync is up to 200 pages
WAIT = 60.0  # seconds get() may wait for the limiter before refusing
PAGE = 100  # Zotero's maximum for non-JSON formats
MAX_PAGES = 200
HOSTED_PAGES = 20  # pages per sync behind the hosted gateway (2000 entries; a bigger library syncs by collection)
HOSTED_TOTAL = 3_000_000  # characters of one hosted sync: it travels to the browser and on to the worker
CACHE_SIZE = 4
MAX_BODY = 8_000_000
MAX_TOTAL = 20_000_000
MAX_OPS = 500
MAX_OPS_CHARS = 1_000_000
MAX_FIELDS = 60
MAX_VALUE = 20_000
MAX_TEXT = 1_000_000  # characters of the target .bib (serve.BIB_MAX_CHARS)
MAX_OTHERS = 4 * MAX_TEXT  # characters of the project's other .bib files together
TITLE_CAP = 300  # characters of a normalised title that are compared
FUZZY_WORK = 30_000_000  # len(a) * len(b) summed over the title comparisons of one compare()
FUZZY_VISITS = 1_500_000  # entries the fuzzy title pass of one compare() looks at
POOL_SCAN = 50  # entries with the same DOI or title looked at for one Zotero entry
FORMATS = ("bibtex", "biblatex")
IGNORE = {"file"}  # Attachment paths on the Zotero machine
CACHE: dict = {}  # (mode, base, format, key hash) -> {"version", "text"}; small, in memory, never holds the key
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


_ENV: dict = {}


def env_key() -> str:
    """ZOTERO_API_KEY, taken out of os.environ on first use (at import, before serve.py starts a build) so no
    build or worker inherits it: kpathsea expands $VARS in file names, and Lua can read the environment."""
    if "key" not in _ENV:
        _ENV["key"] = os.environ.pop("ZOTERO_API_KEY", "").strip()
    return _ENV["key"]


env_key()


def _key(cfg: dict) -> str:
    return env_key() or str(cfg.get("key") or "")


def info() -> dict:
    """What the settings dialog may see. Never the key, only whether there is one."""
    cfg, env = _read(), bool(env_key())
    mode = "local" if cfg.get("mode") == "local" else "web"
    out = {"mode": mode, "library_type": "groups" if cfg.get("library_type") == "groups" else "users",
           "library_id": str(cfg.get("library_id") or ""), "collection": str(cfg.get("collection") or ""),
           "format": cfg.get("format") if cfg.get("format") in FORMATS else "bibtex",
           "has_key": env or bool(cfg.get("key")), "key_from_env": env,
           "share_editors": cfg.get("share_editors") is True}
    out["configured"] = configured(out)
    return out


def configured(cfg: dict) -> bool:
    if cfg.get("mode") == "local":  # My Library needs no ID; a group library needs its group ID
        return cfg.get("library_type") != "groups" or bool(cfg.get("library_id"))
    return bool(cfg.get("library_id") and cfg.get("has_key", cfg.get("key")))


def check_settings(data: dict, local: bool = True) -> dict:
    """The validated settings in `data` (no key): mode, library_type, library_id, collection, format.

    Web collections are keys (8 letters or digits); Better BibTeX (local) also takes a path such as Thesis/Chapter 2."""
    mode, kind = data.get("mode", "web"), data.get("library_type", "users")
    lib, coll = str(data.get("library_id") or "").strip(), str(data.get("collection") or "").strip().strip("/")
    fmt = data.get("format", "bibtex")
    if mode not in (("web", "local") if local else ("web",)) or kind not in ("users", "groups") or fmt not in FORMATS:
        raise ZoteroError("Unknown mode, library type or format.", 400)
    if lib and not re.fullmatch(r"\d{1,12}", lib):
        raise ZoteroError("The library ID is the number in your Zotero profile or group URL.", 400)
    if coll and mode == "web" and not re.fullmatch(r"[A-Za-z0-9]{8}", coll):
        raise ZoteroError("A collection key is 8 letters or digits (the end of the collection's web address).", 400)
    if coll and mode == "local" and (len(coll) > 300 or re.search(r"[\x00-\x1f\x7f]|(^|/)\.\.?(/|$)|//", coll)):
        raise ZoteroError("A collection is its key or its path, such as Thesis/Chapter 2.", 400)
    return {"mode": mode, "library_type": kind, "library_id": lib, "collection": coll, "format": fmt}


def check_key(key) -> str:
    """A new key, or "" for none. Raises on anything that does not look like a Zotero API key."""
    if key in (None, ""):
        return ""
    if not (isinstance(key, str) and re.fullmatch(r"[A-Za-z0-9]{10,64}", key.strip())):
        raise ZoteroError("That does not look like a Zotero API key (letters and digits).", 400)
    return key.strip()


def save_settings(data: dict) -> dict:
    """Validate and write the settings file (mode 0600). `key` "" keeps the stored key; `clear_key` removes it.
    `share_editors` (owner's choice, default off) lets shared-link editors sync with this key, server-side."""
    cfg = _read()
    fields, key = check_settings(data), check_key(data.get("key"))
    cfg.update(fields)
    if "share_editors" in data:
        cfg["share_editors"] = data["share_editors"] is True
    if data.get("clear_key"):
        cfg.pop("key", None)
    if key:
        cfg["key"] = key
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


def get(url: str, headers: dict, local: bool = False, timeout: float = 20.0, limit: int = MAX_BODY) -> tuple:
    """(status, lower-case headers, body bytes); 304 is a normal answer, other failures raise ZoteroError.

    Throttled (waits up to WAIT seconds). Reads at most `limit` + 1 bytes: callers refuse a longer body.
    Errors name the status only: never the URL or a header."""
    try:
        LIMITER.acquire(0, WAIT)
    except grammar.GrammarError:
        raise ZoteroError("Too many Zotero requests; wait a moment.", 429)
    request = urllib.request.Request(url, headers={"User-Agent": USER_AGENT, **headers})
    try:
        with _opener(local).open(request, timeout=timeout) as reply:
            return reply.status, {k.lower(): v for k, v in reply.headers.items()}, reply.read(limit + 1)
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


def fetch(cfg: dict, local_ok: bool, max_pages: int = MAX_PAGES, max_total: int = MAX_TOTAL) -> tuple:
    """(bibtex text, version, from_cache). An unchanged library answers 304 and the cached text is reused.

    The hosted gateway passes HOSTED_PAGES and HOSTED_TOTAL: one sync there must not take the shared LIMITER's
    whole minute, and the cache (CACHE_SIZE entries) stays small."""
    if cfg["mode"] == "local":
        if not local_ok:
            raise ZoteroError("Better BibTeX sync is off while sharing: use the web API.", 409)
        status, _, body = get(local_url(cfg), {}, local=True, limit=MAX_TOTAL)
        if len(body) > MAX_TOTAL:
            raise ZoteroError("The export is too large.", 413)
        return body.decode("utf-8", "replace"), "", False
    if not cfg["library_id"] or not cfg.get("key"):
        raise ZoteroError("Set the library ID and API key first (Zotero settings).", 400)
    scope = f"collections/{cfg['collection']}/" if cfg["collection"] else ""
    base = f"{API}/{cfg['library_type']}/{cfg['library_id']}/{scope}items/top"
    # Per key: a 304 for one person's key must not hand the cached text to someone else's.
    cache_key = ("web", base, cfg["format"], hashlib.sha256(cfg["key"].encode()).hexdigest())
    with CACHE_LOCK:
        cached = CACHE.get(cache_key)
    headers = {"Zotero-API-Version": "3", "Zotero-API-Key": cfg["key"]}
    parts, version, start = [], None, 0
    for _ in range(max_pages):
        page_headers = dict(headers)
        if start == 0 and cached:
            page_headers["If-Modified-Since-Version"] = cached["version"]
        order = "itemType=-attachment&sort=dateAdded&direction=asc"  # stable under edits, so page keys do not shift
        url = f"{base}?format={cfg['format']}&{order}&limit={PAGE}&start={start}"
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
        if not more:
            break
        if sum(map(len, parts)) > max_total:
            raise ZoteroError("The library is too large to sync in one go; pick a collection.", 413)
    else:
        raise ZoteroError("The library is too large to sync in one go; pick a collection.", 413)
    text = "\n".join(parts)
    if len(text) > max_total:
        raise ZoteroError("The library is too large to sync in one go; pick a collection.", 413)
    with CACHE_LOCK:
        if version:
            CACHE[cache_key] = {"version": version, "text": text}
        while len(CACHE) > CACHE_SIZE:
            CACHE.pop(next(iter(CACHE)))
    return text, version or "", False


def local_url(cfg: dict) -> str:
    """Better BibTeX pull export (content/pull-export.ts): /export/library?/<lib>/library.<fmt> and
    /export/collection?/<lib>/<key or path>.<fmt>, where <lib> is 1 (My Library) or a group's ID."""
    lib = cfg["library_id"] if cfg["library_type"] == "groups" else "1"
    if cfg.get("collection"):
        return f"{LOCAL}collection?/{lib}/{quote(cfg['collection'], safe='/')}.{cfg['format']}"
    return f"{LOCAL}library?/{lib}/library.{cfg['format']}"


# ---------------------------------------------------------------------------
# Comparing
# ---------------------------------------------------------------------------

def _entries(text: str) -> list[dict]:
    out = []
    for entry in bibfix.parse(text):
        fields: dict[str, str] = {}
        for field in entry.fields:
            fields.setdefault(field.name, field.value)
        out.append({"type": entry.kind, "key": entry.key, "fields": fields, "doi": _doi(fields),
                    "title": _title(fields)})
    return out


def _flat(value: str) -> str:
    return re.sub(r"\s+", " ", value.replace("{", "").replace("}", "")).strip().lower()


def _doi(fields: dict) -> str:
    return bibfix.clean_doi(fields.get("doi", "")[:500]).lower()


def _title(fields: dict) -> str:
    """The normalised title, at most TITLE_CAP characters: what DOI-less matching compares."""
    return bibfix._norm(fields.get("title", "")[:4 * TITLE_CAP])[:TITLE_CAP]


def _similar(a: str, b: str, floor: float, budget: dict) -> bool | None:
    """SequenceMatcher ratio >= floor, behind the cheap upper bounds; None once the comparison budget is spent."""
    if 2 * min(len(a), len(b)) < floor * (len(a) + len(b)):
        return False  # the ratio cannot reach the floor
    if budget["work"] <= 0:
        return None
    budget["work"] -= len(a) * len(b)
    matcher = difflib.SequenceMatcher(None, a, b)
    return matcher.real_quick_ratio() >= floor and matcher.quick_ratio() >= floor and matcher.ratio() >= floor


def _conflict(a: dict, b: dict, budget: dict) -> bool:
    """Two entries under one key: different works? Only a DOI mismatch or titles that share little say so; a title
    edited in Zotero (same key, no DOI) is an update, and so is anything once the budget is spent (it is offered
    unticked, with its diff)."""
    if a["doi"] and b["doi"]:
        return a["doi"] != b["doi"]
    return bool(a["title"] and b["title"] and _similar(a["title"], b["title"], 0.5, budget) is False)


def _twins(remote: list, local: list, claimed: set, budget: dict) -> dict:
    """remote key -> local entry for the same work under another key: DOI, then exact title, then a close title.

    Indexed: claimed entries leave the front of their pools and at most POOL_SCAN candidates of a pool are looked
    at, so a thousand entries sharing one DOI cost a thousand lookups. The fuzzy fallback shares `budget` (entries
    visited, title characters compared) with the rest of one compare()."""
    by_doi: dict[str, collections.deque] = {}
    by_title: dict[str, collections.deque] = {}
    for other in local:
        if other["doi"]:
            by_doi.setdefault(other["doi"], collections.deque()).append(other)
        if other["title"]:
            by_title.setdefault(other["title"], collections.deque()).append(other)
    found: dict[str, dict] = {}

    def free(item, other) -> bool:
        return (other["key"] not in claimed and other["key"] != item["key"]
                and not (item["doi"] and other["doi"] and item["doi"] != other["doi"]))

    for item in remote:
        pick = None
        for pool in (by_doi.get(item["doi"]) if item["doi"] else None,
                     by_title.get(item["title"]) if item["title"] else None):
            while pool and pool[0]["key"] in claimed:
                pool.popleft()
            pick = next((o for o in itertools.islice(pool or (), POOL_SCAN) if free(item, o)), None)
            if pick:
                break
        title = item["title"]
        if pick is None and title:
            for other in local:
                if budget["visits"] <= 0:
                    break
                budget["visits"] -= 1
                if other["title"] and free(item, other) and _similar(title, other["title"], bibfix.MIN_SIMILARITY,
                                                                      budget):
                    pick = other
                    break
        if pick:
            found[item["key"]] = pick
            claimed.add(pick["key"])
    return found


def _free_key(key: str, taken: set) -> str:
    for suffix in [""] + [chr(97 + i) for i in range(26)] + [str(i) for i in range(2, 1000)]:
        if key + suffix not in taken:
            return key + suffix
    return key


def compare(remote_text: str, local_text: str, taken=(), others=None) -> dict:
    """Which remote entries are new, changed for a local entry, identical; which local ones Zotero lacks; which are
    already in another .bib file of the project (`elsewhere`: not offered again, so no duplicates).

    `taken`: keys of the project's other .bib files (a new entry must not reuse them).
    `others`: {path: text} of the project's other .bib files, matched like the target file (key, DOI, title)."""
    local = _entries(local_text)
    by_key = {e["key"]: e for e in local}
    away = [{**e, "file": path} for path, text in sorted((others or {}).items()) for e in _entries(text)]
    away_by_key: dict[str, dict] = {}
    for e in away:
        away_by_key.setdefault(e["key"], e)
    taken = set(taken) | set(away_by_key)
    raw = _entries(remote_text)
    taken_all = set(by_key) | taken | {e["key"] for e in raw}
    out = {"new": [], "changed": [], "same": 0, "local_only": [], "skipped": 0, "elsewhere": []}
    seen = set()
    remote = []
    for entry in raw:
        try:
            kept = {k: v for k, v in entry["fields"].items() if k not in IGNORE}
            fields = bibfix.check_entry(entry["type"], entry["key"], kept)
        except ValueError:
            out["skipped"] += 1
            continue
        key = entry["key"]
        if key in seen:  # Per-page exports only de-duplicate within a page: a later page may reuse a key.
            key = _free_key(key, taken_all)
            taken_all.add(key)
        seen.add(key)
        remote.append({"type": entry["type"], "key": key, "zotero_key": entry["key"], "fields": fields,
                       "doi": _doi(fields), "title": _title(fields)})
    budget = {"work": FUZZY_WORK, "visits": FUZZY_VISITS}  # shared by every title comparison below
    twins: dict[str, tuple] = {}  # remote key -> (local entry, how matched); same key first, then DOI or title
    collided = set()
    for item in remote:
        twin = by_key.get(item["key"])
        if twin is None:
            continue
        if _conflict(item, twin, budget):
            collided.add(item["key"])  # Same key, different work: not an update.
        else:
            twins[item["key"]] = (twin, "key")
    claimed = {twin["key"] for twin, _ in twins.values()}
    rest = [item for item in remote if item["key"] not in twins]
    for key, other in _twins(rest, local, claimed, budget).items():
        twins[key] = (other, "doi/title")
    found: dict[str, dict] = {}  # remote key -> entry of another file
    for item in remote:
        twin = away_by_key.get(item["key"])
        if item["key"] not in twins and twin and not _conflict(item, twin, budget):
            found[item["key"]] = twin
    rest = [item for item in remote if item["key"] not in twins and item["key"] not in found]
    found.update(_twins(rest, away, {twin["key"] for twin in found.values()}, budget))
    for item in remote:
        if item["key"] in found:
            twin = found[item["key"]]
            out["elsewhere"].append({"key": twin["key"], "file": twin["file"], "zotero_key": item["zotero_key"]})
            continue
        if item["key"] not in twins:
            key = item["key"]
            clash = key in collided or key in taken
            if clash:
                key = _free_key(key, taken_all)
                taken_all.add(key)
            out["new"].append({"type": item["type"], "key": key, "zotero_key": item["zotero_key"],
                               "fields": item["fields"], "collision": clash or item["zotero_key"] != key})
            continue
        twin, by = twins[item["key"]]
        diff = [{"field": name, "old": twin["fields"].get(name, ""), "new": value}
                for name, value in item["fields"].items() if _flat(twin["fields"].get(name, "")) != _flat(value)]
        if diff or item["type"] != twin["type"]:
            out["changed"].append({"key": twin["key"], "zotero_key": item["zotero_key"], "by": by, "type": item["type"],
                                   "old_type": twin["type"], "diff": diff})
        else:
            out["same"] += 1
    out["local_only"] = [e["key"] for e in local if e["key"] not in claimed]
    return out


def check_request(data: dict) -> tuple:
    """(text, taken, others) of a preview request: the target .bib text, keys and texts of the other .bib files."""
    text, taken, others = data.get("text"), data.get("taken") or [], data.get("others") or {}
    if not (isinstance(text, str) and isinstance(taken, list) and all(isinstance(k, str) for k in taken)
            and isinstance(others, dict) and all(isinstance(v, str) for v in others.values())):
        raise ZoteroError("Send the .bib text.", 400)
    if len(text) > MAX_TEXT:
        raise ZoteroError("This .bib file is too large to sync (1 MB limit).", 413)
    if len(others) > 50 or len(taken) > 100_000 or sum(map(len, others.values())) > MAX_OTHERS:
        raise ZoteroError("The other .bib files are too large to compare (4 MB limit).", 413)
    return text, taken, others


def preview(text: str, taken, local_ok: bool, others=None, cfg=None) -> dict:
    """Fetch with `cfg` (default: this computer's settings and key) and compare with `text` and `others`."""
    cfg = cfg or {**info(), "key": _key(_read())}
    remote, version, cached = fetch(cfg, local_ok)
    return {**compare(remote, text, taken, others), "source": cfg["mode"], "version": version, "cached": cached}


def apply(text: str, ops) -> dict:
    """One UTF-16 splice {from, to, insert} that adds/updates the chosen entries of `text` and leaves every other byte.

    ops: [{"op": "add", "type", "key", "fields"} | {"op": "update", "key", "type"?, "fields": changed fields only}].
    The file is parsed once; updates are spliced in at their original offsets, adds are appended."""
    if not isinstance(ops, list) or len(ops) > MAX_OPS:
        raise ZoteroError(f"Send up to {MAX_OPS} chosen entries.", 400)
    size = 0
    for op in ops:
        fields = op.get("fields") if isinstance(op, dict) else None
        if not isinstance(fields, dict) or len(fields) > MAX_FIELDS or not isinstance(op.get("key"), str) \
                or len(op["key"]) > 200:
            raise ZoteroError("Bad entry.", 400)
        if any(not isinstance(v, str) or len(v) > MAX_VALUE for v in fields.values()):
            raise ZoteroError("A field value is missing or too long.", 400)
        size += sum(map(len, fields.values()))
    if size > MAX_OPS_CHARS:
        raise ZoteroError("The chosen entries are too large.", 413)
    entries: dict[str, bibfix.Entry] = {}
    for entry in bibfix.parse(text):
        entries.setdefault(entry.key, entry)
    cuts: list[tuple[int, int, str]] = []
    adds: list[dict] = []
    done: set = set()
    try:
        for op in ops:
            key = op["key"]
            if op.get("op") == "add":
                if key in entries or key in done:
                    raise ZoteroError(f"'{key}' is already in this file.", 409)
                done.add(key)
                adds.append(op)
            elif op.get("op") == "update":
                entry = entries.get(key)
                if entry is None:
                    raise ZoteroError(f"No entry '{key}' in this file any more. Preview again.", 409)
                if key in done:
                    raise ZoteroError(f"'{key}' is listed twice.", 400)
                done.add(key)
                merged: dict[str, str] = {}
                for field in entry.fields:
                    merged.setdefault(field.name, field.value)
                merged.update(bibfix.check_entry(op.get("type") or entry.kind, key, op["fields"]))
                cuts.append(bibfix.replace_entry(text, key, op.get("type") or entry.kind, key, merged, entry))
            else:
                raise ZoteroError("Unknown operation.", 400)
        new = text
        for start, end, piece in sorted(cuts, reverse=True):
            new = new[:start] + piece + new[end:]
        probe = new  # Each add is laid out after the one before (blank line, the file's indent and line ending).
        for op in adds:
            _, insert = bibfix.append_entry(probe, op.get("type"), op["key"], op["fields"])
            new += insert
            probe = insert
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
