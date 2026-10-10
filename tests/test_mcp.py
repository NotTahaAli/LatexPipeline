"""MCP: the protocol (mcp_tools.handle), the tools on a document, and the local stdio server. No network."""

from __future__ import annotations

import base64
import io
import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import _support
import mcp_server
import mcp_tools
import serve

MAIN = r"""\documentclass{article}
\begin{document}
\section{Intro}\label{sec:intro}
Hello world, see \cite{knuth} and \cite{ghost}, and \ref{sec:none}.
\input{chapters/one}
\end{document}
"""
ONE = "\\section{One}\nAlpha beta gamma. Alpha again.\n"
BIB = "@book{knuth, title={The TeXbook}, author={Knuth}, year={1984}}\n@book{unused, title={Other}}\n"


class Fake:
    """A backend for handle(): every tool visible unless `scopes` says otherwise; call records its arguments."""

    def __init__(self, scopes=("write", "review"), challenge=None):
        self.scopes, self.calls, self._challenge = scopes, [], challenge

    def tools(self):
        return mcp_tools.tools_for(self.scopes)

    def call(self, name, args):
        self.calls.append((name, args))
        if args.get("project") == "boom":
            raise RuntimeError("bug")
        return mcp_tools.result({"ok": True})

    def challenge(self, scope):
        return self._challenge


def rpc(method, params=None, mid=1, modern=False):
    message = {"jsonrpc": "2.0", "id": mid, "method": method}
    if params is not None or modern:
        message["params"] = dict(params or {})
    if modern:
        message["params"]["_meta"] = {mcp_tools.META_VERSION: mcp_tools.MODERN,
                                      "io.modelcontextprotocol/clientInfo": {"name": "t", "version": "1"},
                                      "io.modelcontextprotocol/clientCapabilities": {}}
    return message


