"""
Build accelerators for large documents (imported by build.py, standard library only).

externalize: TikZ/pgfplots figures are compiled once, in parallel, and cached by
content hash, so a build re-typesets only the text. The document is not edited:
a small file is injected with latexmk's -usepretex. See README, "Large documents".
"""

from __future__ import annotations

import hashlib
import os
import re
import shutil
import subprocess
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Callable

# Runs before \begin{document}'s own hooks, which is the last moment tikz's
# external library may still be switched on. "list and make" needs no shell escape:
# the main run lists the figures, and the Python side compiles them (no make needed).
INJECT = r"""\makeatletter
\AddToHook{begindocument/before}{\ifcsname ver@tikz.sty\endcsname
\usetikzlibrary{external}\tikzexternalize[prefix=tikz/,mode=list and make]\fi}
% A figure job reads the document only up to its figure: once the figure is
% shipped, \input and \include (the chapters still to come) do nothing.
\def\pgfx@skipinput{\@ifnextchar\bgroup\@gobble\@gobble}
\def\pgfx@skip{\global\let\input\pgfx@skipinput\global\let\include\@gobble}
\AddToHook{begindocument/end}{\ifcsname pgf@externalend\endcsname\ifpgf@external@grabshipout
\ifcsname pgfx@noskip\endcsname\else
\let\pgfx@end\pgf@externalend\def\pgf@externalend{\pgfx@end\pgfx@skip}\fi\fi\fi}
\makeatother
"""

# Draft flag per engine for the figure-listing run (no PDF written).
DRAFT = {"pdflatex": "-draftmode", "lualatex": "--draftmode", "xelatex": "-no-pdf"}

# Files that change what a figure looks like without changing its source text.
FIGURE_DEPS = {".cls", ".sty", ".def", ".cfg", ".clo", ".pgf", ".tikz", ".csv", ".dat", ".tsv", ".table"}
NOSKIP = r"\expandafter\def\csname pgfx@noskip\endcsname{}"
TIKZ_WORDS = re.compile(r"tikz|pgfplots")


def uses_tikz(doc_dir: Path) -> bool:
    """True if any .tex/.cls/.sty file of the document mentions tikz or pgfplots."""
    for path in doc_dir.rglob("*"):
        if path.suffix in {".tex", ".cls", ".sty"} and not path.name.startswith("."):
            try:
                if TIKZ_WORDS.search(path.read_text(encoding="utf-8", errors="replace")):
                    return True
            except OSError:
                pass
    return False


def environment_hash(main_tex: Path, engine: str, shell_escape: bool) -> str:
    """
    Everything besides a figure's own source that can change its PDF: the
    preamble, local classes/packages/data files, the engine.
    """
    digest = hashlib.sha1(f"{engine}{shell_escape}".encode())
    text = main_tex.read_text(encoding="utf-8", errors="replace")
    digest.update(text.split(r"\begin{document}")[0].encode())

    for path in sorted(main_tex.parent.rglob("*")):
        if path.suffix in FIGURE_DEPS and not path.name.startswith(".") and path.is_file():
            digest.update(path.relative_to(main_tex.parent).as_posix().encode())
            digest.update(path.read_bytes())
    return digest.hexdigest()


def tex_path(target: Path, start: Path) -> str:
    """
    target as TeX can read it from cwd=start: relative with forward slashes, so a
    repository path with spaces or backslashes never reaches the command line.
    """
    try:
        return Path(os.path.relpath(target, start)).as_posix()
    except ValueError:  # Windows: another drive.
        return target.as_posix()


