"""scripts/preview.py: the warm preview compiler's restart rules, fallbacks, cancellation and output."""

from __future__ import annotations

import contextlib
import io
import os
import shutil
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest import mock

import _support  # noqa: F401 - puts scripts/ on sys.path
import preview
from _support import build, fake_repo, write_doc

MAIN = r"""\documentclass{article}
\ifdefined\pdfobjcompresslevel\pdfcompresslevel=0 \pdfobjcompresslevel=0 \fi
\usepackage{mystyle}
\begin{document}
Front.
\input{ch/one}
\input{ch/two}
\end{document}
"""
LATEX = shutil.which("latexmk") and shutil.which("pdflatex")
SETTINGS = {"engine": "pdflatex", "shell_escape": False}


def text(pdf: bytes) -> str:
    """The words of a PDF (pdftotext when installed; else the raw uncompressed bytes, words unkerned)."""
    if shutil.which("pdftotext"):
        with tempfile.TemporaryDirectory() as tmp:
            Path(tmp, "x.pdf").write_bytes(pdf)
            return subprocess.run(["pdftotext", str(Path(tmp, "x.pdf")), "-"], capture_output=True, text=True).stdout
    return pdf.decode("latin-1")


def doc(root: Path, build_toml: str | None = None) -> Path:
    main = write_doc(root, "doc", MAIN, build_toml)
    (main.parent / "ch").mkdir()
    (main.parent / "ch" / "one.tex").write_text("\\section{One}\\label{one}Chapter one, see \\ref{two}.\n")
    (main.parent / "ch" / "two.tex").write_text("\\section{Two}\\label{two}Chapter two.\n")
    (main.parent / "mystyle.sty").write_text("\\newcommand\\mine{M}\n")
    return main


class KeyTests(unittest.TestCase):
    """What restarts the waiting process: anything that shaped its preamble, and nothing else."""

    def test_preamble_files_settings_and_environment_change_the_key(self):
        with fake_repo() as root:
            main = doc(root)
            key = preview.preamble_key(main, SETTINGS)
            (main.parent / "ch" / "one.tex").write_text("edited body\n")
            self.assertEqual(preview.preamble_key(main, SETTINGS), key)  # A chapter is read after the trigger.
            self.assertNotEqual(preview.preamble_key(main, {**SETTINGS, "engine": "xelatex"}), key)
            self.assertNotEqual(preview.preamble_key(main, {**SETTINGS, "shell_escape": True}), key)
            for name, value in (("openin_any", "p"), ("LATEX_SANDBOX", "bwrap")):  # Sharing or the sandbox turned on.
                with mock.patch.dict(os.environ, {name: value}):
                    on = preview.preamble_key(main, SETTINGS)
                with mock.patch.dict(os.environ):
                    os.environ.pop(name, None)
                    self.assertNotEqual(preview.preamble_key(main, SETTINGS), on)
            sty = main.parent / "mystyle.sty"
            sty.write_text("\\newcommand\\mine{N}\n")
            os.utime(sty, ns=(1, 1))
            changed = preview.preamble_key(main, SETTINGS)
            self.assertNotEqual(changed, key)
            main.write_text(MAIN.replace("\\begin{document}", "\\usepackage{xcolor}\n\\begin{document}"))
            self.assertNotEqual(preview.preamble_key(main, SETTINGS), changed)


class RefusalTests(unittest.TestCase):
    def test_unsupported_engine_and_missing_snapshot_fall_back_with_a_reason(self):
        with fake_repo() as root:
            main = doc(root)
            warm = preview.Warm(main)
            with mock.patch.object(build, "read_settings", return_value={**SETTINGS, "engine": "latex"}), \
                    self.assertRaisesRegex(preview.PreviewError, "does not support latex"):
                warm.compile("c", "ch/one.tex", None)
            with mock.patch.object(build, "read_settings", return_value=SETTINGS), \
                    self.assertRaisesRegex(preview.PreviewError, "No full build"):
                warm.compile("c", "ch/one.tex", None)

    def test_warm_up_does_nothing_for_an_unsupported_engine(self):
        with fake_repo() as root:
            warm = preview.Warm(doc(root))
            with mock.patch.object(build, "read_settings", return_value={**SETTINGS, "engine": "latex"}), \
                    mock.patch.object(preview, "Proc") as proc:
                warm.warm_up()
            proc.assert_not_called()

    def test_processes_start_through_the_sandbox_with_only_their_directory_writable(self):
        with fake_repo() as root:
            main = doc(root)
            calls = []

            def spawn(cmd, cwd, build_dir, env=None, cpu=0, work=None):
                calls.append((cwd, build_dir, work))
                return {"args": [sys.executable, "-c", "pass"], "cwd": cwd, "env": None}

            with mock.patch.object(preview.sandbox, "spawn", side_effect=spawn):
                proc = preview.Proc(main, SETTINGS, "k")
                proc.popen.wait(10)
                proc.cleanup()
            self.assertEqual(calls, [(main.parent, preview.root_for(main), proc.dir)])
            self.assertFalse(proc.dir.exists())


