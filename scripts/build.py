#!/usr/bin/env python3

from __future__ import annotations

import argparse
import fnmatch
import functools
import html
import json
import os
import posixpath
import re
import shutil
import subprocess
import sys
import threading
import time
import zlib
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

import accel
import hints

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------

SCRIPT_DIR = Path(__file__).resolve().parent
ROOT_DIR = SCRIPT_DIR.parent
FILES_DIR = ROOT_DIR / "files"
# Where documents are found. --source DIR changes it for one run.
SOURCE_DIR = FILES_DIR
# Local benchmark documents. CI never builds them, but they share out/ with
# files/, so prune() keeps their outputs.
BENCH_DIR = ROOT_DIR / "bench"
OUT_DIR = ROOT_DIR / "out"
# latexmk's working files (.aux, .bbl, ...) persist here between builds, so an
# edit reruns only the passes it needs instead of a cold multi-pass build.
CACHE_DIR = ROOT_DIR / ".latex-cache"

# ponytail: capped at 8; every job is a full TeX run, so more would thrash the disk.
DEFAULT_JOBS = min(os.cpu_count() or 1, 8)

# ponytail: at most this many errors per failing document in the summary; the log has all.
MAX_SUMMARY_ERRORS = 10

# Changes to these paths (relative to ROOT_DIR) rebuild every document.
GLOBAL_INPUTS = (
    "scripts/build.py",
    "scripts/accel.py",
    "scripts/hints.py",
    "scripts/publish_release.py",
    "scripts/ci_report.py",
    ".github/workflows/build-pdf.yml",
    ".github/texlive-packages.txt",
)

LATEXMK_ARGS = [
    "-interaction=nonstopmode",
    "-halt-on-error",
    "-file-line-error",
    "-recorder",  # <cache>/<name>.fls: every file the build read, see recorded_inputs().
    "-synctex=1",  # <cache>/<name>.synctex.gz, read by serve.py for source <-> PDF jumps.
]

# kpsewhich variables naming directories of the TeX installation. Files there
# (classes, fonts, texmf.cnf, the format file) never make a document stale.
TEX_TREE_VARIABLES = (
    "TEXMFROOT", "TEXMFMAIN", "TEXMFLOCAL", "TEXMFSYSCONFIG", "TEXMFSYSVAR", "TEXMFVAR", "TEXMFCONFIG",
)

# TeX wraps its log at 79 columns, which splits paths and errors across lines.
# TeX Live and MiKTeX read these limits from the environment (kpathsea).
LATEX_LOG_ENV = {"max_print_line": "10000", "error_line": "254", "half_error_line": "238"}

# Engine name -> latexmk flag. Set per document (see read_settings).
ENGINES = {
    "pdflatex": "-pdf",
    "xelatex": "-pdfxe",
    "lualatex": "-pdflua",
}
DEFAULT_ENGINE = "pdflatex"


# ---------------------------------------------------------------------------
# Output helpers
# ---------------------------------------------------------------------------

# Parallel builds write whole blocks under this lock, so lines never interleave.
OUTPUT_LOCK = threading.Lock()


def write(text: str) -> None:
    with OUTPUT_LOCK:
        sys.stdout.write(text)


def info(message: str = "") -> None:
    write(message + "\n")


def error(message: str) -> None:
    with OUTPUT_LOCK:
        print(f"ERROR: {message}", file=sys.stderr)


RULE = "=" * 72

# Colour only on a terminal; NO_COLOR (https://no-color.org) turns it off.
COLOR = sys.stdout.isatty() and "NO_COLOR" not in os.environ
GREEN = "32"
RED = "31"

if COLOR and os.name == "nt":
    os.system("")  # Enables ANSI escape codes in the Windows console.


def paint(text: str, code: str) -> str:
    return f"\033[{code}m{text}\033[0m" if COLOR else text


def separator() -> None:
    print(RULE)


def source_root(main_tex: Path) -> Path:
    """
    The source directory a document belongs to: SOURCE_DIR (files/, or the
    --source directory), else files/ or bench/. Output and cache names are
    relative to it.
    """
    for root in (SOURCE_DIR, FILES_DIR, BENCH_DIR):
        if root in main_tex.parents:
            return root
    return SOURCE_DIR


def known_sources() -> list[Path]:
    """Existing source directories: the selected one, then files/ and bench/."""
    found: list[Path] = []
    for root in (SOURCE_DIR, FILES_DIR, BENCH_DIR):
        if root.is_dir() and root not in found:
            found.append(root)
    return found


def doc_name(main_tex: Path) -> str:
    """
    files/reports/final/main.tex -> reports/final
    """
    return main_tex.parent.relative_to(source_root(main_tex)).as_posix()


def escape_name(text: str) -> str:
    """
    reports/final_v2 report.pdf -> reports_2Ffinal_5Fv2_20report.pdf

    Every character outside [A-Za-z0-9.-] becomes "_" + its UTF-8 bytes in hex,
    so the result holds no "/" or spaces and can be reversed. Used for release
    asset names (publish_release.py) and for cache directory names.
    """
    return re.sub(
        r"[^A-Za-z0-9.-]",
        lambda match: "".join(f"_{byte:02X}" for byte in match.group().encode()),
        text,
    )


def cache_dir_for(main_tex: Path) -> Path:
    """
    One flat directory per document, so no document's cache lies inside
    another's (files/a and files/a/b). The root document gets "_root", which
    escape_name never produces. Documents outside files/ are keyed with their
    source's name, so bench/x and files/x keep separate caches.
    """
    name = doc_name(main_tex)
    root = source_root(main_tex)
    if root != FILES_DIR:
        prefix = root.relative_to(ROOT_DIR).as_posix()
        name = prefix if name == "." else f"{prefix}/{name}"
    return CACHE_DIR / ("_root" if name == "." else escape_name(name))


def figure_cache_for(main_tex: Path) -> Path:
    """
    Compiled TikZ figures of a document, beside (not inside) its build directory so
    that wiping a broken build directory keeps them.
    """
    return CACHE_DIR / "_figcache" / cache_dir_for(main_tex).name


def name_matches(name: str, pattern: str) -> bool:
    return name == pattern or fnmatch.fnmatch(name, pattern)


def select_documents(documents: list[Path], patterns: list[str]) -> list[Path]:
    """
    Documents whose name equals a pattern or matches it as a glob.
    No patterns selects everything.
    """
    if not patterns:
        return documents

    return [
        document for document in documents
        if any(name_matches(doc_name(document), pattern) for pattern in patterns)
    ]


def unknown_patterns(documents: list[Path], patterns: list[str]) -> list[str]:
    """
    Patterns that match no document.
    """
    names = [doc_name(document) for document in documents]
    return [
        pattern for pattern in patterns
        if not any(name_matches(name, pattern) for name in names)
    ]


# ---------------------------------------------------------------------------
# LaTeX detection
# ---------------------------------------------------------------------------

def find_latexmk() -> str | None:
    """
    Find latexmk using the user's PATH.
    """
    return shutil.which("latexmk")


def check_latex() -> str:
    """
    Verify that latexmk is installed and accessible.
    """
    latexmk = find_latexmk()

    if latexmk:
        return latexmk

    separator()
    error("latexmk was not found on your PATH.")
    print()
    print("A LaTeX distribution with latexmk is required to build this project.")
    print()
    print("Windows:")
    print("  MiKTeX:   https://miktex.org/")
    print("  TeX Live: https://www.tug.org/texlive/")
    print()
    print("macOS:")
    print("  MacTeX:   https://www.tug.org/mactex/")
    print()
    print("Linux:")
    print("  Install TeX Live using your distribution's package manager.")
    print()
    print("After installation, make sure 'latexmk' is available from")
    print("your terminal, then run:")
    print()
    print("  python scripts/build.py")
    print()
    separator()

    raise SystemExit(1)


