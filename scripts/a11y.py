"""
Accessibility checks for documents built with build.toml `tagged = true` (build.py runs them after the build).

Each finding is {kind, file, line, message, hint}; file is relative to the document directory (None for the
PDF itself). The checks read the sources (main.tex and every file it \\input s, \\include s or loads from its
own directory) and the PDF's metadata; they never run TeX. Stdlib only, Python 3.9.
"""

from __future__ import annotations

import re
from pathlib import Path, PurePosixPath

# Packages and classes that the LaTeX Tagging Project lists as "currently-incompatible" or "no-support":
# a curated subset of commonly used ones (the page lists over 2000), from
# https://latex3.github.io/tagging-project/tagging-status/ as fetched on 2026-10-11.
# shortcut: a fixed list goes stale as packages gain tagging support; refresh it from that page with each release.
INCOMPATIBLE = {
    # packages
    "adjustbox": "currently-incompatible", "algorithm2e": "currently-incompatible",
    "authblk": "currently-incompatible", "caption": "currently-incompatible",
    "float": "currently-incompatible", "floatrow": "no-support", "framed": "currently-incompatible",
    "hyperxmp": "no-support", "lineno": "currently-incompatible", "listings": "currently-incompatible",
    "mdframed": "no-support", "minted": "currently-incompatible", "multirow": "currently-incompatible",
    "pdfpages": "currently-incompatible", "pdfx": "no-support", "pgfplots": "currently-incompatible",
    "soul": "currently-incompatible", "subcaption": "currently-incompatible", "subfig": "currently-incompatible",
    "tabu": "no-support", "threeparttable": "currently-incompatible", "tikz-cd": "currently-incompatible",
    "titlesec": "currently-incompatible", "titletoc": "currently-incompatible",
    "todonotes": "currently-incompatible", "ulem": "currently-incompatible", "wrapfig": "currently-incompatible",
    # classes
    "beamer": "no-support", "IEEEtran": "currently-incompatible", "llncs": "currently-incompatible",
    "memoir": "currently-incompatible", "revtex4-2": "currently-incompatible", "scrartcl": "currently-incompatible",
    "scrbook": "currently-incompatible",
}
STATUS_URL = "https://latex3.github.io/tagging-project/tagging-status/"
# What that page says about a class, quoted where a generic "incompatible" would hide the advice (same fetch).
NOTES = {
    "beamer": "The Tagging Project lists beamer as no-support (entry of 2025-06-11): its internals rebox "
              "material repeatedly in a way that is not compatible with any form of tagging support; it recommends "
              "the ltx-talk class (listed as compatible) for new tagged presentations.",
}

LEVELS = {"part": -1, "chapter": 0, "section": 1, "subsection": 2, "subsubsection": 3, "paragraph": 4,
          "subparagraph": 5}
COMMENT = re.compile(r"(?<!\\)((?:\\\\)*)%[^\n]*")
OPTIONS = r"(?:\[(?P<options>(?:[^\[\]{}]|\{[^{}]*\})*)\])?"
TOKEN = re.compile(
    r"\\begin\s*\{frame\}(?P<frame>)|"
    r"\\(?P<heading>part|chapter|section|subsection|subsubsection|paragraph|subparagraph)\*?(?=\s*[\[{])"
    r"|\\(?:input|include|subfile)\s*\{(?P<input>[^{}]+)\}"
    rf"|\\includegraphics\*?\s*{OPTIONS}\s*\{{(?P<graphic>[^{{}}]+)\}}"
    r"|\\(?P<load>usepackage|RequirePackage|documentclass|LoadClass)\s*(?:\[[^\]]*\])?\s*\{(?P<names>[^{}]+)\}"
)
ALT_KEY = re.compile(r"(?:^|,)\s*(?:alt|actualtext|artifact)\s*(?:=|,|$)")
MAX_DEPTH = 20
# \begin{frame}<overlay>[options]<overlay>{Title}
FRAME_TITLE = re.compile(r"\s*(?:<[^>]*>)?\s*(?:\[[^\]]*\])?\s*(?:<[^>]*>)?\s*\{\s*[^}\s]")


