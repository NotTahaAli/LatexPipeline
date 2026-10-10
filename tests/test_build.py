"""Pure logic in scripts/build.py and the ci_report markdown helpers."""

import contextlib
import io
import os
import shutil
import subprocess
import time
import unittest
import urllib.parse
from pathlib import Path
from unittest import mock

from _support import build, ci_report, fake_repo, write_doc


class EscapeNameTests(unittest.TestCase):
    def test_example(self):
        self.assertEqual(build.escape_name("reports/final_v2 report.pdf"),
                         "reports_2Ffinal_5Fv2_20report.pdf")

    def test_keeps_safe_characters(self):
        self.assertEqual(build.escape_name("a-b.c9Z"), "a-b.c9Z")

    def test_utf8_bytes_are_hex_escaped(self):
        self.assertEqual(build.escape_name("é"), "_C3_A9")

    def test_reversible_with_documented_recipe(self):
        # README: replace "_" with "%", then URL-decode.
        original = "FP-123 Proposal/v1_2 (é).pdf"
        escaped = build.escape_name(original)
        self.assertNotIn("/", escaped)
        self.assertNotIn(" ", escaped)
        self.assertEqual(urllib.parse.unquote(escaped.replace("_", "%")), original)


class OutputPathTests(unittest.TestCase):
    def test_paths_mirror_source_tree(self):
        with fake_repo() as root:
            src = root / "files"
            out = root / "out"
            cases = {
                "FP-123 Proposal": out / "FP-123 Proposal.pdf",
                "reports/final": out / "reports" / "final.pdf",
                "v1.2": out / "v1.2.pdf",          # must not become v1.pdf
                "a/b/c": out / "a" / "b" / "c.pdf",
            }
            for name, expected in cases.items():
                main_tex = src / name / "main.tex"
                with self.subTest(name=name):
                    self.assertEqual(build.output_path_for(main_tex), expected)
                    self.assertEqual(build.log_path_for(main_tex), expected.with_suffix(".log"))

    def test_root_main_tex(self):
        with fake_repo() as root:
            main_tex = root / "files" / "main.tex"
            self.assertEqual(build.output_path_for(main_tex), root / "out" / "main.pdf")
            self.assertEqual(build.log_path_for(main_tex), root / "out" / "main.log")

    def test_log_for_dotted_name(self):
        with fake_repo() as root:
            main_tex = root / "files" / "v1.2" / "main.tex"
            self.assertEqual(build.log_path_for(main_tex).name, "v1.2.log")


class DocumentSelectionTests(unittest.TestCase):
    def setUp(self):
        repo = fake_repo()
        root = repo.__enter__()
        self.addCleanup(repo.__exit__, None, None, None)
        self.docs = [
            write_doc(root, "FP-123 Proposal", "x"),
            write_doc(root, "reports/final", "x"),
            write_doc(root, "reports/draft", "x"),
        ]

    def names(self, documents):
        return [build.doc_name(d) for d in documents]

    def test_no_patterns_selects_all(self):
        self.assertEqual(build.select_documents(self.docs, []), self.docs)

    def test_exact_name(self):
        self.assertEqual(self.names(build.select_documents(self.docs, ["FP-123 Proposal"])),
                         ["FP-123 Proposal"])

    def test_glob(self):
        self.assertEqual(self.names(build.select_documents(self.docs, ["reports/*"])),
                         ["reports/final", "reports/draft"])

    def test_unknown_patterns(self):
        self.assertEqual(build.unknown_patterns(self.docs, ["reports/final", "nope", "x*"]),
                         ["nope", "x*"])


class LatexmkArgsTests(unittest.TestCase):
    def test_synctex_is_on_for_every_build(self):
        # serve.py runs synctex against the cache-dir PDF, so focus and full builds both need it.
        self.assertIn("-synctex=1", build.LATEXMK_ARGS)

    def test_log_lines_are_not_wrapped(self):
        # The keys TeX reads from the environment; the wrapped-line tolerance in the regexes stays.
        self.assertEqual(build.LATEX_LOG_ENV["max_print_line"], "10000")
        self.assertLess(int(build.LATEX_LOG_ENV["half_error_line"]), int(build.LATEX_LOG_ENV["error_line"]))
        self.assertLessEqual(int(build.LATEX_LOG_ENV["error_line"]), 255)