# ---------------------------------------------------------------------------
# Document discovery
# ---------------------------------------------------------------------------

def find_documents(root: Path | None = None) -> list[Path]:
    """
    Find every main.tex under root (default: SOURCE_DIR).

    Returns absolute paths.
    """
    root = root or SOURCE_DIR
    return sorted(path for path in root.rglob("main.tex") if path.is_file())


def all_documents() -> list[Path]:
    """Documents of every known source. prune() works from this list."""
    return sorted({path for root in known_sources() for path in find_documents(root)})


def changed_files(ref: str) -> list[str] | None:
    """
    Files changed between the merge base of ref and HEAD, and the working tree.

    Returns None if git cannot answer (unknown ref, shallow clone, no git),
    in which case every document should be built.
    """
    try:
        base = subprocess.run(
            ["git", "merge-base", ref, "HEAD"],
            cwd=ROOT_DIR, capture_output=True, text=True, check=True,
        ).stdout.strip()
        diff = subprocess.run(
            ["git", "diff", "--name-only", "-z", base],
            cwd=ROOT_DIR, capture_output=True, text=True, check=True,
        ).stdout
    except (OSError, subprocess.CalledProcessError):
        return None

    return [name for name in diff.split("\0") if name]


def filter_changed(documents: list[Path], ref: str) -> list[Path]:
    """
    Keep documents whose directory contains a changed file, or that list a
    changed file in their recorded inputs (recorded_inputs: the cached .fls
    from the last build, restored by CI).

    Everything inside a document's directory (.tex, .bib, .cls, figures, ...)
    counts as its input. Inputs outside it count once a build has recorded them.
    """
    changed = changed_files(ref)

    if changed is None:
        info(f"Could not diff against '{ref}'; building everything.")
        return documents

    if any(name.startswith(GLOBAL_INPUTS) for name in changed):
        return documents

    changed_paths = [ROOT_DIR / name for name in changed]

    def affected(document: Path) -> bool:
        recorded = set(recorded_inputs(document))
        return any(document.parent in path.parents or path in recorded for path in changed_paths)

    return [document for document in documents if affected(document)]


@functools.cache
def tex_tree_dirs() -> tuple[Path, ...] | None:
    """
    Directories of the TeX installation, from kpsewhich (one call per variable,
    once per run). None if kpsewhich cannot be run, in which case files outside
    a document's directory are not tracked at all.
    """
    dirs = []
    for variable in TEX_TREE_VARIABLES:
        try:
            result = subprocess.run(
                ["kpsewhich", f"-var-value={variable}"],
                capture_output=True, text=True, check=True,
            )
        except (OSError, subprocess.CalledProcessError):
            return None
        value = result.stdout.strip()
        if value:
            dirs.append(Path(value).resolve())
    return tuple(dirs)


def mirror_dirs(source: Path, target: Path) -> None:
    """
    Create the subdirectories of source (no files, no hidden ones) under target.
    latexmk writes the .aux of \\include{Chapters/ch1} to <outdir>/Chapters/,
    which must exist before it can.
    """
    for path in source.rglob("*"):
        relative = path.relative_to(source)
        if path.is_dir() and not any(part.startswith(".") for part in relative.parts):
            (target / relative).mkdir(parents=True, exist_ok=True)


def source_date_epoch(main_tex: Path) -> str:
    """
    SOURCE_DATE_EPOCH for a document: the time of the last commit that touched
    its directory, so the same commit gives the same PDF bytes. Without git
    history for the directory (new, or no repository), the newest input.
    """
    try:
        found = subprocess.run(
            ["git", "log", "-1", "--format=%ct", "--", main_tex.parent.relative_to(ROOT_DIR).as_posix()],
            cwd=ROOT_DIR, capture_output=True, text=True, check=True,
        ).stdout.strip()
    except (OSError, subprocess.CalledProcessError, ValueError):
        found = ""
    return found if found.isdigit() else str(int(newest_input(main_tex)))


def recorded_inputs(main_tex: Path) -> list[Path]:
    """
    Files outside the document's directory that its last build read, from the
    .fls that latexmk writes (-recorder). Empty before the first build.

    The document directory is skipped (newest_input scans it anyway), and so
    are the TeX installation, the cache and out/: the cache holds generated
    figures and .inject files, rewritten on every build.
    """
    recorder = cache_dir_for(main_tex) / f"{main_tex.stem}.fls"
    tree = tex_tree_dirs()

    if tree is None or not recorder.exists():
        return []

    # A TEXMF* value that contains the repository (say "/") would hide every input.
    skip = [path for path in (*tree, CACHE_DIR, OUT_DIR) if path not in ROOT_DIR.parents and path != ROOT_DIR]
    directory = main_tex.parent
    found: list[Path] = []

    for line in recorder.read_text(encoding="utf-8", errors="replace").splitlines():
        if not line.startswith("INPUT "):
            continue
        path = (directory / line[len("INPUT "):].strip()).resolve()
        if path.is_relative_to(directory) or any(path.is_relative_to(base) for base in skip):
            continue
        if path not in found:
            found.append(path)

    return found


def newest_input(main_tex: Path) -> float:
    """
    Newest modification time among a document's inputs: every file in its
    directory tree (dotfiles such as .DS_Store excluded), the files outside it
    that the last build read (recorded_inputs), and build.py and accel.py.
    """
    # The scripts that decide what a build produces; editing serve.py or ci_report.py rebuilds nothing.
    newest = max(Path(__file__).stat().st_mtime, (SCRIPT_DIR / "accel.py").stat().st_mtime)

    for path in main_tex.parent.rglob("*"):
        if path.name.startswith("."):
            continue
        try:
            if path.is_file():
                newest = max(newest, path.stat().st_mtime)
        except OSError:
            pass  # Deleted mid-scan.

    for path in recorded_inputs(main_tex):
        try:
            newest = max(newest, path.stat().st_mtime)
        except OSError:
            pass  # Gone since the build; the next build drops it from the .fls.

    return newest


def is_stale(main_tex: Path) -> bool:
    """
    True if the PDF is missing or older than any of the document's inputs.
    """
    output = output_path_for(main_tex)

    try:
        return newest_input(main_tex) > output.stat().st_mtime
    except FileNotFoundError:
        return True


def output_path_for(main_tex: Path) -> Path:
    """
    Map:

        files/ANY_FOLDER/main.tex
        -> out/ANY_FOLDER.pdf

    Examples:

        files/FP-123 Proposal/main.tex
        -> out/FP-123 Proposal.pdf

        files/reports/final/main.tex
        -> out/reports/final.pdf

        files/main.tex
        -> out/main.pdf
    """
    relative = main_tex.relative_to(source_root(main_tex)).parent

    if relative == Path("."):
        return OUT_DIR / "main.pdf"

    # Not with_suffix(): it would turn "v1.2" into "v1.pdf".
    return OUT_DIR / relative.parent / f"{relative.name}.pdf"


def log_path_for(main_tex: Path) -> Path:
    """
    files/FP-123 Proposal/main.tex -> out/FP-123 Proposal.log

    Full log of the document's most recent build, successful or not.
    """
    return output_path_for(main_tex).with_suffix(".log")


def docx_path_for(main_tex: Path) -> Path:
    """files/reports/final/main.tex -> out/reports/final.docx (optional pandoc export, never published)."""
    return output_path_for(main_tex).with_suffix(".docx")


# ---------------------------------------------------------------------------
# Per-document settings
# ---------------------------------------------------------------------------

# "% !TEX program = xelatex" (also "% !TeX TS-program = ..."), in the first lines.
ENGINE_MAGIC = re.compile(r"^\s*%\s*!\s*TEX\s+(?:TS-)?PROGRAM\s*=\s*(\S+)", re.IGNORECASE)
CONFIG_KEYS = {"engine", "shell_escape", "latexmk_args", "externalize", "pdfa", "lang", "timeout",
               "grammar", "grammar_url", "disabled_rules"}  # the last three are read by grammar.py
