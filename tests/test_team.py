"""serve.py team features (team.py): suggestion replies, locks, copies, folder uploads, linked files, version zips,
latexdiff, people to mention and access requests; every endpoint for each role, the review role included."""

import io
import shutil
import unittest
import urllib.error
import zipfile
from unittest import mock

import _support  # noqa: F401 - puts scripts/ on sys.path
import serve
import team
import test_serve as ts

ANCHOR = {"from": 12, "to": 17, "quote": "hello", "prefix": "\\section{A}\n", "suffix": "\n"}
PNG = b"\x89PNG\r\n\x1a\n" + b"0" * 20


class Team(ts.SharedState, ts.ServerCase):
    """Sharing on with anonymous view/edit links and named review ("bea") and edit ("ed") links."""

    def setUp(self):
        ts.SharedState.setUp(self)
        ts.ServerCase.setUp(self)
        self.write("build.toml", "engine = 'pdflatex'\n")
        mock.patch.dict(serve.HISTORY, {"dir": self.base / ".hist"}).start()
        other = self.base / "second"
        other.mkdir()
        (other / "main.tex").write_text("second", encoding="utf-8")
        (other / "refs.bib").write_text("@misc{a,title={A}}\n", encoding="utf-8")
        mock.patch.dict(serve.DOCS, {"second": other / "main.tex"}).start()
        mock.patch.dict(serve.STATE, {"demo": serve.fresh_state("demo", self.root / "main.tex")}, clear=True).start()
        serve.SHARE["port"] = self.port
        self.tokens = dict(self.share_on())
        for who, name, role in (("bea", "Bea", "review"), ("ed", "Ed", "edit")):
            info = self.as_("owner", "POST", "/api/share/links", {"name": name, "role": role})[1]
            link = next(x for x in info["named"] if x["name"] == name)
            self.tokens[who] = link["url"].split("token=")[1].split("#")[0]

    def as_(self, who, method, path, body=None, raw=None, ctype=None):
        hdrs = {"Cookie": f"{serve.cookie_name()}={self.tokens[who]}"}
        if raw is not None:
            conn = ts.http.client.HTTPConnection("127.0.0.1", self.port, timeout=10)
            conn.request(method, path, raw, {"Host": f"127.0.0.1:{self.port}", "Content-Type": ctype, **hdrs})
            res = conn.getresponse()
            data = res.read()
            conn.close()
            try:
                return res.status, ts.json.loads(data)
            except ValueError:
                return res.status, data
        return self.request(method, path, body, hdrs)

    def review(self, who, body):
        return self.as_(who, "POST", "/api/review?doc=demo", {"key": "k" * 32, "name": "Kim", **body})


class SuggestionReplies(Team):
    def test_replies_on_a_suggestion_follow_the_comment_rules(self):
        made = self.review("bea", {"op": "suggest", "path": "main.tex", "anchor": ANCHOR, "insert": "hi"})[1]["item"]
        status, out = self.review("ed", {"op": "reply", "suggestion": made["id"], "text": "Why @Bea?"})
        self.assertEqual(status, 200)
        self.assertEqual([c["text"] for c in out["item"]["comments"]], ["Why @Bea?"])
        self.assertEqual(self.review("bea", {"op": "reply", "suggestion": made["id"], "text": "Shorter."})[0], 200)
        self.assertEqual(self.review("view", {"op": "reply", "suggestion": made["id"], "text": "x"})[0], 403)
        listed = self.as_("view", "GET", "/api/review?doc=demo&key=" + "v" * 32)[1]["suggestions"][0]
        self.assertEqual([c["name"] for c in listed["comments"]], ["Ed", "Bea"])
        self.assertNotIn("author", listed["comments"][0])
        ed_reply = listed["comments"][0]["id"]
        delete = {"op": "delete", "suggestion": made["id"], "comment": ed_reply}
        self.assertEqual(self.review("bea", delete)[0], 403)  # not hers
        self.assertEqual(self.review("ed", delete)[0], 200)
        self.assertEqual(self.review("ed", {"op": "reply", "suggestion": "nope", "text": "x"})[0], 404)
        self.assertEqual(len(self.as_("view", "GET", "/api/review?doc=demo&key=" + "v" * 32)[1]["suggestions"]), 1)

    def test_people_to_mention(self):
        self.review("bea", {"op": "comment", "path": "main.tex", "anchor": ANCHOR, "text": "x"})
        self.review("edit", {"op": "comment", "path": "main.tex", "anchor": ANCHOR, "text": "x"})
        status, data = self.as_("view", "GET", "/api/people?doc=demo")
        self.assertEqual(status, 200)
        self.assertEqual(data["people"], ["Bea", "Kim"])  # "Kim (unverified name)" without the mark; no link names
        self.assertEqual(self.as_("ed", "GET", "/api/people?doc=demo")[1]["people"], ["Bea", "Ed", "Kim"])
        self.assertEqual(self.as_("view", "GET", "/api/people?doc=second")[0], 403)


