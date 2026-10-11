"""scripts/arxiv.py and build.py --arxiv: comment stripping, the archive's contents and the preflight checks."""

import contextlib
import importlib
import io
import json
import shutil
import struct
import tempfile
import unittest
import zipfile
import zlib
from pathlib import Path

from _support import build, fake_repo

arxiv = importlib.import_module("arxiv")  # after _support put scripts/ on sys.path

FIXTURE = Path(__file__).resolve().parent / "fixtures" / "arxiv"
SETTINGS = {"engine": "pdflatex", "shell_escape": False, "latexmk_args": [], "pdfa": None, "tagged": False}


def png(width, height):
    def chunk(kind, data):
        return struct.pack(">I", len(data)) + kind + data + struct.pack(">I", zlib.crc32(kind + data))
    return (b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", struct.pack(">IIBBBBB", width, height, 8, 2, 0, 0, 0))
            + chunk(b"IEND", b""))


class StripComments(unittest.TestCase):
    def test_cases(self):
        cases = {
            "% whole line\nText\n": "Text\n",
            "  % indented comment line\nText\n": "Text\n",
            "\\foo{x}% eats the newline\n{y}\n": "\\foo{x}%\n{y}\n",
            "a   % spaces before stay\n": "a   %\n",
            "50\\% done % note\n": "50\\% done %\n",
            "row\\\\% after a line break\n": "row\\\\%\n",
            "\\\\\\% escaped after a break\n": "\\\\\\% escaped after a break\n",
            "\\verb|50%| and \\verb+%+ % c\n": "\\verb|50%| and \\verb+%+ %\n",
            "\\lstinline{a%b} \\lstinline!%!\n": "\\lstinline{a%b} \\lstinline!%!\n",
            "\\url{http://x/a%20{b}} % c\n": "\\url{http://x/a%20{b}} %\n",
            "\\href{http://x/%41}{y} % c\n": "\\href{http://x/%41}{y} %\n",
            "\\begin{verbatim}\n% kept\n\\end{verbatim} % gone\n": "\\begin{verbatim}\n% kept\n\\end{verbatim} %\n",
            "x \\begin{lstlisting}% kept\n%kept\n\\end{lstlisting}\n":
                "x \\begin{lstlisting}% kept\n%kept\n\\end{lstlisting}\n",
            "\\begin{minted}{py}\n# 100%\n\\end{minted}\n": "\\begin{minted}{py}\n# 100%\n\\end{minted}\n",
            "\\iffalse\nhidden % c\n\\fi\n": "\\iffalse\nhidden %\n\\fi\n",
            "last line % no newline": "last line %",
            "\\catcode`\\%=12 % c\n": "\\catcode`\\%=12 % c\n",  # catcode changes: file left alone
        }
        for source, expected in cases.items():
            with self.subTest(source=source):
                self.assertEqual(arxiv.strip_comments(source), expected)