class ParseLatexErrorsTests(unittest.TestCase):
    def test_file_line_error_lines(self):
        console = (
            "./main.tex:5: Undefined control sequence.\n"
            "! LaTeX Error: File `x.sty' not found.\n"
            "sections/intro.tex:12: Missing $ inserted.\n"
            "./main.tex:5: Undefined control sequence.\n"
        )
        found = build.parse_latex_errors(console)
        self.assertEqual([(e["file"], e["line"], e["message"]) for e in found], [
            ("main.tex", 5, "Undefined control sequence."),
            ("sections/intro.tex", 12, "Missing $ inserted."),
        ])
        self.assertIn("Check the spelling", found[0]["hint"])
        self.assertIn("Wrap it in", found[1]["hint"])

    def test_hint_can_come_from_context_lines(self):
        console = "./main.tex:3: Something vague\nl.3 \\foo\n! Undefined control sequence.\n"
        self.assertIn("Check the spelling", build.parse_latex_errors(console)[0]["hint"])

    def test_unknown_error_has_no_hint(self):
        found = build.parse_latex_errors("./main.tex:3: Something vague\nl.3 text\n")
        self.assertIsNone(found[0]["hint"])

    def test_no_errors(self):
        self.assertEqual(build.parse_latex_errors("Latexmk: All targets up-to-date\n"), [])


class LatexLogPatternTests(unittest.TestCase):
    def test_pages_on_one_line(self):
        self.assertEqual(build.LATEX_PAGES.findall("Output written on main.pdf (2 pages, 900 bytes)."),
                         ["2"])

    def test_pages_wrapped_inside_path(self):
        log = "Output written on /home/u/.latex-cache/x/la\ntest/main.pdf (2 pages, 900 bytes).\n"
        self.assertEqual(build.LATEX_PAGES.findall(log), ["2"])

    def test_pages_wrapped_before_word(self):
        self.assertEqual(build.LATEX_PAGES.findall("Output written on main.pdf (1\npage, 9 bytes)."),
                         ["1"])

    def test_warning_count(self):
        log = (
            "LaTeX Warning: Reference `a' on page 1 undefined.\n"
            "Package hyperref Warning: Token not allowed.\n"
            "Class beamer Warning: something.\n"
            "LaTeX Font Warning: Font shape undefined.\n"
            "Overfull \\hbox (3pt too wide)\n"
            "Package hyperref Info: ok.\n"
        )
        self.assertEqual(len(build.LATEX_WARNING.findall(log)), 4)


