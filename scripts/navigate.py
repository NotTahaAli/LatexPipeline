"""
Navigation index for the editor (imported by serve.py only): where labels, citation keys, user commands and
environments are defined and used, figure and table captions, TODO notes, the \\input/\\include/\\bibliography/
\\includegraphics graph and the numbers the last build gave each label (its .aux files). Pure parsing of text it
is handed: it runs no TeX, reads and writes no file. Columns count UTF-16 units, as the editor does.
stdlib-only, Python 3.9.
"""
from __future__ import annotations

import bisect
import posixpath
import re

INDEXED = re.compile(r"\.(tex|ltx|sty|cls|bib)$", re.I)
MAX_ITEMS = 3000  # per list; more is marked truncated
REF_CMDS = r"ref|eqref|cref|Cref|autoref|pageref|nameref|vref|Vref|vpageref|labelcref|cpageref|Cpageref|zref"
CITE_CMDS = (r"cite[A-Za-z]*|[Cc]itep?|[Cc]itet|[Pp]arencite|[Tt]extcite|[Aa]utocite|[Ff]ootcite|nocite|smartcite"
             r"|supercite|fullcite|footfullcite")
REFS = re.compile(r"\\(?:" + REF_CMDS + r")\*?\s*\{([^{}]*)\}")
CITES = re.compile(r"\\(?:" + CITE_CMDS + r")\*?(?:\s*\[[^\]]*\])*\s*\{([^{}]*)\}")
LABEL = re.compile(r"\\label\s*\{([^{}]+)\}")
NEW_COMMAND = re.compile(
    r"\\(?:(?:re)?newcommand|providecommand|DeclareMathOperator|DeclareRobustCommand"
    r"|(?:New|Renew|Provide|Declare)(?:Document|Expandable)Command)\*?\s*\{?\s*\\([A-Za-z]+)(?![A-Za-z])"
    r"|\\[gex]?def\s*\\([A-Za-z]+)(?![A-Za-z])")
NEW_ENV = re.compile(
    r"\\(?:(?:re)?newenvironment|(?:New|Renew|Provide|Declare)DocumentEnvironment|newtheorem|declaretheorem"
    r"|newtcolorbox|newmdenv)\*?\s*(?:\[[^\]]*\]\s*)?\{([A-Za-z*]+)\}")
BIB_ENTRY = re.compile(r"@(\w+)\s*[{(]\s*([^,\s]+)\s*,")
FLOAT = re.compile(r"\\begin\s*\{(figure|table|algorithm|listing)(\*?)\}")
CAPTION = re.compile(r"\\caption\s*(?:\[[^\]]*\])?\s*\{")
TODO_NOTE = re.compile(r"%+\s*(TODO|FIXME)\b[:\s]*(.*)")
TODO_CMD = re.compile(r"\\todo\s*(?:\[[^\]]*\])?\s*\{")
INCLUDES = re.compile(r"\\(input|include|subfile|InputIfFileExists)\s*\{([^{}]+)\}")
BIBS = re.compile(r"\\(bibliography|addbibresource)\s*(?:\[[^\]]*\])?\s*\{([^{}]+)\}")
GRAPHICS = re.compile(r"\\includegraphics\s*\*?(?:\s*\[[^\]]*\])*\s*\{([^{}]+)\}")
GRAPHICSPATH = re.compile(r"\\graphicspath\s*\{((?:\s*\{[^{}]*\})*)\s*\}")
PACKAGES = re.compile(r"\\(usepackage|RequirePackage|documentclass|LoadClass)\s*(?:\[[^\]]*\])?\s*\{([^{}]+)\}")
NEWLABEL = re.compile(r"\\newlabel\{([^{}]+)\}\{\{((?:[^{}]|\{[^{}]*\})*)\}\{((?:[^{}]|\{[^{}]*\})*)\}")
IMAGE_EXTS = ("", ".pdf", ".png", ".jpg", ".jpeg", ".eps")
GOOD_LABEL = re.compile(r"[^\s{}\\,%#]{1,200}")


def mask(text: str) -> str:
    """text with every % comment blanked out (same length, so positions stay)."""
    return re.sub(r"(?<!\\)((?:\\\\)*)%[^\n]*", lambda m: m.group(1) + " " * (len(m.group(0)) - len(m.group(1))), text)


def utf16(text: str) -> int:
    return len(text.encode("utf-16-le")) // 2


