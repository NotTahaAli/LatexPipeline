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


def tearDownModule():
    preview.sandbox.LOW_UNTIL = 0.0  # Previews here put later builds below normal priority for a minute.


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

            def spawn(cmd, cwd, build_dir, env=None, cpu=0, work=None, urgent=False):
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
        while time.monotonic() < deadline and not (self.warm.ready[0].dir / f"{self.main.stem}.log").exists():
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
        self.assertTrue(self.warm.ready)  # The next process is already loading its preamble.
        self.assertFalse(list(preview.root_for(self.main).glob("p*/*.pdf")))  # Used directories are removed.

    def test_a_preamble_change_replaces_the_waiting_process(self):
        self.wait_ready()
        old = self.warm.ready[0]
        sty = self.main.parent / "mystyle.sty"
        sty.write_text("\\newcommand\\mine{Changed}\n")
        os.utime(sty, ns=(2, 2))
        result = self.warm.compile("c", "ch/one.tex", "\\mine\n")
        self.assertTrue(result["ok"], result["errors"])
        self.assertFalse(result["warm"])  # Started cold, from the new preamble.
        self.assertIn("Changed", text(result["pdf"]))
        self.assertNotIn(old, self.warm.ready)
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


FULL = r"""\documentclass{article}
\ifdefined\pdfobjcompresslevel\pdfcompresslevel=0 \pdfobjcompresslevel=0 \fi
\usepackage{mystyle}
\begin{document}
Front.
\input{ch/one}
Between the chapters.
\input{ch/two}
\include{ch/three}
\bibliographystyle{plain}
\bibliography{refs}
\end{document}
"""
BIB = """@misc{known, title={Known Work}, author={A. Author}, year=2000}
@misc{fresh, title={Fresh Work}, author={B. Author}, year=2001}
"""