class ReadSettingsTests(unittest.TestCase):
    PLAIN = "\\documentclass{article}\n\\begin{document}x\\end{document}\n"

    def settings(self, main_tex, build_toml=None):
        with fake_repo() as root:
            path = write_doc(root, "doc", main_tex, build_toml)
            return build.read_settings(path)

    def assertConfigError(self, main_tex, build_toml=None):
        with self.assertRaises(build.ConfigError):
            self.settings(main_tex, build_toml)

    def test_defaults(self):
        self.assertEqual(self.settings(self.PLAIN),
                         {"engine": "pdflatex", "shell_escape": False, "latexmk_args": [], "externalize": True,
                          "pdfa": None, "tagged": False, "lang": "en-US", "timeout": 600})

    def test_magic_comment_variants(self):
        cases = {
            "% !TEX program = xelatex\n": "xelatex",
            "%!TeX TS-program=LuaLaTeX\n": "lualatex",
            "% !TEX program = XeLaTeX\n": "xelatex",
        }
        for magic, engine in cases.items():
            with self.subTest(magic=magic):
                self.assertEqual(self.settings(magic + self.PLAIN)["engine"], engine)

    def test_magic_comment_after_line_20_is_ignored(self):
        text = "\n" * 20 + "% !TEX program = xelatex\n" + self.PLAIN
        self.assertEqual(self.settings(text)["engine"], "pdflatex")

    def test_build_toml_overrides_engine_and_sets_options(self):
        toml = 'engine = "lualatex"\nshell_escape = true\nlatexmk_args = ["-g"]\n'
        self.assertEqual(self.settings("% !TEX program = xelatex\n" + self.PLAIN, toml),
                         {"engine": "lualatex", "shell_escape": True, "latexmk_args": ["-g"], "externalize": True,
                          "pdfa": None, "tagged": False, "lang": "en-US", "timeout": 600})

    def test_build_toml_can_opt_out_of_externalize(self):
        self.assertFalse(self.settings(self.PLAIN, 'externalize = false\n')["externalize"])

    def test_invalid_values_raise_config_error(self):
        self.assertConfigError("% !TEX program = pdftex\n" + self.PLAIN)
        self.assertConfigError(self.PLAIN, 'engine = "bogus"\n')
        self.assertConfigError(self.PLAIN, 'engine = 3\n')
        self.assertConfigError(self.PLAIN, 'unknown_key = 1\n')
        self.assertConfigError(self.PLAIN, 'shell_escape = "yes"\n')
        self.assertConfigError(self.PLAIN, 'externalize = "no"\n')
        self.assertConfigError(self.PLAIN, 'latexmk_args = "-g"\n')
        self.assertConfigError(self.PLAIN, 'latexmk_args = [1]\n')
        self.assertConfigError(self.PLAIN, 'engine = \n')  # invalid TOML

    def test_timeout_validation(self):
        self.assertEqual(self.settings(self.PLAIN, "timeout = 10\n")["timeout"], 10)
        self.assertEqual(self.settings(self.PLAIN, "timeout = 7200\n")["timeout"], 7200)
        for bad in ("9", "7201", "1.5", '"60"', "true"):
            with self.subTest(value=bad):
                self.assertConfigError(self.PLAIN, f"timeout = {bad}\n")

    def test_pdfa_levels_and_metadata(self):
        for value in ("2b", "a-2b", "A-2B"):
            settings = self.settings(self.PLAIN, f'pdfa = "{value}"\nlang = "de-DE"\n')
            self.assertEqual(settings["pdfa"], "a-2b")
            meta = build.document_metadata(settings)
            self.assertTrue(meta.startswith(r"\DocumentMetadata{pdfstandard=a-2b,lang=de-DE}"))
            self.assertNotIn("\n", meta)  # a single latexmk -usepretex argument
            self.assertNotIn("objcompresslevel", meta)
        self.assertIn(r"\pdfobjcompresslevel=0", build.document_metadata(self.settings(self.PLAIN, 'pdfa = "1b"\n')))
        self.assertEqual(build.document_metadata(self.settings(self.PLAIN)), "")
        self.assertConfigError(self.PLAIN, 'pdfa = "9z"\n')
        self.assertConfigError(self.PLAIN, 'pdfa = true\n')
        self.assertConfigError(self.PLAIN, 'lang = "en}US"\n')

    def test_tagged_metadata(self):
        meta = build.document_metadata(self.settings(self.PLAIN, "tagged = true\n"))
        self.assertNotIn("\n", meta)  # a single latexmk -usepretex argument
        self.assertIn(r"\DocumentMetadata{pdfstandard=ua-1,lang=en-US,tagging=on}", meta)
        self.assertIn(r"\DocumentMetadata{pdfstandard=ua-1,lang=en-US,testphase={phase-III,firstaid}}", meta)
        self.assertIn(r"\IfFormatAtLeastTF{2025-06-01}", meta)
        self.assertIn("WARNING: tagged = true needs LaTeX 2023-06-01", meta)  # old kernels: untagged, with a note
        self.assertIn("pdfdisplaydoctitle", meta)
        combined = build.document_metadata(self.settings(self.PLAIN, 'tagged = true\npdfa = "2a"\nlang = "de-DE"\n'))
        self.assertIn(r"\DocumentMetadata{pdfstandard=a-2a,lang=de-DE,tagging=on}", combined)
        self.assertIn("glyphtounicode", combined)
        self.assertNotIn("\n", combined)
        self.assertEqual(build.document_metadata(self.settings(self.PLAIN, "tagged = false\n")), "")
        self.assertConfigError(self.PLAIN, 'tagged = "yes"\n')

    def test_tagged_check_and_ua_flavour(self):
        with fake_repo() as root:
            pdf = root / "a.pdf"
            pdf.write_bytes(b"%PDF-1.7\n/MarkInfo<</Marked true>>/StructTreeRoot 5 0 R\n")
            self.assertIn("present", build.tagged_check(pdf))
            pdf.write_bytes(b"%PDF-1.7\n")
            self.assertIn("no structure tree", build.tagged_check(pdf))
        done = subprocess.CompletedProcess([], 0, stdout='<validationReport isCompliant="true">', stderr="")
        with mock.patch.object(build.subprocess, "run", return_value=done) as call:
            note = build.verapdf_note("verapdf", Path("a.pdf"), "a-2a", tagged=True)
        self.assertEqual([c[0][0][4] for c in call.call_args_list], ["2a", "ua1"])
        self.assertEqual(note, "PDF/A a-2a: veraPDF passed. PDF/UA-1: veraPDF passed.")

    def test_pdfa_check_reads_xmp_and_output_intent(self):
        with fake_repo() as root:
            pdf = root / "a.pdf"
            pdf.write_bytes(b"%PDF-1.7\n<pdfaid:part>2</pdfaid:part>\n/OutputIntents [1 0 R]\n")
            self.assertIn("present", build.pdfa_check(pdf, "a-2b"))
            pdf.write_bytes(b"%PDF-1.7\n")
            self.assertIn("no XMP pdfaid or OutputIntent", build.pdfa_check(pdf, "a-2b"))

    def test_verapdf_note(self):
        def run(stdout):
            done = subprocess.CompletedProcess([], 1, stdout=stdout, stderr="")
            with mock.patch.object(build.subprocess, "run", return_value=done) as call:
                note = build.verapdf_note("verapdf", Path("a.pdf"), "a-2b")
            self.assertEqual(call.call_args[0][0][:5], ["verapdf", "--format", "xml", "--flavour", "2b"])
            return note
        self.assertEqual(run('<validationReport isCompliant="true">'), "PDF/A a-2b: veraPDF passed.")
        failed = run('isCompliant="false"><description>Fonts &amp; glyphs</description><description>Fonts &amp; glyphs'
                     '</description>')
        self.assertEqual(failed, "PDF/A a-2b: veraPDF failed 1 rule(s): Fonts & glyphs")
        self.assertIn("no verdict", run("garbage"))

    def test_size_text(self):
        self.assertEqual(build.size_text(101324), "101 KB")
        self.assertEqual(build.size_text(1622508), "1.6 MB")

    def test_config_error_names_the_file(self):
        with fake_repo() as root:
            path = write_doc(root, "doc", self.PLAIN, 'shell_escape = 1\n')
            with self.assertRaises(build.ConfigError) as ctx:
                build.read_settings(path)
            self.assertIn("build.toml", str(ctx.exception))