class Protocol(unittest.TestCase):
    def test_legacy_initialize_negotiates_a_version(self):
        for asked, got in (("2025-06-18", "2025-06-18"), ("2025-03-26", "2025-03-26"), ("1999-01-01", "2025-11-25")):
            response, status, _ = mcp_tools.handle(rpc("initialize", {"protocolVersion": asked}), Fake())
            self.assertEqual((status, response["result"]["protocolVersion"]), (200, got))
            self.assertIn("tools", response["result"]["capabilities"])
        self.assertEqual(mcp_tools.handle({"jsonrpc": "2.0", "method": "notifications/initialized"}, Fake())[:2],
                         (None, 202))

    def test_modern_requests_carry_their_version(self):
        response, status, _ = mcp_tools.handle(rpc("server/discover", modern=True), Fake())
        self.assertEqual(status, 200)
        result = response["result"]
        self.assertEqual(result["resultType"], "complete")
        self.assertIn(mcp_tools.MODERN, result["supportedVersions"])
        self.assertEqual(result["_meta"][mcp_tools.META_SERVER]["name"], "latex-pipeline")
        listed = mcp_tools.handle(rpc("tools/list", modern=True), Fake())[0]["result"]
        self.assertEqual(listed["cacheScope"], "private")
        self.assertIn("ttlMs", listed)
        bad = rpc("tools/list", modern=True)
        bad["params"]["_meta"][mcp_tools.META_VERSION] = "2030-01-01"
        response, status, _ = mcp_tools.handle(bad, Fake())
        self.assertEqual((status, response["error"]["code"]), (400, -32022))
        self.assertIn(mcp_tools.MODERN, response["error"]["data"]["supported"])
        response, status, _ = mcp_tools.handle(rpc("nope/nothing", modern=True), Fake(), http={
            "version": mcp_tools.MODERN, "method": "nope/nothing"})
        self.assertEqual((status, response["error"]["code"]), (404, -32601))

    def test_http_headers_must_match_the_body(self):
        call = rpc("tools/call", {"name": "list_projects", "arguments": {}}, modern=True)
        good = {"version": mcp_tools.MODERN, "method": "tools/call", "name": "list_projects"}
        self.assertEqual(mcp_tools.handle(call, Fake(), http=good)[1], 200)
        encoded = {**good, "name": "=?base64?" + base64.b64encode(b"list_projects").decode() + "?="}
        self.assertEqual(mcp_tools.handle(call, Fake(), http=encoded)[1], 200)
        for wrong in ({**good, "version": "2025-06-18"}, {**good, "method": "tools/list"}, {**good, "name": "x"},
                      {**good, "name": None}):
            response, status, _ = mcp_tools.handle(call, Fake(), http=wrong)
            self.assertEqual((status, response["error"]["code"]), (400, -32020), wrong)
        legacy = rpc("tools/list")
        self.assertEqual(mcp_tools.handle(legacy, Fake(), http={"version": "2025-06-18"})[1], 200)
        self.assertEqual(mcp_tools.handle(legacy, Fake(), http={"version": None})[1], 200)
        self.assertEqual(mcp_tools.handle(legacy, Fake(), http={"version": "2031-01-01"})[1], 400)
        self.assertEqual(mcp_tools.handle(legacy, Fake(), http={"version": mcp_tools.MODERN})[1], 400)

    def test_bad_messages_and_unknown_tools(self):
        for bad in ([], {"jsonrpc": "1.0", "id": 1, "method": "x"}, {"jsonrpc": "2.0", "id": True, "method": "x"},
                    {"jsonrpc": "2.0", "id": 1, "method": "tools/list", "params": []}):
            self.assertEqual(mcp_tools.handle(bad, Fake())[0]["error"]["code"], -32600, bad)
        response = mcp_tools.handle(rpc("tools/call", {"name": "rm_rf"}), Fake())[0]
        self.assertEqual(response["error"]["code"], -32602)
        self.assertEqual(mcp_tools.handle(rpc("frobnicate"), Fake())[0]["error"]["code"], -32601)

    def test_arguments_are_validated_before_any_tool_runs(self):
        backend = Fake()
        cases = [{"project": 3}, {"project": "p", "path": "a", "extra": 1}, {},
                 {"project": "p", "path": "a", "start_line": 0}, {"project": "p", "path": "a", "start_line": True},
                 {"project": "p", "path": "x" * 300}]
        for args in cases:
            out = mcp_tools.handle(rpc("tools/call", {"name": "read_file", "arguments": args}), backend)[0]
            self.assertTrue(out["result"]["isError"], args)
        self.assertEqual(backend.calls, [])
        out = mcp_tools.handle(rpc("tools/call", {"name": "read_file", "arguments": {
            "project": "p", "path": "a.tex", "end_line": None}}), backend)[0]
        self.assertFalse(out["result"]["isError"])
        self.assertEqual(backend.calls, [("read_file", {"project": "p", "path": "a.tex"})])
        with mock.patch("traceback.print_exc"):
            crash = mcp_tools.handle(rpc("tools/call", {"name": "list_files", "arguments": {"project": "boom"}}),
                                     backend)[0]["result"]
        self.assertTrue(crash["isError"])
        self.assertNotIn("bug", crash["content"][0]["text"])  # internals stay in the server log

    def test_scopes_hide_tools_and_challenge(self):
        backend = Fake(scopes=(), challenge={"WWW-Authenticate": "Bearer error=\"insufficient_scope\""})
        names = [t["name"] for t in mcp_tools.handle(rpc("tools/list"), backend)[0]["result"]["tools"]]
        self.assertIn("read_file", names)
        self.assertNotIn("edit_file", names)
        self.assertNotIn("add_comment", names)
        response, status, headers = mcp_tools.handle(rpc("tools/call", {"name": "edit_file", "arguments": {}}), backend)
        self.assertEqual(status, 403)
        self.assertIn("insufficient_scope", headers["WWW-Authenticate"])

    def test_tool_schemas_are_portable(self):
        for tool in mcp_tools.TOOLS:
            with self.subTest(tool=tool["name"]):
                self.assertRegex(tool["name"], r"^[a-z_]{1,64}$")
                schema = tool["inputSchema"]
                self.assertEqual((schema["type"], schema["additionalProperties"]), ("object", False))
                for prop in schema["properties"].values():
                    self.assertIn(prop["type"], ("string", "integer", "boolean"))
                    self.assertTrue(prop["description"])
                    self.assertFalse({"$ref", "oneOf", "anyOf", "allOf", "items"} & set(prop))
                self.assertNotIn("_scope", mcp_tools.public(tool))
                self.assertLess(len(tool["description"]), 1024)


