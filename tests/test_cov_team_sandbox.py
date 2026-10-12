"""team.py storage and fetch errors, the lock that fails closed, and sandbox.py's child-side limits and probes."""

from __future__ import annotations

import io
import json
import os
import subprocess
import sys
import tempfile
import unittest
import urllib.error
from pathlib import Path
from unittest import mock

import _support  # noqa: F401 - puts scripts/ on sys.path
import sandbox
import team

SCRIPTS = str(Path(sandbox.__file__).resolve().parent)


class Store(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.dir = Path(tmp.name)

    def test_unreadable_lock_counts_as_locked(self):
        (self.dir / team.LOCK_FILE).write_text("{broken", encoding="utf-8")
        self.assertEqual(team.read_lock(self.dir), {"by": "?", "time": 0})
        (self.dir / team.LOCK_FILE).write_text("[1]", encoding="utf-8")  # wrong type: as if absent
        self.assertIsNone(team.read_lock(self.dir))
        self.assertIsNone(team.read_lock(self.dir / "missing"))

    def test_read_json_errors_are_500(self):
        (self.dir / team.LINKS_FILE).mkdir()  # reading a directory fails with OSError
        with self.assertRaises(team.TeamError) as ctx:
            team.read_links(self.dir)
        self.assertEqual(ctx.exception.status, 500)

    def test_write_json_is_atomic_and_cleans_up(self):
        path = self.dir / "sub" / "x.json"
        team.write_json(path, {"a": 1})
        self.assertEqual(json.loads(path.read_text()), {"a": 1})
        with self.assertRaises(TypeError):
            team.write_json(path, {"a": object()})  # not serializable: the old file stays, no temp file is left
        self.assertEqual(json.loads(path.read_text()), {"a": 1})
        self.assertEqual(os.listdir(path.parent), ["x.json"])
        team.write_json(path, None)
        team.write_json(path, None)  # deleting twice is fine
        self.assertFalse(path.exists())

    def test_lock_round_trip(self):
        self.assertEqual(team.write_lock(self.dir, "Ada")["by"], "Ada")
        self.assertEqual(team.read_lock(self.dir)["by"], "Ada")
        self.assertIsNone(team.write_lock(self.dir, None))
        self.assertIsNone(team.read_lock(self.dir))


class Response(io.BytesIO):
    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


class HttpGet(unittest.TestCase):
    def fetch(self, effect, cap=10):
        opener = mock.Mock()
        opener.open.side_effect = effect
        with mock.patch.object(team.urllib.request, "build_opener", return_value=opener):
            return team.http_get("https://example.org/x.bib", cap), opener

    def test_reads_up_to_the_cap(self):
        data, opener = self.fetch(lambda req, timeout: Response(b"0123456789"))
        self.assertEqual(data, b"0123456789")
        request = opener.open.call_args[0][0]
        self.assertEqual(request.full_url, "https://example.org/x.bib")
        self.assertEqual(opener.open.call_args[1]["timeout"], team.FETCH_TIMEOUT)

    def test_errors(self):
        cases = [
            (lambda req, timeout: Response(b"x" * 11), 413),
            (urllib.error.HTTPError("u", 404, "nf", {}, None), 502),
            (urllib.error.HTTPError("u", 0, "", {}, None), 502),
            (urllib.error.URLError("no route"), 502),
            (OSError("reset"), 502),
            (ValueError("bad"), 502),
        ]
        for effect, status in cases:
            with self.subTest(effect=effect), self.assertRaises(team.TeamError) as ctx:
                self.fetch(effect)
            self.assertEqual(ctx.exception.status, status)

    def test_refuses_non_https_before_any_request(self):
        with mock.patch.object(team.urllib.request, "build_opener") as build_opener:
            with self.assertRaises(team.TeamError):
                team.http_get("http://example.org/x", 10)
            build_opener.return_value.open.assert_not_called()

    def test_https_redirect_is_followed(self):
        handler = team._HttpsRedirects()
        req = urllib.request.Request("https://a.example/x")
        new = handler.redirect_request(req, io.BytesIO(), 302, "Found", {}, "https://b.example/y")
        self.assertEqual(new.full_url, "https://b.example/y")


@unittest.skipUnless(os.name == "posix" and sandbox.resource is not None, "POSIX resource limits")
class ChildSide(unittest.TestCase):
    """_limits and _lower run between fork and exec; run them in a fresh interpreter and look at the result."""

    def child(self, code):
        script = f"import sys; sys.path.insert(0, {SCRIPTS!r}); import os, resource, sandbox\n{code}"
        return subprocess.run([sys.executable, "-c", script], capture_output=True, text=True, timeout=60)

    def test_limits_and_nice(self):
        result = self.child(
            "before = os.nice(0)\n"
            "sandbox._limits([(resource.RLIMIT_NOFILE, 64), (resource.RLIMIT_CPU, 100)], 3, True)\n"
            "print(resource.getrlimit(resource.RLIMIT_NOFILE), resource.getrlimit(resource.RLIMIT_CPU),"
            " os.nice(0) - before)\n"
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stdout.split(), ["(64,", "64)", "(100,", "100)", "3"])

    def test_no_nice_only_limits(self):
        result = self.child(
            "before = os.nice(0)\nsandbox._limits([(resource.RLIMIT_NOFILE, 32)], 0, False)\n"
            "print(resource.getrlimit(resource.RLIMIT_NOFILE)[0], os.nice(0) - before)\n"
        )
        self.assertEqual(result.stdout.split(), ["32", "0"], result.stderr)

    def test_lower_without_autogroup(self):
        result = self.child(
            "import builtins\nreal = builtins.open\n"
            "def fake(path, *a, **k):\n"
            "    if path == '/proc/self/autogroup': raise PermissionError(path)\n"
            "    return real(path, *a, **k)\n"
            "builtins.open = fake\nbefore = os.nice(0)\nsandbox._lower(2, True)\nprint(os.nice(0) - before)\n"
        )
        self.assertEqual(result.stdout.strip(), "2", result.stderr)


class Probes(unittest.TestCase):
    def setUp(self):
        for fn in (sandbox.problem, sandbox.tmp_size, sandbox.tex_dirs):
            fn.cache_clear()
            self.addCleanup(fn.cache_clear)

    def test_problem_reasons(self):
        with mock.patch.object(sandbox.sys, "platform", "darwin"):
            self.assertIn("needs Linux", sandbox.problem())
        sandbox.problem.cache_clear()
        with mock.patch.object(sandbox.sys, "platform", "linux"), mock.patch.object(sandbox.shutil, "which",
                                                                                  return_value=None):
            self.assertIn("not found", sandbox.problem())
        sandbox.problem.cache_clear()
        with mock.patch.object(sandbox.sys, "platform", "linux"), \
                mock.patch.object(sandbox.shutil, "which", return_value="/usr/bin/bwrap"), \
                mock.patch.object(sandbox.subprocess, "run", side_effect=OSError("exec format error")):
            self.assertIn("does not run", sandbox.problem())
        sandbox.problem.cache_clear()
        failed = subprocess.CompletedProcess([], 1, "", "setting up uid map: Permission denied\n")
        with mock.patch.object(sandbox.sys, "platform", "linux"), \
                mock.patch.object(sandbox.shutil, "which", return_value="/usr/bin/bwrap"), \
                mock.patch.object(sandbox.subprocess, "run", return_value=failed):
            self.assertIn("user namespaces", sandbox.problem())

    def test_tmp_size_and_tex_dirs_survive_missing_tools(self):
        with mock.patch.object(sandbox.subprocess, "run", side_effect=OSError):
            self.assertEqual(sandbox.tmp_size(), ())
            self.assertEqual(sandbox.tex_dirs(), ())
        sandbox.tmp_size.cache_clear()
        with mock.patch.object(sandbox.subprocess, "run",
                               return_value=subprocess.CompletedProcess([], 0, "  --size BYTES  ...", "")):
            self.assertEqual(sandbox.tmp_size(), ("--size", str(sandbox.TMP_BYTES)))

    def test_process_options(self):
        self.assertEqual(sandbox.process_options(True, False), {"start_new_session": True} if os.name != "nt" else
                         {"creationflags": 512})
        if os.name != "nt":
            self.assertEqual(sandbox.process_options(False, True), {})
        with mock.patch.object(sandbox.os, "name", "nt"), \
                mock.patch.object(sandbox.subprocess, "CREATE_NEW_PROCESS_GROUP", 0x200, create=True), \
                mock.patch.object(sandbox.subprocess, "BELOW_NORMAL_PRIORITY_CLASS", 0x4000, create=True):
            self.assertEqual(sandbox.process_options(True, True), {"creationflags": 0x4200})
            self.assertEqual(sandbox.process_options(False, False), {})

    def test_scrub_is_a_no_op_unsandboxed(self):
        with tempfile.TemporaryDirectory() as tmp:
            link = Path(tmp) / "l"
            try:
                link.symlink_to("/etc/hostname")
            except OSError:
                self.skipTest("no symlinks")
            with mock.patch.object(sandbox, "enabled", return_value=False):
                sandbox.scrub(Path(tmp))
            self.assertTrue(link.is_symlink())


if __name__ == "__main__":
    unittest.main()
