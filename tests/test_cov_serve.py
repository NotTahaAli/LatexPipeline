"""serve.py branches the endpoint tests leave out: linked-file errors, image lookup, restore failures, body parsing."""

from __future__ import annotations

import http.client
import unittest
from unittest import mock

import _support  # noqa: F401 - puts scripts/ on sys.path
import history
import serve
import team
import test_serve as ts


class Docs(ts.ServerCase):
    """One served document "demo", a second one "second", history in the temp dir."""

    def setUp(self):
        super().setUp()
        mock.patch.dict(serve.HISTORY, {"dir": self.base / ".hist"}).start()
        other = self.base / "second"
        other.mkdir()
        (other / "main.tex").write_text("second", encoding="utf-8")
        (other / "refs.bib").write_text("@misc{a}\n", encoding="utf-8")
        (other / "nul.bib").write_bytes(b"a\0b")
        (other / "latin.bib").write_bytes(b"caf\xe9")
        mock.patch.dict(serve.DOCS, {"second": other / "main.tex"}).start()
        mock.patch.dict(serve.STATE, {"demo": serve.fresh_state("demo", self.root / "main.tex")}, clear=True).start()

    def status(self, fn, *args):
        try:
            fn(*args)
        except serve.ApiError as exc:
            return exc.status
        return None


class LinkedErrors(Docs):
    def link(self, data, role="owner"):
        return self.status(serve.linked_change, "demo", role, data, "Owner")

    def test_refusals(self):
        add = {"op": "add", "doc": "second", "from": "refs.bib"}
        self.assertEqual(self.link({"op": "remove", "path": "none.bib"}), 404)
        self.assertEqual(self.link({"op": "refresh", "path": "none.bib"}, role="edit"), 404)
        self.assertEqual(self.link({"op": "bogus", "path": "x.bib"}), 400)
        self.assertEqual(self.link({**add, "path": "x.exe"}), 415)
        self.assertEqual(self.link({**add, "path": "main.tex"}), 409)  # already exists
        with mock.patch.object(team, "MAX_LINKS", 0):
            self.assertEqual(self.link({**add, "path": "a.bib"}), 409)
        self.assertEqual(self.link({**add, "path": "a.bib", "from": "gone.bib"}), 404)
        self.assertEqual(self.link({**add, "path": "b.bib", "from": "nul.bib"}), 415)
        self.assertEqual(self.link({**add, "path": "c.bib", "from": "latin.bib"}), 415)
        self.assertEqual(self.link({**add, "path": "main.tex/d.bib"}), 409)  # a file is in the way of the folder
        for name in ("a.bib", "b.bib", "c.bib"):
            self.assertFalse((self.root / name).exists(), name)

    def test_fetch_and_store_errors_keep_their_status(self):
        with mock.patch.object(team, "http_get", side_effect=team.TeamError("too large", 413)):
            self.assertEqual(self.link({"op": "add", "path": "r.bib", "url": "https://e.org/r.bib"}), 413)
        with mock.patch.object(team, "read_links", side_effect=team.TeamError("unreadable", 500)):
            self.assertEqual(self.link({"op": "remove", "path": "r.bib"}), 500)
        self.assertFalse((self.root / "r.bib").exists())


class FindImage(Docs):
    def test_graphicspath_extensions_and_origin_folder(self):
        self.write("main.tex", "\\graphicspath{{figs/}{../outside/}}\n")
        self.write("figs/plot.png", "png")
        self.write("chap/local.jpg", "jpg")
        self.write("notimage.txt", "x")
        (self.base / "outside").mkdir()
        (self.base / "outside" / "secret.png").write_text("secret")
        self.assertEqual(serve.find_image(self.root, "plot"), (self.root / "figs/plot.png").resolve())
        self.assertEqual(serve.find_image(self.root, "local", "chap/one.tex"), (self.root / "chap/local.jpg").resolve())
        self.assertIsNone(serve.find_image(self.root, "secret"))  # a \graphicspath outside the document is skipped
        self.assertIsNone(serve.find_image(self.root, "notimage.txt"))
        self.assertIsNone(serve.find_image(self.root, "missing"))

    def test_missing_main_tex(self):
        (self.root / "main.tex").unlink()
        self.write("a.svg", "<svg/>")
        self.assertEqual(serve.find_image(self.root, "a"), (self.root / "a.svg").resolve())