class DocCase(unittest.TestCase):
    """A document in a throwaway repository, served to the tools like serve.py would."""

    def setUp(self):
        stack = _support.fake_repo()
        self.repo = stack.__enter__()
        self.addCleanup(stack.__exit__, None, None, None)
        self.main = _support.write_doc(self.repo, "thesis", MAIN)
        self.root = self.main.parent
        (self.root / "chapters").mkdir()
        (self.root / "chapters" / "one.tex").write_text(ONE, encoding="utf-8")
        (self.root / "refs.bib").write_text(BIB, encoding="utf-8")
        (self.root / "build.toml").write_text("engine = \"pdflatex\"\n", encoding="utf-8")
        (self.repo / "outside.tex").write_text("secret", encoding="utf-8")
        for table in (serve.DOCS, serve.STATE, serve.ROOMS, serve.CLIENTS):
            mock.patch.dict(table, {}, clear=True).start()
        mock.patch.dict(serve.HISTORY, {"dir": str(self.repo / ".hist")}).start()
        mock.patch.object(serve, "RATE", {}).start()
        self.addCleanup(mock.patch.stopall)
        serve.DOCS["thesis"] = self.main
        serve.STATE["thesis"] = serve.fresh_state("thesis", self.main)
        self.local = mcp_server.Local(serve, read_only=False)
        self.local.client = "Tester"

    def call(self, name, backend=None, **args):
        out = mcp_tools.handle(rpc("tools/call", {"name": name, "arguments": args}), backend or self.local)[0]
        return out["result"]

    def ok(self, name, **args):
        out = self.call(name, **args)
        self.assertFalse(out["isError"], out["content"][0].get("text"))
        return out

    def error(self, name, **args):
        out = self.call(name, **args)
        self.assertTrue(out["isError"], out)
        return out["content"][0]["text"]

    def doc(self, role, user=None):
        return mcp_tools.Doc(serve, "thesis", role, user, "Ed via Tester")


