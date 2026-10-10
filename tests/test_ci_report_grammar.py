# ruff: noqa: E501
"""ci_report.py lint --grammar: findings, baseline, and the public-API guard. No network."""

import argparse
import contextlib
import importlib
import io
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from _support import build, ci_report

grammar = importlib.import_module("grammar")  # after _support put scripts/ on sys.path


def reply(fields, *needles):
    text = fields["text"]
    return {"matches": [
        {"message": "Bad word", "offset": text.index(n), "length": len(n), "replacements": [{"value": "good"}],
         "rule": {"id": "RULE_X", "category": {"id": "GRAMMAR"}}} for n in needles]}


class GrammarLintTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        root = Path(self.tmp.name)
        self.doc = root / "files" / "doc"
        self.doc.mkdir(parents=True)
        (self.doc / "main.tex").write_text("\\begin{document}\n\\input{two}\nA bad line.\n\\end{document}\n", encoding="utf-8")
        (self.doc / "two.tex").write_text("% c\nSecond bad.\n", encoding="utf-8")
        grammar.CACHE.clear()
        for patch in (
            mock.patch.object(ci_report, "SOURCE_DIR", root / "files"),
            mock.patch.object(build, "ROOT_DIR", root),
            mock.patch.object(ci_report, "OUT_DIR", root / "out"),
            mock.patch.object(ci_report, "LINT_PATH", root / "out" / "lint.json"),
            mock.patch.object(ci_report, "REPORT_PATH", root / "none.json"),
            mock.patch.object(ci_report.shutil, "which", return_value=None),
            mock.patch.dict("os.environ", {"GITHUB_ACTIONS": "", "GRAMMAR_PUBLIC_OK": "", "LANGUAGETOOL_URL": ""}),
        ):
            patch.start()
            self.addCleanup(patch.stop)
        self.post = mock.patch.object(grammar, "post_form", side_effect=lambda url, fields, proxy=False: reply(fields, "bad")).start()
        self.addCleanup(mock.patch.stopall)

    def run_lint(self, **options):
        args = argparse.Namespace(**{"documents": [], "strict": False, "update_baseline": False, "overfull_pt": 10.0,
                                     "grammar": True, **options})
        with contextlib.redirect_stdout(io.StringIO()) as out:
            code = ci_report.cmd_lint(args)
        return code, out.getvalue()

    def toml(self, text):
        (self.doc / "build.toml").write_text(text, encoding="utf-8")

    def test_local_findings_have_file_line_and_replacements(self):
        self.toml('grammar = "local"\ndisabled_rules = ["OTHER"]\n')
        code, output = self.run_lint(strict=True)
        self.assertEqual(code, 1)
        found = ci_report.load_json(ci_report.LINT_PATH)["documents"]["doc"]["findings"]
        grammar_found = sorted((f["path"], f["line"]) for f in found if f["kind"] == "grammar")
        self.assertEqual(grammar_found, [("main.tex", 3), ("two.tex", 2)])
        self.assertIn("Try: good", [f["message"] for f in found if f["kind"] == "grammar"][0])
        self.assertIn("OTHER", self.post.call_args.args[1]["disabledRules"])
        self.assertIs(self.post.call_args.kwargs["proxy"], False)

    def test_invalid_build_toml_skips_the_document_without_crashing(self):
        self.toml("grammar = [\n")
        code, output = self.run_lint()
        self.assertEqual(code, 0)
        self.assertIn("grammar check skipped", output)
        self.post.assert_not_called()

    def test_baseline_suppresses_grammar_findings(self):
        self.toml('grammar = "local"\n')
        self.run_lint(update_baseline=True)
        code, output = self.run_lint(strict=True)
        self.assertEqual(code, 0)
        self.assertIn("lint=0", output)

    def test_off_without_flag_or_server(self):
        self.toml('grammar = "local"\n')
        self.run_lint(grammar=False)
        self.post.assert_not_called()
        self.toml("")
        with mock.patch.object(grammar, "probe", return_value=False):
            _, output = self.run_lint()
        self.assertIn("grammar is off", output)
        self.post.assert_not_called()

    def test_auto_uses_a_local_server_that_answers(self):
        with mock.patch.object(grammar, "probe", return_value=True):
            self.run_lint()
        self.assertEqual(self.post.call_args.args[0], "http://localhost:8081/v2/check")

    def test_public_in_ci_needs_the_environment_opt_in(self):
        self.toml('grammar = "public"\n')
        with mock.patch.dict("os.environ", {"GITHUB_ACTIONS": "true"}):
            _, output = self.run_lint()
            self.assertIn("GRAMMAR_PUBLIC_OK", output)
            self.post.assert_not_called()
        with mock.patch.dict("os.environ", {"GITHUB_ACTIONS": "true", "GRAMMAR_PUBLIC_OK": "1"}):
            _, output = self.run_lint()
            self.assertIn("languagetool.org", output)
            self.assertEqual(self.post.call_args.args[0], grammar.PUBLIC_URL)
            self.assertIs(self.post.call_args.kwargs["proxy"], True)

    def test_unreachable_server_does_not_fail_the_run(self):
        self.toml('grammar = "local"\n')
        self.post.side_effect = grammar.GrammarError("LanguageTool is not reachable: refused")
        code, output = self.run_lint(strict=True)
        self.assertEqual((code, "skipped" in output), (0, True))

    def test_invalid_setting_is_reported_not_raised(self):
        self.toml('grammar = "cloud"\n')
        _, output = self.run_lint()
        self.assertIn("skipped", output)


if __name__ == "__main__":
    unittest.main()
