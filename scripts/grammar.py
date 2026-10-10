"""
Grammar checking for LaTeX sources with LanguageTool (stdlib only, used by ci_report.py and serve.py).

The LaTeX is reduced to plain text and every plain character keeps the source offset it came from, so a
LanguageTool match becomes file:line:col. Two backends, both LanguageTool's /v2/check:

- local: a server you run (default http://localhost:8081, LANGUAGETOOL_URL, or `grammar_url`). Text stays here.
- public: https://api.languagetool.org, only when asked for (build.toml `grammar = "public"` or the editor
  setting). The text is sent to languagetool.org; requests are chunked by paragraph, throttled to the free tier
  (20 requests, 75 KB per minute, 20 KB per request) and cached by paragraph.

With nothing configured the mode is "auto": local when a server answers /v2/languages within 300 ms, else off.
"""

from __future__ import annotations

import bisect
import collections
import hashlib
import json
import math
import os
import re
import threading
import time
import unicodedata
import urllib.error
import urllib.parse
import urllib.request
from typing import Callable, NamedTuple

DEFAULT_LOCAL = "http://localhost:8081"
PUBLIC_URL = "https://api.languagetool.org/v2/check"
PUBLIC_NOTICE = "Public mode sends your text to languagetool.org (api.languagetool.org)."
MODES = ("auto", "off", "local", "public")
PROBE_TIMEOUT = 0.3
CHUNK_BYTES = 15_000  # of text per request; the public limit is 20 KB
MAX_REPLACEMENTS = 3
MAX_MATCHES = 500  # per LanguageTool reply; more are noise and cost time
# Rules that misfire on text extracted from LaTeX (spacing, quotes, placeholders for math and references).
IGNORED_RULES = {
    "WHITESPACE_RULE", "CONSECUTIVE_SPACES", "SENTENCE_WHITESPACE", "COMMA_PARENTHESIS_WHITESPACE", "EN_QUOTES",
    "DOUBLE_PUNCTUATION", "UNLIKELY_OPENING_PUNCTUATION", "EN_UNPAIRED_BRACKETS", "ELLIPSIS",
}
IGNORED_CATEGORIES = {"TYPOGRAPHY"}
UNSAFE_REPLACE = re.compile(r"[\\{}$%&#~^_]")  # a source span holding these is not plain prose: no quick fix


class GrammarError(Exception):
    pass


class Finding(NamedTuple):
    file: str
    line: int
    col: int
    rule: str
    message: str
    replacements: tuple[str, ...]
    offset: int  # code points into the source text
    length: int
    text: str  # the source span the finding covers


# ---------------------------------------------------------------------------
# LaTeX -> plain text, each character mapped back to its source offset
# ---------------------------------------------------------------------------

MATH_ENVS = {"equation", "align", "alignat", "gather", "multline", "flalign", "eqnarray", "math", "displaymath"}
SKIP_ENVS = {
    "verbatim", "Verbatim", "lstlisting", "minted", "tikzpicture", "pgfpicture", "comment", "filecontents",
    "thebibliography", "algorithm", "algorithmic", "forest", "axis", "circuitikz",
}
ENV_ARGS = {"tabular": 1, "tabularx": 2, "longtable": 1, "minipage": 1, "multicols": 1, "tabu": 1}
KEEP_PARAGRAPH = {  # their text argument stands alone as a paragraph
    "part", "chapter", "section", "subsection", "subsubsection", "paragraph", "subparagraph", "caption",
    "footnote", "footnotetext", "title", "subtitle", "thanks",
}
PARAGRAPH_CMDS = {"par", "newpage", "clearpage", "item", "maketitle"}
SPACE_CMDS = {"quad", "qquad", "newline", "linebreak", "hfill", "space", "enspace"}
SKIP_ARGS = {  # name -> brace arguments to drop (optional [..] arguments are always dropped)
    "label": 1, "input": 1, "include": 1, "includegraphics": 1, "usepackage": 1, "documentclass": 1,
    "bibliography": 1, "bibliographystyle": 1, "addbibresource": 1, "href": 1, "hspace": 1, "vspace": 1,
    "setlength": 2, "addtolength": 2, "setcounter": 2, "newcommand": 2, "renewcommand": 2, "providecommand": 2,
    "newenvironment": 3, "renewenvironment": 3, "newtheorem": 2, "color": 1, "textcolor": 1, "definecolor": 3,
    "graphicspath": 1, "hypersetup": 1, "pagestyle": 1, "thispagestyle": 1, "lstset": 1, "tikzset": 1,
    "usetikzlibrary": 1, "DeclareMathOperator": 2, "geometry": 1, "bibitem": 1, "index": 1, "glsadd": 1,
}
PLACEHOLDER_ARG = {"texttt", "url", "path", "code", "nolinkurl"}  # a word we cannot judge
REFERENCE = {"ref", "eqref", "cref", "Cref", "autoref", "Autoref", "pageref", "nameref", "vref"}
CITE = re.compile(r"[A-Za-z]*cite[A-Za-z]*")
SYMBOLS = {
    "LaTeX": "LaTeX", "TeX": "TeX", "ldots": "…", "dots": "…", "textbackslash": "\\", "ss": "ß", "o": "ø",
    "O": "Ø", "ae": "æ", "AE": "Æ", "oe": "œ", "aa": "å", "AA": "Å", "l": "ł", "L": "Ł", "i": "i", "j": "j",
    "today": "today", "S": "§", "textendash": "–", "textemdash": "—",
}
ACCENT_MARKS = {"'": "́", '"': "̈", "`": "̀", "^": "̂", "~": "̃", "=": "̄", ".": "̇"}
ACCENT_NAMES = {"c": "̧", "v": "̌", "u": "̆", "H": "̋", "k": "̨", "r": "̊",
                "b": "̱", "d": "̣"}
