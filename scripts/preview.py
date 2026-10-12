"""
Warm preview compiler for the editor's live preview (imported by serve.py, standard library only).

A chapter preview (build.build_focus) spends most of its time loading the preamble: about 1.1 s of
1.8 s on bench/sample-report. Here a TeX process is started ahead of time on the document with
\\begin{document} redefined to wait for one line on stdin, so it loads the class and packages and then
blocks. When a preview is wanted, the --focus selection (accel.focus_tex) and the editor's unsaved text
of the file being typed (an "overlay" that LaTeX reads in place of that file) are written next to it, the
line is sent, and TeX typesets only the focused chapter; the next process is started at the same moment.

Each process is used once (TeX cannot go back to its preamble state), runs through sandbox.spawn like
every LaTeX run, and is replaced when anything that shaped its preamble changes: main.tex's preamble,
the files it reads, local .cls/.sty files, build settings, the engine, or the build environment
(sharing restrictions, sandbox). The full build's .aux/.bbl/.toc and its \\input tree are read from a
snapshot taken after each successful full build, never from a build that is still writing them.

Labels the chapter defines and citations of keys the full build did not cite are resolved in the same
preview: a second (and third) pass on the next waiting process reads the first pass's labels, and BibTeX or
biber (in the sandbox, on the snapshot's citations plus the new ones) writes the bibliography it needs.
"""

from __future__ import annotations

import hashlib
import itertools
import json
import os
import queue
import re
import shutil
import subprocess
import threading
import time
from pathlib import Path

import accel
import build
import sandbox

# Engines whose warm start was checked to give the same PDF as a plain focus build.
ENGINES = {"pdflatex", "xelatex", "lualatex"}
SNAPSHOT = {".aux", ".bbl", ".toc", ".lof", ".lot", ".out", ".focusmap", ".bcf"}  # .bcf: read, not copied
FIGURE_FILES = {".pdf", ".dpth"}  # of the full build's externalized TikZ figures (build_dir/tikz/)
# Files that can change what the preamble does without changing main.tex (besides files it \input's).
PREAMBLE_DEPS = {".cls", ".sty", ".def", ".cfg", ".clo", ".fd", ".ldf", ".toml", ".latexmkrc"}
ENV_KEYS = ("shell_escape", "openin_any", "openout_any", sandbox.VARIABLE)
IDLE = 600.0  # seconds a waiting process is kept before it is stopped
ACTIVE = 60.0  # seconds after a preview during which full builds and figure jobs run below normal priority
TIMEOUT = 120  # seconds one preview may take
PASSES = 3  # at most: labels of the chapter itself, then a bibliography BibTeX/biber rewrote, then its citations
TRIGGER = "pgfwarm-go"  # file names in the process's own directory (its -output-directory)
OVERLAY = "pgfwarm-ov"
SOURCE = "source.snap"  # main.tex as the snapshot's build saw it (not copied into the processes)
INPUTS = "inputs.snap"  # files of the document the build read (from its .fls)
SYNC_KEEP = 16  # SyncTeX files of the newest previews kept for double-click

# Wait at the very start of \begin{document}, before the .aux files are read. -interaction=nonstopmode
# refuses to read the terminal, so only the read itself runs in errorstopmode. An empty line ends the run.
WAIT = r"""\makeatletter
\let\pgfw@document\document
\def\document{\errorstopmode\endlinechar=-1 \read-1 to\pgfw@line\endlinechar=13 \nonstopmode
\ifx\pgfw@line\@empty\expandafter\@@end\fi\input{\pgfw@line}\pgfw@document}
\makeatother
"""
# The file being typed is read from the overlay instead, however it is read (\input, \include, a package).
SUBSTITUTE = "\\makeatletter\\declare@file@substitution{%s}{%s}\\makeatother\n"
# An \include'd file: the substitution would rename the part (\includeonly and its .aux go by the name), so
# \include's own \@input@ reads the overlay instead.
INCLUDE = r"""\makeatletter
\expandafter\def\csname pgfw@inc@%s\endcsname{%s}
\ifdefined\pgfw@inputat\else\let\pgfw@inputat\@input@
\def\@input@#1{\ifcsname pgfw@inc@#1\endcsname\expandafter\@firstoftwo\else\expandafter\@secondoftwo\fi
{\pgfw@inputat{\csname pgfw@inc@#1\endcsname}}{\pgfw@inputat{#1}}}\fi
\makeatother
"""
# main.tex's own body: read from the overlay once \begin{document} is done; its \end{document} ends the run
# before TeX gets back to main.tex on disk. The primitive \input: the focus selection's \input skips files.
BODY = "\\makeatletter\\AddToHook{begindocument/end}{\\@@input{%s}}\\makeatother\n"


# The full build's TikZ figures instead of typesetting them again (chapter 3 of the bench report: 2.3 s to 0.8 s).
# Pictures are numbered in document order, so the count the full build recorded before the chapter comes back with
# the other counters. Only used when no picture of the chapter changed since that build (figtext.json).
FIGURES_TEX = r"""\makeatletter
\AddToHook{begindocument/before}{\ifcsname ver@tikz.sty\endcsname
\usetikzlibrary{external}\tikzexternalize[prefix=tikz/,mode=graphics if exists]
\g@addto@macro\pgff@restore{\expandafter\xdef\csname c@tikzext@no@\tikzexternal@realjob-figure\endcsname{%d}}\fi}
\makeatother
"""

