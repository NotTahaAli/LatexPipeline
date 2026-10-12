"""
The external programs, really run: one synthetic document is built once with latexmk, then each feature that calls
a tool works on it. Every test is skipped when its tool is missing, so CI (apt TeX Live, no qpdf, mupdf, dvisvgm or
veraPDF) runs what it has. Locally: apt-get install dvisvgm qpdf mupdf-tools; veraPDF as in the tagged-pdf job.

Covered: make4ht + dvisvgm (TikZ as SVG in --html), qpdf (--pages), pdftoppm and mutool (MCP render_page), latexdiff
(build_diff_pdf), the arXiv zip (compiled again on its own), the cited-only .bib, --deps (BibTeX and biber sources),
--query with texcount, --preflight with pdffonts/pdfinfo, chktex and texcount in ci_report, tex-fmt (--format),
veraPDF (pdfa = "2b").
"""

from __future__ import annotations

import contextlib
import io
import json
import os
import shutil
import struct
import subprocess
import tempfile
import unittest
import zipfile
from pathlib import Path
from unittest import mock

import _support  # noqa: F401 - puts scripts/ on sys.path
import citedbib
import doctools
import mcp_tools
import sandbox
from _support import build, ci_report, fake_repo, write_doc

LATEX = bool(shutil.which("latexmk") and shutil.which("pdflatex") and shutil.which("bibtex"))
MAIN = r"""\documentclass{article}
\usepackage{tikz}
\usepackage{hyperref}
\title{Integration Demo}
\author{Test Author}
\begin{document}
\maketitle
\tableofcontents
\section{Intro}\label{sec:intro}
Some text citing \cite{knuth84}. See Figure~\ref{fig:a} and $E = mc^2$.
\begin{figure}[h]
\centering
\begin{tikzpicture}\draw[blue] (0,0) circle (1); \node at (0,0) {A};\end{tikzpicture}
\caption{A circle}\label{fig:a}
\end{figure}
\input{part}
\newpage
\section{Third}
Last page.
\bibliographystyle{plain}
\bibliography{refs}
\end{document}
"""
PART = "\\newpage\n\\section{Second}\nMore words here.\n"
BIB = """@book{knuth84,
  author = {Donald E. Knuth},
  title = {The {\\TeX}book},
  publisher = {Addison-Wesley},
  year = {1984},
}
@article{unused99,
  author = {Nobody},
  title = {Not cited},
  journal = {J},
  year = {1999},
}
"""


def quiet():
    return contextlib.redirect_stdout(io.StringIO())


def png_size(data: bytes) -> tuple[int, int]:
    return struct.unpack(">II", data[16:24])


def page_count(pdf: Path) -> int:
    if shutil.which("qpdf"):
        return int(subprocess.run(["qpdf", "--show-npages", str(pdf)], capture_output=True, text=True).stdout)
    return int(next(line.split()[-1] for line in subprocess.run(["pdfinfo", str(pdf)], capture_output=True,
                                                                     text=True).stdout.splitlines()
                    if line.startswith("Pages:")))