NAME = re.compile(r"[A-Za-z]+")
ENV_NAME = re.compile(r"\s*\{([^{}]*)\}")
BLANK_LINE = re.compile(r"[ \t]*\n(?:[ \t]*\n)+")


class Extracted(NamedTuple):
    plain: str
    src: list[int]  # src[k] = offset in the source of plain[k]
    end: list[int]  # end[k] = offset just past the source that produced plain[k]


def match_brace(text: str, pos: int) -> int:
    """Index of the } closing the { at `pos` (len(text) if there is none). \\{ and \\} do not count."""
    depth, i, n = 0, pos, len(text)
    while i < n:
        c = text[i]
        if c == "\\":
            i += 2
            continue
        if c == "{":
            depth += 1
        elif c == "}":
            depth -= 1
            if depth == 0:
                return i
        i += 1
    return n


def find_unescaped(text: str, token: str, start: int) -> int:
    i = text.find(token, start)
    while i > 0:
        slashes = 0
        while i - 1 - slashes >= 0 and text[i - 1 - slashes] == "\\":
            slashes += 1
        if slashes % 2 == 0:
            break
        i = text.find(token, i + 1)
    return i


class _Plain:
    def __init__(self) -> None:
        self.chars: list[str] = []
        self.src: list[int] = []
        self.end: list[int] = []

    def add(self, text: str, at: int, end: int | None = None) -> None:
        for char in text:
            self.chars.append(char)
            self.src.append(at)
            self.end.append(at + 1 if end is None else end)

    def space(self, at: int) -> None:
        if self.chars and self.chars[-1] not in " \n":
            self.add(" ", at)

    def paragraph(self, at: int) -> None:
        while self.chars and self.chars[-1] == " ":
            self.chars.pop()
            self.src.pop()
            self.end.pop()
        if self.chars and "".join(self.chars[-2:]) != "\n\n":
            self.add("\n\n", at)

    def clear(self) -> None:
        self.chars.clear()
        self.src.clear()
        self.end.clear()


def skip_groups(text: str, i: int, count: int) -> int:
    """Past `count` {..} arguments of a command, and the [..] ones before them. A bare \\name counts as one."""
    n, first = len(text), True
    while True:
        j = i
        while j < n and text[j] in " \t":
            j += 1
        if text[j:j + 1] == "[" and (count > 0 or first):
            end = text.find("]", j)
            if end < 0 or BLANK_LINE.search(text, j, end):
                return i
            i = end + 1
        elif count > 0 and text[j:j + 1] == "{":
            i, count = match_brace(text, j) + 1, count - 1
        elif count > 0 and NAME.match(text, j + 1) and text[j:j + 1] == "\\":
            i, count = NAME.match(text, j + 1).end(), count - 1
        else:
            return i
        first = False


