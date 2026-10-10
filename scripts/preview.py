"""
Warm preview compiler for the editor's live preview (imported by serve.py, standard library only).

A chapter preview (build.build_focus) spends most of its time loading the preamble: about 1.1 s of
1.8 s on bench/sample-report. Here a TeX process is started ahead of time on the document with
\\begin{document} redefined to wait for one line on stdin, so it loads the class and packages and then
blocks. When a preview is wanted, the --focus selection (accel.focus_tex) and the editor's unsaved text
of the file being typed (an "overlay" that replaces that \\input) are written next to it, the line is
sent, and TeX typesets only the focused chapter; the next process is started at the same moment.

Each process is used once (TeX cannot go back to its preamble state), runs through sandbox.spawn like
every LaTeX run, and is replaced when anything that shaped its preamble changes: main.tex's preamble,
the files it reads, local .cls/.sty files, build settings, the engine, or the build environment
(sharing restrictions, sandbox). The full build's .aux/.bbl/.toc and its \\input tree are read from a
snapshot taken after each successful full build, never from a build that is still writing them.
"""

from __future__ import annotations

import hashlib
import itertools
import json
import os
import queue
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
SNAPSHOT = {".aux", ".bbl", ".toc", ".lof", ".lot", ".out", ".focusmap"}
FIGURE_FILES = {".pdf", ".dpth"}  # of the full build's externalized TikZ figures (build_dir/tikz/)
# Files that can change what the preamble does without changing main.tex (besides files it \input's).
PREAMBLE_DEPS = {".cls", ".sty", ".def", ".cfg", ".clo", ".fd", ".ldf", ".toml", ".latexmkrc"}
ENV_KEYS = ("shell_escape", "openin_any", "openout_any", sandbox.VARIABLE)
IDLE = 600.0  # seconds a waiting process is kept before it is stopped
TIMEOUT = 120  # seconds one preview may take
TRIGGER = "pgfwarm-go"  # file names in the process's own directory (its -output-directory)
OVERLAY = "pgfwarm-ov"

# Wait at the very start of \begin{document}, before the .aux files are read. -interaction=nonstopmode
# refuses to read the terminal, so only the read itself runs in errorstopmode. An empty line ends the run.
WAIT = r"""\makeatletter
\let\pgfw@document\document
\def\document{\errorstopmode\endlinechar=-1 \read-1 to\pgfw@line\endlinechar=13 \nonstopmode
\ifx\pgfw@line\@empty\expandafter\@@end\fi\input{\pgfw@line}\pgfw@document}
\makeatother
"""
# Appended to the focus selection: the file being typed is read from the overlay instead.
OVERLAY_TEX = r"""\makeatletter
\expandafter\def\csname pgfw@ov@%s\endcsname{}
\AddToHook{begindocument/end}{\let\pgfw@in\pgff@oldinput
\def\pgff@oldinput#1{\ifcsname pgfw@ov@#1\endcsname\expandafter\@firstoftwo\else\expandafter\@secondoftwo\fi
{\pgfw@in{%s}}{\pgfw@in{#1}}}}
\makeatother
"""


# The full build's TikZ figures instead of typesetting them again (chapter 3 of the bench report: 2.3 s to 0.8 s).
# Pictures are numbered in document order, so the count the full build recorded before the chapter comes back with
# the other counters. Only used when no picture of the chapter changed since that build (figtext.json).
FIGURES_TEX = r"""\makeatletter
\AddToHook{begindocument/before}{\ifcsname ver@tikz.sty\endcsname
\usetikzlibrary{external}\tikzexternalize[prefix=tikz/,mode=graphics if exists]
\g@addto@macro\pgff@restore{\expandafter\xdef\csname c@tikzext@no@\tikzexternal@realjob-figure\endcsname{%d}}\fi}
\makeatother
"""


class PreviewError(Exception):
    """Nothing to preview (no chapter, no full build yet, unsupported engine); the message says why."""


def tex_ref(path: Path, doc_dir: Path) -> str:
    """How TeX in doc_dir names a file of a process directory (accel.write_inject's rule)."""
    if os.environ.get("openin_any") == "p":
        # Paranoid reads refuse "..", but TeX Live also looks in the -output-directory (only with the extension).
        return path.name
    return accel.tex_path(path, doc_dir)


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
    shutil.rmtree(target, ignore_errors=True)
    fresh.rename(target)