# Wall-clock limit for one latexmk run, in seconds (build.toml "timeout"; --timeout changes the default).
DEFAULT_TIMEOUT = 600
TIMEOUT_RANGE = (10, 7200)
PDFA_LEVEL = re.compile(r"^(?:a-)?([123][abu])$")

# ponytail: errors are read from the console; -file-line-error puts each on one "file:line: message" line.
LATEX_ERROR = re.compile(r"^(?P<file>.+?):(?P<line>\d+): (?P<message>\S.*)$")
LATEX_WARNING = re.compile(r"^(?:LaTeX|Package|Class)\b.*\bWarning", re.MULTILINE)
LATEX_PAGES = re.compile(r"^Output written on .*?\((\d+)\s+pages?", re.MULTILINE | re.DOTALL)


class ConfigError(Exception):
    """A document's settings are invalid. The message goes into its log."""


def display(path: Path) -> str:
    """Path relative to the repository root, with forward slashes."""
    return path.relative_to(ROOT_DIR).as_posix()


def read_toml(path: Path) -> dict:
    """
    Parse a build.toml. tomllib is in the standard library from Python 3.11;
    older interpreters need the tomli package.
    """
    try:
        import tomllib
    except ImportError:
        try:
            import tomli as tomllib
        except ImportError:
            raise ConfigError(
                f"{display(path)} needs Python 3.11+ or the 'tomli' package."
            ) from None

    try:
        return tomllib.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise ConfigError(f"{display(path)}: {exc}") from None


def read_settings(main_tex: Path) -> dict:
    """
    Engine, shell_escape and latexmk_args for one document.

    The engine comes from the magic comment in the first 20 lines of main.tex
    and can be overridden by build.toml in the document's directory.
    Raises ConfigError for invalid values.
    """
    settings = {"engine": DEFAULT_ENGINE, "shell_escape": False, "latexmk_args": [], "externalize": True,
                "pdfa": None, "lang": "en-US", "timeout": DEFAULT_TIMEOUT}

    text = main_tex.read_text(encoding="utf-8", errors="replace")
    for line in text.splitlines()[:20]:
        match = ENGINE_MAGIC.match(line)
        if match:
            settings["engine"] = match.group(1)
            break

    config_path = main_tex.parent / "build.toml"
    if config_path.exists():
        data = read_toml(config_path)

        unknown = sorted(set(data) - CONFIG_KEYS)
        if unknown:
            raise ConfigError(f"{display(config_path)}: unknown key(s): {', '.join(unknown)}")

        settings.update(data)

    engine = settings["engine"]
    if not isinstance(engine, str) or engine.lower() not in ENGINES:
        raise ConfigError(
            f"{display(main_tex)}: unknown engine {engine!r} "
            "(use pdflatex, xelatex or lualatex)"
        )
    settings["engine"] = engine.lower()

    for key in ("shell_escape", "externalize"):
        if not isinstance(settings[key], bool):
            raise ConfigError(f"{display(main_tex.parent / 'build.toml')}: {key} must be true or false")

    if settings["pdfa"] is not None:
        match = PDFA_LEVEL.match(str(settings["pdfa"]).lower())
        if not match:
            raise ConfigError(
                f"{display(main_tex.parent / 'build.toml')}: pdfa must be a PDF/A level such as \"2b\" or \"a-2b\""
            )
        settings["pdfa"] = f"a-{match.group(1)}"
    if not (isinstance(settings["lang"], str) and re.fullmatch(r"[A-Za-z]{2,3}(-[A-Za-z0-9]+)*", settings["lang"])):
        raise ConfigError(f"{display(main_tex.parent / 'build.toml')}: lang must be a language tag such as \"en-US\"")

    low, high = TIMEOUT_RANGE
    timeout = settings["timeout"]
    if not (isinstance(timeout, int) and not isinstance(timeout, bool) and low <= timeout <= high):
        raise ConfigError(
            f"{display(main_tex.parent / 'build.toml')}: timeout must be a whole number of seconds, {low} to {high}"
        )

    args = settings["latexmk_args"]
    if not (isinstance(args, list) and all(isinstance(arg, str) for arg in args)):
        raise ConfigError(f"{display(main_tex.parent / 'build.toml')}: latexmk_args must be a list of strings")

    return settings


def pdfa_metadata(settings: dict) -> str:
    """The \\DocumentMetadata line for build.toml's pdfa, or "" when it is off."""
    if not settings["pdfa"]:
        return ""
    level = settings["pdfa"]
    lines = [f"\\DocumentMetadata{{pdfstandard={level},lang={settings['lang']}}}",
             # veraPDF: xcolor's cmyk colours break PDF/A with the RGB OutputIntent, and pdfTeX
             # writes no ToUnicode for symbol glyphs (CMEX) without the glyph name maps.
             r"\PassOptionsToPackage{rgb}{xcolor}",
             r"\ifdefined\pdfgentounicode\input{glyphtounicode}\InputIfFileExists{glyphtounicode-cmr}{}{}"
             r"\pdfgentounicode=1 \fi"]
    if level.startswith("a-1"):
        # PDF/A-1 forbids object streams.
        lines.append(r"\ifdefined\pdfobjcompresslevel\pdfobjcompresslevel=0 \fi"
                     r"\ifdefined\pdfvariable\pdfvariable objcompresslevel=0 \fi")
    return "".join(lines)  # one -usepretex argument: no newlines


def pdfa_check(pdf: Path, level: str) -> str:
    """
    A cheap look, not validation (use veraPDF for that): does the PDF carry
    the XMP pdfaid declaration and an OutputIntent? Returns a one-line note.
    """
    data = pdf.read_bytes()
    streams = [data]
    for chunk in re.findall(rb"stream\r?\n(.*?)endstream", data, re.DOTALL):  # object streams hide the catalog
        try:
            streams.append(zlib.decompress(chunk))
        except zlib.error:
            pass
    text = b"\n".join(streams)
    part = re.search(rb"<pdfaid:part>(\d)</pdfaid:part>|pdfaid:part=\"(\d)\"", text)
    missing = []
    if not part:
        missing.append("XMP pdfaid")
    if b"/OutputIntents" not in text:
        missing.append("OutputIntent")
    if missing:
        return f"PDF/A {level} requested, but the PDF has no {' or '.join(missing)} (needs LaTeX 2023-06 or newer)."
    verapdf = shutil.which("verapdf")
    if verapdf is None:
        return (f"PDF/A {level}: XMP pdfaid and OutputIntent present "
                "(not validated; install veraPDF to check conformance).")
    return verapdf_note(verapdf, pdf, level)


def verapdf_note(verapdf: str, pdf: Path, level: str) -> str:
    """Validate with veraPDF (on PATH): a one-line pass, or fail with the broken rules."""
    try:
        result = subprocess.run([verapdf, "--format", "xml", "--flavour", level.removeprefix("a-"), str(pdf)],
                                capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=300)
    except (OSError, subprocess.TimeoutExpired) as error:
        return f"PDF/A {level}: veraPDF did not run ({error})."
    if 'isCompliant="true"' in result.stdout:
        return f"PDF/A {level}: veraPDF passed."
    rules = list(dict.fromkeys(re.findall(r"<description>([^<]*)</description>", result.stdout)))
    if 'isCompliant="false"' not in result.stdout or not rules:
        return f"PDF/A {level}: veraPDF gave no verdict (exit {result.returncode})."
    return f"PDF/A {level}: veraPDF failed {len(rules)} rule(s): " + " | ".join(html.unescape(r) for r in rules[:3])


