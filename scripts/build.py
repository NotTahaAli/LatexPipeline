#!/usr/bin/env python3

from __future__ import annotations

import argparse
import fnmatch
import functools
import hashlib
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
import zipfile
import zlib
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

import a11y
import accel
import hints
import sandbox

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------

# A PyInstaller binary (see .github/workflows/binaries.yml) runs from its unpack
# folder, so the project is the directory it is started in.
FROZEN = getattr(sys, "frozen", False)
SCRIPT_DIR = Path(__file__).resolve().parent
ROOT_DIR = Path.cwd().resolve() if FROZEN else SCRIPT_DIR.parent
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
    "scripts/sandbox.py",
    "scripts/hints.py",
    "scripts/a11y.py",
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
    "TEXMFROOT", "TEXMFMAIN", "TEXMFDIST", "TEXMFLOCAL", "TEXMFSYSCONFIG", "TEXMFSYSVAR", "TEXMFVAR", "TEXMFCONFIG",
    "TEXMFDEBIAN",  # Debian's /usr/share/texmf (fonts, lm); empty elsewhere
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
    --source directory), else files/ or bench/, else the document folder's
    parent (a document the editor was pointed at directly). Output and cache
    names are relative to it.
    """
    for root in (SOURCE_DIR, FILES_DIR, BENCH_DIR):
        if root in main_tex.parents:
            return root
    return main_tex.parent.parent


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


def path_key(path: Path) -> str:
    """
    The resolved path, casefolded on a case-insensitive file system (macOS, Windows), so
    `--source ~/Thesis` and `--source ~/thesis` give one cache and history key there.
    """
    text = str(path.resolve())
    flipped = text.swapcase()
    try:
        if flipped != text and os.path.samefile(text, flipped):
            return text.casefold()
    except OSError:  # the other casing does not exist: a case-sensitive file system
        pass
    return text


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
        try:
            prefix = root.relative_to(ROOT_DIR).as_posix()
        except ValueError:
            # A --source outside the repository: keyed by its resolved path. "_x" never comes out of
            # escape_name ("_" is always followed by two hex digits), so no in-repo name collides.
            digest = hashlib.sha256(path_key(root).encode()).hexdigest()[:16]
            return CACHE_DIR / (f"_x{digest}" + ("" if name == "." else "_2F" + escape_name(name)))
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

    try:
        sandbox.check()
    except sandbox.SandboxError as exc:
        error(str(exc))
        raise SystemExit(1)

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
    try:
        # One call for all variables ("|" cannot occur in a path on Windows).
        result = subprocess.run(
            ["kpsewhich", "-expand-var=" + "|".join(f"${variable}" for variable in TEX_TREE_VARIABLES)],
            capture_output=True, text=True, check=True,
        )
    except (OSError, subprocess.CalledProcessError):
        return None
    return tuple(Path(value).resolve() for value in result.stdout.strip().split("|") if value)


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

    Files whose real path is inside the document directory are skipped (newest_input scans it; a symlinked
    folder that leads elsewhere does count), and so
    are the TeX installation, the cache and out/: the cache holds generated
    figures and .inject files, rewritten on every build.
    """
    recorder = cache_dir_for(main_tex) / f"{main_tex.stem}.fls"
    tree = tex_tree_dirs()

    if tree is None or not recorder.exists():
        return []

    # A TEXMF* value that contains the repository (say "/") would hide every input.
    skip = [path for path in (*tree, CACHE_DIR, OUT_DIR) if path not in ROOT_DIR.parents and path != ROOT_DIR]
    # Plain strings: a .fls of a big document lists thousands of paths, and pathlib costs 0.7 s on them.
    # The document directory is tested only after realpath: a symlinked folder inside it
    # (files/doc/shared -> ../../shared) holds files that the directory scan does not reach.
    outside = tuple(os.path.normcase(os.path.join(str(path), "")) for path in skip)
    inside = os.path.normcase(os.path.join(str(main_tex.parent.resolve()), ""))
    base = str(main_tex.parent)
    found: dict[str, None] = {}
    seen: set[str] = set()

    for line in recorder.read_text(encoding="utf-8", errors="replace").splitlines():
        if not line.startswith("INPUT "):
            continue
        name = os.path.normpath(os.path.join(base, line[len("INPUT "):].strip()))
        if name in seen or os.path.normcase(name).startswith(outside):
            continue
        seen.add(name)
        name = os.path.realpath(name)
        folded = os.path.normcase(name)
        if not folded.startswith(outside) and not folded.startswith(inside):
            found[name] = None

    return [Path(name) for name in found]


def newest_input(main_tex: Path) -> float:
    """
    Newest modification time among a document's inputs: every file in its
    directory tree (dotfiles such as .DS_Store excluded), the files outside it
    that the last build read (recorded_inputs), and build.py and accel.py.
    """
    # The scripts that decide what a build produces; editing serve.py or ci_report.py rebuilds nothing.
    scripts = [Path(sys.executable)] if FROZEN else [Path(__file__), SCRIPT_DIR / "accel.py"]
    newest = max(path.stat().st_mtime for path in scripts)

    # Directories count too: deleting or renaming an input changes only its folder's time.
    try:
        newest = max(newest, main_tex.parent.stat().st_mtime)
    except OSError:
        pass
    for path in main_tex.parent.rglob("*"):
        if path.name.startswith("."):
            continue
        try:
            newest = max(newest, path.stat().st_mtime)
        except OSError:
            pass  # Deleted mid-scan.

    for path in recorded_inputs(main_tex):
        try:
            newest = max(newest, path.stat().st_mtime)
        except OSError:
            return float("inf")  # Read by the last build, gone now: stale until the next build drops it.

    return newest


def is_stale(main_tex: Path) -> bool:
    """
    True if the PDF is missing or older than any of the document's inputs.
    """
    output = output_path_for(main_tex)

    try:
        if newest_input(main_tex) > output.stat().st_mtime:
            return True
    except FileNotFoundError:
        return True
    return review_switched(main_tex)


def review_switched(main_tex: Path) -> bool:
    """The last build used other review markup than --review or build.toml asks for now (e.g. --review show before)."""
    try:
        recorded = (cache_dir_for(main_tex) / ".mode").read_text()
        if REVIEW_MODE is None and "\\lp@" not in recorded:
            return False  # review markup neither asked for nor used: no settings to read
        settings = read_settings(main_tex)
    except (OSError, ValueError, ConfigError):
        return False
    meta = document_metadata(settings) + review_pretex(settings)
    return recorded not in (f"plain{meta}", f"externalized{meta}")


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


def arxiv_path_for(main_tex: Path) -> Path:
    """files/reports/final/main.tex -> out/reports/final.arxiv.zip (--arxiv, never published)."""
    pdf = output_path_for(main_tex)
    return pdf.with_name(f"{pdf.stem}.arxiv.zip")


