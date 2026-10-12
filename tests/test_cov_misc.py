"""vendor_ui.main with a fake fetcher, doctools' aux/toc/pdffonts parsers, and preview's figure cache check."""

from __future__ import annotations

import contextlib
import hashlib
import io
import json
import subprocess
import tarfile
import tempfile
import unittest
import zipfile
from pathlib import Path
from unittest import mock

import _support  # noqa: F401 - puts scripts/ on sys.path
import accel
import compile_api
import doctools
import preview
import vendor_ui
from _support import fake_repo, write_doc


class Vendor(unittest.TestCase):
    def test_main_follows_imports_and_writes_a_manifest(self):
        page = vendor_ui.UI_DIR.joinpath("index.html").read_text("utf-8")
        roots = json.loads(page.split('<script type="importmap">', 1)[1].split("</script>", 1)[0])["imports"]
        fetched = []

        def getter(url):
            fetched.append(url)
            if url.endswith(".css"):
                return b'@font-face{src:url("f.woff2") format("woff2"), url(f.woff) format("woff")}'
            if url.endswith(".woff2"):
                return b"wOF2"
            if "dep.mjs" in url:
                return b"export const x = 1;"
            return b'import { x } from "./dep.mjs";\nexport default x;'

        with tempfile.TemporaryDirectory() as tmp:
            out = Path(tmp) / "vendor"
            out.mkdir()
            (out / "stale.js").write_text("old")
            with contextlib.redirect_stdout(io.StringIO()):
                self.assertEqual(vendor_ui.main(out, getter), 0)
            manifest = json.loads((out / "manifest.json").read_text())["files"]
            self.assertFalse((out / "stale.js").exists())
            self.assertTrue(set(roots.values()) <= set(manifest))
            for url, entry in manifest.items():
                body = (out / entry["file"]).read_bytes()
                self.assertEqual(hashlib.sha256(body).hexdigest(), entry["sha256"], url)
                self.assertNotIn(b"https://", body)  # every reference points at a local file
        self.assertEqual(len(fetched), len(set(fetched)))  # each URL fetched once

    def test_fetch_sends_a_user_agent(self):
        response = mock.MagicMock()
        response.__enter__.return_value.read.return_value = b"body"
        with mock.patch.object(vendor_ui.urllib.request, "urlopen", return_value=response) as urlopen:
            self.assertEqual(vendor_ui.fetch("https://example.org/x.js"), b"body")
        self.assertEqual(urlopen.call_args[0][0].get_header("User-agent"), "latex-pipeline-vendor")


