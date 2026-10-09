"""serve.py: path safety, atomic writes, outline and word counts, message bus, WebSocket frames, HTTP API."""

import http.client
import io
import json
import os
import shutil
import subprocess
import tempfile
import threading
import unittest
from http.server import ThreadingHTTPServer
from pathlib import Path
from unittest import mock

import _support  # noqa: F401 - puts scripts/ on sys.path
import serve

UI_DIR = Path(serve.__file__).resolve().parent / "serve_ui"


class TempDoc(unittest.TestCase):
    """A document directory in a temp dir, plus a sibling secret outside it."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        base = Path(self.tmp.name)
        self.root = base / "doc"
        self.root.mkdir()
        (base / "secret.tex").write_text("secret", encoding="utf-8")
        self.base = base

    def write(self, rel: str, text: str) -> Path:
        path = self.root / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        with open(path, "w", encoding="utf-8", newline="") as handle:
            handle.write(text)
        return path


class PathSafety(TempDoc):
    def test_rejects_traversal_and_odd_paths(self):
        for bad in ["../secret.tex", "a/../../secret.tex", "/etc/passwd", "a//b.tex", "./a.tex", "a\\b.tex",
                    "C:/x.tex", "x\0.tex", "", "a/./b.tex"]:
            with self.subTest(path=bad), self.assertRaises(serve.ApiError) as ctx:
                serve.resolve_in_doc(self.root, bad)
            self.assertIn(ctx.exception.status, (400, 403))

    def test_accepts_nested_path(self):
        self.write("a/b.tex", "x")
        self.assertEqual(serve.resolve_in_doc(self.root, "a/b.tex"), (self.root / "a/b.tex").resolve())

    def test_rejects_symlink_escaping_the_document(self):
        try:
            os.symlink(self.base / "secret.tex", self.root / "link.tex")
            os.symlink(self.base, self.root / "dirlink")
        except (OSError, NotImplementedError):
            self.skipTest("symlinks not available")
        with self.assertRaises(serve.ApiError) as ctx:
            serve.read_text_file(self.root, "link.tex")
        self.assertEqual(ctx.exception.status, 403)
        with self.assertRaises(serve.ApiError):
            serve.write_text_file(self.root, "dirlink/secret.tex", "pwned", None)
        self.assertEqual((self.base / "secret.tex").read_text(), "secret")
        self.assertEqual([f["path"] for f in serve.list_files(self.root)], [])  # escaping links are not listed

    def test_symlink_inside_document_is_fine(self):
        self.write("real.tex", "hello")
        try:
            os.symlink(self.root / "real.tex", self.root / "alias.tex")
        except (OSError, NotImplementedError):
            self.skipTest("symlinks not available")
        self.assertEqual(serve.read_text_file(self.root, "alias.tex")["text"], "hello")

    def test_non_text_is_rejected(self):
        self.write("fig.png", "x")
        (self.root / "bin.tex").write_bytes(b"ab\0cd")
        (self.root / "latin.tex").write_bytes("caf\xe9".encode("latin-1"))
        for rel in ("fig.png", "bin.tex", "latin.tex"):
            with self.subTest(rel=rel), self.assertRaises(serve.ApiError) as ctx:
                serve.read_text_file(self.root, rel)
            self.assertEqual(ctx.exception.status, 415)
        with self.assertRaises(serve.ApiError) as ctx:
            serve.write_text_file(self.root, "fig.png", "text", None)
        self.assertEqual(ctx.exception.status, 415)
        with self.assertRaises(serve.ApiError):
            serve.write_text_file(self.root, "new.tex", "nul\0byte", None)

    def test_file_kinds(self):
        self.assertEqual(
            [serve.file_kind(n) for n in ("a.TEX", "a.bib", "a.png", "a.pdf", "Makefile")],
            ["text", "text", "image", "other", "other"],
        )


class AtomicWrite(TempDoc):
    def test_write_replaces_content_and_leaves_no_temp_files(self):
        path = self.write("main.tex", "old")
        result = serve.write_text_file(self.root, "main.tex", "new \u00e9", None)
        self.assertEqual(path.read_text(encoding="utf-8"), "new \u00e9")
        self.assertEqual(sorted(os.listdir(self.root)), ["main.tex"])
        self.assertEqual(result["version"], serve.version_of(path.stat()))

    def test_failed_replace_keeps_original_and_cleans_up(self):
        path = self.write("main.tex", "precious")
        with mock.patch("serve.os.replace", side_effect=OSError("disk full")), self.assertRaises(OSError):
            serve.write_text_file(self.root, "main.tex", "half written", None)
        self.assertEqual(path.read_text(), "precious")
        self.assertEqual(sorted(os.listdir(self.root)), ["main.tex"])

    def test_stale_base_is_a_conflict(self):
        path = self.write("main.tex", "v1")
        loaded = serve.read_text_file(self.root, "main.tex")["version"]
        path.write_text("changed elsewhere, longer", encoding="utf-8")
        with self.assertRaises(serve.ApiError) as ctx:
            serve.write_text_file(self.root, "main.tex", "mine", loaded)
        self.assertEqual(ctx.exception.status, 409)
        self.assertEqual(path.read_text(), "changed elsewhere, longer")
        serve.write_text_file(self.root, "main.tex", "mine", None)  # No base: explicit overwrite.
        self.assertEqual(path.read_text(), "mine")

    def test_crlf_files_keep_their_line_endings(self):
        self.write("main.tex", "a\r\nb\r\n")
        loaded = serve.read_text_file(self.root, "main.tex")
        self.assertEqual((loaded["text"], loaded["eol"]), ("a\nb\n", "\r\n"))
        serve.write_text_file(self.root, "main.tex", "a\nB\n", loaded["version"], loaded["eol"])
        self.assertEqual((self.root / "main.tex").read_bytes(), b"a\r\nB\r\n")

    def test_creates_new_file_but_not_in_missing_directory(self):
        serve.write_text_file(self.root, "new.tex", "hi", None)
        self.assertEqual((self.root / "new.tex").read_text(), "hi")
        with self.assertRaises(serve.ApiError) as ctx:
            serve.write_text_file(self.root, "nodir/new.tex", "hi", None)
        self.assertEqual(ctx.exception.status, 404)


class Outline(TempDoc):
    def setUp(self):
        super().setUp()
        self.main = self.write("main.tex", "\n".join([
            r"\documentclass{article}",
            r"\newcommand{\unit}[2]{\input{#2}}  % definition, not a call",
            r"\begin{document}",
            r"\section{Intro} \label{review}",
            "Hello brave world.",
            r"\unit{ch1}{Chapters/one}",
            r"% \input{Chapters/commented}",
            r"\input Chapters/two",
            r"\subsection*{Closing}",
            r"\input{main}",  # cycle
            r"\end{document}",
        ]))
        self.write("review.tex", "never included")  # \label{review} must not pull this in
        self.write("Chapters/one.tex", "\\chapter{One}\nAlpha beta gamma.\n\\section{First}\none two three four\n")
        self.write("Chapters/two.tex", "\\section{Second}\nfive six\n\\input{sub/deep}\n")
        self.write("Chapters/sub/deep.tex", "\\subsubsection{Deep}\nseven\n")
        self.write("Chapters/commented.tex", "\\section{Hidden}\n")

    def test_follows_includes_generically(self):
        parsed = serve.parse_document(self.main)
        self.assertEqual(parsed["files"], ["main.tex", "Chapters/one.tex", "Chapters/two.tex", "Chapters/sub/deep.tex"])
        titles = [(h["level"], h["title"], h["file"], h["line"]) for h in parsed["headings"]]
        self.assertEqual(titles, [
            (2, "Intro", "main.tex", 4),
            (1, "One", "Chapters/one.tex", 1),
            (2, "First", "Chapters/one.tex", 3),
            (2, "Second", "Chapters/two.tex", 1),
            (4, "Deep", "Chapters/sub/deep.tex", 1),
            (3, "Closing", "main.tex", 9),
        ])

    def test_word_counts_roll_up(self):
        with mock.patch("serve.texcount_sections", return_value=None):  # force the built-in counter
            result = serve.outline(self.main)
        self.assertEqual(result["counter"], "builtin")
        by_title = {i["title"]: i for i in result["items"]}
        self.assertEqual(by_title["One"]["own"], 4)  # heading words count, like texcount
        self.assertEqual(by_title["First"]["own"], 5)
        rest = sum(i["own"] for i in result["items"][1:])
        self.assertEqual(by_title["One"]["words"], rest)  # a chapter owns everything after it
        under = [by_title[n]["own"] for n in ("Second", "Deep", "Closing")]  # Closing is a subsection of Second
        self.assertEqual(by_title["Second"]["words"], sum(under))
        self.assertGreaterEqual(result["total_words"], sum(i["own"] for i in result["items"]))

    def test_texcount_subcounts_are_used_when_they_line_up(self):
        out = "Subcounts:\n  text+headers+captions (#headers/#floats/#inlines/#displayed)\n"
        out += "  1+0+0 (0/0/0/0) _top_\n" + "".join(f"  {n}+1+0 (1/0/0/0) Section: S{n}\n" for n in range(6))
        with mock.patch("serve.shutil.which", return_value="/usr/bin/texcount"), mock.patch(
            "serve.subprocess.run", return_value=subprocess.CompletedProcess([], 0, out, ""),
        ):
            self.assertEqual(serve.texcount_sections("x"), [1, 1, 2, 3, 4, 5, 6])
            result = serve.outline(self.main)
        self.assertEqual(result["counter"], "texcount")
        self.assertEqual(result["total_words"], 1 + sum(range(1, 7)))

    def test_rough_words(self):
        self.assertEqual(serve.rough_words(r"Some \textbf{bold} text $x^2$ and \cite{a}."), 4)
        self.assertEqual(serve.rough_words("\\begin{equation}\na+b\n\\end{equation} done"), 1)

    @unittest.skipUnless(shutil.which("texcount"), "texcount not installed")
    def test_real_texcount(self):
        plain = self.write("plain/main.tex", "\\section{A}\nx y z\n\\subsection{B}\nq w\n\\section{C}\nlast\n")
        result = serve.outline(plain)
        self.assertEqual(result["counter"], "texcount")
        self.assertTrue(all(i["words"] >= i["own"] for i in result["items"]))

    def test_references(self):
        bib = "@article{smith20,\n  author = {Smith, A.},\n  title = {A {Great} Paper},\n  year = 2020}\n"
        self.write("refs.bib", bib)
        self.write("lab.tex", "\\section{Sec}\\label{sec:a}\n\\caption{Cap}\\label{fig:b}\n")
        found = serve.references(self.root)
        self.assertEqual(found["bib"]["smith20"]["title"], "A Great Paper")
        self.assertEqual(found["bib"]["smith20"]["year"], "2020")
        self.assertEqual((found["labels"]["fig:b"]["file"], found["labels"]["fig:b"]["line"]), ("lab.tex", 2))

    def test_log_excerpt_starts_at_the_tex_error(self):
        log = ("summary\nfiles/x/main.tex:3: Undefined control sequence.\n\n! Undefined control sequence.\n"
               "<recently read> \\foo\n\nl.3 \\foo\n         \nmore\nextra\n")
        out = serve.log_excerpt(log, {"file": "main.tex", "line": 3, "message": "Undefined control sequence."})
        self.assertTrue(out.startswith("! Undefined control sequence."))
        self.assertIn("l.3 \\foo", out)


class BusTests(unittest.TestCase):
    def test_resume_from_revision(self):
        bus = serve.Bus(keep=3)
        for i in range(2):
            bus.publish("fs", {"i": i})
        self.assertEqual([m["rev"] for m in bus.since(0)], [1, 2])
        self.assertEqual([m["rev"] for m in bus.since(1)], [2])
        self.assertEqual(bus.since(2), [])

    def test_gap_or_unknown_revision_yields_a_state_snapshot(self):
        bus = serve.Bus(keep=3)
        for i in range(6):
            bus.publish("fs", {"i": i})
        for stale in (0, 1, None, 99):  # too old, too old, brand new client, from a previous server run
            with self.subTest(since=stale):
                (msg,) = bus.since(stale)
                self.assertEqual((msg["type"], msg["resync"], msg["rev"]), ("state", True, 6))
        self.assertEqual([m["rev"] for m in bus.since(3)], [4, 5, 6])

    def test_wait_returns_new_messages_and_times_out_empty(self):
        bus = serve.Bus()
        bus.publish("a", 1)
        self.assertEqual(bus.wait(1, 0.05), [])
        threading.Timer(0.05, bus.publish, ("b", 2)).start()
        (msg,) = bus.wait(1, 2.0)
        self.assertEqual((msg["type"], msg["data"], msg["rev"]), ("b", 2, 2))

    def test_ping_is_answered_and_unknown_types_ignored(self):
        reply = serve.handle_client_message({"type": "ping", "data": 7}, "c1")
        self.assertEqual((reply["type"], reply["data"]["t"]), ("pong", 7))
        self.assertIsNone(serve.handle_client_message({"type": "nope"}, "c1"))
        with mock.patch.dict(serve.HANDLERS, {"echo": lambda m, c: {"type": "echo", "data": [m["data"], c]}}):
            self.assertEqual(serve.handle_client_message({"type": "echo", "data": 1}, "c9")["data"], [1, "c9"])


class WebSocketFrames(unittest.TestCase):
    def test_accept_key_matches_rfc6455_example(self):
        self.assertEqual(serve.ws_accept("dGhlIHNhbXBsZSBub25jZQ=="), "s3pPLMBiTxaQ9kYGzzhZRbK+xOo=")

    def test_round_trip_all_length_forms_masked_and_not(self):
        for size in (0, 5, 125, 126, 300, 65535, 65536, 70000):
            for mask in (None, b"\x01\x02\x03\x04"):
                with self.subTest(size=size, masked=bool(mask)):
                    payload = bytes(i % 251 for i in range(size))
                    frame = serve.ws_encode(0x2, payload, mask)
                    self.assertEqual(serve.ws_read(io.BytesIO(frame).read), (0x2, payload))

    def test_header_bytes(self):
        self.assertEqual(serve.ws_encode(0x1, b"hi"), b"\x81\x02hi")
        self.assertEqual(serve.ws_encode(0x1, b"x" * 126)[:4], b"\x81\x7e\x00\x7e")
        self.assertEqual(serve.ws_encode(0x1, b"a", b"\x00\x00\x00\x00"), b"\x81\x81\x00\x00\x00\x00a")

    def test_fragmented_message_is_joined_and_control_frames_pass(self):
        first = bytes([0x01, 3]) + b"abc"          # text, FIN=0
        ping = bytes([0x89, 1]) + b"p"             # ping in the middle
        last = bytes([0x80, 2]) + b"de"            # continuation, FIN=1
        read, partial = io.BytesIO(first + ping + last).read, {}
        self.assertEqual(serve.ws_read(read, partial), (0x9, b"p"))
        self.assertEqual(serve.ws_read(read, partial), (0x1, b"abcde"))
        self.assertIsNone(serve.ws_read(read, partial))

    def test_truncated_and_oversized_frames(self):
        self.assertIsNone(serve.ws_read(io.BytesIO(b"\x81").read))
        self.assertIsNone(serve.ws_read(io.BytesIO(b"\x81\x05ab").read))
        huge = bytes([0x81, 127]) + (serve.WS_MAX + 1).to_bytes(8, "big")
        with self.assertRaises(ValueError):
            serve.ws_read(io.BytesIO(huge).read)


class HttpApi(TempDoc):
    def setUp(self):
        super().setUp()
        self.write("main.tex", "\\section{A}\nhello\n")
        self.write("fig.png", "not really a png")
        patch = mock.patch.dict(serve.DOCS, {"demo": self.root / "main.tex"}, clear=True)
        patch.start()
        self.addCleanup(patch.stop)
        mock.patch.dict(serve.SETTINGS, {"check_host": True}).start()
        self.addCleanup(mock.patch.stopall)
        self.server = ThreadingHTTPServer(("127.0.0.1", 0), serve.Handler)
        self.server.daemon_threads = True
        threading.Thread(target=self.server.serve_forever, daemon=True).start()
        self.addCleanup(self.server.server_close)
        self.addCleanup(self.server.shutdown)
        self.port = self.server.server_address[1]

    def request(self, method, path, body=None, headers=None):
        conn = http.client.HTTPConnection("127.0.0.1", self.port, timeout=10)
        hdrs = {"Host": f"127.0.0.1:{self.port}", **(headers or {})}
        data = None
        if body is not None:
            data = json.dumps(body)
            hdrs.setdefault("Content-Type", "application/json")
        conn.request(method, path, data, hdrs)
        res = conn.getresponse()
        raw = res.read()
        conn.close()
        try:
            return res.status, json.loads(raw)
        except ValueError:
            return res.status, raw

    def test_health(self):
        status, data = self.request("GET", "/api/health")
        self.assertEqual(status, 200)
        self.assertTrue(data["ok"])
        self.assertEqual(data["docs"], ["demo"])
        self.assertEqual(set(data), {"ok", "rev", "uptime", "docs", "synctex", "texcount"})

    def test_health_refuses_foreign_host(self):
        self.assertEqual(self.request("GET", "/api/health", headers={"Host": "evil.example"})[0], 403)

    def test_read_write_roundtrip_and_conflict(self):
        status, loaded = self.request("GET", "/api/file?doc=demo&path=main.tex")
        self.assertEqual((status, loaded["text"]), (200, "\\section{A}\nhello\n"))
        url = "/api/file?doc=demo&path=main.tex"
        status, saved = self.request("PUT", url, {"text": "edited", "base": loaded["version"]})
        self.assertEqual(status, 200)
        self.assertEqual((self.root / "main.tex").read_text(), "edited")
        status, err = self.request("PUT", url, {"text": "again", "base": loaded["version"]})
        self.assertEqual((status, err["current"]), (409, saved["version"]))

    def test_traversal_is_refused_over_http(self):
        for path in ("../secret.tex", "%2e%2e/secret.tex", "..%2fsecret.tex"):
            with self.subTest(path=path):
                self.assertIn(self.request("GET", f"/api/file?doc=demo&path={path}")[0], (400, 403))
                status, _ = self.request("PUT", f"/api/file?doc=demo&path={path}", {"text": "x"})
                self.assertIn(status, (400, 403))
        self.assertEqual((self.base / "secret.tex").read_text(), "secret")
        self.assertEqual(self.request("GET", "/api/file?doc=demo&path=fig.png")[0], 415)
        self.assertEqual(self.request("GET", "/api/file?doc=nope&path=main.tex")[0], 404)

    def test_cross_site_and_non_json_writes_are_refused(self):
        url = "/api/file?doc=demo&path=main.tex"
        status, _ = self.request("PUT", url, {"text": "x"}, {"Origin": "http://evil.example"})
        self.assertEqual(status, 403)
        status, _ = self.request("PUT", url, {"text": "x"}, {"Origin": f"http://127.0.0.1:{self.port}"})
        self.assertEqual(status, 200)
        conn = http.client.HTTPConnection("127.0.0.1", self.port)
        conn.request("PUT", url, "text=x", {"Host": f"127.0.0.1:{self.port}", "Content-Type": "text/plain"})
        self.assertEqual(conn.getresponse().status, 415)

    def test_files_outline_and_raw_image(self):
        _, listing = self.request("GET", "/api/files?doc=demo")
        self.assertEqual({f["path"]: f["kind"] for f in listing["files"]}, {"main.tex": "text", "fig.png": "image"})
        with mock.patch("serve.texcount_sections", return_value=None):
            _, outline = self.request("GET", "/api/outline?doc=demo")
        self.assertEqual([i["title"] for i in outline["items"]], ["A"])
        status, raw = self.request("GET", "/api/raw?doc=demo&path=fig.png")
        self.assertEqual((status, raw), (200, b"not really a png"))
        self.assertEqual(self.request("GET", "/api/raw?doc=demo&path=main.tex")[0], 415)

    def test_long_poll_resumes_by_revision_and_post_replies(self):
        rev = serve.BUS.rev
        with mock.patch.object(serve, "POLL_HOLD", 0.2):
            status, data = self.request("GET", f"/api/poll?since={rev}")
        self.assertEqual((status, data["events"]), (200, []))
        serve.BUS.publish("fs", {"doc": "demo", "changed": ["main.tex"], "removed": []})
        _, data = self.request("GET", f"/api/poll?since={rev}")
        self.assertEqual([e["type"] for e in data["events"]], ["fs"])
        _, data = self.request("GET", "/api/poll")  # new client: snapshot
        self.assertEqual(data["events"][0]["type"], "state")
        status, data = self.request("POST", "/api/send?cid=t", {"messages": [{"type": "ping", "data": 3}]})
        self.assertEqual((status, data["replies"][0]["type"]), (200, "pong"))

    def test_websocket_handshake_and_pong(self):
        import socket
        sock = socket.create_connection(("127.0.0.1", self.port), timeout=10)
        sock.sendall((
            f"GET /ws?cid=t HTTP/1.1\r\nHost: 127.0.0.1:{self.port}\r\nUpgrade: websocket\r\nConnection: Upgrade\r\n"
            "Sec-WebSocket-Key: dGhlIHNhbXBsZSBub25jZQ==\r\nSec-WebSocket-Version: 13\r\n\r\n"
        ).encode())
        stream = sock.makefile("rb")
        self.assertIn(b"101", stream.readline())
        headers = b""
        while (line := stream.readline()) not in (b"\r\n", b""):
            headers += line
        self.assertIn(b"s3pPLMBiTxaQ9kYGzzhZRbK+xOo=", headers)
        self.assertEqual(json.loads(serve.ws_read(stream.read)[1])["type"], "state")  # snapshot first
        sock.sendall(serve.ws_encode(0x1, json.dumps({"type": "ping", "data": 5}).encode(), b"\x09\x08\x07\x06"))
        for _ in range(5):
            message = json.loads(serve.ws_read(stream.read)[1])
            if message["type"] == "pong":
                break
        self.assertEqual(message["data"]["t"], 5)
        sock.sendall(serve.ws_encode(0x8, b"", b"\x01\x01\x01\x01"))
        sock.close()

    def test_ui_files_are_served_and_nothing_else(self):
        self.assertEqual(self.request("GET", "/")[0], 200)
        self.assertEqual(self.request("GET", "/ui/app.js")[0], 200)
        self.assertEqual(self.request("GET", "/ui/..%2fserve.py")[0], 404)
        self.assertEqual(self.request("GET", "/ui/nothing.js")[0], 404)


PROSE_JS = r"""
import * as p from "__PROSE__";
import assert from "node:assert/strict";
const editable = [
  "Plain words only.", "Hello \\textbf{bold \\emph{nested}} and \\textit{it}.",
  "50\\% of a \\& b \\$5 \\#1 \\_x \\{y\\}", "two\nlines of\ntext", "\\textbf{}", "\\textbf{a}\\textbf{b}",
  "  leading and trailing  ", "dash -- and --- and `quotes'' stay", "unicode caf\u00e9 \u2014 ok", "a < b > c",
];
for (const src of editable) {
  const r = p.toEditor(src);
  assert.ok(!r.error, src + " -> " + r.error);
  assert.equal(p.htmlToLatex(r.html), src, "round trip: " + src);
}
const readOnly = ["\\section{x}", "$x$", "50% comment", "x~y", "\\cite{a}", "a_b", "{group}", "\\textbf{open", "close}",
  "\\\\", "\\textbf{\\ref{a}}"];
for (const src of readOnly) assert.ok(p.toEditor(src).error, "should be read-only: " + src);
// Edits made in the contentEditable: browsers add <br>, <div>, <strong>, &nbsp;.
assert.equal(p.htmlToLatex("a<strong>b</strong>&nbsp;c &amp; d"), "a\\textbf{b} c \\& d");
assert.equal(p.htmlToLatex("x<i>y<b>z</b></i>"), "x\\textit{y\\textbf{z}}");
assert.equal(p.htmlToLatex("line<br>two"), "line\ntwo");
assert.deepEqual(p.paragraphAt("a\n\nb c\nd\n\ne", 5), {from: 3, to: 8, text: "b c\nd"});
"""


class ProseRoundTrip(unittest.TestCase):
    @unittest.skipUnless(shutil.which("node"), "node not installed")
    def test_prose_conversion_is_lossless_or_read_only(self):
        script = PROSE_JS.replace("__PROSE__", (UI_DIR / "prose.js").as_uri())
        result = subprocess.run(
            ["node", "--input-type=module", "-e", script], capture_output=True, text=True, timeout=30,
        )
        self.assertEqual(result.returncode, 0, result.stderr)


if __name__ == "__main__":
    unittest.main()