def parse_latex_errors(console: str) -> list[dict]:
    """
    LaTeX errors in latexmk's console output, as {file, line, message, hint}.
    File is relative to the document's directory. hint is a plain-language
    explanation from hints.explain (None when no rule matches), which also
    reads the three console lines after the error (TeX's context lines).
    """
    found: list[dict] = []
    lines = console.splitlines()

    for index, line in enumerate(lines):
        match = LATEX_ERROR.match(line)
        if not match:
            continue

        message = match.group("message")
        context = "\n".join(lines[index + 1:index + 4])
        item = {
            "file": posixpath.normpath(match.group("file")),
            "line": int(match.group("line")),
            "message": message,
            "hint": hints.explain(message, context),
        }
        if item not in found:
            found.append(item)

    return found


def error_text(name: str, found: dict) -> str:
    """
    files/<doc>/<file>:<line>: message. Editors can jump to it.
    """
    path = posixpath.normpath(posixpath.join(SOURCE_DIR.name, name, found["file"]))
    return f"{path}:{found['line']}: {found['message']}"


# ---------------------------------------------------------------------------
# Building
# ---------------------------------------------------------------------------

NEW_GROUP = {"creationflags": subprocess.CREATE_NEW_PROCESS_GROUP} if os.name == "nt" else {"start_new_session": True}


def kill_tree(process: subprocess.Popen) -> None:
    """Kill the process and its children (latexmk starts pdflatex)."""
    try:
        if os.name == "nt":
            subprocess.run(["taskkill", "/T", "/F", "/PID", str(process.pid)], capture_output=True, timeout=10)
        else:
            os.killpg(process.pid, 9)
    except (OSError, subprocess.TimeoutExpired):
        pass
    process.kill()


def build_document(
    main_tex: Path, latexmk: str, live: bool = True, force: bool = False, fig_jobs: int = DEFAULT_JOBS,
    record: bool = False,
) -> tuple[dict, str]:
    """
    Build one LaTeX document. Returns (report entry, console text).

    Compilation happens in .latex-cache/, which persists between builds so
    latexmk reruns only the passes an edit needs. LaTeX auxiliary files never
    pollute the source directory or out/. Only the final PDF and
    <name>.log are written to out/. The log holds the result, the latexmk
    console output, and LaTeX's and BibTeX's own logs: every info line,
    warning and error.

    With live=True the output is streamed to the terminal. With live=False
    nothing is printed except a "Started" line; the caller prints the
    returned text once the document finishes, so parallel builds don't mix.

    force passes latexmk -g, which rebuilds even when its cache says up to date.
    """
    began = time.monotonic()
    relative = main_tex.relative_to(ROOT_DIR)
    name = doc_name(main_tex)
    output_pdf = output_path_for(main_tex)
    log_path = log_path_for(main_tex)

    shown: list[str] = []

    def emit(text: str) -> None:
        shown.append(text)
        if live:
            write(text)

    def say(text: str = "") -> None:
        emit(text + "\n")

    if not live:
        info(f"Started: {relative}")

    say(RULE)
    say(f"Building: {relative}")
    say(f"Output:  {output_pdf.relative_to(ROOT_DIR)}")
    say(RULE)

    output_pdf.parent.mkdir(parents=True, exist_ok=True)

    console: list[str] = []
    errors: list[str] = []
    pages = None
    warnings = 0
    phases = {"figures": 0.0, "latex": 0.0}  # Seconds, for --profile and build-report.json.

    try:
        settings = read_settings(main_tex)
    except ConfigError as exc:
        settings = None
        errors.append(str(exc))

    # Stamp the PDF with the build start time, so edits made while LaTeX is
    # running still count as newer than the PDF.
    started = time.time()

    build_dir = cache_dir_for(main_tex)
    cached = build_dir.exists()
    build_dir.mkdir(parents=True, exist_ok=True)
    generated_pdf = build_dir / f"{main_tex.stem}.pdf"

    notes: list[str] = []

    if settings is not None:
        def latexmk_command(*extra: str) -> list[str]:
            return [
                latexmk,
                *LATEXMK_ARGS,
                ENGINES[settings["engine"]],
                *(["-shell-escape"] if settings["shell_escape"] else []),
                *settings["latexmk_args"],
                *extra,
                f"-outdir={build_dir}",
                main_tex.name,
            ]

        # Fixed dates in the PDF's metadata; \today follows the same epoch (README, "Reproducible PDFs").
        epoch = {"SOURCE_DATE_EPOCH": source_date_epoch(main_tex), "FORCE_SOURCE_DATE": "1"}

        def run(command: list[str], phase: str = "latex") -> int:
            """Run a command in the document's directory, streaming its output. Time goes to phases[phase]."""
            mirror_dirs(main_tex.parent, build_dir)
            say("Running:")
            say("  " + " ".join(f'"{arg}"' if " " in arg else arg for arg in command))
            say()
            began = time.monotonic()
            try:
                return stream(command)
            finally:
                phases[phase] += time.monotonic() - began

        def stream(command: list[str]) -> int:
            try:
                process = subprocess.Popen(
                    command,
                    cwd=main_tex.parent,
                    env={**os.environ, **LATEX_LOG_ENV, **epoch},
                    stdin=subprocess.DEVNULL,
                    stdout=subprocess.PIPE,
                    stderr=subprocess.STDOUT,
                    text=True,
                    errors="replace",
                    **NEW_GROUP,
                )
            except OSError as exc:
                errors.append(f"Could not execute {command[0]}: {exc}")
                return -1

            # Watchdog: killing the tree closes the pipe, which ends the line loop below.
            expired = threading.Event()

            def expire() -> None:
                expired.set()
                kill_tree(process)

            watchdog = threading.Timer(settings["timeout"], expire)
            watchdog.daemon = True
            watchdog.start()
            try:
                for line in process.stdout:
                    emit(line)
                    console.append(line)
                code = process.wait()
            except BaseException:  # Ctrl-C: the child is in its own group and would outlive us.
                kill_tree(process)
                raise
            finally:
                watchdog.cancel()
            if expired.is_set():
                errors.append(f"Build timed out after {settings['timeout']} s (the document may loop forever)")
            return code

        def externalized_build() -> tuple[int, str]:
            """
            TikZ figures compiled in parallel and cached, then latexmk for the text.
            Returns (exit code, problem). The problem is "" unless the figures could not be
            made, which is the only case worth a plain rebuild: a LaTeX error in the text
            would fail the plain build too.
            """
            try:
                figures = accel.Figures(
                    main_tex, build_dir, settings["engine"], settings["shell_escape"], fig_jobs,
                    env={**LATEX_LOG_ENV, **epoch}, cache=figure_cache_for(main_tex),
                )
                if force:
                    figures.wipe()
                say("Figures: TikZ externalization (list and make, compiled in parallel)")
                say()

                last = mode_file.stat().st_mtime if mode_file.exists() and not force else 0.0
                if not figures.names() or figures.touched(last):
                    say("Listing figures ...")
                    run(figures.discover_command(), "figures")
                    if errors:  # Timed out: no figures, no plain rebuild.
                        return -1, ""
                    console.clear()  # The listing run is not a result; latexmk's output is.

                for attempt in range(3):
                    began = time.monotonic()
                    changed, failures = figures.sync(say)
                    phases["figures"] += time.monotonic() - began
                    if failures:
                        console.extend(f"{text}\n" for text in failures)
                        return 1, failures[0].splitlines()[0]
                    if attempt and not changed:
                        return 0, ""
                    extra = [f"-usepretex={meta}{figures.main_pretex}", f"-jobname={main_tex.stem}"]
                    code = run(latexmk_command(*extra, *(["-g"] if switched else [])))
                    if code != 0:
                        return code, ""
                return 0, ""
            except Exception as exc:  # noqa: BLE001 - any failure of the optimisation means: build plainly.
                return 1, f"{type(exc).__name__}: {exc}"

        # Switching between plain and externalized output needs a rebuild latexmk can't see.
        mode_file = build_dir / ".mode"
        # \DocumentMetadata must precede \documentclass, which is what -usepretex gives it.
        meta = pdfa_metadata(settings)
        mode = "plain"
        if settings["externalize"] and accel.uses_tikz(main_tex.parent):
            mode = "externalized"
        previous = mode_file.read_text() if mode_file.exists() else mode + meta
        # --focus needs the \input tree of a full build; (re)build once to get it.
        no_map = record and not (build_dir / f"{main_tex.stem}.focusmap").exists()
        switched = force or previous != mode + meta or no_map
        # Plain builds record the \input tree too (the externalized ones do it through main_pretex),
        # so --focus and --watch --focus auto work for every document.
        def recording() -> list[str]:
            return [
                f"-usepretex={meta}{accel.write_inject(build_dir, main_tex.parent, '_record.tex', accel.RECORD)}",
                f"-jobname={main_tex.stem}",
            ]

        say(f"Engine:  {settings['engine']}")
        say()

        while True:
            code = -1
            used = mode

            if mode == "externalized":
                console = []
                code, problem = externalized_build()
                if problem:
                    notes.append(f"TikZ externalization failed ({problem}); rebuilt without it.")
                    say()
                    say(notes[-1])
                    say()
                    used, switched = "plain", True

            if used == "plain":
                console = []
                code = run(latexmk_command(*recording(), *(["-g"] if switched else [])))

            if code == 0 or not cached or errors:
                break

            # Stale working files can break a build that would pass from scratch.
            say()
            say("Build failed with cached files; retrying from a clean cache.")
            say()
            shutil.rmtree(build_dir, ignore_errors=True)
            build_dir.mkdir(parents=True)
            mirror_dirs(main_tex.parent, build_dir)  # \include{dir/x} writes dir/x.aux here.
            cached = False
            switched = False

        mode_file.write_text(used + meta)
        os.utime(mode_file, (started, started))

        if not errors:
            if code != 0:
                errors.append(f"LaTeX compilation failed: {relative}")
            elif not generated_pdf.exists():
                errors.append("LaTeX reported success, but no PDF was produced.")
            else:
                try:
                    shutil.copy(generated_pdf, output_pdf)
                    os.utime(output_pdf, (started, started))
                    if settings["pdfa"]:
                        notes.append(pdfa_check(output_pdf, settings["pdfa"]))
                        say(notes[-1])
                except OSError as exc:
                    errors.append(f"Could not copy generated PDF: {exc}")

    for message in errors:
        say(f"ERROR: {message}")

    latex_log = build_dir / f"{main_tex.stem}.log"
    log_text = latex_log.read_text(encoding="utf-8", errors="replace") if latex_log.exists() else ""

    # Counts from the final LaTeX run, which is the last pass that wrote the log.
    latex_errors = parse_latex_errors("".join(console))
    warnings = len(LATEX_WARNING.findall(log_text))
    page_counts = LATEX_PAGES.findall(log_text)

    if not errors and page_counts:
        pages = int(page_counts[-1])
    size = output_pdf.stat().st_size if not errors and output_pdf.exists() else None

    sections = [
        f"Build of {relative.as_posix()}: {'FAILED' if errors else 'SUCCESS'}\n",
        *(f"ERROR: {message}\n" for message in errors),
        *(f"{error_text(name, found)}\n" + (f"  Hint: {found['hint']}\n" if found["hint"] else "")
          for found in latex_errors),
        *(f"NOTE: {note}\n" for note in notes),
        "\n===== latexmk output =====\n",
        *console,
    ]

    for suffix, title in ((".log", "LaTeX log"), (".blg", "BibTeX log")):
        path = build_dir / f"{main_tex.stem}{suffix}"
        if path.exists():
            sections += [
                f"\n===== {title} ({path.name}) =====\n",
                path.read_text(encoding="utf-8", errors="replace"),
            ]

    log_path.write_text("".join(sections), encoding="utf-8")

    seconds = round(time.monotonic() - began, 1)

    say()
    say(f"Log:     {log_path.relative_to(ROOT_DIR)}")
    say(f"Time:    {seconds:.1f}s")

    if errors:
        say(paint(f"FAILED:  {relative.as_posix()}", RED))
    else:
        say(paint(f"SUCCESS: {output_pdf.relative_to(ROOT_DIR)}", GREEN))

    entry = {
        "name": name,
        "pdf": output_pdf.relative_to(OUT_DIR).as_posix(),
        "log": log_path.relative_to(OUT_DIR).as_posix(),
        "ok": not errors,
        "seconds": seconds,
        "engine": settings["engine"] if settings else None,
        "pages": pages,
        "size": size,
        "errors": latex_errors,
        "warnings": warnings,
        "error": None,
        "phases": {name: round(seconds, 1) for name, seconds in phases.items()},
    }

    return entry, "".join(shown)


