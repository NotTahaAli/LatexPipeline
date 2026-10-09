#!/usr/bin/env python3

from __future__ import annotations

import argparse
import os
import re
import shutil
import subprocess
import sys
import tempfile
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------

SCRIPT_DIR = Path(__file__).resolve().parent
ROOT_DIR = SCRIPT_DIR.parent
SOURCE_DIR = ROOT_DIR / "files"
OUT_DIR = ROOT_DIR / "out"

# ponytail: capped at 8; every job is a full TeX run, so more would thrash the disk.
DEFAULT_JOBS = min(os.cpu_count() or 1, 8)

# Changes to these paths (relative to ROOT_DIR) rebuild every document.
GLOBAL_INPUTS = (
    "scripts/",
    ".github/workflows/build-pdf.yml",
)

LATEXMK_ARGS = [
    "-interaction=nonstopmode",
    "-halt-on-error",
    "-file-line-error",
]

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

def info(message: str = "") -> None:
    print(message)


def error(message: str) -> None:
    print(f"ERROR: {message}", file=sys.stderr)


RULE = "=" * 72


def separator() -> None:
    print(RULE)


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

def find_documents() -> list[Path]:
    """
    Find every main.tex under SOURCE_DIR.

    Returns absolute paths.
    """
    return sorted(path for path in SOURCE_DIR.rglob("main.tex") if path.is_file())


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
    Keep documents whose directory contains a changed file.

    Everything inside a document's directory (.tex, .bib, .cls, figures, ...)
    counts as its input. Inputs outside that directory are not tracked.
    """
    changed = changed_files(ref)

    if changed is None:
        info(f"Could not diff against '{ref}'; building everything.")
        return documents

    if any(name.startswith(GLOBAL_INPUTS) for name in changed):
        return documents

    changed_paths = [ROOT_DIR / name for name in changed]

    return [
        document for document in documents
        if any(document.parent in path.parents for path in changed_paths)
    ]


def newest_input(main_tex: Path) -> float:
    """
    Newest modification time among a document's inputs: every file in its
    directory tree (dotfiles such as .DS_Store excluded) and this script.
    """
    newest = Path(__file__).stat().st_mtime

    for path in main_tex.parent.rglob("*"):
        if path.name.startswith("."):
            continue
        try:
            if path.is_file():
                newest = max(newest, path.stat().st_mtime)
        except OSError:
            pass  # Deleted mid-scan.

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
    relative = main_tex.relative_to(SOURCE_DIR).parent

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


# ---------------------------------------------------------------------------
# Per-document settings
# ---------------------------------------------------------------------------

# "% !TEX program = xelatex" (also "% !TeX TS-program = ..."), in the first lines.
ENGINE_MAGIC = re.compile(r"^\s*%\s*!\s*TEX\s+(?:TS-)?PROGRAM\s*=\s*(\S+)", re.IGNORECASE)
CONFIG_KEYS = {"engine", "shell_escape", "latexmk_args"}


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
    settings = {"engine": DEFAULT_ENGINE, "shell_escape": False, "latexmk_args": []}

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

    if not isinstance(settings["shell_escape"], bool):
        raise ConfigError(f"{display(main_tex.parent / 'build.toml')}: shell_escape must be true or false")

    args = settings["latexmk_args"]
    if not (isinstance(args, list) and all(isinstance(arg, str) for arg in args)):
        raise ConfigError(f"{display(main_tex.parent / 'build.toml')}: latexmk_args must be a list of strings")

    return settings


# ---------------------------------------------------------------------------
# Building
# ---------------------------------------------------------------------------

def build_document(main_tex: Path, latexmk: str, live: bool = True) -> tuple[bool, str]:
    """
    Build one LaTeX document. Returns (success, console text).

    Compilation happens inside a temporary directory, so LaTeX auxiliary
    files never pollute the source directory or out/. Only the final PDF and
    <name>.log are written to out/. The log holds the result, the latexmk
    console output, and LaTeX's and BibTeX's own logs: every info line,
    warning and error.

    With live=True the output is streamed to the terminal. With live=False
    nothing is printed except a "Started" line; the caller prints the
    returned text once the document finishes, so parallel builds don't mix.
    """
    relative = main_tex.relative_to(ROOT_DIR)
    output_pdf = output_path_for(main_tex)
    log_path = log_path_for(main_tex)

    shown: list[str] = []

    def emit(text: str) -> None:
        shown.append(text)
        if live:
            sys.stdout.write(text)

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

    try:
        settings = read_settings(main_tex)
    except ConfigError as exc:
        settings = None
        errors.append(str(exc))

    # Stamp the PDF with the build start time, so edits made while LaTeX is
    # running still count as newer than the PDF.
    started = time.time()

    with tempfile.TemporaryDirectory(prefix="latex-build-") as temp_dir:
        temp_dir = Path(temp_dir)
        generated_pdf = temp_dir / f"{main_tex.stem}.pdf"

        if settings is not None:
            command = [
                latexmk,
                *LATEXMK_ARGS,
                ENGINES[settings["engine"]],
                *(["-shell-escape"] if settings["shell_escape"] else []),
                *settings["latexmk_args"],
                f"-outdir={temp_dir}",
                main_tex.name,
            ]

            say(f"Engine:  {settings['engine']}")
            say()
            say("Running:")
            say("  " + " ".join(f'"{arg}"' if " " in arg else arg for arg in command))
            say()

            # Stream output to the terminal while keeping a copy for the log.
            try:
                process = subprocess.Popen(
                    command,
                    cwd=main_tex.parent,
                    stdin=subprocess.DEVNULL,
                    stdout=subprocess.PIPE,
                    stderr=subprocess.STDOUT,
                    text=True,
                    errors="replace",
                )
            except OSError as exc:
                errors.append(f"Could not execute latexmk: {exc}")
            else:
                for line in process.stdout:
                    emit(line)
                    console.append(line)

                if process.wait() != 0:
                    errors.append(f"LaTeX compilation failed: {relative}")
                elif not generated_pdf.exists():
                    errors.append("LaTeX reported success, but no PDF was produced.")
                else:
                    try:
                        shutil.copy(generated_pdf, output_pdf)
                        os.utime(output_pdf, (started, started))
                    except OSError as exc:
                        errors.append(f"Could not copy generated PDF: {exc}")

        for message in errors:
            say(f"ERROR: {message}")

        sections = [
            f"Build of {relative.as_posix()}: {'FAILED' if errors else 'SUCCESS'}\n",
            *(f"ERROR: {message}\n" for message in errors),
            "\n===== latexmk output =====\n",
            *console,
        ]

        for suffix, title in ((".log", "LaTeX log"), (".blg", "BibTeX log")):
            path = temp_dir / f"{main_tex.stem}{suffix}"
            if path.exists():
                sections += [
                    f"\n===== {title} ({path.name}) =====\n",
                    path.read_text(encoding="utf-8", errors="replace"),
                ]

    log_path.write_text("".join(sections), encoding="utf-8")

    say()
    say(f"Log:     {log_path.relative_to(ROOT_DIR)}")

    if errors:
        return False, "".join(shown)

    say(f"SUCCESS: {output_pdf.relative_to(ROOT_DIR)}")
    return True, "".join(shown)


def watch(latexmk: str, interval: float = 1.0) -> int:
    """
    Poll for changes and rebuild stale documents until interrupted.
    """
    # ponytail: stdlib polling, not OS file events; fine for a handful of documents.
    info("Watching for changes. Press Ctrl+C to stop.")
    info()

    # Input timestamp of the last failed build, so a broken document is not
    # retried until something changes.
    failed: dict[Path, float] = {}

    try:
        while True:
            documents = find_documents()
            prune(documents)

            for document in documents:
                if not is_stale(document):
                    continue

                newest = newest_input(document)

                if failed.get(document) == newest:
                    continue

                ok, _ = build_document(document, latexmk)
                if ok:
                    failed.pop(document, None)
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
    Delete PDFs and logs in out/ whose main.tex no longer exists.
    """
    expected = {output_path_for(document) for document in documents}
    expected |= {log_path_for(document) for document in documents}

    for path in [*OUT_DIR.rglob("*.pdf"), *OUT_DIR.rglob("*.log")]:
        if path not in expected:
            info(f"Removing stale: {path.relative_to(ROOT_DIR)}")
            path.unlink()