def html_paths(main_tex: Path) -> tuple[Path, Path]:
    """
    files/reports/final/main.tex -> (out/reports/final.html/, out/reports/final.html.zip): --html. A folder of its
    own, not out/reports/final/, which is where the outputs of a document in files/reports/final/x/ go.
    """
    pdf = output_path_for(main_tex)
    return pdf.with_name(f"{pdf.stem}.html"), pdf.with_name(f"{pdf.stem}.html.zip")


# ---------------------------------------------------------------------------
# Per-document settings
# ---------------------------------------------------------------------------

# "% !TEX program = xelatex" (also "% !TeX TS-program = ..."), in the first lines.
ENGINE_MAGIC = re.compile(r"^\s*%\s*!\s*TEX\s+(?:TS-)?PROGRAM\s*=\s*(\S+)", re.IGNORECASE)
CONFIG_KEYS = {"engine", "shell_escape", "latexmk_args", "externalize", "pdfa", "tagged", "lang", "timeout", "review",
               "html", "grammar", "grammar_url", "disabled_rules"}  # the last three are read by grammar.py
# Wall-clock limit for one latexmk run, in seconds (build.toml "timeout"; --timeout changes the default).
DEFAULT_TIMEOUT = 600
TIMEOUT_RANGE = (10, 7200)
REVIEW_MODE: str | None = None  # --review show|clean: overrides build.toml's review for every document
PDFA_LEVEL = re.compile(r"^(?:a-)?([123][abu]|4[ef]?)$")

# ponytail: errors are read from the console; -file-line-error puts each on one "file:line: message" line.
LATEX_ERROR = re.compile(r"^(?P<file>.+?):(?P<line>\d+): (?P<message>\S.*)$")
LATEX_WARNING = re.compile(r"^(?:LaTeX|Package|Class)\b.*\bWarning", re.MULTILINE)
LATEX_PAGES = re.compile(r"^Output written on .*?\((\d+)\s+pages?", re.MULTILINE | re.DOTALL)


class ConfigError(Exception):
    """A document's settings are invalid. The message goes into its log."""


def display(path: Path) -> str:
    """Path relative to the repository root, with forward slashes (absolute outside it: --source /elsewhere)."""
    try:
        return path.relative_to(ROOT_DIR).as_posix()
    except ValueError:
        return path.as_posix()


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
                "pdfa": None, "tagged": False, "lang": "en-US", "timeout": DEFAULT_TIMEOUT, "review": "off",
                "html": False}

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
    if REVIEW_MODE:
        settings["review"] = REVIEW_MODE

    engine = settings["engine"]
    if not isinstance(engine, str) or engine.lower() not in ENGINES:
        raise ConfigError(
            f"{display(main_tex)}: unknown engine {engine!r} "
            "(use pdflatex, xelatex or lualatex)"
        )
    settings["engine"] = engine.lower()

    for key in ("shell_escape", "externalize", "tagged", "html"):
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

    if not (isinstance(settings["review"], str) and settings["review"] in REVIEW_PRETEX):
        raise ConfigError(f"{display(main_tex.parent / 'build.toml')}: review must be \"show\", \"clean\" or \"off\"")

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


# \DocumentMetadata keys for tagging: the kernel has tagging=on from 2025-06-01; TeX Live 2023 to 2025
# (kernel 2023-11 on) has the same through testphase. Older kernels cannot tag, so the build goes on untagged.
# PDF/A-4 (PDF 2.0) is taken from the 2025-06-01 kernel on.
TAGGING_NEW = "2025-06-01"
TAGGING_OLD = "2023-11-01"

# pdfTeX before TeX Live 2025 (pdftexspace.tfm says 0.334249, the font program 333) writes the interword space
# glyph that tagging adds with a width veraPDF rejects; the old "dummy-space" font is consistent. \pdfspacefont
# exists from pdfTeX 1.40.25; a TeX Live tree with the fixed metrics keeps the default font.
SPACE_FONT_FIX = (r"\ifdefined\pdfspacefont\font\ltxspacetest=pdftexspace \relax "
                  r"\ifdim\fontcharwd\ltxspacetest 32>3.335pt \pdfspacefont{dummy-space}\fi\fi")
# PDF/A-1, A-2 and A-4 (without f) forbid embedded files that are not PDF/A. The tagging kernel attaches the
# TeX source of formulas and two CSS files (allowed in A-3 and A-4f); each key is set only if tagpdf has it.
NO_ATTACHED_FILES = (r"\ifdefined\tagpdfsetup\ExplSyntaxOn"
                     r"\bool_if_exist:NT\g__tag_css_bool{\tagpdfsetup{attach-css=false}}"
                     r"\bool_if_exist:NT\l__tag_math_texsource_AF_bool{\tagpdfsetup{math/tex/AF=false}}"
                     r"\ExplSyntaxOff\fi")


def document_metadata(settings: dict) -> str:
    """The pretex for build.toml's pdfa and tagged (PDF/A, tagged PDF/UA), or "" when both are off."""
    level, tagged = settings["pdfa"], settings["tagged"]
    if not level and not tagged:
        return ""
    lang = f"lang={settings['lang']}"
    extras = []
    if level:
        extras = [
            # veraPDF: xcolor's cmyk colours break PDF/A with the RGB OutputIntent, and pdfTeX
            # writes no ToUnicode for symbol glyphs (CMEX) without the glyph name maps.
            r"\PassOptionsToPackage{rgb}{xcolor}",
            r"\ifdefined\pdfgentounicode\input{glyphtounicode}\InputIfFileExists{glyphtounicode-cmr}{}{}"
            r"\pdfgentounicode=1 \fi"]
        if level.startswith("a-1"):
            # PDF/A-1 forbids object streams.
            extras.append(r"\ifdefined\pdfobjcompresslevel\pdfobjcompresslevel=0 \fi"
                          r"\ifdefined\pdfvariable\pdfvariable objcompresslevel=0 \fi")

    def metadata(*standards: str, tail: str = "") -> str:
        names = standards[0] if len(standards) == 1 else "{" + ",".join(standards) + "}"
        return f"\\DocumentMetadata{{pdfstandard={names},{lang}{tail}}}"

    def warning(need: str) -> str:
        what = "tagged = true" if tagged else f"pdfa = {level}"
        return rf"\typeout{{WARNING: {what} needs LaTeX {need} or newer; not applied.}}"

    pdf2 = bool(level) and level.startswith("a-4")  # PDF 2.0: PDF/UA-2
    if not tagged:
        if pdf2:
            return (rf"\IfFormatAtLeastTF{{{TAGGING_NEW}}}{{{metadata(level)}}}{{{warning(TAGGING_NEW)}}}"
                    + "".join(extras))
        return metadata(level) + "".join(extras)
    # Kernel 2025-06 takes the list "pdfstandard={a-2a,ua-1}" (LaTeX News 41); the testphase of TeX Live 2023
    # to 2025 takes one standard, so a PDF/A + tagged file carries no PDF/UA identification there.
    ua = "ua-2" if pdf2 else "ua-1"
    new = metadata(level, ua, tail=",tagging=on") if level else metadata(ua, tail=",tagging=on")
    if pdf2:
        old = warning(TAGGING_NEW)
    else:
        phase = metadata(level or ua, tail=",testphase={phase-III,firstaid}")
        old = rf"\IfFormatAtLeastTF{{{TAGGING_OLD}}}{{{phase}}}{{{warning(TAGGING_OLD)}}}"
    return (
        rf"\IfFormatAtLeastTF{{{TAGGING_NEW}}}{{{new}}}{{{old}}}"
        # A tagged PDF/UA file must show its title (set pdftitle with \hypersetup in the document).
        r"\PassOptionsToPackage{pdfdisplaydoctitle=true}{hyperref}" + SPACE_FONT_FIX
        + (NO_ATTACHED_FILES if level and (level[2] in "12" or level == "a-4") else "")
        + "".join(extras)
    )  # one -usepretex argument: no newlines