def extract(text: str) -> Extracted:
    """Prose of a LaTeX source: no comments, commands, math or code; headings, captions and footnotes kept."""
    out = _Plain()
    closers: set[int] = set()  # } that end a heading/caption/footnote: a paragraph break
    n, i = len(text), 0
    while i < n:
        c = text[i]
        if c == "%":
            end = text.find("\n", i)
            i = n if end < 0 else end
        elif c == "\\":
            i = _command(text, i, out, closers)
        elif c == "$":
            i = _dollar(text, i, out)
        elif c == "}":
            if i in closers:
                out.paragraph(i)
            i += 1
        elif c in "{#^_":
            i += 1
        elif c == "~":
            out.space(i)
            i += 1
        elif c == "&":
            out.paragraph(i)
            i += 1
        elif c in " \t\r\n":
            blank = BLANK_LINE.match(text, i)
            if blank:
                out.paragraph(i)
                i = blank.end()
            else:
                out.space(i)
                i += 1
        elif text.startswith("``", i):
            out.add("“", i, i + 2)
            i += 2
        elif text.startswith("''", i):
            out.add("”", i, i + 2)
            i += 2
        elif c == "`":
            out.add("‘", i)
            i += 1
        else:
            out.add(c, i)
            i += 1
    out.paragraph(n)
    while out.chars and out.chars[-1] in " \n":
        out.chars.pop()
        out.src.pop()
        out.end.pop()
    return Extracted("".join(out.chars), out.src, out.end)


def _dollar(text: str, i: int, out: _Plain) -> int:
    token = "$$" if text.startswith("$$", i) else "$"
    end = find_unescaped(text, token, i + len(token))
    if end < 0 or BLANK_LINE.search(text, i, end):
        return i + 1  # a stray $ never swallows a paragraph
    out.add("X", i, end + len(token))
    return end + len(token)


def _command(text: str, i: int, out: _Plain, closers: set[int]) -> int:
    n = len(text)
    if i + 1 >= n:
        return n
    nxt = text[i + 1]
    if nxt == "\\":
        out.paragraph(i)
        return skip_groups(text, i + 2 + (text[i + 2:i + 3] == "*"), 0)
    if nxt in "%&$#_{}":
        out.add(nxt, i + 1)
        return i + 2
    if nxt in "[(":
        end = find_unescaped(text, "\\]" if nxt == "[" else "\\)", i + 2)
        if end < 0:
            return i + 2
        out.add("X", i, end + 2)
        return end + 2
    if nxt in ACCENT_MARKS:
        return _accent(text, i, i + 2, ACCENT_MARKS[nxt], out)
    if not (nxt.isascii() and nxt.isalpha()):  # control words are ASCII letters; \é is a control symbol
        if nxt == " ":
            out.space(i)
        return i + 2  # \, \; \! \- \/ and the like
    name_match = NAME.match(text, i + 1)
    name, j = name_match.group(), name_match.end()
    if name in ACCENT_NAMES:
        return _accent(text, i, j, ACCENT_NAMES[name], out)
    if name in ("begin", "end"):
        return _environment(text, i, j, name == "begin", out)
    if name in ("verb", "lstinline"):
        j = skip_groups(text, j + (text[j:j + 1] == "*"), 0)
        if text[j:j + 1] == "{":
            close = match_brace(text, j)
        else:
            close = text.find(text[j:j + 1], j + 1) if text[j:j + 1] else -1
        out.add("X", i)
        return n if close < 0 else close + 1
    if name in KEEP_PARAGRAPH:
        out.paragraph(i)
        j = skip_groups(text, j + (text[j:j + 1] == "*"), 0)
        if text[j:j + 1] == "{":
            closers.add(match_brace(text, j))
        return j
    if name in PARAGRAPH_CMDS:
        out.paragraph(i)
        return skip_groups(text, j, 0) if name == "item" else j
    if name in SPACE_CMDS:
        out.space(i)
        return j
    if name in REFERENCE:
        out.add("1", i)
        return skip_groups(text, j + (text[j:j + 1] == "*"), 1)
    if CITE.fullmatch(name):
        out.add("[1]", i)
        return skip_groups(text, j + (text[j:j + 1] == "*"), 1)
    if name in PLACEHOLDER_ARG:
        out.add("X", i)
        return skip_groups(text, j + (text[j:j + 1] == "*"), 1)
    if name in SKIP_ARGS:
        return skip_groups(text, j + (text[j:j + 1] == "*"), SKIP_ARGS[name])
    if name in SYMBOLS:
        out.add(SYMBOLS[name], i)
    return j  # an unknown command: the name goes, braces after it stay as grouping around prose


