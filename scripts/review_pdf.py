"""
The review PDF (build.py --review-pdf, the editor's "Build with suggestions"): a document's open suggestions inlined
as \\lpdel{old}\\lpadd{new} and its unresolved comments as \\lpnote{...}, for a copy of the document that build.py
typesets with review = "show". Stdlib only; reads review.json (scripts/review.py owns the format) and never writes
the document itself.

Anchors are found the way serve_ui/anchors.js finds them (locate, the same rules). A change is inlined only where it
is plain prose: in the body of main.tex or a file without a preamble, outside math, verbatim, comments, command
names and the arguments of commands other than PROSE_ARGS, and with old and new text free of TeX specials. Anything
else is skipped with a reason, never guessed. A grouped suggestion (one multi-cursor edit) goes in whole or not at all.
"""

from __future__ import annotations

import bisect
import re
import unicodedata

PROSE_ARGS = {"emph", "textbf", "textit", "textsl", "textsc", "textup", "textmd", "textrm", "textsf", "footnote"}
NON_PROSE_ENVS = {
    "equation", "align", "alignat", "gather", "multline", "flalign", "eqnarray", "math", "displaymath",
    "verbatim", "Verbatim", "BVerbatim", "lstlisting", "minted", "comment", "filecontents", "tikzpicture",
    "pgfpicture",
}
COMMAND = re.compile(r"\\([A-Za-z@]+\*?|.)", re.DOTALL)
BEGIN = re.compile(r"\\begin\s*\{([^{}]*)\}")
BEGIN_DOCUMENT = re.compile(r"^[^%\n]*?\\begin\{document\}", re.MULTILINE)
PREAMBLE_FILE = re.compile(r"\\(?:documentclass|usepackage|RequirePackage|ProvidesPackage|ProvidesClass)\b")
ESCAPED = re.compile(r"\\[%&$#_]")
SPECIAL = re.compile(r"[\\{}$&#^_%]|\n[ \t]*\n")
TEX_ESCAPE = {"\\": r"\textbackslash{}", "{": r"\{", "}": r"\}", "$": r"\$", "&": r"\&", "#": r"\#",
              "^": r"\^{}", "_": r"\_", "~": r"\~{}", "%": r"\%"}
NOT_PLAIN = "not plain text (math, verbatim, a comment, a command or its argument, or the preamble)"


# --- anchors (a port of serve_ui/anchors.js) ---------------------------------------------------------------------

def from_utf16(text: str, unit: int) -> int:
    """The code point index of a UTF-16 offset (the editor counts in UTF-16)."""
    units = [i + k for k, i in enumerate(i for i, c in enumerate(text) if ord(c) > 0xFFFF)]
    return min(max(unit - bisect.bisect_right(units, unit - 2), 0), len(text))


def locate(text: str, anchor) -> tuple[int, int] | None:
    """Where the anchored text is now, (from, to), or None: anchors.js locate()."""
    if not isinstance(anchor, dict) or not isinstance(anchor.get("quote"), str):
        return None
    quote, prefix, suffix = anchor["quote"], anchor.get("prefix") or "", anchor.get("suffix") or ""
    size, whole = len(quote), len(prefix) + len(suffix)

    def score(at: int) -> int:
        tail = 0
        while tail < len(prefix) and at - tail - 1 >= 0 and text[at - tail - 1] == prefix[-1 - tail]:
            tail += 1
        head, end = 0, at + size
        while head < len(suffix) and end + head < len(text) and text[end + head] == suffix[head]:
            head += 1
        return tail + head

    start = anchor.get("from")
    start = from_utf16(text, start) if isinstance(start, int) and not isinstance(start, bool) and start >= 0 else -1
    if start >= 0 and text[start:start + size] == quote and start + size <= len(text) \
            and (size >= 8 or score(start) * 2 >= whole):
        return start, start + size
    seen: list[int] = []
    needles = [(quote, 0)] if size else [(prefix, len(prefix)), (suffix, 0)]
    for needle, shift in needles:
        if not needle:
            continue
        at = text.find(needle)
        while at >= 0 and len(seen) < 2000:
            seen.append(at + shift)
            at = text.find(needle, at + 1)
    best, best_score, best_dist = None, -1, None
    for at in seen:
        s, d = score(at), abs(at - start)
        if s > best_score or (s == best_score and d < best_dist):
            best, best_score, best_dist = at, s, d
    if best is None:
        return None
    need = 0 if size >= 8 else min(whole, 6 if size else 10)  # a short quote needs its context to agree
    return (best, best + size) if best_score >= need else None