class Locks(Team):
    def test_owner_locks_and_nobody_writes_until_unlocked(self):
        self.assertEqual(self.as_("edit", "POST", "/api/lock?doc=demo", {"locked": True})[0], 403)
        self.assertEqual(self.as_("bea", "POST", "/api/lock?doc=demo", {"locked": True})[0], 403)
        status, out = self.as_("owner", "POST", "/api/lock?doc=demo", {"locked": True})
        self.assertEqual((status, bool(out["locked"])), (200, True))
        self.assertTrue(serve.STATE["demo"]["locked"])
        self.assertTrue(serve.fresh_state("demo", self.root / "main.tex")["locked"])  # survives a restart
        for who in ("owner", "edit"):
            self.assertEqual(self.as_(who, "PUT", "/api/file?doc=demo&path=main.tex", {"text": "x"})[0], 423)
            self.assertEqual(self.as_(who, "POST", "/api/fs?doc=demo&op=newfile&path=n.tex")[0], 423)
        self.assertEqual(
            self.as_("edit", "POST", "/api/upload?doc=demo&name=a.png", raw=PNG, ctype="image/png")[0], 423
        )
        self.assertEqual(
            self.review("bea", {"op": "suggest", "path": "main.tex", "anchor": ANCHOR, "insert": "y"})[0], 423
        )
        self.assertEqual(
            self.review("bea", {"op": "comment", "path": "main.tex", "anchor": ANCHOR, "text": "ok"})[0], 200
        )
        self.assertEqual(self.as_("edit", "POST", "/api/replace?doc=demo", {"q": "hello", "replacement": "x"})[0], 423)
        reply = serve.on_update({"data": {"room": "demo\nmain.tex", "u": "AAAA"}}, "c1", "edit")
        self.assertIn("locked", reply["data"]["error"])
        with self.assertRaises(serve.ApiError):
            serve.check_writable("demo", "main.tex")  # what MCP's Doc.save calls
        self.assertEqual(self.as_("owner", "POST", "/api/lock?doc=demo", {"locked": False})[1]["locked"], None)
        self.assertEqual(self.as_("edit", "PUT", "/api/file?doc=demo&path=main.tex", {"text": "x"})[0], 200)

    def test_hosted_workspace_admin_may_lock(self):
        mock.patch.dict(serve.GATEWAY, {"secret": b""}).start()
        serve.gateway_enable(ts.GatewayMode.SECRET, "demo")
        as_ = ts.GatewayMode.as_.__get__(self)
        self.SECRET = ts.GatewayMode.SECRET
        self.assertEqual(as_("edit", "POST", "/api/lock?doc=demo", {"locked": True})[0], 403)
        self.assertEqual(
            as_("edit", "POST", "/api/lock?doc=demo", {"locked": True}, headers={"X-Host-Admin": "1"})[0], 200
        )
        self.assertTrue(as_("view", "GET", "/api/config")[1]["role"] == "view")
        self.assertTrue(as_("edit", "GET", "/api/config", headers={"X-Host-Admin": "1"})[1]["lock"])
        self.assertEqual(as_("edit", "PUT", "/api/file?doc=demo&path=main.tex", {"text": "x"})[0], 423)


