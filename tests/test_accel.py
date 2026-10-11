"""Pure logic in scripts/accel.py: the recorded \\input tree and --focus selection."""

import os
import shutil
import subprocess
import tempfile
import time
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

    def test_read_attaches_shipped_page_counts_to_start_and_end(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "main.focusmap"
            path.write_text(
                "\\pgff@e{n}{1}{a}{}\n\\pgff@f{-1}\n\\pgff@s{3}{1}\n\\pgff@e{n}{2}{b}{}\n\\pgff@f{-1}\n\\pgff@s{4}{0}\n"
                "\\pgff@x{2}{-1}{}\n\\pgff@s{6}{3}\n\\pgff@x{1}{-1}{}\n\\pgff@s{9}{2}\n\\pgff@e{n}{1}{c}{}\n"
                "\\pgff@s{-1}{0}\n",
                encoding="utf-8",
            )
            entries = accel.read_focusmap(path)
        self.assertEqual([(e.get("shipped"), e.get("shipped_end")) for e in entries], [(3, 9), (4, 6), (None, None)])
        self.assertEqual([(e.get("pending"), e.get("pending_end")) for e in entries], [(1, 2), (0, 3), (None, None)])

    def test_a_chapter_after_a_filled_page_starts_on_the_next_one(self):
        with tempfile.TemporaryDirectory() as tmp:
            doc = Path(tmp)
            (doc / "ch.tex").write_text("% a comment\n\\label{x}\\chapter{Two}\n", encoding="utf-8")
            (doc / "sec.tex").write_text("\\section{Two}\n", encoding="utf-8")
            (doc / "wrap.tex").write_text("\\appendix\n\\input{ch}\n", encoding="utf-8")
            entry = {"kind": "n", "raw": "ch", "pending": 1}
            self.assertEqual(accel.next_page(entry, doc), 1)
            self.assertEqual(accel.next_page({**entry, "pending": 3}, doc), 2)  # and a page of waiting floats
            self.assertEqual(accel.next_page({**entry, "pending": 0}, doc), 0)  # an empty page: no break
            self.assertEqual(accel.next_page({**entry, "raw": "sec"}, doc), 0)  # a section continues the page
            self.assertEqual(accel.next_page({**entry, "raw": "wrap"}, doc), 1)
            self.assertEqual(accel.next_page(entry, doc, "unsaved text"), 0)
            entries = [{"kind": "n", "raw": "ch", "path": "ch", "depth": 1, "counters": [("page", "7")]}]
            self.assertIn(r"\pgff@c{page}{9}", accel.focus_tex(entries, {0}, 0, 2))

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


class SkipDetectionTests(unittest.TestCase):
    def test_switches_that_change_later_figures_block_skipping(self):
        for text in (r"\newcommand{\x}{1}", r"\small", r"\twocolumn", r"\color{red}", r"\sffamily", r"\newgeometry{}"):
            self.assertTrue(accel.DEFINES.search(text), text)
        for text in (r"Plain \textbf{words} and \cite{k}.", r"\section{Intro}\label{s}", r"\textcolor{red}{x}"):
            self.assertFalse(accel.DEFINES.search(text), text)


class BodyDependencyTests(unittest.TestCase):
    def test_only_definitions_pictures_use_count(self):
        with tempfile.TemporaryDirectory() as tmp:
            doc = Path(tmp)
            main = doc / "main.tex"
            main.write_text("\\newcommand{\\pre}{x}\\begin{document}\\input{ch}\\end{document}", encoding="utf-8")
            ch = doc / "ch.tex"
            ch.write_text("\\newcommand{\\used}{a}\\newcommand{\\text}{b}\n\\tikzset{s/.style={\nred}}\n"
                          "\\definecolor{mine}{rgb}{1,0,0}\n\\begin{tikzpicture}\\node[mine]{\\used};\\end{tikzpicture}\n",
                          encoding="utf-8")
            before = accel.body_dependencies(main)
            self.assertIn(b"\\newcommand{\\used}", before)
            self.assertIn(b"red}}", before)  # the whole \\tikzset, across lines
            self.assertIn(b"definecolor{mine}", before)
            self.assertNotIn(b"\\text}", before)
            self.assertNotIn(b"\\pre", before)  # the preamble is hashed on its own
            ch.write_text(ch.read_text(encoding="utf-8").replace("{b}", "{c}") + "Words.\n", encoding="utf-8")
            self.assertEqual(accel.body_dependencies(main), before)


class TikzDetectionTests(unittest.TestCase):
    def test_only_documents_that_mention_tikz_are_externalized(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "main.tex").write_text(r"\documentclass{article}", encoding="utf-8")
            self.assertFalse(accel.uses_tikz(root))
            (root / "my.cls").write_text(r"\RequirePackage{tikz}", encoding="utf-8")
            self.assertTrue(accel.uses_tikz(root))

    def test_words_and_comments_do_not_count_only_code_that_loads_or_draws(self):
        for text in ("Text that names pgfplots and tikz.", "% \\usepackage{tikz}", "\\verb|x| 50\\% tikz",
                     "\\usepackage{pgfopts}", "\\documentclass[a4paper]{article}"):
            self.assertFalse(accel.TIKZ_WORDS.search(accel.COMMENT.sub("", text)), text)
        for text in ("\\usepackage{tikz}", "\\usepackage[compat=1.18]{amsmath, pgfplots}", "\\usepackage{tikz-cd}",
                     "\\RequirePackage{circuitikz}", "\\usetikzlibrary{arrows}", "\\usepgfplotslibrary{fillbetween}",
                     "\\begin{tikzpicture}", "\\documentclass[tikz,border=2pt]{standalone}", "\\usepackage{pgf}",
                     "\\tikz \\draw (0,0) -- (1,1);", "\\usepackage{forest}", "\\usepackage{amsmath,tkz-euclide}",
                     "\\usepackage{chemfig}", "\\usepackage{pgfgantt}", "\\usepackage{smartdiagram}"):
            self.assertTrue(accel.TIKZ_WORDS.search(accel.COMMENT.sub("", text)), text)
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "main.tex").write_text("A sentence that names pgfplots.\n% \\usepackage{tikz}\n", encoding="utf-8")
            self.assertFalse(accel.uses_tikz(root))


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
        # Older than the runs, so figure jobs may skip files (Figures.plan_skips).
        os.utime(self.doc / name, (time.time() - 10, time.time() - 10))

    def render(self):
        """One externalized build in the same cache; returns the PDF text."""
        figures = accel.Figures(self.doc / "main.tex", self.build, "pdflatex", False, 2)
        tex = ["pdflatex", "-interaction=batchmode", "-halt-on-error", f"-output-directory={self.build}"]
        quiet = {"stdin": subprocess.DEVNULL, "stdout": subprocess.DEVNULL, "check": True}
        figures.recorded = time.time()
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

    def test_injected_hooks_leave_a_document_without_tikz_alone(self):
        # The externalization hooks run even when TikZ is not loaded: an undefined \\if in a
        # skipped branch used to give "Extra \\fi" at \\begin{document}.
        self.write("main.tex", "\\documentclass{article}\n\\begin{document}\n"
                   "A sentence that names pgfplots.\n\\end{document}\n")
        figures = accel.Figures(self.doc / "main.tex", self.build, "pdflatex", False, 2)
        run = subprocess.run(
            ["pdflatex", "-interaction=nonstopmode", "-halt-on-error", f"-output-directory={self.build}",
             "-jobname=main", figures.main_pretex + r"\input{main.tex}"],
            cwd=self.doc, stdin=subprocess.DEVNULL, capture_output=True, text=True,
        )
        self.assertEqual(run.returncode, 0, run.stdout[-2000:])

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

    def test_styles_and_macros_defined_in_a_chapter_invalidate_figures(self):
        self.write("main.tex", HEAD + "\\begin{document}\n\\input{ch}\n\\end{document}\n")
        self.write("ch.tex", "\\newcommand{\\lbl}{one}\n" + pic("\\lbl"))
        self.assertEqual(self.render(), ["one"])
        self.write("ch.tex", "\\newcommand{\\lbl}{two}\n" + pic("\\lbl"))
        self.assertEqual(self.render(), ["two"])

    def test_a_file_input_inside_a_picture_invalidates_figures(self):
        self.write("main.tex", HEAD + "\\begin{document}\n\\begin{tikzpicture}\\input{node}\\end{tikzpicture}\n\\end{document}\n")
        self.write("node.tex", "\\node{one};\n")
        self.assertEqual(self.render(), ["one"])
        self.write("node.tex", "\\node{two};\n")
        self.assertEqual(self.render(), ["two"])

    def test_unused_cached_figures_are_deleted(self):
        self.write("main.tex", HEAD + "\\begin{document}\n" + pic("one") + "\\end{document}\n")
        self.render()
        self.write("main.tex", HEAD + "\\begin{document}\n" + pic("two") + "\\end{document}\n")
        self.render()
        self.assertEqual(len(list(self.figures.cache.glob("*.pdf"))), 1)

    def test_figure_jobs_skip_text_files_but_keep_counters_and_definitions(self):
        self.write("a.tex", "\\section{One} Text.\n")
        self.write("b.tex", "\\newcommand{\\lbl}{bee}\\section{Two}\n")
        self.write("c.tex", pic("\\thesection-\\lbl"))
        self.write("main.tex", HEAD + "\\begin{document}\n\\input{a}\\input{b}\\input{c}\n\\end{document}\n")
        self.render()
        self.write("a.tex", "\\section{One} Text, edited.\n")  # Recompile the figure from the recorded tree.
        self.figures.wipe()
        words = self.render()
        skips = self.figures.skips["tikz/main-figure0"]
        self.assertIn("pgfx@s@a\\endcsname", skips)
        self.assertNotIn("pgfx@s@b\\endcsname", skips)
        self.assertIn("2-bee", words)

    def test_partial_listing_gives_the_md5_of_the_full_listing(self):
        self.write("a.tex", "\\section{One} Text.\n")
        self.write("b.tex", pic("one"))
        self.write("c.tex", pic("two"))
        self.write("main.tex", HEAD + "\\begin{document}\n\\input{a}\\input{b}\\input{c}\n\\end{document}\n")
        self.render()
        figures = self.figures
        figures.touched(0.0)  # Snapshot of the figure text.
        quiet = {"stdin": subprocess.DEVNULL, "stdout": subprocess.DEVNULL, "check": True}

        self.write("b.tex", pic("ONE"))
        self.assertTrue(figures.touched(0.0))
        self.assertEqual(figures.dirty, {"b.tex"})
        figures.recorded = time.time()
        self.assertTrue(figures.discover_partial())
        partial = {path.name: path.read_text() for path in (self.build / "tikz").glob("*.md5")}
        subprocess.run(figures.discover_command(), cwd=self.doc, **quiet)
        full = {path.name: path.read_text() for path in (self.build / "tikz").glob("*.md5")}
        self.assertEqual(partial, full)
        self.assertFalse((self.build / "figwork" / "list").exists())

        # A figure more in the file shifts the numbers of the files after it: list everything.
        self.write("b.tex", pic("ONE") + pic("EXTRA"))
        self.assertTrue(figures.touched(0.0))
        figures.recorded = time.time()
        self.assertFalse(figures.discover_partial())

    def test_partial_listing_reads_files_whose_figures_a_macro_draws(self):
        # a.tex draws with a macro (no picture in its text); the macro now draws two pictures.
        head = HEAD + "\\newcommand\\myfig{\\begin{tikzpicture}\\node{m};\\end{tikzpicture}}\n"
        self.write("a.tex", "\\section{One}\\myfig\n")
        self.write("b.tex", pic("bee"))
        self.write("main.tex", head + "\\begin{document}\n\\input{a}\\input{b}\n\\end{document}\n")
        self.render()
        figures = self.figures
        figures.touched(0.0)
        self.write("main.tex", head.replace("\\end{tikzpicture}}", "\\end{tikzpicture}\\tikz\\node{n};}")
                   + "\\begin{document}\n\\input{a}\\input{b}\n\\end{document}\n")
        self.write("b.tex", pic("BEE"))
        self.assertTrue(figures.touched(0.0))
        figures.recorded = time.time() + 10  # a.tex counts as unedited since the map.
        # Skipping a.tex would list b.tex as figure 1 (it is figure 2 now) and pass the names check.
        self.assertFalse(figures.discover_partial())

    def test_partial_listing_does_not_trust_a_stale_map(self):
        # a.tex adds a section, b.tex draws \\thesection: the map's counters are one section behind.
        self.write("a.tex", "\\section{One}\n")
        self.write("b.tex", pic("S\\thesection"))
        self.write("main.tex", HEAD + "\\begin{document}\n\\input{a}\\input{b}\n\\end{document}\n")
        self.render()
        figures = self.figures
        figures.mark_recorded()  # What build.py does before the run that wrote the map.
        old = figures.recorded
        figures.touched(0.0)
        self.write("a.tex", "\\section{One}\\section{Two}\n")
        self.write("b.tex", pic("S\\thesection!"))
        self.assertTrue(figures.touched(0.0))
        self.assertEqual(figures.load_recorded(), old)  # A partial listing leaves the time alone.
        os.utime(self.doc / "a.tex", (old + 5, old + 5))  # Edited after the map was written.
        os.utime(self.doc / "b.tex", (old + 5, old + 5))
        # Both files were edited after the map: nothing may be skipped, so list everything.
        self.assertFalse(figures.discover_partial())
        figures.plan_skips()
        self.assertEqual(figures.skips, {})  # Files newer than the map: no figure job skips anything.

    def test_focus_counters_tolerate_undefined_names(self):
        self.assertIn("ifcsname c@#1\\endcsname", accel.FIGSKIP)
        self.assertIn("ifcsname c@#1\\endcsname", accel.FOCUS)

    def test_pictures_that_need_the_page_disable_externalization(self):
        self.write("main.tex", HEAD + "\\begin{document}\n\\begin{tikzpicture}[remember picture,overlay]"
                   "\\node{x};\\end{tikzpicture}\n\\end{document}\n")
        self.assertFalse(accel.uses_tikz(self.doc))


if __name__ == "__main__":
    unittest.main()