def focus_paths(main_tex: Path) -> tuple[Path, Path]:
    """out/<name>.focus.pdf and out/<name>.focus.log for a document."""
    pdf = output_path_for(main_tex)
    return pdf.with_name(f"{pdf.stem}.focus.pdf"), pdf.with_name(f"{pdf.stem}.focus.log")


def build_focus(main_tex: Path, latexmk: str, focus: str, fig_jobs: int = DEFAULT_JOBS) -> bool:
    """_build_focus, but an exception (a file that cannot be read or written) is an error message and False."""
    try:
        return _build_focus(main_tex, latexmk, focus, fig_jobs)
    except Exception as exc:  # noqa: BLE001 - a watch loop must survive a failed preview.
        error(f"{main_tex.relative_to(ROOT_DIR).as_posix()}: focus build crashed: {exc}")
        return False


def _build_focus(main_tex: Path, latexmk: str, focus: str, fig_jobs: int) -> bool:
    """
    Typeset only the part of a document under `focus` (a path relative to the
    document, e.g. "Chapters/chapter5") into out/<name>.focus.pdf, with one
    LaTeX run that reads the last full build's .aux files, so references,
    citations, counters and page numbers match the full document.
    Returns True on success.
    """
    relative = main_tex.relative_to(ROOT_DIR)
    build_dir = cache_dir_for(main_tex)
    mapfile = build_dir / f"{main_tex.stem}.focusmap"

    try:
        settings = read_settings(main_tex)
    except ConfigError as exc:
        error(str(exc))
        return False

    if not mapfile.exists():
        info("No full build with a recorded input tree yet; building the whole document first.")
        entry, _ = build_safely(main_tex, latexmk, True, False, fig_jobs, record=True)
        info()
        if not entry["ok"] or not mapfile.exists():
            error("The full build did not produce an input tree; --focus cannot be used.")
            return False

    entries = accel.read_focusmap(mapfile)
    keep, first = accel.focus_selection(entries, focus)
    if first is None:
        error(f"--focus {focus!r}: no \\input or \\include in {relative.as_posix()} reads that path.")
        top = sorted({entry["path"] for entry in entries if entry["depth"] == 1})
        info("Files read by main.tex: " + ", ".join(top))
        return False

    out_pdf, out_log = focus_paths(main_tex)
    out_pdf.parent.mkdir(parents=True, exist_ok=True)
    focus_dir = build_dir / "focus"
    focus_dir.mkdir(exist_ok=True)

    # Fresh copies of the full build's cross-reference data; this run writes its own.
    for path in build_dir.iterdir():
        if path.suffix in {".aux", ".bbl", ".toc", ".lof", ".lot", ".out"}:
            shutil.copy2(path, focus_dir / path.name)

    pretex = accel.write_inject(focus_dir, main_tex.parent, "_focus.tex", accel.focus_tex(entries, keep, first))
    command = [
        settings["engine"], "-interaction=nonstopmode", "-halt-on-error", "-file-line-error",
        f"-output-directory={focus_dir}", f"-jobname={main_tex.stem}",
        *(["-shell-escape"] if settings["shell_escape"] else []),
        f"{pretex}\\input{{{main_tex.name}}}",
    ]
    info(f"Focus:   {focus} ({len(keep)} of {len(entries)} files read)")
    began = time.monotonic()
    generated = focus_dir / f"{main_tex.stem}.pdf"
    generated.unlink(missing_ok=True)
    try:
        process = subprocess.run(
            command, cwd=main_tex.parent, stdin=subprocess.DEVNULL, capture_output=True, text=True, errors="replace",
            timeout=FOCUS_TIMEOUT,
        )
    except subprocess.TimeoutExpired:  # run() kills the child before raising.
        error(f"{relative.as_posix()}: focus build timed out after {FOCUS_TIMEOUT}s.")
        out_log.write_text(
            f"Focus build of {relative.as_posix()} ({focus}): FAILED\nTimed out after {FOCUS_TIMEOUT}s.\n",
            encoding="utf-8",
        )
        return False
    log_file = focus_dir / f"{main_tex.stem}.log"
    log_text = log_file.read_text(encoding="utf-8", errors="replace") if log_file.exists() else ""
    ok = process.returncode == 0 and generated.exists()
    found = parse_latex_errors(process.stdout)
    pages = LATEX_PAGES.findall(log_text)

    out_log.write_text(
        f"Focus build of {relative.as_posix()} ({focus}): {'SUCCESS' if ok else 'FAILED'}\n"
        + "".join(f"{error_text(doc_name(main_tex), item)}\n" for item in found)
        + "\n===== LaTeX output =====\n" + process.stdout + "\n===== LaTeX log =====\n" + log_text,
        encoding="utf-8",
    )
    seconds = time.monotonic() - began

    if ok:
        shutil.copy(generated, out_pdf)
        count = f"{pages[-1]} pages, " if pages else ""
        info(paint(f"SUCCESS: {out_pdf.relative_to(ROOT_DIR)}", GREEN) + f" ({count}{seconds:.1f}s, preview)")
    else:
        for item in found[:MAX_SUMMARY_ERRORS]:
            info("  " + error_text(doc_name(main_tex), item))
        info(paint(f"FAILED:  {relative.as_posix()} (focus {focus}); log: {out_log.relative_to(ROOT_DIR)}", RED))
    return ok