# build.toml review = "show" | "clean" (README, "Review markup"): \lpadd{new}, \lpdel{old}, \lpedit{old}{new},
# \lpnote{text} and the environments lpaddblock, lpdelblock and lpnoteblock, injected with -usepretex. The definitions
# wait for \begin{document} (hook begindocument/before), and a name the document or a package already defined is
# left alone: the document's own macro wins. "show" loads xcolor and ulem (normalem, unless the document loaded
# ulem itself), "clean" no package. Each value is one -usepretex argument: no newlines.
REVIEW_PROVIDE = (r"\def\lp@provide#1#2{\ifdefined#1\else\let#1#2\fi}"
                  r"\def\lp@all{\lp@provide\lpadd\lp@add\lp@provide\lpdel\lp@del\lp@provide\lpedit\lp@edit"
                  r"\lp@provide\lpnote\lp@note\lp@provide\lpaddblock\lp@addblock\lp@provide\endlpaddblock\lp@endblock}")
REVIEW_PRETEX = {
    "off": "",
    "show": (
        r"\makeatletter"
        r"\protected\def\lp@add#1{\bgroup\markoverwith{\textcolor{lpaddbg}{\rule[-.5ex]{2pt}{2.5ex}}}\ULon{#1}}"
        r"\protected\def\lp@del#1{\textcolor{lpdelfg}{\sout{#1}}}"
        r"\protected\def\lp@edit#1#2{\lp@del{#1}\lp@add{#2}}"
        r"\protected\def\lp@note#1{{\color{lpnotefg}\sffamily\footnotesize[#1]}}"
        r"\def\lp@addblock{\par\color{lpaddfg}}\def\lp@delblock{\par\color{lpdelfg}}"
        r"\def\lp@noteblock{\par\color{lpnotefg}\sffamily\small}\def\lp@endblock{\par}" + REVIEW_PROVIDE +
        r"\AddToHook{begindocument/before}{\RequirePackage{xcolor}"
        r"\ifcsname ver@ulem.sty\endcsname\else\RequirePackage[normalem]{ulem}\fi"
        r"\definecolor{lpaddbg}{RGB}{198,239,206}\definecolor{lpaddfg}{RGB}{0,110,40}"
        r"\definecolor{lpdelfg}{RGB}{192,0,0}\definecolor{lpnotefg}{RGB}{0,70,160}\lp@all"
        r"\lp@provide\lpdelblock\lp@delblock\lp@provide\endlpdelblock\lp@endblock"
        r"\lp@provide\lpnoteblock\lp@noteblock\lp@provide\endlpnoteblock\lp@endblock}"
        r"\makeatother"
    ),
    "clean": (  # added text plain; deleted text and notes dropped (\@bsphack keeps one space, as \label does)
        r"\makeatletter"
        r"\protected\def\lp@add#1{#1}\protected\def\lp@del#1{\@bsphack\@esphack}\protected\def\lp@edit#1#2{#2}"
        r"\let\lp@note\lp@del\def\lp@addblock{}\def\lp@endblock{}" + REVIEW_PROVIDE +
        r"\AddToHook{begindocument/before}{\lp@all"
        r"\ifdefined\lpdelblock\else\NewDocumentEnvironment{lpdelblock}{+b}{}{}\fi"
        r"\ifdefined\lpnoteblock\else\NewDocumentEnvironment{lpnoteblock}{+b}{}{}\fi}"
        r"\makeatother"
    ),
}


def review_pretex(settings: dict) -> str:
    """The pretex for build.toml's review ("" when off)."""
    return REVIEW_PRETEX[settings["review"]]


def pdf_text(pdf: Path) -> bytes:
    """The PDF with its object streams inflated, so the catalog and the XMP can be searched."""
    data = pdf.read_bytes()
    streams = [data]
    for chunk in re.findall(rb"stream\r?\n(.*?)endstream", data, re.DOTALL):  # object streams hide the catalog
        try:
            streams.append(zlib.decompress(chunk))
        except zlib.error:
            pass
    return b"\n".join(streams)


def xmp_part(text: bytes, name: str) -> int:
    """The part number in the XMP identification (name "pdfaid" or "pdfuaid"), or 0."""
    match = re.search(rb"<%s:part>(\d)</%s:part>|%s:part=\"(\d)\"" % ((name.encode(),) * 3), text)
    return int(match.group(1) or match.group(2)) if match else 0


def tagged_check(pdf: Path) -> str:
    """Does the PDF carry a structure tree and a PDF/UA identification? Returns a one-line note."""
    text = pdf_text(pdf)
    if b"/StructTreeRoot" in text and b"/MarkInfo" in text:
        if xmp_part(text, "pdfuaid"):
            return "Tagged PDF: structure tree present."
        return (f"Tagged PDF: structure tree present, but no PDF/UA identification in the XMP "
                f"(a list with pdfa needs LaTeX {TAGGING_NEW}), so PDF/UA is not validated.")
    return ("Tagged PDF requested, but the PDF has no structure tree: tagging needs LaTeX "
            f"{TAGGING_OLD} or newer (see the LaTeX log).")


def pdfa_check(pdf: Path, level: str, validate: bool = True, tagged: bool = False) -> str:
    """
    A cheap look, not validation (use veraPDF for that): does the PDF carry
    the XMP pdfaid declaration and an OutputIntent? Returns a one-line note.
    """
    text = pdf_text(pdf)
    missing = []
    if not xmp_part(text, "pdfaid"):
        missing.append("XMP pdfaid")
    if b"/OutputIntents" not in text:
        missing.append("OutputIntent")
    if missing:
        need = TAGGING_NEW if level.startswith("a-4") else "2023-06"
        return f"PDF/A {level} requested, but the PDF has no {' or '.join(missing)} (needs LaTeX {need} or newer)."
    verapdf = shutil.which("verapdf") if validate else None
    if verapdf is not None and os.name == "nt" and verapdf.lower().endswith((".bat", ".cmd")) and "%" in str(pdf):
        return f"PDF/A {level}: veraPDF skipped (a .bat launcher cannot take '%' in the path); run veraPDF manually."
    if verapdf is None:
        return (f"PDF/A {level}: XMP pdfaid and OutputIntent present "
                "(not validated; install veraPDF to check conformance).")
    return verapdf_note(verapdf, pdf, level, xmp_part(text, "pdfuaid") if tagged else 0)


