"""Suggest missing BibTeX fields from Crossref, and insert them into a .bib without touching the rest.

Stdlib-only. The only network use is `get_json` (Crossref's public works API, which sees just a DOI or a
title); callers must opt in (`ci_report.py lint --bib-lookup`, the editor's "Look up" button). Tests replace
`get_json`. `insert_fields` / `edit_for` are pure: they cut nothing and leave every other byte as it was.
"""

from __future__ import annotations

import collections
import difflib
import html
import json
import re
import threading
import urllib.error
import urllib.parse
import urllib.request
from typing import NamedTuple

import grammar

API = "https://api.crossref.org/works"
USER_AGENT = "LatexPipeline (https://github.com/NotTahaAli/LatexPipeline)"
MIN_SIMILARITY = 0.9
LIMITER = grammar.Limiter(requests=30, size=1, window=60.0)  # size is unused: one hit per lookup
CACHE: collections.OrderedDict = collections.OrderedDict()
CACHE_SIZE = 200
CACHE_LOCK = threading.Lock()
ALIASES = [{"journal", "journaltitle"}, {"year", "date"}, {"author", "editor"}]
CONTAINER_KINDS = {"article": "journal", "inproceedings": "booktitle", "conference": "booktitle",
                   "incollection": "booktitle", "inbook": "booktitle"}


class BibLookupError(Exception):
    pass


# ---------------------------------------------------------------------------
# Parsing: just enough BibTeX to locate entries, fields and their exact byte ranges
# ---------------------------------------------------------------------------

class Field(NamedTuple):
    name: str
    value: str  # outer braces / quotes removed when the value is one piece
    name_start: int
    name_end: int
    eq: int
    value_start: int
    value_end: int
    comma: int  # index of the comma after the value, -1 when there is none


class Entry(NamedTuple):
    kind: str
    key: str
    start: int
    end: int  # just past the closing delimiter
    head_end: int  # just past the comma after the key (or past the key when there is none)
    has_head_comma: bool
    fields: list


def _balanced(text: str, i: int) -> int:
    """text[i] is "{": the index just past its match (len(text) when unbalanced)."""
    depth = 0
    for j in range(i, len(text)):
        if text[j] == "{":
            depth += 1
        elif text[j] == "}":
            depth -= 1
            if depth == 0:
                return j + 1
    return len(text)


def _skip_ws(text: str, i: int) -> int:
    while i < len(text) and text[i].isspace():
        i += 1
    return i


def _value_end(text: str, i: int) -> int:
    """End of one field value: pieces ("..." {...} number macro) joined by #."""
    while True:
        i = _skip_ws(text, i)
        if i < len(text) and text[i] == "{":
            i = _balanced(text, i)
        elif i < len(text) and text[i] == '"':
            depth, i = 0, i + 1
            while i < len(text) and not (text[i] == '"' and depth <= 0):
                depth += (text[i] == "{") - (text[i] == "}")
                i += 1
            i += 1
        else:
            while i < len(text) and not text[i].isspace() and text[i] not in ',}#)=':
                i += 1
        j = _skip_ws(text, i)
        if j < len(text) and text[j] == "#":
            i = j + 1
        else:
            return i


def _plain(raw: str) -> str:
    raw = raw.strip()
    if len(raw) >= 2 and raw[0] == "{" and _balanced(raw, 0) == len(raw):
        return raw[1:-1].strip()
    if len(raw) >= 2 and raw[0] == raw[-1] == '"' and '"' not in raw[1:-1].replace('{"}', ""):
        return raw[1:-1].strip()
    return raw


def parse(text: str) -> list[Entry]:
    """Every real entry (not @comment, @string, @preamble), with field positions. Malformed ones are skipped."""
    out: list[Entry] = []
    pos = 0
    head = re.compile(r"@[ \t]*(\w+)[ \t\r\n]*([({])")
    while True:
        match = head.search(text, pos)
        if not match:
            return out
        kind, opener = match.group(1).lower(), match.group(2)
        pos = match.end()
        if kind in ("comment", "string", "preamble"):
            if opener == "{":
                pos = _balanced(text, match.end() - 1)
            continue
        closer = "}" if opener == "{" else ")"
        key_match = re.compile(r"\s*([^,\s{}()]+)\s*").match(text, pos)
        if not key_match:
            continue
        i = key_match.end()
        has_comma = i < len(text) and text[i] == ","
        head_end = i + 1 if has_comma else key_match.end(1)
        fields: list[Field] = []
        i = head_end
        ok = False
        while i < len(text):
            i = _skip_ws(text, i)
            if i >= len(text):
                break
            if text[i] == ",":
                i += 1
                continue
            if text[i] == closer:
                ok = True
                break
            name = re.compile(r"[\w:.+-]+").match(text, i)
            if not name:
                break
            eq = _skip_ws(text, name.end())
            if eq >= len(text) or text[eq] != "=":
                break
            vstart = _skip_ws(text, eq + 1)
            vend = _value_end(text, vstart)
            after = _skip_ws(text, vend)
            comma = after if after < len(text) and text[after] == "," else -1
            fields.append(Field(name.group(0).lower(), _plain(text[vstart:vend]), name.start(), name.end(),
                                eq, vstart, vend, comma))
            i = vend
        if ok:
            out.append(Entry(kind, key_match.group(1), match.start(), i + 1, head_end, has_comma, fields))
            pos = i + 1


