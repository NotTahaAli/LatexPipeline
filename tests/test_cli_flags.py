"""
Every command-line flag of build.py, serve.py, host.py, mcp_server.py, ci_report.py and publish_release.py, run for
real in a subprocess (real LaTeX where the flag needs it). The scripts run from a copy of scripts/ in a temporary
repository, so out/, .latex-cache/ and files/ of this checkout are never touched. FLAGS maps each flag of every
--help to the test that runs it; test_inventory fails when a script gains a flag no test runs.

Needs latexmk and pdflatex for most tests (they skip without), POSIX for the shims (a fake `gh`, `xdg-open`,
`notify-send` and browser on PATH that record their arguments) and bubblewrap for the --sandbox tests. Nothing here
touches the network: Crossref lookups go to a closed local port (and fail as "not reachable"), LanguageTool is a fake
server on loopback, and `gh` is the shim.
"""

from __future__ import annotations

import base64
import json
import os
import re
import shutil
import socket
import subprocess
import sys
import tempfile
import threading
import time
import unittest
import urllib.error
import urllib.request
import zipfile
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
LATEX = bool(shutil.which("latexmk") and shutil.which("pdflatex"))
POSIX = os.name == "posix"
need_latex = unittest.skipUnless(LATEX and POSIX, "needs latexmk, pdflatex and a POSIX shell for the shims")
PNG = base64.b64decode("iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mP8z8DwHwAFBQIAX8jx0gAAAABJRU5E"
                       "rkJggg==")
SECRET = "s" * 40


def bwrap_works() -> bool:
    if not (sys.platform.startswith("linux") and shutil.which("bwrap")):
        return False
    done = subprocess.run(["bwrap", "--unshare-all", "--ro-bind", "/", "/", "true"], capture_output=True)
    return done.returncode == 0


need_bwrap = unittest.skipUnless(LATEX and bwrap_works(), "needs LaTeX and a working bubblewrap")

# Every flag of every --help, and the test that runs it. ci_report.py's subcommands are listed as "sub --flag".
FLAGS = {
    "build.py": {
        "-h/--help": "Inventory.test_inventory", "DOC": "Build.test_list_and_docs", "--timeout": "Build.test_timeout",
        "--sandbox": "Build.test_sandbox", "--clean": "Fresh.test_new_template_clean",
        "--new": "Fresh.test_new_template_clean", "--template": "Fresh.test_new_template_clean",
        "--docx": "Build.test_docx", "--html": "Build.test_html", "--format": "Build.test_format_check",
        "--arxiv": "Build.test_extras", "--cited-bib": "Build.test_extras", "--list": "Build.test_list_and_docs",
        "--changed-since": "Build.test_changed_since", "-j/--jobs": "Build.test_jobs_profile",
        "--force": "Build.test_force", "--watch": "Build.test_watch", "--open": "Build.test_watch",
        "--source": "Build.test_timeout", "--profile": "Build.test_jobs_profile", "--focus": "Build.test_focus",
        "--review": "Build.test_review", "--review-pdf": "Build.test_review_pdf",
        "--no-comments": "Build.test_review_pdf", "--draft": "Build.test_draft", "--strict": "Build.test_strict",
        "--quiet-log": "Build.test_quiet_log", "--tidy-bib": "Build.test_tidy_bib", "--bib-sort": "Build.test_tidy_bib",
        "--check": "Build.test_format_check", "--deps": "Build.test_deps_query", "--query": "Build.test_deps_query",
        "--pages": "Build.test_extras", "--preflight": "Build.test_extras", "--notify": "Build.test_watch",
        "--exec": "Build.test_watch",
    },
    "serve.py": {
        "-h/--help": "Inventory.test_inventory", "DOC": "Serve.test_local_options",
        "--port": "Serve.test_local_options",
        "--host": "Serve.test_local_options", "--source": "Serve.test_local_options",
        "--no-open": "Serve.test_local_options", "--editor": "Serve.test_local_options",
        "--sandbox": "Serve.test_sandbox", "--share": "Serve.test_share_local",
        "--share-selftest": "Serve.test_share_selftest", "--gateway": "Serve.test_gateway",
    },
    "host.py": {
        "-h/--help": "Inventory.test_inventory", "--data": "Host.test_init_and_serve",
        "init": "Host.test_init_and_serve",
        "serve": "Host.test_init_and_serve", "--insecure-no-sandbox": "Host.test_init_and_serve",
    },
    "mcp_server.py": {
        "-h/--help": "Inventory.test_inventory", "DOC": "Mcp.test_docs_read_only",
        "--source": "Mcp.test_docs_read_only",
        "--read-only": "Mcp.test_docs_read_only", "--allow-build-config": "Mcp.test_allow_build_config",
        "--sandbox": "Mcp.test_sandbox",
    },
    "ci_report.py": {
        "-h/--help": "Inventory.test_inventory", "summary": "CiReport.test_summary",
        "lint": "CiReport.test_lint_options",
        "lint documents": "CiReport.test_lint_options", "lint --strict": "CiReport.test_lint_options",
        "lint --update-baseline": "CiReport.test_lint_options", "lint --overfull-pt": "CiReport.test_lint_options",
        "lint --grammar": "CiReport.test_lint_network_options",
        "lint --bib-lookup": "CiReport.test_lint_network_options",
        "lint --verify-citations": "CiReport.test_lint_network_options", "lint --format": "CiReport.test_lint_options",
        "diff": "CiReport.test_diff_and_pr_comment", "diff --base": "CiReport.test_diff_and_pr_comment",
        "diff --out": "CiReport.test_diff_and_pr_comment", "pr-comment": "CiReport.test_diff_and_pr_comment",
        "pr-comment --diff-dir": "CiReport.test_diff_and_pr_comment",
        "pr-comment --print": "CiReport.test_diff_and_pr_comment",
    },
    "publish_release.py": {"-h/--help": "Publish.test_help_and_publish"},
}

