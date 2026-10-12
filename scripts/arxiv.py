"""
arXiv export for build.py --arxiv (and the editor's "Export for arXiv"): out/<name>.arxiv.zip with the files the
last build read from the document directory (its .fls), the generated .bbl (.ind, .gls, .nls) named after the
main file, % comments stripped from .tex files, plus preflight findings with a fix hint each.

It runs no TeX: it reads the cache a sandboxed build left and the document directory, and writes one zip.
arXiv accepts .tar.gz and .zip uploads alike (https://info.arxiv.org/help/tar.html); zip opens everywhere.
Stdlib only, Python 3.9.
"""

from __future__ import annotations

import json
import os
import re
import struct
import zipfile
from pathlib import Path, PurePosixPath

# https://info.arxiv.org/help/submit_tex.html (2026-10-11): TeX Live 2025 (the default) reads biblatex .bbl
# format 3.3 only; TeX Live 2023, which a submission can select, reads 3.2 and 3.3.
BBL_DEFAULT = "3.3"
BBL_OLD = "3.2"
BBL_VERSION = re.compile(r"biblatex bbl format version (\d+(?:\.\d+)*)")
# https://info.arxiv.org/help/sizes.html (2026-10-11): no byte limit is stated, but since February 2026 the
# submission warns about images larger than 34 megapixels.
MAX_PIXELS = 34_000_000
# https://info.arxiv.org/help/00README.html (2026-10-11): the 00README.json compilers arXiv supports for LaTeX
# are latex, pdflatex and xelatex (https://info.arxiv.org/help/faq/texlive.html has no LuaLaTeX).
README_NAME = "00README.json"
GENERATED = (".bbl", ".ind", ".gls", ".nls")  # arXiv runs neither makeindex nor glossaries; .bbl it may run itself

# Environments whose body TeX reads verbatim (or not at all): a % there is text, so they are copied unchanged.
VERBATIM = ("verbatim", "verbatim*", "Verbatim", "Verbatim*", "BVerbatim", "LVerbatim", "lstlisting", "minted",
            "comment", "alltt", "filecontents", "filecontents*", "spverbatim", "tcblisting", "pycode")
SCAN = re.compile(
    r"\\\\|\\%"  # an escaped backslash or percent sign: skipped whole, so \\% still starts a comment
    r"|(?P<comment>%)"
    r"|\\begin\{(?P<env>" + "|".join(re.escape(name) for name in VERBATIM) + r")\}"
    r"|\\(?P<verb>verb\*?|Verb\*?|lstinline|mintinline\{[^{}]*\})(?:\[[^\]]*\])?(?P<delim>[^\s\w{])"
    r"|\\(?:url|path|href|lstinline|mintinline\{[^{}]*\})\{(?P<brace>)"
)
PATH_ARG = re.compile(
    r"\\(?P<cmd>input|include|includegraphics|includepdf|bibliography|addbibresource|subfile|lstinputlisting|"
    r"import|subimport|inputminted)\*?\s*(?:\[[^\]]*\])?\s*(?:\{[^{}]*\}\s*)?\{(?P<arg>[^{}]+)\}"
)
GRAPHIC = re.compile(r"\\includegraphics\*?\s*(?:\[(?:[^\[\]{}]|\{[^{}]*\})*\])?\s*\{(?P<arg>[^{}]+)\}")
SHELL = re.compile(r"\\(?:immediate\s*)?\\write\s*18|\\ShellEscape\b")
SHELL_PACKAGES = ("minted", "pythontex", "svg", "gnuplottex", "epstopdf")
LOAD = re.compile(r"\\(?:usepackage|RequirePackage)\s*(?:\[[^\]]*\])?\s*\{(?P<names>[^{}]+)\}")
COMMENT = re.compile(r"(?<!\\)((?:\\\\)*)%[^\n]*")


def strip_comments(text: str) -> str:
    """
    Drop % comments from LaTeX source. A line that is only a comment goes; a comment after text leaves a bare %,
    which keeps the end of line from turning into a space. \\%, verbatim-like environments, \\verb, \\lstinline,
    \\url, \\href and \\path keep their % signs; a file that changes \\catcode is returned unchanged.
    """
    if "\\catcode" in text:
        return text
    out: list[str] = []
    env: str | None = None
    for line in text.splitlines(keepends=True):
        body, newline = (line[:-1], "\n") if line.endswith("\n") else (line, "")
        kept, position, dropped = "", 0, False
        while position <= len(body):
            if env:
                end = body.find(f"\\end{{{env}}}", position)
                if end < 0:
                    kept += body[position:]
                    break
                end += len(f"\\end{{{env}}}")
                kept += body[position:end]
                position, env = end, None
                continue
            match = SCAN.search(body, position)
            if not match:
                kept += body[position:]
                break
            if match.group("comment") is not None:
                head = kept + body[position:match.start()]
                if head.strip():
                    kept = head + "%"
                else:
                    dropped = True
                break
            kept += body[position:match.end()]
            position = match.end()
            if match.group("env"):
                env = match.group("env")
            elif match.group("verb"):
                close = body.find(match.group("delim"), position)
                stop = len(body) if close < 0 else close + 1  # unclosed: keep the rest of the line as it is
                kept += body[position:stop]
                position = stop
            elif match.group("brace") is not None:
                depth, stop = 1, position
                while stop < len(body) and depth:
                    depth += {"{": 1, "}": -1}.get(body[stop], 0) if body[stop - 1] != "\\" else 0
                    stop += 1
                kept += body[position:stop]
                position = stop
        if not dropped:
            out.append(kept + newline)
    return "".join(out)