@unittest.skipUnless(LATEX and shutil.which("bibtex") and shutil.which("pdftotext"), "needs latexmk, bibtex, pdftotext")
class LiveFeatureTests(unittest.TestCase):
    """One full build (no hyperref), then: new labels and citations, main.tex's body, \\include, package-read files,
    pages placed without page labels, SyncTeX, preamble edits, build priority."""

    @classmethod
    def setUpClass(cls):
        cls.stack = contextlib.ExitStack()
        root = cls.stack.enter_context(fake_repo())
        cls.stack.enter_context(contextlib.redirect_stdout(io.StringIO()))
        cls.main = write_doc(root, "doc", FULL)
        doc = cls.main.parent
        (doc / "ch").mkdir()
        (doc / "ch" / "one.tex").write_text(
            "\\clearpage\\section{One}\\label{one}Chapter one, see \\ref{two} and \\cite{known}.\n"
            "\\InputIfFileExists{ch/table.tex}{}{}\n")
        (doc / "ch" / "table.tex").write_text("Table text.\n")
        (doc / "ch" / "two.tex").write_text("\\clearpage\\section{Two}\\label{two}Chapter two.\n")
        (doc / "ch" / "three.tex").write_text("\\section{Three}Chapter three.\n")
        (doc / "refs.bib").write_text(BIB)
        (doc / "mystyle.sty").write_text("\\newcommand\\mine{M}\n")
        entry, _ = build.build_safely(cls.main, shutil.which("latexmk"), False, False, record=True)
        assert entry["ok"], entry
        preview.snapshot(cls.main)

    @classmethod
    def tearDownClass(cls):
        cls.stack.close()

    def setUp(self):
        self.warm = preview.Warm(self.main)
        self.addCleanup(self.warm.shutdown)
        self.warm.warm_up()

    def compile(self, rel, typed, client="c"):
        result = self.warm.compile(client, rel, typed)
        self.assertTrue(result["ok"], result["errors"])
        return result, text(result["pdf"])

    def test_labels_and_citations_new_in_the_chapter_resolve_in_the_same_preview(self):
        result, words = self.compile("ch/one.tex", "\\clearpage\\section{One}Plain edit, see \\ref{two}.\n")
        self.assertEqual(result["passes"], 1)  # Nothing new: one pass, as before.
        typed = ("\\clearpage\\section{One}\\label{one}\\subsection{Sub}\\label{brandnew}See \\ref{brandnew} "
                 "and \\cite{fresh}.\n")
        result, words = self.compile("ch/one.tex", typed)
        self.assertNotIn("??", words)  # The label the chapter itself defines.
        self.assertNotIn("[?]", words)  # A key the full build never cited, from refs.bib through BibTeX.
        self.assertIn("1.1", words)
        self.assertEqual(result["passes"], 3)  # labels and bibliography, then the citation's number
        self.assertEqual(self.warm.pool, 2)  # From now on a second waiting process is kept for such passes.

    def test_main_tex_body_is_previewed_between_its_chapters(self):
        result, words = self.compile("main.tex", FULL.replace("Between the chapters.", "Edited between."))
        self.assertIn("Edited between", words)
        self.assertNotIn("Chapter two", words)
        self.assertNotIn("Chapter one", words)
        self.assertEqual((result["pages"]["first"], result["pages"]["last"]), (1, 1))  # page 2 holds that text
        self.assertEqual(self.warm.compile("c", "main.tex", FULL)["status"], "same")
        moved = self.warm.compile("c", "main.tex", FULL.replace("\\input{ch/two}\n", ""))
        self.assertEqual((moved["status"], moved["saved"]), ("rebuild", False))  # the input tree is the build's
        edited = self.warm.compile("c", "main.tex", FULL.replace("\\begin{document}", "\\usepackage{xcolor}\n"
                                                                                       "\\begin{document}"))
        self.assertEqual((edited["status"], edited["saved"]), ("preamble", False))

    def test_include_and_package_read_files_use_the_typed_text(self):
        result, words = self.compile("ch/three.tex", "\\section{Three}Typed into an include.\n")
        self.assertIn("Typed into an include", words)
        with self.assertRaises(preview.PreviewError):  # Read by a package: which chapter is unknown yet.
            self.warm.compile("fresh-tab", "ch/table.tex", "Typed table.\n")
        self.compile("ch/one.tex", None)
        result, words = self.compile("ch/table.tex", "Typed table.\n")
        self.assertIn("Typed table", words)
        self.assertEqual(result["target"], "ch/one")

    def test_pages_are_placed_in_the_full_pdf_without_page_labels(self):
        result, words = self.compile("ch/two.tex", "\\clearpage\\section{Two}Chapter two typed.\n")
        self.assertEqual((result["pages"]["first"], result["pages"]["last"]), (2, 2))  # the full PDF's third page
        self.assertIn("\n3\n", words)  # numbered like the full build: the page before held chapter one's text

    @unittest.skipUnless(shutil.which("synctex"), "needs synctex")
    def test_synctex_on_a_preview_page_names_the_typed_file(self):
        self.compile("ch/two.tex", "\\clearpage\\section{Two}\n\nFirst typed line.\n\nSecond typed line.\n", "tab1")
        pdf, names = preview.sync_files(self.main, "tab1")
        out = subprocess.run(["synctex", "edit", "-o", f"1:150:130:{pdf}"], capture_output=True, text=True).stdout
        fields = dict(line.split(":", 1) for line in out.splitlines() if line.startswith(("Input:", "Line:")))
        self.assertEqual(names[Path(fields["Input"]).name], "ch/two.tex")
        self.assertGreaterEqual(int(fields["Line"]), 1)
        self.assertIsNone(preview.sync_files(self.main, "other-tab"))

    def test_a_saved_package_edit_restarts_the_waiting_process_and_previews_wait_for_the_build(self):
        sty = self.main.parent / "mystyle.sty"
        old = self.warm.ready[0]
        sty.write_text("\\newcommand\\mine{N}\n")
        self.addCleanup(sty.write_text, "\\newcommand\\mine{M}\n")
        os.utime(sty, ns=(5, 5))
        self.assertEqual(self.warm.compile("c", "mystyle.sty", "unsaved"), preview.status("preamble", saved=False))
        result = self.warm.compile("c", "mystyle.sty", None)
        self.assertEqual((result["status"], result["saved"]), ("preamble", True))
        self.assertNotIn(old, self.warm.ready)  # Restarted from the new preamble.
        self.assertIsNotNone(old.popen.poll())

    def test_previews_put_full_builds_below_normal_priority(self):
        with mock.patch.object(preview.sandbox, "LOW_UNTIL", 0.0):
            self.compile("ch/two.tex", None)
            self.assertGreater(preview.sandbox.LOW_UNTIL, time.monotonic())


class LabelTests(unittest.TestCase):
    def test_only_what_a_reference_prints_counts_as_a_change(self):
        line = "\\newlabel{x}{{1.2}{5}{Title}{section.1.2}{}}"
        self.assertEqual(preview.shown(line), preview.shown(line.replace("section.1.2", "section.7")))
        self.assertNotEqual(preview.shown(line), preview.shown(line.replace("{5}", "{6}")))
        self.assertEqual(preview.labels(["\\newlabel{a}{{1}{2}}\n\\bibcite{k}{3}\n", "\\newlabel{a}{{9}{9}}"]),
                         {"a": "\\newlabel{a}{{9}{9}}", "k": "\\bibcite{k}{3}"})

    def test_referenced_ignores_the_label_itself(self):
        self.assertFalse(preview.referenced("x", "\\label{x} text"))
        self.assertTrue(preview.referenced("x", "\\label{x} see \\ref{x}"))
        self.assertTrue(preview.referenced("x@cref", "\\cref{a, x}"))
        self.assertTrue(preview.referenced("key", "\\citep[p.~3]{other,key}"))


BLX = r"""\documentclass{article}
\ifdefined\pdfobjcompresslevel\pdfcompresslevel=0 \pdfobjcompresslevel=0 \fi
\usepackage[backend=%s,sorting=none]{biblatex}
\addbibresource{refs.bib}
\begin{document}
\input{ch/one}
\input{ch/two}
\printbibliography
\end{document}
"""