class RecordedInputsTests(unittest.TestCase):
    """Files outside a document's directory, tracked through the .fls of its last build."""

    def setUp(self):
        repo = fake_repo()
        self.root = repo.__enter__()
        self.addCleanup(repo.__exit__, None, None, None)
        self.main = write_doc(self.root, "doc", "x")
        self.shared = self.root / "files" / "shared" / "macros.tex"
        self.shared.parent.mkdir(parents=True)
        self.shared.write_text("\\newcommand{\\x}{y}\n", encoding="utf-8")
        self.fls = self.root / ".latex-cache" / "doc" / "main.fls"
        self.fls.parent.mkdir(parents=True)
        self.fls.write_text("\n".join([
            "PWD /x",
            "INPUT ./main.tex",
            "INPUT ../shared/macros.tex",
            f"INPUT {self.root / 'texlive' / 'tikz.sty'}",        # TeX installation: ignored
            f"INPUT {self.root / '.latex-cache' / 'doc' / 'tikz' / 'f.pdf'}",  # cache: ignored
            "INPUT /elsewhere/notes.tex",                         # outside everything: tracked
            "OUTPUT main.pdf",
        ]) + "\n", encoding="utf-8")
        self.tree = mock.patch.object(build, "tex_tree_dirs", return_value=(self.root / "texlive",))
        self.tree.start()
        self.addCleanup(self.tree.stop)

    def test_external_inputs_only(self):
        self.assertEqual(build.recorded_inputs(self.main),
                         [self.shared.resolve(), (self.main.parent / "/elsewhere/notes.tex").resolve()])

    def test_no_recorder_file_means_nothing_recorded(self):
        self.fls.unlink()
        self.assertEqual(build.recorded_inputs(self.main), [])

    def test_unknown_tex_tree_disables_the_rule(self):
        with mock.patch.object(build, "tex_tree_dirs", return_value=None):
            self.assertEqual(build.recorded_inputs(self.main), [])

    def test_root_directory_in_tex_tree_is_ignored(self):
        # kpsewhich can report "/" (SELFAUTOPARENT); that must not hide the whole disk.
        with mock.patch.object(build, "tex_tree_dirs", return_value=(Path("/"),)):
            self.assertEqual(build.recorded_inputs(self.main), [
                self.shared.resolve(), self.root / "texlive" / "tikz.sty",
                (self.main.parent / "/elsewhere/notes.tex").resolve(),
            ])

    def test_is_stale_follows_recorded_input(self):
        pdf = build.output_path_for(self.main)
        pdf.parent.mkdir(parents=True)
        pdf.write_text("pdf")
        now = time.time()
        os.utime(pdf, (now + 1000, now + 1000))          # PDF newer than the build script
        os.utime(self.shared, (now - 1000, now - 1000))
        self.assertFalse(build.is_stale(self.main))
        os.utime(self.shared, (now + 2000, now + 2000))  # edited after the build
        self.assertTrue(build.is_stale(self.main))

    def test_ci_filter_follows_recorded_input(self):
        doc = self.main
        other = write_doc(self.root, "other", "x")
        with mock.patch.object(build, "changed_files", return_value=["files/shared/macros.tex"]):
            self.assertEqual(build.filter_changed([doc, other], "origin/main"), [doc])
            self.fls.unlink()
            self.assertEqual(build.filter_changed([doc, other], "origin/main"), [])