class Tools(DocCase):
    def test_read_tools(self):
        projects = self.ok("list_projects")["structuredContent"]["projects"]
        self.assertEqual([p["project"] for p in projects], ["thesis"])
        files = {f["path"] for f in self.ok("list_files", project="thesis")["structuredContent"]["files"]}
        self.assertTrue({"main.tex", "chapters/one.tex", "refs.bib"} <= files)
        text = self.ok("read_file", project="thesis", path="main.tex", start_line=3, end_line=4)["content"][0]["text"]
        self.assertTrue(text.startswith("main.tex, lines 3-4 of 7"))
        self.assertIn("\\section{Intro}", text)
        self.assertNotIn("documentclass", text)
        hits = self.ok("grep", project="thesis", query="alpha")["structuredContent"]["matches"]
        self.assertEqual([(h["path"], h["line"]) for h in hits], [("chapters/one.tex", 2)])
        refs = self.ok("references", project="thesis")["structuredContent"]
        self.assertEqual(refs["cited_but_missing"], ["ghost"])
        self.assertEqual(refs["never_cited"], ["unused"])
        self.assertEqual(refs["refs_to_unknown_labels"], ["sec:none"])
        outline = self.ok("outline", project="thesis")["structuredContent"]
        self.assertEqual([h["title"] for h in outline["headings"]], ["Intro", "One"])
        self.assertGreater(outline["total_words"], 5)
        status = self.ok("build_status", project="thesis")["structuredContent"]
        self.assertEqual((status["status"], status["pdf"]), ("never built", False))
        self.assertIn("unknown project", self.error("read_file", project="nope", path="main.tex").lower()
                      .replace("no project", "unknown project"))

    def test_search_and_fetch(self):
        found = self.ok("search", query="TeXbook Knuth")["structuredContent"]["results"]
        self.assertEqual(found[0]["id"], "thesis::refs.bib")
        self.assertTrue(found[0]["url"].startswith("file:"))
        fetched = self.ok("fetch", id=found[0]["id"])
        self.assertIn("TeXbook", fetched["structuredContent"]["text"])
        self.assertIn("TeXbook", fetched["content"][0]["text"])  # the JSON mirror ChatGPT reads
        for bad in ("thesis::../outside.tex", "thesis::/etc/passwd", "nope::main.tex", "thesis::.hidden"):
            self.assertTrue(self.call("fetch", id=bad)["isError"], bad)

    def test_paths_stay_inside_the_document(self):
        for bad in ("../outside.tex", "/etc/passwd", "a/../../outside.tex", "C:/x.tex", "chapters\\one.tex"):
            self.error("read_file", project="thesis", path=bad)
            self.error("edit_file", project="thesis", path=bad, old_text="a", new_text="b")
            self.error("write_file", project="thesis", path=bad, content="x")
        if os.name != "nt":
            os.symlink(self.repo / "outside.tex", self.root / "link.tex")
            self.error("read_file", project="thesis", path="link.tex")
            self.assertNotIn("link.tex", json.dumps(self.ok("search", query="secret")["structuredContent"]))
        self.assertEqual((self.repo / "outside.tex").read_text(), "secret")

    def test_edit_is_exact_and_recorded_in_history(self):
        self.assertIn("appears 2 times", self.error("edit_file", project="thesis", path="chapters/one.tex",
                                                    old_text="Alpha", new_text="Omega"))
        self.assertIn("not found", self.error("edit_file", project="thesis", path="chapters/one.tex",
                                              old_text="Alpha  beta", new_text="x"))
        done = self.ok("edit_file", project="thesis", path="chapters/one.tex", old_text="Alpha beta",
                       new_text="Omega beta")["structuredContent"]
        self.assertEqual((done["replacements"], done["first_line"]), (1, 2))
        self.assertEqual((self.root / "chapters/one.tex").read_text(),
                         "\\section{One}\nOmega beta gamma. Alpha again.\n")
        self.ok("edit_file", project="thesis", path="chapters/one.tex", old_text="a", new_text="A", replace_all=True)
        versions = serve.history_store("thesis").versions("chapters/one.tex")
        self.assertEqual(versions[0]["authors"], ["Tester (MCP)"])
        texts = [serve.history_store("thesis").text_at(v["id"], "chapters/one.tex") for v in versions]
        self.assertIn(ONE, texts)  # the text before the first change was kept too

    def test_write_rename_delete(self):
        self.assertIn("exists", self.error("write_file", project="thesis", path="main.tex", content="x"))
        made = self.ok("write_file", project="thesis", path="chapters/two.tex", content="Two\r\n")["structuredContent"]
        self.assertTrue(made["created"])
        self.assertEqual((self.root / "chapters/two.tex").read_bytes(), b"Two\n")
        self.error("write_file", project="thesis", path=".hidden.tex", content="x")
        self.error("write_file", project="thesis", path="pic.png", content="x")
        self.ok("rename_file", project="thesis", path="chapters/two.tex", to="chapters/three.tex")
        self.assertTrue((self.root / "chapters/three.tex").is_file())
        self.ok("delete_file", project="thesis", path="chapters/three.tex")
        self.assertFalse((self.root / "chapters/three.tex").exists())
        self.error("delete_file", project="thesis", path="main.tex")
        kinds = [v["kind"] for v in serve.history_store("thesis").versions("chapters/three.tex")]
        self.assertIn("delete", kinds)

    def test_build_config_files_are_the_local_owners_only(self):
        self.ok("edit_file", project="thesis", path="build.toml", old_text="pdflatex", new_text="xelatex")
        editor = self.doc("edit", "7;Ed")
        for tool, args in (("edit_file", {"path": "build.toml", "old_text": "x", "new_text": "y"}),
                           ("write_file", {"path": "latexmkrc", "content": "$x"}),
                           ("rename_file", {"path": "chapters/one.tex", "to": ".latexmkrc"}),
                           ("delete_file", {"path": "build.toml"}),
                           ("add_comment", {"path": "build.toml", "quote": "xelatex", "comment": "hm"})):
            with self.subTest(tool=tool), self.assertRaises(mcp_tools.ToolError) as ctx:
                editor.run(tool, args)
            self.assertIn("configures the build", str(ctx.exception))
        self.assertIn("xelatex", (self.root / "build.toml").read_text())

    def test_viewers_cannot_change_anything(self):
        viewer = self.doc("view", "8;Vi")
        for tool, args in (("edit_file", {"path": "main.tex", "old_text": "Hello", "new_text": "Bye"}),
                           ("write_file", {"path": "x.tex", "content": "x"}), ("delete_file", {"path": "refs.bib"}),
                           ("build", {}), ("add_suggestion", {"path": "main.tex", "quote": "Hello",
                                                              "replacement": "Hi"})):
            with self.subTest(tool=tool), self.assertRaises(mcp_tools.ToolError) as ctx:
                viewer.run(tool, args)
            self.assertIn("view-only", str(ctx.exception))
        self.assertIn("Hello", (self.root / "main.tex").read_text())
        self.assertFalse(viewer.run("read_file", {"path": "main.tex"})["isError"])

    def test_files_open_in_a_room_are_not_written(self):
        serve.ROOMS[serve.room_id("thesis", "chapters/one.tex")] = {
            "doc": "thesis", "path": "chapters/one.tex", "members": {"c1": {"role": "edit"}}}
        for tool, args in (("edit_file", {"path": "chapters/one.tex", "old_text": "gamma", "new_text": "delta"}),
                           ("write_file", {"path": "chapters/one.tex", "content": "x", "overwrite": True}),
                           ("delete_file", {"path": "chapters"}), ("rename_file", {"path": "chapters/one.tex",
                                                                                     "to": "two.tex"})):
            self.assertIn("open in the editor", self.error(tool, project="thesis", **args))
        self.assertEqual((self.root / "chapters/one.tex").read_text(), ONE)
        self.ok("add_suggestion", project="thesis", path="chapters/one.tex", quote="gamma", replacement="delta")

    def test_comments_and_suggestions(self):
        (self.root / "u.tex").write_text("😀 x quote here\nquote here\n", encoding="utf-8")
        self.assertIn("appears 2 times", self.error("add_comment", project="thesis", path="u.tex",
                                                    quote="quote here", comment="c"))
        made = self.ok("add_comment", project="thesis", path="u.tex", quote="quote here", comment="Fine?",
                       occurrence=1)["structuredContent"]
        self.assertEqual(made["line"], 1)
        self.ok("add_suggestion", project="thesis", path="u.tex", quote="x", replacement="y")
        data = serve.review_store("thesis").load()
        anchor = data["threads"][0]["anchor"]
        self.assertEqual((anchor["from"], anchor["to"]), (5, 15))  # UTF-16: the emoji counts twice
        self.assertEqual(data["threads"][0]["comments"][0]["name"], "Tester (MCP)")
        self.assertEqual(data["suggestions"][0]["insert"], "y")
        listed = self.ok("list_reviews", project="thesis")["structuredContent"]
        self.assertEqual((len(listed["threads"]), len(listed["suggestions"])), (1, 1))
        self.assertNotIn("accept", mcp_tools.TOOL_BY_NAME)

    def test_outputs_are_capped(self):
        (self.root / "big.tex").write_text("\n".join(f"line {i} {'x' * 50}" for i in range(5000)), "utf-8")
        with mock.patch.object(mcp_tools, "MAX_TEXT_OUT", 2000):
            text = self.ok("read_file", project="thesis", path="big.tex")["content"][0]["text"]
            self.assertLess(len(text), 2200)
            self.assertIn("more from line", text)
            listed = self.ok("grep", project="thesis", query="line")["content"][0]["text"]
            self.assertLessEqual(len(listed), 2100)
        self.assertTrue(self.ok("grep", project="thesis", query="line")["structuredContent"]["truncated"])

    def test_read_only_server_offers_no_changes(self):
        local = mcp_server.Local(serve, read_only=True)
        names = [t["name"] for t in local.tools()]
        self.assertIn("render_page", names)
        self.assertFalse({"edit_file", "write_file", "build", "add_comment"} & set(names))
        out = mcp_tools.handle(rpc("tools/call", {"name": "write_file", "arguments": {
            "project": "thesis", "path": "x.tex", "content": "x"}}), local)[0]
        self.assertEqual(out["error"]["code"], -32602)
        self.assertFalse((self.root / "x.tex").exists())

    def test_worker_call_runs_one_tool_as_the_gateway_user(self):
        out = mcp_tools.worker_call(serve, "thesis", "edit", "7;Ed via Tester", {"tool": "edit_file", "arguments": {
            "path": "main.tex", "old_text": "Hello", "new_text": "Hi"}})
        self.assertFalse(out["isError"], out)
        self.assertEqual(serve.history_store("thesis").versions("main.tex")[0]["authors"], ["Ed via Tester"])
        for bad in ({"tool": "list_projects"}, {"tool": "nope"}, {"tool": "read_file", "arguments": {"path": 1}}):
            self.assertTrue(mcp_tools.worker_call(serve, "thesis", "view", "7;Ed", bad)["isError"], bad)
        self.assertTrue(mcp_tools.worker_call(serve, "thesis", "view", "7;Ed", {"tool": "write_file", "arguments": {
            "path": "n.tex", "content": "x"}})["isError"])