class Preflight(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.base = Path(tmp.name)
        self.doc = self.base / "doc"
        self.cache = self.base / "cache"
        self.doc.mkdir()
        self.cache.mkdir()

    def run_preflight(self, files, main, settings=None, extra_inputs=(), outside=()):
        (self.doc / "main.tex").write_text(main, encoding="utf-8")
        for rel, data in files.items():
            (self.doc / rel).parent.mkdir(parents=True, exist_ok=True)
            (self.doc / rel).write_bytes(data)
        inputs = ["main.tex", *files, *extra_inputs]
        (self.cache / "main.fls").write_text("".join(f"INPUT ./{rel}\n" for rel in inputs), encoding="utf-8")
        read = arxiv.read_files(self.doc / "main.tex", self.cache)
        return arxiv.preflight(self.doc / "main.tex", {**SETTINGS, **(settings or {})}, read, self.cache,
                               list(outside))

    def messages(self, findings):
        return [(f["level"], f["file"], f["message"]) for f in findings]

    def test_clean_document(self):
        found = self.run_preflight({"fig/a.png": png(10, 10)}, "\\includegraphics[width=1cm]{fig/a}\n")
        self.assertEqual(found, [])

    def test_names_hidden_files_and_big_images(self):
        found = self.run_preflight({"my fig.png": png(1, 1), "é.tex": b"", ".hidden/x.tex": b"",
                                    "big.png": png(7000, 5000)}, "x\n")
        files = [f["file"] for f in found]
        self.assertEqual(files, ["my fig.png", "é.tex", ".hidden/x.tex", "big.png"])
        self.assertIn("35 megapixels", found[-1]["message"])
        self.assertTrue(all(f["hint"] for f in found))

    def test_paths_shell_escape_missing_figures_and_outside_files(self):
        main = ("\\input{../shared/defs}\n\\includegraphics{/abs/fig}\n\\usepackage{minted}\n"
                "\\immediate\\write18{ls}\n\\includegraphics{nowhere}\n\\includegraphics{example-image}\n"
                "% \\input{../commented}\n")
        found = self.run_preflight({}, main, {"shell_escape": True}, extra_inputs=[
            "/usr/share/texlive/texmf-dist/tex/latex/mwe/example-image.pdf"], outside=[Path("/elsewhere/defs.tex")])
        got = self.messages(found)
        self.assertEqual([level for level, _, _ in got], ["error"] * 8)  # /abs/fig is also missing
        text = " | ".join(message for _, _, message in got)
        for part in ("/elsewhere/defs.tex", "\\input{../shared/defs}", "\\includegraphics{/abs/fig}",
                     "\\write18", "minted", "Figure 'nowhere'", "Figure '/abs/fig'", "shell escape"):
            self.assertIn(part, text)
        self.assertNotIn("example-image", text)  # read from TeX Live, which arXiv has
        self.assertNotIn("commented", text)

    def test_bbl_versions_and_engines(self):
        def bbl(version):
            (self.cache / "main.bbl").write_text(f"% $ biblatex bbl format version {version} $\n", encoding="utf-8")
            return self.run_preflight({}, "x\n")
        self.assertEqual(bbl("3.3"), [])
        self.assertEqual([f["level"] for f in bbl("3.2")], ["warning"])
        self.assertIn("TeX Live 2023", bbl("3.2")[0]["hint"])
        self.assertEqual([f["level"] for f in bbl("3.1")], ["error"])
        (self.cache / "main.bbl").unlink()
        self.assertEqual([f["level"] for f in self.run_preflight({}, "x\n", {"engine": "lualatex"})], ["error"])
        self.assertEqual([f["level"] for f in self.run_preflight({}, "x\n", {"tagged": True})], ["note"])
        self.assertIsNone(arxiv.readme(SETTINGS, "main.tex"))
        readme = json.loads(arxiv.readme({**SETTINGS, "engine": "xelatex"}, "main.tex"))
        self.assertEqual(readme["process"], {"compiler": "xelatex"})
        self.assertEqual(readme["sources"], [{"filename": "main.tex", "usage": "toplevel"}])

    def test_read_files_never_leave_the_document(self):
        (self.base / "secret.tex").write_text("s", encoding="utf-8")
        try:
            (self.doc / "link").symlink_to(self.base)
        except OSError:  # Windows without the symlink privilege
            pass
        (self.doc / "main.tex").write_text("x", encoding="utf-8")
        (self.cache / "main.fls").write_text(
            f"INPUT {self.base / 'secret.tex'}\nINPUT ../secret.tex\nINPUT ./link/secret.tex\nINPUT main.tex\n"
            f"INPUT {self.cache / 'main.bbl'}\n", encoding="utf-8")
        self.assertEqual(arxiv.read_files(self.doc / "main.tex", self.cache), ["main.tex"])

    def test_image_pixels(self):
        path = self.base / "a.png"
        path.write_bytes(png(30, 20))
        self.assertEqual(arxiv.image_pixels(path), 600)
        jpeg = (b"\xff\xd8" + b"\xff\xe0" + struct.pack(">H", 4) + b"JF"
                + b"\xff\xc0" + struct.pack(">HBHH", 11, 8, 40, 50) + b"\x03")
        path.write_bytes(jpeg)
        self.assertEqual(arxiv.image_pixels(path), 2000)
        path.write_bytes(b"GIF89a")
        self.assertIsNone(arxiv.image_pixels(path))


class Export(unittest.TestCase):
    def test_archive_has_only_what_the_build_read_plus_generated_files(self):
        with tempfile.TemporaryDirectory() as tmp:
            doc = Path(tmp) / "doc"
            shutil.copytree(FIXTURE, doc)
            cache = Path(tmp) / "cache"
            cache.mkdir()
            (cache / "main.fls").write_text(
                "PWD x\nINPUT main.tex\nINPUT ./chapters/one.tex\nINPUT ./figures/dot.png\n"
                f"INPUT {cache / 'main.aux'}\nINPUT {cache / 'main.bbl'}\nINPUT /usr/share/texlive/x.sty\n",
                encoding="utf-8")
            (cache / "main.bbl").write_text("\\begin{thebibliography}{1}\\end{thebibliography}\n", encoding="utf-8")
            (cache / "main.ind").write_text("index\n", encoding="utf-8")
            target = Path(tmp) / "out" / "doc.arxiv.zip"
            findings = arxiv.export(doc / "main.tex", {**SETTINGS, "engine": "xelatex"}, cache, target, [])
            self.assertEqual(findings, [])
            with zipfile.ZipFile(target) as archive:
                self.assertEqual(sorted(archive.namelist()), [
                    "00README.json", "chapters/one.tex", "figures/dot.png", "main.bbl", "main.ind", "main.tex"])
                main = archive.read("main.tex").decode()
                self.assertNotIn("full-line comment", main)
                self.assertIn("\\url{http://example.org/a%20b} and \\verb|100%| stay.", main)
                self.assertIn("% kept: inside verbatim", main)
                self.assertEqual(archive.read("figures/dot.png"), (doc / "figures" / "dot.png").read_bytes())
                first = target.read_bytes()
            arxiv.export(doc / "main.tex", {**SETTINGS, "engine": "xelatex"}, cache, target, [])
            self.assertEqual(target.read_bytes(), first)  # reproducible
            self.assertEqual(sorted(p.name for p in target.parent.iterdir()), ["doc.arxiv.zip"])  # no temp left

    def test_cli_step_reports_into_the_log_and_fails_on_errors(self):
        with fake_repo() as root, contextlib.redirect_stdout(io.StringIO()) as out:
            main = root / "files" / "paper" / "main.tex"
            main.parent.mkdir()
            main.write_text("\\input{/abs/x}\n", encoding="utf-8")
            self.assertEqual(build.export_arxiv_all([main]), 1)  # no build yet
            self.assertIn("no recorded build", out.getvalue())
            cache = build.cache_dir_for(main)
            cache.mkdir(parents=True)
            (cache / "main.fls").write_text("INPUT main.tex\n", encoding="utf-8")
            (root / "out").mkdir()
            build.log_path_for(main).write_text("Build of paper: SUCCESS\n", encoding="utf-8")
            for _ in range(2):
                self.assertEqual(build.export_arxiv_all([main]), 1)  # \input{/abs/x} is an error
            log = build.log_path_for(main).read_text(encoding="utf-8")
            self.assertTrue(log.startswith("Build of paper: SUCCESS\n"))
            self.assertEqual(log.count("===== arXiv export ====="), 1)  # replaced, not appended again
            self.assertIn("ERROR: main.tex: \\input{/abs/x} uses an absolute path", log)
            self.assertTrue(build.arxiv_path_for(main).is_file())
            self.assertEqual(build.arxiv_path_for(main), root / "out" / "paper.arxiv.zip")
            main.unlink()
            build.prune([])
            self.assertFalse(build.arxiv_path_for(main).exists())


if __name__ == "__main__":
    unittest.main()