PAPER = r"""\documentclass{article}
\usepackage{graphicx}
\title{Flag Paper}
\author{A. Tester}
\begin{document}
\maketitle
\section{Intro}\label{sec:intro}
See \cite{knuth} and Section~\ref{sec:intro}.
\includegraphics[width=1cm]{fig.png}
\input{ch/one}
\newpage
Second page.
\bibliographystyle{plain}
\bibliography{refs}
\end{document}
"""
BIB = """@book{knuth,
  author = {Donald Knuth},
  title = {The TeXbook},
  publisher = {Addison-Wesley},
  year = {1984},
}
@article{unused,
  author = {Nobody},
  title = {Never cited},
  journal = {Nowhere},
  year = {2000},
}
"""
# Unindented list items (tex-fmt indents them) and a box 5pt too wide (an overfull box between 1 and 10 pt).
OTHER = ("\\documentclass{article}\n\\begin{document}\nTest words for the other document.\n\n"
         "\\begin{itemize}\n\\item one\n\\end{itemize}\n\n\\noindent\\hbox to \\dimexpr\\linewidth+5pt\\relax{}\n"
         "\\end{document}\n")


def simple(body: str) -> str:
    return f"\\documentclass{{article}}\n\\begin{{document}}\n{body}\n\\end{{document}}\n"


def free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def pdf_text(path: Path) -> str:
    return subprocess.run(["pdftotext", str(path), "-"], capture_output=True, text=True, check=True).stdout


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *args):  # a share link answers with its cookie and a redirect: keep both
        return None


def get(url: str, headers: dict | None = None, timeout: float = 10) -> tuple[int, dict, bytes]:
    """GET without proxies or redirects: (status, headers, body)."""
    request = urllib.request.Request(url, headers=headers or {})
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}), NoRedirect)
    try:
        with opener.open(request, timeout=timeout) as reply:
            return reply.status, dict(reply.headers), reply.read()
    except urllib.error.HTTPError as error:
        return error.code, dict(error.headers), error.read()


class Workspace:
    """A temporary repository: a copy of scripts/, files/, git, and shims on PATH that log to shims.log."""

    def __init__(self) -> None:
        self.tmp = tempfile.TemporaryDirectory(prefix="cli-flags-")
        self.base = Path(self.tmp.name).resolve()
        self.root = self.base / "repo"
        shutil.copytree(REPO / "scripts", self.root / "scripts",
                        ignore=shutil.ignore_patterns("__pycache__", "vendor", "*.pyc"))
        (self.root / "files").mkdir()
        self.bin = self.base / "bin"
        self.bin.mkdir()
        self.log = self.base / "shims.log"
        record = (f"#!{sys.executable}\nimport json, os, sys\n"
                  "with open(os.environ['SHIM_LOG'], 'a') as f:\n"
                  "    f.write(json.dumps([os.path.basename(sys.argv[0]), *sys.argv[1:]]) + '\\n')\n")
        gh = record + ("a = sys.argv[1:]\n"
                       "if a[:2] == ['release', 'view'] and 'body' in a: sys.exit(1)\n"
                       "if a[:2] == ['release', 'view'] and 'assets' in a: print('paper.pdf\\nold_2Fgone.pdf')\n")
        for name, text in (("gh", gh), ("xdg-open", record), ("notify-send", record), ("browser", record)):
            (self.bin / name).write_text(text, encoding="utf-8")
            (self.bin / name).chmod(0o755)
        self.git("init", "-q", "-b", "main")

    def env(self, extra: dict | None = None) -> dict:
        env = {key: value for key, value in os.environ.items()
               if key not in ("LATEX_SANDBOX", "LP_HOST_SECRET", "LANGUAGETOOL_URL", "GITHUB_ACTIONS", "BROWSER")}
        env.update(PATH=f"{self.bin}{os.pathsep}{env.get('PATH', '')}", SHIM_LOG=str(self.log),
                   PYTHONIOENCODING="utf-8",
                   GIT_AUTHOR_NAME="T", GIT_AUTHOR_EMAIL="t@example.org", GIT_COMMITTER_NAME="T",
                   GIT_COMMITTER_EMAIL="t@example.org", GIT_CONFIG_GLOBAL=os.devnull)
        env.update(extra or {})
        return env

    def git(self, *args: str) -> str:
        return subprocess.run(["git", *args], cwd=self.root, env=self.env(), capture_output=True, text=True,
                              check=True).stdout

    def script(self, name: str) -> list[str]:
        return [sys.executable, str(self.root / "scripts" / name)]

    def run(self, name: str, *args: str, env: dict | None = None, timeout: float = 300,
            stdin: str | None = None) -> subprocess.CompletedProcess:
        return subprocess.run([*self.script(name), *args], cwd=self.root, env=self.env(env), capture_output=True,
                              text=True, encoding="utf-8", timeout=timeout, input=stdin)

    def popen(self, name: str, *args: str, env: dict | None = None) -> subprocess.Popen:
        return subprocess.Popen([*self.script(name), *args], cwd=self.root, env=self.env(env), stdout=subprocess.PIPE,
                                stderr=subprocess.STDOUT, text=True, encoding="utf-8")

    def write(self, rel: str, text: str | bytes) -> Path:
        path = self.base / rel if not rel.startswith("files/") else self.root / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        (path.write_bytes if isinstance(text, bytes) else path.write_text)(text)
        return path

    def source(self, name: str, doc: str, main: str) -> Path:
        """A --source directory outside the repository holding one document <doc>."""
        self.write(f"{name}/{doc}/main.tex", main)
        return self.base / name

    def shim_calls(self, name: str) -> list[list[str]]:
        if not self.log.exists():
            return []
        return [call[1:] for call in map(json.loads, self.log.read_text().splitlines()) if call[0] == name]

    def cache_name(self, doc: str) -> str:
        code = ("import sys; from pathlib import Path; sys.path.insert(0, 'scripts'); import build; "
                f"print(build.cache_dir_for(Path('files/{doc}/main.tex').resolve()).name)")
        return subprocess.run([sys.executable, "-I", "-c", code], cwd=self.root, capture_output=True, text=True,
                              check=True).stdout.strip()