class FilesCopyAndFolders(Team):
    def test_copy_files_and_folders(self):
        self.write("ch/one.tex", "one")
        self.assertEqual(self.as_("view", "POST", "/api/fs?doc=demo&op=copy&path=ch&to=ch2")[0], 403)
        self.assertEqual(self.as_("bea", "POST", "/api/fs?doc=demo&op=copy&path=ch&to=ch2")[0], 403)
        self.assertEqual(self.as_("edit", "POST", "/api/fs?doc=demo&op=copy&path=ch&to=ch2")[0], 200)
        self.assertEqual((self.root / "ch2" / "one.tex").read_text(), "one")
        self.assertEqual(self.as_("edit", "POST", "/api/fs?doc=demo&op=copy&path=ch/one.tex&to=two.tex")[0], 200)
        self.assertEqual(self.as_("edit", "POST", "/api/fs?doc=demo&op=copy&path=ch/one.tex&to=two.tex")[0], 409)
        self.assertEqual(self.as_("edit", "POST", "/api/fs?doc=demo&op=copy&path=ch&to=ch/inner")[0], 400)
        self.assertEqual(self.as_("edit", "POST", "/api/fs?doc=demo&op=copy&path=ch/one.tex&to=build.toml")[0], 403)
        self.assertEqual(self.as_("edit", "POST", "/api/fs?doc=demo&op=copy&path=ch/one.tex&to=../out.tex")[0], 400)
        rows = self.as_("owner", "GET", "/api/history?doc=demo&path=two.tex")[1]["versions"]
        self.assertEqual(rows[0]["label"], "Copied from ch/one.tex")

    def test_folder_upload_checks_each_file(self):
        url = "/api/upload?doc=demo&path="
        self.assertEqual(self.as_("edit", "POST", url + "pics/a.png", raw=PNG, ctype="image/png")[0], 200)
        self.assertEqual((self.root / "pics" / "a.png").read_bytes(), PNG)
        self.assertEqual(self.as_("edit", "POST", url + "pics/a.png", raw=PNG, ctype="image/png")[0], 409)
        self.assertEqual(self.as_("edit", "POST", url + "pics/b.png", raw=b"GIF89a", ctype="image/png")[0], 415)
        self.assertEqual(self.as_("edit", "POST", url + "sec/x.tex", raw=b"\\section{X}", ctype="text/plain")[0], 200)
        self.assertEqual(self.as_("edit", "POST", url + "sec/y.tex", raw=b"\xff\xfe", ctype="text/plain")[0], 415)
        self.assertEqual(self.as_("edit", "POST", url + "sec/run.sh", raw=b"echo", ctype="text/plain")[0], 415)
        self.assertEqual(self.as_("edit", "POST", url + "sub/latexmkrc", raw=b"x", ctype="text/plain")[0], 403)
        self.assertEqual(self.as_("edit", "POST", url + ".git/config", raw=b"x", ctype="text/plain")[0], 400)
        self.assertEqual(self.as_("view", "POST", url + "v.tex", raw=b"x", ctype="text/plain")[0], 403)
        self.assertEqual(self.as_("bea", "POST", url + "v.tex", raw=b"x", ctype="text/plain")[0], 403)
        self.assertEqual(self.as_("owner", "POST", url + "build2/build.toml", raw=b"x", ctype="text/plain")[0], 200)


class LinkedFiles(Team):
    def test_url_links_are_owner_made_read_only_and_refreshable(self):
        fetched = []
        mock.patch.object(team, "http_get", side_effect=lambda url, cap: fetched.append(url) or b"@misc{b}\n").start()
        add = {"op": "add", "path": "refs.bib", "url": "https://example.org/refs.bib"}
        self.assertEqual(self.as_("edit", "POST", "/api/linked?doc=demo", add)[0], 403)
        self.assertEqual(self.as_("view", "POST", "/api/linked?doc=demo", add)[0], 403)
        self.assertEqual(
            self.as_("owner", "POST", "/api/linked?doc=demo", {**add, "url": "http://example.org/x"})[0], 400
        )
        self.assertEqual(self.as_("owner", "POST", "/api/linked?doc=demo", add)[0], 200)
        self.assertEqual((self.root / "refs.bib").read_text(), "@misc{b}\n")
        files = self.as_("view", "GET", "/api/files?doc=demo")[1]
        self.assertEqual(files["linked"]["refs.bib"]["url"], "https://example.org/refs.bib")
        self.assertEqual(self.as_("edit", "PUT", "/api/file?doc=demo&path=refs.bib", {"text": "x"})[0], 409)
        self.assertEqual(self.as_("owner", "PUT", "/api/file?doc=demo&path=refs.bib", {"text": "x"})[0], 409)
        self.assertEqual(
            self.as_("view", "POST", "/api/linked?doc=demo", {"op": "refresh", "path": "refs.bib"})[0], 403
        )
        self.assertEqual(self.as_("bea", "POST", "/api/linked?doc=demo", {"op": "refresh", "path": "refs.bib"})[0], 403)
        self.assertEqual(
            self.as_("edit", "POST", "/api/linked?doc=demo", {"op": "refresh", "path": "refs.bib"})[0], 200
        )
        self.assertEqual(len(fetched), 2)
        self.assertEqual(self.as_("edit", "POST", "/api/linked?doc=demo", {"op": "remove", "path": "refs.bib"})[0], 403)
        self.assertEqual(
            self.as_("owner", "POST", "/api/linked?doc=demo", {"op": "remove", "path": "refs.bib"})[0], 200
        )
        self.assertEqual(self.as_("edit", "PUT", "/api/file?doc=demo&path=refs.bib", {"text": "x"})[0], 200)

    def test_links_to_another_document_and_bad_sources(self):
        add = {"op": "add", "path": "lib/refs.bib", "doc": "second", "from": "refs.bib"}
        self.assertEqual(self.as_("owner", "POST", "/api/linked?doc=demo", add)[0], 200)
        self.assertEqual((self.root / "lib" / "refs.bib").read_text(), "@misc{a,title={A}}\n")
        self.assertEqual(
            self.as_("owner", "POST", "/api/linked?doc=demo", {**add, "path": "x.bib", "from": "../secret.tex"})[0], 400
        )
        self.assertEqual(
            self.as_("owner", "POST", "/api/linked?doc=demo", {**add, "path": "y.bib", "doc": "demo"})[0], 400
        )
        mock.patch.object(team, "http_get", return_value=b"GIF89a").start()
        self.assertEqual(
            self.as_("owner", "POST", "/api/linked?doc=demo", {"op": "add", "path": "p.png", "url": "https://e.org/p"})[
                0
            ],
            415,
        )
        self.assertFalse((self.root / "p.png").exists())

    def test_only_https_redirects_are_followed(self):
        handler = team._HttpsRedirects()
        with self.assertRaises(urllib.error.HTTPError):
            handler.redirect_request(mock.Mock(), io.BytesIO(), 302, "Found", {}, "http://example.org/")
        for bad in ("http://e.org/x", "https://user:pw@e.org/x", "file:///etc/passwd", "https://", 5):
            with self.assertRaises(team.TeamError):
                team.check_url(bad)