def clean() -> None:
    """
    Remove all generated build output.
    """
    if not OUT_DIR.exists():
        info("Nothing to clean.")
        return

    info(f"Removing: {OUT_DIR.relative_to(ROOT_DIR)}")

    try:
        shutil.rmtree(OUT_DIR)
    except OSError as exc:
        error(f"Could not remove output directory: {exc}")
        raise SystemExit(1)

    info("Clean complete.")


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Build all LaTeX documents containing main.tex."
    )

    parser.add_argument(
        "--clean",
        action="store_true",
        help="Remove the generated out/ directory and exit.",
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

    args = parser.parse_args()

    if args.jobs < 1:
        parser.error("--jobs must be at least 1")

    return args


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main() -> int:
    args = parse_args()

    if args.clean:
        clean()
        return 0

    documents = find_documents()

    if not args.list:
        prune(documents)

    if not documents:
        info(f"No main.tex files found under {SOURCE_DIR.relative_to(ROOT_DIR)}/.")
        return 0

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

    latexmk = check_latex()

    if args.watch:
        return watch(latexmk)

    if not args.force:
        documents = [document for document in documents if is_stale(document)]

        if not documents:
            info("Everything is up to date.")
            return 0

    separator()
    info(f"Found {len(documents)} document(s).")
    info()

    # One document runs live; several run in a pool, printed as each finishes.
    jobs = min(args.jobs, len(documents))
    results: list[bool] = []

    if jobs == 1:
        for document in documents:
            results.append(build_document(document, latexmk)[0])
            info()
    else:
        with ThreadPoolExecutor(max_workers=jobs) as pool:
            futures = [
                pool.submit(build_document, document, latexmk, False)
                for document in documents
            ]

            for future in as_completed(futures):
                ok, text = future.result()
                sys.stdout.write(text)
                results.append(ok)
                info()

    successes = sum(results)
    failures = len(results) - successes

    separator()
    info("Build Summary")
    separator()
    info(f"Successful: {successes}")
    info(f"Failed:     {failures}")
    info(f"Total:      {len(documents)}")
    separator()

    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())