@unittest.skipUnless(LATEX, "needs latexmk, pdflatex and bibtex")
class Built(unittest.TestCase):
    """A three-page document with a TikZ picture, a label, an \\input and one cited of two .bib entries."""

    @classmethod
    def setUpClass(cls):
        cls.stack = contextlib.ExitStack()
        cls.root = cls.stack.enter_context(fake_repo())
        cls.main = write_doc(cls.root, "demo", MAIN)
        (cls.main.parent / "part.tex").write_text(PART, encoding="utf-8")
        (cls.main.parent / "refs.bib").write_text(BIB, encoding="utf-8")
        with quiet():
            cls.entry, _ = build.build_document(cls.main, shutil.which("latexmk"), live=False, validate=False)
        cls.pdf = build.output_path_for(cls.main)

    @classmethod
    def tearDownClass(cls):
        cls.stack.close()

    def test_the_build(self):
        self.assertTrue(self.entry["ok"], self.entry)
        self.assertEqual(self.entry["pages"], 3)

    @unittest.skipUnless(shutil.which("qpdf"), "needs qpdf")
    def test_pages_with_qpdf(self):
        target = self.pdf.with_name("demo.pages.pdf")
        self.assertIsNone(doctools.extract_pages(self.pdf, "1,3", target))
        self.assertEqual(page_count(target), 2)
        self.assertIn("qpdf failed", doctools.extract_pages(self.pdf, "7", target))  # past the end

    def render(self, tool: str, size: int = 300) -> bytes:
        other = "mutool" if tool == "pdftoppm" else "pdftoppm"
        real = shutil.which
        with mock.patch.object(mcp_tools.shutil, "which", side_effect=lambda n: None if n == other else real(n)):
            return mcp_tools.render_png(self.pdf, 2, size)

    def check_renderers(self):
        for tool in ("pdftoppm", "mutool"):
            if not shutil.which(tool):
                continue
            with self.subTest(tool=tool):
                data = self.render(tool)
                self.assertTrue(data.startswith(b"\x89PNG"))
                self.assertEqual(max(png_size(data)), 300)  # size is the longest side, with either program

    @unittest.skipUnless(shutil.which("pdftoppm") or shutil.which("mutool"), "needs pdftoppm or mutool")
    def test_page_images_for_mcp(self):
        self.check_renderers()

    @unittest.skipUnless((shutil.which("pdftoppm") or shutil.which("mutool")) and shutil.which("bwrap")
                         and sandbox.problem() is None, "needs bwrap and pdftoppm or mutool")
    def test_page_images_in_the_sandbox(self):
        with mock.patch.dict(os.environ, {"LATEX_SANDBOX": "bwrap"}):
            self.check_renderers()

    @unittest.skipUnless(shutil.which("make4ht") and shutil.which("dvisvgm"), "needs make4ht and dvisvgm")
    def test_html_with_tikz_as_svg(self):
        with quiet():
            ok, message = build.export_html(self.main)
        self.assertTrue(ok, message)
        folder, archive = build.html_paths(self.main)
        page = (folder / "index.html").read_text(encoding="utf-8")
        svgs = sorted(folder.glob("*.svg"))
        self.assertTrue(svgs, list(folder.iterdir()))
        self.assertTrue(any("dvisvgm" in svg.read_text(encoding="utf-8") for svg in svgs))
        self.assertTrue(any(f"src='{svg.name}'" in page or f'src="{svg.name}"' in page for svg in svgs))
        self.assertIn("<math", page)
        self.assertNotIn("cdn", page.lower())
        with zipfile.ZipFile(archive) as bundle:
            self.assertTrue(any(name.endswith(".svg") for name in bundle.namelist()))

    @unittest.skipUnless(shutil.which("latexdiff"), "needs latexdiff")
    def test_latexdiff_pdf(self):
        old = {"main.tex": MAIN.replace("Last page.", "Final page."), "part.tex": PART.replace("More", "Fewer")}
        with quiet():
            result = build.build_diff_pdf(self.main, shutil.which("latexmk"), old, live=False)
        self.assertTrue(result["ok"], result)
        pdf, _ = build.diff_paths(self.main)
        self.assertTrue(pdf.is_file())
        if shutil.which("pdftotext"):
            text = subprocess.run(["pdftotext", str(pdf), "-"], capture_output=True, text=True).stdout
            self.assertIn("Final", text)
            self.assertIn("Last", text)
            self.assertIn("Fewer", text)  # the \input was flattened

    def test_arxiv_zip_compiles_on_its_own(self):
        findings, lines = build.export_arxiv(self.main)
        self.assertIsNotNone(findings, lines)
        with zipfile.ZipFile(build.arxiv_path_for(self.main)) as bundle:
            names = sorted(bundle.namelist())
            self.assertEqual(names, ["main.bbl", "main.tex", "part.tex"])
            with tempfile.TemporaryDirectory() as tmp:
                work = Path(tmp).resolve()
                bundle.extractall(work)
                for _ in range(2):  # no BibTeX: the .bbl is in the archive
                    run = sandbox.spawn(["pdflatex", "-interaction=nonstopmode", "-no-shell-escape", "main.tex"],
                                        work, work, cpu=120)
                    try:
                        subprocess.run(run.pop("args"), **run, capture_output=True, timeout=180)
                    finally:
                        sandbox.scrub(work)
                log = (work / "main.log").read_text(encoding="utf-8", errors="replace")
                self.assertTrue((work / "main.pdf").is_file(), log[-2000:])
                self.assertNotIn("undefined", log)

    def test_cited_bib(self):
        result, lines = build.export_cited_bib(self.main)
        self.assertIsNotNone(result, lines)
        self.assertEqual(result["keys"], ["knuth84"])
        text = build.cited_bib_path_for(self.main).read_text(encoding="utf-8")
        self.assertIn("@book{knuth84", text)
        self.assertNotIn("unused99", text)
        self.assertEqual(citedbib.export(self.main.parent, build.cache_dir_for(self.main), "main")["keys"],
                         ["knuth84"])

    def test_deps_list_the_bib_file_bibtex_read(self):
        """Regression: BibTeX is not in the .fls, so refs.bib was missing from --deps and an outside .bib from
        rebuild detection."""
        inputs = {p.name for p in build.recorded_inputs(self.main, everything=True)}
        self.assertEqual(inputs, {"main.tex", "part.tex", "refs.bib"})
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            self.assertEqual(build.deps_all([self.main], "json"), 0)
        self.assertEqual({Path(p).name for p in json.loads(out.getvalue())["inputs"]},
                         {"main.tex", "part.tex", "refs.bib"})

    def test_query_with_texcount_and_preflight(self):
        found = doctools.query(self.main, build.cache_dir_for(self.main), 3)
        self.assertEqual((found["title"], found["author"]), ("Integration Demo", "Test Author"))
        self.assertEqual({label["key"]: label["number"] for label in found["labels"]},
                         {"sec:intro": "1", "fig:a": "1"})
        self.assertEqual([s["title"] for s in found["sections"]], ["Intro", "Second", "Third"])
        if shutil.which("texcount"):
            self.assertGreater(found["words"], 10)  # part.tex counted too (-inc)
            self.assertEqual(found["words"], ci_report.count_words(self.main.parent))
        else:
            self.assertIsNone(found["words"])
        if shutil.which("pdffonts") and shutil.which("pdfinfo"):
            levels = doctools.preflight(self.pdf, build.pdf_text(self.pdf))
            self.assertNotIn("error", [level for level, _ in levels])
            self.assertIn(("info", "page size 612 x 792 pts (letter)"), levels)
            self.assertTrue(any("bookmarks" in message for _, message in levels))  # hyperref


