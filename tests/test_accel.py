"""Pure logic in scripts/accel.py: the recorded \\input tree and --focus selection."""

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


if __name__ == "__main__":
    unittest.main()