@unittest.skipUnless(LATEX and shutil.which("biber") and shutil.which("pdftotext"), "needs latexmk, biber, pdftotext")
class BiberTests(unittest.TestCase):
    """biblatex: a key the full build never cited resolves through biber; backend=bibtex keeps BibTeX."""

    def preview(self, backend, typed):
        with fake_repo() as root, contextlib.redirect_stdout(io.StringIO()):
            main = write_doc(root, "doc", BLX % backend)
            (main.parent / "ch").mkdir()
            (main.parent / "ch" / "one.tex").write_text("\\section{One}See \\cite{known}.\n")
            (main.parent / "ch" / "two.tex").write_text("\\section{Two}Chapter two, \\cite{other}.\n")
            (main.parent / "refs.bib").write_text(BIB + "@misc{other, title={Other}, author={C. Author}, year=2002}\n")
            entry, _ = build.build_safely(main, shutil.which("latexmk"), False, False, record=True)
            self.assertTrue(entry["ok"], entry)
            preview.snapshot(main)
            warm = preview.Warm(main)
            try:
                warm.warm_up()
                with mock.patch.object(warm, "biber", wraps=warm.biber) as biber:
                    plain = warm.compile("c", "ch/two.tex", "\\section{Two}Edited, \\cite{other}.\n")
                    result = warm.compile("c", "ch/two.tex", typed)
            finally:
                warm.shutdown()
        self.assertTrue(plain["ok"] and result["ok"], (plain["errors"], result["errors"]))
        self.assertEqual(plain["passes"], 1)
        return result, text(result["pdf"]), biber

    def test_new_key_resolves_through_biber_and_known_numbers_stay(self):
        result, words, biber = self.preview("biber", "\\section{Two}Now \\cite{fresh} and \\cite{other}.\n")
        self.assertEqual(biber.call_count, 1)  # Only for the preview with a new key.
        self.assertNotIn("fresh", words)  # biblatex prints an undefined key as [fresh]
        self.assertIn("[3]", words)  # fresh comes after the full build's known [1] and other [2] (sorting=none)
        self.assertIn("[2]", words)
        self.assertEqual(result["passes"], 2)

    def test_backend_bibtex_keeps_the_bibtex_path(self):
        result, words, biber = self.preview("bibtex", "\\section{Two}Now \\cite{fresh}.\n")
        self.assertEqual(biber.call_count, 0)
        self.assertNotIn("fresh", words)
        self.assertEqual(result["passes"], 2)  # biblatex reads its labels from the .bbl: no third pass


class BcfTests(unittest.TestCase):
    """The .bcf biber reads: the snapshot's keys first, then this run's; only local data files of the document."""

    def test_merged_control_file(self):
        bcf = ('<bcf:controlfile>\n  <bcf:bibdata section="0">\n'
               '    <bcf:datasource type="file" datatype="bibtex" glob="false">refs.bib</bcf:datasource>\n'
               '    <bcf:datasource type="file" datatype="bibtex" glob="false">../out.bib</bcf:datasource>\n'
               '    <bcf:datasource type="url" datatype="bibtex" glob="false">http://example.org/x.bib</bcf:datasource>\n'
               '  </bcf:bibdata>\n  <bcf:section number="0">\n'
               '    <bcf:citekey order="1" intorder="1">new</bcf:citekey>\n'
               '    <bcf:citekey order="2" intorder="1">old</bcf:citekey>\n  </bcf:section>\n</bcf:controlfile>\n')
        with fake_repo() as root:
            main = write_doc(root, "doc", "x")
            (main.parent / "refs.bib").write_text("")
            aux = preview.root_for(main) / "aux"
            aux.mkdir(parents=True)
            (aux / "main.bcf").write_text(bcf.replace(">new<", ">first<"))
            proc = mock.Mock(dir=root / "p", root=preview.root_for(main))
            proc.dir.mkdir()
            warm = preview.Warm(main)
            with mock.patch.object(preview.subprocess, "run") as ran:
                self.assertEqual(warm.biber(proc, {"new"}), {})  # No .bcf: biblatex with backend=bibtex, or none.
                (proc.dir / "main.bcf").write_text(bcf)
                self.assertEqual(warm.biber(proc, {"elsewhere"}), {})  # Not a key of this run's .bcf.
                ran.assert_not_called()
                ran.side_effect = lambda **_: (proc.dir / "pgfwarm-bib.bbl").write_text("\\entry{new}{misc}{}")
                self.assertEqual(warm.biber(proc, {"new"}), {"main.bbl": b"\\entry{new}{misc}{}"})
                self.assertIn("biber", ran.call_args.kwargs["args"])
            written = (proc.dir / "pgfwarm-bib.bcf").read_text()
            self.assertEqual(preview.BCF_KEY.findall(written), ["first", "old", "new"])
            self.assertIn('order="3" intorder="1">new<', written)
            self.assertIn(">refs.bib<", written)
            self.assertNotIn("out.bib", written)
            self.assertNotIn("example.org", written)
            self.assertTrue((proc.dir / "refs.bib").is_file())
