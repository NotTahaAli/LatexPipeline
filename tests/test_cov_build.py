"""build.py command line, clean, change detection, the summary and failure paths the other tests leave out."""

from __future__ import annotations

import contextlib
import io
import os
import shutil
import subprocess
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

from _support import build, fake_repo, write_doc

MAIN = "\\documentclass{article}\n\\begin{document}\nHello.\n\\end{document}\n"


def run_main(*argv):
    out, err = io.StringIO(), io.StringIO()
    with mock.patch.object(build.sys, "argv", ["build.py", *argv]), contextlib.redirect_stdout(out), \
            contextlib.redirect_stderr(err):
        try:
            code = build.main()
        except SystemExit as exc:
            code = exc.code
    return code, out.getvalue() + err.getvalue()


class Arguments(unittest.TestCase):
    def test_incompatible_flags_are_refused(self):
        for argv in (["--notify"], ["--exec", "x"], ["--check"], ["--pages", "1", "--watch"],
                     ["--preflight", "--focus", "x"], ["--no-comments"], ["--review-pdf", "--watch"],
                     ["--focus", "auto"], ["--jobs", "0"], ["--timeout", "0"], ["--open"],
                     ["--html", "--watch"], ["--format", "--docx"], ["--arxiv", "--watch"],
                     ["--cited-bib", "--focus", "x"]):
            with self.subTest(argv=argv):
                self.assertEqual(run_main(*argv)[0], 2)


class MainPaths(unittest.TestCase):
    def test_document_selection_and_listing(self):
        with fake_repo() as root:
            self.assertEqual(run_main()[0], 0)  # no documents at all
            write_doc(root, "one", MAIN)
            write_doc(root, "two", MAIN)
            code, text = run_main("nope")
            self.assertEqual(code, 2)
            self.assertIn("No document matches: nope", text)
            code, text = run_main("--list")
            self.assertEqual(code, 0)
            self.assertIn("one", text)
            self.assertEqual(run_main("--source", "missing-dir")[0], 2)
            self.assertEqual(run_main("--pages", "1")[0], 2)  # two documents
            with mock.patch.object(build, "check_latex", return_value="latexmk"):
                self.assertEqual(run_main("--focus", "x")[0], 2)
                self.assertEqual(run_main("--review-pdf")[0], 2)
            with mock.patch.object(build.shutil, "which", return_value=None):
                self.assertEqual(run_main("--docx")[0], 2)

    def test_clean_and_new(self):
        with fake_repo() as root:
            code, text = run_main("--clean")
            self.assertIn("Nothing to clean.", text)
            (root / "out").mkdir()
            (root / ".latex-cache" / "x").mkdir(parents=True)
            self.assertEqual(run_main("--clean")[0], 0)
            self.assertFalse((root / "out").exists() or (root / ".latex-cache").exists())
            (root / "out").mkdir()
            with mock.patch.object(build.shutil, "rmtree", side_effect=OSError("busy")):
                self.assertEqual(run_main("--clean")[0], 1)
            self.assertEqual(run_main("--new", "fresh")[0], 0)
            self.assertTrue((root / "files" / "fresh" / "main.tex").is_file())

    def test_up_to_date_documents_are_not_rebuilt(self):
        with fake_repo() as root:
            main = write_doc(root, "one", MAIN)
            pdf = build.output_path_for(main)
            pdf.parent.mkdir(parents=True)
            pdf.write_bytes(b"%PDF")
            future = time.time() + 100
            os.utime(pdf, (future, future))
            with mock.patch.object(build, "check_latex", return_value="latexmk"), \
                    mock.patch.object(build, "build_safely", side_effect=AssertionError("built")):
                code, text = run_main()
            self.assertEqual(code, 0, text)
            self.assertIn("Everything is up to date.", text)

    def test_one_crash_does_not_stop_the_others(self):
        with fake_repo() as root:
            main = write_doc(root, "one", MAIN)
            with mock.patch.object(build, "_build_document", side_effect=RuntimeError("kaboom")), \
                    contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
                entry, _log = build.build_safely(main, "latexmk", False, True)
            self.assertFalse(entry["ok"])
            self.assertIn("kaboom", entry["error"])

    def test_check_latex_without_latexmk(self):
        with mock.patch.object(build.shutil, "which", return_value=None), \
                contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()), \
                self.assertRaises(SystemExit) as ctx:
            build.check_latex()
        self.assertEqual(ctx.exception.code, 1)