class MirrorDirsTests(unittest.TestCase):
    def test_subdirectories_are_created_without_files(self):
        with fake_repo() as root:
            source = root / "files" / "doc"
            (source / "Chapters" / "ch1").mkdir(parents=True)
            (source / ".hidden").mkdir()
            (source / "Chapters" / "ch1.tex").write_text("x")
            target = root / ".latex-cache" / "doc"
            target.mkdir(parents=True)
            build.mirror_dirs(source, target)
            self.assertTrue((target / "Chapters" / "ch1").is_dir())
            self.assertFalse((target / ".hidden").exists())
            self.assertEqual(sorted(p.name for p in target.rglob("*") if p.is_file()), [])


class PhaseTextTests(unittest.TestCase):
    def test_phase_text(self):
        self.assertEqual(build.phase_text({"figures": 1.25, "latex": 5.6}), "figures 1.2s, latex 5.6s")


class SourceDirTests(unittest.TestCase):
    """bench/ and --source: outputs named relative to their source, prune keeps every source's outputs."""

    def setUp(self):
        repo = fake_repo()
        self.root = repo.__enter__()
        self.addCleanup(repo.__exit__, None, None, None)
        self.files_doc = write_doc(self.root, "shared", "x")
        bench = self.root / "bench" / "shared"
        bench.mkdir(parents=True)
        self.bench_doc = bench / "main.tex"
        self.bench_doc.write_text("x", encoding="utf-8")

    def test_bench_document_is_named_relative_to_bench(self):
        self.assertEqual(build.doc_name(self.bench_doc), "shared")
        self.assertEqual(build.output_path_for(self.bench_doc), self.root / "out" / "shared.pdf")

    def test_same_named_documents_keep_separate_caches(self):
        self.assertNotEqual(build.cache_dir_for(self.files_doc), build.cache_dir_for(self.bench_doc))

    def test_source_option_changes_the_documents_found(self):
        with mock.patch.object(build, "SOURCE_DIR", self.root / "bench"):
            self.assertEqual(build.find_documents(), [self.bench_doc])
            self.assertEqual(build.doc_name(self.bench_doc), "shared")
        self.assertEqual(build.find_documents(), [self.files_doc])

    def test_prune_keeps_outputs_of_every_known_source(self):
        out = self.root / "out"
        out.mkdir()
        for name in ("shared.pdf", "shared.log", "gone.pdf", "gone.log"):
            (out / name).write_text("x")
        with contextlib.redirect_stdout(io.StringIO()):
            build.prune(build.all_documents())
        self.assertEqual(sorted(p.name for p in out.iterdir()), ["shared.log", "shared.pdf"])


