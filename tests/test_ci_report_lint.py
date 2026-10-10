"""ci_report.py lint: structural findings on a throwaway document tree, and the baseline."""

import argparse
import contextlib
import io
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from _support import ci_report


def tree(files):
    """A temporary files/ tree: {"doc/main.tex": text, ...}. Returns (root, files dir)."""
    root = tempfile.TemporaryDirectory()
    base = Path(root.name) / "files"
    for rel, text in files.items():
        path = base / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        if isinstance(text, bytes):
            path.write_bytes(text)
        else:
            path.write_text(text, encoding="utf-8")
    return root, base


def kinds(findings):
    return sorted((finding.kind, finding.subject or finding.path) for finding in findings)


class LintDocumentTests(unittest.TestCase):
    def lint(self, files, doc="doc"):
        root, base = tree(files)
        self.addCleanup(root.cleanup)
        return ci_report.lint_document(base / doc, None, 10.0)

    def test_labels_refs_and_comments(self):
        findings = self.lint({
            "doc/main.tex": (
                "\\input{chap}\n"
                "\\section{Intro}\\label{sec:intro} % \\label{sec:commented}\n"
                "See \\ref{sec:intro}, \\cref{fig:a,fig:missing} and \\eqref{eq:gone}. % \\ref{fig:c}\n"
                "100\\% sure \\label{sec:pct}\n"
                "\\label{fig:\\thechapter}\n"
                "\\begin{lstlisting}[label={lst:code}]\n\\end{lstlisting}\\cref{lst:code}\n"
            ),
            "doc/chap.tex": "\\section{Chap}\\label{sec:chap}\n",
        })
        self.assertEqual(kinds(findings), [
            ("undefined-ref", "eq:gone"),
            ("undefined-ref", "fig:a"),
            ("undefined-ref", "fig:missing"),
            ("unused-label", "sec:chap"),
            ("unused-label", "sec:pct"),
        ])
        by_subject = {finding.subject: finding for finding in findings}
        self.assertEqual((by_subject["eq:gone"].path, by_subject["eq:gone"].line), ("main.tex", 3))
        self.assertEqual(by_subject["sec:chap"].path, "chap.tex")

    def test_unused_figure_matches_with_and_without_extension(self):
        findings = self.lint({
            "doc/main.tex": "\\includegraphics[width=2cm]{Figures/a}\n\\includegraphics{b.pdf}\n",
            "doc/Figures/a.png": b"",
            "doc/Figures/b.pdf": b"",
            "doc/Figures/c.pdf": b"",
            "doc/.lint-baseline": "",
        })
        self.assertEqual(kinds(findings), [("unused-figure", "Figures/c.pdf")])

    def test_bib_duplicates_uncited_and_missing_doi(self):
        findings = self.lint({
            "doc/main.tex": "\\cite{smith}\\citep[p.~3]{jones,lee}\n\\bibliography{refs}\n",
            "doc/refs.bib": (
                "@article{smith, title={A}, doi={10.1/x}}\n"
                "@article{jones, title={B}}\n"
                "@comment{ignored, not an entry}\n"
                "@book{lee, title={C}}\n"
                "@book{unused, title={D}}\n"
            ),
            "doc/extra.bib": "@misc{smith, title={E}}\n",
        })
        findings = [f for f in findings if f.kind != "missing-bib-field"]
        self.assertEqual(kinds(findings), [
            ("dup-bib-key", "smith"),
            ("missing-doi", "jones"),
            ("uncited-bib", "unused"),
            ("unused-bib-file", "extra.bib"),
        ])
        missing = next(f for f in findings if f.kind == "missing-doi")
        self.assertEqual(missing.level, "info")

    def test_bib_entry_checks(self):
        findings = self.lint({
            "doc/main.tex": "\\cite{a,b,c,d,e,f}\n\\bibliography{refs}\n",
            "doc/refs.bib": (
                "@article{a, author={X}, title={Fine {BERT} one}, journal={J}, year={2020}, doi={10.1/Z}}\n"
                "@article{b, author={X}, title={Deep BERT models}, journal={J}, year={20x0},\n"
                "  doi={https://doi.org/10.1/z}}\n"
                "@book{c, editor={X}, title={T = x}, year={1999}}\n"
                "@inproceedings{d, author={X}, title={T}, booktitle={B}, year={1200}}\n"
                "@article{e, crossref={a}}\n"
                "@misc{f, title={Anything}}\n"
            ),
            "doc/old.bib": "@misc{g, title={x}}\n",
            "doc/.lint-baseline": "",
        })
        self.assertEqual(sorted(kinds(findings)), [
            ("bib-title-case", "b"),
            ("bib-year", "b"),
            ("bib-year", "d"),
            ("dup-doi", "b"),
            ("missing-bib-field", "c"),
            ("missing-doi", "e"),  # a crossref entry is exempt from required fields, not from this
            ("uncited-bib", "g"),
            ("unused-bib-file", "old.bib"),
        ])
        self.assertIn("publisher", next(f for f in findings if f.kind == "missing-bib-field").message)
        self.assertEqual(next(f for f in findings if f.kind == "bib-title-case").level, "info")

    def test_nocite_all_cites_every_entry(self):
        findings = self.lint({
            "doc/main.tex": "\\nocite{*}\n\\bibliography{refs}\n",
            "doc/refs.bib": "@misc{unused, title={D}}\n",
        })
        self.assertEqual(kinds(findings), [])

    def test_inputs_without_extension_and_fypinput_are_followed(self):
        findings = self.lint({
            "doc/main.tex": "\\fypinput{ch}{Chapters/one}\n\\input{two}\n",
            "doc/Chapters/one.tex": "\\label{sec:one}\n",
            "doc/two.tex": "\\ref{sec:one}\n",
        })
        self.assertEqual(kinds(findings), [])

    def test_overfull_boxes_from_log_over_budget(self):
        root, base = tree({"doc/main.tex": "x\n"})
        self.addCleanup(root.cleanup)
        log = Path(root.name) / "doc.log"
        log.write_text("(./main.tex\nOverfull \\hbox (12.3pt too wide) in paragraph at lines 10--12\n"
                       "Overfull \\hbox (4pt too wide) detected at line 20\n)", encoding="utf-8")
        findings = ci_report.lint_document(base / "doc", log, 10.0)
        self.assertEqual([(f.kind, f.path, f.line) for f in findings], [("overfull", "main.tex", 10)])