def find_entry(text: str, key: str) -> Entry | None:
    return next((entry for entry in parse(text) if entry.key == key), None)


# ---------------------------------------------------------------------------
# Inserting fields
# ---------------------------------------------------------------------------

def edit_for(text: str, key: str, new: dict[str, str]) -> tuple[int, str] | None:
    """(index, insertion) that adds the fields `new` to entry `key`, in the entry's own style; None if no such entry.

    Fields the entry already has are skipped, never replaced."""
    entry = find_entry(text, key)
    if entry is None:
        return None
    have = {field.name for field in entry.fields}
    new = {name: value for name, value in new.items() if name.lower() not in have}
    if not new:
        return None
    eol = "\r\n" if "\r\n" in text[entry.start:entry.end] else "\n"
    last = entry.fields[-1] if entry.fields else None
    if last is None:
        indent, inline, quote, gap, spaced, eq_col = "  ", False, False, " ", True, 0
    else:
        line_start = text.rfind("\n", 0, last.name_start) + 1
        before = text[line_start:last.name_start]
        inline = bool(before.strip())
        indent = "" if inline else before
        quote = text[last.value_start] == '"'
        gap = text[last.eq + 1:last.value_start] if "\n" not in text[last.eq + 1:last.value_start] else " "
        spaced = last.eq > last.name_end
        eq_col = last.eq - line_start - len(indent)
    upper = last is not None and text[last.name_start:last.name_end].isupper()
    lines = []
    for name, value in new.items():
        shown = name.upper() if upper else name.lower()
        pad = " " * max(eq_col - len(shown), 1 if spaced else 0) if spaced else ""
        body = '"' + value.replace('"', "''") + '"' if quote else "{" + value + "}"
        lines.append(f"{shown}{pad}={gap}{body}")
    sep = " " if inline else eol + indent
    if last is None:
        return entry.head_end, ("" if entry.has_head_comma else ",") + "".join(sep + line + "," for line in lines)
    if last.comma >= 0:
        return last.comma + 1, "".join(sep + line + "," for line in lines)
    return last.value_end, "," + ",".join(sep + line for line in lines)


def insert_fields(text: str, key: str, new: dict[str, str]) -> str:
    edit = edit_for(text, key, new)
    return text if edit is None else text[:edit[0]] + edit[1] + text[edit[0]:]


# ---------------------------------------------------------------------------
# Crossref
# ---------------------------------------------------------------------------

def get_json(url: str, timeout: float = 15.0) -> dict:
    """GET and decode JSON, throttled and cached. Tests replace this function."""
    with CACHE_LOCK:
        if url in CACHE:
            return CACHE[url]
    try:
        LIMITER.acquire(0, 0)
    except grammar.GrammarError:
        raise BibLookupError("Too many Crossref lookups; wait a moment.")
    request = urllib.request.Request(url, headers={"Accept": "application/json", "User-Agent": USER_AGENT})
    try:
        with urllib.request.urlopen(request, timeout=timeout) as reply:
            data = json.loads(reply.read().decode("utf-8"))
    except urllib.error.HTTPError as error:
        raise BibLookupError("Crossref does not know this DOI." if error.code == 404
                             else f"Crossref answered HTTP {error.code}.")
    except (OSError, ValueError) as error:
        raise BibLookupError(f"Crossref is not reachable: {error}")
    with CACHE_LOCK:
        CACHE[url] = data
        while len(CACHE) > CACHE_SIZE:
            CACHE.popitem(last=False)
    return data


def clean_doi(value: str) -> str:
    return re.sub(r"^(?:https?://(?:dx\.)?doi\.org/|doi:)", "", value.strip(), flags=re.IGNORECASE)


