"""Start-up refusals: serve.py --gateway without its preconditions, host.py serve without a sandbox or a database."""

from __future__ import annotations

import contextlib
import io
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import _support  # noqa: F401 - puts scripts/ on sys.path
import host

SERVE = Path(_support.SCRIPTS_DIR) / "serve.py"


class GatewayFlags(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.project = Path(tmp.name) / "proj"
        self.project.mkdir()
        (self.project / "main.tex").write_text("x", encoding="utf-8")

    def run_serve(self, args, secret="s" * 40):
        env = {k: v for k, v in os.environ.items() if k != "LP_HOST_SECRET"}
        if secret is not None:
            env["LP_HOST_SECRET"] = secret
        return subprocess.run([sys.executable, str(SERVE), *args], env=env, capture_output=True, text=True,
                              timeout=60)

    def test_refused_without_every_precondition(self):
        base = ["--gateway", "--no-open", "--port", "0", "--source", str(self.project)]
        cases = {
            "short secret": (base, "short"),
            "no secret": (base, None),
            "public host": ([*base, "--host", "0.0.0.0"], "s" * 40),
            "sharing": ([*base, "--share", "local"], "s" * 40),
            "document names": ([*base, "proj"], "s" * 40),
            "no main.tex": (["--gateway", "--no-open", "--source", str(self.project.parent)], "s" * 40),
        }
        for name, (args, secret) in cases.items():
            with self.subTest(name):
                result = self.run_serve(args, secret)
                self.assertEqual(result.returncode, 2, result.stderr)
                self.assertIn("--gateway needs LP_HOST_SECRET", result.stderr + result.stdout)

    def test_bad_source_and_document(self):
        result = self.run_serve(["--no-open", "--source", str(self.project / "missing")], None)
        self.assertEqual(result.returncode, 2)
        result = self.run_serve(["--no-open", "--source", str(self.project.parent), "nope"], None)
        self.assertEqual(result.returncode, 2)
        self.assertIn("No document matches: nope", result.stderr + result.stdout)


class HostServe(unittest.TestCase):
    def call(self, data, insecure=False):
        err = io.StringIO()
        with contextlib.redirect_stderr(err), contextlib.redirect_stdout(err):
            return host.cmd_serve(data, insecure), err.getvalue()

    def test_refuses_to_serve_without_the_sandbox(self):
        with tempfile.TemporaryDirectory() as tmp:
            data = Path(tmp)
            with mock.patch.object(host.sys, "platform", "darwin"):
                self.assertEqual(self.call(data)[0], 2)
            with mock.patch.object(host.sys, "platform", "linux"), \
                    mock.patch.object(host, "sandbox_works", return_value=False):
                code, text = self.call(data)
            self.assertEqual(code, 2)
            self.assertIn("Refusing to serve", text)
            with mock.patch.object(host.sys, "platform", "linux"), \
                    mock.patch.object(host, "sandbox_works", return_value=True):
                code, text = self.call(data)
            self.assertEqual(code, 2)
            self.assertIn("No database", text)
            code, text = self.call(data, insecure=True)
            self.assertEqual(code, 2)
            self.assertIn("WITHOUT the bubblewrap sandbox", text)

    def test_sandbox_probe(self):
        with mock.patch.object(host.shutil, "which", return_value=None):
            self.assertFalse(host.sandbox_works())
        with mock.patch.object(host.subprocess, "run", side_effect=OSError), \
                mock.patch.object(host.shutil, "which", return_value="/usr/bin/bwrap"):
            self.assertFalse(host.sandbox_works())
        with mock.patch.object(host.subprocess, "run", return_value=subprocess.CompletedProcess([], 0)), \
                mock.patch.object(host.shutil, "which", return_value="/usr/bin/bwrap"):
            self.assertTrue(host.sandbox_works())

    def test_main_reports_config_errors(self):
        with tempfile.TemporaryDirectory() as tmp, \
                mock.patch.object(host.sys, "argv", ["host.py", "serve", "--data", tmp]), \
                mock.patch.object(host, "cmd_serve", side_effect=host.ConfigError("bad config")), \
                contextlib.redirect_stderr(io.StringIO()) as err, contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(host.main(), 2)
        self.assertIn("bad config", err.getvalue())


if __name__ == "__main__":
    unittest.main()