class VersionsZipAndDiff(Team):
    def test_a_named_version_downloads_as_a_zip(self):
        self.assertEqual(self.as_("edit", "PUT", "/api/file?doc=demo&path=main.tex", {"text": "v1"})[0], 200)
        vid = self.as_("edit", "POST", "/api/history/label?doc=demo", {"label": "Draft 1"})[1]["id"]
        self.as_("edit", "PUT", "/api/file?doc=demo&path=main.tex", {"text": "v2"})
        status, raw = self.as_("view", "GET", f"/api/history/zip?doc=demo&id={vid}")
        self.assertEqual(status, 200)
        names = zipfile.ZipFile(io.BytesIO(raw)).namelist()
        self.assertEqual(zipfile.ZipFile(io.BytesIO(raw)).read("main.tex"), b"v1")
        self.assertIn("fig.png", names)  # binaries as they are now
        self.assertNotIn("build.toml", names)  # build configuration: the owner's only
        self.assertIn(
            "build.toml",
            zipfile.ZipFile(io.BytesIO(self.as_("owner", "GET", f"/api/history/zip?doc=demo&id={vid}")[1])).namelist(),
        )
        self.assertEqual(self.as_("view", "GET", "/api/history/zip?doc=demo&id=999")[0], 404)
        self.assertEqual(self.as_("view", "GET", f"/api/history/zip?doc=second&id={vid}")[0], 403)

    def test_files_history_never_saw_are_in_an_unnamed_version(self):
        self.write("intro.tex", "never edited")
        self.as_("edit", "PUT", "/api/file?doc=demo&path=main.tex", {"text": "v1"})
        vid = self.as_("view", "GET", "/api/history?doc=demo")[1]["versions"][0]["id"]
        archive = zipfile.ZipFile(io.BytesIO(self.as_("view", "GET", f"/api/history/zip?doc=demo&id={vid}")[1]))
        self.assertEqual(archive.read("intro.tex"), b"never edited")
        self.assertEqual(archive.read("main.tex"), b"v1")

    def test_latexdiff_needs_the_edit_role(self):
        vid = self.as_("edit", "POST", "/api/history/label?doc=demo", {"label": "x"})[1]["id"]
        for who in ("view", "bea"):
            self.assertEqual(self.as_(who, "POST", "/api/latexdiff?doc=demo", {"id": vid})[0], 403)
        with mock.patch.object(serve.build, "build_diff_pdf", return_value={"ok": True, "error": None}) as run:
            self.assertEqual(self.as_("edit", "POST", "/api/latexdiff?doc=demo", {"id": vid})[0], 200)
        self.assertNotIn("build.toml", run.call_args[0][2])  # the shared role's old texts: no build configuration
        self.assertEqual(self.as_("edit", "POST", "/api/latexdiff?doc=demo", {"id": 999})[0], 404)
        self.assertEqual(self.as_("view", "GET", "/pdf/demo?diff=1")[0], 404)  # not built: the view role may read it

    @unittest.skipUnless(shutil.which("latexmk") and shutil.which("latexdiff"), "needs latexmk and latexdiff")
    def test_latexdiff_builds_a_pdf(self):
        mock.patch.object(serve.build, "OUT_DIR", self.base / "out").start()
        mock.patch.object(serve.build, "CACHE_DIR", self.base / ".latex-cache").start()
        mock.patch.object(serve.build, "output_path_for", lambda main: self.base / "out" / "demo.pdf").start()
        self.write("main.tex", "\\documentclass{article}\\begin{document}\nOld words here.\n\\end{document}\n")
        vid = self.as_("owner", "POST", "/api/history/label?doc=demo", {"label": "old"})[1]["id"]
        self.write("main.tex", "\\documentclass{article}\\begin{document}\nNew words here.\n\\end{document}\n")
        status, out = self.as_("owner", "POST", "/api/latexdiff?doc=demo", {"id": vid})
        self.assertEqual(status, 200, out)
        self.assertTrue(out["ok"], out)
        status, pdf = self.as_("view", "GET", "/pdf/demo?diff=1")
        self.assertEqual((status, pdf[:5]), (200, b"%PDF-"))
        self.assertIn("Old words", (self.root / "main.tex").read_text().replace("New", "Old"))  # source untouched