def scan(doc_dir: Path, rel: str = "main.tex", seen: set | None = None, depth: int = 0) -> list[tuple]:
    """(kind, rel, line, value, options) in reading order: kind is heading, graphic or load; files are followed."""
    seen = set() if seen is None else seen
    path = (doc_dir / rel).resolve()
    # Only the document's own files: a shared editor's \input{../other/x} must not reach the log.
    if rel in seen or depth > MAX_DEPTH or doc_dir.resolve() not in path.parents or not path.is_file():
        return []
    seen.add(rel)
    text = COMMENT.sub(r"\1", path.read_text(encoding="utf-8", errors="replace"))
    found: list[tuple] = []
    for match in TOKEN.finditer(text):
        line = text.count("\n", 0, match.start()) + 1
        if match.group("frame") is not None:
            body = text[match.end():].split("\\end{frame}", 1)[0]
            titled = FRAME_TITLE.match(body) or "\\frametitle" in body \
                or re.search(r"\\(?:titlepage|maketitle)\b", body)
            found.append(("frame", rel, line, bool(titled), None))
        elif match.group("heading"):
            found.append(("heading", rel, line, match.group("heading"), None))
        elif match.group("input"):
            target = match.group("input").strip()
            if "\\" not in target and "#" not in target:
                target = target if target.endswith(".tex") else f"{target}.tex"
                found += scan(doc_dir, PurePosixPath(target).as_posix(), seen, depth + 1)
        elif match.group("graphic"):
            found.append(("graphic", rel, line, match.group("graphic").strip(), match.group("options") or ""))
        else:
            suffix = ".cls" if match.group("load") in ("documentclass", "LoadClass") else ".sty"
            for name in (part.strip() for part in match.group("names").split(",")):
                if name:
                    found.append(("load", rel, line, name, None))
                    found += scan(doc_dir, name + suffix, seen, depth + 1)  # a local .sty or .cls only
    return found


def title_set(pdf_text: bytes) -> bool | None:
    """Does the PDF carry a title (XMP dc:title, else the Info dictionary)? None if neither can be found."""
    xmp = re.search(rb"<dc:title>(.*?)</dc:title>", pdf_text, re.DOTALL)
    if xmp:
        return bool(re.search(rb"<rdf:li[^>]*>\s*[^<\s]", xmp.group(1)))
    info = re.search(rb"/Info\s+(\d+)\s+0\s+R", pdf_text)
    body = info and re.search(rb"(?<!\d)%s\s+0\s+obj\s*<<(.*?)>>\s*endobj" % info.group(1), pdf_text, re.DOTALL)
    if body:
        title = re.search(rb"/Title\s*(\((?:\\.|[^\\)])*\)|<[0-9A-Fa-f\s]*>)", body.group(1))
        if not title:
            return False
        value = re.sub(rb"\s", b"", title.group(1)[1:-1])
        return value not in (b"", b"FEFF", b"feff")
    if b"<x:xmpmeta" in pdf_text:
        return False  # XMP without a dc:title
    return None


def check(doc_dir: Path, pdf_text: bytes | None) -> list[dict]:
    """
    Findings for a tagged document: title, alt text, heading levels, packages the tagging code cannot handle,
    and for slides the Tagging Project's beamer status and frames without a title.
    """
    findings: list[dict] = []

    def add(kind: str, file: str | None, line: int | None, message: str, hint: str) -> None:
        findings.append({"kind": kind, "file": file, "line": line, "message": message, "hint": hint})

    if pdf_text is not None and title_set(pdf_text) is False:
        add("a11y-title", None, None, "The PDF has no title (pdftitle is empty).",
            "PDF/UA requires a title: add \\hypersetup{pdftitle={...}} to the preamble.")

    previous: tuple | None = None
    loaded: set[str] = set()
    for kind, rel, line, value, options in scan(doc_dir):
        if kind == "graphic" and "#" not in value and not ALT_KEY.search(options):
            add("a11y-alt", rel, line, f"\\includegraphics{{{value}}} has no alt text.",
                "Add alt={a short description} to its options, or artifact for a purely decorative image.")
        elif kind == "heading":
            level = LEVELS[value]
            if previous and level > LEVELS[previous[0]] + 1 and previous[0] != "part":
                add("a11y-heading", rel, line, f"\\{value} follows \\{previous[0]} ({previous[1]}:{previous[2]}): "
                    "a heading level is skipped.",
                    "Screen readers navigate by heading level; use the next level down, or change the earlier heading.")
            previous = (value, rel, line)
        elif kind == "frame" and not value:
            add("a11y-frame-title", rel, line, "A frame has no title.",
                "Slide titles are the headings screen readers navigate by: add \\frametitle{...} or "
                "\\begin{frame}{Title}.")
        elif kind == "load" and value in NOTES and value not in loaded:
            loaded.add(value)
            add("a11y-class", rel, line, NOTES[value],
                f"Consider ltx-talk for a tagged presentation; see {STATUS_URL}.")
        elif kind == "load" and value in INCOMPATIBLE and value not in loaded:
            loaded.add(value)
            add("a11y-package", rel, line, f"{value} is {INCOMPATIBLE[value]} with tagging "
                "(LaTeX Tagging Project).",
                f"The structure tree may be wrong or incomplete; see {STATUS_URL} for the package and alternatives.")
    return findings