class CiReportCellTests(unittest.TestCase):
    def test_cell_values(self):
        self.assertEqual(ci_report.cell(None), "-")
        self.assertEqual(ci_report.cell(0), "0")
        self.assertEqual(ci_report.cell("a|b"), "a\\|b")
        self.assertEqual(ci_report.cell("x\ny"), "x y")

    def test_table_row_with_pipe_in_name_has_all_columns(self):
        doc = {"name": "weird|name", "ok": False, "pages": None, "seconds": 1.5,
               "warnings": 2, "errors": [{"file": "main.tex", "line": 3, "message": "a|b"}]}
        rows = ci_report.table([doc], {})
        row = rows[2]
        self.assertIn("weird\\|name", row)
        self.assertEqual(row.replace("\\|", "").count("|"), 11)  # 10 columns
        self.assertIn("| 12 KB |", ci_report.table([{**doc, "size": 12000}], {})[2])
        self.assertTrue(row.startswith("| weird\\|name | **failed** |"))


class NewTemplateTests(unittest.TestCase):
    FILES = {
        "article": {"main.tex"},
        "report": {"main.tex", "refs.bib", "build.toml", "figures/README.txt", "chapters/introduction.tex",
                   "chapters/methods.tex", "chapters/conclusion.tex"},
        "beamer": {"main.tex"},
        "letter": {"main.tex"},
    }

    def test_each_template_writes_its_files_and_never_overwrites(self):
        for template, expected in self.FILES.items():
            with fake_repo() as root, contextlib.redirect_stdout(io.StringIO()):
                self.assertEqual(build.new_document("doc", template), 0)
                folder = root / "files" / "doc"
                self.assertEqual({p.relative_to(folder).as_posix() for p in folder.rglob("*") if p.is_file()}, expected)
                self.assertEqual(build.new_document("doc", template), 1)

    def test_nothing_is_written_when_any_target_file_exists(self):
        with fake_repo() as root, contextlib.redirect_stdout(io.StringIO()):
            folder = root / "files" / "doc"
            folder.mkdir(parents=True)
            (folder / "refs.bib").write_text("mine")
            self.assertEqual(build.new_document("doc", "report"), 1)
            self.assertEqual([p.name for p in folder.rglob("*") if p.is_file()], ["refs.bib"])
            self.assertEqual((folder / "refs.bib").read_text(), "mine")

    def test_verapdf_only_when_validating(self):
        with fake_repo() as root, mock.patch.object(build.shutil, "which", return_value="verapdf"), \
                mock.patch.object(build, "verapdf_note", return_value="ran") as note:
            pdf = root / "a.pdf"
            pdf.write_bytes(b"%PDF-1.7\n<pdfaid:part>2</pdfaid:part>\n/OutputIntents [1 0 R]\n")
            self.assertEqual(build.pdfa_check(pdf, "a-2b", validate=False)[:14], "PDF/A a-2b: XM")
            note.assert_not_called()
            self.assertEqual(build.pdfa_check(pdf, "a-2b"), "ran")

    def test_template_needs_new_and_docx_excludes_watch_and_focus(self):
        for argv in (["--template", "report"], ["--docx", "--watch"], ["--docx", "--focus", "x"]):
            with mock.patch.object(build.sys, "argv", ["build.py", *argv]), \
                    contextlib.redirect_stderr(io.StringIO()), self.assertRaises(SystemExit):
                build.parse_args()

    def test_article_is_unchanged(self):
        with fake_repo() as root, contextlib.redirect_stdout(io.StringIO()):
            build.new_document("a_b")
            text = (root / "files" / "a_b" / "main.tex").read_text(encoding="utf-8")
            self.assertEqual(text, build.NEW_TEMPLATE % {"title": "a\\_b"})

    @unittest.skipUnless(shutil.which("latexmk") and shutil.which("bibtex"), "needs latexmk and bibtex")
    def test_report_template_builds(self):
        with fake_repo() as root, contextlib.redirect_stdout(io.StringIO()):
            build.new_document("rep", "report")
            main = root / "files" / "rep" / "main.tex"
            report, text = build.build_document(main, shutil.which("latexmk"), live=False)
            self.assertTrue(report["ok"], report["errors"])
            self.assertNotIn("Citation `knuth84' undefined", text)