@unittest.skipUnless(shutil.which("latexmk") and shutil.which("pdflatex") and shutil.which("biber"),
                     "needs latexmk, pdflatex and biber")
class BiberDeps(unittest.TestCase):
    def test_deps_and_rebuild_detection_see_a_bib_file_outside(self):
        with fake_repo() as root:
            shared = root / "shared"
            shared.mkdir()
            (shared / "refs.bib").write_text(BIB, encoding="utf-8")
            main = write_doc(root, "bl", "\\documentclass{article}\\usepackage[backend=biber]{biblatex}\n"
                                         "\\addbibresource{../../shared/refs.bib}\n\\begin{document}\n"
                                         "\\cite{knuth84}\\printbibliography\\end{document}\n")
            with quiet():
                entry, _ = build.build_document(main, shutil.which("latexmk"), live=False, validate=False)
            self.assertTrue(entry["ok"], entry)
            outside = build.recorded_inputs(main)
            self.assertIn((shared / "refs.bib").resolve(), [p.resolve() for p in outside])
            before = build.newest_input(main)
            os.utime(shared / "refs.bib", (before + 100, before + 100))
            self.assertEqual(build.newest_input(main), before + 100)  # an edit there rebuilds the document
            findings, lines = build.export_arxiv(main)  # arXiv needs the .bbl, not the .bib it was made from
            self.assertFalse([f for f in findings if "outside the document directory" in f["message"]], lines)


@unittest.skipUnless(shutil.which("chktex"), "needs chktex")
class Chktex(unittest.TestCase):
    def test_warnings_per_file_and_line(self):
        with tempfile.TemporaryDirectory() as tmp:
            folder = Path(tmp)
            (folder / "main.tex").write_text("\\documentclass{article}\n\\begin{document}\nSee section \\ref{x}."
                                             " Dots ... here\n\\input{b}\n\\end{document}\n", encoding="utf-8")
            (folder / "b.tex").write_text("Fine text.\n", encoding="utf-8")
            found = ci_report.run_chktex(folder)
        self.assertTrue(found)
        self.assertTrue(all(line.startswith("main.tex:3: ") for line in found), found)
        self.assertTrue(any("\\ldots" in line for line in found))


@unittest.skipUnless(shutil.which("tex-fmt"), "needs tex-fmt")
class TexFmt(unittest.TestCase):
    def test_format_check_then_write(self):
        with fake_repo() as root:
            main = write_doc(root, "f", "\\documentclass{article}\n\\begin{document}\n\\begin{itemize}\n"
                                        "\\item a\n      \\item b\n\\end{itemize}\n\\end{document}\n")
            with quiet():
                self.assertEqual(build.format_all([main], check=True), 1)
                self.assertIn("      \\item b", main.read_text())  # --check changes nothing
                self.assertEqual(build.format_all([main], check=False), 0)
                self.assertEqual(build.format_all([main], check=True), 0)
            self.assertIn("  \\item b", main.read_text())


@unittest.skipUnless(shutil.which("verapdf") and shutil.which("latexmk") and shutil.which("pdflatex"),
                     "needs veraPDF, latexmk and pdflatex")
class VeraPdf(unittest.TestCase):
    def test_pdfa_2b_is_declared_and_validated(self):
        with fake_repo() as root:
            main = write_doc(root, "a", "\\documentclass{article}\n\\usepackage{hyperref}\n"
                                        "\\hypersetup{pdftitle={A}}\n\\begin{document}\nHello $x^2$.\n"
                                        "\\end{document}\n", 'pdfa = "2b"\n')
            with quiet():
                entry, _ = build.build_document(main, shutil.which("latexmk"), live=False)
            self.assertTrue(entry["ok"], entry)
            log = build.log_path_for(main).read_text(encoding="utf-8", errors="replace")
            self.assertIn("veraPDF passed.", log)
            self.assertNotIn("veraPDF failed", log)


if __name__ == "__main__":
    unittest.main()
