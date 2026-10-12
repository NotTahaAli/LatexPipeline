"""mcp_tools.py: the refusals each tool gives on bad input, protocol corner cases, and the rate limit per tool."""

from __future__ import annotations

import unittest
from unittest import mock

import _support  # noqa: F401 - puts scripts/ on sys.path
import mcp_tools
import serve
import test_mcp as tm


class Refusals(tm.DocCase):
    def test_read_and_search_arguments(self):
        self.assertIn("only 7 lines", self.error("read_file", project="thesis", path="main.tex", start_line=50))
        self.assertIn("before start_line",
                      self.error("read_file", project="thesis", path="main.tex", start_line=3, end_line=2))
        self.assertIn("Bad regular expression", self.error("grep", project="thesis", query="(", regex=True))
        hits = self.ok("grep", project="thesis", query="Alpha", path="chapters")["structuredContent"]["matches"]
        self.assertEqual({h["path"] for h in hits}, {"chapters/one.tex"})
        self.assertEqual(self.ok("grep", project="thesis", query="Alpha", path="nope")["structuredContent"]["matches"],
                         [])
        with mock.patch.object(mcp_tools, "SEARCH_BYTES", 0):  # stops after the first file it reads
            hits = self.ok("grep", project="thesis", query="e")["structuredContent"]["matches"]
        self.assertEqual(len({h["path"] for h in hits}), 1)
        self.assertTrue(self.call("search", query="  ")["isError"])

    def test_edit_and_write_arguments(self):
        self.assertIn("old_text is empty",
                      self.error("edit_file", project="thesis", path="main.tex", old_text="", new_text="x"))
        self.assertIn("nothing to change", self.error("edit_file", project="thesis", path="main.tex",
                                                      old_text="Hello", new_text="Hello"))
        done = self.ok("write_file", project="thesis", path="chapters/one.tex", content="new\n", overwrite=True)
        self.assertFalse(done["structuredContent"]["created"])
        self.assertEqual((self.root / "chapters/one.tex").read_text(), "new\n")
        entry = "@book{unused, title={Other}}"
        with self.assertRaises(mcp_tools.ToolError):  # the same entry again: Doc.change refuses a no-op
            self.doc("edit").t_bib_edit_entry("refs.bib", "unused", entry)

    def test_comment_and_suggestion_arguments(self):
        self.assertIn("quote is empty", self.error("add_comment", project="thesis", path="main.tex", quote="",
                                                   comment="c"))
        self.assertIn("only 1 time", self.error("add_comment", project="thesis", path="main.tex",
                                                quote="Hello world", comment="c", occurrence=3))
        self.assertIn("No such comment", self.error("delete_comment", project="thesis", thread="t", comment="c"))
        self.assertIn("No such open suggestion", self.error("withdraw_suggestion", project="thesis", id="x"))

    def test_bib_entry_errors(self):
        self.assertIn("already in this file", self.error(
            "bib_edit_entry", project="thesis", path="refs.bib", key="knuth", bibtex="@book{unused, title={X}}"))
        self.assertIn("No entry", self.error("bib_edit_entry", project="thesis", path="refs.bib", key="ghost",
                                             bibtex="@book{ghost, title={X}}"))
        self.assertTrue(self.call("bib_edit_entry", None, project="thesis", path="refs.bib", key="knuth",
                                  bibtex="not bibtex")["isError"])

    def test_restore_with_files_open_elsewhere(self):
        serve.remember("thesis", "main.tex", "old\n", ["A"])
        vid = serve.history_list("thesis", "owner", None, None)["versions"][0]["id"]
        with mock.patch.object(serve, "rooms_elsewhere", return_value={"main.tex"}):
            self.assertIn("open in the editor", self.error("restore_version", project="thesis", id=vid))
            self.assertTrue(self.call("restore_version", None, project="thesis", id=vid, path="main.tex")["isError"])
        self.assertNotEqual((self.root / "main.tex").read_text(), "old\n")

    def test_rate_limit_for_shared_roles_only(self):
        with mock.patch.object(serve, "rate_ok", return_value=False):
            self.doc("owner").need_rate("put")
            for limit in ("put", "grammar"):
                with self.assertRaises(mcp_tools.ToolError):
                    self.doc("edit").need_rate(limit)


class Protocol(unittest.TestCase):
    def test_corner_cases(self):
        self.assertEqual(mcp_tools.handle({"jsonrpc": "2.0", "id": 1, "result": {}}, tm.Fake())[:2], (None, 202))
        self.assertIsNone(mcp_tools.decode_header("=?base64?***?="))
        self.assertIsNone(mcp_tools.decode_header("=?base64?" + "//79" + "?="))  # not UTF-8
        self.assertEqual(mcp_tools.check_args({"type": "object", "properties": {}}, None), {})
        with self.assertRaises(mcp_tools.ToolError):
            mcp_tools.check_args({"type": "object", "properties": {}}, [1])

    def test_need_scope_answers_403_with_the_challenge(self):
        class Scoped(tm.Fake):
            def call(self, name, args):
                raise mcp_tools.NeedScope("write")

        response, status, headers = mcp_tools.handle(
            tm.rpc("tools/call", {"name": "list_projects", "arguments": {}}),
            Scoped(challenge={"WWW-Authenticate": "Bearer"}))
        self.assertEqual((status, response["error"]["code"]), (403, -32602))
        self.assertEqual(headers, {"WWW-Authenticate": "Bearer"})


if __name__ == "__main__":
    unittest.main()
