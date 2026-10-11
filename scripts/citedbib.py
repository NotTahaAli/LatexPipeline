"""
Cited-only .bib export: the entries a document actually cites, read from its last build (stdlib only, Python 3.9).

BibTeX's .aux files list the cited keys (\\citation{key}, \\citation{*} for \\nocite{*}) and the databases
(\\bibdata{refs}); biblatex's .bcf lists them as <bcf:citekey> and <bcf:datasource> (its .aux has \\abx@aux@cite).
The result keeps the @string and @preamble definitions of each database (cited entries may use them), every cited
entry as it is written, and the entries they inherit from (crossref, xdata). Only .bib files inside the document
directory are read. Nothing runs TeX and nothing is written here: build.py --cited-bib writes
out/<name>.cited.bib, serve.py offers the text as a download.
"""

from __future__ import annotations

import re
from pathlib import Path

import bibfix

MAX_AUX = 500  # .aux files read (one per \include)
MAX_BIB_CHARS = 5_000_000
AUX_CITE = re.compile(r"\\citation\{([^}]*)\}|\\abx@aux@cite\{[^}]*\}\{([^}]*)\}")
AUX_DATA = re.compile(r"\\bibdata\{([^}]*)\}")
BCF_CITE = re.compile(r"<bcf:citekey\b[^>]*>([^<]*)</bcf:citekey>")
BCF_DATA = re.compile(r'<bcf:datasource\b[^>]*type="file"[^>]*>([^<]*)</bcf:datasource>')
DEFINITIONS = re.compile(r"@[ \t]*(string|preamble)[ \t\r\n]*\{", re.I)
PARENTS = ("crossref", "xdata")


def _unxml(text: str) -> str:
    return text.replace("&lt;", "<").replace("&gt;", ">").replace("&quot;", '"').replace("&amp;", "&").strip()


def cited(build_dir: Path, stem: str) -> tuple[list[str], list[str]]:
    """(cited keys in first-cited order, "*" included when everything is cited; database names as written)."""
    keys: list[str] = []
    bibs: list[str] = []
    texts = []
    bcf = build_dir / f"{stem}.bcf"
    if bcf.is_file():
        text = bcf.read_text(encoding="utf-8", errors="replace")
        keys += [_unxml(k) for k in BCF_CITE.findall(text)]
        bibs += [_unxml(b) for b in BCF_DATA.findall(text)]
    for aux in sorted(build_dir.rglob("*.aux"))[:MAX_AUX]:
        texts.append(aux.read_text(encoding="utf-8", errors="replace"))
    for text in texts:
        for match in AUX_CITE.finditer(text):
            keys += [k.strip() for k in (match.group(1) or match.group(2) or "").split(",")]
        for match in AUX_DATA.finditer(text):
            bibs += [b.strip() for b in match.group(1).split(",")]
    return list(dict.fromkeys(k for k in keys if k)), list(dict.fromkeys(b for b in bibs if b))


def _database(root: Path, name: str) -> Path | None:
    """The .bib file a name stands for, only when it is inside the document directory."""
    path = (root / (name if name.lower().endswith(".bib") else name + ".bib")).resolve()
    try:
        path.relative_to(root.resolve())
    except ValueError:
        return None
    return path if path.is_file() else None


def _definitions(text: str) -> list[str]:
    out = []
    for match in DEFINITIONS.finditer(text):
        end = bibfix._balanced(text, match.end() - 1)
        out.append(text[match.start():end])
    return out


def export(root: Path, build_dir: Path, stem: str) -> dict:
    """{"text", "keys" (cited and written), "missing" (cited, in no database), "files", "notes"}. Raises OSError when
    the build left no .aux or .bcf to read."""
    if not (build_dir / f"{stem}.aux").is_file() and not (build_dir / f"{stem}.bcf").is_file():
        raise OSError("no finished build to read the citations from (.aux or .bcf); build the document first")
    keys, names = cited(build_dir, stem)
    every = "*" in keys
    wanted = [k for k in keys if k != "*"]
    notes: list[str] = []
    files: list[str] = []
    entries: list[tuple[bibfix.Entry, str]] = []
    definitions: list[str] = []
    size = 0
    for name in names:
        path = _database(root, name)
        if path is None:
            notes.append(f"{name}: not a .bib file inside the document directory; skipped.")
            continue
        text = path.read_text(encoding="utf-8", errors="replace")
        size += len(text)
        if size > MAX_BIB_CHARS:
            notes.append(f"{name}: the databases are too large (5 MB in all); the rest are skipped.")
            break
        files.append(path.relative_to(root.resolve()).as_posix())
        definitions += _definitions(text)
        entries += [(entry, text) for entry in bibfix.parse(text)]
    by_key: dict[str, tuple[bibfix.Entry, str]] = {}
    folded: dict[str, str] = {}
    for entry, text in entries:
        by_key.setdefault(entry.key, (entry, text))  # the first database wins, as in BibTeX
        folded.setdefault(entry.key.casefold(), entry.key)
    chosen = list(by_key) if every else []
    missing = []
    for key in wanted:
        real = key if key in by_key else folded.get(key.casefold())
        if real is None:
            missing.append(key)
        elif real not in chosen:
            chosen.append(real)
    i = 0
    while i < len(chosen):  # parents of chosen entries, and their parents
        entry, _ = by_key[chosen[i]]
        for field in entry.fields:
            if field.name in PARENTS:
                for parent in (p.strip() for p in field.value.split(",")):
                    real = parent if parent in by_key else folded.get(parent.casefold())
                    if real and real not in chosen:
                        chosen.append(real)
        i += 1
    parts = list(dict.fromkeys(definitions)) + [text[e.start:e.end] for e, text in (by_key[k] for k in chosen)]
    header = "% Cited entries only, written from the last build by LaTeX Pipeline.\n"
    return {"text": header + "\n\n".join(parts) + "\n", "keys": chosen, "missing": missing, "files": files,
            "notes": notes}
