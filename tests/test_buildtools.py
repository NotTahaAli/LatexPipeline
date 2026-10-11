"""build.py's draft, strict, anonymous, [[output]], cancel, --quiet-log and timing; doctools.py; bibtidy.py."""

import contextlib
import importlib
import io
import json
import os
import shutil
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

from _support import build, fake_repo, write_doc

bibtidy = importlib.import_module("bibtidy")  # after _support, which puts scripts/ on sys.path
doctools = importlib.import_module("doctools")

PLAIN = "\\documentclass{article}\n\\begin{document}x\\end{document}\n"


class SettingsTests(unittest.TestCase):
    def settings(self, toml):
        with fake_repo() as root:
            return build.read_settings(write_doc(root, "doc", PLAIN, toml))

    def test_new_keys_and_outputs(self):
        found = self.settings('draft = true\nstrict = true\nanonymous = true\n'
                              '[[output]]\nname = "handout"\ndefine = ["handout", "slides"]\nengine = "XeLaTeX"\n'
                              '[[output]]\nname = "camera_ready"\nanonymous = false\n')
        self.assertEqual((found["draft"], found["strict"], found["anonymous"]), (True, True, True))
        self.assertEqual([output["name"] for output in found["output"]], ["handout", "camera_ready"])
        merged = build.output_settings(found, "handout")
        self.assertEqual((merged["define"], merged["output"], merged["draft"]), (["handout", "slides"], [], True))
        self.assertFalse(build.output_settings(found, "camera_ready")["anonymous"])

    def test_invalid_values_fail_the_document(self):
        for toml in ('draft = "yes"\n', 'strict = 1\n', 'define = ["x"]\n', 'output = 3\n',
                     '[[output]]\ndefine = ["x"]\n',  # no name
                     '[[output]]\nname = "a b"\n',
                     '[[output]]\nname = "a"\n[[output]]\nname = "a"\n',
                     '[[output]]\nname = "a"\ndefine = ["x1"]\n',  # \def\x1 would be \x followed by 1
                     '[[output]]\nname = "a"\ndefine = "x"\n',
                     '[[output]]\nname = "a"\nengine = "tex"\n',
                     '[[output]]\nname = "a"\nlatexmk_args = ["-shell-escape"]\n'):
            with self.subTest(toml=toml), self.assertRaises(build.ConfigError):
                self.settings(toml)

    def test_pretex(self):
        base = {"review": "off"}
        self.assertEqual(build.review_pretex(base), "")  # off stays byte-identical
        self.assertEqual(build.review_pretex({**base, "draft": True, "define": ["handout"]}),
                         "\\PassOptionsToPackage{draft}{graphicx}\\def\\handout{}")

    def test_draft_flag_switches_a_built_document(self):
        with fake_repo() as root:
            main = write_doc(root, "doc", PLAIN)
            cache = build.cache_dir_for(main)
            cache.mkdir(parents=True)
            (cache / ".mode").write_text("plain")
            self.assertFalse(build.review_switched(main))
            with mock.patch.object(build, "DRAFT_MODE", True):
                self.assertTrue(build.review_switched(main))
            (cache / ".mode").write_text("plain\\PassOptionsToPackage{draft}{graphicx}")
            self.assertTrue(build.review_switched(main))  # built with --draft, now without