def has_snapshot(main_tex: Path) -> bool:
    return (root_for(main_tex) / "aux" / f"{main_tex.stem}.focusmap").exists()


def preamble_key(main_tex: Path, settings: dict) -> str:
    """What a waiting process depends on. A different key means it must be replaced."""
    digest = hashlib.sha1(repr((
        settings["engine"], bool(settings["shell_escape"]), [os.environ.get(k) for k in ENV_KEYS],
    )).encode())
    digest.update(main_tex.read_text(encoding="utf-8", errors="replace").split(r"\begin{document}")[0].encode())
    for path in accel.preamble_files(main_tex):
        digest.update(path.read_bytes())
    for path in sorted(main_tex.parent.rglob("*")):
        if path.suffix in PREAMBLE_DEPS and path.is_file():
            stat = path.stat()
            digest.update(f"{path.relative_to(main_tex.parent).as_posix()}:{stat.st_mtime_ns}:{stat.st_size}".encode())
    return digest.hexdigest()


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

    def __init__(self, main_tex: Path, settings: dict, key: str) -> None:
        self.key, self.main_tex = key, main_tex
        self.root = root_for(main_tex)
        self.dir = self.root / f"p{os.getpid()}-{next(Proc.ids)}"
        shutil.rmtree(self.dir, ignore_errors=True)
        self.dir.mkdir(parents=True)
        self.cancelled = False
        command = [
            settings["engine"], "-interaction=nonstopmode", "-halt-on-error", "-file-line-error",
            f"-output-directory={self.dir}", f"-jobname={main_tex.stem}",
            *(["-shell-escape"] if settings["shell_escape"] else []),
            accel.write_inject(self.dir, main_tex.parent, "pgfwarm-wait.tex", WAIT) + f"\\input{{{main_tex.name}}}",
        ]
        self.out = open(self.dir / "console.txt", "w+", encoding="utf-8", errors="replace")
        try:
            spec = sandbox.spawn(command, main_tex.parent, self.root, cpu=TIMEOUT, work=self.dir)
            self.popen = spawn_here(lambda: subprocess.Popen(
                **spec, stdin=subprocess.PIPE, stdout=self.out, stderr=subprocess.STDOUT,
            ))
        except BaseException:
            self.out.close()
            shutil.rmtree(self.dir, ignore_errors=True)
            raise

    def alive(self) -> bool:
        return self.popen.poll() is None

    def trigger(self, focus_tex: str, overlay: str | None = None, raw: str = "") -> None:
        """Start typesetting: focus_tex selects the chapter; overlay replaces the \\input named raw."""
        if overlay is not None:
            (self.dir / f"{OVERLAY}.tex").write_text(overlay, encoding="utf-8")
            focus_tex += OVERLAY_TEX % (raw, tex_ref(self.dir / f"{OVERLAY}.tex", self.main_tex.parent))
        (self.dir / f"{TRIGGER}.tex").write_text(focus_tex, encoding="utf-8")
        with SNAPSHOT_LOCK:
            self._copy_snapshot()
        self.popen.stdin.write(tex_ref(self.dir / f"{TRIGGER}.tex", self.main_tex.parent).encode() + b"\n")
        self.popen.stdin.close()

    def _copy_snapshot(self) -> None:
        for path in (self.root / "aux").iterdir():
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


