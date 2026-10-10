"""
Build accelerators for large documents (imported by build.py, standard library only).

externalize: TikZ/pgfplots figures are compiled once, in parallel, and cached by
content hash, so a build re-typesets only the text. The document is not edited:
a small file is injected with latexmk's -usepretex. See README, "Large documents".
"""

from __future__ import annotations

import functools
import hashlib
import json
import os
import re
import shutil
import subprocess
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Callable

import sandbox

# Runs before \begin{document}'s own hooks, which is the last moment tikz's
# external library may still be switched on. "list and make" needs no shell escape:
# the main run lists the figures, and the Python side compiles them (no make needed).
INJECT = r"""\makeatletter
\AddToHook{begindocument/before}{\ifcsname ver@tikz.sty\endcsname
\usetikzlibrary{external}\tikzexternalize[prefix=tikz/,mode=list and make]\fi}
% A figure job reads the document only up to its figure: once the figure is
% shipped, \input and \include (the chapters still to come) do nothing.
% Measured on a 300 page report: without this each figure job takes ~9 s (it
% reads the whole document), with it 1 to 6 s.
\def\pgfx@skipinput{\@ifnextchar\bgroup\@gobble\@gobble}
\def\pgfx@skip{\global\let\input\pgfx@skipinput\global\let\include\@gobble}
\AddToHook{begindocument/end}{\ifcsname pgf@externalend\endcsname\ifpgf@external@grabshipout
\ifcsname pgfx@noskip\endcsname\else
\let\pgfx@end\pgf@externalend\def\pgf@externalend{\pgfx@end\pgfx@skip}\fi\fi\fi}
\makeatother
"""

# Full builds record, for every \input/\include after \begin{document}, its file name,
# nesting depth and all counters, in <jobname>.focusmap. --focus uses that map.
# \pgff@f and \pgff@x add the number of TikZ figures before the file and the figures
# and counters after it, so a figure job can skip the file (see Figures.skip_tex).
RECORD = r"""\makeatletter
\newwrite\pgff@w \newcount\pgff@depth
\def\pgff@elt#1{\string\pgff@c{#1}{\the\csname c@#1\endcsname}}
\def\pgff@cnt{\begingroup\let\@elt\pgff@elt\xdef\pgff@cnt@{\cl@@ckpt}\endgroup}
\def\pgff@figs{\ifcsname tikzexternal@realjob\endcsname
\ifcsname c@tikzext@no@\tikzexternal@realjob-figure\endcsname
\csname c@tikzext@no@\tikzexternal@realjob-figure\endcsname\else0\fi\else-1\fi}
\def\pgff@enter#1#2{\global\advance\pgff@depth\@ne \pgff@cnt
\immediate\write\pgff@w{\string\pgff@e{#1}{\the\pgff@depth}{#2}{\pgff@cnt@}}%
\immediate\write\pgff@w{\string\pgff@f{\pgff@figs}}}
\def\pgff@leave{\pgff@cnt\immediate\write\pgff@w{\string\pgff@x{\the\pgff@depth}{\pgff@figs}{\pgff@cnt@}}%
\global\advance\pgff@depth\m@ne}
\AddToHook{begindocument/end}{\immediate\openout\pgff@w=\jobname.focusmap
\let\pgff@oldinput\input \let\pgff@oldinclude\include
\def\input{\@ifnextchar\bgroup\pgff@input\pgff@oldinput}
\def\pgff@input#1{\pgff@enter n{#1}\pgff@oldinput{#1}\pgff@leave}
\def\include#1{\pgff@enter i{#1}\pgff@oldinclude{#1}\pgff@leave}}
\makeatother
"""