class OutputTests(unittest.TestCase):
    def test_targets_prune_and_build(self):
        with fake_repo() as root:
            main = write_doc(root, "doc", PLAIN, '[[output]]\nname = "handout"\ndefine = ["handout"]\n')
            ((name, work, pdf),) = build.output_targets(main)
            self.assertEqual((name, pdf), ("handout", root / "out" / "doc-handout.pdf"))
            self.assertEqual(work, root / ".latex-cache" / "_output" / "doc" / "handout")
            out = root / "out"
            out.mkdir()
            for file in ("doc.pdf", "doc-handout.pdf", "doc-handout.log", "doc.pages.pdf", "doc-old.pdf"):
                (out / file).write_bytes(b"%PDF")
            with contextlib.redirect_stdout(io.StringIO()):
                build.prune([main])
            self.assertEqual(sorted(p.name for p in out.iterdir()),
                             ["doc-handout.log", "doc-handout.pdf", "doc.pages.pdf", "doc.pdf"])

            calls = []

            def fake(main_tex, latexmk, live=True, force=False, fig_jobs=1, record=False, validate=True,
                     review=None, target=None, output=None):
                calls.append(output)
                pdf = target[1] if target else build.output_path_for(main_tex)
                return {"name": "doc", "pdf": pdf.relative_to(out).as_posix(), "log": "x.log", "ok": output is None,
                        "seconds": 1.0, "engine": "pdflatex", "pages": 1, "size": 1, "errors": [], "warnings": 0,
                        "error": None, "phases": {}}, f"text {output}\n"

            with mock.patch.object(build, "build_document", side_effect=fake):
                entry, text = build.build_safely(main, "latexmk", False, False, outputs=True)
                self.assertEqual(calls, [None, "handout"])
                self.assertFalse(entry["ok"])  # the output failed, so the document did
                self.assertEqual(entry["error"], "output handout failed (see its log)")
                self.assertEqual(entry["outputs"][0]["pdf"], "doc-handout.pdf")
                self.assertEqual(text, "text None\ntext handout\n")
                calls.clear()
                entry, _ = build.build_safely(main, "latexmk", False, False)  # the editor: main PDF only
                self.assertEqual((calls, entry["ok"], "outputs" in entry), ([None], True, False))

    def test_an_output_may_not_take_another_documents_pdf(self):
        with fake_repo() as root:
            main = write_doc(root, "a", PLAIN, '[[output]]\nname = "b"\n')
            write_doc(root, "a-b", PLAIN)
            with mock.patch.object(build, "build_document") as fake, contextlib.redirect_stderr(io.StringIO()):
                found, _ = build.build_outputs(main, "latexmk", False, False, 1, False)
            fake.assert_not_called()
            self.assertIn("is the PDF of another document", found[0]["error"])


class StrictAndAnonymousTests(unittest.TestCase):
    def test_strict_problems(self):
        log = ("LaTeX Warning: Reference `sec:x' on page 1 undefined on input line 9.\n"
               "Package natbib Warning: Citation `k' on page 2 undefined on input line 3.\n"
               "LaTeX Warning: Label `a' multiply defined.\n"
               "LaTeX Warning: Reference `sec:x' on page 1 undefined on input line 9.\n"
               "LaTeX Font Warning: Font shape `OT1/cmr/m/n' undefined\n"
               "(./ch.tex\nOverfull \\hbox (0.5pt too wide) in paragraph at lines 3--4\n"
               "Overfull \\hbox (12.0pt too wide) in paragraph at lines 7--8\n)\n")
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "main.log"
            path.write_text(log, encoding="utf-8")
            self.assertEqual(build.strict_problems(path), [
                "LaTeX Warning: Reference `sec:x' on page 1 undefined on input line 9.",
                "Package natbib Warning: Citation `k' on page 2 undefined on input line 3.",
                "LaTeX Warning: Label `a' multiply defined.",
                "ch.tex:7: Overfull box 12pt too wide",
            ])
            self.assertEqual(build.strict_problems(Path(tmp) / "missing.log"), [])

    def test_anonymity_findings(self):
        with tempfile.TemporaryDirectory() as tmp:
            folder = Path(tmp)
            (folder / "main.tex").write_text(
                "\\author{Anonymous}\n\\author{Jane Doe}\n% \\author{Hidden} me@example.org\n"
                "Write to jane@example.org.\n\\section*{Acknowledgments}\n"
                "As our previous work showed, \\cite{x}.\n100\\% sure, a@b.cd\n", encoding="utf-8")
            (folder / ".git").mkdir()
            (folder / ".git" / "x.tex").write_text("\\author{Git}\n", encoding="utf-8")
            self.assertEqual(build.anonymity_findings(folder), [
                "main.tex:2: author information (\\author, \\affiliation, \\email or \\thanks)",
                "main.tex:4: an e-mail address",
                "main.tex:7: an e-mail address",
                "main.tex:5: an acknowledgements section",
                "main.tex:6: a self-citation phrase",
            ])


SAMPLE_LOG = """This is pdfTeX, Version 3.141592653
(./main.tex
LaTeX2e <2023-11-01>
LaTeX Warning: You have requested package `x', but the package provides `y'.
./main.tex:12: Undefined control sequence.
l.12 \\foo
Package hyperref Warning: Token not allowed in a PDF string
removed on input line 3.
Overfull \\hbox (3.2pt too wide) in paragraph at lines 4--5
[1] [2]
Output written on main.pdf (2 pages).
===== LaTeX log (main.log) =====
plain line
"""


