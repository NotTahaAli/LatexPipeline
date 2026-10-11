"""
Tidy a .bib file: entries sorted (by key, first author or year), fields in a fixed order with their "=" aligned,
and possible duplicates reported (never removed). Standard library only; imported by build.py (--tidy-bib) and
serve.py (POST /api/bib/tidy) only when used.

Nothing is ever dropped: every value is copied byte for byte, text between entries (comments) moves with the
entry after it, @string / @preamble / @comment blocks and anything that does not parse as an entry go to the top
in their original order (a @string must come before its use), and tidy() checks the result before returning it.
"""

from __future__ import annotations

import re
from collections import Counter

import bibfix

# bibtex-tidy's default --sort-fields order (https://github.com/FlamingTempura/bibtex-tidy); other fields follow
# in their own order.
FIELD_ORDER = ["title", "shorttitle", "author", "year", "month", "day", "journal", "booktitle", "location", "on",
               "publisher", "address", "series", "volume", "number", "pages", "doi", "isbn", "issn", "url",
               "urldate", "copyright", "category", "note", "metadata"]
ORDERS = ("key", "author", "year", "none")
INDENT = "  "


def _first_author(entry: bibfix.Entry) -> str:
    value = next((field.value for field in entry.fields if field.name in ("author", "editor")), "")
    first = re.split(r"\s+and\s+", value, maxsplit=1)[0]
    last = first.split(",")[0] if "," in first else (first.split() or [""])[-1]
    return re.sub(r"[^a-z0-9]", "", last.lower())


def _year(entry: bibfix.Entry) -> str:
    value = next((field.value for field in entry.fields if field.name in ("year", "date")), "")
    match = re.search(r"\d{4}", value)
    return match.group() if match else "9999"


def sort_key(entry: bibfix.Entry, order: str) -> tuple:
    # Natural order: main-2 before main-10. re.split alternates text and digits, so the types line up.
    key = tuple(int(part) if part.isdigit() else part for part in re.split(r"(\d+)", entry.key.lower()))
    if order == "author":
        return (_first_author(entry), _year(entry), key)
    if order == "year":
        return (_year(entry), _first_author(entry), key)
    return (key,)


def format_entry(text: str, entry: bibfix.Entry, eol: str) -> str:
    """The entry with one field per line, names padded so the "=" line up; each value as written."""
    rank = {name: i for i, name in enumerate(FIELD_ORDER)}
    fields = sorted(entry.fields, key=lambda field: rank.get(field.name, len(FIELD_ORDER)))  # stable
    width = max((len(field.name) for field in fields), default=0)
    lines = [f"@{entry.kind}{{{entry.key},"]
    lines += [f"{INDENT}{field.name.ljust(width)} = {text[field.value_start:field.value_end].strip()},"
              for field in fields]
    return eol.join(lines) + eol + "}"


def _blocks(text: str) -> tuple[list[str], list[tuple[bibfix.Entry, str]], str]:
    """(top: @string etc. and unparsed text, [(entry, comment text before it)], text after the last entry)."""
    top: list[str] = []
    entries: list[tuple[bibfix.Entry, str]] = []
    pos = 0
    for entry in bibfix.parse(text):
        gap = text[pos:entry.start]
        if "@" in gap:  # a @string/@preamble/@comment or something unparsed: keep its order, at the top
            opener = re.compile(r"@\w*\s*\{").search(gap, gap.rfind("@"))
            cut = bibfix._balanced(gap, opener.end() - 1) if opener else len(gap)
            top.append(gap[:cut].strip())  # comments after the last block stay with the entry
            gap = gap[cut:]
        entries.append((entry, gap.strip()))
        pos = entry.end
    return [block for block in top if block], entries, text[pos:].strip()


def duplicates(entries: list[bibfix.Entry]) -> list[str]:
    """"a and b: same key / DOI / title" for each pair that is probably one work twice."""
    groups: dict[tuple[str, str], list[str]] = {}
    for entry in entries:
        fields = {field.name: field.value for field in entry.fields}
        doi = re.sub(r"^(?:https?://(?:dx\.)?doi\.org/|doi:)", "", fields.get("doi", "").strip().lower())
        title = re.sub(r"[^a-z0-9]", "", fields.get("title", "").lower())
        for kind, value in (("key", entry.key.lower()), ("DOI", doi), ("title", title)):
            if value:
                groups.setdefault((kind, value), []).append(entry.key)
    return [f"{' and '.join(keys)}: same {kind}" for (kind, _value), keys in groups.items() if len(keys) > 1]


def _content(text: str) -> Counter:
    """What tidy must keep: each entry's type, key and (field, value) pairs, and every other non-blank line."""
    _top, entries, _tail = _blocks(text)
    counted: Counter = Counter()
    for entry, _gap in entries:
        counted[(entry.kind, entry.key, tuple(sorted((field.name, text[field.value_start:field.value_end].strip())
                                                     for field in entry.fields)))] += 1
    rest = text
    for entry, _gap in reversed(entries):
        rest = rest[:entry.start] + rest[entry.end:]
    counted.update(line.strip() for line in rest.splitlines() if line.strip())
    return counted


def tidy(text: str, order: str = "key") -> tuple[str, list[str]]:
    """
    (tidied text, possible duplicates). order: "key", "author" (first author, then year), "year" or "none".
    Raises ValueError if the result would lose or change anything (the caller then keeps the file as it is).
    """
    if order not in ORDERS:
        raise ValueError(f"unknown order {order!r} (use {', '.join(ORDERS)})")
    eol = "\r\n" if "\r\n" in text else "\n"
    top, entries, tail = _blocks(text)
    if order != "none":
        entries.sort(key=lambda item: sort_key(item[0], order))
    parts = [*top]
    for entry, gap in entries:
        parts.append((gap + eol if gap else "") + format_entry(text, entry, eol))
    if tail:
        parts.append(tail)
    out = (eol + eol).join(parts) + eol if parts else text
    if _content(out) != _content(text):
        raise ValueError("tidying would change the content (an entry the parser cannot read?); left as it is")
    return out, duplicates([entry for entry, _gap in entries])