# Figure jobs skip the \input files that neither hold their figure nor define anything,
# and set the counters and the figure count to what the full run recorded after them.
# Measured on a 300 page report: the last figure takes 1.8 s instead of 7.3 s.
FIGSKIP = r"""\makeatletter
\def\pgff@c#1#2{\ifcsname c@#1\endcsname\setcounter{#1}{#2}\fi}
\def\pgff@n#1{\expandafter\xdef\csname c@tikzext@no@\tikzexternal@realjob-figure\endcsname{#1}}
%s
\AddToHook{begindocument/end}{\let\pgfx@in\input
\def\input{\@ifnextchar\bgroup\pgfx@sin\pgfx@in}
\def\pgfx@sin#1{\ifcsname pgfx@s@#1\endcsname\expandafter\@firstoftwo\else\expandafter\@secondoftwo\fi
{\csname pgfx@s@#1\endcsname}{\pgfx@in{#1}}}}
\makeatother
"""

# Commands that can change what a later figure looks like. A file that uses one
# (outside its pictures) is always read by the figure jobs.
DEFINES = re.compile(
    r"\\(?:[gex]?def|let|global|(?:re)?new(?:command|environment|counter|length|if|theorem|toks|box)"
    r"|provide\w*|Declare\w*|\w*DocumentCommand|set(?:length|counter)|addto\w*|stepcounter|refstepcounter"
    r"|definecolor|colorlet|usepackage|RequirePackage|makeat\w*|catcode|AddToHook|Expl\w*|\w*setup"
    r"|\w*set|selectlanguage|tikz\w*|pgf\w*|use\w*library|include"
    # Not definitions, but they change what a later figure looks like when left switched on:
    r"|tiny|scriptsize|footnotesize|small|normalsize|large|Large|LARGE|huge|Huge|fontsize|selectfont|linespread"
    r"|\w*family|\w*series|\w*shape|color|(?:one|two)column|\w*geometry|setstretch)(?![a-zA-Z])"
)
COMMENT = re.compile(r"(?<!\\)%.*")
PICTURE = re.compile(r"(?s)\\begin\{tikzpicture\}.*?\\end\{tikzpicture\}")

# --focus: only the allowed files are read; every other \input does nothing
# (\include uses \includeonly). Counters are restored when the first focused file starts.
# %(allow)s, %(restore)s and %(only)s are filled in by focus_tex().
FOCUS = r"""\makeatletter
\def\pgff@c#1#2{\ifcsname c@#1\endcsname\setcounter{#1}{#2}\fi}
%(allow)s
\def\pgff@restore{%(restore)s}
\def\pgff@first{%(first)s}
\def\pgff@last{%(last)s}
%(only)s
\newbox\pgff@box
%% Pages before the focused part are thrown away, then the real \shipout comes back.
\def\pgff@go{\clearpage\global\let\shipout\pgff@shipout\global\let\pgff@first\relax}
\def\pgff@stop{\clearpage\gdef\shipout{\setbox\pgff@box=}}
\def\pgff@run#1{\ifx\pgff@n\pgff@first\pgff@go\pgff@restore\fi
\pgff@oldinput{#1}\edef\pgff@n{#1}\ifx\pgff@n\pgff@last\pgff@stop\fi}
\def\pgff@finput#1{\edef\pgff@n{#1}%%
\ifcsname pgff@ok@\pgff@n\endcsname\expandafter\pgff@run\else\expandafter\@gobble\fi{#1}}
\def\pgff@finclude#1{\edef\pgff@n{#1}\ifx\pgff@n\pgff@first\pgff@go\fi
\pgff@oldinclude{#1}\edef\pgff@n{#1}\ifx\pgff@n\pgff@last\pgff@stop\fi}
\AddToHook{begindocument/end}{\let\pgff@oldinput\input \let\pgff@oldinclude\include
\global\let\pgff@shipout\shipout \gdef\shipout{\setbox\pgff@box=}
\def\input{\@ifnextchar\bgroup\pgff@finput\pgff@oldinput}
\let\include\pgff@finclude
\let\maketitle\relax \let\tableofcontents\relax \let\listoffigures\relax \let\listoftables\relax}
\makeatother
"""

# Draft flag per engine for the figure-listing run (no PDF written).
DRAFT = {"pdflatex": "-draftmode", "lualatex": "--draftmode", "xelatex": "-no-pdf"}

