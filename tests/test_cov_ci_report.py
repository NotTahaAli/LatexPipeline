"""ci_report.py summary, PR comment (gh mocked), chktex, latexdiff and the command line."""

from __future__ import annotations

import argparse
import contextlib
import io
import json
import os
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from _support import ci_report

DOCS = [
    {"name": "good", "ok": True, "pages": 3, "size": 2048, "seconds": 1.2, "warnings": 0, "errors": []},
    {"name": "bad", "ok": False, "errors": [{"file": "main.tex", "line": 4, "message": "Undefined control sequence"}]},
    {"name": "crashed", "ok": False, "error": "latexmk not found"},
]
LINT = {"documents": {"good": {"words": 120, "chktex": 2, "chktex_top": ["main.tex:1: a", "main.tex:2: b"],
                               "lint": 0}}}


class Paths(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.out = Path(tmp.name)
        for name, value in (("REPORT_PATH", self.out / "build-report.json"),
                            ("LINT_PATH", self.out / "lint-report.json")):
            patch = mock.patch.object(ci_report, name, value)
            patch.start()
            self.addCleanup(patch.stop)

    def reports(self, documents=DOCS, lint=LINT):
        ci_report.REPORT_PATH.write_text(json.dumps({"documents": documents}), encoding="utf-8")
        if lint is not None:
            ci_report.LINT_PATH.write_text(json.dumps(lint), encoding="utf-8")


class Summary(Paths):
    def test_table_and_chktex_details(self):
        self.reports()
        text = ci_report.report_markdown("##")
        self.assertIn("## LaTeX build: 1 ok, 2 failed", text)
        self.assertIn("| good | ok | 3 | 2 KB | 120 | 1.2 s | 0 | 2 | 0 | - |", text)
        self.assertIn("main.tex:4: Undefined control sequence", text)
        self.assertIn("latexmk not found", text)
        self.assertIn("<details><summary>chktex warnings", text)
        self.assertIn("- main.tex:2: b", text)

    def test_nothing_built_and_no_lint(self):
        self.assertIn("No documents were built", ci_report.report_markdown("###"))
        self.reports(documents=[DOCS[0]], lint=None)
        self.assertNotIn("<details>", ci_report.report_markdown("##"))
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            ci_report.cmd_summary(argparse.Namespace())
        self.assertIn("1 ok, 0 failed", out.getvalue())

    def test_cells_escape_pipes(self):
        self.assertEqual(ci_report.cell("a|b\nc"), "a\\|b c")


class Comment(Paths):
    ENV = {"GITHUB_REPOSITORY": "o/r", "GITHUB_RUN_ID": "9", "GITHUB_SHA": "abcdef123456",
           "DIFF_ARTIFACT_URL": "https://example.org/a"}

    def test_body_with_diff_results(self):
        self.reports()
        diff_dir = self.out / "diff"
        diff_dir.mkdir()
        (diff_dir / ci_report.DIFF_REPORT).write_text(json.dumps({"documents": {
            "good": {"ok": True}, "bad": {"ok": False, "error": "boom"}}}), encoding="utf-8")
        with mock.patch.dict(os.environ, self.ENV):
            body = ci_report.comment_body(diff_dir)
        self.assertTrue(body.startswith(ci_report.MARKER))
        self.assertIn("Built commit `abcdef1`.", body)
        self.assertIn("https://github.com/o/r/actions/runs/9", body)
        self.assertIn("- `good`: compiled, download [`diff-pdfs`](https://example.org/a).", body)
        self.assertIn("- `bad`: failed (boom).", body)
        (diff_dir / ci_report.DIFF_REPORT).write_text(json.dumps({"documents": {}}), encoding="utf-8")
        self.assertIn("No changed document to compare.", ci_report.comment_body(diff_dir))
        self.assertEqual(ci_report.diff_section(self.out / "none"), [])

    def run_comment(self, event, existing=""):
        calls = []

        def gh(*args):
            calls.append(args)
            if "--paginate" in args:
                return existing
            return ""

        args = argparse.Namespace(diff_dir=None, print_only=False)
        env = dict(self.ENV)
        if event is not None:
            path = self.out / "event.json"
            path.write_text(json.dumps(event), encoding="utf-8")
            env["GITHUB_EVENT_PATH"] = str(path)
        out = io.StringIO()
        with mock.patch.dict(os.environ, env), mock.patch.object(ci_report, "gh", gh), contextlib.redirect_stdout(out):
            ci_report.cmd_pr_comment(args)
        return calls, out.getvalue()

    def test_posts_updates_and_skips(self):
        self.reports()
        pull = {"number": 7, "head": {"repo": {"full_name": "o/r"}}}
        calls, text = self.run_comment(None)
        self.assertEqual((calls, text.strip()), ([], "Not a pull request event: no comment."))
        calls, text = self.run_comment({"pull_request": {**pull, "head": {"repo": {"full_name": "fork/r"}}}})
        self.assertEqual((calls, text.strip()), ([], "Pull request from a fork: no comment."))
        calls, text = self.run_comment({"pull_request": pull})
        self.assertIn(f'.user.login == "{ci_report.BOT_LOGIN}"', calls[0][-1])  # only our own comments
        self.assertEqual(calls[1][:4], ("api", "--method", "POST", "repos/o/r/issues/7/comments"))
        self.assertIn("Posted comment on PR #7.", text)
        calls, text = self.run_comment({"pull_request": pull}, existing="111\n222\n")
        self.assertEqual(calls[1][:4], ("api", "--method", "PATCH", "repos/o/r/issues/comments/111"))
        self.assertIn("Updated comment 111", text)

    def test_print_only(self):
        self.reports()
        out = io.StringIO()
        with mock.patch.object(ci_report, "gh", side_effect=AssertionError), contextlib.redirect_stdout(out):
            ci_report.cmd_pr_comment(argparse.Namespace(diff_dir=None, print_only=True))
        self.assertTrue(out.getvalue().startswith(ci_report.MARKER))


class Tools(unittest.TestCase):
    @unittest.skipUnless(shutil.which("chktex"), "needs chktex")
    def test_chktex_warnings(self):
        with tempfile.TemporaryDirectory() as tmp:
            Path(tmp, "main.tex").write_text("\\documentclass{article}\n\\begin{document}\nSee Fig. \\ref{x}.\n"
                                             "\\end{document}\n", encoding="utf-8")
            warnings = ci_report.run_chktex(Path(tmp))
        self.assertTrue(warnings and all(w.startswith("main.tex:") for w in warnings), warnings)

    def test_word_count_without_texcount(self):
        with mock.patch.object(ci_report.shutil, "which", return_value=None):
            self.assertIsNone(ci_report.count_words(Path(".")))
        failed = subprocess.CompletedProcess([], 1, "oops", "")
        with mock.patch.object(ci_report.shutil, "which", return_value="/x"), \
                mock.patch.object(ci_report.subprocess, "run", return_value=failed):
            self.assertIsNone(ci_report.count_words(Path(".")))

    def test_owning_document_is_the_deepest(self):
        self.assertEqual(ci_report.owning_document("files/a/b/x.tex", ["a", "a/b"]), "a/b")
        self.assertIsNone(ci_report.owning_document("README.md", ["a"]))


MAIN = "\\documentclass{article}\n\\begin{document}\nOld words here.\n\\end{document}\n"


@unittest.skipUnless(shutil.which("git") and shutil.which("latexdiff") and shutil.which("latexmk"),
                     "needs git, latexdiff and latexmk")
class Diff(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name).resolve()
        files = self.root / "files"
        for name in ("changed", "broken"):
            (files / name).mkdir(parents=True)
            (files / name / "main.tex").write_text(MAIN, encoding="utf-8")
        git = ["git", "-c", "user.name=t", "-c", "user.email=t@example.org", "-c", "commit.gpgsign=false"]
        subprocess.run(["git", "init", "-q"], cwd=self.root, check=True)
        subprocess.run([*git, "add", "-A"], cwd=self.root, check=True)
        subprocess.run([*git, "commit", "-qm", "base"], cwd=self.root, check=True)
        (files / "changed" / "main.tex").write_text(MAIN.replace("Old", "New"), encoding="utf-8")
        (files / "broken" / "main.tex").write_text(MAIN + "x", encoding="utf-8")
        (files / "fresh").mkdir()
        (files / "fresh" / "main.tex").write_text(MAIN, encoding="utf-8")
        subprocess.run([*git, "add", "-A"], cwd=self.root, check=True)
        self.out = self.root / "diffs"
        for name, value in (("ROOT_DIR", self.root), ("SOURCE_DIR", files),
                            ("REPORT_PATH", self.root / "build-report.json")):
            patch = mock.patch.object(ci_report, name, value)
            patch.start()
            self.addCleanup(patch.stop)
        documents = [{"name": "changed", "ok": True}, {"name": "broken", "ok": False}, {"name": "fresh", "ok": True}]
        ci_report.REPORT_PATH.write_text(json.dumps({"documents": documents}), encoding="utf-8")

    def test_changed_documents_get_a_diff_pdf(self):
        with contextlib.redirect_stdout(io.StringIO()):
            ci_report.cmd_diff(argparse.Namespace(base="HEAD", out=str(self.out)))
        report = json.loads((self.out / ci_report.DIFF_REPORT).read_text())["documents"]
        self.assertEqual(report["changed"], {"ok": True})
        self.assertEqual(report["broken"], {"ok": False, "error": "the new version does not build"})
        self.assertNotIn("fresh", report)  # new: nothing to compare
        self.assertTrue((self.out / "changed.pdf").read_bytes().startswith(b"%PDF"))

    def test_latexdiff_failure_is_reported(self):
        failed = subprocess.CompletedProcess([], 2, b"", b"latexdiff: bad input")
        real = subprocess.run

        def fake(cmd, *a, **k):
            return failed if cmd[0] == "latexdiff" else real(cmd, *a, **k)

        with mock.patch.object(ci_report.subprocess, "run", fake), contextlib.redirect_stdout(io.StringIO()):
            ci_report.cmd_diff(argparse.Namespace(base="HEAD", out=str(self.out)))
        report = json.loads((self.out / ci_report.DIFF_REPORT).read_text())["documents"]
        self.assertEqual(report["changed"], {"ok": False, "error": "latexdiff: bad input"})


class CommandLine(Paths):
    def test_main_dispatches(self):
        self.reports()
        out = io.StringIO()
        with mock.patch("sys.argv", ["ci_report.py", "summary"]), contextlib.redirect_stdout(out):
            self.assertEqual(ci_report.main(), 0)
        self.assertIn("LaTeX build", out.getvalue())


if __name__ == "__main__":
    unittest.main()