def verapdf_note(verapdf: str, pdf: Path, level: str | None, ua: int = 0) -> str:
    """
    Validate with veraPDF (on PATH): a one-line pass, or fail with the broken rules. level is the PDF/A
    level or None; ua is the PDF/UA part the file declares (0: none), checked as flavour ua1 or ua2.
    """
    checks = [(level.removeprefix("a-"), f"PDF/A {level}")] if level else []
    if ua:
        checks.append((f"ua{ua}", f"PDF/UA-{ua}"))
    notes = []
    for flavour, label in checks:
        try:
            result = subprocess.run([verapdf, "--format", "xml", "--flavour", flavour, str(pdf)],
                                    capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=300)
        except (OSError, subprocess.TimeoutExpired) as error:
            notes.append(f"{label}: veraPDF did not run ({error}).")
            continue
        if 'isCompliant="true"' in result.stdout:
            notes.append(f"{label}: veraPDF passed.")
            continue
        rules = list(dict.fromkeys(re.findall(r"<description>([^<]*)</description>", result.stdout)))
        if 'isCompliant="false"' not in result.stdout or not rules:
            notes.append(f"{label}: veraPDF gave no verdict (exit {result.returncode}).")
        else:
            notes.append(f"{label}: veraPDF failed {len(rules)} rule(s): "
                         + " | ".join(html.unescape(r) for r in rules[:3]))
    return " ".join(notes)


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
        if message.startswith("Emergency stop"):
            # A missing file prints "! LaTeX Error: File `x.sty' not found." without file:line, then the
            # file prompt fails in nonstop mode; report that cause, not the stop.
            cause = next((row[2:] for row in reversed(lines[max(0, index - 8):index]) if row.startswith("! ")), None)
            message = cause or message
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