# An edit of main.tex between two chapters: every chapter is skipped; pages are kept from where the chapter
# before the edit ends (its counters come back) to where the chapter after it starts.
REGION = r"""\makeatletter
\def\pgff@c#1#2{\ifcsname c@#1\endcsname\setcounter{#1}{#2}\fi}
\newbox\pgff@box
\def\pgff@prev{%(prev)s}\def\pgff@next{%(next)s}
\def\pgff@after#1{\edef\pgff@n{#1}\ifx\pgff@n\pgff@prev\clearpage\global\let\shipout\pgff@shipout%(restore)s\fi}
\def\pgff@before#1{\edef\pgff@n{#1}\ifx\pgff@n\pgff@next\clearpage\gdef\shipout{\setbox\pgff@box=}\fi}
\def\pgff@rinput#1{\pgff@before{#1}\pgff@after{#1}}
\def\pgff@rinclude#1{\pgff@before{#1}\pgff@oldinclude{#1}\pgff@after{#1}}
\AddToHook{begindocument/end}{\let\pgff@oldinput\input \let\pgff@oldinclude\include
\global\let\pgff@shipout\shipout%(hide)s
\def\input{\@ifnextchar\bgroup\pgff@rinput\pgff@oldinput}\let\include\pgff@rinclude}
%(only)s\makeatother
"""

LABEL = re.compile(r"^\\(?:newlabel|bibcite)\{([^{}]*)\}", re.M)
CITATION = re.compile(r"^\\citation\{([^{}]*)\}", re.M)
UNDEFINED_CITE = re.compile(r"Citation [`']([^']+)' (?:on page \S+ )?undefined")
LABEL_CMD = re.compile(r"\\label(?:\[[^\]]*\])?\{[^{}]*\}")
# biblatex's control file (.bcf) for biber: cited keys per refsection, and the files to read.
BCF_KEY = re.compile(r"<bcf:citekey [^>]*>([^<]*)</bcf:citekey>")
BCF_SECTION = re.compile(r'(<bcf:section number="(\d+)">)(.*?)(</bcf:section>)', re.S)
BCF_SOURCE = re.compile(r"[ \t]*<bcf:datasource ([^>]*)>([^<]*)</bcf:datasource>\n?")


class PreviewError(Exception):
    """Nothing to preview (no chapter, no full build yet, unsupported engine); the message says why."""


def tex_ref(path: Path, doc_dir: Path) -> str:
    """How TeX in doc_dir names a file of a process directory (accel.write_inject's rule)."""
    if os.environ.get("openin_any") == "p":
        # Paranoid reads refuse "..", but TeX Live also looks in the -output-directory (only with the extension).
        return path.name
    return accel.tex_path(path, doc_dir)


def tex_name(raw: str) -> str:
    """The file an \\input argument reads: Chapters/ch1 -> Chapters/ch1.tex."""
    return raw if raw.endswith(".tex") else raw + ".tex"


def root_for(main_tex: Path) -> Path:
    return build.cache_dir_for(main_tex) / "preview"


SNAPSHOT_LOCK = threading.Lock()  # a snapshot is never replaced while a preview copies it


def snapshot(main_tex: Path) -> None:
    """Copy the full build's cross-reference files and input tree; call it while no full build runs."""
    with SNAPSHOT_LOCK:
        _snapshot(main_tex)


def _snapshot(main_tex: Path) -> None:
    build_dir = build.cache_dir_for(main_tex)
    target = root_for(main_tex) / "aux"
    fresh = target.with_name("aux.new")
    shutil.rmtree(fresh, ignore_errors=True)
    fresh.mkdir(parents=True)
    for path in build_dir.iterdir():
        if (path.suffix in SNAPSHOT or path.name == "figtext.json") and path.is_file():
            shutil.copy2(path, fresh / path.name)
    if (build_dir / "tikz").is_dir():
        (fresh / "tikz").mkdir()
        for path in (build_dir / "tikz").iterdir():
            if path.suffix in FIGURE_FILES:
                shutil.copy2(path, fresh / "tikz" / path.name)
    shutil.copy2(main_tex, fresh / SOURCE)
    (fresh / INPUTS).write_text("\n".join(document_inputs(main_tex, build_dir / f"{main_tex.stem}.fls")),
                                encoding="utf-8")
    shutil.rmtree(target, ignore_errors=True)
    fresh.rename(target)


def document_inputs(main_tex: Path, fls: Path) -> list[str]:
    """Files of the document's directory that a build read (its -recorder list), relative to it."""
    doc = os.path.normcase(str(main_tex.parent.resolve()))
    found: dict[str, None] = {}
    try:
        lines = fls.read_text(encoding="utf-8", errors="replace").splitlines()
    except OSError:
        return []
    for line in lines:
        if line.startswith("INPUT "):
            name = os.path.normcase(os.path.realpath(os.path.join(str(main_tex.parent), line[6:].strip())))
            if name.startswith(os.path.join(doc, "")):
                found[Path(os.path.relpath(name, doc)).as_posix()] = None
    return list(found)


def has_snapshot(main_tex: Path) -> bool:
    return (root_for(main_tex) / "aux" / f"{main_tex.stem}.focusmap").exists()


def preamble_of(text: str) -> str:
    return text.split(r"\begin{document}")[0]


def preamble_key(main_tex: Path, settings: dict, overrides: dict[str, str] | None = None) -> str:
    """
    What a waiting process depends on. A different key means it must be replaced. overrides: unsaved text of
    preamble files (relative name -> text) that the process reads in place of the file on disk.
    """
    overrides = overrides or {}
    digest = hashlib.sha1(repr((
        settings["engine"], bool(settings["shell_escape"]), [os.environ.get(k) for k in ENV_KEYS], settings["review"],
        sorted(overrides),
    )).encode())
    main_text = overrides.get(main_tex.name) or main_tex.read_text(encoding="utf-8", errors="replace")
    digest.update(preamble_of(main_text).encode())
    for path in accel.preamble_files(main_tex):
        rel = path.relative_to(main_tex.parent).as_posix()
        digest.update(overrides[rel].encode() if rel in overrides else path.read_bytes())
    for path in sorted(main_tex.parent.rglob("*")):
        if path.suffix in PREAMBLE_DEPS and path.is_file():
            rel, stat = path.relative_to(main_tex.parent).as_posix(), path.stat()
            mark = hashlib.sha1(overrides[rel].encode()).hexdigest() if rel in overrides \
                else f"{stat.st_mtime_ns}:{stat.st_size}"
            digest.update(f"{rel}:{mark}".encode())
    return digest.hexdigest()