class Summary(unittest.TestCase):
    def test_every_kind_of_line(self):
        found = {"file": "main.tex", "line": 3, "message": "Undefined control sequence", "hint": "Check the name."}
        results = [
            {"name": "good", "ok": True, "engine": "pdflatex", "pages": 1, "size": 2_000_000, "warnings": 2,
             "a11y": ["x"], "seconds": 1.5, "error": None, "phases": {"latex": 1.0}, "errors": [], "log": "good.log",
             "steps": [{"rule": "pdflatex", "seconds": 0.9}],
             "outputs": [{"name": "print", "ok": False, "pdf": "good-print.pdf", "error": "bad"}]},
            {"name": "bad", "ok": False, "engine": None, "pages": None, "warnings": 0, "seconds": None,
             "error": "LaTeX failed", "phases": {}, "errors": [found] * (build.MAX_SUMMARY_ERRORS + 2),
             "log": "bad.log"},
        ]
        out = io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(out):
            build.print_summary(results, profile=True)
        text = out.getvalue()
        for part in ("2.0 MB", "2 warning(s)", "1 accessibility finding(s)", "phases: latex 1.0s",
                     "steps: pdflatex 0.9s", "output print: FAILED, out/good-print.pdf (bad)", "ERROR: LaTeX failed",
                     "hint: Check the name.", "... 2 more in out/bad.log", "Failed:     1"):
            self.assertIn(part, text)


@unittest.skipUnless(shutil.which("git"), "needs git")
class ChangeDetection(unittest.TestCase):
    def git(self, root, *args):
        subprocess.run(["git", "-c", "user.name=t", "-c", "user.email=t@example.org", "-c", "commit.gpgsign=false",
                        *args], cwd=root, check=True, capture_output=True)

    def test_changed_since(self):
        with fake_repo() as root:
            one = write_doc(root, "one", MAIN)
            two = write_doc(root, "two", MAIN)
            self.git(root, "init", "-q")
            self.git(root, "add", "-A")
            self.git(root, "commit", "-qm", "base")
            (one.parent / "main.tex").write_text(MAIN + "%\n", encoding="utf-8")
            with contextlib.redirect_stdout(io.StringIO()):
                self.assertEqual(build.changed_files("HEAD"), ["files/one/main.tex"])
                self.assertEqual(build.filter_changed([one, two], "HEAD"), [one])
                self.assertIsNone(build.changed_files("no-such-ref"))
                self.assertEqual(build.filter_changed([one, two], "no-such-ref"), [one, two])
                with mock.patch.object(build, "changed_files", return_value=[build.GLOBAL_INPUTS[0]]):
                    self.assertEqual(build.filter_changed([one, two], "HEAD"), [one, two])