@unittest.skipUnless(shutil.which("latexmk") and shutil.which("pdflatex") and shutil.which("pdftoppm"),
                     "needs latexmk, pdflatex and pdftoppm")
class BuildAndPages(DocCase):
    def setUp(self):
        super().setUp()
        (self.root / "main.tex").write_text(MAIN.replace(" see \\cite{knuth} and \\cite{ghost}, and \\ref{sec:none}",
                                                         ""), encoding="utf-8")
        mock.patch.dict(serve.SETTINGS, {"latexmk": shutil.which("latexmk")}).start()
        (self.root / "build.toml").unlink()

    def test_build_errors_and_page_images(self):
        self.ok("edit_file", project="thesis", path="chapters/one.tex", old_text="gamma", new_text="\\undefinedthing")
        report = self.ok("build", project="thesis")["structuredContent"]
        self.assertFalse(report["ok"])
        self.assertEqual(report["errors"][0]["file"], "chapters/one.tex")
        self.assertIn("Undefined control sequence", report["errors"][0]["message"])
        self.assertTrue(report["errors"][0]["hint"])
        self.ok("edit_file", project="thesis", path="chapters/one.tex", old_text="\\undefinedthing", new_text="gamma")
        report = self.ok("build", project="thesis")["structuredContent"]
        self.assertTrue(report["ok"], report)
        self.assertEqual(report["pages"], 1)
        page = self.ok("render_page", project="thesis", size=400)
        image = page["content"][1]
        self.assertEqual(image["mimeType"], "image/png")
        self.assertTrue(base64.b64decode(image["data"]).startswith(b"\x89PNG"))
        by_line = self.ok("render_page", project="thesis", path="chapters/one.tex", line=2, size=400)
        self.assertIn("Page 1", by_line["content"][0]["text"])
        self.assertIn("1 pages", self.error("render_page", project="thesis", page=3))
        chapter = self.ok("preview_chapter", project="thesis", path="chapters/one.tex")["structuredContent"]
        self.assertEqual(chapter["status"], "ok", chapter)
        self.ok("render_page", project="thesis", chapter=True, size=400)


