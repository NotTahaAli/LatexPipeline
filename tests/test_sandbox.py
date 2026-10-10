from __future__ import annotations

import contextlib
import io
import os
import shutil
import subprocess
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

from _support import build, fake_repo, write_doc

sandbox = build.sandbox

ON = {"LATEX_SANDBOX": "bwrap"}
PYTHON = "/usr/bin/python3"  # Inside the sandbox only system directories exist (not a uv or venv Python).


SYS_OK = os.path.exists(PYTHON) and not sandbox.problem()


class CommandTests(unittest.TestCase):
    def test_off_by_default_and_unknown_values_refused(self):
        with mock.patch.dict(os.environ, {"LATEX_SANDBOX": ""}):
            self.assertEqual(sandbox.spawn(["pdflatex", "x"], Path("/d"), Path("/b")),
                             {"args": ["pdflatex", "x"], "cwd": Path("/d"), "env": None})
        with mock.patch.dict(os.environ, {"LATEX_SANDBOX": "yes"}), self.assertRaises(sandbox.SandboxError):
            sandbox.spawn(["pdflatex"], Path("/d"), Path("/b"))

    def test_wrapped_command_env_and_limits(self):
        with tempfile.TemporaryDirectory() as tmp, mock.patch.dict(os.environ, ON), \
                mock.patch.object(sandbox, "problem", return_value=None):
            doc, out = Path(tmp, "doc"), Path(tmp, "cache")
            spec = sandbox.spawn(["latexmk", "main.tex"], doc, out,
                                 {"PATH": "/usr/bin", "GITHUB_TOKEN": "s3cret", "openin_any": "p"}, cpu=60)
            args = spec["args"]
            self.assertIn("--unshare-all", args)
            self.assertIn("--die-with-parent", args)
            self.assertEqual(args[-3:], ["--", "latexmk", "main.tex"])
            joined = " ".join(args)
            self.assertIn(f"--ro-bind {doc} {doc}", joined)
            self.assertIn(f"--bind {out} {out}", joined)
            self.assertLess(joined.index("--tmpfs /tmp"), joined.index(f"--ro-bind {doc}"))
            self.assertNotIn("--ro-bind-try /etc /etc", joined)
            self.assertNotIn("GITHUB_TOKEN", spec["env"])
            self.assertEqual(spec["env"]["openin_any"], "p")
            self.assertEqual(spec["env"]["TEXMFVAR"], str(out / "texmf-var"))
            self.assertTrue(callable(spec["preexec_fn"]))
            # A figure job writes only its own directory and the caches.
            args = sandbox.spawn(["pdflatex"], doc, out, work=out / "figwork" / "a")["args"]
            self.assertIn(f"--ro-bind {out} {out} ", " ".join(args))
            self.assertNotIn(f"--bind {out} {out} ", " ".join(args).replace("--ro-bind", ""))
            self.assertIn(f"--bind {out / 'texmf-var'} ", " ".join(args))

    def test_scrub_removes_links_but_keeps_files(self):
        with tempfile.TemporaryDirectory() as tmp, mock.patch.dict(os.environ, ON):
            root = Path(tmp)
            (root / "sub").mkdir()
            (root / "sub" / "main.pdf").write_text("pdf", encoding="utf-8")
            (root / "main.log").symlink_to("/etc/passwd")
            (root / "sub" / "dir").symlink_to("/etc")
            sandbox.scrub(root)
            self.assertEqual(sorted(p.name for p in root.rglob("*")), ["main.pdf", "sub"])

    def test_required_but_unusable_refuses_to_build(self):
        with fake_repo() as root, contextlib.redirect_stdout(io.StringIO()), mock.patch.dict(os.environ, ON), \
                mock.patch.object(sandbox, "problem", return_value="bwrap was not found."), \
                mock.patch.object(build.subprocess, "Popen", wraps=subprocess.Popen) as popen:
            main = write_doc(root, "doc", "\\documentclass{article}\\begin{document}x\\end{document}\n")
            report, text = build.build_document(main, "latexmk", live=False)
            started = [(c.kwargs["args"] if "args" in c.kwargs else c.args[0])[0] for c in popen.call_args_list]
            self.assertNotIn("latexmk", started)  # kpsewhich and git may run; LaTeX must not.
            self.assertFalse(report["ok"])
            self.assertIn("Refusing to build unsandboxed", text)
            with contextlib.redirect_stderr(io.StringIO()), self.assertRaises(SystemExit):
                build.check_latex()