@unittest.skipUnless(shutil.which("latexmk") and shutil.which("pdflatex"), "needs latexmk and pdflatex")
class RealBuilds(unittest.TestCase):
    def test_strict_mode_keeps_the_old_pdf(self):
        with fake_repo() as root:
            main = write_doc(root, "one", "\\documentclass{article}\n\\begin{document}\nSee \\ref{nowhere}.\n"
                                          "\\end{document}\n", "strict = true\n")
            pdf = build.output_path_for(main)
            pdf.parent.mkdir(parents=True)
            pdf.write_bytes(b"old")
            with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
                report, _log = build.build_document(main, shutil.which("latexmk"), live=False, force=True)
            self.assertFalse(report["ok"])
            self.assertTrue(any("Strict mode" in e for e in [report["error"] or ""] + [str(e) for e in
                                                                                          report["errors"]]), report)
            self.assertEqual(pdf.read_bytes(), b"old")

    def test_success_without_a_pdf_is_a_failure(self):
        with fake_repo() as root:
            main = write_doc(root, "one", "\\documentclass{article}\n\\begin{document}\n\\end{document}\n")
            with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
                report, _log = build.build_document(main, shutil.which("latexmk"), live=False, force=True)
            self.assertFalse(report["ok"])
            self.assertEqual(report["error"], "LaTeX produced no pages: the document body is empty.")

    def test_parallel_build_then_the_tools_that_read_it(self):
        body = "\\documentclass{article}\n\\begin{document}\n\\section{A}\\label{s:a}\nHello \\cite{k}.\n" \
               "\\bibliographystyle{plain}\\bibliography{refs}\n\\end{document}\n"
        with fake_repo() as root:
            for name in ("one", "two"):
                main = write_doc(root, name, body)
                (main.parent / "refs.bib").write_text("@misc{k, title={T}, year={2020}}\n@misc{u, title={U}}\n")
            code, text = run_main("--jobs", "2", "--force")
            self.assertEqual(code, 0, text)
            self.assertIn("Successful: 2", text)
            code, text = run_main("one", "--query")
            self.assertEqual(code, 0, text)
            self.assertIn('"s:a"', text)
            code, text = run_main("one", "--deps", "make")
            self.assertEqual(code, 0, text)
            self.assertIn("files/one/refs.bib", text)  # read by BibTeX, so only in .fdb_latexmk
            self.assertEqual(run_main("one", "--preflight")[0], 0)
            code, text = run_main("one", "--cited-bib")
            self.assertEqual(code, 0, text)
            cited = next(root.joinpath("out").glob("one*.bib")).read_text()
            self.assertIn("@misc{k", cited)
            self.assertNotIn("@misc{u", cited)
            write_doc(root, "three", body)
            self.assertEqual(run_main("three", "--deps", "make")[0], 1)  # never built: nothing recorded

    def test_an_outside_bib_counts_as_an_input(self):
        """BibTeX reads the .bib, not TeX, so it is missing from the .fls; latexmk's .fdb_latexmk lists it."""
        with fake_repo() as root:
            shared = root / "shared"
            shared.mkdir()
            (shared / "refs.bib").write_text("@misc{k, title={T}, year={2020}}\n")
            main = write_doc(root, "one", "\\documentclass{article}\n\\begin{document}\nHello \\cite{k}.\n"
                                          "\\bibliographystyle{plain}\\bibliography{../../shared/refs}\n"
                                          "\\end{document}\n")
            with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
                report, _log = build.build_document(main, shutil.which("latexmk"), live=False)
            self.assertTrue(report["ok"], report)
            self.assertEqual(build.recorded_inputs(main), [(shared / "refs.bib").resolve()])
            self.assertFalse(build.is_stale(main))
            future = time.time() + 100
            os.utime(shared / "refs.bib", (future, future))
            self.assertTrue(build.is_stale(main))  # editing the shared .bib rebuilds the document


class BibSources(unittest.TestCase):
    def test_bibtex_and_biber_rules_only(self):
        fdb = (
            '# Fdb version 4\n'
            '["biber main"] 1 "main.bcf" "main.bbl" "main" 1 0\n'
            '  "main.bcf" 1 2 abc "pdflatex"\n'
            '  "/abs/refs.bib" 1 2 abc ""\n'
            '  (generated)\n'
            '  "main.bbl"\n'
            '["pdflatex"] 1 "main.tex" "main.pdf" "main" 1 0\n'
            '  "main.tex" 1 2 abc ""\n'
            '["bibtex chap"] 1 "chap.aux" "chap.bbl" "chap" 1 0\n'
            '  "../shared/x.bib" 1 2 abc ""\n'
        )
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "main.fdb_latexmk"
            path.write_text(fdb)
            self.assertEqual(build.bib_sources(path), ["main.bcf", "/abs/refs.bib", "../shared/x.bib"])
            self.assertEqual(build.bib_sources(Path(tmp) / "missing"), [])


if __name__ == "__main__":
    unittest.main()