def build_safely(
    main_tex: Path, latexmk: str, live: bool, force: bool, fig_jobs: int = DEFAULT_JOBS, record: bool = False,
) -> tuple[dict, str]:
    """
    build_document, but an exception (OSError and the like) fails only this
    document: its report entry carries the message and the run goes on.
    """
    try:
        return build_document(main_tex, latexmk, live, force, fig_jobs, record)
    except Exception as exc:  # noqa: BLE001 - one document must not stop the others.
        message = f"Build crashed: {exc}"
        error(f"{main_tex.relative_to(ROOT_DIR).as_posix()}: {message}")
        name = doc_name(main_tex)
        entry = {
            "name": name,
            "pdf": output_path_for(main_tex).relative_to(OUT_DIR).as_posix(),
            "log": log_path_for(main_tex).relative_to(OUT_DIR).as_posix(),
            "ok": False,
            "seconds": None,
            "engine": None,
            "pages": None,
            "size": None,
            "errors": [],
            "warnings": 0,
            "error": message,
            "phases": None,
        }
        return entry, ""


def build_parallel(documents: list[Path], latexmk: str, jobs: int, force: bool) -> list[dict]:
    """
    Build in a thread pool. Ctrl+C cancels the queued documents and re-raises
    KeyboardInterrupt; the running ones finish or die with the terminal's SIGINT.
    """
    pool = ThreadPoolExecutor(max_workers=jobs)
    fig_jobs = max(1, DEFAULT_JOBS // jobs)
    futures = [pool.submit(build_safely, document, latexmk, False, force, fig_jobs) for document in documents]
    results: list[dict] = []

    try:
        for future in as_completed(futures):
            entry, text = future.result()
            write(text)
            results.append(entry)
            info()
    except KeyboardInterrupt:
        pool.shutdown(wait=False, cancel_futures=True)
        raise

    pool.shutdown()
    return results


REPORT_LOCK = threading.RLock()  # Callers that read-modify-write build-report.json hold it.
FOCUS_TIMEOUT = 600


def write_report(entries: list[dict]) -> None:
    """
    Write out/build-report.json for the documents built in this run.
    """
    OUT_DIR.mkdir(parents=True, exist_ok=True)

    report = {"documents": sorted(entries, key=lambda entry: entry["name"])}
    text = json.dumps(report, indent=2) + "\n"
    target = OUT_DIR / "build-report.json"
    tmp = target.with_name(f"build-report.{os.getpid()}.{threading.get_ident()}.tmp")
    try:
        tmp.write_text(text, encoding="utf-8")
        os.replace(tmp, target)
    finally:
        tmp.unlink(missing_ok=True)


def open_pdf(path: Path) -> None:
    """
    Open a PDF in the platform's default viewer.
    """
    try:
        if sys.platform == "win32":
            os.startfile(path)  # type: ignore[attr-defined]
        else:
            opener = "open" if sys.platform == "darwin" else "xdg-open"
            subprocess.Popen(
                [opener, str(path)],
                stdin=subprocess.DEVNULL,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
            )
    except OSError as exc:
        error(f"Could not open {path}: {exc}")


def latest_file(main_tex: Path) -> str | None:
    """The most recently modified file of a document (any kind), relative to its directory."""
    files = [path for path in main_tex.parent.rglob("*") if path.is_file() and not path.name.startswith(".")]
    if not files:
        return None
    return max(files, key=lambda path: path.stat().st_mtime).relative_to(main_tex.parent).as_posix()


def watch_target(document: Path, focus: str | None) -> str | None:
    """
    What to focus on after a change: the --focus path, or with "auto" the
    top-level file that reads the file saved last. None means a full build,
    also when the newest file is main.tex, a class, a bibliography, a figure ...
    """
    if focus != "auto":
        return focus

    mapfile = cache_dir_for(document) / f"{document.stem}.focusmap"
    changed = latest_file(document)
    if changed is None or changed == document.name or not changed.endswith(".tex") or not mapfile.exists():
        return None
    return accel.top_unit(accel.read_focusmap(mapfile), changed)


def watch(
    latexmk: str, patterns: list[str], open_pdfs: bool, focus: str | None = None, interval: float = 0.5,
) -> int:
    """
    Poll for changes and rebuild stale documents until interrupted.
    """
    # ponytail: stdlib polling, not OS file events; fine for a handful of documents.
    info("Watching for changes. Press Ctrl+C to stop.")
    info()

    # Input timestamp of the last failed build, so a broken document is not
    # retried until something changes.
    failed: dict[Path, float] = {}

    # Input timestamp seen on the previous poll. A document is built only once
    # its inputs have stayed the same for a full interval, so a save that
    # writes several files triggers one build.
    seen: dict[Path, float] = {}
    built: dict[Path, float] = {}
    opened: set[Path] = set()

    try:
        while True:
            everything = find_documents()
            prune(all_documents())

            for document in select_documents(everything, patterns):
                newest = newest_input(document)

                # With --focus the full PDF stays old, so track the last build ourselves.
                if focus and document not in built and focus == "auto" and not is_stale(document):
                    built[document] = newest
                if focus:
                    stale = built.get(document, 0.0) < newest
                else:
                    stale = is_stale(document)

                if not stale:
                    seen.pop(document, None)
                    continue

                if failed.get(document) == newest:
                    continue

                if seen.get(document) != newest:
                    seen[document] = newest
                    continue

                del seen[document]
                target = watch_target(document, focus) if focus else None
                if target:
                    entry = {"ok": build_focus(document, latexmk, target), "pdf": focus_paths(document)[0]}
                    entry["pdf"] = entry["pdf"].relative_to(OUT_DIR).as_posix()
                else:
                    entry, _ = build_safely(document, latexmk, True, False)

                if entry["ok"]:
                    built[document] = newest
                    failed.pop(document, None)

                    if open_pdfs and document not in opened:
                        opened.add(document)
                        open_pdf(OUT_DIR / entry["pdf"])
                else:
                    failed[document] = newest

                info()

            time.sleep(interval)
    except KeyboardInterrupt:
        info()
        return 0


# ---------------------------------------------------------------------------
# Cleaning
# ---------------------------------------------------------------------------

def prune(documents: list[Path]) -> None:
    """
    Delete PDFs and logs in out/ that no document in `documents` owns. Pass
    all_documents(): then building files/ never deletes the outputs of bench/,
    or the other way round.
    """
    expected = {output_path_for(document) for document in documents}
    expected |= {log_path_for(document) for document in documents}
    expected |= {path for document in documents for path in focus_paths(document)}
    expected |= {docx_path_for(document) for document in documents}

    for path in [*OUT_DIR.rglob("*.pdf"), *OUT_DIR.rglob("*.log"), *OUT_DIR.rglob("*.docx")]:
        if path not in expected:
            info(f"Removing stale: {path.relative_to(ROOT_DIR)}")
            path.unlink()


def clean() -> None:
    """
    Remove all generated build output and the LaTeX cache.
    """
    targets = [path for path in (OUT_DIR, CACHE_DIR) if path.exists()]

    if not targets:
        info("Nothing to clean.")
        return

    for path in targets:
        info(f"Removing: {path.relative_to(ROOT_DIR)}")

        try:
            shutil.rmtree(path)
        except OSError as exc:
            error(f"Could not remove {path.name}: {exc}")
            raise SystemExit(1)

    info("Clean complete.")


# ---------------------------------------------------------------------------
# DOCX export (optional: needs pandoc)
# ---------------------------------------------------------------------------

def export_docx(main_tex: Path) -> tuple[bool, str]:
    """
    pandoc main.tex -> out/<name>.docx, run in the document's directory, with every .bib there as bibliography.
    Returns (ok, message). Pandoc reads \\input files itself, so callers must not use this on untrusted LaTeX.
    """
    pandoc = shutil.which("pandoc")
    if not pandoc:
        return False, "pandoc was not found on your PATH (https://pandoc.org/installing.html)."

    target = docx_path_for(main_tex)
    target.parent.mkdir(parents=True, exist_ok=True)
    folder = main_tex.parent
    command = [pandoc, main_tex.name, "-o", str(target), f"--resource-path={folder}"]
    bibs = sorted(path.name for path in folder.glob("*.bib"))
    if bibs:
        command += ["--citeproc", *(f"--bibliography={name}" for name in bibs)]

    try:
        result = subprocess.run(command, cwd=folder, capture_output=True, text=True, timeout=120)
    except (OSError, subprocess.TimeoutExpired) as exc:
        return False, f"pandoc failed: {exc}"

    if result.returncode != 0:
        return False, (result.stderr.strip() or f"pandoc exited with {result.returncode}").splitlines()[0]

    return True, display(target)


def export_docx_all(documents: list[Path]) -> int:
    """The --docx step: export each document, print one line each; 1 if any failed."""
    failed = 0
    for document in documents:
        ok, message = export_docx(document)
        (info if ok else error)(f"DOCX {doc_name(document)}: {message}")
        failed += not ok
    return 1 if failed else 0


# ---------------------------------------------------------------------------
# Creating documents
# ---------------------------------------------------------------------------

NEW_TEMPLATE = r"""\documentclass{article}

\title{%(title)s}
\date{\today}

\begin{document}

\maketitle

\section{Introduction}

\end{document}
"""

LATEX_SPECIAL = {
    "\\": r"\textbackslash{}", "{": r"\{", "}": r"\}", "$": r"\$", "&": r"\&",
    "#": r"\#", "^": r"\textasciicircum{}", "_": r"\_", "%": r"\%", "~": r"\textasciitilde{}",
}


# Extra templates: {relative file: text}. "article" is NEW_TEMPLATE above; %(title)s is filled in main.tex only.
TEMPLATES = {
    "report": {
        "main.tex": r"""\documentclass[11pt]{report}
\usepackage{graphicx}
\graphicspath{{figures/}}

\title{%(title)s}
\author{Author}
\date{\today}

\begin{document}

\maketitle
\tableofcontents

\input{chapters/introduction}
\input{chapters/methods}
\input{chapters/conclusion}

\bibliographystyle{plain}
\bibliography{refs}

\end{document}
""",
        "chapters/introduction.tex": "\\chapter{Introduction}\n\nPrior work~\\cite{knuth84} goes here.\n",
        "chapters/methods.tex": "\\chapter{Methods}\n\nDescribe the approach.\n",
        "chapters/conclusion.tex": "\\chapter{Conclusion}\n\nSummarise the results.\n",
        "refs.bib": "@book{knuth84,\n  author    = {Donald E. Knuth},\n  title     = {The {TeXbook}},\n"
                    "  publisher = {Addison-Wesley},\n  year      = {1984}\n}\n",
        "figures/README.txt": "Put figures here (png, jpg, pdf); \\includegraphics{name} finds them.\n",
        "build.toml": '# engine = "xelatex"\n# timeout = 600\n# externalize = false\n# lang = "en-US"\n',
    },
    "beamer": {
        "main.tex": r"""\documentclass{beamer}

\title{%(title)s}
\author{Author}
\date{\today}

\begin{document}

\frame{\titlepage}

\begin{frame}{Outline}
  \begin{itemize}
    \item First point
    \item Second point
  \end{itemize}
\end{frame}

\end{document}
""",
    },
    "letter": {
        "main.tex": r"""\documentclass{letter}
\signature{Your name}
\address{Your address}

\begin{document}

\begin{letter}{Recipient\\Address}
\opening{Dear Sir or Madam,}

Body of the letter (%(title)s).

\closing{Yours sincerely,}
\end{letter}

\end{document}
""",
    },
}


def new_document(name: str, template: str = "article") -> int:
    """
    Create files/<name>/main.tex (plus companion files for some templates). Never overwrites.
    """
    path = Path(name)

    if not name.strip() or path.is_absolute() or ".." in path.parts:
        error(f"Invalid document name: {name!r}")
        return 1

    target = SOURCE_DIR / path / "main.tex"

    if target.exists():
        error(f"Already exists: {display(target)}")
        return 1

    title = "".join(LATEX_SPECIAL.get(char, char) for char in name)
    files = TEMPLATES.get(template) or {"main.tex": NEW_TEMPLATE}

    for rel, text in files.items():
        out = target.parent / rel
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text(text % {"title": title} if rel == "main.tex" else text, encoding="utf-8")

    info(f"Created: {display(target)}")
    return 0


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Build all LaTeX documents containing main.tex."
    )

    parser.add_argument(
        "docs",
        nargs="*",
        metavar="DOC",
        help="Only build these documents: a name relative to files/ "
             "(e.g. 'reports/final'), or a glob (e.g. 'reports/*'). Default: all.",
    )

    parser.add_argument(
        "--timeout",
        type=int,
        metavar="SECONDS",
        help=f"Wall-clock limit for one latexmk run (default {DEFAULT_TIMEOUT}; a build.toml timeout overrides it).",
    )

    parser.add_argument(
        "--clean",
        action="store_true",
        help="Remove out/ and the .latex-cache/ directory, then exit.",
    )

    parser.add_argument(
        "--new",
        metavar="NAME",
        help="Create files/NAME/main.tex from a template and exit.",
    )

    parser.add_argument(
        "--template",
        choices=["article", "report", "beamer", "letter"],
        default="article",
        help="Template for --new (default article; report adds chapters/, refs.bib, figures/, build.toml).",
    )

    parser.add_argument(
        "--docx",
        action="store_true",
        help="After building, also export each selected document to out/<name>.docx with pandoc (must be installed).",
    )

    parser.add_argument(
        "--list",
        action="store_true",
        help="List discovered main.tex files without building them.",
    )

    parser.add_argument(
        "--changed-since",
        metavar="REF",
        help="Only consider documents with files changed since REF (git ref).",
    )

    parser.add_argument(
        "-j",
        "--jobs",
        type=int,
        default=DEFAULT_JOBS,
        metavar="N",
        help=f"Build N documents in parallel (default: {DEFAULT_JOBS}). "
             "A parallel build's output is printed when that document finishes.",
    )

    parser.add_argument(
        "--force",
        action="store_true",
        help="Rebuild documents even if their PDF is up to date.",
    )

    parser.add_argument(
        "--watch",
        action="store_true",
        help="Keep running and rebuild documents whenever their files change.",
    )

    parser.add_argument(
        "--open",
        action="store_true",
        help="With --watch: open each PDF in its default viewer after its first successful build.",
    )

    parser.add_argument(
        "--source",
        metavar="DIR",
        help="Build the documents under DIR (relative to the repository root, e.g. bench) "
             "instead of files/. Outputs go to out/ as usual, named relative to DIR.",
    )

    parser.add_argument(
        "--profile",
        action="store_true",
        help="Show how long the figures and LaTeX phases of each document took, in the summary.",
    )

    parser.add_argument(
        "--focus",
        metavar="PATH",
        help="Preview only PATH (a file or directory relative to the document, e.g. Chapters/chapter5) in "
             "out/<name>.focus.pdf, using the last full build's references and numbering. "
             "With --watch, 'auto' focuses on the part holding the file saved last.",
    )

    args = parser.parse_args()

    if args.focus == "auto" and not args.watch:
        parser.error("--focus auto needs --watch")

    if args.jobs < 1:
        parser.error("--jobs must be at least 1")

    if args.timeout is not None and not TIMEOUT_RANGE[0] <= args.timeout <= TIMEOUT_RANGE[1]:
        parser.error(f"--timeout must be {TIMEOUT_RANGE[0]} to {TIMEOUT_RANGE[1]}")

    if args.open and not args.watch:
        parser.error("--open needs --watch")

    return args