def _accent(text: str, i: int, j: int, mark: str, out: _Plain) -> int:
    """\\'e, \\"{o}, \\c c: the letter with its accent as one character."""
    while j < len(text) and text[j] == " ":
        j += 1
    braced = text[j:j + 1] == "{"
    k = j + 1 if braced else j
    base = text[k:k + 1]
    if base == "\\" and text[k + 1:k + 2] in ("i", "j"):
        base, k = text[k + 1], k + 1
    if not base.isalpha():
        return j
    k += 1
    if braced:
        if text[k:k + 1] != "}":
            return j  # {\'ab}: not a single letter, leave it
        k += 1
    out.add(unicodedata.normalize("NFC", base + mark), i, k)
    return k


def _environment(text: str, i: int, j: int, begin: bool, out: _Plain) -> int:
    found = ENV_NAME.match(text, j)
    if not found:
        return j
    env, after = found.group(1).strip(), found.end()
    base = env.rstrip("*")
    if not begin:
        out.paragraph(i)
        return len(text) if env == "document" else after
    if env == "document":
        out.clear()  # everything before was preamble
        return after
    if env in SKIP_ENVS or base in MATH_ENVS:
        end = text.find(f"\\end{{{env}}}", after)
        if end < 0:
            return len(text)
        if base in MATH_ENVS:
            out.add("X", i)
        else:
            out.paragraph(i)
        return end + len(f"\\end{{{env}}}")
    out.paragraph(i)
    return skip_groups(text, after, ENV_ARGS.get(env, 0))


class LineIndex:
    def __init__(self, text: str) -> None:
        self.starts = [0] + [m.end() for m in re.finditer("\n", text)]

    def locate(self, offset: int) -> tuple[int, int]:
        line = bisect.bisect_right(self.starts, offset) - 1
        return line + 1, offset - self.starts[line] + 1


def utf16_length(text: str) -> int:
    return len(text) + sum(ord(c) > 0xFFFF for c in text)


def to_utf16(text: str, index: int) -> int:
    """UTF-16 code unit offset (what CodeMirror and LanguageTool count) of a code point index."""
    return index + sum(ord(c) > 0xFFFF for c in text[:index])


class Utf16Map:
    """from_utf16 for many offsets into one text: O(log n) each after one pass."""

    def __init__(self, text: str) -> None:
        self.units = [i + k for k, i in enumerate(i for i, c in enumerate(text) if ord(c) > 0xFFFF)]
        self.size = len(text)

    def index(self, unit: int) -> int:
        return min(max(unit - bisect.bisect_right(self.units, unit - 2), 0), self.size)


def from_utf16(text: str, unit: int) -> int:
    seen = 0
    for index, char in enumerate(text):
        if seen >= unit:
            return index
        seen += 2 if ord(char) > 0xFFFF else 1
    return len(text)


# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------

def endpoint(base: str) -> str:
    parts = urllib.parse.urlsplit(base.strip())
    if parts.scheme not in ("http", "https") or not parts.netloc:
        raise GrammarError(f"grammar_url must be an http(s) URL, not {base!r}")
    path = parts.path.rstrip("/")
    if not path.endswith("/v2/check"):
        path += "/v2/check"
    return urllib.parse.urlunsplit((parts.scheme, parts.netloc, path, "", ""))


def _opener(proxy: bool) -> urllib.request.OpenerDirector:
    # A local server must never be reached through a proxy: the text stays on this machine.
    return urllib.request.build_opener() if proxy else urllib.request.build_opener(urllib.request.ProxyHandler({}))


def probe(base: str, timeout: float = PROBE_TIMEOUT) -> bool:
    """Does a LanguageTool server answer /v2/languages at `base` within `timeout` seconds?"""
    try:
        url = endpoint(base).removesuffix("/check") + "/languages"
        with _opener(False).open(url, timeout=timeout) as reply:
            return reply.status == 200
    except (GrammarError, OSError, ValueError):
        return False


def resolve(mode: str | None, url: str | None = None, environ=None, probe_fn: Callable[[str], bool] | None = None
            ) -> tuple[str, str | None]:
    """(effective mode "off"/"local"/"public", endpoint URL). `auto` is local when a server answers, else off."""
    environ = os.environ if environ is None else environ
    mode = (mode or "auto").lower()
    if mode not in MODES:
        raise GrammarError(f"grammar must be one of {', '.join(MODES)}, not {mode!r}")
    if mode == "off":
        return "off", None
    if mode == "public":
        return "public", PUBLIC_URL
    base = url or environ.get("LANGUAGETOOL_URL") or DEFAULT_LOCAL
    if mode == "local":
        return "local", endpoint(base)
    return ("local", endpoint(base)) if (probe_fn or probe)(base) else ("off", None)