# --- where plain prose is ----------------------------------------------------------------------------------------

def prose_mask(text: str, main: bool) -> bytearray:
    """1 for each character of plain prose. main: main.tex (prose only after \\begin{document})."""
    mask = bytearray(len(text))
    start, end = 0, len(text)
    if main:
        found = BEGIN_DOCUMENT.search(text)
        if not found:
            return mask
        start = found.end()
        stop = text.find("\\end{document}", start)
        end = stop if stop >= 0 else end
    elif PREAMBLE_FILE.search(text):
        return mask  # a preamble or a class read with \input: no prose
    groups: list[bool] = []  # per open brace: inside prose
    command = None  # the command a following { or [ belongs to
    i = start
    while i < end:
        char = text[i]
        if char == "\\":
            found = COMMAND.match(text, i)
            name = found.group(1)
            if name in ("%", "&", "$", "#", "_"):  # an escaped character is prose
                mask[i:found.end()] = b"\1\1" if all(groups) else b"\0\0"
                i, command = found.end(), None
                continue
            if name in ("verb", "verb*") and found.end() < end:
                close = text.find(text[found.end()], found.end() + 1)
                i, command = (close + 1 if close >= 0 else end), None
                continue
            if name in ("(", "["):
                close = text.find("\\)" if name == "(" else "\\]", found.end())
                i, command = (close + 2 if close >= 0 else end), None
                continue
            if name == "begin":
                env = BEGIN.match(text, i)
                if env and env.group(1).rstrip("*") in NON_PROSE_ENVS:
                    close = text.find(f"\\end{{{env.group(1)}}}", env.end())
                    i, command = (close + len(env.group(1)) + 6 if close >= 0 else end), None
                    continue
            i, command = found.end(), name
            continue
        if char == "%":
            close = text.find("\n", i)
            i = close if close >= 0 else end
            continue
        if char == "$":
            double = text.startswith("$$", i)
            close = text.find("$$" if double else "$", i + (2 if double else 1))
            while close > 0 and text[close - 1] == "\\" and not double:
                close = text.find("$", close + 1)
            i, command = (close + (2 if double else 1) if close >= 0 else end), None
            continue
        if char == "{":
            groups.append(command in PROSE_ARGS and all(groups))
            i += 1
            continue
        if char == "}":
            if groups:
                groups.pop()
            i += 1
            continue  # command stays: \cmd{a}{b} gives b to \cmd too
        if char == "[" and command is not None:  # an optional argument
            depth, j = 0, i
            while j < end:
                depth += {"[": 1, "]": -1}.get(text[j], 0)
                if depth == 0:
                    break
                j += 1
            i = j + 1
            continue
        if char.isspace():
            mask[i] = all(groups)
            i += 1
            continue  # \item [x] and \cmd {x}: spaces keep the command
        mask[i] = char not in "&#^_" and all(groups)
        command = None
        i += 1
    return mask


def plain(text: str) -> bool:
    """No TeX specials besides \\% \\& \\$ \\# \\_, and no blank line (ulem's marks cannot span paragraphs)."""
    return not SPECIAL.search(ESCAPED.sub("", text))


def fits(text: str, mask: bytearray, start: int, end: int) -> bool:
    """Can [start, end) of text become \\lpdel{...}\\lpadd{...}: plain prose, and not glued to a command name?"""
    if not all(mask[start:end]):
        return False
    if start > 0 and mask[start - 1]:
        return True
    before = text[start - 1] if start > 0 else ""
    return start < len(text) and bool(mask[start]) and not (before.isalpha() or before in "@*")


def escape(text: str, ascii_only: bool) -> str:
    """Comment text (not LaTeX) for \\lpnote: specials escaped, whitespace folded; ASCII for pdfLaTeX."""
    text = " ".join(text.split())
    if ascii_only:
        text = "".join(c for c in unicodedata.normalize("NFKD", text) if not unicodedata.combining(c))
        text = text.encode("ascii", "replace").decode()  # a character pdfLaTeX has no glyph for would stop the build
    return "".join(TEX_ESCAPE.get(c, c) for c in text)