def labels(texts) -> dict[str, str]:
    """\\newlabel and \\bibcite lines of .aux texts, by name (a later one wins, as in LaTeX)."""
    found: dict[str, str] = {}
    for text in texts:
        for match in LABEL.finditer(text):
            line_end = text.find("\n", match.start())
            found[match.group(1)] = text[match.start():line_end if line_end >= 0 else len(text)]
    return found


def shown(line: str) -> list[str]:
    """
    What a reference prints from a label line: the first two groups of its value (number and page). hyperref's
    anchor names that follow differ in every preview (counters of other chapters are not restored) and print nothing.
    """
    groups, depth, start = [], 0, line.find("}{") + 1
    for i in range(start, len(line)):
        if line[i] == "{":
            depth += 1
            if depth == 2:
                begin = i
        elif line[i] == "}":
            depth -= 1
            if depth == 1:
                groups.append(line[begin:i + 1])
                if len(groups) == 2:
                    break
            elif depth == 0:
                break
    return groups or [line]


def referenced(name: str, text: str) -> bool:
    """name (a label or citation key) is used in text other than by \\label: a \\ref, \\cite, \\cref ... of it."""
    base = name[:-5] if name.endswith("@cref") else name
    return re.search(r"[{,]\s*" + re.escape(base) + r"\s*[},]", LABEL_CMD.sub("", text)) is not None


def _spawner(jobs: queue.Queue) -> None:
    while True:
        job, box, done = jobs.get()
        try:
            box.append(job())
        except BaseException as exc:  # noqa: BLE001 - handed back to the caller.
            box.append(exc)
        done.set()


SPAWN_JOBS: queue.Queue = queue.Queue()
threading.Thread(target=_spawner, args=(SPAWN_JOBS,), daemon=True, name="preview-spawner").start()


def spawn_here(job):
    """
    Run job (a Popen) on one long-lived thread. bwrap --die-with-parent ties the sandbox to the thread that started
    it, and the HTTP thread that asks for a preview ends with its request, long before the waiting process is used.
    """
    box: list = []
    done = threading.Event()
    SPAWN_JOBS.put((job, box, done))
    done.wait()
    if isinstance(box[0], BaseException):
        raise box[0]
    return box[0]


class Proc:
    """One TeX process waiting at \\begin{document} in its own directory."""

    ids = itertools.count()

    def __init__(self, main_tex: Path, settings: dict, key: str, overrides: dict[str, str] | None = None) -> None:
        self.key, self.main_tex = key, main_tex
        self.root = root_for(main_tex)
        self.dir = self.root / f"p{os.getpid()}-{next(Proc.ids)}"
        shutil.rmtree(self.dir, ignore_errors=True)
        self.dir.mkdir(parents=True)
        build.mirror_dirs(main_tex.parent, self.dir)  # \include{dir/x} writes dir/x.aux here.
        self.cancelled = False
        # Unsaved preamble text (main.tex, a class, a package, a file the preamble reads), read in place of the file.
        swap = ""
        for i, (rel, text) in enumerate(sorted((overrides or {}).items())):
            path = self.dir / f"pgfwarm-pre{i}{Path(rel).suffix}"
            path.write_text(text, encoding="utf-8")
            swap += (SUBSTITUTE % (rel, tex_ref(path, main_tex.parent))).strip()  # one command line: no newline
        command = [
            settings["engine"], "-interaction=nonstopmode", "-halt-on-error", "-file-line-error", "-synctex=1",
            f"-output-directory={self.dir}", f"-jobname={main_tex.stem}",
            *(["-shell-escape"] if settings["shell_escape"] else []),
            build.review_pretex(settings) + accel.write_inject(self.dir, main_tex.parent, "pgfwarm-wait.tex", WAIT)
            + swap + f"\\input{{{main_tex.name}}}",
        ]
        self.out = open(self.dir / "console.txt", "w+", encoding="utf-8", errors="replace")
        try:
            spec = sandbox.spawn(command, main_tex.parent, self.root, {**os.environ, **build.LATEX_LOG_ENV},
                                 cpu=TIMEOUT, work=self.dir, urgent=True)
            self.popen = spawn_here(lambda: subprocess.Popen(
                **spec, stdin=subprocess.PIPE, stdout=self.out, stderr=subprocess.STDOUT,
            ))
        except BaseException:
            self.out.close()
            shutil.rmtree(self.dir, ignore_errors=True)
            raise

    def alive(self) -> bool:
        return self.popen.poll() is None

    def trigger(self, job: dict, extra: dict[str, bytes], labels_tex: str) -> None:
        """
        Start typesetting job (focus TeX and overlays). extra: files written over the snapshot's (a new .bbl);
        labels_tex: lines appended to the main .aux, read after the snapshot's (labels of an earlier pass).
        """
        doc = self.main_tex.parent
        tex = job["tex"]
        for i, (name, _, text) in enumerate(job["overlays"]):
            path = self.dir / f"{OVERLAY}{i}.tex"
            path.write_text(text, encoding="utf-8")
            if name is None:
                tex += BODY % tex_ref(path, doc)
            elif name.startswith("include:"):
                tex += INCLUDE % (name[8:], tex_ref(path, doc))
            else:
                tex += SUBSTITUTE % (name, tex_ref(path, doc))
        (self.dir / f"{TRIGGER}.tex").write_text(tex, encoding="utf-8")
        with SNAPSHOT_LOCK:
            self._copy_snapshot()
        for name, data in extra.items():
            (self.dir / name).write_bytes(data)
        if labels_tex:
            with open(self.dir / f"{self.main_tex.stem}.aux", "a", encoding="utf-8") as aux:
                aux.write("\n" + labels_tex + "\n")
        self.popen.stdin.write(tex_ref(self.dir / f"{TRIGGER}.tex", doc).encode() + b"\n")
        self.popen.stdin.close()

    def _copy_snapshot(self) -> None:
        for path in (self.root / "aux").iterdir():
            if path.suffix in (".snap", ".bcf"):  # A .bcf in the process's directory is one its run wrote.
                continue
            if path.is_dir():  # Figures: linked, not copied (TeX only reads them).
                (self.dir / path.name).mkdir(exist_ok=True)
                for figure in path.iterdir():
                    try:
                        os.link(figure, self.dir / path.name / figure.name)
                    except OSError:
                        shutil.copy2(figure, self.dir / path.name / figure.name)
            else:
                shutil.copy2(path, self.dir / path.name)

    def stop(self) -> None:
        self.cancelled = True
        try:
            self.popen.kill()
            self.popen.wait(10)
        except (OSError, subprocess.TimeoutExpired):
            pass
        self.cleanup()

    def cleanup(self) -> None:
        self.out.close()
        if self.popen.stdin and not self.popen.stdin.closed:
            try:
                self.popen.stdin.close()
            except OSError:
                pass
        sandbox.scrub(self.dir)
        shutil.rmtree(self.dir, ignore_errors=True)