def a11y_text(name: str, found: dict) -> str:
    """An accessibility finding as an error line (files/<doc>/<file>:<line>: message), or the bare message."""
    return error_text(name, found) if found["file"] else found["message"]


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
    record: bool = False, validate: bool = True, review: str | None = None,
    target: tuple[Path, Path] | None = None,
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
    validate=False skips veraPDF (the editor's builds: validation takes seconds).
    review overrides build.toml's review; target = (build directory, PDF) replaces the document's own (the review
    PDF's copy of a document, build_review_pdf).
    """
    began = time.monotonic()
    relative = Path(display(main_tex))
    name = doc_name(main_tex)
    output_pdf = target[1] if target else output_path_for(main_tex)
    log_path = output_pdf.with_suffix(".log") if target else log_path_for(main_tex)

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
    say(f"Output:  {display(output_pdf)}")
    say(RULE)

    output_pdf.parent.mkdir(parents=True, exist_ok=True)

    console: list[str] = []
    errors: list[str] = []
    pages = None
    warnings = 0
    phases = {"figures": 0.0, "latex": 0.0}  # Seconds, for --profile and build-report.json.

    try:
        settings = read_settings(main_tex)
        if review:
            settings["review"] = review
    except ConfigError as exc:
        settings = None
        errors.append(str(exc))

    # Stamp the PDF with the build start time, so edits made while LaTeX is
    # running still count as newer than the PDF.
    started = time.time()

    build_dir = target[0] if target else cache_dir_for(main_tex)
    cached = build_dir.exists()
    build_dir.mkdir(parents=True, exist_ok=True)
    generated_pdf = build_dir / f"{main_tex.stem}.pdf"

    notes: list[str] = []
    access: list[dict] | None = None  # a11y findings, for tagged documents that built

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
                    **sandbox.spawn(command, main_tex.parent, build_dir, {**os.environ, **LATEX_LOG_ENV, **epoch},
                                    settings["timeout"], new_group=True),
                    stdin=subprocess.DEVNULL,
                    stdout=subprocess.PIPE,
                    stderr=subprocess.STDOUT,
                    text=True,
                    errors="replace",
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
                sandbox.scrub(build_dir)
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
                    env={**LATEX_LOG_ENV, **epoch}, cache=None if target else figure_cache_for(main_tex),
                    pretex=review_pretex(settings),
                )
                if force:
                    figures.wipe()
                say("Figures: TikZ externalization (list and make, compiled in parallel)")
                say()

                last = mode_file.stat().st_mtime if mode_file.exists() and not force else 0.0
                # touched() runs every time: it also records the figure text for the next build.
                touched = figures.touched(last)
                if not figures.names() or touched:
                    say("Listing figures ...")
                    began = time.monotonic()
                    # A figure edit: list just the changed files (falls back to the whole document).
                    # Only a full listing rewrites the focusmap, so only it moves figures.recorded.
                    if touched and figures.discover_partial():
                        phases["figures"] += time.monotonic() - began
                    else:
                        figures.mark_recorded()
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
                    extra = [f"-usepretex={pdf_meta}{figures.main_pretex}", f"-jobname={main_tex.stem}"]
                    figures.mark_recorded()
                    code = run(latexmk_command(*extra, *(["-g"] if switched else [])))
                    if code != 0:
                        return code, ""
                return 0, ""
            except Exception as exc:  # noqa: BLE001 - any failure of the optimisation means: build plainly.
                return 1, f"{type(exc).__name__}: {exc}"

        # Switching between plain and externalized output needs a rebuild latexmk can't see.
        mode_file = build_dir / ".mode"
        # \DocumentMetadata must precede \documentclass, which is what -usepretex gives it.
        pdf_meta = document_metadata(settings)
        meta = pdf_meta + review_pretex(settings)  # main_pretex of the figures carries the review markup itself
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
                        notes.append(pdfa_check(output_pdf, settings["pdfa"], validate, settings["tagged"]))
                        say(notes[-1])
                    if settings["tagged"]:
                        notes.append(tagged_check(output_pdf))
                        say(notes[-1])
                        verapdf = shutil.which("verapdf") if validate and not settings["pdfa"] else None
                        ua = xmp_part(pdf_text(output_pdf), "pdfuaid")
                        if verapdf and ua and "structure tree present" in notes[-1]:
                            notes.append(verapdf_note(verapdf, output_pdf, None, ua))
                            say(notes[-1])
                except OSError as exc:
                    errors.append(f"Could not copy generated PDF: {exc}")
                if settings["tagged"] and not errors:
                    try:
                        access = a11y.check(main_tex.parent, pdf_text(output_pdf))
                    except (OSError, ValueError) as exc:
                        access = []
                        notes.append(f"Accessibility check skipped: {exc}")
                    for found in access:
                        say(f"Accessibility: {a11y_text(name, found)}")

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
        *(f"ACCESSIBILITY: {a11y_text(name, found)}\n  Hint: {found['hint']}\n" for found in access or []),
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
    say(f"Log:     {display(log_path)}")
    say(f"Time:    {seconds:.1f}s")

    if errors:
        say(paint(f"FAILED:  {relative.as_posix()}", RED))
    else:
        say(paint(f"SUCCESS: {display(output_pdf)}", GREEN))

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
    if access is not None:
        entry["a11y"] = access

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
        error(f"{display(main_tex)}: focus build crashed: {exc}")
        return False


def _build_focus(main_tex: Path, latexmk: str, focus: str, fig_jobs: int) -> bool:
    """
    Typeset only the part of a document under `focus` (a path relative to the
    document, e.g. "Chapters/chapter5") into out/<name>.focus.pdf, with one
    LaTeX run that reads the last full build's .aux files, so references,
    citations, counters and page numbers match the full document.
    Returns True on success.
    """
    relative = Path(display(main_tex))
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

    bump = accel.next_page(entries[first], main_tex.parent) if entries else 0  # see accel.next_page
    focus = accel.focus_tex(entries, keep, first, bump)
    pretex = accel.write_inject(focus_dir, main_tex.parent, "_focus.tex", focus)
    command = [
        settings["engine"], "-interaction=nonstopmode", "-halt-on-error", "-file-line-error",
        f"-output-directory={focus_dir}", f"-jobname={main_tex.stem}",
        *(["-shell-escape"] if settings["shell_escape"] else []),
        f"{review_pretex(settings)}{pretex}\\input{{{main_tex.name}}}",
    ]
    info(f"Focus:   {focus} ({len(keep)} of {len(entries)} files read)")
    began = time.monotonic()
    generated = focus_dir / f"{main_tex.stem}.pdf"
    generated.unlink(missing_ok=True)
    try:
        process = subprocess.run(
            **sandbox.spawn(command, main_tex.parent, build_dir, cpu=FOCUS_TIMEOUT),
            stdin=subprocess.DEVNULL, capture_output=True, text=True, errors="replace", timeout=FOCUS_TIMEOUT,
        )
    except subprocess.TimeoutExpired:  # run() kills the child before raising.
        error(f"{relative.as_posix()}: focus build timed out after {FOCUS_TIMEOUT}s.")
        out_log.write_text(
            f"Focus build of {relative.as_posix()} ({focus}): FAILED\nTimed out after {FOCUS_TIMEOUT}s.\n",
            encoding="utf-8",
        )
        return False
    finally:
        sandbox.scrub(build_dir)
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
        info(paint(f"SUCCESS: {display(out_pdf)}", GREEN) + f" ({count}{seconds:.1f}s, preview)")
    else:
        for item in found[:MAX_SUMMARY_ERRORS]:
            info("  " + error_text(doc_name(main_tex), item))
        info(paint(f"FAILED:  {relative.as_posix()} (focus {focus}); log: {display(out_log)}", RED))
    return ok


def review_paths(main_tex: Path) -> tuple[Path, Path]:
    """out/<name>.review.pdf and out/<name>.review.log: the document with its open suggestions (never published)."""
    pdf = output_path_for(main_tex)
    return pdf.with_name(f"{pdf.stem}.review.pdf"), pdf.with_name(f"{pdf.stem}.review.log")


def review_file_for(main_tex: Path) -> Path:
    """The editor's comments and suggestions (serve.py's history_folder with the default .latex-history)."""
    return CACHE_DIR.parent / ".latex-history" / cache_dir_for(main_tex).name / "review.json"


def build_review_pdf(main_tex: Path, latexmk: str, review_file: Path | None = None, notes: bool = True,
                     live: bool = True) -> dict:
    """
    out/<name>.review.pdf: a copy of the document in the build cache with each open suggestion of review_file inlined
    as \\lpdel{old}\\lpadd{new} (and, with notes, each unresolved comment as \\lpnote), built through build_document
    with review = "show". The document's own files are never written. Returns {"ok", "applied", "notes",
    "skipped": [{"id", "kind", "path", "reason"}], "error"}.
    """
    import review_pdf  # only here: CI never builds a review PDF

    result: dict = {"ok": False, "applied": 0, "notes": 0, "skipped": [], "error": None}
    review_file = review_file or review_file_for(main_tex)
    try:
        data = json.loads(review_file.read_text("utf-8")) if review_file.exists() else {}
        if not isinstance(data, dict):
            raise ValueError("not an object")
        engine = read_settings(main_tex)["engine"]
    except (OSError, ValueError, ConfigError) as exc:
        result["error"] = f"Could not read the review data: {exc}" if not isinstance(exc, ConfigError) else str(exc)
        return result
    work = CACHE_DIR / "_review" / cache_dir_for(main_tex).name  # "_r" never comes out of escape_name
    copy = work / "src"
    shutil.rmtree(copy, ignore_errors=True)
    generated = {path.resolve() for path in (CACHE_DIR, OUT_DIR, CACHE_DIR.parent / ".latex-history")}

    def ignore(folder: str, names: list[str]) -> list[str]:  # a document at the repository root holds these
        return [name for name in names if name == ".git" or (Path(folder) / name).resolve() in generated]

    # Symbolic links stay links: the copy reads nothing the document itself could not.
    shutil.copytree(main_tex.parent, copy, symlinks=True, ignore=ignore)
    root = copy.resolve()

    def inside(rel: str) -> Path | None:
        path = (copy / rel).resolve()
        return path if root in path.parents and path.is_file() else None

    def read(rel: str) -> str | None:
        path = inside(rel)
        try:
            return path.read_text(encoding="utf-8") if path else None
        except (OSError, UnicodeDecodeError):
            return None

    try:
        found = review_pdf.plan(data, read, notes, ascii_only=engine == "pdflatex", main=main_tex.name)
        for rel, text in found["texts"].items():
            inside(rel).write_text(text, encoding="utf-8")
        pdf, log = review_paths(main_tex)
        pdf.unlink(missing_ok=True)  # never the PDF of an older review build next to this build's log
        entry, _ = build_document(copy / main_tex.name, latexmk, live, False, validate=False, review="show",
                                  target=(work / "build", pdf))
    finally:
        shutil.rmtree(copy, ignore_errors=True)
    skipped = found["skipped"]
    summary = [f"Review PDF of {display(main_tex)}: {found['applied']} suggestion(s) and {found['notes']} comment(s) "
               f"inlined, {len(skipped)} skipped.\n"]
    summary += [f"SKIPPED {s['kind']} {s['id']} in {s['path']}: {s['reason']}\n" for s in skipped]
    try:
        log.write_text("".join(summary) + "\n" + log.read_text(encoding="utf-8", errors="replace"), encoding="utf-8")
    except OSError:
        pass
    for line in summary:
        info(line.rstrip())
    first = entry["error"] or next((e["message"] for e in entry["errors"]), None)
    result.update(ok=entry["ok"], applied=found["applied"], notes=found["notes"], skipped=skipped,
                  error=None if entry["ok"] else first or "The review PDF failed; see its log.")
    return result


def build_safely(
    main_tex: Path, latexmk: str, live: bool, force: bool, fig_jobs: int = DEFAULT_JOBS, record: bool = False,
    validate: bool = True,
) -> tuple[dict, str]:
    """
    build_document, but an exception (OSError and the like) fails only this
    document: its report entry carries the message and the run goes on.
    """
    try:
        return build_document(main_tex, latexmk, live, force, fig_jobs, record, validate)
    except Exception as exc:  # noqa: BLE001 - one document must not stop the others.
        message = f"Build crashed: {exc}"
        error(f"{display(main_tex)}: {message}")
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
    expected |= {path for document in documents for path in review_paths(document)}
    expected |= {docx_path_for(document) for document in documents}
    expected |= {arxiv_path_for(document) for document in documents}
    html = {path for document in documents for path in html_paths(document)}

    for path in [*OUT_DIR.rglob("*.pdf"), *OUT_DIR.rglob("*.log"), *OUT_DIR.rglob("*.docx"),
                 *OUT_DIR.rglob("*.arxiv.zip"), *OUT_DIR.rglob("*.html.zip")]:
        if path not in expected | html and not any(folder in path.parents for folder in html):
            info(f"Removing stale: {display(path)}")
            path.unlink()
    for path in [*OUT_DIR.rglob("*.html")] if OUT_DIR.exists() else []:
        if path.is_dir() and path not in html and not any(folder in path.parents for folder in html):
            info(f"Removing stale: {display(path)}")
            shutil.rmtree(path, ignore_errors=True)


def clean() -> None:
    """
    Remove all generated build output and the LaTeX cache.
    """
    targets = [path for path in (OUT_DIR, CACHE_DIR) if path.exists()]

    if not targets:
        info("Nothing to clean.")
        return

    for path in targets:
        info(f"Removing: {display(path)}")

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
        result = subprocess.run(command, cwd=folder, capture_output=True, text=True, encoding="utf-8",
                                errors="replace", timeout=120)
    except (OSError, subprocess.TimeoutExpired) as exc:
        return False, f"pandoc failed: {exc}"

    if result.returncode != 0:
        lines = [line for line in result.stderr.splitlines() if line.strip()]
        return False, next((line for line in reversed(lines) if not line.startswith("[WARNING]")),
                           lines[-1] if lines else f"pandoc exited with {result.returncode}")

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
# HTML export (make4ht / TeX4ht, part of TeX Live: collection-binextra and collection-plaingeneric)
# ---------------------------------------------------------------------------

# tex4ht configuration: MathML for math (no MathJax, nothing loaded from a CDN), PDF figures converted to SVG with
# pdftocairo (poppler-utils) instead of tex4ht's default Ghostscript PNGs (only once graphics is loaded: tex4ht
# defines PdfConvert with it). Pictures (TikZ) go through dvisvgm.
HTML_CFG = r"""\Preamble{xhtml,mathml}
\makeatletter
\ifcsname a:PdfConvert\endcsname
\def\lp@page{\ifx\Gin@page\@empty1\else\Gin@page\fi}\def\lp@pages{-f \lp@page\space -l \lp@page\space}
\Configure{PdfConvert}{svg}{"pdftocairo -svg \lp@pages "\Gin@base.pdf" "\Gin@base-\Gin@page.svg""}
\fi
\makeatother
\begin{document}
\EndPreamble
"""
HTML_ENGINE = {"pdflatex": [], "xelatex": ["-x"], "lualatex": ["-l"]}
HTML_WORK = "lp-html"  # make4ht's output folder inside the work copy (it has no build-directory option before 0.4)


def export_html(main_tex: Path) -> tuple[bool, str]:
    """
    make4ht main.tex -> out/<name>.html/index.html (+ .css, images) and out/<name>.html.zip. TeX4ht runs LaTeX,
    so the whole conversion runs through sandbox.spawn() on a copy of the document in the cache (make4ht writes its
    auxiliary files next to the source), with the last build's .bbl. Returns (ok, message).
    """
    make4ht = shutil.which("make4ht")
    if not make4ht:
        return False, "make4ht was not found on your PATH (TeX Live: tlmgr install make4ht tex4ht; " \
                      "Debian/Ubuntu: texlive-extra-utils)."
    try:
        settings = read_settings(main_tex)
    except ConfigError as exc:
        return False, str(exc)
    folder, archive = html_paths(main_tex)
    work = cache_dir_for(main_tex) / "html"
    shutil.rmtree(work, ignore_errors=True)
    shutil.copytree(main_tex.parent, work, symlinks=True, ignore=shutil.ignore_patterns(".*"))
    for bbl in cache_dir_for(main_tex).glob("*.bbl"):  # citations without running BibTeX or biber again
        shutil.copy2(bbl, work / bbl.name)
    (work / "lp-html.cfg").write_text(HTML_CFG, encoding="utf-8")
    command = [make4ht, *HTML_ENGINE[settings["engine"]], *(["-s"] if settings["shell_escape"] else []),
               "-c", "lp-html.cfg", "-d", HTML_WORK, "-f", "html5", main_tex.name]
    timeout = settings["timeout"]
    try:
        result = subprocess.run(**sandbox.spawn(command, work, work, {**os.environ, **LATEX_LOG_ENV}, cpu=timeout),
                                stdin=subprocess.DEVNULL, capture_output=True, text=True, encoding="utf-8",
                                errors="replace", timeout=timeout)
    except (OSError, subprocess.TimeoutExpired) as exc:
        return False, f"make4ht failed: {exc}"
    finally:
        sandbox.scrub(work)
    page = work / HTML_WORK / f"{main_tex.stem}.html"
    if not page.is_file():
        lines = [line for line in (result.stdout + result.stderr).splitlines() if line.strip()]
        errors = [line for line in lines if "error" in line.lower()]
        return False, (errors or lines or [f"make4ht exited with {result.returncode}"])[-1].strip()
    shutil.rmtree(folder, ignore_errors=True)
    folder.mkdir(parents=True)
    for path in sorted((work / HTML_WORK).rglob("*")):
        if path.is_file() and not path.is_symlink():  # scrub() removed other kinds; links stay behind too
            target = folder / path.relative_to(work / HTML_WORK)
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(path, target)
    old = f"{main_tex.stem}.html"
    for path in folder.rglob("*.html"):  # main.html -> index.html, and the links to it
        text = path.read_text(encoding="utf-8", errors="replace")
        path.write_text(text.replace(f"'{old}", "'index.html").replace(f'"{old}', '"index.html'), encoding="utf-8")
    (folder / old).replace(folder / "index.html")
    archive.unlink(missing_ok=True)
    with zipfile.ZipFile(archive, "w", zipfile.ZIP_DEFLATED) as bundle:
        for path in sorted(folder.rglob("*")):
            if path.is_file():
                bundle.write(path, path.relative_to(folder.parent).as_posix())
    missing = sorted({name for page in folder.rglob("*.html")
                      for name in re.findall(r"<img[^>]*\ssrc=['\"]([^'\"]+)", page.read_text(encoding="utf-8"))
                      if "://" not in name and not (folder / name).is_file()})
    note = f" (images not converted: {', '.join(missing[:5])}; PDF figures need pdftocairo, pictures dvisvgm)" \
        if missing else ""
    return True, display(folder / "index.html") + note


def wants_html(main_tex: Path) -> bool:
    """build.toml html = true: CI exports the HTML and publish_release.py uploads its .html.zip."""
    try:
        return read_settings(main_tex)["html"]
    except ConfigError:
        return False


def export_html_all(documents: list[Path]) -> int:
    """The --html step (and build.toml html = true): export each document, one line each; 1 if any failed."""
    failed = 0
    for document in documents:
        ok, message = export_html(document)
        (info if ok else error)(f"HTML {doc_name(document)}: {message}")
        failed += not ok
    return 1 if failed else 0


# ---------------------------------------------------------------------------
# Formatting (optional: needs tex-fmt, https://github.com/WGUNDERWOOD/tex-fmt)
# ---------------------------------------------------------------------------

TEXFMT_MISSING = "tex-fmt was not found on your PATH (https://github.com/WGUNDERWOOD/tex-fmt)."
FORMAT_TIMEOUT = 60


def _texfmt_problem(result: subprocess.CompletedProcess) -> str:
    lines = [line for line in (result.stderr or "").splitlines() if line.strip()]
    return lines[-1] if lines else f"tex-fmt exited with {result.returncode}"


def format_sources(folder: Path, check: bool = False) -> tuple[list[str], list[str]]:
    """
    tex-fmt on every .tex file in a document directory (hidden ones skipped), run there so it reads that
    directory's tex-fmt.toml. Returns (files it changed, or with check would change; problems). tex-fmt is not
    a TeX engine (it only rewrites text), so it runs outside the sandbox. Raises FileNotFoundError without it.
    """
    texfmt = shutil.which("tex-fmt")
    if not texfmt:
        raise FileNotFoundError(TEXFMT_MISSING)
    changed: list[str] = []
    problems: list[str] = []
    for path in sorted(folder.rglob("*.tex")):
        rel = path.relative_to(folder)
        if any(part.startswith(".") for part in rel.parts) or not path.is_file():
            continue
        before = path.read_bytes()
        try:
            result = subprocess.run([texfmt, *(["--check"] if check else []), rel.as_posix()], cwd=folder,
                                    capture_output=True, text=True, encoding="utf-8", errors="replace",
                                    timeout=FORMAT_TIMEOUT)
        except (OSError, subprocess.TimeoutExpired) as exc:
            problems.append(f"{rel.as_posix()}: {exc}")
            continue
        if check and result.returncode == 1:  # --check: 1 means "Incorrect formatting"
            changed.append(rel.as_posix())
        elif result.returncode != 0:
            problems.append(f"{rel.as_posix()}: {_texfmt_problem(result)}")
        elif not check and path.read_bytes() != before:
            changed.append(rel.as_posix())
    return changed, problems


def format_text(folder: Path, text: str) -> str:
    """tex-fmt --stdin on one file's text, with the tex-fmt.toml of `folder`. Raises FileNotFoundError, ValueError."""
    texfmt = shutil.which("tex-fmt")
    if not texfmt:
        raise FileNotFoundError(TEXFMT_MISSING)
    try:
        result = subprocess.run([texfmt, "--stdin"], cwd=folder, input=text, capture_output=True, text=True,
                                encoding="utf-8", errors="replace", timeout=20)
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise ValueError(f"tex-fmt failed: {exc}")
    if result.returncode != 0:
        raise ValueError(f"tex-fmt failed: {_texfmt_problem(result)}")
    return result.stdout