def validate_settings(settings: dict) -> None:
    """Raise GrammarError for bad `grammar`, `grammar_url` or `disabled_rules` values from build.toml."""
    if settings.get("grammar", "auto") not in MODES:
        raise GrammarError(f"grammar must be one of {', '.join(MODES)}")
    url = settings.get("grammar_url")
    if url is not None:
        endpoint(url if isinstance(url, str) else "")
    rules = settings.get("disabled_rules", [])
    if not (isinstance(rules, list) and all(isinstance(r, str) for r in rules)):
        raise GrammarError("disabled_rules must be a list of strings")


# ---------------------------------------------------------------------------
# HTTP, throttling, cache
# ---------------------------------------------------------------------------

def post_form(url: str, fields: dict, proxy: bool = False, timeout: float = 30.0) -> dict:
    """POST application/x-www-form-urlencoded and return the JSON reply. Tests replace this function."""
    request = urllib.request.Request(
        url, urllib.parse.urlencode(fields).encode("utf-8"),
        {"Accept": "application/json", "User-Agent": "latex-pipeline-grammar"},
    )
    try:
        with _opener(proxy).open(request, timeout=timeout) as reply:
            return json.loads(reply.read().decode("utf-8"))
    except urllib.error.HTTPError as error:
        if error.code == 429:
            raise GrammarError("LanguageTool says too many requests (HTTP 429); try again in a minute.")
        raise GrammarError(f"LanguageTool answered HTTP {error.code}.")
    except (OSError, ValueError) as error:
        raise GrammarError(f"LanguageTool is not reachable: {error}")


class Limiter:
    """The free tier's window: at most `requests` and `size` bytes in any `window` seconds."""

    def __init__(self, requests: int = 20, size: int = 70_000, window: float = 60.0,
                 clock=time.monotonic, sleep=time.sleep) -> None:
        self.requests, self.size, self.window, self.clock, self.sleep = requests, size, window, clock, sleep
        self.hits: collections.deque = collections.deque()
        self.lock = threading.Lock()

    def acquire(self, size: int, max_wait: float | None = None) -> None:
        with self.lock:  # one waiter at a time: the sleeper holds the line
            while True:
                now = self.clock()
                while self.hits and now - self.hits[0][0] >= self.window:
                    self.hits.popleft()
                if len(self.hits) < self.requests and sum(s for _, s in self.hits) + size <= self.size:
                    self.hits.append((now, size))
                    return
                wait = self.window - (now - self.hits[0][0])
                if max_wait is not None and wait > max_wait:
                    raise GrammarError(f"The free LanguageTool limit is reached; try again in {math.ceil(wait)} s.")
                self.sleep(max(wait, 0.01))


PUBLIC_LIMITER = Limiter()
CACHE: collections.OrderedDict = collections.OrderedDict()  # paragraph hash -> raw matches
CACHE_SIZE = 4000
CACHE_LOCK = threading.Lock()


def _cache_get(key: str):
    with CACHE_LOCK:
        if key in CACHE:
            CACHE.move_to_end(key)
            return CACHE[key]
    return None


def _cache_put(key: str, value: list) -> None:
    with CACHE_LOCK:
        CACHE[key] = value
        while len(CACHE) > CACHE_SIZE:
            CACHE.popitem(last=False)


# ---------------------------------------------------------------------------
# Checking
# ---------------------------------------------------------------------------