def status(kind: str, **more) -> dict:
    """A reply without a PDF: preamble (rebuilding), rebuild (main.tex's chapters changed), same (as built)."""
    return {"ok": False, "cancelled": False, "pdf": None, "errors": [], "log": "", "warm": False, "status": kind,
            **more}


class Warm:
    """The waiting processes of one document, and the previews running from it."""

    def __init__(self, main_tex: Path) -> None:
        self.main_tex = main_tex
        self.lock = threading.Lock()
        self.ready: list[Proc] = []  # oldest (most likely done loading its preamble) first
        # Waiting processes kept: 2 once a preview needed a second pass (new labels or citations), so that
        # pass also starts on a process that has its preamble loaded already (about 0.4 s on the bench report).
        self.pool = 1
        self.running: dict[str, Proc] = {}  # client -> its preview in flight
        self.latest: dict[str, int] = {}  # client -> number of its newest compile (an older one stops)
        self.targets: dict[str, str] = {}  # client -> chapter it previewed last (for files a package reads)
        self.timer: threading.Timer | None = None
        self.labels: tuple = (None, {})  # (snapshot .aux times, labels)
        # client -> its unsaved preamble text: main.tex's preamble, or a whole class, package or file the preamble
        # reads. Previews of that client use a process started from it until the file on disk says the same.
        self.unsaved: dict[str, dict[str, str]] = {}

    def _spare(self, settings: dict, key: str, overrides: dict[str, str] | None = None) -> None:
        """(lock held) Start waiting processes up to the pool size, stopped again after IDLE seconds unused."""
        while len(self.ready) < self.pool:
            self.ready.append(Proc(self.main_tex, settings, key, overrides))
        if self.timer:
            self.timer.cancel()
        self.timer = threading.Timer(IDLE, self.stop_ready)
        self.timer.daemon = True
        self.timer.start()

    def stop_ready(self) -> None:
        with self.lock:
            old, self.ready, self.pool = self.ready, [], 1
        for proc in old:
            proc.stop()

    def warm_up(self) -> None:
        """Start a waiting process now if none matches the current preamble (after a full build)."""
        settings = build.read_settings(self.main_tex)
        if settings["engine"] not in ENGINES:
            return
        key = preamble_key(self.main_tex, settings)
        with self.lock:
            old = [proc for proc in self.ready if proc.key != key or not proc.alive()]
            self.ready = [proc for proc in self.ready if proc not in old]
            self._spare(settings, key)
        for proc in old:
            proc.stop()

    # --- what to typeset ---------------------------------------------------------------------------------------

    def preamble_edit(self, client: str, rel: str, text: str | None) -> bool | None:
        """
        None if rel is not part of the preamble: main.tex up to \\begin{document}, a file it reads there, a class or
        package. Else whether the preamble differs from the one the full build used, after keeping the client's
        unsaved preamble text (see overrides()), so its previews start a process from that text meanwhile.
        """
        main_tex, doc = self.main_tex, self.main_tex.parent
        path = doc / rel
        mine = self.unsaved.setdefault(client, {})
        if rel == main_tex.name:
            disk = main_tex.read_text(encoding="utf-8", errors="replace")
            new = preamble_of(disk if text is None else text)
            try:
                built = (root_for(main_tex) / "aux" / SOURCE).read_text(encoding="utf-8", errors="replace")
            except OSError:
                built = disk
            mine[rel] = new  # main.tex: only its preamble; the body stays the file's (or the region's overlay)
            return new != preamble_of(built)
        if path.suffix in PREAMBLE_DEPS or path in accel.preamble_files(main_tex):
            if text is not None:
                mine[rel] = text
            else:
                mine.pop(rel, None)
            return True
        return None

    def overrides(self, client: str) -> dict[str, str]:
        """The client's unsaved preamble files that still differ from the disk, as the text a process reads."""
        doc, kept = self.main_tex.parent, {}
        for rel, text in list(self.unsaved.get(client, {}).items()):
            try:
                disk = (doc / rel).read_text(encoding="utf-8", errors="replace")
            except OSError:
                disk = None
            if rel == self.main_tex.name and disk is not None:
                if text != preamble_of(disk):
                    kept[rel] = text + disk[len(preamble_of(disk)):]
            elif disk is not None and text != disk:
                kept[rel] = text
        self.unsaved[client] = {rel: self.unsaved[client][rel] for rel in kept}
        return kept

    def chapter_job(self, entries: list[dict], rel: str, text: str | None, client: str, reuse: bool = True) -> dict:
        """The --focus job for the chapter holding rel; reuse: the full build's figures may stand in (same preamble)."""
        target = accel.top_unit(entries, rel)
        inputs = []
        if target is None:
            try:
                inputs = (root_for(self.main_tex) / "aux" / INPUTS).read_text(encoding="utf-8").splitlines()
            except OSError:
                pass
            target = self.targets.get(client) if rel in inputs else None  # Read by a package of that chapter.
        if target is None:
            raise PreviewError(f"{rel} is not read by main.tex with \\input or \\include, so it has no chapter.")
        keep, first = accel.focus_selection(entries, target)
        entry, doc = entries[first], self.main_tex.parent
        typed = text if text is not None and entry["path"] == accel.normalize(rel) else None
        bump = accel.next_page(entry, doc, typed)
        tex = accel.focus_tex(entries, keep, first, bump)
        mine = [e for e in entries if e["path"] == accel.normalize(rel)]
        names = {("include:" if e["kind"] == "i" else "") + tex_name(e["raw"]) for e in mine}
        names = names or ({rel} if inputs else set())
        overlays = [(name, rel, text) for name in sorted(names)] if text is not None else []
        figures = reuse and self.figures_unchanged(entries, keep, first, rel, text if overlays else None)
        if figures:
            tex += FIGURES_TEX % entries[first]["figs"]
        files = [doc / tex_name(entries[i]["raw"]) for i in sorted(keep)]
        # Pages of the full PDF the chapter covers (0-based), from the pages shipped when it started and ended.
        # Floats it left behind come out at the next page break (the preview prints them at its end): with the
        # next chapter, if one follows.
        first_page = last_page = None
        if "shipped" in entry and "shipped_end" in entry:
            first_page = entry["shipped"] + bump
            last_page = entry["shipped_end"] - 1 + (entry["pending_end"] & 1)
            after = next((e for e in entries[first + 1:] if e["depth"] <= entry["depth"]), None)
            if entry["pending_end"] & 2:
                last_page = after["shipped"] + accel.next_page(after, doc) - 1 if after and "shipped" in after \
                    else last_page + 1
        return {
            "tex": tex, "overlays": overlays, "target": target, "figures": figures, "files": files,
            "pages": {"start": dict(entry["counters"]).get("page"), "end": dict(entry.get("exit", [])).get("page"),
                      "first": first_page, "last": last_page},
        }

    def region_job(self, entries: list[dict], text: str) -> dict | None:
        """
        An edit of main.tex's body: the part between the chapters around it, typeset from the editor's text.
        None if main.tex is as the full build saw it; status "rebuild" if the edit adds, removes or changes an
        \\input or \\include (the input tree is the full build's to record).
        """
        try:
            built = (root_for(self.main_tex) / "aux" / SOURCE).read_text(encoding="utf-8", errors="replace")
        except OSError:
            return status("rebuild")
        typed = text
        text = preamble_of(built) + text[len(preamble_of(text)):]  # the preamble is the process's (compile())
        new, old = text.splitlines(), built.splitlines()
        a = 0
        while a < min(len(new), len(old)) and new[a] == old[a]:
            a += 1
        if a == len(new) == len(old):
            return None
        b_new, b_old = len(new), len(old)
        while b_new > a and b_old > a and new[b_new - 1] == old[b_old - 1]:
            b_new, b_old = b_new - 1, b_old - 1
        top = [e for e in entries if e["depth"] == 1]
        at_new, at_old = unit_lines(new, top), unit_lines(old, top)
        if at_new is None or at_old is None or any(a <= n < b_new for n in at_new) \
                or any(a <= n < b_old for n in at_old):
            return status("rebuild")
        before = [i for i, n in enumerate(at_new) if n < a]
        prev = top[before[-1]] if before else None
        nxt = top[len(before)] if len(before) < len(top) else None
        if (prev and "shipped_end" not in prev) or (nxt and "shipped" not in nxt):
            return status("rebuild")  # A map from before shipped pages were recorded: the next build has them.
        # The region starts on a new page if the page in progress held the chapter before it and its first
        # command breaks the page (accel.next_page); it ends where the next chapter started.
        region = "\n".join(new[at_new[len(before) - 1] + 1 if before else 0:at_new[len(before)] if nxt else len(new)])
        doc = self.main_tex.parent
        bump = accel.next_page({"kind": "n", "pending": prev["pending_end"]}, doc, region) if prev else 0
        first = prev["shipped_end"] + bump if prev else 0
        last = nxt["shipped"] + accel.next_page(nxt, doc) - 1 if nxt else None
        tex = REGION % {
            "prev": prev["raw"] if prev else "", "next": nxt["raw"] if nxt else "",
            "restore": "".join(rf"\pgff@c{{{c}}}{{{int(v) + bump if c == 'page' else v}}}"
                               for c, v in prev.get("exit", [])) if prev else "",
            "hide": r"\gdef\shipout{\setbox\pgff@box=}" if prev else "",
            "only": r"\AddToHook{begindocument/before}{\includeonly{}}" + "\n"
            if any(e["kind"] == "i" for e in entries) else "",
        }
        start = typed.find(r"\begin{document}")
        body = "\n" * typed.count("\n", 0, start) + typed[start + len(r"\begin{document}"):] + "\n\\end{document}\n"
        return {
            "tex": tex, "overlays": [(None, self.main_tex.name, body)], "target": self.main_tex.name,
            "figures": False, "files": [],
            "pages": {"start": None, "end": None, "first": first,
                      "last": None if last is None else max(last, first - 1)},  # first - 1: inserted pages
        }

    # --- typesetting -------------------------------------------------------------------------------------------

    def compile(self, client: str, rel: str, text: str | None, max_running: int = 2) -> dict:
        """
        Typeset the chapter that holds rel, with text (if given) in place of rel's saved content.
        Returns {ok, pdf (bytes or None), errors, log, seconds, warm, target, pages, cancelled, passes, status}.
        status is "preview" with a PDF; "preamble", "rebuild" or "same" say why there is none (see status()).
        A newer compile for the same client cancels this one. Raises PreviewError if there is nothing to do.
        """
        main_tex = self.main_tex
        settings = build.read_settings(main_tex)
        if settings["engine"] not in ENGINES:
            raise PreviewError(f"Live preview does not support {settings['engine']}; use Preview chapter.")
        sandbox.LOW_UNTIL = time.monotonic() + ACTIVE  # Full builds and figure jobs step back meanwhile.
        if not has_snapshot(main_tex):
            raise PreviewError("No full build with a recorded input tree yet.")
        edit = self.preamble_edit(client, rel, text)
        overrides = self.overrides(client)
        entries = accel.read_focusmap(root_for(main_tex) / "aux" / f"{main_tex.stem}.focusmap")
        job = None
        if rel == main_tex.name:
            job = self.region_job(entries, main_tex.read_text(encoding="utf-8", errors="replace")
                                  if text is None else text)
            if job is not None and "status" in job:
                job["saved"] = text is None or text == main_tex.read_text(encoding="utf-8", errors="replace")
                return job
            if job is None and not edit:
                return status("same")
        elif not edit:
            job = self.chapter_job(entries, rel, text, client, not overrides)
            self.targets[client] = job["target"]
        if job is None:
            # The preamble changed: typeset the chapter the client looked at last (else the first one) again, on a
            # process started from the new preamble; its pages replace the old ones when they arrive.
            target = self.targets.get(client) or next((e["path"] for e in entries if e["depth"] == 1), None)
            if target is None:
                return status("preamble", saved=not overrides)
            job = self.chapter_job(entries, target, None, client, False)
        job["preamble"] = overrides
        key = preamble_key(main_tex, settings, overrides)
        began, started = time.monotonic(), time.time()
        with self.lock:
            number = self.latest[client] = self.latest.get(client, 0) + 1

        inject: dict[str, str] = {}  # label lines the next pass reads after the snapshot's
        bbls: dict[str, bytes] = {}  # .bbl files BibTeX rewrote for new citations
        known = self.snapshot_labels()
        result: dict = {}
        warm_first = True
        for passes in range(1, PASSES + 1):
            run = self._pass(client, number, settings, key, job, bbls, "\n".join(inject.values()), max_running)
            if run is None:
                return {"ok": False, "cancelled": True, "pdf": None, "errors": [], "log": "", "warm": warm_first}
            result, proc, found = run
            warm_first = warm_first and result["warm"]
            try:
                if not result["ok"] or passes == PASSES:
                    break
                # Labels this pass wrote that differ from what it read (the snapshot's, or an earlier pass's).
                changed = {name: line for name, line in found.items()
                           if shown(inject.get(name, known.get(name)) or "") != shown(line)}
                again = False
                if changed:
                    typed = self.job_text(job)
                    again = any(referenced(name, typed) for name in changed)
                    inject.update(changed)
                missing = set(UNDEFINED_CITE.findall(result["log"])) - set(inject)
                if missing and not bbls:
                    new = self.bibtex(proc, missing) or self.biber(proc, missing)
                    if new:
                        bbls.update(new)
                        again = True
                if not again:
                    break
            finally:
                proc.cleanup()
        if passes > 1 and self.pool < 2:
            with self.lock:
                self.pool = 2
                try:
                    self._spare(settings, key, overrides)
                except OSError:
                    pass
        result.update(warm=warm_first, passes=passes, seconds=round(time.monotonic() - began, 3), t=started,
                      target=job["target"], pages=job["pages"], overlay=bool(job["overlays"]),
                      figures=job["figures"], status="preview", preamble=bool(edit or overrides))
        return result

    def _pass(self, client: str, number: int, settings: dict, key: str, job: dict, bbls: dict, labels_tex: str,
              max_running: int):
        """One TeX run of job on a waiting process. None if a newer compile of the client took over."""
        with self.lock:
            if self.latest.get(client) != number:
                return None
            older = self.running.pop(client, None)
            if older:
                older.stop()
            if len(self.running) >= max_running:
                raise PreviewError("Too many previews of this document at once; try again in a moment.")
            while self.ready and (self.ready[0].key != key or not self.ready[0].alive()):
                self.ready.pop(0).stop()
            proc, warm = (self.ready.pop(0), True) if self.ready \
                else (Proc(self.main_tex, settings, key, job["preamble"]), False)
            self.running[client] = proc
            try:
                proc.trigger(job, bbls, labels_tex)
            except OSError:  # Died before the trigger (a preamble error; its log says why), or a file is missing.
                proc.popen.kill()
            try:
                self._spare(settings, key, job["preamble"])  # The next one loads its preamble while this one typesets.
            except OSError:
                pass  # The next compile starts one itself and reports why it cannot.

        sandbox.preview_running(1)  # New figure jobs of a full build wait meanwhile.
        try:
            proc.popen.wait(TIMEOUT)
            timed_out = False
        except subprocess.TimeoutExpired:
            proc.popen.kill()
            proc.popen.wait()
            timed_out = True
        finally:
            sandbox.preview_running(-1)
        with self.lock:
            if self.running.get(client) is proc:
                del self.running[client]
        if proc.cancelled:
            return None
        sandbox.scrub(proc.dir)
        try:
            proc.out.seek(0)
            console = proc.out.read()
            stem = self.main_tex.stem
            log_file = proc.dir / f"{stem}.log"
            log = log_file.read_text(encoding="utf-8", errors="replace") if log_file.exists() else ""
            pdf_file = proc.dir / f"{stem}.pdf"
            ok = not timed_out and proc.popen.returncode == 0 and pdf_file.exists()
            pdf = pdf_file.read_bytes() if ok else None
            found = labels(path.read_text(encoding="utf-8", errors="replace")
                           for path in sorted(proc.dir.rglob("*.aux")) if not path.name.startswith("pgfw"))
            if ok:
                self.keep_sync(client, proc, job)
        except BaseException:
            proc.cleanup()
            raise
        names = {f"{OVERLAY}{i}": rel for i, (_, rel, _) in enumerate(job["overlays"])}
        names.update({f"pgfwarm-pre{i}": rel for i, rel in enumerate(sorted(job["preamble"]))})
        errors = build.parse_latex_errors(console)
        for item in errors:  # An overlay stands for the file being typed.
            item["file"] = names.get(Path(item["file"]).stem, item["file"])
        target = job["overlays"][0][1] if job["overlays"] else job["target"]
        if timed_out:
            errors.insert(0, {"file": target, "line": 0, "message": f"Preview timed out after {TIMEOUT}s.",
                              "hint": None})
        elif not ok and not errors:
            errors.append({"file": target, "line": 0, "message": "The preview failed; see its log.", "hint": None})
        return {"ok": ok, "cancelled": False, "pdf": pdf, "errors": errors, "log": log, "warm": warm}, proc, found

    def snapshot_labels(self) -> dict[str, str]:
        aux = root_for(self.main_tex) / "aux"
        files = sorted(aux.rglob("*.aux"))
        stamp = [(path.name, path.stat().st_mtime_ns) for path in files]
        if self.labels[0] != stamp:
            self.labels = (stamp, labels(path.read_text(encoding="utf-8", errors="replace") for path in files))
        return self.labels[1]

    def job_text(self, job: dict) -> str:
        """The text the preview typeset: its overlays and the kept files from disk."""
        parts = [text for _, _, text in job["overlays"] if text]
        typed = {self.main_tex.parent / rel for _, rel, _ in job["overlays"]}
        for path in job["files"]:
            if path not in typed:
                try:
                    parts.append(path.read_text(encoding="utf-8", errors="replace"))
                except OSError:
                    pass
        return "\n".join(parts)

    def bibtex(self, proc: Proc, missing: set[str]) -> dict[str, bytes]:
        """
        New .bbl files for citations the snapshot's bibliography lacks: BibTeX (sandboxed, in the finished
        process's directory) on the snapshot's citations plus this run's, for every .aux that names a .bib.
        Only bibliographies that now hold one of the missing keys are returned. biblatex's biber: see biber().
        """
        aux_dir = root_for(self.main_tex) / "aux"
        found: dict[str, bytes] = {}
        for i, path in enumerate(sorted(proc.dir.rglob("*.aux"))):
            text = path.read_text(encoding="utf-8", errors="replace")
            data = re.search(r"^\\bibdata\{([^{}]*)\}", text, re.M)
            cited = {key.strip() for keys in CITATION.findall(text) for key in keys.split(",")}
            if not data or not cited & missing:
                continue
            name = path.relative_to(proc.dir).as_posix()
            try:
                before = (aux_dir / name).read_text(encoding="utf-8", errors="replace")
            except OSError:
                before = ""
            lines = list(dict.fromkeys(m.group(0) for m in CITATION.finditer(before + "\n" + text)))
            style = re.search(r"^\\bibstyle\{[^{}]*\}", text, re.M)
            lines += [data.group(0), *([style.group(0)] if style else [])]
            for bib in data.group(1).split(","):  # BibTeX runs in the process directory: copy what it reads.
                self.copy_bib(proc, bib.strip() if bib.strip().endswith(".bib") else bib.strip() + ".bib")
            job = f"pgfwarm-bib{i}"
            (proc.dir / f"{job}.aux").write_text("\n".join(lines) + "\n", encoding="utf-8")
            try:
                spec = sandbox.spawn(["bibtex", job], proc.dir, proc.root, cpu=60, work=proc.dir, urgent=True)
                subprocess.run(**spec, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                               stderr=subprocess.DEVNULL, timeout=60)
            except (OSError, subprocess.TimeoutExpired):
                continue
            finally:
                sandbox.scrub(proc.dir)
            bbl = proc.dir / f"{job}.bbl"
            if bbl.is_file():
                data_bbl = bbl.read_bytes()
                if any(b"{" + key.encode() + b"}" in data_bbl for key in missing):
                    found[path.with_suffix(".bbl").relative_to(proc.dir).as_posix()] = data_bbl
        return found

    def copy_bib(self, proc: Proc, bib: str) -> bool:
        """Copy the document's file bib into the process directory, where BibTeX and biber run; False if not one."""
        doc = self.main_tex.parent
        source = (doc / bib).resolve()
        if ".." in Path(bib).parts or Path(bib).is_absolute() or not source.is_file() \
                or doc.resolve() not in source.parents:
            return False
        (proc.dir / bib).parent.mkdir(parents=True, exist_ok=True)
        shutil.copy(source, proc.dir / bib)
        return True

    def biber(self, proc: Proc, missing: set[str]) -> dict[str, bytes]:
        """
        A new .bbl for citations the snapshot's biblatex bibliography lacks: biber (sandboxed, in the finished
        process's directory) on the .bcf this run wrote, each refsection holding the snapshot's keys first (their
        order, and so sorting=none numbers, stays) and then this run's. Only local data files of the document are
        read (copies; remote and other sources are left out). Only runs when the run wrote a .bcf, which biblatex
        does only with backend=biber; with backend=bibtex its \\bibdata goes through bibtex().
        """
        stem = self.main_tex.stem
        try:
            bcf = (proc.dir / f"{stem}.bcf").read_text(encoding="utf-8", errors="replace")
        except OSError:
            return {}
        if not missing & set(BCF_KEY.findall(bcf)):
            return {}
        try:
            before = (root_for(self.main_tex) / "aux" / f"{stem}.bcf").read_text(encoding="utf-8", errors="replace")
        except OSError:
            before = ""
        earlier = {m.group(2): BCF_KEY.findall(m.group(3)) for m in BCF_SECTION.finditer(before)}

        def section(m: re.Match) -> str:
            keys = dict.fromkeys([*earlier.get(m.group(2), []), *BCF_KEY.findall(m.group(3))])
            return m.group(1) + "\n" + "".join(f'    <bcf:citekey order="{n}" intorder="1">{key}</bcf:citekey>\n'
                                                for n, key in enumerate(keys, 1)) + "  " + m.group(4)

        def source(m: re.Match) -> str:
            return m.group(0) if 'type="file"' in m.group(1) and self.copy_bib(proc, m.group(2)) else ""

        job = "pgfwarm-bib"
        (proc.dir / f"{job}.bcf").write_text(BCF_SOURCE.sub(source, BCF_SECTION.sub(section, bcf)), encoding="utf-8")
        try:
            spec = sandbox.spawn(["biber", job], proc.dir, proc.root, cpu=60, work=proc.dir, urgent=True)
            subprocess.run(**spec, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                           stderr=subprocess.DEVNULL, timeout=60)
        except (OSError, subprocess.TimeoutExpired):
            return {}
        finally:
            sandbox.scrub(proc.dir)
        bbl = proc.dir / f"{job}.bbl"
        data = bbl.read_bytes() if bbl.is_file() else b""
        return {f"{stem}.bbl": data} if any(b"{" + key.encode() + b"}" in data for key in missing) else {}

    # --- SyncTeX -----------------------------------------------------------------------------------------------

    def keep_sync(self, client: str, proc: Proc, job: dict) -> None:
        """Keep the preview's PDF and SyncTeX file for double-click (until SYNC_KEEP newer previews replace it)."""
        sync = proc.dir / f"{self.main_tex.stem}.synctex.gz"
        if not sync.is_file():
            return
        base = sync_base(self.main_tex, client)
        base.parent.mkdir(exist_ok=True)
        shutil.copy(proc.dir / f"{self.main_tex.stem}.pdf", base.with_suffix(".pdf"))
        shutil.copy(sync, base.with_suffix(".synctex.gz"))
        names = {f"{OVERLAY}{i}.tex": rel for i, (_, rel, _) in enumerate(job["overlays"])}
        base.with_suffix(".json").write_text(json.dumps(names), encoding="utf-8")
        for old in sorted(base.parent.glob("*.json"), key=lambda p: p.stat().st_mtime)[:-SYNC_KEEP]:
            for suffix in (".json", ".pdf", ".synctex.gz"):
                old.with_suffix(suffix).unlink(missing_ok=True)

    def figures_unchanged(self, entries: list[dict], keep: set[int], first: int, rel: str, overlay: str | None) -> bool:
        """True if the full build externalized its figures and no picture of the kept files changed since."""
        aux = root_for(self.main_tex) / "aux"
        if entries[first]["kind"] != "n" or entries[first].get("figs", -1) < 0 or not (aux / "tikz").is_dir():
            return False
        try:
            before = json.loads((aux / "figtext.json").read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return False
        for i in keep:
            name = entries[i]["path"] + ".tex"
            if overlay is not None and name == accel.normalize(rel) + ".tex":
                text = overlay
            else:
                try:
                    text = (self.main_tex.parent / name).read_text(encoding="utf-8", errors="replace")
                except OSError:
                    return False
            figure_text = "\n".join(accel.FIGURE_TEXT.findall(text))
            if hashlib.sha1(figure_text.encode()).hexdigest() != before.get(name, accel.NO_FIGURES):
                return False
        return True

    def shutdown(self) -> None:
        with self.lock:
            procs = [*self.ready, *self.running.values()]
            self.ready, self.running = [], {}
            if self.timer:
                self.timer.cancel()
        for proc in procs:
            proc.stop()


def unit_lines(lines: list[str], top: list[dict]) -> list[int] | None:
    """Line of main.tex's body where each chapter (depth-1 entry, in order) is read; None if one is missing."""
    found, at = [], next((i for i, line in enumerate(lines) if r"\begin{document}" in line), None)
    if at is None:
        return None
    for entry in top:
        needle = "{" + entry["raw"] + "}"
        while at < len(lines) and needle not in accel.COMMENT.sub("", lines[at]):
            at += 1
        if at == len(lines):
            return None
        found.append(at)
    return found


def sync_base(main_tex: Path, client: str) -> Path:
    """Where the newest preview of a client keeps its PDF and SyncTeX file (the client id is never a path)."""
    return root_for(main_tex) / "sync" / hashlib.sha1(client.encode()).hexdigest()[:20]


def sync_files(main_tex: Path, client: str) -> tuple[Path, dict[str, str]] | None:
    """(PDF of the client's newest preview, overlay file name -> the document file it stands for), or None."""
    base = sync_base(main_tex, client)
    try:
        names = json.loads(base.with_suffix(".json").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    return (base.with_suffix(".pdf"), names) if base.with_suffix(".synctex.gz").is_file() else None


WARM: dict[Path, Warm] = {}
WARM_LOCK = threading.Lock()


def warm_for(main_tex: Path) -> Warm:
    with WARM_LOCK:
        if main_tex not in WARM:
            for old in root_for(main_tex).glob("p*"):  # Directories of processes from an earlier server run.
                shutil.rmtree(old, ignore_errors=True)
            WARM[main_tex] = Warm(main_tex)
        return WARM[main_tex]


def shutdown() -> None:
    with WARM_LOCK:
        warms = list(WARM.values())
        WARM.clear()
    for warm in warms:
        warm.shutdown()