def format_all(documents: list[Path]) -> int:
    """The --format step: rewrite each document's .tex files in place; 1 if tex-fmt failed (a missing tex-fmt is only
    a message, like a missing chktex in the lint)."""
    failed = 0
    for document in documents:
        try:
            changed, problems = format_sources(document.parent)
        except FileNotFoundError as exc:
            info(str(exc))
            return 0
        info(f"Format {doc_name(document)}: " + (", ".join(changed) if changed else "nothing to change"))
        for problem in problems:
            error(f"  {problem}")
        failed += bool(problems)
    return 1 if failed else 0


# ---------------------------------------------------------------------------
# arXiv export (reads the last build's cache; runs no TeX)
# ---------------------------------------------------------------------------

def export_arxiv(main_tex: Path) -> tuple[list[dict] | None, list[str]]:
    """
    out/<name>.arxiv.zip from the last build (arxiv.py), its preflight appended to out/<name>.log.
    Returns (findings, lines to print); findings is None when the archive could not be written.
    """
    import arxiv  # only here: the export is never part of a build or of CI

    try:
        settings = read_settings(main_tex)
        if not (cache_dir_for(main_tex) / f"{main_tex.stem}.fls").is_file():
            raise OSError("no recorded build (.fls); build the document first")
        target = arxiv_path_for(main_tex)
        findings = arxiv.export(main_tex, settings, cache_dir_for(main_tex), target, recorded_inputs(main_tex))
    except (ConfigError, OSError, ValueError) as exc:
        return None, [f"ERROR: arXiv export failed: {exc}"]
    lines = [f"arXiv archive: {display(target)}"]
    for found in findings:
        where = f"{found['file']}: " if found["file"] else ""
        lines += [f"{found['level'].upper()}: {where}{found['message']}", f"  Hint: {found['hint']}"]
    if not findings:
        lines.append("Preflight: no problems found.")
    log, heading = log_path_for(main_tex), "\n===== arXiv export =====\n"
    try:
        kept = log.read_text(encoding="utf-8", errors="replace").split(heading)[0] if log.exists() else ""
        log.write_text(kept + heading + "".join(f"{line}\n" for line in lines), encoding="utf-8")
    except OSError:
        pass  # The archive is written; a log that cannot be is not worth failing for.
    return findings, lines