class Warm:
    """The waiting process of one document, and the previews running from it."""

    def __init__(self, main_tex: Path) -> None:
        self.main_tex = main_tex
        self.lock = threading.Lock()
        self.ready: Proc | None = None
        self.running: dict[str, Proc] = {}  # client -> its preview in flight
        self.timer: threading.Timer | None = None

    def _spare(self, settings: dict, key: str) -> None:
        """(lock held) Start the next waiting process, stopped again after IDLE seconds unused."""
        self.ready = Proc(self.main_tex, settings, key)
        if self.timer:
            self.timer.cancel()
        self.timer = threading.Timer(IDLE, self.stop_ready)
        self.timer.daemon = True
        self.timer.start()

    def stop_ready(self) -> None:
        with self.lock:
            old, self.ready = self.ready, None
        if old:
            old.stop()

    def warm_up(self) -> None:
        """Start a waiting process now if none matches the current preamble (after a full build)."""
        settings = build.read_settings(self.main_tex)
        if settings["engine"] not in ENGINES:
            return
        key = preamble_key(self.main_tex, settings)
        with self.lock:
            if self.ready and self.ready.key == key and self.ready.alive():
                return
            old = self.ready
            self._spare(settings, key)
        if old:
            old.stop()

    def compile(self, client: str, rel: str, text: str | None, max_running: int = 2) -> dict:
        """
        Typeset the chapter that holds rel, with text (if given) in place of rel's saved content.
        Returns {ok, pdf (bytes or None), errors, log, seconds, warm, target, pages, cancelled}.
        A newer compile for the same client cancels this one. Raises PreviewError if there is nothing to do.
        """
        main_tex = self.main_tex
        settings = build.read_settings(main_tex)
        if settings["engine"] not in ENGINES:
            raise PreviewError(f"Live preview does not support {settings['engine']}; use Preview chapter.")
        if not has_snapshot(main_tex):
            raise PreviewError("No full build with a recorded input tree yet.")
        entries = accel.read_focusmap(root_for(main_tex) / "aux" / f"{main_tex.stem}.focusmap")
        target = accel.top_unit(entries, rel)
        if target is None:
            raise PreviewError(f"{rel} is not read by main.tex with \\input or \\include, so it has no chapter.")
        keep, first = accel.focus_selection(entries, target)
        focus_tex = accel.focus_tex(entries, keep, first)
        mine = next((e for e in entries if e["path"] == accel.normalize(rel) and e["kind"] == "n"), None)
        overlay = text if text is not None and mine is not None else None
        figures = self.figures_unchanged(entries, keep, first, rel, overlay)
        if figures:
            focus_tex += FIGURES_TEX % entries[first]["figs"]
        pages = {"start": dict(entries[first]["counters"]).get("page"),
                 "end": dict(entries[first].get("exit", [])).get("page")}
        key = preamble_key(main_tex, settings)
        began, started = time.monotonic(), time.time()

        with self.lock:
            older = self.running.pop(client, None)
            if older:
                older.stop()
            if len(self.running) >= max_running:
                raise PreviewError("Too many previews of this document at once; try again in a moment.")
            proc, warm = self.ready, True
            self.ready = None
            if proc is None or proc.key != key or not proc.alive():
                if proc:
                    proc.stop()
                proc, warm = Proc(main_tex, settings, key), False
            self.running[client] = proc
            try:
                proc.trigger(focus_tex, overlay, mine["raw"] if mine else "")
            except OSError:  # Died before the trigger (a preamble error; its log says why), or a file is missing.
                proc.popen.kill()
            try:
                self._spare(settings, key)  # The next one loads its preamble while this one typesets.
            except OSError:
                self.ready = None  # The next compile starts one itself and reports why it cannot.

        try:
            proc.popen.wait(TIMEOUT)
            timed_out = False
        except subprocess.TimeoutExpired:
            proc.popen.kill()
            proc.popen.wait()
            timed_out = True
        with self.lock:
            if self.running.get(client) is proc:
                del self.running[client]
        if proc.cancelled:
            return {"ok": False, "cancelled": True, "pdf": None, "errors": [], "log": "", "warm": warm}
        sandbox.scrub(proc.dir)
        try:
            proc.out.seek(0)
            console = proc.out.read()
            log_file = proc.dir / f"{main_tex.stem}.log"
            log = log_file.read_text(encoding="utf-8", errors="replace") if log_file.exists() else ""
            pdf_file = proc.dir / f"{main_tex.stem}.pdf"
            ok = not timed_out and proc.popen.returncode == 0 and pdf_file.exists()
            pdf = pdf_file.read_bytes() if ok else None
        finally:
            proc.cleanup()
        errors = build.parse_latex_errors(console)
        for item in errors:  # The overlay stands for the file being typed.
            if Path(item["file"]).stem == OVERLAY:
                item["file"] = rel
        if timed_out:
            errors.insert(0, {"file": rel, "line": 0, "message": f"Preview timed out after {TIMEOUT}s.", "hint": None})
        elif not ok and not errors:
            errors.append({"file": rel, "line": 0, "message": "The preview failed; see its log.", "hint": None})
        return {
            "ok": ok, "cancelled": False, "pdf": pdf, "errors": errors, "log": log, "warm": warm,
            "seconds": round(time.monotonic() - began, 3), "target": target, "pages": pages, "t": started,
            "overlay": overlay is not None, "figures": figures,
        }

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
            procs = [p for p in (self.ready, *self.running.values()) if p]
            self.ready, self.running = None, {}
            if self.timer:
                self.timer.cancel()
        for proc in procs:
            proc.stop()


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