class Where:
    """Offsets -> (line, UTF-16 column) for one file."""

    def __init__(self, text: str) -> None:
        self.text = text
        self.starts = [0] + [m.end() for m in re.finditer("\n", text)]

    def __call__(self, path: str, offset: int, length: int = 0, **extra) -> dict:
        line = bisect.bisect_right(self.starts, offset)
        start = self.starts[line - 1]
        return {"file": path, "line": line, "col": utf16(self.text[start:offset]),
                "len": utf16(self.text[offset:offset + length]), **extra}

    def line(self, number: int) -> str:
        end = self.starts[number] - 1 if number < len(self.starts) else len(self.text)
        return self.text[self.starts[number - 1]:end].strip()[:160]


def brace(text: str, start: int) -> int:
    """Index after the group whose { is at text[start - 1]; len(text) when it never closes."""
    depth, i = 1, start
    while i < len(text):
        c = text[i]
        if c == "\\":
            i += 2
            continue
        if c == "{":
            depth += 1
        elif c == "}":
            depth -= 1
            if not depth:
                return i + 1
        i += 1
    return len(text)


def plain(tex: str) -> str:
    """A caption or note as a line of text: commands dropped, arguments kept."""
    tex = re.sub(r"\\label\{[^{}]*\}", "", tex)
    tex = re.sub(r"\\[A-Za-z]+\*?(?:\[[^\]]*\])?", "", tex)
    return re.sub(r"\s+", " ", tex.replace("{", "").replace("}", "").replace("~", " ")).strip()[:200]


def keys(match: re.Match, group: int = 1):
    """(key, offset) for each comma-separated key of a group."""
    at = match.start(group)
    for part in match.group(group).split(","):
        key = part.strip()
        if key:
            yield key, at + part.index(key)
        at += len(part) + 1


def resolve(arg: str, exts, files: set) -> tuple[str, bool]:
    """The document file an argument names (TeX looks from the document's folder), and whether it exists."""
    base = posixpath.normpath(arg.strip().replace("\\", "/"))
    for ext in exts:
        if base + ext in files:
            return base + ext, True
    return base, False


def capped(items: list) -> list:
    return items[:MAX_ITEMS]


def index(texts: dict, paths, numbers: dict | None = None, uses: str | None = None) -> dict:
    """
    texts: path -> text of the document's .tex/.sty/.cls/.bib files; paths: every file of the document (for the
    graph); numbers: aux_numbers() of the last build; uses: "kind:name" (label, cite, command, env) to list every
    use of one name. Returns definitions, floats, todos, the file graph, the numbers and, when asked, the uses.
    """
    files = set(paths)
    defs, floats, todos, edges, found = [], [], [], [], []
    graphics_dirs = [""]
    for path, text in texts.items():
        if path.lower().endswith(".bib"):
            continue
        for m in GRAPHICSPATH.finditer(mask(text)):
            graphics_dirs += [d.strip().rstrip("/") + "/" for d in re.findall(r"\{([^{}]*)\}", m.group(1)) if d.strip()]
    want_kind, _, want = (uses or "").partition(":")
    for path, text in sorted(texts.items()):
        at = Where(text)
        if path.lower().endswith(".bib"):
            for m in BIB_ENTRY.finditer(text):
                if m.group(1).lower() not in ("string", "comment", "preamble"):
                    defs.append(at(path, m.start(2), len(m.group(2)), kind="cite", name=m.group(2)))
            continue
        code = mask(text)
        for m in NEW_COMMAND.finditer(code):
            group = 1 if m.group(1) else 2
            defs.append(at(path, m.start(group) - 1, len(m.group(group)) + 1, kind="command", name=m.group(group)))
        for m in NEW_ENV.finditer(code):
            defs.append(at(path, m.start(1), len(m.group(1)), kind="env", name=m.group(1)))
        for m in LABEL.finditer(code):
            for key, offset in keys(m):
                defs.append(at(path, offset, len(key), kind="label", name=key))
        for m in FLOAT.finditer(code):
            end = code.find("\\end{" + m.group(1) + m.group(2) + "}", m.end())
            body = code[m.end():len(code) if end < 0 else end]
            cap = CAPTION.search(body)
            label = LABEL.search(body)
            caption = plain(body[cap.end():brace(body, cap.end()) - 1]) if cap else ""
            floats.append(at(path, m.start(), 0, kind=m.group(1), caption=caption,
                             label=label.group(1).strip() if label else None))
        for m in TODO_NOTE.finditer(text):
            if code[m.start()] == " ":  # a comment, not an escaped \%
                todos.append(at(path, m.start(), len(m.group(0)), kind=m.group(1), text=m.group(2).strip()[:200]))
        for m in TODO_CMD.finditer(code):
            todos.append(at(path, m.start(), m.end() - m.start(), kind="todo",
                            text=plain(code[m.end():brace(code, m.end()) - 1])))
        for m in INCLUDES.finditer(code):
            to, ok = resolve(m.group(2), ("", ".tex"), files)
            edges.append({"from": path, "to": to, "kind": m.group(1), "missing": not ok})
        for m in BIBS.finditer(code):
            for key, _ in keys(m, 2):
                to, ok = resolve(key, ("", ".bib"), files)
                edges.append({"from": path, "to": to, "kind": "bibliography", "missing": not ok})
        for m in GRAPHICS.finditer(code):
            hits = [resolve(d + m.group(1), IMAGE_EXTS, files) for d in graphics_dirs]
            to, ok = next((h for h in hits if h[1]), hits[0])
            edges.append({"from": path, "to": to, "kind": "graphics", "missing": not ok})
        for m in PACKAGES.finditer(code):
            ext = ".cls" if m.group(1) in ("documentclass", "LoadClass") else ".sty"
            for key, _ in keys(m, 2):
                if key + ext in files:  # packages from TeX Live are not part of the graph
                    edges.append({"from": path, "to": key + ext, "kind": "package", "missing": False})
        if want_kind:
            found += uses_in(path, code, at, want_kind, want, defs)
    result = {
        "defs": capped(defs), "floats": capped(floats), "todos": capped(todos), "edges": capped(edges),
        "numbers": numbers or {}, "truncated": max(len(defs), len(floats), len(todos), len(edges)) > MAX_ITEMS,
    }
    if want_kind:
        result["uses"] = capped(found)
        result["uses_truncated"] = len(found) > MAX_ITEMS
    return result


