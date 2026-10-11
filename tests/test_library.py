"""library.py (the personal reference library), citedbib.py (cited-only .bib) and their serve.py endpoints, plus
POST /api/bib/search. Crossref is mocked (bibfix.get_json); nothing here touches the network or runs LaTeX."""

from __future__ import annotations

import json
import os
import stat
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import _support  # noqa: F401 - puts scripts/ on sys.path
import bibfix
import build
import citedbib
import library
import serve
import test_serve as ts

BIB = ("@article{lovelace1843,\n  author = {Ada Lovelace},\n  title = {Notes on the Analytical Engine},\n"
       "  journal = {Scientific Memoirs},\n  year = {1843},\n  doi = {10.1000/notes}\n}\n"
       "@book{turing,\n  author = {Alan Turing},\n  title = {A Made Up Book},\n  year = {1950}\n}\n")
RIS = "TY  - JOUR\nAU  - Hopper, Grace\nTI  - Compilers for Everyone\nPY  - 1952\nDO  - 10.1000/other\nER  - \n"


class Store(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.path = Path(tmp.name) / "config" / "latex-pipeline" / "library.json"

    def test_add_list_flag_duplicates_and_delete(self):
        out = library.change({"op": "add", "text": BIB}, self.path)
        self.assertEqual((len(out["added"]), out["skipped"]), (2, []))
        out = library.change({"op": "add", "text": RIS}, self.path)
        self.assertEqual(len(out["added"]), 1)
        twin = {"type": "misc", "key": "copy", "fields": {"title": "Other", "doi": "https://doi.org/10.1000/NOTES"}}
        out = library.change({"op": "add", "entries": [twin, {"type": "misc", "key": "bad key", "fields": {}}]},
                             self.path)
        self.assertEqual((len(out["added"]), len(out["skipped"])), (1, 1))
        by_key = {e["key"]: e for e in out["entries"]}
        self.assertEqual(by_key["copy"]["duplicates"], [by_key["lovelace1843"]["id"]])
        self.assertEqual(by_key["turing"]["duplicates"], [])
        self.assertIn("@book{turing,", by_key["turing"]["bibtex"])
        if os.name != "nt":
            self.assertEqual(stat.S_IMODE(self.path.stat().st_mode), 0o600)
            self.assertEqual(stat.S_IMODE(self.path.parent.stat().st_mode), 0o700)
        out = library.change({"op": "delete", "ids": [by_key["copy"]["id"]]}, self.path)
        self.assertEqual((out["deleted"], len(out["entries"])), (1, 3))
        self.assertNotIn("copy", [e["key"] for e in library.listing(library.read(self.path))["entries"]])

    def test_bad_input_and_damaged_store(self):
        for data in ({"op": "add", "text": "no entries"}, {"op": "add", "entries": []}, {"op": "nope"},
                     {"op": "delete", "ids": "x"}, {"op": "add", "text": 5}):
            with self.subTest(data=data), self.assertRaises(library.LibraryError):
                library.apply(library.empty(), data)
        with self.assertRaises(library.LibraryError) as caught:
            library.apply(library.empty(), {"op": "add", "text": "x" * (library.MAX_ADD + 1)})
        self.assertEqual(caught.exception.status, 413)
        self.path.parent.mkdir(parents=True)
        self.path.write_text(json.dumps({"entries": [{"id": "zz"}, {"id": "0" * 16, "type": "misc", "key": "k",
                                                                     "fields": {"title": "T"}}]}), encoding="utf-8")
        self.assertEqual([e["key"] for e in library.read(self.path)["entries"]], ["k"])
        self.path.write_text("{not json", encoding="utf-8")
        self.assertEqual(library.read(self.path), library.empty())

    def test_size_caps(self):
        with mock.patch.object(library, "MAX_ENTRIES", 1):
            _, reply = library.apply(library.empty(), {"op": "add", "text": BIB})
        self.assertEqual((len(reply["added"]), len(reply["skipped"])), (1, 1))
        with mock.patch.object(library, "MAX_STORE", 100), self.assertRaises(library.LibraryError):
            library.apply(library.empty(), {"op": "add", "text": BIB})


class CitedOnly(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.base = Path(tmp.name)
        self.root = self.base / "doc"
        self.cache = self.base / "cache"
        self.root.mkdir()
        self.cache.mkdir()
        (self.root / "refs.bib").write_text(
            "@string{sm = {Scientific Memoirs}}\n" + BIB
            + "@inproceedings{part,\n  title = {A Part},\n  crossref = {proc}\n}\n"
            "@proceedings{proc,\n  title = {The Proceedings}\n}\n@misc{unused,\n  title = {Never Cited}\n}\n",
            encoding="utf-8")
        (self.base / "secret.bib").write_text("@misc{secret,\n  title = {Outside}\n}\n", encoding="utf-8")

    def test_bibtex_aux(self):
        (self.cache / "main.aux").write_text("\\citation{Lovelace1843,part}\n\\citation{ghost}\n"
                                             "\\bibdata{refs,../secret}\n", encoding="utf-8")
        (self.cache / "ch1.aux").write_text("\\citation{turing}\n", encoding="utf-8")
        out = citedbib.export(self.root, self.cache, "main")
        self.assertEqual(sorted(out["keys"]), ["lovelace1843", "part", "proc", "turing"])  # casefold match too
        self.assertEqual((out["missing"], out["files"]), (["ghost"], ["refs.bib"]))
        self.assertIn("../secret", out["notes"][0])
        self.assertIn("@string{sm", out["text"])
        self.assertNotIn("unused", out["text"])
        self.assertNotIn("Outside", out["text"])

    def test_biblatex_bcf_and_nocite_all(self):
        (self.cache / "main.bcf").write_text(
            '<bcf:datasource type="file" datatype="bibtex">refs.bib</bcf:datasource>\n'
            '<bcf:citekey order="1" intorder="1">turing</bcf:citekey>\n', encoding="utf-8")
        self.assertEqual(citedbib.export(self.root, self.cache, "main")["keys"], ["turing"])
        (self.cache / "main.aux").write_text("\\abx@aux@cite{0}{*}\n", encoding="utf-8")
        self.assertIn("unused", citedbib.export(self.root, self.cache, "main")["keys"])

    def test_no_build_and_the_cli(self):
        with self.assertRaises(OSError):
            citedbib.export(self.root, self.cache, "main")
        main = self.root / "main.tex"
        target = self.base / "out" / "doc.cited.bib"
        with mock.patch.object(build, "cache_dir_for", return_value=self.cache), \
                mock.patch.object(build, "cited_bib_path_for", return_value=target):
            self.assertEqual(build.export_cited_bib(main)[0], None)
            (self.cache / "main.aux").write_text("\\citation{turing}\n\\bibdata{refs}\n", encoding="utf-8")
            result, lines = build.export_cited_bib(main)
        self.assertEqual(result["keys"], ["turing"])
        self.assertIn("@book{turing,", target.read_text(encoding="utf-8"))
        self.assertIn("1 entries", lines[0])


class Endpoints(ts.SharedState, ts.ServerCase):
    """GET/POST /api/library (owner only, never behind the gateway), GET /api/cited-bib (view role) and
    POST /api/bib/search (edit role)."""

    def setUp(self):
        ts.SharedState.setUp(self)
        ts.ServerCase.setUp(self)
        mock.patch.object(library, "LIBRARY_FILE", self.base / "config" / "library.json").start()
        self.cache = self.base / "cache"
        self.cache.mkdir()
        mock.patch.object(build, "cache_dir_for", return_value=self.cache).start()
        self.write("refs.bib", BIB)
        work = json.loads((Path(__file__).parent / "fixtures" / "crossref_work.json").read_text(encoding="utf-8"))
        self.fetch = mock.patch.object(bibfix, "get_json",
                                       return_value={"message": {"items": [work["message"]]}}).start()
        serve.SHARE["port"] = self.port
        self.tokens = dict(self.share_on())
        status, info = self.request("POST", "/api/share/links", {"name": "Bea", "role": "review"}, self.hdr("owner"))
        self.assertEqual(status, 200)
        link = next(x for x in info["named"] if x["name"] == "Bea")
        self.tokens["review"] = link["url"].split("token=")[1].split("#")[0]

    def hdr(self, role):
        return {"Cookie": f"{serve.cookie_name()}={self.tokens[role]}"}

    def test_library_is_the_owners_only(self):
        status, out = self.request("POST", "/api/library", {"op": "add", "text": BIB}, self.hdr("owner"))
        self.assertEqual((status, len(out["added"])), (200, 2))
        self.assertEqual(len(self.request("GET", "/api/library", headers=self.hdr("owner"))[1]["entries"]), 2)
        for role in ("edit", "review", "view"):
            with self.subTest(role=role):
                self.assertEqual(self.request("GET", "/api/library", headers=self.hdr(role))[0], 403)
                self.assertEqual(self.request("POST", "/api/library", {"op": "add", "text": BIB},
                                              self.hdr(role))[0], 403)
        self.assertEqual(self.request("POST", "/api/library", {"op": "add", "text": "nothing"},
                                      self.hdr("owner"))[0], 400)
        self.assertEqual(len(library.read()["entries"]), 2)

    def test_cited_bib_for_every_reader_of_the_document(self):
        self.assertEqual(self.request("GET", "/api/cited-bib?doc=demo", headers=self.hdr("owner"))[0], 409)
        (self.cache / "main.aux").write_text("\\citation{turing}\n\\bibdata{refs}\n", encoding="utf-8")
        for role in ("owner", "edit", "review", "view"):
            status, out = self.request("GET", "/api/cited-bib?doc=demo", headers=self.hdr(role))
            self.assertEqual((status, out["keys"]), (200, ["turing"]), role)
        self.assertEqual(self.request("GET", "/api/cited-bib?doc=other", headers=self.hdr("view"))[0], 403)

    def test_search_is_edit_role_and_returns_candidates_only(self):
        for role in ("view", "review"):
            self.assertEqual(self.request("POST", "/api/bib/search?doc=demo", {"query": "deep learning"},
                                          self.hdr(role))[0], 403)
        self.assertEqual(self.fetch.call_count, 0)
        status, out = self.request("POST", "/api/bib/search?doc=demo", {"query": "deep learning"}, self.hdr("edit"))
        self.assertEqual(status, 200)
        self.assertEqual(out["candidates"][0]["fields"]["doi"], "10.1000/xyz_123")
        self.assertIn("query.bibliographic=deep+learning", self.fetch.call_args[0][0])
        self.assertIn("error", self.request("POST", "/api/bib/search?doc=demo", {"query": "x"}, self.hdr("edit"))[1])
        self.assertEqual(self.request("POST", "/api/bib/search?doc=demo", {"query": 5}, self.hdr("edit"))[0], 400)


class Worker(ts.SharedState, ts.ServerCase):
    """Behind the gateway the library is host.py's (per account); a worker never answers it."""

    as_ = ts.GatewayMode.as_
    SECRET = ts.GatewayMode.SECRET

    def setUp(self):
        ts.SharedState.setUp(self)
        ts.ServerCase.setUp(self)
        mock.patch.dict(serve.GATEWAY, {"secret": b""}).start()
        serve.gateway_enable(self.SECRET, "demo")

    def test_worker_refuses_the_library(self):
        with mock.patch.object(library, "LIBRARY_FILE", self.base / "library.json"):
            for role in ("edit", "review", "view"):
                self.assertIn(self.as_(role, "GET", "/api/library")[0], (403, 404))
                self.assertIn(self.as_(role, "POST", "/api/library", {"op": "add", "text": BIB})[0], (403, 404))
        self.assertFalse((self.base / "library.json").exists())


if __name__ == "__main__":
    unittest.main()