def no_suffix(name: str) -> str:
    """fig/a.png -> fig/a (only an image extension: "v1.2" stays)."""
    return re.sub(r"\.(?:pdf|png|jpe?g|eps|ps|svg|gif|tiff?|bmp)$", "", name, flags=re.IGNORECASE)


def read_files(main_tex: Path, build_dir: Path) -> list[str]:
    """Files inside the document directory that the last build read (its .fls), relative to it, in read order."""
    recorder = build_dir / f"{main_tex.stem}.fls"
    root = main_tex.parent.resolve()
    found: dict[str, None] = {}
    for line in recorder.read_text(encoding="utf-8", errors="replace").splitlines():
        if not line.startswith("INPUT "):
            continue
        path = Path(os.path.realpath(os.path.join(str(main_tex.parent), line[len("INPUT "):].strip())))
        # The real path, so a symlink (or a .fls written by a hostile build) never leads out of the directory.
        if root in path.parents and path.is_file():
            found[path.relative_to(root).as_posix()] = None
    return list(found)


def image_pixels(path: Path) -> int | None:
    """Width x height of a PNG or JPEG from its header, else None."""
    try:
        with path.open("rb") as handle:
            head = handle.read(32)
            if head.startswith(b"\x89PNG\r\n\x1a\n") and head[12:16] == b"IHDR":
                width, height = struct.unpack(">II", head[16:24])
                return width * height
            if not head.startswith(b"\xff\xd8"):
                return None
            handle.seek(2)
            while True:
                marker = handle.read(4)
                if len(marker) < 4 or marker[0] != 0xFF:
                    return None
                kind, size = marker[1], struct.unpack(">H", marker[2:])[0]
                if 0xC0 <= kind <= 0xCF and kind not in (0xC4, 0xC8, 0xCC):  # a start-of-frame marker
                    height, width = struct.unpack(">xHH", handle.read(5))
                    return width * height
                handle.seek(size - 2, os.SEEK_CUR)
    except (OSError, struct.error):
        return None