class AccessRequests(Team):
    def test_a_view_link_asks_and_the_owner_grants_a_named_link(self):
        self.assertEqual(self.as_("edit", "POST", "/api/access/request?doc=demo", {"role": "edit"})[0], 403)
        self.assertEqual(self.as_("view", "POST", "/api/access/request?doc=demo", {"role": "owner"})[0], 400)
        status, out = self.as_(
            "view", "POST", "/api/access/request?doc=demo", {"role": "review", "name": "Zed", "note": "hi"}
        )
        self.assertEqual(status, 200)
        ticket = out["ticket"]
        self.assertEqual(self.as_("view", "GET", f"/api/access/status?ticket={ticket}")[1]["state"], "pending")
        self.assertEqual(self.as_("view", "GET", "/api/access/status?ticket=nope")[0], 404)
        for who in ("view", "edit", "bea"):
            self.assertEqual(self.as_(who, "GET", "/api/access")[0], 403)
            self.assertEqual(self.as_(who, "POST", "/api/access/decide", {"id": "x", "grant": True})[0], 403)
        pending = self.as_("owner", "GET", "/api/access")[1]["requests"]
        self.assertEqual(
            [(r["name"], r["want"], r["note"]) for r in pending], [("Zed (unverified name)", "review", "hi")]
        )
        self.assertNotIn("ticket", pending[0])
        self.assertEqual(
            self.as_("owner", "POST", "/api/access/decide", {"id": pending[0]["id"], "grant": True})[0], 200
        )
        state = self.as_("view", "GET", f"/api/access/status?ticket={ticket}")[1]
        self.assertEqual(state["state"], "granted")
        token = state["url"].split("token=")[1].split("#")[0]
        self.assertEqual(serve.identity_for_token(token), ("review", serve.identity_for_token(token)[1]))
        self.assertTrue(serve.identity_for_token(token)[1].endswith(";Zed"))
        self.assertEqual(
            self.as_("owner", "POST", "/api/access/decide", {"id": pending[0]["id"], "grant": False})[0], 404
        )

    def test_a_review_link_asks_for_edit_and_is_denied(self):
        ticket = self.as_("bea", "POST", "/api/access/request?doc=demo", {"role": "edit"})[1]["ticket"]
        rid = self.as_("owner", "GET", "/api/access")[1]["requests"][0]["id"]
        self.assertEqual(self.as_("owner", "POST", "/api/access/decide", {"id": rid, "grant": False})[1]["url"], None)
        self.assertEqual(
            self.as_("bea", "GET", f"/api/access/status?ticket={ticket}")[1],
            {"state": "denied", "url": None, "want": "edit"},
        )

    def test_the_access_message_is_for_the_owner_only(self):
        messages = [{"type": "access", "data": {"doc": "demo"}, "rev": 1, "topic": "doc"}]
        self.assertEqual(serve.visible(messages, "owner"), messages)
        for role in ("edit", "review", "view"):
            self.assertEqual(serve.visible(messages, role), [])

    def test_requests_are_capped(self):
        requests = team.Requests()
        for _ in range(team.MAX_REQUESTS):
            requests.add("A", "view", "edit", "")
        with self.assertRaises(team.TeamError):
            requests.add("A", "view", "edit", "")
        with self.assertRaises(team.TeamError):
            requests.add("A", "view", "edit", "x" * (team.MAX_NOTE + 1))


if __name__ == "__main__":
    unittest.main()