# Files that change what a figure looks like without changing its source text.
FIGURE_DEPS = {".cls", ".sty", ".def", ".cfg", ".clo", ".pgf", ".tikz", ".csv", ".dat", ".tsv", ".table"}
NOSKIP = r"\expandafter\def\csname pgfx@noskip\endcsname{}"
TIKZ_WORDS = re.compile(r"tikz|pgfplots")
# The part of a .tex file that can change a figure: its tikzpicture environments and
# every other line that mentions tikz, pgf or axis (\tikzset, \pgfplotsset, \tikz ...;).
FIGURE_TEXT = re.compile(r"(?s:\\begin\{tikzpicture\}.*?\\end\{tikzpicture\})|^.*(?:tikz|pgf|axis).*$", re.M)
NO_FIGURES = hashlib.sha1(b"").hexdigest()


# Pictures that refer to each other or to the page cannot be cut out as stand-alone
# figures (they come out empty), so a document that has any is built without externalization.
UNSAFE = re.compile(r"remember\s*picture|overlay|tikzmark|tikzpagenodes|current\s+page")
LOCAL_INPUT = re.compile(r"\\(?:input|InputIfFileExists)\s*\{([^}]+)\}")


def uses_tikz(doc_dir: Path) -> bool:
    """
    True if the document draws with tikz/pgfplots and every picture can be
    externalized: any .tex/.cls/.sty file mentions tikz or pgfplots, and none
    uses remember picture, overlay or tikzmark.
    """
    found = False
    for path in doc_dir.rglob("*"):
        if path.suffix in {".tex", ".cls", ".sty"} and not path.name.startswith("."):
            try:
                text = path.read_text(encoding="utf-8", errors="replace")
            except OSError:
                continue
            if UNSAFE.search(text):
                return False
            found = found or bool(TIKZ_WORDS.search(text))
    return found


@functools.cache
def distribution(engine: str) -> str:
    """The TeX engine's version line and the timestamps of tikz.sty and pgfplots.sty."""
    parts = []
    for command in ([engine, "--version"], ["kpsewhich", "tikz.sty", "pgfplots.sty"]):
        try:
            output = subprocess.run(command, capture_output=True, text=True, timeout=60).stdout
        except (OSError, subprocess.TimeoutExpired):
            continue
        if command[0] == "kpsewhich":
            for line in output.split():
                try:
                    parts.append(f"{line}:{os.stat(line).st_mtime_ns}")
                except OSError:
                    pass
        else:
            parts.append(output.splitlines()[0] if output else "")
    return "|".join(parts)


def preamble_files(main_tex: Path) -> list[Path]:
    """Local files main.tex reads before \\begin{document}, found by following \\input recursively."""
    found: list[Path] = []
    pending = [main_tex.read_text(encoding="utf-8", errors="replace").split(r"\begin{document}")[0]]
    while pending:
        for name in LOCAL_INPUT.findall(pending.pop()):
            path = main_tex.parent / name.strip().strip('"')
            for candidate in (path, path.with_name(path.name + ".tex")):
                if candidate.is_file() and candidate not in found:
                    found.append(candidate)
                    pending.append(candidate.read_text(encoding="utf-8", errors="replace"))
                    break
    return found


def environment_hash(main_tex: Path, engine: str, shell_escape: bool) -> str:
    """
    Everything besides a figure's own source that can change its PDF: the
    engine and TikZ versions, the preamble (and the files it \\input's),
    local classes/packages/data files.
    """
    digest = hashlib.sha1(f"{engine}{shell_escape}{distribution(engine)}".encode())
    text = main_tex.read_text(encoding="utf-8", errors="replace")
    digest.update(text.split(r"\begin{document}")[0].encode())

    for path in preamble_files(main_tex):
        digest.update(path.read_bytes())

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


def write_inject(build_dir: Path, doc_dir: Path, name: str, text: str) -> str:
    """Write build_dir/name and return the TeX that inputs it from the document's directory."""
    path = build_dir / name
    path.write_text(text, encoding="utf-8")
    if os.environ.get("openin_any") == "p":
        # Paranoid reads (serve.py while sharing) refuse "..", but TeX Live also looks in the
        # -output-directory, which is build_dir (figure jobs get a copy in their own one).
        return rf"\input{{{name}}}"
    return rf"\input{{{tex_path(path, doc_dir)}}}"