class DocTools(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.dir = Path(tmp.name)

    def test_group_edges(self):
        self.assertEqual(doctools.group("  {a{b}c} x", 0), ("a{b}c", 9))
        self.assertIsNone(doctools.group("x", 0))
        self.assertIsNone(doctools.group("{open", 0))
        self.assertIsNone(doctools.group("", 0))

    def test_aux_chain_without_loops(self):
        (self.dir / "main.aux").write_text("\\@input{ch.aux}\n\\@input{ch.aux}\n\\@input{missing.aux}\n"
                                           "\\newlabel{a}{{1}{2}}\n")
        (self.dir / "ch.aux").write_text("\\@input{main.aux}\n\\newlabel{b}{{1.1}{3}}\n\\newlabel{b@cref}{{x}{3}}\n")
        self.assertEqual([p.name for p in doctools.aux_files(self.dir, "main")], ["main.aux", "ch.aux"])
        self.assertEqual(doctools.labels(self.dir, "main"), [{"key": "a", "number": "1", "page": "2"},
                                                             {"key": "b", "number": "1.1", "page": "3"}])

    def test_sections(self):
        self.assertEqual(doctools.sections(self.dir, "main"), [])
        (self.dir / "main.toc").write_text(
            "\\contentsline {section}{\\numberline {1}Intro}{1}{}%\n\\contentsline {section}{Broken\n"
            "\\contentsline {subsection}{Plain}{4}{}%\n")
        found = doctools.sections(self.dir, "main")
        self.assertIn({"level": "section", "number": "1", "title": "Intro", "page": "1"}, found)
        self.assertNotIn("Broken", json.dumps(found))

    def test_fonts_table(self):
        table = ("name                                 type              encoding         emb sub uni object ID\n"
                 "------------------------------------ ----------------- ---------------- --- --- --- ---------\n"
                 "ABCDEF+CMR10                         Type 1            Builtin          yes yes no       4  0\n"
                 "Arial                                TrueType          WinAnsi          no  no  no       5  0\n")
        with mock.patch.object(doctools.shutil, "which", return_value="/usr/bin/pdffonts"), \
                mock.patch.object(doctools.subprocess, "run", return_value=subprocess.CompletedProcess([], 0, table)):
            self.assertEqual(doctools.fonts(Path("x.pdf")), [
                {"name": "ABCDEF+CMR10", "type": "Type 1", "emb": "yes"},
                {"name": "Arial", "type": "TrueType", "emb": "no"}])
        with mock.patch.object(doctools.shutil, "which", return_value="/usr/bin/pdffonts"), \
                mock.patch.object(doctools.subprocess, "run", return_value=subprocess.CompletedProcess([], 1, "")):
            self.assertEqual(doctools.fonts(Path("x.pdf")), [])

    def test_preflight_single_page_size(self):
        info = "PDF version:    1.5\nPage    1 size: 612 x 792 pts (letter)\nPage    2 size: 612 x 792 pts (letter)\n"
        with mock.patch.object(doctools, "fonts", return_value=[]), \
                mock.patch.object(doctools.shutil, "which", return_value="/usr/bin/pdfinfo"), \
                mock.patch.object(doctools.subprocess, "run", return_value=subprocess.CompletedProcess([], 0, info)):
            found = doctools.preflight(Path("x.pdf"))
        self.assertIn(("info", "page size 612 x 792 pts (letter)"), found)

    def test_word_count(self):
        with mock.patch.object(doctools.shutil, "which", return_value="/x"), \
                mock.patch.object(doctools.subprocess, "run", return_value=subprocess.CompletedProcess([], 0, "42\n")):
            self.assertEqual(doctools.word_count(self.dir), 42)
        with mock.patch.object(doctools.shutil, "which", return_value="/x"), \
                mock.patch.object(doctools.subprocess, "run", return_value=subprocess.CompletedProcess([], 1, "")):
            self.assertIsNone(doctools.word_count(self.dir))


class PreviewFigures(unittest.TestCase):
    """figures_unchanged: the preview reuses the full build's figures only while no kept file's picture changed."""

    def setUp(self):
        stack = fake_repo()
        root = stack.__enter__()
        self.addCleanup(stack.__exit__, None, None, None)
        self.main = write_doc(root, "doc", "x")
        self.chapter = self.main.parent / "ch.tex"
        self.chapter.write_text("\\begin{tikzpicture}\\draw (0,0)--(1,1);\\end{tikzpicture}\n")
        self.aux = preview.root_for(self.main) / "aux"
        (self.aux / "tikz").mkdir(parents=True)
        figure = "\n".join(accel.FIGURE_TEXT.findall(self.chapter.read_text()))
        (self.aux / "figtext.json").write_text(json.dumps({"ch.tex": hashlib.sha1(figure.encode()).hexdigest()}))
        self.warm = preview.Warm(self.main)
        self.entries = [{"kind": "n", "figs": 1, "path": "ch"}]

    def unchanged(self, overlay=None, entries=None):
        return self.warm.figures_unchanged(entries or self.entries, {0}, 0, "ch.tex", overlay)

    def test_reuse_rules(self):
        self.assertTrue(self.unchanged())
        self.assertTrue(self.unchanged(overlay="text only, same picture\n" + self.chapter.read_text()))
        self.assertFalse(self.unchanged(overlay="\\begin{tikzpicture}\\draw (0,0)--(2,2);\\end{tikzpicture}"))
        self.assertFalse(self.unchanged(entries=[{"kind": "n", "figs": -1, "path": "ch"}]))
        self.chapter.unlink()
        self.assertFalse(self.unchanged())
        (self.aux / "figtext.json").write_text("{bad")
        self.assertFalse(self.unchanged())

    def test_warm_for_reuses_and_shutdown_clears(self):
        stale = preview.root_for(self.main) / "p999-1"
        stale.mkdir(parents=True)
        with mock.patch.dict(preview.WARM, {}, clear=True):
            first = preview.warm_for(self.main)
            self.assertIs(preview.warm_for(self.main), first)
            self.assertFalse(stale.exists())  # an earlier server's process folders go
            preview.shutdown()
            self.assertEqual(preview.WARM, {})

    def test_document_inputs_without_a_recorder_file(self):
        self.assertEqual(preview.document_inputs(self.main, self.main.parent / "missing.fls"), [])


class CompileUnpack(unittest.TestCase):
    """compile_api.unpack: the refusals of untrusted uploads that test_serve's cases leave out."""

    def refused(self, body, kind, **kw):
        with self.assertRaises(compile_api.UploadError) as ctx:
            compile_api.unpack(body, kind, 10 ** 6, **kw)
        return str(ctx.exception)

    def test_json_shapes(self):
        self.assertIn("Bad JSON", self.refused(b"{nope", "application/json"))
        self.assertIn("Expected", self.refused(b"[1]", "application/json"))
        self.assertIn("give text", self.refused(json.dumps({"files": {"a.tex": 5}}).encode(), "application/vnd+json"))
        for bad in ("a\x01.tex", "x" * 256, "./../a.tex"):
            with self.subTest(bad=bad):
                self.assertIn("Unsafe path", self.refused(json.dumps({"files": {bad: "x"}}).encode(),
                                                          "application/json"))
        files, _ = compile_api.unpack(json.dumps({"files": {"./main.tex": "x", "dir/": "y"}}).encode(),
                                      "application/json", 10 ** 6)
        self.assertEqual(files, {"main.tex": b"x"})

    def test_zip_limits(self):
        buf = io.BytesIO()
        with zipfile.ZipFile(buf, "w") as archive:
            for i in range(5):
                archive.writestr(f"f{i}.tex", "x")
        self.assertIn("more than 2 files", self.refused(buf.getvalue(), "application/zip", max_files=2))
        self.assertIn("Bad zip file", self.refused(b"PK\x03\x04 broken", "application/octet-stream"))
        buf = io.BytesIO()
        with zipfile.ZipFile(buf, "w") as archive:
            archive.writestr("main.tex", "x")
        data = bytearray(buf.getvalue())
        data[6] |= 0x1  # the local header's general purpose flag: encrypted
        central = data.rfind(b"PK\x01\x02")
        data[central + 8] |= 0x1  # and the central directory's
        self.assertIn("Encrypted", self.refused(bytes(data), "application/zip"))

    def test_tar_limits_and_directories(self):
        def tar(names):
            buf = io.BytesIO()
            with tarfile.open(fileobj=buf, mode="w") as archive:
                for name in names:
                    info = tarfile.TarInfo(name)
                    if name.endswith("/"):
                        info.type = tarfile.DIRTYPE
                        archive.addfile(info)
                    else:
                        info.size = 1
                        archive.addfile(info, io.BytesIO(b"x"))
            return buf.getvalue()

        self.assertIn("more than 1 files", self.refused(tar(["a.tex", "b.tex", "c.tex"]), "application/x-tar",
                                                        max_files=1))
        files, _ = compile_api.unpack(tar(["sub/", "main.tex"]), "application/x-tar", 10 ** 6)
        self.assertEqual(files, {"main.tex": b"x"})


if __name__ == "__main__":
    unittest.main()