def size_text(size: int) -> str:
    """"101 KB", "1.6 MB"."""
    return f"{size / 1e6:.1f} MB" if size >= 1e6 else f"{max(1, round(size / 1e3))} KB"


def phase_text(phases: dict) -> str:
    """"figures 1.2s, latex 5.6s"."""
    return ", ".join(f"{name} {seconds:.1f}s" for name, seconds in phases.items())


def print_summary(results: list[dict], profile: bool = False) -> None:
    """
    One line per document, then the errors of the failed ones. With profile,
    each document also gets its phase times.
    """
    separator()
    info("Build Summary")
    separator()

    width = max(len(entry["name"]) for entry in results)

    for entry in results:
        status = paint("SUCCESS", GREEN) if entry["ok"] else paint("FAILED ", RED)

        details = [entry["engine"] or "-"]
        if entry["pages"] is not None:
            details.append(f"{entry['pages']} page{'' if entry['pages'] == 1 else 's'}")
        if entry.get("size"):
            details.append(size_text(entry["size"]))
        if entry["warnings"]:
            details.append(f"{entry['warnings']} warning(s)")

        seconds = "" if entry["seconds"] is None else f"{entry['seconds']:6.1f}s"
        info(f"  {status}  {entry['name']:<{width}}  {seconds:>7}  {', '.join(details)}")

        if entry["error"]:
            info(f"      ERROR: {entry['error']}")

        if profile and entry["phases"]:
            info(f"      phases: {phase_text(entry['phases'])}")

        # ponytail: at most MAX_SUMMARY_ERRORS shown per document; the log has all.
        for found in entry["errors"][:MAX_SUMMARY_ERRORS]:
            info(f"      {error_text(entry['name'], found)}")
            if found["hint"]:
                info(f"        hint: {found['hint']}")

        hidden = len(entry["errors"]) - MAX_SUMMARY_ERRORS
        if hidden > 0:
            info(f"      ... {hidden} more in out/{entry['log']}")

    successes = sum(entry["ok"] for entry in results)

    info()
    info(f"Successful: {successes}")
    info(f"Failed:     {len(results) - successes}")
    info(f"Total:      {len(results)}")
    separator()


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main() -> int:
    global SOURCE_DIR

    args = parse_args()
    if args.timeout is not None:
        global DEFAULT_TIMEOUT
        DEFAULT_TIMEOUT = args.timeout

    if args.source:
        source = Path(args.source) if Path(args.source).is_absolute() else ROOT_DIR / args.source
        if not source.is_dir():
            error(f"--source {args.source}: not a directory")
            return 2
        SOURCE_DIR = source.resolve()

    if args.clean:
        clean()
        return 0

    if args.new is not None:
        return new_document(args.new, args.template)

    documents = find_documents()

    unknown = unknown_patterns(documents, args.docs)
    if unknown:
        error(f"No document matches: {', '.join(unknown)}")
        info("Available documents:")
        for document in documents:
            info(f"  {doc_name(document)}")
        return 2

    if not args.list:
        prune(all_documents())

    if not documents:
        info(f"No main.tex files found under {SOURCE_DIR.relative_to(ROOT_DIR)}/.")
        return 0

    documents = select_documents(documents, args.docs)

    if args.changed_since:
        documents = filter_changed(documents, args.changed_since)

        if not documents:
            info(f"No documents changed since {args.changed_since}.")
            return 0

    if args.list:
        info("Discovered LaTeX documents:")
        info()

        for document in documents:
            output = output_path_for(document)
            info(f"  {document.relative_to(ROOT_DIR)}")
            status = "" if is_stale(document) else "  (up to date)"
            info(f"    -> {output.relative_to(ROOT_DIR)}{status}")

        return 0

    if args.docx and not shutil.which("pandoc"):
        error("--docx needs pandoc, which was not found on your PATH (https://pandoc.org/installing.html).")
        return 2

    latexmk = check_latex()

    if args.focus and not args.watch:
        if len(documents) != 1:
            error("--focus needs exactly one document; name it, e.g. build.py sample-report --focus Chapters/ch1")
            return 2
        return 0 if build_focus(documents[0], latexmk, args.focus) else 1

    if args.watch:
        return watch(latexmk, args.docs, args.open, args.focus)

    exported = documents

    if not args.force:
        documents = [document for document in documents if is_stale(document)]

        if not documents:
            info("Everything is up to date.")
            return export_docx_all(exported) if args.docx else 0

    separator()
    info(f"Found {len(documents)} document(s).")
    info()

    # One document runs live; several run in a pool, printed as each finishes.
    jobs = min(args.jobs, len(documents))

    try:
        if jobs == 1:
            results = []
            for document in documents:
                results.append(build_safely(document, latexmk, True, args.force)[0])
                info()
        else:
            results = build_parallel(documents, latexmk, jobs, args.force)
    except KeyboardInterrupt:
        info()
        error("Interrupted; queued documents were not built.")
        return 130

    write_report(results)
    print_summary(results, args.profile)

    failed = any(not entry["ok"] for entry in results)
    if args.docx:  # Also after a failed build: pandoc reads the sources, not the PDF.
        failed = bool(export_docx_all(exported)) or failed

    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
