"""hints.py: every rule is hit by a realistic log line, and overfull boxes parse."""

import re
import unittest

import _support  # noqa: F401 - puts scripts/ on sys.path
import hints

# One realistic log line per rule in hints.RULES, in table order.
SAMPLES = [
    ("! Undefined control sequence.", "l.12 \\foo"),
    ("! Missing $ inserted.", "l.4 x_1"),
    ("! LaTeX Error: File `tikz.sty' not found.", ""),
    ("! Runaway argument?", "{Some text \\par"),
    ("! Missing \\begin{document}.", ""),
    ("! LaTeX Error: Environment tikzpicture undefined.", ""),
    ("! Too many }'s.", "l.20 }"),
    ("! Extra alignment tab has been changed to \\cr.", ""),
    ("! Misplaced alignment tab character &.", "l.9 a & b"),
    ("LaTeX Warning: Citation `smith2020' on page 2 undefined on input line 14.", ""),
    ("LaTeX Warning: Reference `fig:x' on page 3 undefined on input line 9.", ""),
    ("LaTeX Warning: There were undefined references.", ""),
    ("LaTeX Warning: Label `sec:a' multiply defined.", ""),
    ("Overfull \\hbox (12.3pt too wide) in paragraph at lines 10--12", ""),
    ("Underfull \\hbox (badness 10000) in paragraph at lines 5--6", ""),
    ("LaTeX Warning: Float too large for page by 40.0pt on input line 12.", ""),
    ("LaTeX Font Warning: Font shape `OT1/cmr/bx/sc' undefined", ""),
    ("! LaTeX Error: Option clash for package xcolor.", ""),
    ("! Dimension too large.", ""),
    ("! TeX capacity exceeded, sorry [main memory size=5000000].", ""),
    ("! Emergency stop.", "*** (job aborted, no legal \\end found)"),
    ("! Package inputenc Error: Unicode character \u00e9 (U+E9)", "(inputenc) not set up for use with LaTeX."),
    ("! Package inputenc Error: Keyboard character used is undefined", ""),
    ("! I can't write on file `main.aux'.", ""),
    ("! Paragraph ended before \\caption was complete.", ""),
    ("! Missing number, treated as zero.", "l.7 \\hspace{}"),
    ("! Illegal unit of measure (pt inserted).", "l.8 \\setlength{\\parindent}{2px}"),
    ("! Lonely \\item--perhaps a missing list?", ""),
    ("! LaTeX Error: Command \\foo already defined.", ""),
    ("! Missing } inserted.", ""),
    ("! Double superscript.", "l.11 x^a^b"),
    ("Warning--I didn't find a database entry for \"smith\"", ""),
    ("I couldn't open database file refs.bib", ""),
    ("LaTeX Warning: Label(s) may have changed. Rerun to get cross-references right.", ""),
]


def matched_rule(message):
    """Index of the first RULES entry that matches, the same order explain() uses."""
    for index, (pattern, _hint) in enumerate(hints.RULES):
        if pattern.search(message):
            return index
    return None


class ExplainTests(unittest.TestCase):
    def test_every_rule_is_hit_by_a_sample(self):
        hit = {matched_rule(message) for message, _context in SAMPLES}
        self.assertEqual(hit, set(range(len(hints.RULES))))

    def test_every_sample_gets_a_hint(self):
        for message, context in SAMPLES:
            with self.subTest(message=message):
                hint = hints.explain(message, context)
                self.assertTrue(hint, "no hint")
                self.assertLessEqual(len(re.split(r"(?<=[.!?])\s", hint)), 2, "hints are 1-2 sentences")

    def test_known_sty_names_its_package(self):
        hint = hints.explain("! LaTeX Error: File `siunitx.sty' not found.")
        self.assertIn("texlive-science", hint)

    def test_unknown_sty_points_at_search_tools(self):
        hint = hints.explain("! LaTeX Error: File `nosuch.sty' not found.")
        self.assertIn("tlmgr search --global --file nosuch.sty", hint)
        self.assertIn("apt-file search nosuch.sty", hint)

    def test_context_is_used_when_message_has_no_match(self):
        self.assertIsNotNone(hints.explain("! Error.", "! Undefined control sequence."))

    def test_no_match_returns_none(self):
        self.assertIsNone(hints.explain("Package hyperref Info: nothing wrong here."))


class OverfullTests(unittest.TestCase):
    LOG = (
        "(./main.tex (./Chapters/a.tex (font) text)\n"
        "Overfull \\hbox (12.3pt too wide) in paragraph at lines 10--12\n"
        ") Overfull \\hbox (4pt too wide) detected at line 7\n"
        "(./Chapters/b.tex [1] Overfull \\hbox (25.0pt too wide) in alignment at lines 3--4\n"
        "Underfull \\hbox (badness 10000) in paragraph at lines 1--2\n"
    )

    def test_parses_file_line_and_points(self):
        self.assertEqual(hints.overfull_boxes(self.LOG), [
            ("main.tex", 10, 12.3),
            (None, 7, 4.0),
            ("Chapters/b.tex", 3, 25.0),
        ])

    def test_no_overfull_boxes(self):
        self.assertEqual(hints.overfull_boxes("Underfull \\hbox (badness 10000) at lines 1--2"), [])


if __name__ == "__main__":
    unittest.main()