class Figures:
    """The cached figures of one document."""

    def __init__(
        self, main_tex: Path, build_dir: Path, engine: str, shell_escape: bool, jobs: int,
        env: dict[str, str] | None = None, cache: Path | None = None,
    ):
        self.main_tex = main_tex
        self.build_dir = build_dir
        self.engine = engine
        self.shell_escape = shell_escape
        self.jobs = jobs
        self.env = {**os.environ, **(env or {})}  # e.g. SOURCE_DATE_EPOCH, so figure PDFs are reproducible
        self.stem = main_tex.stem
        # Outside build_dir: a clean-cache retry wipes build_dir, not the compiled figures.
        self.cache = cache or build_dir / "figcache"
        self.environment = environment_hash(main_tex, engine, shell_escape)
        (build_dir / "tikz").mkdir(exist_ok=True)
        self.pretex = write_inject(build_dir, main_tex.parent, "_inject.tex", INJECT)
        # The main run also records the \input tree (figure jobs must not).
        self.main_pretex = self.pretex + write_inject(build_dir, main_tex.parent, "_record.tex", RECORD)
        # When the run that wrote <stem>.focusmap started; figure jobs trust the map only if
        # no file of the document changed after that (build.py sets it before each run).
        self.recorded: float | None = self.load_recorded()
        self.skips: dict[str, str] = {}
        # .tex files whose figure text changed (set by touched()); None: something else changed too.
        self.dirty: set[str] | None = set()

    def load_recorded(self) -> float | None:
        """Start time of the run that wrote the focusmap, as persisted by mark_recorded()."""
        try:
            return float((self.build_dir / "focusmap.time").read_text())
        except (OSError, ValueError):
            return None

    def mark_recorded(self) -> None:
        """A run that rewrites the focusmap starts now. A partial listing does not, so it must not call this."""
        self.recorded = time.time()
        (self.build_dir / "focusmap.time").write_text(repr(self.recorded))

    def file(self, name: str, suffix: str) -> Path:
        # Not with_suffix(): a document called "v1.2" would lose its tail.
        return self.build_dir / f"{name}{suffix}"

    def touched(self, since: float) -> bool:
        """
        True if a file edited after `since` can have changed a figure: a class/package/data
        file, or a .tex file whose figure text (FIGURE_TEXT) differs from the last call's.
        Only a speed-up (the sync/latexmk loop is what makes figures correct): it lets a
        figure edit list and compile first. Measured on a 300 page report, a figure edit
        takes 24 s with it and 36 s without; comparing the figure text, not the whole file,
        spares a text edit next to a figure the listing run (23 s to 13 s).
        """
        snapshot = self.build_dir / "figtext.json"
        try:
            before = json.loads(snapshot.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            before = {}
        now: dict[str, str] = {}
        found = False
        dirty: set[str] = set()
        other = False
        for path in self.main_tex.parent.rglob("*"):
            if path.name.startswith(".") or not path.is_file():
                continue
            if path.suffix == ".tex":
                text = "\n".join(FIGURE_TEXT.findall(path.read_text(encoding="utf-8", errors="replace")))
                key = path.relative_to(self.main_tex.parent).as_posix()
                now[key] = hashlib.sha1(text.encode()).hexdigest()
                changed = now[key] != before.get(key, NO_FIGURES)
            else:
                changed = path.suffix in FIGURE_DEPS
            if changed and path.stat().st_mtime > since:
                found = True
                # Only a figure-text edit in a .tex file can be listed on its own (discover_partial).
                if path.suffix == ".tex":
                    dirty.add(key)
                else:
                    other = True
        self.dirty = None if other else dirty
        snapshot.write_text(json.dumps(now), encoding="utf-8")
        return found

    def names(self) -> list[str]:
        figlist = self.build_dir / f"{self.stem}.figlist"
        if not figlist.exists():
            return []
        return [line.strip() for line in figlist.read_text(encoding="utf-8").splitlines() if line.strip()]

    def keys(self) -> dict[str, str | None]:
        """
        Cache key per figure: environment + source hash + how many figures before it have
        the same source. Equal sources can draw different pictures (a \\foreach loop
        in a macro), so the n-th copy gets its own key; renumbering still costs nothing.
        """
        seen: dict[str, int] = {}
        keys: dict[str, str | None] = {}
        for name in self.names():
            md5 = self.file(name, ".md5")
            if not md5.exists():
                keys[name] = None
                continue
            text = md5.read_text(encoding="utf-8")
            seen[text] = seen.get(text, -1) + 1
            keys[name] = hashlib.sha1(f"{self.environment}{text}#{seen[text]}".encode()).hexdigest()
        return keys

    def discover_command(self) -> list[str]:
        return [
            self.engine, "-interaction=batchmode", DRAFT[self.engine], "-file-line-error",
            f"-output-directory={self.build_dir}", f"-jobname={self.stem}",
            *(["-shell-escape"] if self.shell_escape else []),
            f"{self.main_pretex}\\input{{{self.main_tex.name}}}",
        ]

    def quiet_entries(self, entries: list[dict]) -> list[bool]:
        """Per focusmap entry: neither its file nor any file it reads defines anything."""
        doc = self.main_tex.parent
        own = []  # Per entry: its file defines nothing.
        for entry in entries:
            path = doc / entry["raw"]
            path = path if path.suffix == ".tex" and path.is_file() else path.with_name(path.name + ".tex")
            try:
                own.append(not DEFINES.search(PICTURE.sub("", COMMENT.sub("", path.read_text(encoding="utf-8")))))
            except (OSError, UnicodeDecodeError):
                own.append(False)
        quiet = []
        for i, entry in enumerate(entries):
            j = i + 1
            while j < len(entries) and entries[j]["depth"] > entry["depth"]:
                j += 1
            quiet.append(all(own[i:j]))
        return quiet

    def discover_partial(self) -> bool:
        """
        List only the figures of the files whose figure text changed (touched() found them),
        reading nothing else: the files that define nothing are skipped, as in a figure job.
        That is one or two seconds where the full listing run typesets the whole document
        (11 s of a 25 s build of a 300 page report). Writes the new <figure>.md5 files into
        build_dir/tikz and returns True. Returns False, having changed nothing, whenever it is
        not sure the figure numbers are still the ones of the last full listing (a figure was
        added or removed, the \\input tree is unknown): the caller then lists everything.
        This is only a speed-up: the text pass compares every figure with its .md5 and sets
        a figure whose source differs inline, and the sync/latexmk loop corrects it afterwards.
        """
        mapfile = self.build_dir / f"{self.stem}.focusmap"
        old = self.names()
        if not self.dirty or not old or not mapfile.exists():
            return False
        doc = self.main_tex.parent
        if self.recorded is None:
            return False  # Unknown when the map was written: nothing in it can be trusted.
        entries = read_focusmap(mapfile)
        quiet = self.quiet_entries(entries)
        files = [normalize(entry["raw"]) for entry in entries]

        def edited(entry: dict) -> bool:
            # A file changed since the map can hold other figures (made by a macro) or other counters.
            path = doc / entry["raw"]
            path = path if path.suffix == ".tex" and path.is_file() else path.with_name(path.name + ".tex")
            try:
                return path.stat().st_mtime > self.recorded - 2
            except OSError:
                return True

        wanted = {normalize(path) for path in self.dirty}
        if not (wanted - {normalize(self.main_tex.name)}) <= set(files):
            return False  # A changed file the last run did not read: its place is unknown.
        skip, covered = [], set()
        for i, entry in enumerate(entries):
            j = i + 1
            while j < len(entries) and entries[j]["depth"] > entry["depth"]:
                j += 1
            if (
                entry["kind"] == "n" and quiet[i] and files.count(files[i]) == 1
                and 0 <= entry.get("figs", -1) <= entry.get("end", -1)
                and not wanted & set(files[i:j]) and not any(edited(e) for e in entries[i:j])
            ):
                skip.append(entry)
                covered.update(range(entry["figs"], entry["end"]))
        # Skipping an entry that sits inside a skipped one is harmless; the figures it
        # holds are in `covered` twice at most.
        numbers = {name: re.fullmatch(rf"tikz/{re.escape(self.stem)}-figure(\d+)", name) for name in old}
        if not all(numbers.values()):
            return False  # Figures with their own names (\tikzsetnextfilename) have no place in the map.
        expected = {name for name, found in numbers.items() if int(found.group(1)) not in covered}
        if not skip or not expected:
            return False
        lines = [
            rf"\expandafter\def\csname pgfx@s@{entry['raw']}\endcsname{{"
            + "".join(rf"\pgff@c{{{c}}}{{{v}}}" for c, v in entry["exit"]) + rf"\pgff@n{{{entry['end']}}}}}"
            for entry in skip
        ]
        work = self.build_dir / "figwork" / "list"
        shutil.rmtree(work, ignore_errors=True)
        (work / "tikz").mkdir(parents=True)
        try:
            for aux in self.build_dir.glob("*.aux"):  # Lets \ref and \cite inside a figure resolve.
                shutil.copy(aux, work / aux.name)
            shutil.copy(self.build_dir / "_inject.tex", work / "_inject.tex")  # See write_inject.
            inject = write_inject(work, doc, "_skip.tex", FIGSKIP % "\n".join(lines))
            command = self.discover_command()
            command[command.index(f"-output-directory={self.build_dir}")] = f"-output-directory={work}"
            command[-1] = self.pretex + inject + f"\\input{{{self.main_tex.name}}}"
            try:
                subprocess.run(
                    **sandbox.spawn(command, doc, self.build_dir, self.env, work=work),
                    stdin=subprocess.DEVNULL, timeout=1800, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                )
            except (OSError, subprocess.TimeoutExpired):
                return False
            finally:
                sandbox.scrub(work)
            listed = work / f"{self.stem}.figlist"
            names = {line.strip() for line in listed.read_text(encoding="utf-8").splitlines() if line.strip()} \
                if listed.exists() else set()
            if names != expected or not all((work / f"{name}.md5").exists() for name in names):
                return False
            for name in names:
                shutil.copy(work / f"{name}.md5", self.file(name, ".md5"))
            return True
        finally:
            shutil.rmtree(work, ignore_errors=True)

    def plan_skips(self) -> None:
        """
        Work out, per figure, which files its job may skip (self.skips: name -> TeX).
        Nothing is skipped unless the recorded \\input tree is as new as every file of the document.
        """
        self.skips = {}
        mapfile = self.build_dir / f"{self.stem}.focusmap"
        if self.recorded is None or not mapfile.exists():
            return
        doc = self.main_tex.parent
        # 2 s: file systems with coarse timestamps (FAT, HFS+) round an edit made during the run down.
        files = [path for path in doc.rglob("*") if path.is_file() and not path.name.startswith(".")]
        if any(path.stat().st_mtime > self.recorded - 2 for path in files):
            return
        entries = read_focusmap(mapfile)
        raws = [entry["raw"] for entry in entries]
        quiet = self.quiet_entries(entries)
        for name in self.names():
            match = re.fullmatch(rf"tikz/{re.escape(self.stem)}-figure(\d+)", name)
            if not match:
                continue
            number = int(match.group(1))
            lines = [
                rf"\expandafter\def\csname pgfx@s@{entry['raw']}\endcsname{{"
                + "".join(rf"\pgff@c{{{c}}}{{{v}}}" for c, v in entry["exit"]) + rf"\pgff@n{{{entry['end']}}}}}"
                for i, entry in enumerate(entries)
                if entry["kind"] == "n" and quiet[i] and raws.count(entry["raw"]) == 1
                and 0 <= entry.get("figs", -1) <= entry.get("end", -1)
                and not entry["figs"] <= number < entry["end"]
            ]
            if lines:
                self.skips[name] = FIGSKIP % "\n".join(lines)

    def figure_command(self, name: str, work: Path, skip: bool = True) -> list[str]:
        extra = NOSKIP
        if skip:
            extra = ""
            if name in self.skips:
                extra = write_inject(work, self.main_tex.parent, "_skip.tex", self.skips[name])
        return [
            self.engine, "-interaction=batchmode", "-halt-on-error", "-file-line-error",
            f"-output-directory={work}", f"-jobname={name}",
            *(["-shell-escape"] if self.shell_escape else []),
            self.pretex + extra
            + f"\\def\\tikzexternalrealjob{{{self.stem}}}\\input{{{self.main_tex.name}}}",
        ]

    def compile(self, name: str) -> str | None:
        """Compile one figure. Returns an error text, or None on success."""
        # A private output directory: the document may write its own .aux files (one per
        # chapter unit) and parallel jobs must not touch the ones latexmk reads.
        work = self.build_dir / "figwork" / hashlib.sha1(name.encode()).hexdigest()[:12]

        def made(suffix: str) -> Path:
            return work / f"{name}{suffix}"

        # Skipping the rest of the document is an optimisation; if it breaks
        # something that is read later, run the figure once more without it.
        for skip in (True, False):
            shutil.rmtree(work, ignore_errors=True)
            made(".aux").parent.mkdir(parents=True)
            for aux in self.build_dir.glob("*.aux"):  # Lets \ref and \cite inside the figure resolve.
                shutil.copy(aux, work / aux.name)
            shutil.copy(self.build_dir / "_inject.tex", work / "_inject.tex")  # See write_inject.
            if (work / f"{self.stem}.aux").exists():
                shutil.copy(work / f"{self.stem}.aux", made(".aux"))
            try:
                subprocess.run(
                    **sandbox.spawn(self.figure_command(name, work, skip), self.main_tex.parent, self.build_dir,
                                    self.env, work=work),
                    stdin=subprocess.DEVNULL, timeout=1800, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                )
            except (OSError, subprocess.TimeoutExpired) as exc:
                return f"{name}: {exc}"
            finally:
                sandbox.scrub(work)
            if made(".pdf").exists():
                for suffix in (".pdf", ".dpth"):
                    if made(suffix).exists():
                        shutil.copy(made(suffix), self.file(name, suffix))
                shutil.rmtree(work, ignore_errors=True)
                return None

        log = made(".log")
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
        self.plan_skips()

        keys = self.keys()
        for name, key in keys.items():
            pdf = self.file(name, ".pdf")
            marker = self.file(name, ".key")
            if key is None or (pdf.exists() and marker.exists() and marker.read_text() == key):
                continue
            cached = self.cache / f"{key}.pdf"
            if cached.exists():
                shutil.copy(cached, pdf)
                if (self.cache / f"{key}.dpth").exists():
                    shutil.copy(self.cache / f"{key}.dpth", self.file(name, ".dpth"))
                marker.write_text(key)
                changed += 1
            else:
                pdf.unlink(missing_ok=True)
                todo.append((name, key))

        if not todo:
            self.prune(keys)
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
        if not errors:
            self.prune(keys)
        return changed, errors

    def prune(self, keys: dict[str, str | None]) -> None:
        """Delete cached figures that no figure of the document refers to any more."""
        used = {key for key in keys.values() if key}
        for path in self.cache.glob("*"):
            if path.stem not in used:
                path.unlink(missing_ok=True)

    def wipe(self) -> None:
        """Forget every cached figure (--force)."""
        shutil.rmtree(self.cache, ignore_errors=True)
        shutil.rmtree(self.build_dir / "tikz", ignore_errors=True)
        (self.build_dir / "figtext.json").unlink(missing_ok=True)
        (self.build_dir / "tikz").mkdir(exist_ok=True)


# ---------------------------------------------------------------------------
# --focus
# ---------------------------------------------------------------------------

ENTRY = re.compile(r"\\pgff@e\{([in])\}\{(\d+)\}\{(.*?)\}\{(.*)\}$")
COUNTER = re.compile(r"\\pgff@c\{([^}]*)\}\{(-?\d+)\}")
FIGS = re.compile(r"\\pgff@f\{(-?\d+)\}$")
EXIT = re.compile(r"\\pgff@x\{(\d+)\}\{(-?\d+)\}\{(.*)\}$")


def normalize(path: str) -> str:
    """Chapters/./ch1.tex -> Chapters/ch1 (as an \\input argument is compared)."""
    path = path.strip().replace("\\", "/")
    path = re.sub(r"(^|/)\./", r"\1", path)
    return path[:-4] if path.endswith(".tex") else path


def read_focusmap(path: Path) -> list[dict]:
    """
    The \\input tree a full build recorded: [{kind, depth, path, counters}, ...] in reading order.
    Entries whose file was read to the end also have figs/end (TikZ figures before and after it)
    and exit (counters after it).
    """
    entries: list[dict] = []
    open_: list[dict] = []
    for line in path.read_text(encoding="utf-8", errors="replace").splitlines():
        line = line.strip()
        match = ENTRY.match(line)
        if match:
            entries.append({
                "kind": match.group(1), "depth": int(match.group(2)), "path": normalize(match.group(3)),
                "raw": match.group(3), "counters": COUNTER.findall(match.group(4)),
            })
            open_.append(entries[-1])
        elif (match := FIGS.match(line)) and entries:
            entries[-1]["figs"] = int(match.group(1))
        elif (match := EXIT.match(line)) and open_ and open_[-1]["depth"] == int(match.group(1)):
            entry = open_.pop()
            entry["end"], entry["exit"] = int(match.group(2)), COUNTER.findall(match.group(3))
    return entries


def focus_selection(entries: list[dict], focus: str) -> tuple[set[int], int | None]:
    """
    Indexes of the entries to read for `focus`, and the index of the first one
    that matches. Read: the matches, everything they read themselves, and the
    files that read them (their ancestors).
    """
    focus = normalize(focus).rstrip("/")
    matches = [
        i for i, entry in enumerate(entries)
        if entry["path"] == focus or entry["path"].startswith(focus + "/")
    ]
    keep: set[int] = set()

    for i in matches:
        keep.add(i)
        depth = entries[i]["depth"]
        for j in range(i + 1, len(entries)):  # Children.
            if entries[j]["depth"] <= depth:
                break
            keep.add(j)
        for j in range(i - 1, -1, -1):  # Ancestors.
            if entries[j]["depth"] < depth:
                keep.add(j)
                depth = entries[j]["depth"]

    return keep, (matches[0] if matches else None)


def focus_tex(entries: list[dict], keep: set[int], first: int) -> str:
    """The TeX file injected for a focus run."""
    allow = "\n".join(
        rf"\expandafter\def\csname pgff@ok@{entries[i]['raw']}\endcsname{{}}"
        for i in sorted(keep) if entries[i]["kind"] == "n"
    )
    restore = "".join(
        rf"\pgff@c{{{name}}}{{{value}}}" for name, value in entries[first]["counters"]
    ) if entries[first]["kind"] == "n" else ""
    included = [entries[i]["raw"] for i in sorted(keep) if entries[i]["kind"] == "i"]
    # \includeonly lets LaTeX itself keep the counters and page numbers of skipped chapters.
    only = rf"\AddToHook{{begindocument/before}}{{\includeonly{{{','.join(included)}}}}}" if any(
        entry["kind"] == "i" for entry in entries
    ) else ""
    shallow = min(entries[i]["depth"] for i in keep)
    last = max(i for i in keep if entries[i]["depth"] == shallow)
    return FOCUS % {
        "allow": allow, "restore": restore, "only": only,
        "first": entries[first]["raw"], "last": entries[last]["raw"],
    }


def top_unit(entries: list[dict], changed: str) -> str | None:
    """
    The outermost recorded file (depth 1, read by main.tex) that holds the file
    `changed` (a path relative to the document, with or without .tex). For --focus auto.
    """
    changed = normalize(changed)
    for i, entry in enumerate(entries):
        if entry["path"] != changed:
            continue
        for j in range(i, -1, -1):
            if entries[j]["depth"] == 1:
                return entries[j]["path"]
    return None