def preflight(main_tex: Path, settings: dict, files: list[str], build_dir: Path, outside: list[Path]) -> list[dict]:
    """Problems arXiv would have with these files: {level (error, warning, note), file, message, hint}."""
    findings: list[dict] = []

    def add(level: str, file: str | None, message: str, hint: str) -> None:
        found = {"level": level, "file": file, "message": message, "hint": hint}
        if found not in findings:
            findings.append(found)

    folder = main_tex.parent
    # Everything the build read, TeX Live's own files included: a figure found there (example-image) is not missing.
    fls = (build_dir / f"{main_tex.stem}.fls").read_text(encoding="utf-8", errors="replace").splitlines()
    read = [line[len("INPUT "):].strip().replace("\\", "/") for line in fls if line.startswith("INPUT ")]
    texts = {rel: COMMENT.sub(r"\1", (folder / rel).read_text(encoding="utf-8", errors="replace"))
             for rel in files if rel.endswith((".tex", ".sty", ".cls"))}

    for path in outside:
        add("error", None, f"The build read {path}, outside the document directory; it is not in the archive.",
            "Copy the file into the document directory and refer to it with a relative path.")
    for rel in files:
        if any(part.startswith(".") for part in PurePosixPath(rel).parts):
            add("error", rel, "Hidden file or folder: arXiv deletes these when the paper is announced.",
                "Rename it without the leading dot and update the command that reads it.")
        if not rel.isascii() or re.search(r"[\s&:\\]", rel):
            add("error", rel, "The file name has spaces, non-ASCII or special characters; arXiv renames them to _.",
                "Rename it to letters, digits, '.', '-' and '_' only, and update the command that reads it.")
        if rel.endswith("-eps-converted-to.pdf"):
            add("warning", rel, "An EPS figure converted during the build: arXiv does not convert figures.",
                "Convert the .eps to .pdf yourself and include the .pdf.")
        pixels = image_pixels(folder / rel) if rel.lower().endswith((".png", ".jpg", ".jpeg")) else None
        if pixels and pixels > MAX_PIXELS:
            add("warning", rel, f"Image of {pixels / 1e6:.0f} megapixels: arXiv warns above 34.",
                "Scale it down (300 to 600 dpi at its printed size is plenty) or use a vector format.")

    for rel, text in texts.items():
        for match in PATH_ARG.finditer(text):
            for arg in (part.strip() for part in match.group("arg").split(",")):
                if arg.startswith(("/", "~")) or re.match(r"[A-Za-z]:[\\/]", arg) or ".." in re.split(r"[\\/]", arg):
                    add("error", rel, f"\\{match.group('cmd')}{{{arg}}} uses an absolute path or '..'.",
                        "arXiv compiles in the submission's root only: move the file into the document "
                        "directory and use a path relative to main.tex.")
        if SHELL.search(text):
            add("error", rel, "\\write18 runs a program: arXiv never enables shell escape.",
                "Run the program yourself and include its output files instead.")
        for match in LOAD.finditer(text):
            for name in (part.strip() for part in match.group("names").split(",")):
                if name in SHELL_PACKAGES:
                    add("error" if name != "epstopdf" else "warning", rel,
                        f"Package {name} needs shell escape, which arXiv never enables.",
                        "For minted: build once with shell escape and include the _minted folder it writes "
                        "(https://info.arxiv.org/help/faq/texlive.html). Otherwise include the files the "
                        "package generates and load it in a mode that does not run programs.")
        for match in GRAPHIC.finditer(text):
            arg = match.group("arg").strip().removeprefix("./")
            if "\\" in arg or "#" in arg:
                continue
            stem = no_suffix(arg)
            # With or without extension, and by a trailing part of the path (\graphicspath).
            if not any(f == arg or no_suffix(f) == stem or f.endswith(f"/{arg}") or no_suffix(f).endswith(f"/{stem}")
                       for f in [*files, *read]):
                add("error", rel, f"Figure '{arg}' is not in the archive.",
                    "Put the figure inside the document directory and build again before exporting.")
    if settings["shell_escape"] or any("shell-escape" in arg for arg in settings["latexmk_args"]):
        add("error", "build.toml", "The build runs with shell escape; arXiv never enables it.",
            "Turn shell_escape off and check that the document still builds.")

    bbl = build_dir / f"{main_tex.stem}.bbl"
    version = BBL_VERSION.search(bbl.read_text(encoding="utf-8", errors="replace")[:500]) if bbl.is_file() else None
    if version and version.group(1) == BBL_OLD:
        add("warning", bbl.name, f"biblatex .bbl format {BBL_OLD}: arXiv's default TeX Live 2025 reads only "
            f"{BBL_DEFAULT}.", "Select TeX Live 2023 when you submit, or build with biblatex 3.20 and Biber 2.20.")
    elif version and version.group(1) != BBL_DEFAULT:
        add("error", bbl.name, f"biblatex .bbl format {version.group(1)}: arXiv reads {BBL_DEFAULT} "
            f"(TeX Live 2025) and {BBL_OLD} (TeX Live 2023).",
            "Build with the biblatex and Biber of TeX Live 2025 (biblatex 3.20, Biber 2.20).")

    if settings["engine"] == "lualatex":
        add("error", None, "arXiv does not compile LuaLaTeX (its processors are latex, pdflatex and xelatex).",
            "Switch the document to pdflatex or xelatex, or submit the PDF instead.")
    if settings["pdfa"] or settings["tagged"]:
        add("note", "build.toml", "pdfa / tagged reach the build through -usepretex, so arXiv's build lacks them.",
            "If arXiv's PDF must be tagged or PDF/A, put the \\DocumentMetadata{...} line first in main.tex.")
    return findings


def readme(settings: dict, main_name: str) -> str | None:
    """The 00README.json for a non-pdflatex engine arXiv supports, else None (pdflatex is arXiv's default)."""
    if settings["engine"] != "xelatex":
        return None
    return json.dumps({"spec_version": 1, "process": {"compiler": "xelatex"},
                       "sources": [{"filename": main_name, "usage": "toplevel"}]}, indent=2) + "\n"


def export(main_tex: Path, settings: dict, build_dir: Path, target: Path, outside: list[Path]) -> list[dict]:
    """Write the archive to target (replaced atomically) and return the preflight findings."""
    files = [rel for rel in read_files(main_tex, build_dir)
             if PurePosixPath(rel).name not in {f"{main_tex.stem}{suffix}" for suffix in GENERATED}]
    findings = preflight(main_tex, settings, files, build_dir, outside)
    members: dict[str, bytes] = {}
    for rel in files:
        data = (main_tex.parent / rel).read_bytes()
        if rel.endswith(".tex"):
            data = strip_comments(data.decode("utf-8", errors="surrogateescape")).encode("utf-8", "surrogateescape")
        members[rel] = data
    for suffix in GENERATED:
        path = build_dir / f"{main_tex.stem}{suffix}"
        if path.is_file():
            members[path.name] = path.read_bytes()
    text = readme(settings, main_tex.name)
    if text:
        members[README_NAME] = text.encode()

    target.parent.mkdir(parents=True, exist_ok=True)
    temporary = target.with_name(target.name + f".{os.getpid()}.tmp")
    try:
        with zipfile.ZipFile(temporary, "w", zipfile.ZIP_DEFLATED) as archive:
            for name in sorted(members):
                info = zipfile.ZipInfo(name, date_time=(1980, 1, 1, 0, 0, 0))  # same sources, same bytes
                info.external_attr = 0o644 << 16
                info.compress_type = zipfile.ZIP_DEFLATED
                archive.writestr(info, members[name])
        os.replace(temporary, target)
    finally:
        if temporary.exists():
            temporary.unlink()
    return findings