def tex(text: str) -> str:
    """Crossref text as a safe braced BibTeX value: no markup, entities decoded, specials escaped, braces dropped."""
    text = html.unescape(re.sub(r"<[^>]+>", "", text))
    text = re.sub(r"\s+", " ", text).strip().replace("{", "").replace("}", "").replace("\\", "")
    return re.sub(r"([&%#_])", r"\\\1", text)


def _norm(title: str) -> str:
    title = re.sub(r"\\[a-zA-Z]+", " ", html.unescape(re.sub(r"<[^>]+>", "", title)))
    return " ".join(re.sub(r"[^a-z0-9]+", " ", title.lower()).split())


def _year(item: dict) -> str:
    for name in ("issued", "published-print", "published-online", "published"):
        parts = (item.get(name) or {}).get("date-parts") or [[None]]
        if parts[0] and parts[0][0]:
            return str(parts[0][0])
    return ""


def _author(person: dict) -> str:
    if person.get("family"):
        return tex(person["family"] + (", " + person["given"] if person.get("given") else ""))
    return "{" + tex(person.get("name", "")) + "}" if person.get("name") else ""


def map_item(item: dict, kind: str) -> dict[str, str]:
    """Crossref work -> BibTeX fields (all of them; the caller keeps the missing ones)."""
    out: dict[str, str] = {}
    authors = [a for a in map(_author, item.get("author") or []) if a]
    if authors:
        out["author"] = " and ".join(authors)
    if item.get("title"):
        out["title"] = tex(item["title"][0])
    container = (item.get("container-title") or [""])[0]
    if container and kind in CONTAINER_KINDS:
        out[CONTAINER_KINDS[kind]] = tex(container)
    if _year(item):
        out["year"] = _year(item)
    for name in ("volume", "publisher"):
        if item.get(name):
            out[name] = tex(str(item[name]))
    if item.get("issue"):
        out["number"] = tex(str(item["issue"]))
    if item.get("page"):
        out["pages"] = re.sub(r"\s*[-\u2013\u2014]+\s*", "--", tex(str(item["page"])))
    if item.get("DOI"):
        out["doi"] = re.sub(r"[\s{}]", "", item["DOI"])
    return out


def accept_by_title(item: dict, title: str, year: str) -> bool:
    """High title similarity and (if the entry has a year) a year within one. Anything less is a guess."""
    titles = item.get("title") or []
    candidates = [titles[0] + " " + sub for sub in item.get("subtitle") or []] + titles[:1]
    want = _norm(title)
    if not want or not candidates:
        return False
    if max(difflib.SequenceMatcher(None, want, _norm(c)).ratio() for c in candidates) < MIN_SIMILARITY:
        return False
    found = _year(item)
    return not (re.fullmatch(r"\d{4}", year) and found and abs(int(found) - int(year)) > 1)


def suggest(kind: str, fields: dict[str, str]) -> dict:
    """{"fields": {name: value} (only missing ones), "source": "doi"|"title", "doi": str}; raises BibLookupError.

    `fields` maps lower-case names to plain values. Nothing is guessed: with no DOI the title match must be close."""
    doi = clean_doi(fields.get("doi", ""))
    if doi:
        item = (get_json(f"{API}/{urllib.parse.quote(doi, safe='/')}") or {}).get("message") or {}
        source = "doi"
    elif fields.get("title"):
        query = urllib.parse.urlencode({"query.bibliographic": fields["title"], "rows": 1})
        items = ((get_json(f"{API}?{query}") or {}).get("message") or {}).get("items") or []
        item = items[0] if items else {}
        if not item or not accept_by_title(item, fields["title"], fields.get("year", "")):
            raise BibLookupError("No Crossref record matches the title closely enough; add the DOI to look it up.")
        source = "title"
    else:
        raise BibLookupError("The entry has neither a DOI nor a title to look up.")
    have = {name for name, value in fields.items() if value}
    for group in ALIASES:
        if group & have:
            have |= group
    found = {name: value for name, value in map_item(item, kind).items() if name not in have and value}
    return {"fields": found, "source": source, "doi": clean_doi(item.get("DOI", doi))}


def suggest_for(text: str, key: str) -> dict:
    """suggest() for entry `key` of .bib `text`, plus the edit that applies it: {"at": utf16 index, "insert": str}."""
    entry = find_entry(text, key)
    if entry is None:
        raise BibLookupError(f"No entry '{key}' in this file.")
    result = suggest(entry.kind, {field.name: field.value for field in entry.fields})
    edit = edit_for(text, key, result["fields"])
    if edit:
        result["at"], result["insert"] = grammar.to_utf16(text, edit[0]), edit[1]
    return result