class RestoreErrors(Docs):
    def version(self):
        serve.remember("demo", "main.tex", "old text\n", ["A"])
        serve.remember("demo", "build.toml", "engine = 'pdflatex'\n", ["A"])
        return serve.history_list("demo", "owner", None, None)["versions"][0]["id"]

    def test_pruned_and_missing_texts(self):
        vid = self.version()
        restore = serve.history_restore
        self.assertEqual(self.status(restore, "demo", "edit", vid, "build.toml", "E"), 403)
        self.assertEqual(self.status(restore, "demo", "owner", vid, "never.tex", "O"), 409)
        with mock.patch.object(history.Store, "text_at", side_effect=KeyError):
            self.assertEqual(self.status(restore, "demo", "owner", vid, "main.tex", "O"), 410)
        with mock.patch.object(history.Store, "state_at", side_effect=KeyError):
            self.assertEqual(self.status(restore, "demo", "owner", vid, None, "O"), 410)
        with mock.patch.object(history.Store, "state_at", side_effect=KeyError):
            self.assertEqual(self.status(serve.version_texts, "demo", "owner", vid), 410)

    def test_write_failures_are_listed_not_raised(self):
        vid = self.version()
        self.write("main.tex", "new text\n")
        with mock.patch.object(serve, "write_restored", side_effect=OSError("disk full")):
            out = serve.history_restore("demo", "edit", vid, None, "E")
        self.assertEqual(out["failed"], [{"path": "main.tex", "error": "disk full"}])
        self.assertEqual(out["skipped"], ["build.toml"])  # build config: owner only
        self.assertEqual((self.root / "main.tex").read_text(), "new text\n")

    def test_room_files_that_fail_to_pend_are_listed(self):
        vid = self.version()
        with mock.patch.object(serve, "room_editable", return_value=True), \
                mock.patch.object(history.Store, "pend", side_effect=history.Full("full")):
            self.assertEqual(serve.history_restore("demo", "owner", vid, "main.tex", "O")["failed"],
                             [{"path": "main.tex", "error": "the history is full"}])
        with mock.patch.object(serve, "room_editable", return_value=True), \
                mock.patch.object(history.Store, "pend", side_effect=OSError("io")):
            self.assertEqual(serve.history_restore("demo", "owner", vid, "main.tex", "O")["failed"],
                             [{"path": "main.tex", "error": "io"}])


class Bodies(Docs):
    def raw(self, body, headers):
        conn = http.client.HTTPConnection("127.0.0.1", self.port, timeout=10)
        conn.putrequest("POST", "/api/bib?doc=demo")
        for key, value in {"Host": f"127.0.0.1:{self.port}", **headers}.items():
            conn.putheader(key, value)
        conn.endheaders(body)
        res = conn.getresponse()
        res.read()
        conn.close()
        return res.status

    def test_bad_bodies(self):
        json_type = {"Content-Type": "application/json"}
        self.assertEqual(self.raw(b"{}", {**json_type, "Content-Length": "x"}), 413)
        self.assertEqual(self.raw(b"", {**json_type, "Content-Length": str(serve.MAX_BODY + 1)}), 413)
        self.assertEqual(self.raw(b"{}", {"Content-Type": "text/plain", "Content-Length": "2"}), 415)
        self.assertEqual(self.raw(b"{nope", {**json_type, "Content-Length": "5"}), 400)
        self.assertEqual(self.raw(b"[1]", {**json_type, "Content-Length": "3"}), 400)
        self.assertEqual(self.raw(b"{}", {**json_type, "Content-Length": "2"}), 200)


class Awareness(unittest.TestCase):
    """y-aware: only a member of the room may publish its cursor, and at most 64 KiB of it."""

    def setUp(self):
        mock.patch.dict(serve.ROOMS, {"d\nmain.tex": {"members": {"me": {"role": "edit"}}, "aware": {}}},
                        clear=True).start()
        self.addCleanup(mock.patch.stopall)
        self.published = []
        mock.patch.object(serve.BUS, "publish", side_effect=lambda *a, **k: self.published.append(a)).start()

    def test_members_only_and_capped(self):
        serve.on_aware({"data": {"room": "d\nmain.tex", "u": "AA"}}, "me", "edit")
        serve.on_aware({"data": {"room": "d\nmain.tex", "u": "BB"}}, "stranger", "edit")
        serve.on_aware({"data": {"room": "d\nother.tex", "u": "CC"}}, "me", "edit")
        serve.on_aware({"data": {"room": "d\nmain.tex", "u": "x" * (64 * 1024 + 1)}}, "me", "edit")
        serve.on_aware({"data": {"room": "d\nmain.tex", "u": 5}}, "me", "edit")
        serve.on_aware({}, "me", "edit")
        self.assertEqual(serve.ROOMS["d\nmain.tex"]["aware"], {"me": "AA"})
        self.assertEqual([a[1]["u"] for a in self.published], ["AA"])


class HttpEdges(Docs):
    def test_image_lint_and_bad_upgrade(self):
        self.write("figs/p.png", "png")
        self.write("main.tex", "\\graphicspath{{figs/}}\n")
        status, body = self.request("GET", "/api/image?doc=demo&name=p")
        self.assertEqual((status, body), (200, b"png"))
        self.assertEqual(self.request("GET", "/api/image?doc=demo&name=../secret")[0], 404)
        status, body = self.request("GET", "/api/lint?doc=demo")
        self.assertEqual(status, 200)
        self.assertIn("findings", body)
        self.assertEqual(self.request("GET", "/ws?cid=abc")[0], 400)  # no Upgrade header

    def test_share_links_need_sharing_on(self):
        with self.assertRaises(serve.ApiError) as ctx:
            serve.share_link_create({"name": "Ann", "role": "edit"})
        self.assertEqual(ctx.exception.status, 409)


if __name__ == "__main__":
    unittest.main()