class Stdio(unittest.TestCase):
    def test_line_framing_and_client_name(self):
        backend = mcp_server.Local(serve, read_only=True)
        lines = [json.dumps(rpc("initialize", {"protocolVersion": "2025-06-18", "clientInfo": {"name": "Claude\x07 "
                                                                                                         "Desktop"}})),
                 "", "not json", json.dumps({"jsonrpc": "2.0", "method": "notifications/initialized"}),
                 json.dumps(rpc("tools/list", mid=2))]
        out = io.StringIO()
        mcp_server.serve_stdio(backend, [(line + "\n").encode() for line in lines], out)
        replies = [json.loads(line) for line in out.getvalue().splitlines()]
        self.assertEqual([r.get("id") for r in replies], [1, None, 2])
        self.assertEqual(replies[1]["error"]["code"], -32700)
        self.assertEqual(backend.author, "Claude Desktop (MCP)")

    def test_real_process_over_pipes(self):
        with tempfile.TemporaryDirectory() as tmp:
            doc = Path(tmp) / "paper"
            doc.mkdir()
            (doc / "main.tex").write_text(MAIN, encoding="utf-8")
            script = Path(mcp_server.__file__)
            messages = [rpc("initialize", {"protocolVersion": "2025-11-25", "clientInfo": {"name": "t"}}),
                        {"jsonrpc": "2.0", "method": "notifications/initialized"},
                        rpc("tools/call", {"name": "read_file", "arguments": {"project": "paper", "path": "main.tex",
                                                                              "end_line": 1}}, mid=2),
                        rpc("server/discover", mid=3, modern=True)]
            done = subprocess.run([sys.executable, str(script), "--source", tmp, "--read-only"],
                                  input="".join(json.dumps(m) + "\n" for m in messages), capture_output=True,
                                  text=True, encoding="utf-8", timeout=60)
            replies = [json.loads(line) for line in done.stdout.splitlines()]
            self.assertEqual([r["id"] for r in replies], [1, 2, 3], done.stderr)
            self.assertIn("\\documentclass{article}", replies[1]["result"]["content"][0]["text"])
            self.assertEqual(replies[2]["result"]["resultType"], "complete")
            self.assertIn("ready", done.stderr)


if __name__ == "__main__":
    unittest.main()