def split_paragraphs(plain: str) -> list[tuple[int, str]]:
    """(start, text) of each paragraph; one longer than CHUNK_BYTES is cut at spaces."""
    pieces = []
    for match in re.finditer(r"\S(?:.|\n(?!\n))*", plain):
        start, text = match.start(), match.group()
        while len(text.encode("utf-8")) > CHUNK_BYTES:
            cut = text.rfind(" ", 0, CHUNK_BYTES // 4)  # characters, so a multi-byte text still fits
            cut = cut if cut > 0 else CHUNK_BYTES // 4
            pieces.append((start, text[:cut]))
            start, text = start + cut, text[cut:]
        if text.strip():
            pieces.append((start, text))
    return pieces


def _raw_matches(reply: dict, chunk: str) -> list[tuple[int, int, str, str, str, tuple[str, ...]]]:
    found = []
    units = Utf16Map(chunk)
    for match in reply.get("matches", [])[:MAX_MATCHES]:
        rule = match.get("rule") or {}
        category = (rule.get("category") or {}).get("id", "")
        start = units.index(int(match.get("offset", 0)))
        end = units.index(int(match.get("offset", 0)) + int(match.get("length", 0)))
        replacements = tuple(r.get("value", "") for r in match.get("replacements", [])[:MAX_REPLACEMENTS])
        found.append((start, end, rule.get("id", "UNKNOWN"), str(match.get("message", "")), category, replacements))
    return found


def check_plain(plain: str, *, url: str, public: bool, lang: str, disabled: set[str], max_wait: float | None = None,
                ) -> list[tuple[int, int, str, str, tuple[str, ...]]]:
    """LanguageTool matches (plain start, plain end, rule, message, replacements) for extracted text."""
    off = IGNORED_RULES | disabled
    salt = f"{lang}\0{','.join(sorted(off))}\0"
    pending: list[tuple[int, str, str]] = []  # (start, paragraph, cache key)
    matches: list[tuple[int, int, str, str, tuple[str, ...]]] = []
    for start, paragraph in split_paragraphs(plain):
        key = hashlib.sha1((salt + paragraph).encode("utf-8")).hexdigest()
        cached = _cache_get(key)
        if cached is None:
            pending.append((start, paragraph, key))
        else:
            matches += [(start + a, start + b, *rest) for a, b, *rest in cached]

    batches: list[list[tuple[int, str, str]]] = []
    size = 0
    for item in pending:
        length = len(item[1].encode("utf-8")) + 2
        if not batches or size + length > CHUNK_BYTES:
            batches.append([])
            size = 0
        batches[-1].append(item)
        size += length

    for batch in batches:
        chunk = "\n\n".join(paragraph for _, paragraph, _ in batch)
        if public:
            PUBLIC_LIMITER.acquire(len(chunk.encode("utf-8")), max_wait)
        fields = {"language": lang or "auto", "text": chunk, "disabledRules": ",".join(sorted(off)),
                  "disabledCategories": ",".join(sorted(IGNORED_CATEGORIES))}
        raw = _raw_matches(post_form(url, fields, proxy=public), chunk)
        position, spans = 0, []
        for start, paragraph, key in batch:
            spans.append((position, position + len(paragraph), start, key))
            position += len(paragraph) + 2
        lows = [span[0] for span in spans]
        per_paragraph: dict[str, list] = {key: [] for _, _, _, key in spans}
        for a, b, rule, message, category, replacements in raw:
            if rule in off or category in IGNORED_CATEGORIES:
                continue
            low, high, start, key = spans[max(bisect.bisect_right(lows, a) - 1, 0)]
            if low <= a and b <= high:  # a match across two paragraphs is dropped
                per_paragraph[key].append((a - low, b - low, rule, message, replacements))
                matches.append((start + a - low, start + b - low, rule, message, replacements))
        for _, _, _, key in spans:
            _cache_put(key, per_paragraph[key])
    return sorted(matches)


def _exact(span: str, plain: str) -> bool:
    """Does the source span read as exactly this plain text (quotes and whitespace aside)? Else no quick fix."""
    def norm(t: str) -> str:
        return " ".join(t.replace("``", "“").replace("''", "”").replace("`", "‘").replace("~", " ").split())
    return norm(span) == norm(plain)


def check_source(text: str, path: str, *, url: str, public: bool, lang: str = "auto", disabled=(),
                 max_wait: float | None = None) -> list[Finding]:
    """Findings for one LaTeX file, positioned in `text`."""
    extracted = extract(text)
    index = LineIndex(text)
    findings = []
    for start, end, rule, message, replacements in check_plain(
            extracted.plain, url=url, public=public, lang=lang, disabled=set(disabled), max_wait=max_wait):
        if end <= start:
            continue
        first, last = extracted.src[start], extracted.end[end - 1]
        span = text[first:last]
        if (UNSAFE_REPLACE.search(span) or "\n" in span and "\n\n" in span
                or not _exact(span, extracted.plain[start:end])):
            replacements = ()
        line, col = index.locate(first)
        findings.append(Finding(path, line, col, rule, message, replacements, first, last - first, span))
    return findings
