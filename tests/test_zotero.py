# ruff: noqa: E501
"""zotero: settings storage, paged and incremental fetch, comparing and applying. The network is always mocked."""

import importlib
import json
import os
import stat
import sys
import tempfile
import unittest
import urllib.error
from pathlib import Path
from unittest import mock

import _support  # noqa: F401  (puts scripts/ on sys.path)

zotero = importlib.import_module("zotero")
bibfix = importlib.import_module("bibfix")

KEY = "AbCdEfGhIjKlMnOp"
ENTRY = "@article{{{key},\n  author = {{{author}}},\n  title = {{{title}}},\n  year = {{{year}}},\n  doi = {{{doi}}},\n  file = {{/home/me/x.pdf}}\n}}\n"


def entry(key="smith20", author="Smith, A.", title="A long enough title", year="2020", doi="10.1/a"):
    return ENTRY.format(key=key, author=author, title=title, year=year, doi=doi)


class Base(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.cfg = Path(tmp.name) / "cfg" / "zotero.json"
        mock.patch.object(zotero, "config_path", return_value=self.cfg).start()
        mock.patch.dict(os.environ).start()
        os.environ.pop("ZOTERO_API_KEY", None)
        zotero.CACHE.clear()
        zotero.LIMITER.hits.clear()
        self.addCleanup(mock.patch.stopall)

    def configure(self, **extra):
        return zotero.save_settings({"library_type": "users", "library_id": "12345", "key": KEY, **extra})


class Settings(Base):
    def test_key_is_stored_privately_and_never_returned(self):
        shown = self.configure(collection="ABCD2345")
        self.assertTrue(shown["has_key"] and shown["configured"])
        self.assertNotIn(KEY, json.dumps(shown))
        self.assertEqual(json.loads(self.cfg.read_text())["key"], KEY)
        if sys.platform != "win32":
            self.assertEqual(stat.S_IMODE(self.cfg.stat().st_mode), 0o600)
        self.assertNotIn(KEY, json.dumps(zotero.info()))
        zotero.save_settings({"library_id": "12345", "key": ""})  # empty keeps the key
        self.assertTrue(zotero.info()["has_key"])
        self.assertFalse(zotero.save_settings({"library_id": "12345", "clear_key": True})["has_key"])

    def test_environment_key_wins_and_is_flagged(self):
        os.environ["ZOTERO_API_KEY"] = "E" * 20
        shown = zotero.save_settings({"library_id": "7"})
        self.assertTrue(shown["key_from_env"] and shown["configured"])
        self.assertNotIn("key", json.loads(self.cfg.read_text()))

    def test_rejects_bad_values(self):
        for bad in ({"library_id": "abc"}, {"library_id": "1", "collection": "x"}, {"mode": "ftp"},
                    {"library_type": "orgs"}, {"format": "csl"}, {"library_id": "1", "key": "a b"}, {"library_id": "1", "key": 5}):
            with self.assertRaises(zotero.ZoteroError) as ctx:
                zotero.save_settings(bad)
            self.assertEqual(ctx.exception.status, 400)
        self.assertFalse(self.cfg.exists())


class Fetch(Base):
    def pages(self, total, version="10"):
        """A fake zotero.get serving `total` entries, 100 per page."""
        calls = []

        def fake(url, headers, local=False, timeout=20.0):
            calls.append((url, dict(headers)))
            if headers.get("If-Modified-Since-Version") == version:
                return 304, {}, b""
            start = int(url.rsplit("start=", 1)[1])
            body = "".join(entry(f"k{i}", title=f"Title number {i}", doi=f"10.1/{i}") for i in range(start, min(start + 100, total)))
            return 200, {"last-modified-version": version, "total-results": str(total)}, body.encode()
        return fake, calls

    def test_paginates_and_sends_the_key_only_as_a_header(self):
        fake, calls = self.pages(250)
        self.configure(collection="ABCD2345")
        with mock.patch.object(zotero, "get", fake):
            result = zotero.preview("", [], True)
        self.assertEqual(len(result["new"]), 250)
        self.assertEqual([c[0].rsplit("start=", 1)[1] for c in calls], ["0", "100", "200"])
        self.assertIn("/users/12345/collections/ABCD2345/items/top?format=bibtex", calls[0][0])
        for url, headers in calls:
            self.assertNotIn(KEY, url)
            self.assertEqual(headers["Zotero-API-Key"], KEY)
            self.assertEqual(headers["Zotero-API-Version"], "3")
        self.assertNotIn(KEY, json.dumps(result))
        self.assertTrue(all("file" not in e["fields"] for e in result["new"]))

    def test_incremental_sync_reuses_the_cache_on_304_and_refetches_when_changed(self):
        fake, calls = self.pages(3)
        self.configure()
        with mock.patch.object(zotero, "get", fake):
            first = zotero.preview("", [], True)
            second = zotero.preview("", [], True)
        self.assertEqual((first["cached"], second["cached"]), (False, True))
        self.assertEqual(len(second["new"]), 3)
        self.assertEqual(calls[1][1]["If-Modified-Since-Version"], "10")
        fake2, calls2 = self.pages(4, version="11")  # the library moved on: a full fetch with the new version
        with mock.patch.object(zotero, "get", fake2):
            third = zotero.preview("", [], True)
        self.assertEqual((third["cached"], third["version"], len(third["new"])), (False, "11", 4))

    def test_library_changing_between_pages_is_an_error(self):
        versions = iter(["5", "6"])

        def fake(url, headers, local=False, timeout=20.0):
            return 200, {"last-modified-version": next(versions), "total-results": "150"}, entry().encode()
        self.configure()
        with mock.patch.object(zotero, "get", fake), self.assertRaises(zotero.ZoteroError) as ctx:
            zotero.preview("", [], True)
        self.assertEqual(ctx.exception.status, 409)

    def test_needs_settings(self):
        with self.assertRaises(zotero.ZoteroError) as ctx:
            zotero.preview("", [], True)
        self.assertEqual(ctx.exception.status, 400)

    def test_http_errors_become_messages_without_url_or_key(self):
        self.configure()
        for code, word in ((403, "refused"), (404, "library"), (429, "wait"), (503, "busy"), (500, "500")):
            error = urllib.error.HTTPError("https://api.zotero.org/x?k=" + KEY, code, "no", {"Retry-After": "30"}, None)
            opener = mock.Mock()
            opener.open.side_effect = error
            with mock.patch.object(zotero, "_opener", return_value=opener), self.assertRaises(zotero.ZoteroError) as ctx:
                zotero.preview("", [], True)
            self.assertIn(word, str(ctx.exception))
            self.assertNotIn(KEY, str(ctx.exception))
        opener.open.side_effect = OSError("boom " + KEY)
        with mock.patch.object(zotero, "_opener", return_value=opener), self.assertRaises(zotero.ZoteroError) as ctx:
            zotero.preview("", [], True)
        self.assertNotIn(KEY, str(ctx.exception))

    def test_redirects_are_not_followed(self):
        handler = zotero._NoRedirect()
        self.assertIsNone(handler.redirect_request(mock.Mock(), None, 302, "Found", {}, "https://evil.example/"))

    def test_requests_are_throttled(self):
        zotero.LIMITER.hits.extend([(zotero.LIMITER.clock(), 0)] * zotero.LIMITER.requests)
        with self.assertRaises(zotero.ZoteroError) as ctx:
            zotero.get("https://api.zotero.org/x", {})
        self.assertEqual(ctx.exception.status, 429)

    def test_local_mode_is_refused_while_sharing_and_bypasses_proxies(self):
        zotero.save_settings({"mode": "local"})
        with self.assertRaises(zotero.ZoteroError) as ctx:
            zotero.preview("", [], False)
        self.assertEqual(ctx.exception.status, 409)
        seen = {}

        def fake(url, headers, local=False, timeout=20.0):
            seen.update(url=url, local=local, headers=headers)
            return 200, {}, entry().encode()
        with mock.patch.object(zotero, "get", fake):
            result = zotero.preview("", [], True)
        self.assertTrue(seen["local"] and seen["url"].startswith("http://127.0.0.1:23119/"))
        self.assertEqual((result["source"], len(result["new"])), ("local", 1))


class Compare(unittest.TestCase):
    def test_new_changed_same_and_local_only(self):
        local = entry("a", title="Same title here") + entry("b", title="Old title of b", year="2019", doi="10.1/b") + entry("c", title="Only here", doi="10.1/c")
        remote = entry("a", title="Same title here") + entry("b", title="Old title of b", year="2021", doi="10.1/b") + entry("d", title="Brand new one", doi="10.1/d")
        out = zotero.compare(remote, local)
        self.assertEqual(out["same"], 1)
        self.assertEqual([e["key"] for e in out["new"]], ["d"])
        self.assertEqual([(c["key"], [d["field"] for d in c["diff"]]) for c in out["changed"]], [("b", ["year"])])
        self.assertEqual(out["local_only"], ["c"])

    def test_braces_and_spacing_are_not_changes(self):
        out = zotero.compare(entry("a", title="Deep  {DNA} models"), entry("a", title="Deep {DNA}  models"))
        self.assertEqual((out["same"], out["changed"]), (1, []))

    def test_same_work_under_another_key_updates_the_local_entry(self):
        out = zotero.compare(entry("smith_title_2020", year="2021"), entry("smith20"))
        self.assertEqual(out["new"], [])
        self.assertEqual((out["changed"][0]["key"], out["changed"][0]["zotero_key"], out["changed"][0]["by"]),
                         ("smith20", "smith_title_2020", "doi/title"))

    def test_key_collision_with_a_different_work_gets_a_new_key(self):
        out = zotero.compare(entry("k", title="Quite another paper", doi="10.9/z") + entry("k2", doi="10.9/y"),
                             entry("k", title="The first paper ever", doi="10.1/a"), taken=["k2"])
        self.assertEqual([(e["key"], e["zotero_key"], e["collision"]) for e in out["new"]], [("ka", "k", True), ("k2a", "k2", True)])
        self.assertEqual((out["changed"], out["local_only"]), ([], ["k"]))

    def test_unusable_remote_entries_are_counted_not_fatal(self):
        out = zotero.compare("@article{ok, title={fine}}\n@article{bad key, title={x}}\n@article{ok, title={twice}}\n", "")
        self.assertEqual((len(out["new"]), out["skipped"]), (1, 1))


class Apply(unittest.TestCase):
    LOCAL = "% mine\n@article{a,\n  author = {Smith, A.},\n  title  = {Old},\n  note = {mine only}\n}\n\n@book{z, title={Z}}\n"

    def run_apply(self, text, ops):
        splice = zotero.apply(text, ops)
        u16 = text.encode("utf-16-le")
        return (u16[:splice["from"] * 2] + splice["insert"].encode("utf-16-le") + u16[splice["to"] * 2:]).decode("utf-16-le"), splice

    def test_update_keeps_other_fields_and_bytes_add_appends(self):
        got, splice = self.run_apply(self.LOCAL, [
            {"op": "update", "key": "a", "fields": {"title": "New", "year": "2020"}},
            {"op": "add", "type": "misc", "key": "n", "fields": {"title": "Fresh"}}])
        self.assertTrue(got.startswith("% mine\n@article{a,\n  author = {Smith, A.},\n  title  = {New},\n  note = {mine only},\n  year"))
        self.assertIn("\n\n@book{z, title={Z}}\n\n@misc{n,\n  title = {Fresh},\n}\n", got)
        self.assertEqual(self.LOCAL[:splice["from"]], got[:splice["from"]])  # nothing before the first change moved

    def test_no_ops_is_an_empty_splice_and_unicode_offsets_are_utf16(self):
        self.assertEqual(zotero.apply(self.LOCAL, []), {"from": len(self.LOCAL), "to": len(self.LOCAL), "insert": ""})
        text = "@misc{e, title={\U0001F600}}\n"
        got, _ = self.run_apply(text, [{"op": "add", "type": "misc", "key": "f", "fields": {"title": "x"}}])
        self.assertTrue(got.startswith(text))

    def test_refuses_what_would_break_the_file(self):
        for ops, status in (([{"op": "add", "type": "misc", "key": "a", "fields": {"title": "x"}}], 409),
                            ([{"op": "update", "key": "gone", "fields": {"title": "x"}}], 409),
                            ([{"op": "add", "type": "misc", "key": "bad key", "fields": {"title": "x"}}], 400),
                            ([{"op": "add", "type": "misc", "key": "q", "fields": {"title": "}{"}}], 400),
                            ([{"op": "drop", "key": "a", "fields": {}}], 400), ("nope", 400)):
            with self.assertRaises(zotero.ZoteroError) as ctx:
                zotero.apply(self.LOCAL, ops)
            self.assertEqual(ctx.exception.status, status, ops)


class Ris(unittest.TestCase):
    def test_reads_records_and_makes_keys_unique(self):
        text = ("TY  - JOUR\nAU  - Smith, John\nAU  - Doe, Jane\nTI  - A study of {things}\nJO  - Journal of Stuff\nPY  - 2020/05/01\n"
                "SP  - 10\nEP  - 20\nDO  - 10.1000/xyz\nER  - \n\nTY  - JOUR\nAU  - Smith, John\nTI  - A study again\nPY  - 2020\nER  - \n"
                "TY  - CHAP\nTI  - Chapter\nT2  - Big Book\nER  -\n")
        first, second, third = bibfix.from_ris(text)
        self.assertEqual(first["type"], "article")
        self.assertEqual(first["fields"], {"title": "A study of things", "author": "Smith, John and Doe, Jane",
                                           "journal": "Journal of Stuff", "year": "2020", "pages": "10--20", "doi": "10.1000/xyz"})
        self.assertEqual((first["key"], second["key"]), ("Smith2020Study", "Smith2020Studya"))
        self.assertEqual((third["type"], third["fields"]["booktitle"]), ("incollection", "Big Book"))
        self.assertEqual(bibfix.from_ris("nothing here"), [])


if __name__ == "__main__":
    unittest.main()