class DocxExportTests(unittest.TestCase):
    def test_error_is_the_last_real_stderr_line_and_output_is_decoded_as_utf8(self):
        with fake_repo() as root:
            main = write_doc(root, "a", "x")
            done = mock.Mock(returncode=1, stderr="[WARNING] meh\n\nError: cannot open ../x\n[WARNING] later\n")
            with mock.patch.object(build.shutil, "which", return_value="/bin/pandoc"), \
                    mock.patch.object(build.subprocess, "run", return_value=done) as run:
                self.assertEqual(build.export_docx(main), (False, "Error: cannot open ../x"))
            self.assertEqual((run.call_args[1]["encoding"], run.call_args[1]["errors"]), ("utf-8", "replace"))

    def test_pandoc_command_and_prune(self):
        with fake_repo() as root:
            main = write_doc(root, "a", "x")
            (main.parent / "r.bib").write_text("", encoding="utf-8")
            ran = []
            done = mock.Mock(returncode=0, stderr="")
            fake_run = lambda cmd, **kw: ran.append((cmd, kw)) or done  # noqa: E731
            with mock.patch.object(build.shutil, "which", return_value="/bin/pandoc"), \
                    mock.patch.object(build.subprocess, "run", side_effect=fake_run):
                ok, _ = build.export_docx(main)
            cmd, kw = ran[0]
            self.assertTrue(ok)
            self.assertEqual(kw["cwd"], main.parent)
            self.assertEqual(cmd[1:3], ["main.tex", "-o"])
            self.assertIn("--citeproc", cmd)
            self.assertIn("--bibliography=r.bib", cmd)
            self.assertEqual(cmd[3], str(root / "out" / "a.docx"))

            (root / "out").mkdir(exist_ok=True)
            (root / "out" / "a.docx").write_bytes(b"x")
            (root / "out" / "gone.docx").write_bytes(b"x")
            with contextlib.redirect_stdout(io.StringIO()):
                build.prune([main])
            self.assertEqual(sorted(p.name for p in (root / "out").iterdir()), ["a.docx"])

    def test_missing_pandoc_is_a_clear_message(self):
        with fake_repo() as root, mock.patch.object(build.shutil, "which", return_value=None):
            ok, message = build.export_docx(write_doc(root, "a", "x"))
        self.assertFalse(ok)
        self.assertIn("pandoc was not found", message)


if __name__ == "__main__":
    unittest.main()


@unittest.skipUnless(shutil.which("latexmk") and shutil.which("pdflatex"), "needs latexmk and pdflatex")
class CleanCacheRetryTests(unittest.TestCase):
    """A failed build with cached files retries from scratch; the retry must report the real error."""

    def build(self, main):
        report, _ = build.build_document(main, shutil.which("latexmk"), live=False)
        return report

    def test_retry_keeps_injected_files_and_include_dirs(self):
        with fake_repo() as root, contextlib.redirect_stdout(io.StringIO()):
            main = write_doc(root, "doc", "\\documentclass{article}\\begin{document}\\include{ch/a}\\end{document}\n")
            (main.parent / "ch").mkdir()
            (main.parent / "ch" / "a.tex").write_text("ok\n", encoding="utf-8")
            self.assertTrue(self.build(main)["ok"])

            (main.parent / "ch" / "a.tex").write_text("\\undefinedmacro\n", encoding="utf-8")
            report = self.build(main)
            self.assertFalse(report["ok"])
            self.assertEqual([e["file"] for e in report["errors"]], ["ch/a.tex"])

            (main.parent / "ch" / "a.tex").write_text("fixed\n", encoding="utf-8")
            self.assertTrue(self.build(main)["ok"])


@unittest.skipUnless(shutil.which("latexmk") and shutil.which("pdflatex"), "needs latexmk and pdflatex")
class TimeoutTests(unittest.TestCase):
    def test_looping_document_is_killed_and_reported(self):
        with fake_repo() as root, contextlib.redirect_stdout(io.StringIO()):
            main = write_doc(root, "loop", "\\documentclass{article}\\begin{document}\\def\\a{\\a}\\a\\end{document}\n",
                             "timeout = 10\n")
            began = time.monotonic()
            report, text = build.build_document(main, shutil.which("latexmk"), live=False)
            self.assertFalse(report["ok"])
            self.assertIn("Build timed out after", text)
            self.assertLess(time.monotonic() - began, 25)
            self.assertNotIn("retrying from a clean cache", text)
            if os.name != "nt" and shutil.which("pgrep"):
                left = subprocess.run(["pgrep", "-f", str(main.parent)], capture_output=True, text=True)
                self.assertEqual(left.stdout.strip(), "")