class LintCommandTests(unittest.TestCase):
    """Baseline, --strict and --update-baseline through cmd_lint on a temporary tree."""

    def setUp(self):
        root, base = tree({
            "doc/main.tex": "\\label{sec:unused}\n",
            "doc/Figures/old.pdf": b"",
        })
        self.addCleanup(root.cleanup)
        out = Path(root.name) / "out"
        patches = [
            mock.patch.object(ci_report, "SOURCE_DIR", base),
            mock.patch.object(ci_report, "OUT_DIR", out),
            mock.patch.object(ci_report, "LINT_PATH", out / "lint-report.json"),
            mock.patch.object(ci_report, "REPORT_PATH", Path(root.name) / "missing.json"),
            mock.patch.object(ci_report.shutil, "which", return_value=None),
        ]
        for patch in patches:
            patch.start()
            self.addCleanup(patch.stop)
        self.doc = base / "doc"

    def run_lint(self, **options):
        defaults = {"documents": [], "strict": False, "update_baseline": False, "overfull_pt": 10.0}
        args = argparse.Namespace(**{**defaults, **options})
        with contextlib.redirect_stdout(io.StringIO()) as out:
            code = ci_report.cmd_lint(args)
        return code, out.getvalue()

    def test_strict_fails_then_baseline_suppresses(self):
        code, output = self.run_lint(strict=True)
        self.assertEqual(code, 1)
        self.assertIn("lint=2", output)

        code, _ = self.run_lint(update_baseline=True)
        self.assertEqual(code, 0)
        self.assertEqual(
            (self.doc / ci_report.BASELINE_NAME).read_text(encoding="utf-8").split(),
            ["unused-figure:Figures/old.pdf", "unused-label:main.tex:sec:unused"],
        )

        code, output = self.run_lint(strict=True)
        self.assertEqual(code, 0)
        self.assertIn("lint=0 suppressed=2", output)

    def test_new_finding_still_reported_with_baseline(self):
        self.run_lint(update_baseline=True)
        (self.doc / "main.tex").write_text("\\label{sec:unused}\\label{sec:new}\n", encoding="utf-8")
        code, output = self.run_lint(strict=True)
        self.assertEqual(code, 1)
        self.assertIn("lint=1 suppressed=2", output)

    def test_github_annotations_are_capped_and_escaped(self):
        findings = [ci_report.Finding("unused-label", "a,b.tex", n, "x: 100%", str(n))
                    for n in range(ci_report.LINT_ANNOTATION_LIMIT + 2)]
        with mock.patch.dict("os.environ", {"GITHUB_ACTIONS": "true"}), \
                contextlib.redirect_stdout(io.StringIO()) as out:
            ci_report.annotate("doc", findings)
        lines = out.getvalue().splitlines()
        self.assertEqual(lines[0], "::warning file=files/doc/a%2Cb.tex,line=0::x: 100%25")
        self.assertEqual(sum(line.startswith("::warning") for line in lines),
                         ci_report.LINT_ANNOTATION_LIMIT)
        self.assertIn("2 more findings", lines[-1])


if __name__ == "__main__":
    unittest.main()