@unittest.skipUnless(SYS_OK, "needs a working bwrap and /usr/bin/python3")
class SandboxedProcessTests(unittest.TestCase):
    """sandbox.spawn on a plain Python process: what the sandbox lets through."""

    def run_py(self, code: str) -> str:
        with tempfile.TemporaryDirectory() as tmp, mock.patch.dict(os.environ, {**ON, "API_SECRET": "s3cret"}):
            doc, out = Path(tmp, "doc"), Path(tmp, "cache")
            doc.mkdir()
            out.mkdir()
            (doc / "in.txt").write_text("source", encoding="utf-8")
            result = subprocess.run(
                **sandbox.spawn([PYTHON, "-c", code, str(doc), str(out)], doc, out),
                capture_output=True, text=True, timeout=60,
            )
            self.assertEqual(result.returncode, 0, result.stderr)
            return result.stdout.strip()

    def test_no_network(self):
        code = "import socket\ntry:\n socket.create_connection(('1.1.1.1', 80), 3); print('open')\n" \
               "except OSError as e: print(type(e).__name__)"
        self.assertNotEqual(self.run_py(code), "open")
        self.assertEqual(self.run_py("import socket; print(len(socket.if_nameindex()))"), "1")  # Only lo.

    def test_reads_and_writes_confined(self):
        code = (
            "import os, sys\n"
            "doc, out = sys.argv[1:]\n"
            "def can(path, mode='r'):\n"
            " try:\n  open(path, mode).close(); return 1\n except OSError:\n  return 0\n"
            "print(can(doc + '/in.txt'), can(out + '/x', 'w'), can(doc + '/x', 'w'), can('/etc/shadow'),\n"
            "      can('/etc/hostname'), int(os.path.exists(os.path.expanduser('~root/.ssh'))), can('/usr/x', 'w'),\n"
            "      os.environ.get('API_SECRET'), sum(p.isdigit() for p in os.listdir('/proc')) < 5)"
        )
        self.assertEqual(self.run_py(code), "1 1 0 0 0 0 0 None True")


@unittest.skipUnless(shutil.which("latexmk") and shutil.which("lualatex") and SYS_OK, "needs bwrap, latexmk, lualatex")
class SandboxedBuildTests(unittest.TestCase):
    """build.py with LATEX_SANDBOX=bwrap, end to end."""

    def build(self, root, name, tex, toml=None, files=None):
        main = write_doc(root, name, tex, toml)
        for file, text in (files or {}).items():
            (main.parent / file).write_text(text, encoding="utf-8")
        with mock.patch.dict(os.environ, ON):
            return build.build_document(main, shutil.which("latexmk"), live=False)

    def test_plain_bibtex_and_tikz_documents_build(self):
        bib = "@book{k, author={A}, title={T}, year={2000}, publisher={P}}\n"
        tikz = "\\usepackage{tikz}\\begin{document}\\begin{tikzpicture}\\node{A};\\end{tikzpicture}\\end{document}\n"
        cite = "\\begin{document}\\cite{k}\\bibliographystyle{plain}\\bibliography{refs}\\end{document}\n"
        with fake_repo() as root, contextlib.redirect_stdout(io.StringIO()):
            for name, body in (("cite", cite), ("pic", tikz)):
                report, text = self.build(root, name, "\\documentclass{article}" + body, files={"refs.bib": bib})
                self.assertTrue(report["ok"], (name, report["errors"], text[-2000:]))
                self.assertNotIn("rebuilt without it", text)  # The figure jobs worked sandboxed too.
                self.assertTrue((root / "out" / f"{name}.pdf").exists())
            self.assertTrue(any((root / ".latex-cache" / "_figcache").rglob("*.pdf")))

    def test_lua_cannot_read_write_run_or_connect_outside(self):
        home = Path.home() / f".sandbox-probe-{os.getpid()}"
        probe = (
            'local function log(k, v) texio.write_nl("term and log", "PROBE " .. k .. "=" .. tostring(v)) end\n'
            'local f = io.open("/etc/passwd"); log("read", f ~= nil); if f then f:close() end\n'
            f'local w = io.open("{home}", "w"); log("write", w ~= nil); if w then w:write("x"); w:close() end\n'
            f'os.execute("echo x > {home}.exec")\n'
            'local ok, socket = pcall(require, "socket")\n'
            'if ok then local c = socket.tcp(); c:settimeout(3); log("net", c:connect("1.1.1.1", 80)) end\n'
        )
        self.addCleanup(lambda: [p.unlink(missing_ok=True) for p in (home, Path(f"{home}.exec"))])
        tex = "\\documentclass{article}\\begin{document}\\directlua{dofile('probe.lua')}Hi\\end{document}\n"
        with fake_repo() as root, contextlib.redirect_stdout(io.StringIO()):
            # Shell escape on: even a document allowed to run programs stays inside.
            report, text = self.build(root, "lua", tex, 'engine = "lualatex"\nshell_escape = true\n',
                                      {"probe.lua": probe})
            self.assertTrue(report["ok"], report["errors"])
            self.assertIn("PROBE read=false", text)
            self.assertIn("PROBE write=false", text)
            self.assertNotIn("PROBE net=1", text)
            self.assertFalse(home.exists())
            self.assertFalse(Path(f"{home}.exec").exists())

    def test_timeout_kills_the_sandbox(self):
        with fake_repo() as root, contextlib.redirect_stdout(io.StringIO()):
            began = time.monotonic()
            loop = "\\documentclass{article}\\begin{document}\\def\\a{\\a}\\a\\end{document}\n"
            report, text = self.build(root, "loop", loop, "timeout = 10\n")
            self.assertIn("Build timed out after", text)
            self.assertLess(time.monotonic() - began, 25)
            if shutil.which("pgrep"):
                left = subprocess.run(["pgrep", "-f", str(root)], capture_output=True, text=True)
                self.assertEqual(left.stdout.strip(), "")


if __name__ == "__main__":
    unittest.main()
