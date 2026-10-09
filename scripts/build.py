#!/usr/bin/env python3

from __future__ import annotations

import argparse
import os
import shutil
import subprocess
import sys
import time
from pathlib import Path


# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------

SCRIPT_DIR = Path(__file__).resolve().parent
ROOT_DIR = SCRIPT_DIR.parent
SOURCE_DIR = ROOT_DIR / "files"
OUT_DIR = ROOT_DIR / "out"
# latexmk's working files (.aux, .bbl, ...) persist here between builds, so an
# edit reruns only the passes it needs instead of a cold multi-pass build.
CACHE_DIR = ROOT_DIR / ".latex-cache"

# Changes to these paths (relative to ROOT_DIR) rebuild every document.
GLOBAL_INPUTS = (
    "scripts/",
    ".github/workflows/build-pdf.yml",
)

LATEXMK_ARGS = [
    "-pdf",
    "-interaction=nonstopmode",
    "-halt-on-error",
    "-file-line-error",
]


# ---------------------------------------------------------------------------
# Output helpers
# ---------------------------------------------------------------------------

def info(message: str = "") -> None:
    print(message)


def error(message: str) -> None:
    print(f"ERROR: {message}", file=sys.stderr)


def separator() -> None:
    print("=" * 72)


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
# Building
# ---------------------------------------------------------------------------

def build_document(main_tex: Path, latexmk: str) -> bool:
    """
    Build one LaTeX document.

    Compilation happens in .latex-cache/, which persists between builds so
    latexmk reruns only the passes an edit needs. LaTeX auxiliary files never
    pollute the source directory or out/. Only the final PDF and
    <name>.log are written to out/. The log holds the result, the latexmk
    console output, and LaTeX's and BibTeX's own logs: every info line,
    warning and error.
    """
    relative = main_tex.relative_to(ROOT_DIR)
    output_pdf = output_path_for(main_tex)
    log_path = log_path_for(main_tex)

    separator()
    info(f"Building: {relative}")
    info(f"Output:  {output_pdf.relative_to(ROOT_DIR)}")
    separator()

    output_pdf.parent.mkdir(parents=True, exist_ok=True)

    # Stamp the PDF with the build start time, so edits made while LaTeX is
    # running still count as newer than the PDF.
    started = time.time()

    build_dir = CACHE_DIR / main_tex.parent.relative_to(SOURCE_DIR)
    cached = build_dir.exists()
    build_dir.mkdir(parents=True, exist_ok=True)

    command = [
        latexmk,
        *LATEXMK_ARGS,
        f"-outdir={build_dir}",
        main_tex.name,
    ]
    generated_pdf = build_dir / f"{main_tex.stem}.pdf"

    while True:
        info("Running:")
        info("  " + " ".join(f'"{arg}"' if " " in arg else arg for arg in command))
        info()

        console = []
        errors = []

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
            break

        for line in process.stdout:
            sys.stdout.write(line)
            console.append(line)

        if process.wait() == 0 or not cached:
            break

        # Stale working files can break a build that would pass from scratch.
        info()
        info("Build failed with cached files; retrying from a clean cache.")
        info()
        shutil.rmtree(build_dir, ignore_errors=True)
        build_dir.mkdir(parents=True)
        cached = False

    if not errors:
        if process.returncode != 0:
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
        error(message)

    sections = [
        f"Build of {relative.as_posix()}: {'FAILED' if errors else 'SUCCESS'}\n",
        *(f"ERROR: {message}\n" for message in errors),
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

    info()
    info(f"Log:     {log_path.relative_to(ROOT_DIR)}")

    if errors:
        return False

    info(f"SUCCESS: {output_pdf.relative_to(ROOT_DIR)}")
    return True


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

                if build_document(document, latexmk):
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
# CLI
# ---------------------------------------------------------------------------

def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Build all LaTeX documents containing main.tex."
    )

    parser.add_argument(
        "--clean",
        action="store_true",
        help="Remove out/ and the .latex-cache/ directory, then exit.",
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
        "--force",
        action="store_true",
        help="Rebuild documents even if their PDF is up to date.",
    )

    parser.add_argument(
        "--watch",
        action="store_true",
        help="Keep running and rebuild documents whenever their files change.",
    )

    return parser.parse_args()


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

    successes = 0
    failures = 0

    for document in documents:
        if build_document(document, latexmk):
            successes += 1
        else:
            failures += 1

        info()

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