class Figures:
    """The cached figures of one document."""

    def __init__(self, main_tex: Path, build_dir: Path, engine: str, shell_escape: bool, jobs: int):
        self.main_tex = main_tex
        self.build_dir = build_dir
        self.engine = engine
        self.shell_escape = shell_escape
        self.jobs = jobs
        self.stem = main_tex.stem
        self.cache = build_dir / "figcache"
        self.environment = environment_hash(main_tex, engine, shell_escape)
        inject = build_dir / "_inject.tex"
        inject.write_text(INJECT, encoding="utf-8")
        (build_dir / "tikz").mkdir(exist_ok=True)
        self.pretex = rf"\input{{{tex_path(inject, main_tex.parent)}}}"

    def file(self, name: str, suffix: str) -> Path:
        # Not with_suffix(): a document called "v1.2" would lose its tail.
        return self.build_dir / f"{name}{suffix}"

    def touched(self, since: float) -> bool:
        """
        True if a file edited after `since` can have changed a figure: a
        .tex file mentioning tikz/pgfplots/axes, or a class/package/data file.
        """
        for path in self.main_tex.parent.rglob("*"):
            if path.name.startswith(".") or not path.is_file() or path.stat().st_mtime <= since:
                continue
            if path.suffix in FIGURE_DEPS:
                return True
            if path.suffix == ".tex" and re.search(r"tikz|pgf|axis", path.read_text(errors="replace")):
                return True
        return False

    def names(self) -> list[str]:
        figlist = self.build_dir / f"{self.stem}.figlist"
        if not figlist.exists():
            return []
        return [line.strip() for line in figlist.read_text(encoding="utf-8").splitlines() if line.strip()]

    def key(self, name: str) -> str | None:
        md5 = self.file(name, ".md5")
        if not md5.exists():
            return None
        return hashlib.sha1((self.environment + md5.read_text(encoding="utf-8")).encode()).hexdigest()

    def discover_command(self) -> list[str]:
        return [
            self.engine, "-interaction=batchmode", DRAFT[self.engine], "-file-line-error",
            f"-output-directory={self.build_dir}", f"-jobname={self.stem}",
            *(["-shell-escape"] if self.shell_escape else []),
            f"{self.pretex}\\input{{{self.main_tex.name}}}",
        ]

    def figure_command(self, name: str, skip: bool = True) -> list[str]:
        return [
            self.engine, "-interaction=batchmode", "-halt-on-error", "-file-line-error",
            f"-output-directory={self.build_dir}", f"-jobname={name}",
            *(["-shell-escape"] if self.shell_escape else []),
            self.pretex + ("" if skip else NOSKIP)
            + f"\\def\\tikzexternalrealjob{{{self.stem}}}\\input{{{self.main_tex.name}}}",
        ]

    def compile(self, name: str) -> str | None:
        """Compile one figure. Returns an error text, or None on success."""
        aux = self.build_dir / f"{self.stem}.aux"
        (self.build_dir / name).parent.mkdir(parents=True, exist_ok=True)
        pdf = self.file(name, ".pdf")

        # Skipping the rest of the document is an optimisation; if it breaks
        # something that is read later, run the figure once more without it.
        for skip in (True, False):
            if aux.exists():  # Lets \ref inside the figure resolve.
                shutil.copy(aux, self.file(name, ".aux"))
            pdf.unlink(missing_ok=True)
            try:
                subprocess.run(
                    self.figure_command(name, skip), cwd=self.main_tex.parent, stdin=subprocess.DEVNULL,
                    stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=1800,
                )
            except (OSError, subprocess.TimeoutExpired) as exc:
                return f"{name}: {exc}"
            if pdf.exists():
                return None

        log = self.file(name, ".log")
        lines = log.read_text(encoding="utf-8", errors="replace").splitlines() if log.exists() else []
        errors = [line for line in lines if re.match(r"^(\S.*:\d+:|!) ", line)]
        return f"Figure {name} failed to compile:\n" + "\n".join(errors[:5] or lines[-10:])

    def sync(self, say: Callable[[str], None]) -> tuple[int, list[str]]:
        """
        Bring tikz/<figure>.pdf in line with the figure sources the last run
        listed: reuse a cached PDF with the same content hash, compile the rest in
        parallel. Returns (figures changed, errors).
        """
        changed = 0
        todo: list[tuple[str, str]] = []

        for name in self.names():
            key = self.key(name)
            pdf = self.file(name, ".pdf")
            marker = self.file(name, ".key")
            if key is None or (pdf.exists() and marker.exists() and marker.read_text() == key):
                continue
            cached = self.cache / f"{key}.pdf"
            if cached.exists():
                shutil.copy(cached, pdf)
                if self.cache / f"{key}.dpth".exists():
                    shutil.copy(self.cache / f"{key}.dpth", self.file(name, ".dpth"))
                marker.write_text(key)
                changed += 1
            else:
                pdf.unlink(missing_ok=True)
                todo.append((name, key))

        if not todo:
            return changed, []

        say(f"Compiling {len(todo)} figure(s) on {min(self.jobs, len(todo))} worker(s) ...")
        self.cache.mkdir(parents=True, exist_ok=True)
        began = time.monotonic()

        # Later figures take longer (their job reads the document up to the figure), so start them first.
        with ThreadPoolExecutor(max_workers=max(1, self.jobs)) as pool:
            results = list(pool.map(lambda item: self.compile(item[0]), reversed(todo)))

        say(f"Figures compiled in {time.monotonic() - began:.1f}s")
        errors = [result for result in results if result]
        for name, key in todo:
            pdf = self.file(name, ".pdf")
            if pdf.exists():
                shutil.copy(pdf, self.cache / f"{key}.pdf")
                dpth = self.file(name, ".dpth")
                if dpth.exists():
                    shutil.copy(dpth, self.cache / f"{key}.dpth")
                self.file(name, ".key").write_text(key)
                changed += 1
        return changed, errors

    def wipe(self) -> None:
        """Forget every cached figure (--force)."""
        shutil.rmtree(self.cache, ignore_errors=True)
        shutil.rmtree(self.build_dir / "tikz", ignore_errors=True)
        (self.build_dir / "tikz").mkdir(exist_ok=True)
