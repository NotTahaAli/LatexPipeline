"""Pure logic in scripts/build.py and the ci_report markdown helpers."""

import contextlib
import io
import os
import shutil
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
                         {"engine": "pdflatex", "shell_escape": False, "latexmk_args": [], "externalize": True})

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
                         {"engine": "lualatex", "shell_escape": True, "latexmk_args": ["-g"], "externalize": True})

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
        self.assertEqual(row.replace("\\|", "").count("|"), 10)  # 9 columns
        self.assertTrue(row.startswith("| weird\\|name | **failed** |"))


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
