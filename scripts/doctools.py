"""
Tools around a built document that never run TeX: desktop notifications (--watch --notify), dependency lists
(--deps), metadata (--query), page extracts (--pages) and a camera-ready preflight (--preflight).

Standard library only. build.py imports it only inside the functions that need it, so it is not a build input.
External programs are optional: each function says what it needs and degrades to a message without it.
"""

from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import sys
from pathlib import Path

NOTIFY_TIMEOUT = 15
# Windows has no notification command; a tray balloon through .NET works from PowerShell 5 and 7. The text comes
# from the environment, never from the script, so a document name cannot inject code.
POWERSHELL_NOTIFY = (
    "Add-Type -AssemblyName System.Windows.Forms, System.Drawing; "
    "$n = New-Object System.Windows.Forms.NotifyIcon; $n.Icon = [System.Drawing.SystemIcons]::Information; "
    "$n.Visible = $true; $n.ShowBalloonTip(5000, $env:LP_TITLE, $env:LP_MESSAGE, 'Info'); "
    "Start-Sleep -Seconds 6; $n.Dispose()"
)


def notify_command() -> list[str] | None:
    """The notification program of this platform, or None (then notify() does nothing)."""
    if sys.platform == "darwin" and shutil.which("osascript"):
        # "system attribute" reads an environment variable (AppleScript Standard Additions).
        return ["osascript", "-e",
                'display notification (system attribute "LP_MESSAGE") with title (system attribute "LP_TITLE")']
    if os.name == "nt":
        shell = shutil.which("powershell") or shutil.which("pwsh")
        return [shell, "-NoProfile", "-NonInteractive", "-Command", POWERSHELL_NOTIFY] if shell else None
    if shutil.which("notify-send"):
        return ["notify-send", "--app-name=LaTeX build", "--", "$LP_TITLE", "$LP_MESSAGE"]
    return None