@unittest.skipUnless(LATEX, "needs latexmk and pdflatex")
class WarmTests(unittest.TestCase):
    def setUp(self):
        stack = contextlib.ExitStack()
        self.addCleanup(stack.close)
        self.root = stack.enter_context(fake_repo())
        stack.enter_context(contextlib.redirect_stdout(io.StringIO()))
        self.main = doc(self.root)
        entry, _ = build.build_safely(self.main, shutil.which("latexmk"), False, False, record=True)
        self.assertTrue(entry["ok"], entry)
        preview.snapshot(self.main)
        self.warm = preview.Warm(self.main)
        self.addCleanup(self.warm.shutdown)

    def wait_ready(self):
        self.warm.warm_up()
        deadline = time.monotonic() + 20
        while time.monotonic() < deadline and not (self.warm.ready.dir / f"{self.main.stem}.log").exists():
            time.sleep(0.05)
        time.sleep(0.5)  # Loading the (small) preamble.

    def test_typesets_the_chapter_from_the_unsaved_text_with_the_full_builds_references(self):
        self.wait_ready()
        result = self.warm.compile("c", "ch/one.tex", "\\section{One}Typed but not saved, see \\ref{two}.\n")
        self.assertTrue(result["ok"], result["errors"])
        self.assertTrue(result["warm"])
        self.assertEqual(result["target"], "ch/one")
        words = text(result["pdf"])
        self.assertIn("Typed", words)
        self.assertNotIn("Two", words)  # Only the chapter around the cursor.
        self.assertNotIn("??", words)  # \ref{two} comes from the full build's .aux.
        self.assertIsNotNone(self.warm.ready)  # The next process is already loading its preamble.
        self.assertFalse(list(preview.root_for(self.main).glob("p*/*.pdf")))  # Used directories are removed.

    def test_a_preamble_change_replaces_the_waiting_process(self):
        self.wait_ready()
        old = self.warm.ready
        sty = self.main.parent / "mystyle.sty"
        sty.write_text("\\newcommand\\mine{Changed}\n")
        os.utime(sty, ns=(2, 2))
        result = self.warm.compile("c", "ch/one.tex", "\\mine\n")
        self.assertTrue(result["ok"], result["errors"])
        self.assertFalse(result["warm"])  # Started cold, from the new preamble.
        self.assertIn("Changed", text(result["pdf"]))
        self.assertIsNot(self.warm.ready, old)
        self.assertIsNotNone(old.popen.poll())  # The stale one was stopped.

    def test_a_newer_preview_of_the_same_client_cancels_the_older_one(self):
        self.wait_ready()
        slow = "\\count255=0 \\loop\\advance\\count255 1 \\ifnum\\count255<400000000 \\repeat slow\n"
        results = {}
        thread = threading.Thread(target=lambda: results.update(old=self.warm.compile("c", "ch/one.tex", slow)))
        thread.start()
        deadline = time.monotonic() + 10
        while "c" not in self.warm.running and time.monotonic() < deadline:
            time.sleep(0.01)
        began = time.monotonic()
        new = self.warm.compile("c", "ch/one.tex", "fast\n")
        thread.join(30)
        self.assertTrue(results["old"]["cancelled"])
        self.assertTrue(new["ok"], new["errors"])
        self.assertIn("fast", text(new["pdf"]))
        self.assertLess(time.monotonic() - began, 15)

    def test_errors_point_at_the_typed_file_and_a_preamble_error_does_not_hang(self):
        self.wait_ready()
        result = self.warm.compile("c", "ch/one.tex", "fine\n\n\\undefinedmacro\n")
        self.assertFalse(result["ok"])
        self.assertEqual((result["errors"][0]["file"], result["errors"][0]["line"]), ("ch/one.tex", 3))
        self.main.write_text(MAIN.replace("\\begin{document}", "\\brokenpreamble\n\\begin{document}"))
        began = time.monotonic()
        result = self.warm.compile("c", "ch/one.tex", None)
        self.assertFalse(result["ok"])
        self.assertLess(time.monotonic() - began, 20)


@unittest.skipUnless(shutil.which("latexmk") and shutil.which("pdftotext"), "needs latexmk and pdftotext")
class OtherEngineTests(unittest.TestCase):
    """XeLaTeX and LuaLaTeX wait and resume the same way."""

    def test_xelatex_and_lualatex(self):
        for engine in ("xelatex", "lualatex"):
            if not shutil.which(engine):
                continue
            with self.subTest(engine=engine), fake_repo() as root, contextlib.redirect_stdout(io.StringIO()):
                main = doc(root, f'engine = "{engine}"\n')
                entry, _ = build.build_safely(main, shutil.which("latexmk"), False, False, record=True)
                self.assertTrue(entry["ok"], entry)
                preview.snapshot(main)
                warm = preview.Warm(main)
                try:
                    warm.warm_up()
                    result = warm.compile("c", "ch/one.tex", "Typed with the engine, see \\ref{two}.\n")
                finally:
                    warm.shutdown()
                self.assertTrue(result["ok"], result["errors"])
                self.assertIn("Typed", text(result["pdf"]))
                self.assertNotIn("??", text(result["pdf"]))