class QuietLogTests(unittest.TestCase):
    EXPECTED = ["./main.tex:12: Undefined control sequence.", "l.12 \\foo",
                "Package hyperref Warning: Token not allowed in a PDF string", "removed on input line 3.",
                "Overfull \\hbox (3.2pt too wide) in paragraph at lines 4--5", "Output written on main.pdf (2 pages).",
                "===== LaTeX log (main.log) ====="]

    def test_python_filter(self):
        keep = build.quiet_filter()
        self.assertEqual([line for line in SAMPLE_LOG.splitlines() if keep(line)], self.EXPECTED)

    @unittest.skipUnless(shutil.which("node"), "node is not installed")
    def test_editor_filter_matches(self):
        module = Path(build.__file__).parent / "serve_ui" / "buildctl.js"
        code = (f"import {{ filterLog }} from {json.dumps(module.as_uri())}; "
                f"process.stdout.write(filterLog({json.dumps(SAMPLE_LOG)}));")
        out = subprocess.run(["node", "--input-type=module", "-e", code], capture_output=True, text=True, check=True)
        self.assertEqual(out.stdout.split("\n"), self.EXPECTED)


class CancelTests(unittest.TestCase):
    @unittest.skipIf(os.name == "nt", "process groups are POSIX here; kill_tree uses taskkill on Windows")
    def test_cancel_kills_the_running_process_group(self):
        with fake_repo() as root:
            main = write_doc(root, "doc", PLAIN)
            self.assertFalse(build.cancel(main))  # nothing runs
            build_dir = build.cache_dir_for(main)
            process = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(30)"], start_new_session=True)
            try:
                with build.ACTIVE_LOCK:
                    build.ACTIVE[build_dir] = process
                self.assertTrue(build.cancel(main))
                self.assertIsNotNone(process.wait(timeout=10))
                self.assertIn(build_dir, build.CANCELLED)
            finally:
                process.kill()
                build.ACTIVE.pop(build_dir, None)
                build.CANCELLED.discard(build_dir)

    @unittest.skipUnless(shutil.which("latexmk"), "latexmk is not installed")
    def test_a_cancelled_build_fails_with_a_reason_and_steps_are_timed(self):
        with fake_repo() as root:
            main = write_doc(root, "doc", "\\documentclass{article}\\begin{document}"
                                          "\\loop\\iftrue\\repeat\\end{document}")
            latexmk = shutil.which("latexmk")

            def stop_soon():
                time.sleep(1.5)
                build.cancel(main)

            import threading
            timer = threading.Thread(target=stop_soon)
            timer.start()
            with contextlib.redirect_stdout(io.StringIO()):
                entry, _ = build.build_document(main, latexmk, live=False)
            timer.join()
            self.assertFalse(entry["ok"])
            self.assertEqual(entry["error"], build.CANCEL_TEXT)
            self.assertNotIn(build.cache_dir_for(main), build.ACTIVE)
            write_doc(root, "doc", PLAIN)
            with contextlib.redirect_stdout(io.StringIO()):
                entry, _ = build.build_document(main, latexmk, live=False)
            self.assertTrue(entry["ok"])
            self.assertEqual(entry["steps"][0]["rule"], "pdflatex")