def notify(title: str, message: str) -> bool:
    """A desktop notification, in the background. False (and silence) when the platform has no way to show one."""
    command = notify_command()
    if command is None:
        return False
    # notify-send takes the text as arguments; the others read it from the environment.
    command = [title if part == "$LP_TITLE" else message if part == "$LP_MESSAGE" else part for part in command]
    try:
        subprocess.Popen(command, env={**os.environ, "LP_TITLE": title, "LP_MESSAGE": message},
                         stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    except OSError:
        return False
    return True


# ---------------------------------------------------------------------------
# --deps
# ---------------------------------------------------------------------------

def make_escape(path: str) -> str:
    """A path in a Make rule: spaces, # and : escaped with a backslash, $ doubled."""
    return re.sub(r"([ #:])", r"\\\1", path.replace("$", "$$"))


def deps_text(target: str, inputs: list[str], form: str) -> str:
    """'out/x.pdf: in1 in2' (Make, one input per line) or a JSON object {"target", "inputs"}."""
    if form == "json":
        return json.dumps({"target": target, "inputs": inputs}, indent=2) + "\n"
    return make_escape(target) + ":" + "".join(f" \\\n  {make_escape(name)}" for name in inputs) + "\n"


# ---------------------------------------------------------------------------
# --query
# ---------------------------------------------------------------------------

def group(text: str, start: int) -> tuple[str, int] | None:
    """text[start] is "{" (after spaces): its content and the index past the matching "}"."""
    start = len(text) - len(text[start:].lstrip())
    if start >= len(text) or text[start] != "{":
        return None
    depth = 0
    for i in range(start, len(text)):
        depth += (text[i] == "{") - (text[i] == "}")
        if depth == 0:
            return text[start + 1:i], i + 1
    return None


def plain(tex: str) -> str:
    """Rough plain text of a LaTeX fragment: commands without arguments dropped, braces removed."""
    tex = re.sub(r"\\(?:thanks|footnote|label)\s*\{[^{}]*\}", "", tex)
    tex = re.sub(r"\\(?:and|\\)", ",", tex)
    tex = re.sub(r"\\[A-Za-z@]+\*?\s*", "", tex)
    tex = re.sub(r"[{}~]", lambda m: " " if m.group() == "~" else "", tex)
    return re.sub(r"\s*,\s*(,\s*)*", ", ", re.sub(r"\s+", " ", tex)).strip(" ,")


def command_argument(text: str, name: str) -> str | None:
    """The argument of the first \\name{...} (an optional [...] skipped), comments removed."""
    text = re.sub(r"(?<!\\)((?:\\\\)*)%[^\n]*", r"\1", text)
    match = re.search(rf"\\{name}\s*(?:\[[^\]]*\])?\s*(?=\{{)", text)
    found = group(text, match.end()) if match else None
    return found[0] if found else None


def aux_files(build_dir: Path, stem: str) -> list[Path]:
    """<stem>.aux and the .aux files it \\@input's (\\include), in order."""
    found, queue = [], [build_dir / f"{stem}.aux"]
    while queue:
        path = queue.pop(0)
        if path in found or not path.is_file():
            continue
        found.append(path)
        text = path.read_text(encoding="utf-8", errors="replace")
        queue += [build_dir / name for name in re.findall(r"\\@input\{([^{}]+)\}", text)]
    return found


def labels(build_dir: Path, stem: str) -> list[dict]:
    """{"key", "number", "page"} for each \\newlabel of the last build (cleveref's @cref twins skipped)."""
    found = []
    for path in aux_files(build_dir, stem):
        text = path.read_text(encoding="utf-8", errors="replace")
        for match in re.finditer(r"\\newlabel\{([^{}]+)\}", text):
            key = match.group(1)
            outer = group(text, match.end())
            if key.endswith("@cref") or outer is None:
                continue
            number = group(outer[0], 0)
            page = group(outer[0], number[1]) if number else None
            found.append({"key": key, "number": plain(number[0]) if number else None,
                          "page": page[0] if page else None})
    return found


def sections(build_dir: Path, stem: str) -> list[dict]:
    """{"level", "number", "title", "page"} for each line of the table of contents of the last build."""
    toc = build_dir / f"{stem}.toc"
    if not toc.is_file():
        return []
    text = toc.read_text(encoding="utf-8", errors="replace")
    found = []
    for match in re.finditer(r"\\contentsline\s*\{(\w+)\}", text):
        title = group(text, match.end())
        page = group(text, title[1]) if title else None
        if not title or not page:
            continue
        number = re.match(r"\s*\\numberline\s*\{([^{}]*)\}", title[0])
        found.append({"level": match.group(1), "number": number.group(1) if number else None,
                      "title": plain(title[0][number.end():] if number else title[0]), "page": page[0]})
    return found


def word_count(folder: Path) -> int | None:
    """Words in main.tex and what it inputs, by texcount (None without it); as ci_report.count_words."""
    if shutil.which("texcount") is None:
        return None
    result = subprocess.run(["texcount", "-1", "-sum", "-inc", "-q", "main.tex"],
                            cwd=folder, check=False, capture_output=True, text=True)
    text = result.stdout.strip()
    return int(text) if result.returncode == 0 and text.isdigit() else None


def query(main_tex: Path, build_dir: Path, pages: int | None) -> dict:
    """Metadata of a document: title and author from main.tex, labels and sections from its last build."""
    text = main_tex.read_text(encoding="utf-8", errors="replace")
    title, author = command_argument(text, "title"), command_argument(text, "author")
    return {
        "title": plain(title) if title is not None else None,
        "author": plain(author) if author is not None else None,
        "pages": pages,
        "words": word_count(main_tex.parent),
        "labels": labels(build_dir, main_tex.stem),
        "sections": sections(build_dir, main_tex.stem),
    }


# ---------------------------------------------------------------------------
# --pages
# ---------------------------------------------------------------------------

PAGE_RANGES = re.compile(r"\d+(?:-\d+)?(?:,\d+(?:-\d+)?)*")


def pages_command(pdf: Path, ranges: str, target: Path) -> list[str] | None:
    """The qpdf (preferred) or pdftk command that writes those pages of pdf to target, or None without either."""
    if shutil.which("qpdf"):
        return ["qpdf", "--empty", "--pages", str(pdf), ranges, "--", str(target)]
    if shutil.which("pdftk"):
        return ["pdftk", str(pdf), "cat", *ranges.split(","), "output", str(target)]
    return None


def extract_pages(pdf: Path, ranges: str, target: Path) -> str | None:
    """Write pages "1-3,7" of pdf to target. Returns an error message, or None on success."""
    if not PAGE_RANGES.fullmatch(ranges) or any(int(n) < 1 for n in re.findall(r"\d+", ranges)):
        return f"--pages {ranges!r}: use page numbers and ranges such as 1-3,7"
    command = pages_command(pdf, ranges, target)
    if command is None:
        return "--pages needs qpdf (https://qpdf.readthedocs.io) or pdftk on your PATH; neither was found."
    result = subprocess.run(command, capture_output=True, text=True, errors="replace", timeout=120)
    if result.returncode != 0 or not target.exists():  # qpdf exits 3 for warnings and still writes the file
        return f"{command[0]} failed: {(result.stderr or result.stdout).strip()[:300]}"
    return None


# ---------------------------------------------------------------------------
# --preflight
# ---------------------------------------------------------------------------

def fonts(pdf: Path) -> list[dict] | None:
    """pdffonts' table as {"name", "type", "emb"} (columns cut by its dashed line), or None without pdffonts."""
    if shutil.which("pdffonts") is None:
        return None
    lines = subprocess.run(["pdffonts", str(pdf)], capture_output=True, text=True, errors="replace",
                           timeout=60).stdout.splitlines()
    if len(lines) < 2 or not lines[1].startswith("-"):
        return []
    spans = [(dashes.start(), dashes.end()) for dashes in re.finditer(r"-+", lines[1])]
    found = []
    for line in lines[2:]:
        cells = [line[a:b + 1].strip() for a, b in spans]
        if len(cells) >= 4:
            found.append({"name": cells[0], "type": cells[1], "emb": cells[3]})
    return found


def preflight(pdf: Path, catalog: bytes = b"") -> list[tuple[str, str]]:
    """
    (level, message) camera-ready checks with pdffonts and pdfinfo (poppler-utils): fonts not embedded ("error"),
    Type 3 fonts, mixed page sizes ("warning"), the PDF version, page size and bookmarks ("info").
    catalog: the PDF with its object streams inflated (build.pdf_text), to find bookmarks.
    """
    found: list[tuple[str, str]] = []
    table = fonts(pdf)
    if table is None:
        found.append(("info", "pdffonts not found (poppler-utils): fonts not checked."))
    else:
        missing = sorted({row["name"] for row in table if row["emb"] != "yes"})
        if missing:
            found.append(("error", f"fonts not embedded: {', '.join(missing[:8])}. Hint: usually a figure PDF made "
                                   "by another program (R, Excel, a plotting tool); export it with fonts embedded, or "
                                   "re-distill it with Ghostscript (-dEmbedAllFonts=true)."))
        type3 = sorted({row["name"] or "(unnamed)" for row in table if row["type"] == "Type 3"})
        if type3:
            found.append(("warning", f"Type 3 (bitmap) fonts: {', '.join(type3[:8])}. Hint: \\usepackage[T1]"
                                     "{fontenc} needs vector fonts (lmodern or cm-super); a figure can bring them "
                                     "too. Many publishers reject Type 3 fonts."))
    if shutil.which("pdfinfo") is None:
        found.append(("info", "pdfinfo not found (poppler-utils): PDF version and page size not checked."))
        return found
    info = subprocess.run(["pdfinfo", "-f", "1", "-l", "100000", str(pdf)], capture_output=True, text=True,
                          errors="replace", timeout=60).stdout
    version = re.search(r"^PDF version:\s*(\S+)", info, re.MULTILINE)
    sizes = re.findall(r"^Page\s+\d+ size:\s*(.+?)\s*$", info, re.MULTILINE)
    if version:
        found.append(("info", f"PDF version {version.group(1)}. Hint: check what your venue accepts."))
    distinct = list(dict.fromkeys(sizes))
    if len(distinct) > 1:
        found.append(("warning", f"pages of {len(distinct)} different sizes: {'; '.join(distinct[:4])}"))
    elif distinct:
        found.append(("info", f"page size {distinct[0]}"))
    if b"/Outlines" in catalog:
        found.append(("info", "the PDF has bookmarks; IEEE PDF eXpress has flagged them: hyperref's "
                              "bookmarks=false turns them off."))
    return found
