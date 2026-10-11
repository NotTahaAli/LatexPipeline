"""scripts/a11y.py: accessibility checks of tagged documents, and how build.py and ci_report.py report them."""

import contextlib
import importlib
import io
import json
import shutil
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from _support import build, ci_report, fake_repo, write_doc

a11y = importlib.import_module("a11y")  # after _support put scripts/ on sys.path

FIXTURE = Path(__file__).resolve().parent / "fixtures" / "a11y"


def kinds(findings):
    return [(found["kind"], found["file"], found["line"]) for found in findings]


class SourceChecks(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name) / "doc"
        self.root.mkdir()

    def write(self, rel, text):
        path = self.root / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8")

    def test_fixture(self):
        self.assertEqual(kinds(a11y.check(FIXTURE, None)), [
            ("a11y-package", "main.tex", 3), ("a11y-heading", "main.tex", 12), ("a11y-alt", "main.tex", 15)])

    def test_headings_are_followed_through_inputs_in_reading_order(self):
        self.write("main.tex", "\\chapter{A}\n\\input{ch/one}\n\\section{C}\n\\subsubsection{D}\n")
        self.write("ch/one.tex", "\\section{B}\n\\paragraph{Deep}\n")
        found = a11y.check(self.root, None)
        self.assertEqual(kinds(found), [("a11y-heading", "ch/one.tex", 2), ("a11y-heading", "main.tex", 4)])
        self.assertIn("\\paragraph follows \\section (ch/one.tex:1)", found[0]["message"])

    def test_part_resets_and_starred_headings_count(self):
        self.write("main.tex", "\\part{P}\n\\section*{A}\n\\subsection*{B}\n\\subsubsection{C}\n")
        self.assertEqual(a11y.check(self.root, None), [])

    def test_alt_text_keys(self):
        self.write("main.tex", "\\includegraphics[alt={A [bracketed] dog}, width=1cm]{a}\n"
                               "\\includegraphics[artifact]{b}\n\\includegraphics[width=1cm,actualtext=x]{c}\n"
                               "\\includegraphics[width=\\linewidth]{d}\n\\includegraphics{e}\n"
                               "\\newcommand\\pic[1]{\\includegraphics{#1}}\n% \\includegraphics{f}\n"
                               "\\includegraphics[salt=1]{g}\n")
        self.assertEqual([f["message"] for f in a11y.check(self.root, None)], [
            "\\includegraphics{d} has no alt text.", "\\includegraphics{e} has no alt text.",
            "\\includegraphics{g} has no alt text."])

    def test_packages_from_lists_classes_and_local_styles(self):
        self.write("main.tex", "\\documentclass{beamer}\n\\usepackage[x]{amsmath, soul}\n\\usepackage{mine}\n"
                               "\\usepackage{soul}\n")
        self.write("mine.sty", "\\RequirePackage{titlesec}\n")
        found = a11y.check(self.root, None)
        self.assertEqual([(f["file"], f["message"].split()[0]) for f in found],
                         [("main.tex", "beamer"), ("main.tex", "soul"), ("mine.sty", "titlesec")])
        self.assertIn(a11y.STATUS_URL, found[0]["hint"])

    def test_files_outside_the_document_are_not_read(self):
        (self.root.parent / "outside.tex").write_text("\\section{A}\n\\subsubsection{B}\n", encoding="utf-8")
        self.write("main.tex", "\\input{../outside}\n\\input{" + str(self.root.parent / "outside") + "}\n")
        self.assertEqual(a11y.check(self.root, None), [])

    def test_title(self):
        xmp = b'<x:xmpmeta><dc:title><rdf:Alt><rdf:li xml:lang="en">%s</rdf:li></rdf:Alt></dc:title></x:xmpmeta>'
        self.assertTrue(a11y.title_set(xmp % b"My title"))
        self.assertFalse(a11y.title_set(xmp % b" "))
        self.assertFalse(a11y.title_set(b"<x:xmpmeta></x:xmpmeta> /Title 34 0 R"))  # a structure element, not Info
        info = b"trailer << /Info 7 0 R >>\n7 0 obj\n<< /Producer (pdfTeX) %s >>\nendobj"
        self.assertTrue(a11y.title_set(info % b"/Title (A \\) title)"))
        self.assertTrue(a11y.title_set(info % b"/Title <FEFF0041>"))
        for empty in (b"/Title ()", b"/Title <FEFF>", b""):
            self.assertFalse(a11y.title_set(info % empty), empty)
        self.assertIsNone(a11y.title_set(b"%PDF-1.5 nothing"))
        self.assertEqual(kinds(a11y.check(self.root, info % b"/Title ()")), [("a11y-title", None, None)])


@unittest.skipUnless(shutil.which("latexmk") and shutil.which("pdflatex"), "needs latexmk and pdflatex")
class BuildReport(unittest.TestCase):
    def test_tagged_build_reports_findings_in_log_report_and_lint(self):
        with fake_repo() as root, contextlib.redirect_stdout(io.StringIO()):
            shutil.copytree(FIXTURE, root / "files" / "a11y")
            plain = write_doc(root, "plain", "\\documentclass{article}\\begin{document}\\section{A}\\subsubsection{B}"
                                             "\\end{document}\n")
            entry, _ = build.build_document(root / "files" / "a11y" / "main.tex", build.find_latexmk(), live=False,
                                            validate=False)
            self.assertTrue(entry["ok"], entry)
            self.assertEqual([f["kind"] for f in entry["a11y"]],
                             ["a11y-title", "a11y-package", "a11y-heading", "a11y-alt"])
            log = (root / "out" / "a11y.log").read_text(encoding="utf-8")
            head = log.split("===== latexmk output")[0]
            self.assertIn("ACCESSIBILITY: The PDF has no title", head)
            self.assertIn("ACCESSIBILITY: files/a11y/main.tex:12: \\subsubsection follows", head)
            self.assertNotIn("a11y", build.build_document(plain, build.find_latexmk(), live=False)[0])  # untagged

            build.write_report([entry])
            with mock.patch.object(ci_report, "REPORT_PATH", root / "out" / "build-report.json"):
                found = ci_report.a11y_findings("a11y")
            self.assertEqual({f.level for f in found}, {"info"})
            self.assertEqual(found[0].path, "main.tex")
            self.assertNotIn("main.tex:9", found[2].subject)  # stable under edits that move the earlier heading
            json.dumps([f.as_dict() for f in found])


if __name__ == "__main__":
    unittest.main()