class DoctoolsTests(unittest.TestCase):
    def test_deps_text(self):
        self.assertEqual(doctools.deps_text("out/my doc.pdf", ["files/my doc/main.tex", "a$b.tex"], "make"),
                         "out/my\\ doc.pdf: \\\n  files/my\\ doc/main.tex \\\n  a$$b.tex\n")
        self.assertEqual(json.loads(doctools.deps_text("out/x.pdf", ["a"], "json")),
                         {"target": "out/x.pdf", "inputs": ["a"]})

    def test_query_reads_main_tex_aux_and_toc(self):
        with tempfile.TemporaryDirectory() as tmp:
            folder, cache = Path(tmp) / "doc", Path(tmp) / "cache"
            folder.mkdir()
            cache.mkdir()
            (folder / "main.tex").write_text("% \\title{Old}\n\\title[Short]{A {Big} Title\\thanks{t}}\n"
                                             "\\author{Ann \\and Bob}\n", encoding="utf-8")
            (cache / "main.aux").write_text("\\newlabel{sec:a}{{1.2}{3}{Intro}{section.1}{}}\n"
                                            "\\newlabel{sec:a@cref}{{[section][2][1]1.2}{[1][3][]3}}\n"
                                            "\\@input{ch.aux}\n", encoding="utf-8")
            (cache / "ch.aux").write_text("\\newlabel{fig:x}{{\\textbf {4}}{5}}\n", encoding="utf-8")
            (cache / "main.toc").write_text(
                "\\contentsline {section}{\\numberline {1}Intro to {\\em X}}{3}{section.1}%\n"
                "\\contentsline {section}{References}{9}{section*.2}%\n", encoding="utf-8")
            with mock.patch.object(doctools.shutil, "which", return_value=None):
                found = doctools.query(folder / "main.tex", cache, 12)
        self.assertEqual(found, {
            "title": "A Big Title", "author": "Ann, Bob", "pages": 12, "words": None,
            "labels": [{"key": "sec:a", "number": "1.2", "page": "3"}, {"key": "fig:x", "number": "4", "page": "5"}],
            "sections": [{"level": "section", "number": "1", "title": "Intro to X", "page": "3"},
                         {"level": "section", "number": None, "title": "References", "page": "9"}],
        })

    def test_pages(self):
        pdf, target = Path("in.pdf"), Path("out.pdf")
        self.assertIn("use page numbers", doctools.extract_pages(pdf, "1-a", target))
        self.assertIn("use page numbers", doctools.extract_pages(pdf, "0-2", target))
        with mock.patch.object(doctools.shutil, "which", return_value=None):
            self.assertIn("needs qpdf", doctools.extract_pages(pdf, "1-3,7", target))
        with mock.patch.object(doctools.shutil, "which", side_effect=lambda name: name == "qpdf"):
            self.assertEqual(doctools.pages_command(pdf, "1-3,7", target),
                             ["qpdf", "--empty", "--pages", "in.pdf", "1-3,7", "--", "out.pdf"])
        with mock.patch.object(doctools.shutil, "which", side_effect=lambda name: name == "pdftk"):
            self.assertEqual(doctools.pages_command(pdf, "1-3,7", target),
                             ["pdftk", "in.pdf", "cat", "1-3", "7", "output", "out.pdf"])

    def test_preflight_parses_pdffonts_and_pdfinfo(self):
        fonts = ("name                                 type              encoding         emb sub uni object ID\n"
                 "------------------------------------ ----------------- ---------------- --- --- --- ---------\n"
                 "ABCDEF+CMR10                         Type 1            Builtin          yes yes no       4  0\n"
                 "Helvetica                            Type 1            WinAnsi          no  no  no       9  0\n"
                 "[none]                               Type 3            Custom           yes no  no      12  0\n")
        info = ("Pages:           2\nPage    1 size:  612 x 792 pts (letter)\n"
                "Page    2 size:  595.276 x 841.89 pts (A4)\nPDF version:     1.5\n")

        def run(command, **_kwargs):
            return subprocess.CompletedProcess(command, 0, fonts if command[0] == "pdffonts" else info, "")

        with mock.patch.object(doctools.shutil, "which", return_value="/bin/x"), \
                mock.patch.object(doctools.subprocess, "run", side_effect=run):
            found = doctools.preflight(Path("x.pdf"), b"<< /Outlines 3 0 R >>")
        levels = [(level, message.split(":")[0].split(".")[0]) for level, message in found]
        self.assertEqual(levels, [("error", "fonts not embedded"), ("warning", "Type 3 (bitmap) fonts"),
                                  ("info", "PDF version 1"), ("warning", "pages of 2 different sizes"),
                                  ("info", "the PDF has bookmarks; IEEE PDF eXpress has flagged them")])
        self.assertIn("Helvetica", found[0][1])
        self.assertNotIn("CMR10", found[0][1])
        with mock.patch.object(doctools.shutil, "which", return_value=None):
            self.assertEqual([level for level, _ in doctools.preflight(Path("x.pdf"))], ["info", "info"])

    def test_notify_passes_text_as_data(self):
        with mock.patch.object(doctools, "notify_command", return_value=None):
            self.assertFalse(doctools.notify("t", "m"))
        with mock.patch.object(doctools.sys, "platform", "linux"), mock.patch.object(doctools.os, "name", "posix"), \
                mock.patch.object(doctools.shutil, "which", side_effect=lambda n: n == "notify-send"), \
                mock.patch.object(doctools.subprocess, "Popen") as popen:
            self.assertTrue(doctools.notify("doc; rm -rf ~", "$(x)"))
        command = popen.call_args[0][0]
        self.assertEqual(command[-2:], ["doc; rm -rf ~", "$(x)"])
        self.assertEqual(popen.call_args[1]["env"]["LP_TITLE"], "doc; rm -rf ~")
        with mock.patch.object(doctools.sys, "platform", "darwin"), \
                mock.patch.object(doctools.shutil, "which", return_value="/usr/bin/osascript"):
            self.assertNotIn("doc", " ".join(doctools.notify_command()))  # text only through the environment