class GlobalInputsTests(unittest.TestCase):
    """GLOBAL_INPUTS (rebuild everything) and the workflow's paths: filters name the same real build inputs."""

    ROOT = Path(__file__).resolve().parent.parent

    def test_only_build_inputs_are_global_and_the_workflow_filters_agree(self):
        workflow = (self.ROOT / ".github/workflows/build-pdf.yml").read_text(encoding="utf-8")
        listed = [name for name in build.GLOBAL_INPUTS if name.startswith("scripts/")]
        self.assertTrue(listed)
        for name in listed:
            self.assertTrue((self.ROOT / name).is_file(), name)
            self.assertEqual(workflow.count(f'- "{name}"'), 2, name)  # push and pull_request
        self.assertNotIn('"scripts/**"', workflow)
        self.assertNotIn("scripts/", [n for n in build.GLOBAL_INPUTS])
        for editor_file in ("scripts/serve.py", "scripts/serve_ui/app.js", "scripts/serve_ui/collab.js"):
            self.assertFalse(editor_file.startswith(build.GLOBAL_INPUTS), editor_file)

    def test_ci_scripts_do_not_import_the_editor_server(self):
        for name in ("build.py", "accel.py", "hints.py", "publish_release.py", "ci_report.py"):
            text = (self.ROOT / "scripts" / name).read_text(encoding="utf-8")
            self.assertNotRegex(text, r"(?m)^\s*(import|from)\s+serve\b", name)


class ReportAndFocusTests(unittest.TestCase):
    def test_write_report_is_atomic(self):
        with fake_repo() as root:
            build.write_report([{"name": "a"}])
            target = root / "out" / "build-report.json"
            with mock.patch.object(build.os, "replace", side_effect=OSError("boom")), self.assertRaises(OSError):
                build.write_report([{"name": "b"}])
            self.assertIn('"a"', target.read_text(encoding="utf-8"))  # old report intact
            self.assertEqual([p.name for p in (root / "out").iterdir()], ["build-report.json"])  # no temp left

    def test_focus_build_times_out_instead_of_hanging(self):
        import subprocess
        with fake_repo() as root, contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
            main = write_doc(root, "d", "\\documentclass{article}")
            cache = build.cache_dir_for(main)
            cache.mkdir(parents=True)
            (cache / "main.focusmap").write_text("x", encoding="utf-8")
            seen = {}

            def fake_run(*args, **kwargs):
                seen.update(kwargs)
                raise subprocess.TimeoutExpired(kwargs["args"], kwargs["timeout"])

            with mock.patch.object(build.accel, "read_focusmap", return_value=[]), \
                    mock.patch.object(build.accel, "focus_selection", return_value=([], "a")), \
                    mock.patch.object(build.accel, "focus_tex", return_value=""), \
                    mock.patch.object(build.subprocess, "run", side_effect=fake_run):
                self.assertFalse(build._build_focus(main, "latexmk", "a", 1))
            self.assertTrue(seen.get("timeout"))


@unittest.skipUnless(shutil.which("latexmk") and shutil.which("pdflatex"), "needs latexmk and pdflatex")
class ParanoidReadsTests(unittest.TestCase):
    """serve.py builds shared documents with openin_any=p/openout_any=p, which refuse ".." paths."""

    def test_plain_and_tikz_documents_build(self):
        tikz = "\\usepackage{tikz}\\begin{document}\\begin{tikzpicture}\\node{A};\\end{tikzpicture}\\end{document}\n"
        with fake_repo() as root, contextlib.redirect_stdout(io.StringIO()), \
                mock.patch.dict(os.environ, {"openin_any": "p", "openout_any": "p"}):
            for name, body in (("plain", "\\begin{document}Hi\\end{document}\n"), ("pic", tikz)):
                main = write_doc(root, name, "\\documentclass{article}" + body)
                report, text = build.build_document(main, shutil.which("latexmk"), live=False)
                self.assertTrue(report["ok"], (name, report["errors"]))
                self.assertNotIn("rebuilt without it", text)  # Externalization itself must work.
                self.assertEqual(build.build_document(main, shutil.which("latexmk"), live=False)[0]["ok"], True)