# --- the plan ----------------------------------------------------------------------------------------------------

def plan(data: dict, read, notes: bool = True, ascii_only: bool = False, main: str = "main.tex") -> dict:
    """
    data: review.json; read(path) -> the file's text or None. Returns {"texts": {path: new text}, "applied": n,
    "notes": n, "skipped": [{"id", "kind", "path", "reason"}]}.
    """
    texts: dict[str, str] = {}
    masks: dict[str, bytearray] = {}
    placed: dict[str, list[tuple[int, int, str]]] = {}
    skipped: list[dict] = []
    applied = noted = 0

    def load(path) -> str | None:
        if not isinstance(path, str) or not path.endswith(".tex"):
            return None
        if path not in texts:
            text = read(path)
            if text is None:
                return None
            texts[path], masks[path], placed[path] = text, prose_mask(text, path == main), []
        return texts[path]

    def skip(item: dict, kind: str, reason: str) -> None:
        skipped.append({"id": item.get("id"), "kind": kind, "path": item.get("path"), "reason": reason})

    for item in data.get("suggestions") or []:
        if not isinstance(item, dict):
            continue
        text = load(item.get("path"))
        if text is None:
            skip(item, "suggestion", "not a .tex file of the document")
            continue
        parts = [item, *(p for p in item.get("more") or [] if isinstance(p, dict))]
        edits, reason = [], None
        for part in parts:
            spot, new = locate(text, part.get("anchor")), part.get("insert")
            if spot is None or not isinstance(new, str):
                reason = "its text is no longer in the file"
                break
            old = text[spot[0]:spot[1]]
            if not (plain(old) and plain(new) and fits(text, masks[item["path"]], *spot)):
                reason = NOT_PLAIN
                break
            markup = old if old == new else (f"\\lpdel{{{old}}}" if old else "") + (f"\\lpadd{{{new}}}" if new else "")
            edits.append((*spot, markup))
        if reason is None and clash(edits, placed[item["path"]]):
            reason = "overlaps another suggestion"
        if reason:
            skip(item, "suggestion", reason)
            continue
        placed[item["path"]] += edits
        applied += 1

    for item in (data.get("threads") or []) if notes else []:
        if not isinstance(item, dict) or item.get("resolved") or not item.get("comments"):
            continue
        text = load(item.get("path"))
        spot = locate(text, item.get("anchor")) if text is not None else None
        if spot is None:
            skip(item, "comment", "its text is no longer in the file" if text is not None
                 else "not a .tex file of the document")
            continue
        at = spot[1]
        if not fits(text, masks[item["path"]], at, at) or any(a < at < b for a, b, _ in placed[item["path"]]):
            skip(item, "comment", NOT_PLAIN if not fits(text, masks[item["path"]], at, at)
                 else "inside a suggestion")
            continue
        said = " / ".join(f"{c.get('name') or '?'}: {c.get('text') or ''}" for c in item["comments"]
                          if isinstance(c, dict))
        placed[item["path"]].append((at, at, f"\\lpnote{{{escape(said, ascii_only)}}}"))
        noted += 1

    changed = {path: apply(texts[path], edits) for path, edits in placed.items() if edits}
    return {"texts": changed, "applied": applied, "notes": noted, "skipped": skipped}


def clash(edits: list[tuple[int, int, str]], placed: list[tuple[int, int, str]]) -> bool:
    """Do edits overlap each other or placed ones? (anchors.js disjoint: two insertions at one place clash too.)"""
    spans = sorted([*edits, *placed])
    for (a, b, _), (c, d, _) in zip(spans, spans[1:]):
        if c < b or (c == b and a == b == c == d):
            return True
    return False


def apply(text: str, edits: list[tuple[int, int, str]]) -> str:
    out, pos = [], 0
    for start, end, markup in sorted(edits, key=lambda e: (e[0], e[1])):
        out += [text[pos:start], markup]
        pos = end
    return "".join(out) + text[pos:]