class BibTidyTests(unittest.TestCase):
    BIB = ("% my refs\n@string{jnl = \"J. Test\"}\n\n% about zeta\n"
           "@Article{zeta2020, title={Zeta}, author = {Zed, A. and B, C}, year=2020, journal=jnl, doi={10.1/x}}\n"
           "@book{alpha,\n  author = \"Alpha, Bob\",\n  title = {A {Big} Book},\n  year = {1999},\n}\n"
           "@misc{dup, title={zeta}, doi = {https://doi.org/10.1/X}}\n")

    def test_sorted_aligned_and_nothing_lost(self):
        out, twins = bibtidy.tidy(self.BIB)
        self.assertEqual(out, (
            "% my refs\n@string{jnl = \"J. Test\"}\n\n"
            "@book{alpha,\n  title  = {A {Big} Book},\n  author = \"Alpha, Bob\",\n  year   = {1999},\n}\n\n"
            "@misc{dup,\n  title = {zeta},\n  doi   = {https://doi.org/10.1/X},\n}\n\n"
            "% about zeta\n@article{zeta2020,\n  title   = {Zeta},\n  author  = {Zed, A. and B, C},\n"
            "  year    = 2020,\n  journal = jnl,\n  doi     = {10.1/x},\n}\n"))
        self.assertEqual(twins, ["dup and zeta2020: same DOI", "dup and zeta2020: same title"])
        self.assertEqual(bibtidy.tidy(out)[0], out)  # idempotent

    def test_orders_crlf_and_unparsable_text(self):
        years = [line for line in bibtidy.tidy(self.BIB, "year")[0].splitlines() if line.startswith("@")]
        self.assertEqual(years, ["@string{jnl = \"J. Test\"}", "@book{alpha,", "@article{zeta2020,", "@misc{dup,"])
        authors = [line for line in bibtidy.tidy(self.BIB, "author")[0].splitlines() if line.startswith("@")]
        self.assertEqual(authors[1:], ["@misc{dup,", "@book{alpha,", "@article{zeta2020,"])
        crlf = bibtidy.tidy("@misc{b, title={B}}\r\n@misc{a, title={A}}\r\n")[0]
        self.assertEqual(crlf, "@misc{a,\r\n  title = {A},\r\n}\r\n\r\n@misc{b,\r\n  title = {B},\r\n}\r\n")
        broken = "@misc{b, title={B}}\n@misc{a, % comment inside\n title={A}}\n"
        out = bibtidy.tidy(broken)[0]
        self.assertIn("@misc{a, % comment inside\n title={A}}", out)  # unparsed: kept verbatim, at the top
        self.assertEqual(bibtidy.tidy("@misc{a, title={A}}\n@misc{a, note={N}}\n")[1], ["a and a: same key"])
        with self.assertRaises(ValueError):
            bibtidy.tidy(self.BIB, "colour")

    def test_tidy_bib_cli_check_and_write(self):
        with fake_repo() as root:
            main = write_doc(root, "doc", PLAIN)
            bib = main.parent / "refs.bib"
            bib.write_bytes(b"@misc{b, title={B}}\r\n@misc{a, title={A}}\r\n")
            with contextlib.redirect_stdout(io.StringIO()) as out:
                self.assertEqual(build.tidy_bib_all([main], "key", check=True), 1)
            self.assertIn("Would tidy: files/doc/refs.bib", out.getvalue())
            self.assertTrue(bib.read_bytes().startswith(b"@misc{b"))
            with contextlib.redirect_stdout(io.StringIO()):
                self.assertEqual(build.tidy_bib_all([main], "key", check=False), 0)
                self.assertEqual(build.tidy_bib_all([main], "key", check=True), 0)
            self.assertTrue(bib.read_bytes().startswith(b"@misc{a,\r\n"))


if __name__ == "__main__":
    unittest.main()