WS: Workspace | None = None
FIRST: subprocess.CompletedProcess | None = None


def workspace() -> Workspace:
    """The shared repository with files/paper and files/other, committed and built once (-j 2 --profile)."""
    global WS, FIRST
    if WS is None:
        WS = Workspace()
        WS.write("files/paper/main.tex", PAPER)
        WS.write("files/paper/ch/one.tex", "Chapter one text.\n")
        WS.write("files/paper/refs.bib", BIB)
        WS.write("files/paper/fig.png", PNG)
        WS.write("files/other/main.tex", OTHER)
        WS.git("add", "-A")
        WS.git("commit", "-q", "-m", "documents")
        FIRST = WS.run("build.py", "-j", "2", "--profile")
    return WS


def tearDownModule() -> None:
    if WS is not None:
        WS.tmp.cleanup()


def outside(ws: Workspace) -> Path:
    """A file outside every document and build directory: readable without the sandbox, hidden inside it."""
    return ws.write("secret/outside.tex", "secret")


def probe(ws: Workspace) -> str:
    path = outside(ws)
    return simple(f"\\IfFileExists{{{path.as_posix()}}}{{Outside readable.}}{{Outside hidden.}}")


class Inventory(unittest.TestCase):
    def help_flags(self, script: str, *sub: str) -> set[str]:
        done = subprocess.run([sys.executable, str(REPO / "scripts" / script), *sub, "--help"], capture_output=True,
                              text=True, encoding="utf-8", timeout=60, env={**os.environ, "COLUMNS": "200"})
        self.assertEqual(done.returncode, 0, done.stderr)
        text = done.stdout + done.stderr  # mcp_server.py: stdout is the protocol's, so its help goes to stderr
        usage = text.split("\n\n")[0]
        flags = set(re.findall(r"(?<=[\[ ])(--?[a-z][\w-]*)", usage))
        for short, long in re.findall(r"^\s+(-\w)(?: [A-Z]+)?, (--[\w-]+)", text, re.M):  # 3.9: "-j N, --jobs N"
            flags -= {short, long}
            flags.add(f"{short}/{long}")
        flags.discard("-h")
        flags.discard("--help")
        flags.add("-h/--help")
        return flags

    def test_inventory(self):
        module = sys.modules[__name__]
        covered = {name for name in dir(module) if isinstance(getattr(module, name), type)}
        for script, wanted in FLAGS.items():
            for test in wanted.values():
                cls, method = test.split(".")
                self.assertIn(cls, covered, test)
                self.assertTrue(hasattr(getattr(module, cls), method), test)
            if script == "ci_report.py":
                found = {"-h/--help", "summary", "lint", "diff", "pr-comment", "lint documents"}
                for sub in ("summary", "lint", "diff", "pr-comment"):
                    found |= {f"{sub} {flag}" for flag in self.help_flags(script, sub) if flag != "-h/--help"}
            else:
                found = self.help_flags(script, *(["--data", "x"] if script == "host.py" else []))
                done = subprocess.run([sys.executable, str(REPO / "scripts" / script), "--help"], capture_output=True,
                                      text=True, encoding="utf-8", timeout=60)
                usage = done.stdout + done.stderr
                if "DOC ..." in usage:
                    found.add("DOC")
                if "{init,serve}" in usage:
                    found |= {"init", "serve"}
            self.assertEqual(set(wanted), found, f"{script}: every --help flag needs a test in FLAGS")


