"""Pure logic in scripts/accel.py: the recorded \\input tree and --focus selection."""

import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path

from _support import build

accel = build.accel

MAP = r"""\pgff@e{n}{1}{Chapters/ch1}{\pgff@c{chapter}{0}\pgff@c{page}{7}}
\pgff@e{n}{1}{Chapters/ch5/ch5}{\pgff@c{chapter}{4}\pgff@c{page}{80}}
\pgff@e{n}{2}{Chapters/ch5/a}{\pgff@c{chapter}{5}\pgff@c{page}{81}}
\pgff@e{n}{2}{Chapters/ch5/b.tex}{\pgff@c{chapter}{5}\pgff@c{page}{85}}
\pgff@e{n}{1}{Chapters/ch6/ch6}{\pgff@c{chapter}{5}\pgff@c{page}{90}}
"""


class FocusTests(unittest.TestCase):
    def entries(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "main.focusmap"
            path.write_text(MAP, encoding="utf-8")
            return accel.read_focusmap(path)

    def test_read_normalizes_and_parses_counters(self):
        entries = self.entries()
        self.assertEqual([e["path"] for e in entries][3], "Chapters/ch5/b")
        self.assertEqual(entries[1]["counters"], [("chapter", "4"), ("page", "80")])

    def test_directory_focus_keeps_matches_and_their_ancestors(self):
        entries = self.entries()
        keep, first = accel.focus_selection(entries, "Chapters/ch5")
        self.assertEqual((sorted(keep), first), ([1, 2, 3], 1))

    def test_file_focus_keeps_the_file_that_reads_it(self):
        entries = self.entries()
        keep, first = accel.focus_selection(entries, "Chapters/ch5/b")
        self.assertEqual((sorted(keep), first), ([1, 3], 3))

    def test_unknown_focus(self):
        self.assertEqual(accel.focus_selection(self.entries(), "nope"), (set(), None))

    def test_top_unit_is_the_depth_one_file_holding_a_path(self):
        self.assertEqual(accel.top_unit(self.entries(), "Chapters/ch5/a.tex"), "Chapters/ch5/ch5")
        self.assertIsNone(accel.top_unit(self.entries(), "main.tex"))

    def test_focus_tex_restores_counters_of_the_first_match(self):
        entries = self.entries()
        keep, first = accel.focus_selection(entries, "Chapters/ch5")
        text = accel.focus_tex(entries, keep, first)
        self.assertIn(r"\pgff@c{page}{80}", text)
        self.assertIn(r"pgff@ok@Chapters/ch5/a\endcsname", text)
        self.assertNotIn("Chapters/ch1", text)


class TikzDetectionTests(unittest.TestCase):
    def test_only_documents_that_mention_tikz_are_externalized(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "main.tex").write_text(r"\documentclass{article}", encoding="utf-8")
            self.assertFalse(accel.uses_tikz(root))
            (root / "my.cls").write_text(r"\RequirePackage{tikz}", encoding="utf-8")
            self.assertTrue(accel.uses_tikz(root))


class TouchedTests(unittest.TestCase):
    def test_only_figure_text_counts_as_a_figure_edit(self):
        with tempfile.TemporaryDirectory() as tmp:
            doc, cache = Path(tmp) / "doc", Path(tmp) / "cache"
            doc.mkdir()
            cache.mkdir()
            (doc / "main.tex").write_text("\\documentclass{article}\n\\begin{document}\\input{a}\\end{document}\n")
            chapter = doc / "a.tex"
            chapter.write_text("Text.\n\\begin{tikzpicture}\\node{A};\\end{tikzpicture}\n")
            figures = accel.Figures(doc / "main.tex", cache, "pdflatex", False, 1)
            self.assertTrue(figures.touched(0.0))  # No snapshot yet: any file with a figure.
            chapter.write_text("More text.\n\\begin{tikzpicture}\\node{A};\\end{tikzpicture}\n")
            self.assertFalse(figures.touched(0.0))
            chapter.write_text("More text.\n\\begin{tikzpicture}\\node{B};\\end{tikzpicture}\n")
            self.assertTrue(figures.touched(0.0))


HEAD = "\\documentclass{article}\n\\usepackage{tikz}\n\\pagestyle{empty}\n"


def pic(text):
    return "\\begin{tikzpicture}\\node{" + text + "};\\end{tikzpicture}\n"


@unittest.skipUnless(shutil.which("pdflatex") and shutil.which("pdftotext"), "needs pdflatex and pdftotext")
class ExternalizedBuildTests(unittest.TestCase):
    """The figure pipeline end to end, with real LaTeX, on tiny documents."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.doc = Path(self.tmp.name) / "doc"
        self.doc.mkdir()
        self.build = Path(self.tmp.name) / "cache"
        self.build.mkdir()

    def write(self, name, text):
        (self.doc / name).write_text(text, encoding="utf-8")

    def render(self):
        """One externalized build in the same cache; returns the PDF text."""
        figures = accel.Figures(self.doc / "main.tex", self.build, "pdflatex", False, 2)
        tex = ["pdflatex", "-interaction=batchmode", "-halt-on-error", f"-output-directory={self.build}"]
        quiet = {"stdin": subprocess.DEVNULL, "stdout": subprocess.DEVNULL, "check": True}
        subprocess.run(figures.discover_command(), cwd=self.doc, **quiet)
        for _ in range(2):
            changed, errors = figures.sync(lambda text: None)
            self.assertEqual(errors, [])
            subprocess.run(
                [*tex, "-jobname=main", figures.main_pretex + r"\input{main.tex}"],
                cwd=self.doc, **quiet,
            )
        self.figures = figures
        return subprocess.run(
            ["pdftotext", str(self.build / "main.pdf"), "-"], capture_output=True, text=True,
        ).stdout.split()

    def test_cache_hit_after_reordering_chapters(self):
        self.write("a.tex", pic("AAA"))
        self.write("b.tex", pic("BBB"))
        self.write("main.tex", HEAD + "\\begin{document}\n\\input{a}\\input{b}\n\\end{document}\n")
        self.assertEqual(self.render(), ["AAA", "BBB"])
        self.write("main.tex", HEAD + "\\begin{document}\n\\input{b}\\input{a}\n\\end{document}\n")
        self.assertEqual(self.render(), ["BBB", "AAA"])

    def test_pictures_with_identical_source_stay_distinct(self):
        loop = "\\foreach \\c in {red,blue}{\\begin{tikzpicture}\\node{\\c};\\end{tikzpicture}}\n"
        self.write("main.tex", HEAD + "\\begin{document}\n" + loop + "\\end{document}\n")
        self.assertEqual(self.render(), ["red", "blue"])
        self.write("main.tex", HEAD + "\\begin{document}\n" + pic("NEW") + loop + "\\end{document}\n")
        self.assertEqual(self.render(), ["NEW", "red", "blue"])

    def test_file_input_by_the_preamble_invalidates_figures(self):
        self.write("styles.tex", "\\newcommand{\\lbl}{one}\n")
        self.write("main.tex", HEAD + "\\input{styles}\n\\begin{document}\n" + pic("\\lbl") + "\\end{document}\n")
        self.assertEqual(self.render(), ["one"])
        self.write("styles.tex", "\\newcommand{\\lbl}{two}\n")
        self.assertEqual(self.render(), ["two"])

    def test_unused_cached_figures_are_deleted(self):
        self.write("main.tex", HEAD + "\\begin{document}\n" + pic("one") + "\\end{document}\n")
        self.render()
        self.write("main.tex", HEAD + "\\begin{document}\n" + pic("two") + "\\end{document}\n")
        self.render()
        self.assertEqual(len(list(self.figures.cache.glob("*.pdf"))), 1)

    def test_pictures_that_need_the_page_disable_externalization(self):
        self.write("main.tex", HEAD + "\\begin{document}\n\\begin{tikzpicture}[remember picture,overlay]"
                   "\\node{x};\\end{tikzpicture}\n\\end{document}\n")
        self.assertFalse(accel.uses_tikz(self.doc))


if __name__ == "__main__":
    unittest.main()