def uses_in(path: str, code: str, at: Where, kind: str, name: str, defs: list) -> list:
    """Every place one label, citation key, user command or environment is used in one file (definitions apart),
    with the text of its line."""
    out = []
    if kind == "label":
        for m in REFS.finditer(code):
            out += [at(path, offset, len(key), kind=kind) for key, offset in keys(m) if key == name]
    elif kind == "cite":
        for m in CITES.finditer(code):
            out += [at(path, offset, len(key), kind=kind) for key, offset in keys(m) if key == name]
    elif kind == "command" and re.fullmatch(r"[A-Za-z]+", name):
        own = {(d["line"], d["col"]) for d in defs if d["file"] == path and d["kind"] == "command"}
        for m in re.finditer(r"\\" + name + r"(?![A-Za-z])", code):
            hit = at(path, m.start(), len(m.group(0)), kind=kind)
            if (hit["line"], hit["col"]) not in own:
                out.append(hit)
    elif kind == "env":
        for m in re.finditer(r"\\begin\s*\{" + re.escape(name) + r"\}", code):
            out.append(at(path, m.start(), len(m.group(0)), kind=kind))
    for hit in out:
        hit["text"] = at.line(hit["line"])
    return out


def aux_numbers(texts) -> dict:
    """label -> {"number", "page"} from the \\newlabel lines of .aux files (cleveref's @cref twins skipped)."""
    out = {}
    for text in texts:
        for m in NEWLABEL.finditer(text):
            if m.group(1).endswith("@cref"):
                continue
            number = re.sub(r"\\relax\s*|[{}]", "", m.group(2)).strip()
            out[m.group(1)] = {"number": number[:40], "page": re.sub(r"[{}]", "", m.group(3)).strip()[:20]}
    return out


def good_label(name) -> bool:
    return isinstance(name, str) and GOOD_LABEL.fullmatch(name) is not None


def rename_pattern(old: str) -> re.Pattern:
    """\\label{old} and each \\ref-like use of old (one key among several too); group "a" is what comes before it."""
    return re.compile(r"(?P<a>\\(?:label|" + REF_CMDS + r")\*?\s*\{(?:[^{}]*?,)?\s*)" + re.escape(old)
                      + r"(?=\s*[,}])")


def rename_template(new: str) -> str:
    """Replacement for rename_pattern (re.Match.expand); good_label() names hold no backslash."""
    return r"\g<a>" + new