def export_arxiv_all(documents: list[Path]) -> int:
    """The --arxiv step: export each document and print its preflight; 1 if any failed or has an error."""
    failed = 0
    for document in documents:
        findings, lines = export_arxiv(document)
        info(f"arXiv {doc_name(document)}:")
        for line in lines:
            info(f"  {line}")
        failed += findings is None or any(found["level"] == "error" for found in findings)
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


def template_files(title: str, template: str = "article") -> dict[str, str]:
    """{relative path: text} of a template, with the title (LaTeX-escaped) filled into main.tex."""
    files = TEMPLATES.get(template) or {"main.tex": NEW_TEMPLATE}
    escaped = "".join(LATEX_SPECIAL.get(char, char) for char in title)
    return {rel: text % {"title": escaped} if rel == "main.tex" else text for rel, text in files.items()}


def new_document(name: str, template: str = "article") -> int:
    """
    Create files/<name>/main.tex (plus companion files for some templates). Never overwrites.
    """
    path = Path(name)

    if not name.strip() or path.is_absolute() or ".." in path.parts:
        error(f"Invalid document name: {name!r}")
        return 1

    target = SOURCE_DIR / path / "main.tex"
    files = template_files(name, template)

    for rel in files:
        if (target.parent / rel).exists():
            error(f"Already exists: {display(target.parent / rel)}")
            return 1

    for rel, text in files.items():
        out = target.parent / rel
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text(text, encoding="utf-8")

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
        "--sandbox",
        action="store_true",
        help="Run LaTeX under bubblewrap (Linux): no network, writes only to the build directory "
             "(sets LATEX_SANDBOX=bwrap; README, 'Sandboxed builds').",
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
        help="Template for --new (default article; report adds chapters/, refs.bib, figures/, build.toml).",
    )

    parser.add_argument(
        "--docx",
        action="store_true",
        help="After building, also export each selected document to out/<name>.docx with pandoc (must be installed).",
    )

    parser.add_argument(
        "--html",
        action="store_true",
        help="After building, also export each selected document to out/<name>.html/index.html (and .html.zip) with "
             "make4ht/TeX4ht: MathML math, figures as SVG; sandboxed like a build. Published only with html = true "
             "in build.toml.",
    )

    parser.add_argument(
        "--format",
        action="store_true",
        help="Rewrite the .tex files of each selected document with tex-fmt (must be installed; reads a "
             "tex-fmt.toml in the document directory), then exit without building.",
    )

    parser.add_argument(
        "--arxiv",
        action="store_true",
        help="After building, write out/<name>.arxiv.zip for arXiv (the files the build read, the .bbl, "
             "comments stripped) and print preflight checks into the summary and the log. Never published.",
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
        help="Build the documents under DIR (relative to the repository root, e.g. bench, or absolute, also "
             "outside the repository) instead of files/. Outputs go to out/ as usual, named relative to DIR.",
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

    parser.add_argument(
        "--review",
        choices=["show", "clean"],
        help="Review markup (\\lpadd, \\lpdel, \\lpnote ...) for every selected document, overriding build.toml: "
             "'show' marks changes and notes, 'clean' prints the text as if every change were accepted.",
    )

    parser.add_argument(
        "--review-pdf",
        action="store_true",
        help="Build out/<name>.review.pdf for one document: its open editor suggestions inlined as marked changes "
             "and its unresolved comments as notes (the document's files are not changed).",
    )

    parser.add_argument(
        "--no-comments",
        action="store_true",
        help="With --review-pdf: leave the comments out.",
    )

    args = parser.parse_args()

    if args.no_comments and not args.review_pdf:
        parser.error("--no-comments needs --review-pdf")

    if args.review_pdf and (args.watch or args.focus or args.docx):
        parser.error("--review-pdf cannot be combined with --watch, --focus or --docx")

    if args.focus == "auto" and not args.watch:
        parser.error("--focus auto needs --watch")

    if args.jobs < 1:
        parser.error("--jobs must be at least 1")

    if args.timeout is not None and not TIMEOUT_RANGE[0] <= args.timeout <= TIMEOUT_RANGE[1]:
        parser.error(f"--timeout must be {TIMEOUT_RANGE[0]} to {TIMEOUT_RANGE[1]}")

    if args.open and not args.watch:
        parser.error("--open needs --watch")

    if args.template and args.new is None:
        parser.error("--template needs --new")

    if args.docx and (args.watch or args.focus):
        parser.error("--docx cannot be combined with --watch or --focus")
    if args.html and (args.watch or args.focus or args.review_pdf or args.format):
        parser.error("--html cannot be combined with --watch, --focus, --review-pdf or --format")

    if args.format and (args.watch or args.focus or args.docx):
        parser.error("--format cannot be combined with --watch, --focus or --docx")
    if args.arxiv and (args.watch or args.focus):
        parser.error("--arxiv cannot be combined with --watch or --focus")

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
        if entry.get("a11y"):
            details.append(f"{len(entry['a11y'])} accessibility finding(s)")

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
    if args.sandbox:
        os.environ[sandbox.VARIABLE] = "bwrap"
    if args.timeout is not None:
        global DEFAULT_TIMEOUT
        DEFAULT_TIMEOUT = args.timeout
    if args.review:
        global REVIEW_MODE
        REVIEW_MODE = args.review

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
        return new_document(args.new, args.template or "article")

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
        info(f"No main.tex files found under {display(SOURCE_DIR)}/.")
        return 0

    documents = select_documents(documents, args.docs)

    if args.changed_since and ROOT_DIR not in SOURCE_DIR.parents and SOURCE_DIR != ROOT_DIR:
        info(f"--changed-since ignored: {display(SOURCE_DIR)} is outside the repository, so git cannot diff it.")
    elif args.changed_since:
        documents = filter_changed(documents, args.changed_since)

        if not documents:
            info(f"No documents changed since {args.changed_since}.")
            return 0

    if args.list:
        info("Discovered LaTeX documents:")
        info()

        for document in documents:
            output = output_path_for(document)
            info(f"  {display(document)}")
            status = "" if is_stale(document) else "  (up to date)"
            info(f"    -> {display(output)}{status}")

        return 0

    if args.format:
        return format_all(documents)

    if args.docx and not shutil.which("pandoc"):
        error("--docx needs pandoc, which was not found on your PATH (https://pandoc.org/installing.html).")
        return 2

    latexmk = check_latex()

    if args.focus and not args.watch:
        if len(documents) != 1:
            error("--focus needs exactly one document; name it, e.g. build.py sample-report --focus Chapters/ch1")
            return 2
        return 0 if build_focus(documents[0], latexmk, args.focus) else 1

    if args.review_pdf:
        if len(documents) != 1:
            error("--review-pdf needs exactly one document; name it, e.g. build.py sample-report --review-pdf")
            return 2
        result = build_review_pdf(documents[0], latexmk, notes=not args.no_comments)
        if result["error"]:
            error(result["error"])
        return 0 if result["ok"] else 1

    if args.watch:
        return watch(latexmk, args.docs, args.open, args.focus)

    exported = documents

    def extras(failed: bool, results: list[dict]) -> int:
        """
        --docx, --html and --arxiv after the build; --arxiv skips documents whose build failed in this run. Documents
        with html = true in build.toml that built in this run get their HTML too (so CI can publish it).
        """
        if args.docx:  # Also after a failed build: pandoc reads the sources, not the PDF.
            failed = bool(export_docx_all(exported)) or failed
        built = {entry["name"] for entry in results if entry["ok"]}
        html = [doc for doc in exported if args.html or doc_name(doc) in built and wants_html(doc)]
        if html:
            failed = bool(export_html_all(html)) or failed
        if args.arxiv:
            broken = {entry["name"] for entry in results if not entry["ok"]}
            failed = bool(export_arxiv_all([doc for doc in exported if doc_name(doc) not in broken])) or failed
        return 1 if failed else 0

    if not args.force:
        # --arxiv needs the .fls of a finished build, which a cleaned cache lacks even when the PDF is current.
        documents = [document for document in documents if is_stale(document)
                     or args.arxiv and not (cache_dir_for(document) / f"{document.stem}.fls").exists()]

        if not documents:
            info("Everything is up to date.")
            return extras(False, [])

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

    return extras(any(not entry["ok"] for entry in results), results)


if __name__ == "__main__":
    sys.exit(main())