@need_latex
class Build(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.ws = workspace()

    def ok(self, *args, **kw) -> str:
        done = self.ws.run("build.py", *args, **kw)
        self.assertEqual(done.returncode, 0, done.stdout + done.stderr)
        return done.stdout + done.stderr

    def test_jobs_profile(self):
        out = FIRST.stdout + FIRST.stderr
        self.assertEqual(FIRST.returncode, 0, out)
        self.assertEqual(len(re.findall(r"^  SUCCESS  (paper|other) ", out, re.M)), 2, out)
        self.assertIn("phases: ", out)  # --profile
        self.assertIn("Found 2 document(s)", out)
        bad = self.ws.run("build.py", "-j", "0")
        self.assertEqual(bad.returncode, 2)
        self.assertIn("--jobs must be at least 1", bad.stderr)

    def test_list_and_docs(self):
        out = self.ok("--list")
        self.assertIn("files/paper/main.tex", out)
        self.assertIn("files/other/main.tex", out)
        out = self.ok("--list", "pap*")
        self.assertIn("files/paper/main.tex", out)
        self.assertNotIn("files/other/main.tex", out)
        missing = self.ws.run("build.py", "--list", "nope")
        self.assertEqual(missing.returncode, 2)
        self.assertIn("No document matches: nope", missing.stdout + missing.stderr)

    def test_force(self):
        self.ok("paper")  # Whatever earlier tests left stale.
        self.assertIn("Everything is up to date.", self.ok("paper"))
        self.assertIn("Found 1 document(s)", self.ok("paper", "--force"))

    def test_timeout(self):
        bad = self.ws.run("build.py", "--timeout", "5")
        self.assertEqual(bad.returncode, 2)
        self.assertIn("--timeout must be 10 to 7200", bad.stderr)
        src = self.ws.source("slow", "slowdoc", simple("\\loop\\iftrue\\repeat"))
        done = self.ws.run("build.py", "--source", str(src), "--timeout", "10", timeout=200)
        self.assertEqual(done.returncode, 1, done.stdout)
        self.assertIn("Build timed out after 10 s", done.stdout + done.stderr)
        self.assertIn("slowdoc", self.ok("--source", str(src), "--list"))

    @need_bwrap
    def test_sandbox(self):
        src = self.ws.source("sbx", "sbxdoc", probe(self.ws))
        self.ok("--source", str(src), "--force")
        self.assertIn("Outside readable.", pdf_text(self.ws.root / "out" / "sbxdoc.pdf"))
        self.ok("--source", str(src), "--force", "--sandbox")
        self.assertIn("Outside hidden.", pdf_text(self.ws.root / "out" / "sbxdoc.pdf"))

    def test_extras(self):
        out = self.ok("paper", "--cited-bib", "--arxiv", "--preflight", "--pages", "2")
        cited = (self.ws.root / "out" / "paper.cited.bib").read_text()
        self.assertIn("@book{knuth", cited)
        self.assertNotIn("unused", cited)
        with zipfile.ZipFile(self.ws.root / "out" / "paper.arxiv.zip") as z:
            names = set(z.namelist())
        self.assertTrue({"main.tex", "ch/one.tex", "fig.png"} <= names, names)
        self.assertTrue(any(name.endswith(".bbl") for name in names), names)
        self.assertIn("Preflight", out)
        pages = self.ws.root / "out" / "paper.pages.pdf"
        info = subprocess.run(["pdfinfo", str(pages)], capture_output=True, text=True, check=True).stdout
        self.assertRegex(info, r"Pages:\s+1\n")
        self.assertIn("Second page.", pdf_text(pages))

    def test_docx(self):
        if not shutil.which("pandoc"):
            self.skipTest("needs pandoc")
        self.assertIn("DOCX paper", self.ok("paper", "--docx"))
        with zipfile.ZipFile(self.ws.root / "out" / "paper.docx") as z:
            self.assertIn("Chapter one text.", z.read("word/document.xml").decode())

    def test_html(self):
        if not shutil.which("make4ht"):
            self.skipTest("needs make4ht")
        self.assertIn("HTML other", self.ok("other", "--html"))
        with zipfile.ZipFile(self.ws.root / "out" / "other.html.zip") as z:
            pages = [name for name in z.namelist() if name.endswith(".html")]
            self.assertTrue(pages)
            self.assertIn("Test words", "".join(z.read(name).decode("utf-8", "replace") for name in pages))

    def test_format_check(self):
        bad = self.ws.run("build.py", "--check")
        self.assertEqual(bad.returncode, 2)
        self.assertIn("--check needs --format or --tidy-bib", bad.stderr)
        if not shutil.which("tex-fmt"):
            self.skipTest("needs tex-fmt")
        src = self.ws.source("fmt", "fmtdoc", simple("\\begin{itemize}\n\\item one\n\\end{itemize}"))
        main = src / "fmtdoc" / "main.tex"
        before = main.read_text()
        self.assertEqual(self.ws.run("build.py", "--source", str(src), "--format", "--check").returncode, 1)
        self.assertEqual(main.read_text(), before, "--check changes nothing")
        self.ok("--source", str(src), "--format")
        self.assertIn("  \\item one", main.read_text())
        self.ok("--source", str(src), "--format", "--check")

    def test_tidy_bib(self):
        src = self.ws.source("bib", "bibdoc", simple("Text."))
        bib = src / "bibdoc" / "refs.bib"
        bib.write_text("@misc{aaa, title={A}, year={2005}}\n@misc{zzz, title={Z}, year={1990}}\n")
        self.assertEqual(self.ws.run("build.py", "--source", str(src), "--tidy-bib", "--check",
                                     "--bib-sort", "year").returncode, 1)
        self.ok("--source", str(src), "--tidy-bib", "--bib-sort", "year")
        text = bib.read_text()
        self.assertLess(text.index("zzz"), text.index("aaa"), text)
        self.ok("--source", str(src), "--tidy-bib", "--check", "--bib-sort", "year")
        self.assertEqual(self.ws.run("build.py", "--source", str(src), "--tidy-bib", "--check",
                                     "--bib-sort", "key").returncode, 1)

    def test_changed_since(self):
        main = self.ws.root / "files" / "other" / "main.tex"
        main.write_text(OTHER + "% changed\n")
        try:
            out = self.ok("--list", "--changed-since", "HEAD")
            self.assertIn("files/other/main.tex", out)
            self.assertNotIn("files/paper/main.tex", out)
        finally:
            main.write_text(OTHER)

    def test_deps_query(self):
        self.ok("paper")
        deps = json.loads(self.ws.run("build.py", "paper", "--deps", "json").stdout)
        self.assertEqual(deps["target"], "out/paper.pdf")
        self.assertTrue({"files/paper/main.tex", "files/paper/ch/one.tex", "files/paper/fig.png"}
                        <= set(deps["inputs"]), deps)
        self.assertTrue(self.ws.run("build.py", "paper", "--deps", "make").stdout.startswith("out/paper.pdf:"))
        query = json.loads(self.ws.run("build.py", "paper", "--query").stdout)
        text = json.dumps(query)
        self.assertIn("Flag Paper", text)
        self.assertIn("sec:intro", text)

    def test_focus(self):
        self.ok("paper")
        self.ok("paper", "--focus", "ch/one.tex")
        self.assertIn("Chapter one text.", pdf_text(self.ws.root / "out" / "paper.focus.pdf"))
        bad = self.ws.run("build.py", "paper", "--focus", "auto")
        self.assertEqual(bad.returncode, 2)

    def test_review(self):
        src = self.ws.source("rev", "revdoc", simple("Kept \\lpadd{added words} \\lpdel{deleted words} end."))
        self.ok("--source", str(src), "--force", "--review", "show")
        shown = pdf_text(self.ws.root / "out" / "revdoc.pdf")
        self.assertIn("added words", shown)
        self.assertIn("deleted words", shown)
        self.ok("--source", str(src), "--force", "--review", "clean")
        clean = pdf_text(self.ws.root / "out" / "revdoc.pdf")
        self.assertIn("added words", clean)
        self.assertNotIn("deleted words", clean)

    def test_review_pdf(self):
        text = PAPER
        at = text.index("Second page.")
        anchor = {"from": at, "to": at + 12, "quote": "Second page.", "prefix": text[at - 32:at],
                  "suffix": text[at + 12:at + 44]}
        data = {"suggestions": [{"id": "s1", "path": "main.tex", "anchor": anchor, "insert": "Second sheet."}],
                "threads": [{"id": "t1", "path": "main.tex", "anchor": anchor,
                             "comments": [{"name": "Ann", "text": "Please cite more."}]}]}
        store = self.ws.root / ".latex-history" / self.ws.cache_name("paper") / "review.json"
        store.parent.mkdir(parents=True, exist_ok=True)
        store.write_text(json.dumps(data))
        review = self.ws.root / "out" / "paper.review.pdf"
        self.ok("paper", "--review-pdf")
        words = " ".join(pdf_text(review).split())
        self.assertIn("sheet", words)
        self.assertIn("Please cite more.", words)
        self.ok("paper", "--review-pdf", "--no-comments")
        words = " ".join(pdf_text(review).split())
        self.assertIn("sheet", words)
        self.assertNotIn("Please cite more.", words)
        self.assertEqual(self.ws.run("build.py", "paper", "--no-comments").returncode, 2)

    def images(self) -> int:
        listing = subprocess.run(["pdfimages", "-list", str(self.ws.root / "out" / "paper.pdf")], capture_output=True,
                                 text=True, check=True).stdout
        return len(listing.splitlines()) - 2  # two header lines

    def test_draft(self):
        try:
            self.ok("paper", "--force", "--draft")
            self.assertEqual(self.images(), 0, "graphicx draft: a box, not the image")
        finally:
            self.ok("paper", "--force")
        self.assertGreater(self.images(), 0)

    def test_strict(self):
        src = self.ws.source("strict", "strictdoc", simple("See \\ref{missing}."))
        self.ok("--source", str(src), "--force")
        done = self.ws.run("build.py", "--source", str(src), "--force", "--strict")
        self.assertEqual(done.returncode, 1, done.stdout)
        self.assertIn("FAILED", done.stdout)

    def test_quiet_log(self):
        loud = self.ok("other", "--force")
        quiet = self.ok("other", "--force", "--quiet-log")
        self.assertIn("SUCCESS", quiet)
        self.assertLess(len(quiet.splitlines()), len(loud.splitlines()), quiet)

    def test_watch(self):
        src = self.ws.source("watch", "watchdoc", simple("Watched."))
        marker = self.ws.base / "exec.json"
        code = (f"import json, os; open({str(marker)!r}, 'w')"
                ".write(json.dumps([os.environ['LP_DOC'], os.environ['LP_PDF']]))")
        proc = self.ws.popen("build.py", "--source", str(src), "--watch", "--open", "--notify",
                             "--exec", f"{sys.executable} -c \"{code}\"")
        try:
            deadline = time.monotonic() + 180
            while time.monotonic() < deadline and not (marker.exists() and self.ws.shim_calls("notify-send")):
                time.sleep(0.5)
            self.assertTrue(marker.exists(), "--exec ran after the build")
            doc, pdf = json.loads(marker.read_text())
            self.assertEqual(doc, "watchdoc")
            self.assertEqual(Path(pdf), self.ws.root / "out" / "watchdoc.pdf")
            self.assertIn(["--app-name=LaTeX build", "--", "watchdoc: built", "out/watchdoc.pdf"],
                          self.ws.shim_calls("notify-send"))
            deadline = time.monotonic() + 10
            while time.monotonic() < deadline and not self.ws.shim_calls("xdg-open"):
                time.sleep(0.2)
            self.assertIn([str(self.ws.root / "out" / "watchdoc.pdf")], self.ws.shim_calls("xdg-open"))
        finally:
            proc.terminate()
            proc.communicate(timeout=30)
        for flag in ("--open", "--notify"):
            bad = self.ws.run("build.py", flag)
            self.assertEqual(bad.returncode, 2, flag)


@unittest.skipUnless(LATEX and POSIX, "needs LaTeX")
class Fresh(unittest.TestCase):
    def test_new_template_clean(self):
        ws = Workspace()
        try:
            self.assertEqual(ws.run("build.py", "--template", "report").returncode, 2)  # needs --new
            done = ws.run("build.py", "--new", "notes", "--template", "report")
            self.assertEqual(done.returncode, 0, done.stderr)
            self.assertIn("\\documentclass", (ws.root / "files" / "notes" / "main.tex").read_text())
            self.assertIn("{report}", (ws.root / "files" / "notes" / "main.tex").read_text())
            self.assertEqual(ws.run("build.py", "notes").returncode, 0)
            self.assertTrue((ws.root / "out" / "notes.pdf").exists())
            self.assertTrue((ws.root / ".latex-cache").exists())
            self.assertEqual(ws.run("build.py", "--clean").returncode, 0)
            self.assertFalse((ws.root / "out").exists())
            self.assertFalse((ws.root / ".latex-cache").exists())
            self.assertTrue((ws.root / "files" / "notes" / "main.tex").exists(), "sources stay")
        finally:
            ws.tmp.cleanup()


class Server:
    """A running serve.py or host.py: its output collected in the background."""

    def __init__(self, proc: subprocess.Popen) -> None:
        self.proc, self.lines = proc, []
        self.reader = threading.Thread(target=self._read, daemon=True)
        self.reader.start()

    def _read(self) -> None:
        for line in self.proc.stdout:
            self.lines.append(line)

    def output(self) -> str:
        return "".join(self.lines)

    def wait_for(self, pattern: str, timeout: float = 60) -> re.Match:
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            found = re.search(pattern, self.output())
            if found:
                return found
            if self.proc.poll() is not None:
                break
            time.sleep(0.1)
        raise AssertionError(f"no {pattern!r} in:\n{self.output()}")

    def stop(self) -> None:
        self.proc.terminate()
        try:
            self.proc.wait(timeout=30)
        except subprocess.TimeoutExpired:
            self.proc.kill()
            self.proc.wait()
        self.reader.join(timeout=10)  # EOF once the process is gone
        self.proc.stdout.close()


def wait_http(url: str, headers: dict | None = None, timeout: float = 60) -> tuple[int, dict, bytes]:
    deadline = time.monotonic() + timeout
    while True:
        try:
            return get(url, headers)
        except OSError:
            if time.monotonic() > deadline:
                raise
            time.sleep(0.2)


@need_latex
class Serve(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.ws = workspace()
        cls.src = cls.ws.source("served", "s1", simple("Served one."))
        cls.ws.write("served/s2/main.tex", simple("Served two."))

    def serve(self, *args: str, env: dict | None = None) -> Server:
        server = Server(self.ws.popen("serve.py", *args, env=env))
        self.addCleanup(server.stop)
        return server

    def test_local_options(self):
        port = free_port()
        browser = {"BROWSER": str(self.ws.bin / "browser")}
        server = self.serve("s1", "--source", str(self.src), "--no-open", "--port", str(port), "--host", "127.0.0.1",
                            "--editor", "cursor", env=browser)
        base = f"http://127.0.0.1:{port}/"
        status, _, body = wait_http(base + "api/health")
        self.assertEqual(status, 200)
        self.assertEqual(json.loads(body)["docs"], ["s1"])  # DOC selects one of the two
        status, _, body = get(base + "api/config?doc=s1")
        self.assertEqual(json.loads(body)["editor"], "cursor")
        server.wait_for(rf"Serving http://localhost:{port}/#s1")
        time.sleep(1)
        self.assertEqual(self.ws.shim_calls("browser"), [], "--no-open")
        server.stop()
        port = free_port()
        self.serve("s1", "--source", str(self.src), "--port", str(port), env=browser)
        deadline = time.monotonic() + 20
        while time.monotonic() < deadline and not self.ws.shim_calls("browser"):
            time.sleep(0.2)
        self.assertIn([f"http://localhost:{port}/#s1"], self.ws.shim_calls("browser"), "without --no-open")

    @need_bwrap
    def test_sandbox(self):
        src = self.ws.source("servesbx", "servesbx", probe(self.ws))
        pdf = self.ws.root / "out" / "servesbx.pdf"
        pdf.unlink(missing_ok=True)
        self.serve("--source", str(src), "--no-open", "--port", str(free_port()), "--sandbox")
        deadline = time.monotonic() + 180
        while time.monotonic() < deadline and not pdf.exists():
            time.sleep(0.5)
        time.sleep(1)
        self.assertIn("Outside hidden.", pdf_text(pdf))

    def test_share_local(self):
        port = free_port()
        server = self.serve("s1", "--source", str(self.src), "--no-open", "--port", str(port), "--share", "local")
        view = server.wait_for(r"view \(read-only\): (\S+)").group(1)
        edit = server.wait_for(r"edit\s+: (\S+)").group(1)
        base = f"http://127.0.0.1:{port}/"
        self.assertEqual(get(base + "api/health")[0], 401, "sharing on: a token is required")
        for link, role in ((view, "view"), (edit, "edit")):
            token = re.search(r"token=([\w-]+)", link).group(1)
            status, headers, _ = get(f"{base}?token={token}")
            cookie = headers["Set-Cookie"].split(";")[0]
            status, _, body = get(base + "api/config?doc=s1", {"Cookie": cookie})
            self.assertEqual((status, json.loads(body)["role"]), (200, role))

    def test_share_selftest(self):
        done = self.ws.run("serve.py", "--share", "local", "--share-selftest", "--port", str(free_port()), timeout=180)
        out = done.stdout + done.stderr
        self.assertEqual(done.returncode, 0, out)
        for check in ("GET /api/health", "token required", "WebSocket echo", "30 s long-poll"):
            self.assertIn(check, out)

    def test_gateway(self):
        project = self.ws.base / "gw" / "gwdoc"
        self.ws.write("gw/gwdoc/main.tex", simple("Gateway."))
        bad = self.ws.run("serve.py", "--gateway", "--source", str(project), env={"LP_HOST_SECRET": "short"})
        self.assertEqual(bad.returncode, 2)
        server = self.serve("--gateway", "--source", str(project), "--host", "127.0.0.1", "--port", "0", "--no-open",
                            env={"LP_HOST_SECRET": SECRET})
        port = int(server.wait_for(r"LP_GATEWAY_PORT=(\d+)").group(1))
        url = f"http://127.0.0.1:{port}/api/health"
        self.assertIn(get(url)[0], (401, 403), "no secret, no access")
        wrong = {"X-Host-Secret": "x" * 40, "X-Host-Role": "edit", "X-Host-User": "u1;User"}
        self.assertIn(get(url, wrong)[0], (401, 403))
        status, _, body = get(url, {**wrong, "X-Host-Secret": SECRET})
        self.assertEqual(status, 200)
        self.assertEqual(json.loads(body)["docs"], ["gwdoc"])


@need_latex
class Host(unittest.TestCase):
    def test_init_and_serve(self):
        ws = workspace()
        data = ws.base / "hostdata"
        env = {"LP_ADMIN_EMAIL": "root@example.org", "LP_ADMIN_PASSWORD": "password-123", "LP_ADMIN_NAME": "Root"}
        done = ws.run("host.py", "--data", str(data), "init", env=env)
        self.assertEqual(done.returncode, 0, done.stdout + done.stderr)
        self.assertTrue((data / "host.db").exists())
        port = free_port()
        origin = f"http://127.0.0.1:{port}"
        (data / "config.toml").write_text(f'public_url = "{origin}"\nport = {port}\n')
        server = Server(ws.popen("host.py", "--data", str(data), "--insecure-no-sandbox", "serve"))
        self.addCleanup(server.stop)
        status, _, body = wait_http(origin + "/api/me")
        self.assertEqual(status, 200)
        self.assertFalse(json.loads(body).get("user"), "not signed in yet")
        opener = urllib.request.build_opener(urllib.request.ProxyHandler({}), urllib.request.HTTPCookieProcessor())
        csrf = {}

        def api(method, path, body=None):
            headers = {"Origin": origin, "Content-Type": "application/json", **csrf}
            request = urllib.request.Request(origin + path, method=method, headers=headers,
                                             data=None if body is None else json.dumps(body).encode())
            with opener.open(request, timeout=60) as reply:
                data = json.loads(reply.read() or b"{}")
            if data.get("csrf"):
                csrf["X-CSRF-Token"] = data["csrf"]
            return data

        api("POST", "/api/login", {"email": "root@example.org", "password": "password-123"})
        tid = api("POST", "/api/admin/tenants", {"name": "W"})["id"]
        pid = api("POST", f"/api/tenants/{tid}/projects", {"name": "hosted"})["id"]
        # The project's worker (serve.py --gateway) starts without bubblewrap because of --insecure-no-sandbox.
        health = api("GET", f"/p/{pid}/api/health")
        self.assertEqual(health["docs"], ["hosted"])


def mcp(ws: Workspace, args: list[str], calls: list[tuple[str, dict]], timeout: float = 300) -> list[dict]:
    messages = [{"jsonrpc": "2.0", "id": 0, "method": "initialize",
                 "params": {"protocolVersion": "2025-06-18", "clientInfo": {"name": "flags"}, "capabilities": {}}},
                {"jsonrpc": "2.0", "method": "notifications/initialized"}]
    messages += [{"jsonrpc": "2.0", "id": i, "method": method, "params": params}
                 for i, (method, params) in enumerate(calls, 1)]
    done = ws.run("mcp_server.py", *args, stdin="".join(json.dumps(m) + "\n" for m in messages), timeout=timeout)
    replies = {reply["id"]: reply for reply in map(json.loads, done.stdout.splitlines())}
    assert len(replies) == len(calls) + 1, done.stdout + done.stderr
    return [replies[i] for i in range(1, len(calls) + 1)]


def tool(name: str, **arguments) -> tuple[str, dict]:
    return "tools/call", {"name": name, "arguments": arguments}


@need_latex
class Mcp(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.ws = workspace()
        cls.src = cls.ws.source("mcpsrc", "m1", simple("MCP one."))
        cls.ws.write("mcpsrc/m2/main.tex", simple("MCP two."))

    def test_docs_read_only(self):
        listed, projects = mcp(self.ws, ["--source", str(self.src), "m1", "--read-only"],
                               [("tools/list", {}), tool("list_projects")])
        names = {t["name"] for t in listed["result"]["tools"]}
        self.assertIn("read_file", names)
        self.assertFalse(names & {"write_file", "edit_file", "build", "add_comment"}, names)
        self.assertIn("m1", json.dumps(projects["result"]))
        self.assertNotIn("m2", json.dumps(projects["result"]))
        listed, = mcp(self.ws, ["--source", str(self.src)], [("tools/list", {})])
        self.assertIn("write_file", {t["name"] for t in listed["result"]["tools"]})

    def test_allow_build_config(self):
        write = tool("write_file", project="m2", path="build.toml", content="engine = \"pdflatex\"\n", overwrite=True)
        refused, = mcp(self.ws, ["--source", str(self.src), "m2"], [write])
        self.assertTrue(refused["result"].get("isError"), refused)
        self.assertFalse((self.src / "m2" / "build.toml").exists())
        allowed, = mcp(self.ws, ["--source", str(self.src), "m2", "--allow-build-config"], [write])
        self.assertFalse(allowed["result"].get("isError"), allowed)
        self.assertEqual((self.src / "m2" / "build.toml").read_text(), "engine = \"pdflatex\"\n")

    @need_bwrap
    def test_sandbox(self):
        src = self.ws.source("mcpsbx", "mcpsbx", probe(self.ws))
        built, = mcp(self.ws, ["--source", str(src), "--sandbox"], [tool("build", project="mcpsbx", force=True)])
        self.assertFalse(built["result"].get("isError"), built)
        self.assertIn("Outside hidden.", pdf_text(self.ws.root / "out" / "mcpsbx.pdf"))


class FakeLanguageTool(BaseHTTPRequestHandler):
    def log_message(self, *args):
        pass

    def reply(self, data) -> None:
        body = json.dumps(data).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):  # /v2/languages: the probe of grammar = "auto"
        self.reply([{"name": "English", "code": "en", "longCode": "en-US"}])

    def do_POST(self):
        self.rfile.read(int(self.headers.get("Content-Length") or 0))
        self.reply({"matches": [{"message": "Fake finding", "offset": 0, "length": 4, "replacements": [{"value": "x"}],
                                 "rule": {"id": "FAKE_RULE", "category": {"id": "TYPOS"}}}]})


@need_latex
class CiReport(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.ws = workspace()

    def report(self) -> dict:
        return json.loads((self.ws.root / "out" / "lint-report.json").read_text())["documents"]

    def build_both(self):
        done = self.ws.run("build.py", "--force")
        self.assertEqual(done.returncode, 0, done.stdout)

    def test_summary(self):
        self.build_both()
        done = self.ws.run("ci_report.py", "summary")
        self.assertEqual(done.returncode, 0, done.stderr)
        self.assertIn("paper", done.stdout)
        self.assertIn("other", done.stdout)
        self.assertIn("|", done.stdout)

    def test_lint_options(self):
        self.build_both()
        baseline = self.ws.root / "files" / "other" / ".lint-baseline"
        self.addCleanup(baseline.unlink, missing_ok=True)
        done = self.ws.run("ci_report.py", "lint", "other", "--strict")
        self.assertEqual(done.returncode, 0, done.stdout)  # the 5pt box is under the default 10pt
        self.assertEqual(list(self.report()), ["other"], "documents selects one")
        done = self.ws.run("ci_report.py", "lint", "other", "--overfull-pt", "1", "--strict")
        self.assertEqual(done.returncode, 1, done.stdout)
        self.assertTrue(any("verfull" in f["message"] for f in self.report()["other"]["findings"]))
        done = self.ws.run("ci_report.py", "lint", "other", "--overfull-pt", "1", "--update-baseline")
        self.assertEqual(done.returncode, 0)
        self.assertTrue(baseline.read_text().strip())
        done = self.ws.run("ci_report.py", "lint", "other", "--overfull-pt", "1", "--strict")
        self.assertEqual(done.returncode, 0, done.stdout)
        self.assertGreater(self.report()["other"]["suppressed"], 0)
        if shutil.which("tex-fmt"):
            self.ws.run("ci_report.py", "lint", "other", "--format")
            kinds = {f["kind"] for f in self.report()["other"]["findings"]}
            self.assertIn("format", kinds)

    def test_lint_network_options(self):
        self.build_both()
        server = ThreadingHTTPServer(("127.0.0.1", 0), FakeLanguageTool)
        threading.Thread(target=server.serve_forever, daemon=True).start()
        self.addCleanup(server.server_close)
        self.addCleanup(server.shutdown)
        closed = free_port()  # nothing listens: a Crossref request fails at once, offline
        bib = self.ws.root / "files" / "paper" / "refs.bib"
        bib.write_text(BIB + "@article{noyear, author={A. B}, title={Missing its year and journal}, doi={10.1/x}}\n")
        self.addCleanup(bib.write_text, BIB)
        env = {"LANGUAGETOOL_URL": f"http://127.0.0.1:{server.server_address[1]}",
               "HTTPS_PROXY": f"http://127.0.0.1:{closed}", "https_proxy": f"http://127.0.0.1:{closed}",
               "NO_PROXY": "", "no_proxy": ""}
        done = self.ws.run("ci_report.py", "lint", "paper", "--grammar", "--bib-lookup", "--verify-citations", env=env)
        self.assertEqual(done.returncode, 0, done.stdout + done.stderr)
        kinds = {f["kind"] for f in self.report()["paper"]["findings"]}
        self.assertIn("grammar", kinds)
        self.assertIn("no suggestion for 'noyear': Crossref is not reachable", done.stdout)  # --bib-lookup
        self.assertIn("could not verify 'knuth': Crossref is not reachable", done.stdout)  # --verify-citations

    def test_diff_and_pr_comment(self):
        if not shutil.which("latexdiff"):
            self.skipTest("needs latexdiff")
        self.build_both()
        main = self.ws.root / "files" / "other" / "main.tex"
        main.write_text(OTHER.replace("Test words", "Changed words"))
        self.addCleanup(main.write_text, OTHER)
        out = self.ws.base / "diffs"
        done = self.ws.run("ci_report.py", "diff", "--base", "HEAD", "--out", str(out))
        self.assertEqual(done.returncode, 0, done.stdout + done.stderr)
        pdfs = sorted(p.name for p in out.rglob("*.pdf"))
        self.assertEqual(pdfs, ["other.pdf"], done.stdout)
        done = self.ws.run("ci_report.py", "pr-comment", "--diff-dir", str(out), "--print")
        self.assertEqual(done.returncode, 0, done.stderr)
        self.assertIn("`other`: compiled", done.stdout)
        self.assertEqual(self.ws.shim_calls("gh"), [], "--print posts nothing")


@need_latex
class Publish(unittest.TestCase):
    def test_help_and_publish(self):
        ws = workspace()
        log = ws.log
        before = len(ws.shim_calls("gh"))
        done = ws.run("publish_release.py", "--help")
        self.assertEqual(done.returncode, 0)
        self.assertIn("usage: publish_release.py", done.stdout)
        self.assertEqual(len(ws.shim_calls("gh")), before, "--help publishes nothing")
        self.assertEqual(ws.run("build.py", "paper").returncode, 0)
        done = ws.run("publish_release.py", env={"GITHUB_SHA": "a" * 40, "BUILD_OK": "true"})
        self.assertEqual(done.returncode, 0, done.stdout + done.stderr)
        calls = ws.shim_calls("gh")[before:]
        create = ["release", "create", "pdfs", "--title", "Latest PDFs", "--notes", "", "--target", "a" * 40]
        self.assertIn(create, calls)
        uploads = [c[3] for c in calls if c[:2] == ["release", "upload"]]
        self.assertTrue(any(u.endswith("/paper.pdf#paper.pdf") for u in uploads), uploads)
        self.assertTrue(any(u.endswith("/paper.log#paper.log") for u in uploads), uploads)
        self.assertFalse(any("cited.bib" in u or "pages" in u for u in uploads), uploads)
        self.assertIn(["release", "delete-asset", "pdfs", "old_2Fgone.pdf", "--yes"], calls)
        self.assertTrue(any(c[:2] == ["release", "edit"] for c in calls))
        self.assertTrue(any(c[:3] == ["api", "--method", "PATCH"] and f"sha={'a' * 40}" in c for c in calls))
        self.assertTrue(log.exists())


if __name__ == "__main__":
    unittest.main()
