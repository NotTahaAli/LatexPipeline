"""serve.py: path safety, atomic writes, outline and word counts, message bus, WebSocket frames, HTTP API."""

import http.client
import importlib.util
import io
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from http.server import ThreadingHTTPServer
from pathlib import Path
from unittest import mock
from urllib.parse import quote

import _support  # noqa: F401 - puts scripts/ on sys.path
import build
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

    def test_save_with_a_base_never_recreates_a_deleted_file(self):
        path = self.write("main.tex", "v1")
        loaded = serve.read_text_file(self.root, "main.tex")["version"]
        path.unlink()
        with self.assertRaises(serve.ApiError) as ctx:
            serve.write_text_file(self.root, "main.tex", "mine", loaded)
        self.assertEqual((ctx.exception.status, ctx.exception.extra), (409, {"deleted": True}))
        self.assertFalse(path.exists())
        serve.write_text_file(self.root, "main.tex", "mine", None)  # No base may create.
        self.assertTrue(path.exists())

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
        with mock.patch.dict(serve.HANDLERS, {"echo": lambda m, c, r: {"type": "echo", "data": [m["data"], c]}}):
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


class ServerCase(TempDoc):
    """A live server over a one-document temp directory, with a request helper."""

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


class HttpApi(ServerCase):
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


class SharedState(unittest.TestCase):
    """Isolates the module-level sharing and room state between tests."""

    def setUp(self):
        saved = dict(serve.SHARE)
        self.addCleanup(lambda: (serve.SHARE.clear(), serve.SHARE.update(saved)))
        serve.SHARE.update(on=False, tokens={}, doc=None, public=None, hosts=set(), port=0, tunnel=None)
        self.addCleanup(serve.share_env, False)
        for table in (serve.ROOMS, serve.CLIENTS, serve.RATE):
            table.clear()
            self.addCleanup(table.clear)

    def share_on(self, doc="demo"):
        serve.share_enable(doc, "local", serve.SHARE["port"])
        return serve.SHARE["tokens"]


class TunnelParsing(unittest.TestCase):
    def test_urls_from_each_tools_output(self):
        samples = {
            "cloudflared": (
                "2025-01-01T00:00:00Z INF Requesting new quick Tunnel on trycloudflare.com...\n"
                'ERR failed to request quick Tunnel: Post "https://api.trycloudflare.com/tunnel": EOF\n'
                "INF |  https://quiet-river-1234.trycloudflare.com  |\n",
                "https://quiet-river-1234.trycloudflare.com"),
            "ngrok": (
                'lvl=info msg="started tunnel" obj=tunnels name=command_line addr=http://localhost:8000 '
                "url=https://ab12-34-56.ngrok-free.app\n", "https://ab12-34-56.ngrok-free.app"),
            "localtunnel": ("your url is: https://little-owls-jump.loca.lt\n", "https://little-owls-jump.loca.lt"),
            "pinggy": (
                "You are not authenticated.\nhttp://rnabc-1-2-3-4.a.free.pinggy.link\n"
                "https://rnabc-1-2-3-4.a.free.pinggy.link\n", "https://rnabc-1-2-3-4.a.free.pinggy.link"),
            "localhost.run": (
                "create an account: https://admin.localhost.run/\n"
                "abc123def.lhr.life tunneled with tls termination, https://abc123def.lhr.life\n",
                "https://abc123def.lhr.life"),
        }
        for provider, (text, want) in samples.items():
            with self.subTest(provider=provider):
                self.assertEqual(serve.find_public_url(provider, text), want)
                self.assertIsNone(serve.find_public_url(provider, "starting up, nothing yet\n"))

    def test_ngrok_local_api(self):
        body = json.dumps({"tunnels": [{"public_url": "http://x.ngrok.io"}, {"public_url": "https://x.ngrok.io"}]})
        self.assertEqual(serve.ngrok_public_url(body), "https://x.ngrok.io")
        self.assertIsNone(serve.ngrok_public_url("not json"))
        self.assertIsNone(serve.ngrok_public_url('{"tunnels": []}'))

    def test_auto_prefers_cloudflared_then_ngrok_then_npx_then_ssh(self):
        def with_tools(*present):
            return mock.patch("serve.shutil.which", lambda n: f"/bin/{n}" if n in present else None)

        for present, want in [
            (("cloudflared", "ngrok", "npx", "ssh"), "cloudflared"), (("ngrok", "npx", "ssh"), "ngrok"),
            (("npx", "ssh"), "localtunnel"), (("ssh",), "pinggy"),
        ]:
            with self.subTest(present=present), with_tools(*present):
                self.assertEqual(serve.pick_provider("auto"), want)
        with with_tools(), self.assertRaises(serve.ApiError) as ctx:
            serve.pick_provider("auto")
        self.assertIn("cloudflared", str(ctx.exception))
        with with_tools(), self.assertRaises(serve.ApiError) as ctx:
            serve.pick_provider("ngrok")
        self.assertIn("ngrok config add-authtoken", str(ctx.exception))
        self.assertEqual(serve.pick_provider("local"), "local")

    def test_tunnel_start_reads_the_url_and_stop_kills_the_process(self):
        script = "import sys,time; print('your url is: https://t.loca.lt', flush=True); time.sleep(60)"
        spec = dict(serve.TUNNELS["localtunnel"], cmd=lambda: [sys.executable, "-c", script], args=lambda p: [])
        with mock.patch.dict(serve.TUNNELS, {"localtunnel": spec}):
            tunnel = serve.Tunnel("localtunnel", 1)
            self.assertEqual(tunnel.start(timeout=10), "https://t.loca.lt")
            self.assertIsNone(tunnel.proc.poll())
            tunnel.stop()
            self.assertIsNotNone(tunnel.proc.poll())
            dead = dict(spec, cmd=lambda: [sys.executable, "-c", "print('boom')"])
        with mock.patch.dict(serve.TUNNELS, {"localtunnel": dead}), self.assertRaises(RuntimeError) as ctx:
            serve.Tunnel("localtunnel", 1).start(timeout=10)
        self.assertIn("boom", str(ctx.exception))


class TokenAuth(SharedState):
    def test_off_means_owner_and_on_needs_a_matching_token(self):
        self.assertEqual(serve.role_for_token(None), "owner")
        tokens = self.share_on()
        self.assertEqual({len(t) for t in tokens.values()}, {43})  # token_urlsafe(32)
        self.assertEqual(len(set(tokens.values())), 3)
        for role, token in tokens.items():
            self.assertEqual(serve.role_for_token(token), role)
        for bad in (None, "", "nope", tokens["view"][:-1], tokens["view"] + "x", "\u00e9" * 43):
            self.assertIsNone(serve.role_for_token(bad))

    def test_regenerate_replaces_view_and_edit_but_not_owner(self):
        before = dict(self.share_on())
        serve.share_regenerate()
        after = serve.SHARE["tokens"]
        self.assertEqual(after["owner"], before["owner"])
        self.assertIsNone(serve.role_for_token(before["view"]))
        self.assertIsNone(serve.role_for_token(before["edit"]))
        serve.share_disable()
        self.assertEqual(serve.role_for_token(before["view"]), "owner")  # Off again: no tokens, no gate.

    def test_links_carry_the_tokens(self):
        tokens = self.share_on("my doc")
        serve.SHARE["public"] = "https://x.trycloudflare.com"
        links = serve.share_links()
        self.assertEqual(links["view"], f"https://x.trycloudflare.com/?token={tokens['view']}#my%20doc")
        self.assertEqual(set(links), {"view", "edit"})

    def test_rate_limit(self):
        self.assertEqual([serve.rate_ok("k", 2, 60) for _ in range(3)], [True, True, False])
        self.assertTrue(serve.rate_ok("other", 2, 60))

    @unittest.skipUnless(sys.version_info >= (3, 11) or importlib.util.find_spec("tomli"), "build.toml needs tomllib")
    def test_shell_escape_is_refused_while_sharing(self):
        with _support.fake_repo() as root:
            main = _support.write_doc(root, "d", "x", 'shell_escape = true\nlatexmk_args = ["-bibtex"]\n')
            self.assertTrue(serve.build.read_settings(main)["shell_escape"])  # Not sharing: the owner's choice.
            self.share_on("d")
            settings = serve.build.read_settings(main)
            self.assertFalse(settings["shell_escape"])
            for bad in ("-shell-escape", "--shell-escape", "-enable-write18", '-pdflatex="pdflatex -shell-escape %O"',
                        "-pdflatex=/tmp/evil", "-e", "-r"):
                with self.subTest(arg=bad):
                    (main.parent / "build.toml").write_text(f"latexmk_args = [{json.dumps(bad)}]\n", encoding="utf-8")
                    with self.assertRaises(serve.build.ConfigError):
                        serve.build.read_settings(main)
            for fine in ("-bibtex", "-f", "-silent", "-pdflatex"):
                (main.parent / "build.toml").write_text(f"latexmk_args = [{json.dumps(fine)}]\n", encoding="utf-8")
                self.assertEqual(serve.build.read_settings(main)["latexmk_args"], ["-norc", fine])


class ShareHttp(SharedState, ServerCase):
    def setUp(self):
        SharedState.setUp(self)
        ServerCase.setUp(self)
        (self.root / "other").mkdir()
        self.write("build.toml", "engine = 'pdflatex'\n")
        other = self.base / "second"
        other.mkdir()
        (other / "main.tex").write_text("secret doc", encoding="utf-8")
        mock.patch.dict(serve.DOCS, {"second": other / "main.tex"}).start()
        serve.SHARE["port"] = self.port
        self.tokens = self.share_on()

    def get(self, method, path, role=None, body=None, headers=None):
        hdrs = dict(headers or {})
        if role:
            hdrs["Cookie"] = f"{serve.cookie_name()}={self.tokens[role]}"
        return self.request(method, path, body, hdrs)

    def test_every_endpoint_needs_a_token(self):
        for path in ("/", "/ui/app.js", "/api/health", "/api/config", "/api/file?doc=demo&path=main.tex", "/api/poll",
                     "/pdf/demo", "/events"):
            with self.subTest(path=path):
                self.assertEqual(self.get("GET", path)[0], 401)
        self.assertEqual(self.get("PUT", "/api/file?doc=demo&path=main.tex", None, {"text": "x"})[0], 401)
        self.assertEqual(self.get("POST", "/rebuild?doc=demo")[0], 401)
        self.assertEqual(self.get("POST", "/api/send", None, {"messages": []})[0], 401)
        wrong = {"Cookie": f"{serve.cookie_name()}={'a' * 43}"}
        self.assertEqual(self.request("GET", "/api/health", headers=wrong)[0], 401)
        self.assertEqual(self.request("GET", "/api/health", headers={"Cookie": "garbage;;="})[0], 401)

    def raw_get(self, path, headers=None):
        conn = http.client.HTTPConnection("127.0.0.1", self.port, timeout=10)
        conn.request("GET", path, headers={"Host": f"127.0.0.1:{self.port}", **(headers or {})})
        res = conn.getresponse()
        res.read()
        conn.close()
        return res

    def test_cookie_flow_sets_an_httponly_lax_cookie_and_strips_the_token(self):
        res = self.raw_get(f"/?token={self.tokens['edit']}")
        self.assertEqual((res.status, res.getheader("Location")), (302, "/"))
        cookie = res.getheader("Set-Cookie")
        self.assertIn(f"{serve.cookie_name()}={self.tokens['edit']}", cookie)
        self.assertIn("HttpOnly", cookie)
        self.assertIn("SameSite=Lax", cookie)
        self.assertEqual(self.raw_get("/?token=wrong").status, 401)
        # The token in the query string authenticates nothing but the first page load.
        self.assertEqual(self.raw_get(f"/api/health?token={self.tokens['edit']}").status, 401)
        self.assertEqual(self.request("GET", "/", headers={"Cookie": cookie.split(";")[0]})[0], 200)

    def test_view_role_is_read_only_and_confined_to_the_shared_document(self):
        ok = ["/", "/api/config", "/api/health", "/api/files?doc=demo", "/api/file?doc=demo&path=main.tex",
              "/api/outline?doc=demo", "/api/raw?doc=demo&path=fig.png", "/api/warnings?doc=demo"]
        for path in ok:
            with self.subTest(path=path):
                self.assertEqual(self.get("GET", path, "view")[0], 200)
        _, config = self.get("GET", "/api/config", "view")
        self.assertEqual(config["role"], "view")
        self.assertEqual(self.get("GET", "/api/health", "view")[1]["docs"], ["demo"])  # no 'second'
        denied = [
            ("GET", "/api/files?doc=second"), ("GET", "/api/file?doc=second&path=main.tex"), ("GET", "/pdf/second"),
            ("GET", "/api/warnings?doc=second"),
            ("GET", "/events"), ("GET", "/api/share"), ("GET", "/forward?doc=demo&file=main.tex&line=1"),
            ("POST", "/rebuild?doc=demo"), ("POST", "/api/share"), ("POST", "/api/share/stop"),
            ("POST", "/api/share/regenerate"), ("PUT", "/api/file?doc=demo&path=main.tex"),
        ]
        for method, path in denied:
            with self.subTest(path=path):
                status, _ = self.get(method, path, "view", {"text": "pwn"} if method != "GET" else None)
                self.assertEqual(status, 403)
        self.assertEqual((self.root / "main.tex").read_text(), "\\section{A}\nhello\n")

    def test_edit_role_edits_only_the_shared_document_and_never_build_config(self):
        url = "/api/file?doc=demo&path=main.tex"
        self.assertEqual(self.get("PUT", url, "edit", {"text": "edited"})[0], 200)
        self.assertEqual((self.root / "main.tex").read_text(), "edited")
        for path in ("build.toml", "other/.latexmkrc", "latexmkrc", ".latexmkrc"):
            with self.subTest(path=path):
                self.assertEqual(self.get("PUT", f"/api/file?doc=demo&path={path}", "edit", {"text": "x"})[0], 403)
        self.assertEqual(self.get("PUT", "/api/file?doc=second&path=main.tex", "edit", {"text": "x"})[0], 403)
        self.assertEqual((self.base / "second" / "main.tex").read_text(), "secret doc")
        self.assertEqual(self.get("GET", "/api/files?doc=second", "edit")[0], 403)
        self.assertEqual(self.get("POST", "/api/share", "edit", {"provider": "local"})[0], 403)

    def test_file_operations_need_the_edit_link_stay_in_the_shared_document_and_spare_build_config(self):
        for who in ("view", None):
            for op, path in (("newfile", "x.tex"), ("mkdir", "d"), ("delete", "other"), ("rename", "other&to=y")):
                with self.subTest(who=who, op=op):
                    self.assertEqual(self.get("POST", f"/api/fs?doc=demo&op={op}&path={path}", who)[0],
                                     403 if who else 401)
        self.assertEqual(self.get("POST", "/api/fs?doc=second&op=newfile&path=x.tex", "edit")[0], 403)
        for path in ("build.toml", "sub/.latexmkrc", "latexmkrc", ".latexmkrc", "sub/BUILD.TOML"):
            for op in ("newfile", "delete", "mkdir"):
                with self.subTest(op=op, path=path):
                    self.assertEqual(self.get("POST", f"/api/fs?doc=demo&op={op}&path={path}", "edit")[0], 403)
        # Neither can a harmless file be renamed into a build-config name, nor the config renamed away.
        self.assertEqual(self.get("POST", "/api/fs?doc=demo&op=rename&path=other&to=latexmkrc", "edit")[0], 403)
        self.assertEqual(self.get("POST", "/api/fs?doc=demo&op=rename&path=build.toml&to=x.toml", "edit")[0], 403)
        self.assertTrue((self.root / "build.toml").exists())
        self.assertEqual(self.get("POST", "/api/fs?doc=demo&op=newfile&path=made.tex", "edit")[0], 200)
        self.assertTrue((self.root / "made.tex").exists())
        self.assertEqual(self.get("POST", "/api/fs?doc=demo&op=mkdir&path=made2", "owner")[0], 200)
        self.assertEqual(self.get("POST", "/api/fs?doc=second&op=newfile&path=x.tex", "owner")[0], 200)
        self.assertEqual(self.get("POST", "/api/fs?doc=demo&op=newfile&path=../escape.tex", "edit")[0], 400)

    def test_uploads_need_the_edit_link_and_the_shared_document(self):
        png = b"\x89PNG\r\n\x1a\n" + b"0" * 20
        conn = http.client.HTTPConnection("127.0.0.1", self.port, timeout=10)

        def post(doc, role):
            hdrs = {"Host": f"127.0.0.1:{self.port}", "Content-Type": "image/png"}
            if role:
                hdrs["Cookie"] = f"{serve.cookie_name()}={self.tokens[role]}"
            conn.request("POST", f"/api/upload?doc={doc}&name=up.png", png, hdrs)
            res = conn.getresponse()
            res.read()
            return res.status

        self.assertEqual(post("demo", None), 401)
        self.assertEqual(post("demo", "view"), 403)
        self.assertEqual(post("second", "edit"), 403)
        self.assertFalse((self.root / "up.png").exists())
        self.assertEqual(post("demo", "edit"), 200)
        self.assertTrue((self.root / "up.png").is_file())
        conn.close()

    def test_rebuilds_are_rate_limited_for_editors_only(self):
        limit = serve.REBUILD_LIMIT[0]
        codes = [self.get("POST", "/rebuild?doc=demo", "edit")[0] for _ in range(limit + 2)]
        self.assertEqual(codes, [200] * limit + [429, 429])
        self.assertEqual(self.get("POST", "/rebuild?doc=demo", "owner")[0], 200)
        self.assertEqual(self.get("POST", "/rebuild?doc=second", "edit")[0], 403)

    def test_owner_manages_sharing(self):
        status, info = self.get("GET", "/api/share", "owner")
        self.assertEqual((status, info["on"]), (200, True))
        self.assertEqual(set(info["links"]), {"view", "edit"})
        self.assertEqual(self.get("GET", "/api/files?doc=second", "owner")[0], 200)
        old = self.tokens["view"]
        self.assertEqual(self.get("POST", "/api/share/regenerate", "owner")[0], 200)
        self.assertEqual(self.request("GET", "/api/health", headers={"Cookie": f"{serve.cookie_name()}={old}"})[0], 401)
        self.assertEqual(self.get("POST", "/api/share/stop", "owner")[0], 200)
        self.assertEqual(self.request("GET", "/api/health")[0], 200)  # Sharing off: open again.

    def test_starting_a_share_hands_the_owner_a_cookie(self):
        serve.share_disable()
        conn = http.client.HTTPConnection("127.0.0.1", self.port, timeout=10)
        conn.request("POST", "/api/share", json.dumps({"provider": "local", "doc": "demo"}),
                     {"Host": f"127.0.0.1:{self.port}", "Content-Type": "application/json"})
        res = conn.getresponse()
        res.read()
        self.assertEqual(res.status, 200)
        self.assertIn(serve.SHARE["tokens"]["owner"], res.getheader("Set-Cookie"))
        conn.close()

    def test_tunnel_host_is_accepted_and_others_are_not(self):
        self.assertEqual(self.get("GET", "/api/health", "view", headers={"Host": "x.trycloudflare.com"})[0], 403)
        serve.SHARE["hosts"].add("x.trycloudflare.com")
        self.assertEqual(self.get("GET", "/api/health", "view", headers={"Host": "x.trycloudflare.com"})[0], 200)
        self.assertEqual(self.get("GET", "/api/health", "view", headers={"Host": "evil.example"})[0], 403)
        # Cross-site writes are still refused even with a valid cookie.
        origin = {"Origin": "https://evil.example"}
        self.assertEqual(self.get("PUT", "/api/file?doc=demo&path=main.tex", "edit", {"text": "x"}, origin)[0], 403)

    def test_security_headers(self):
        res = self.raw_get("/", {"Cookie": f"{serve.cookie_name()}={self.tokens['view']}"})
        self.assertEqual(res.getheader("X-Content-Type-Options"), "nosniff")
        self.assertEqual(res.getheader("Referrer-Policy"), "no-referrer")
        policy = res.getheader("Content-Security-Policy")
        self.assertIn("frame-ancestors 'none'", policy)
        self.assertIn("default-src 'none'", policy)
        self.assertRegex(policy, r"script-src 'self' 'sha256-[A-Za-z0-9+/=]+' https://esm\.sh")
        self.assertNotIn("unsafe-eval", policy)
        for host in re.findall(r"https://[^\s;]+", policy):
            self.assertIn(host, ("https://esm.sh", "https://cdnjs.cloudflare.com", "https://cdn.jsdelivr.net"))

    def ws(self, role=None, **extra):
        import socket
        sock = socket.create_connection(("127.0.0.1", self.port), timeout=10)
        cookie = f"Cookie: {serve.cookie_name()}={self.tokens[role]}\r\n" if role else ""
        sock.sendall((
            f"GET /ws?cid={extra.get('cid', 'w1')} HTTP/1.1\r\nHost: 127.0.0.1:{self.port}\r\nUpgrade: websocket\r\n"
            "Connection: Upgrade\r\nSec-WebSocket-Key: dGhlIHNhbXBsZSBub25jZQ==\r\n"
            f"Sec-WebSocket-Version: 13\r\n{cookie}\r\n"
        ).encode())
        stream = sock.makefile("rb")
        status = stream.readline()
        while stream.readline() not in (b"\r\n", b""):
            pass
        return sock, stream, status

    def send(self, sock, message):
        sock.sendall(serve.ws_encode(0x1, json.dumps(message).encode(), b"\x01\x02\x03\x04"))

    def read_until(self, stream, kind, limit=20):
        for _ in range(limit):
            frame = serve.ws_read(stream.read)
            message = json.loads(frame[1])
            if message["type"] == kind:
                return message
        self.fail(f"no {kind} message")

    def test_websocket_needs_a_token_and_view_cannot_write(self):
        sock, _, status = self.ws()
        self.assertIn(b"401", status)
        sock.close()
        sock, stream, status = self.ws("view")
        self.assertIn(b"101", status)
        state = self.read_until(stream, "state")
        self.assertEqual([d["name"] for d in state["data"]["docs"]], [])  # 'demo' is not in STATE here; 'second' hidden
        self.send(sock, {"type": "y-join", "data": {"doc": "demo", "path": "main.tex", "aid": 1}})
        self.assertEqual(self.read_until(stream, "y-state")["data"]["seed"], "\\section{A}\nhello\n")
        self.send(sock, {"type": "y-join", "data": {"doc": "second", "path": "main.tex", "aid": 1}})
        self.assertEqual(self.read_until(stream, "error")["data"]["status"], 404)
        self.send(sock, {"type": "y-update", "data": {"room": "demo\nmain.tex", "u": "AAA"}})
        self.assertEqual(self.read_until(stream, "error")["data"]["status"], 403)
        self.assertEqual(serve.ROOMS["demo\nmain.tex"]["log"], [])
        sock.close()

    def test_websocket_closes_when_the_token_is_regenerated(self):
        sock, stream, _ = self.ws("view", cid="w2")
        self.read_until(stream, "state")
        serve.share_regenerate()
        self.send(sock, {"type": "ping", "data": 1})
        for _ in range(20):
            frame = serve.ws_read(stream.read)
            if frame is None or frame[0] == 0x8:
                break
        else:
            self.fail("socket stayed open after revocation")
        sock.close()

    def test_edit_websocket_relays_updates_to_everyone(self):
        a, sa, _ = self.ws("edit", cid="ea")
        b, sb, _ = self.ws("view", cid="vb")
        self.send(a, {"type": "y-join", "data": {"doc": "demo", "path": "main.tex", "aid": 7}})
        self.send(b, {"type": "y-join", "data": {"doc": "demo", "path": "main.tex", "aid": 8}})
        self.read_until(sa, "y-state")
        self.read_until(sb, "y-state")
        self.send(a, {"type": "y-update", "data": {"room": "demo\nmain.tex", "u": "AQID"}})
        self.assertEqual(self.read_until(sb, "y-update")["data"], {"room": "demo\nmain.tex", "u": "AQID", "cid": "ea"})
        a.close()
        b.close()

    def test_long_poll_needs_a_token_and_filters_documents(self):
        self.assertEqual(self.get("GET", "/api/poll")[0], 401)
        start = serve.BUS.rev
        serve.BUS.publish("fs", {"doc": "second", "changed": ["main.tex"], "removed": []})
        serve.BUS.publish("fs", {"doc": "demo", "changed": ["main.tex"], "removed": []})
        _, data = self.get("GET", f"/api/poll?since={start}&cid=p1", "view")
        docs = [e["data"].get("doc") for e in data["events"] if e["type"] == "fs"]
        self.assertEqual(docs, ["demo"])
        rev = serve.BUS.rev
        serve.BUS.publish("fs", {"doc": "second", "changed": [], "removed": []})
        _, data = self.get("GET", f"/api/poll?since={rev}&cid=p1", "view")
        self.assertEqual(data["events"], [])
        self.assertEqual(data["rev"], rev + 1)  # Hidden messages still move the cursor.
        status, data = self.get("POST", "/api/send?cid=p1", "view",
                                {"messages": [{"type": "y-update", "data": {"room": "x", "u": "AA"}}]})
        self.assertEqual(data["replies"][0]["data"]["status"], 403)
        # A client id is bound to the role that first used it.
        self.assertEqual(self.get("GET", "/api/poll?since=0&cid=p1", "edit")[0], 403)

    def test_focus_only_the_edit_link_and_the_owner_can_start_one_and_only_for_the_shared_document(self):
        started = []
        with mock.patch.object(serve, "start_focus", side_effect=lambda name, rel: started.append((name, rel))):
            url = "/api/focus?doc=demo&path=main.tex"
            self.assertEqual(self.get("POST", url)[0], 401)  # no token
            self.assertEqual(self.get("POST", url, "view")[0], 403)
            self.assertEqual(self.get("POST", "/api/focus?doc=second&path=main.tex", "edit")[0], 403)
            self.assertEqual(self.get("POST", "/api/focus?doc=second&path=main.tex", "view")[0], 403)
            self.assertEqual(started, [])
            self.assertEqual(self.get("POST", url, "edit")[0], 200)
            self.assertEqual(self.get("POST", url, "owner")[0], 200)
            self.assertEqual(self.get("POST", "/api/focus?doc=second&path=main.tex", "owner")[0], 200)
            self.assertEqual(started, [("demo", "main.tex"), ("demo", "main.tex"), ("second", "main.tex")])

    def test_previews_count_against_the_rebuild_limit_of_editors(self):
        with mock.patch.object(serve, "start_focus", return_value=1.0):
            limit = serve.REBUILD_LIMIT[0]
            codes = [self.get("POST", "/api/focus?doc=demo&path=main.tex", "edit")[0] for _ in range(limit + 1)]
        self.assertEqual(codes, [200] * limit + [429])

    def test_the_preview_pdf_and_log_are_readable_for_the_shared_document_only(self):
        (self.root / "demo.focus.pdf").write_bytes(b"%PDF")
        with mock.patch.object(build, "focus_paths", return_value=(self.root / "demo.focus.pdf", self.root / "x.log")):
            self.assertEqual(self.get("GET", "/pdf/demo?focus=1", "view")[0], 200)
            self.assertEqual(self.get("GET", "/pdf/second?focus=1", "view")[0], 403)
            self.assertEqual(self.get("GET", "/log/second?focus=1", "edit")[0], 403)
            self.assertEqual(self.get("GET", "/pdf/demo?focus=1")[0], 401)

    def test_the_preview_state_of_another_document_never_reaches_a_shared_session(self):
        state = {"rev": 1, "topic": "doc", "type": "state", "data": {"docs": [
            {"name": "demo", "focus": {"status": "ok"}},
            {"name": "second", "focus": {"status": "ok", "path": "secret.tex"}}]}}
        shown = serve.visible([state], "view")[0]["data"]["docs"]
        self.assertEqual([d["name"] for d in shown], ["demo"])

    def test_a_preview_while_sharing_uses_the_guarded_settings(self):
        # build_focus reads build.toml through build.read_settings, which refuses code-running options while sharing.
        (self.root / "build.toml").write_text('latexmk_args = ["-shell-escape"]\n', encoding="utf-8")
        main = self.root / "main.tex"
        with mock.patch.object(build, "ROOT_DIR", self.base), mock.patch.object(build, "SOURCE_DIR", self.base), \
                mock.patch.object(build, "error") as error, \
                mock.patch.object(build.subprocess, "run", side_effect=AssertionError("LaTeX must not start")):
            self.assertFalse(build.build_focus(main, "latexmk", "x"))
        self.assertIn("not allowed while sharing", error.call_args[0][0])


class Rooms(SharedState):
    def setUp(self):
        super().setUp()
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        root = Path(self.tmp.name)
        with open(root / "main.tex", "w", encoding="utf-8", newline="") as handle:
            handle.write("seed\r\ntext")
        patch = mock.patch.dict(serve.DOCS, {"d": root / "main.tex"}, clear=True)
        patch.start()
        self.addCleanup(patch.stop)
        serve.SHARE["doc"] = "d"

    def join(self, cid, role="edit", **extra):
        serve.bind_client(cid, role)
        reply = serve.handle_client_message(
            {"type": "y-join", "data": {"doc": "d", "path": "main.tex", "aid": hash(cid) % 1000, **extra}}, cid, role)
        return reply["data"] if reply["type"] == "y-state" else reply

    def test_first_joiner_gets_the_disk_text_as_seed_and_the_leader_is_the_earliest_editor(self):
        first = self.join("a")
        got = (first["seed"], first["eol"], first["leader"], first["conflict"])
        self.assertEqual(got, ("seed\ntext", "\r\n", "a", False))
        self.assertEqual(self.join("viewer", "view")["leader"], "a")
        self.assertEqual(self.join("b")["leader"], "a")

    def test_updates_are_stored_deduplicated_and_replayed_to_late_joiners(self):
        self.join("a")
        for u in ("U1", "U1", "U2"):
            serve.handle_client_message({"type": "y-update", "data": {"room": "d\nmain.tex", "u": u}}, "a", "edit")
        late = self.join("late")
        self.assertEqual((late["updates"], late["seed"]), (["U1", "U2"], None))
        published = [m["data"]["u"] for m in serve.BUS.since(0) if m["type"] == "y-update"]
        self.assertEqual(published[-2:], ["U1", "U2"])
        update = {"type": "y-update", "data": {"room": "d\nmain.tex", "u": "x"}}
        reply = serve.handle_client_message(update, "ghost", "edit")
        self.assertEqual(reply["data"]["status"], 409)  # not a member

    def test_leader_moves_when_the_leader_leaves_or_goes_silent(self):
        self.join("a")
        self.join("b")
        serve.handle_client_message({"type": "y-leave", "data": {"room": "d\nmain.tex"}}, "a", "edit")
        self.assertEqual(serve.ROOMS["d\nmain.tex"]["leader"], "b")
        leaders = [m["data"]["leader"] for m in serve.BUS.since(0) if m["type"] == "y-leader"]
        self.assertEqual(leaders[-1], "b")
        serve.reap(time.monotonic() + serve.CLIENT_TIMEOUT + 1)
        self.assertEqual(serve.CLIENTS, {})
        self.assertEqual(serve.ROOMS["d\nmain.tex"]["members"], {})

    def test_empty_rooms_expire_after_the_grace_period(self):
        self.join("a")
        serve.client_gone("a")
        self.assertIn("d\nmain.tex", serve.ROOMS)
        serve.reap(time.monotonic() + serve.ROOM_GRACE + 1)
        self.assertNotIn("d\nmain.tex", serve.ROOMS)

    def test_a_returning_client_may_claim_an_expired_room_but_not_overrule_a_new_one(self):
        first = self.join("a")
        serve.client_gone("a")
        serve.reap(time.monotonic() + serve.ROOM_GRACE + 1)
        claimed = self.join("a", epoch=first["epoch"])
        self.assertEqual((claimed["epoch"], claimed["seed"], claimed["conflict"]), (first["epoch"], None, False))
        serve.client_gone("a")
        serve.reap(time.monotonic() + serve.ROOM_GRACE + 1)
        fresh = self.join("b")  # Someone else starts the file from disk meanwhile.
        stale = self.join("a", epoch=first["epoch"])
        self.assertTrue(stale["conflict"])
        self.assertEqual(stale["epoch"], fresh["epoch"])

    def test_viewers_cannot_claim_epochs_and_unknown_files_are_refused(self):
        viewer = self.join("v", "view", epoch="abc")  # Ignored: a viewer cannot make up a lineage.
        self.assertNotEqual(viewer["epoch"], "abc")
        self.assertEqual(viewer["seed"], "seed\ntext")
        serve.bind_client("x", "edit")
        for data in ({"doc": "nope", "path": "main.tex"}, {"doc": "d", "path": "fig.png"},
                     {"doc": "d", "path": "../x.tex"}):
            reply = serve.handle_client_message({"type": "y-join", "data": data}, "x", "edit")
            self.assertEqual(reply["type"], "error")

    def test_presence_lists_named_clients(self):
        serve.bind_client("a", "edit")
        ada = {"name": "Ada", "color": "#112233", "path": "main.tex"}
        serve.handle_client_message({"type": "hello", "data": ada}, "a", "edit")
        serve.handle_client_message({"type": "hello", "data": {"name": "Bob", "color": "javascript:1"}}, "b", "view")
        users = serve.presence()["users"]
        got = [(u["name"], u["role"], u["color"]) for u in users]
        self.assertEqual(got, [("Ada", "edit", "#112233"), ("Bob", "view", "#0969da")])
        serve.client_gone("a")
        self.assertEqual([u["name"] for u in serve.presence()["users"]], ["Bob"])

    def test_bye_removes_the_client_and_hands_over_leadership(self):
        self.join("a")
        self.join("b")
        serve.handle_client_message({"type": "bye"}, "a", "edit")
        self.assertEqual((serve.ROOMS["d\nmain.tex"]["leader"], list(serve.CLIENTS)), ("b", ["b"]))

    def test_ephemeral_messages_never_cause_a_resync(self):
        bus = serve.Bus(keep=3, ephemeral=2)
        bus.publish("fs", 1)
        for i in range(20):
            bus.publish("y-aware", i, ephemeral=True)
        self.assertEqual([m["type"] for m in bus.since(1)], ["y-aware", "y-aware"])
        self.assertNotIn("resync", bus.since(1)[0])

    def test_resume_from_a_future_revision_resyncs_at_once(self):
        bus = serve.Bus()
        bus.publish("fs", 1)
        began = time.monotonic()
        (msg,) = bus.wait(99, 5.0)  # A previous server run: no reason to hold the poll open
        self.assertTrue(msg["resync"])
        self.assertLess(time.monotonic() - began, 1.0)

    def test_memory_is_bounded_by_bytes_not_only_count(self):
        bus = serve.Bus(keep=2000, max_bytes=10_000)
        for _ in range(20):
            bus.publish("y-update", {"u": "x" * 3000})
        self.assertLessEqual(bus.bytes["log"], 10_000)
        self.assertLessEqual(len(bus.log), 3)
        self.assertTrue(bus.since(1)[0]["resync"])  # What was dropped can no longer be replayed
        bus.publish("y-aware", {"u": "y" * 100_000}, ephemeral=True)  # Larger than the cap: kept alone, never grows
        self.assertEqual(len(bus.eph), 1)


def pdflatex_reads(source: str, **env) -> bool:
    """Does `pdflatex` succeed on `source` (a main.tex body) in a scratch directory, under os.environ + env?"""
    with tempfile.TemporaryDirectory() as tmp:
        (Path(tmp) / "main.tex").write_text("\\documentclass{article}\\begin{document}" + source + "\\end{document}")
        (Path(tmp) / "sub").mkdir()
        (Path(tmp) / "sub" / "x.tex").write_text("inner")
        (Path(tmp) / "sub" / "up.tex").write_text("\\input{../sub/x}")
        done = subprocess.run(
            ["pdflatex", "-interaction=nonstopmode", "-halt-on-error", "main.tex"], cwd=tmp, capture_output=True,
            text=True, stdin=subprocess.DEVNULL, env={**os.environ, **env}, timeout=120,
        )
        return done.returncode == 0


class SharedBuilds(SharedState):
    """Builds while sharing cannot run code or read files outside the document."""

    def doc(self, build_toml="", magic=""):
        self.tmpdir = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmpdir.cleanup)
        directory = Path(self.tmpdir.name)
        (directory / "main.tex").write_text(magic + "\\documentclass{article}\n", encoding="utf-8")
        if build_toml:
            (directory / "build.toml").write_text(build_toml, encoding="utf-8")
        return directory / "main.tex"

    def test_environment_is_restricted_only_while_sharing_and_restored_after(self):
        before = {k: os.environ.get(k) for k in serve.SHARE_ENV}
        self.share_on("d")
        self.assertEqual({k: os.environ[k] for k in serve.SHARE_ENV}, serve.SHARE_ENV)
        serve.share_disable()
        self.assertEqual({k: os.environ.get(k) for k in serve.SHARE_ENV}, before)

    def test_lualatex_stays_allowed_while_sharing(self):
        # The owner's choice: allowed, with a warning in the Share dialog (app.js).
        main = self.doc(magic="% !TeX program = lualatex\n")
        self.share_on("d")
        settings = serve.build.read_settings(main)
        self.assertEqual(settings["engine"], "lualatex")
        self.assertFalse(settings["shell_escape"])
        self.assertEqual(settings["latexmk_args"][0], "-norc")

    def test_timeout_is_capped_while_sharing(self):
        main = self.doc("timeout = 7000\n")
        self.assertEqual(serve.build.read_settings(main)["timeout"], 7000)
        self.share_on("d")
        self.assertEqual(serve.build.read_settings(main)["timeout"], serve.SHARE_TIMEOUT)
        main.parent.joinpath("build.toml").write_text("timeout = 20\n", encoding="utf-8")
        self.assertEqual(serve.build.read_settings(main)["timeout"], 20)

    def test_latexmk_never_reads_rc_files_while_sharing(self):
        main = self.doc()
        self.assertNotIn("-norc", serve.build.read_settings(main)["latexmk_args"])
        self.share_on("d")
        self.assertEqual(serve.build.read_settings(main)["latexmk_args"][0], "-norc")

    def test_options_that_load_code_or_move_output_are_refused(self):
        for bad in ("-latexoption=-shell-escape", "-latexoption", "-pretex=\\write18{x}", "-usepretex", "-cnf-line=a=b",
                    "-e", "-r", "--r", "-r=x", "-pdflua", "-lualatex", "-pdflatex=lualatex", "-outdir=/tmp",
                    "-auxdir=/tmp", "-jobname=../x", "-pdflatex=x", '-e=$pdflatex="x"', "-SHELL_ESCAPE", "-x-write18"):
            with self.subTest(arg=bad):
                self.assertTrue(serve.unsafe_latexmk_arg(bad))
        for fine in ("-bibtex", "-f", "-silent", "-pdflatex", "-pdfxe", "-g"):
            with self.subTest(arg=fine):
                self.assertFalse(serve.unsafe_latexmk_arg(fine))

    @unittest.skipUnless(shutil.which("pdflatex"), "pdflatex not installed")
    def test_tex_cannot_read_absolute_or_parent_paths_while_sharing(self):
        absolute = "\\input{/etc/hostname}"
        self.share_on("d")
        env = dict(serve.SHARE_ENV)
        self.assertFalse(pdflatex_reads(absolute, **env))
        self.assertTrue(pdflatex_reads("\\input{sub/x}", **env))
        self.assertFalse(pdflatex_reads("\\input{sub/up}", **env))  # "..": blocked on purpose, see README
        target = Path(tempfile.gettempdir()) / f"lp-pwned-{os.getpid()}"
        self.addCleanup(lambda: target.unlink(missing_ok=True))
        pdflatex_reads(f"\\immediate\\openout3={target.as_posix()}\\immediate\\write3{{x}}", **env)
        self.assertFalse(target.exists())


class TunnelLifecycle(SharedState):
    def test_stop_before_start_leaves_no_process(self):
        tunnel = serve.Tunnel("localtunnel", 1)
        tunnel.stop()
        with self.assertRaises(RuntimeError):
            tunnel.start(timeout=1)
        self.assertIsNone(tunnel.proc)

    def test_a_failing_tunnel_start_leaves_the_starting_state(self):
        for exc in (OSError("no such binary"), RuntimeError("exited")):
            with self.subTest(exc=type(exc).__name__):
                with mock.patch.object(serve.Tunnel, "start", side_effect=exc), \
                        mock.patch.object(serve.build, "error"), \
                        mock.patch.object(serve, "pick_provider", return_value="cloudflared"):
                    serve.share_start("auto", "d", 1, wait=True)
                self.assertEqual((serve.SHARE["on"], serve.SHARE["status"]), (False, "off"))
                self.assertIn(str(exc), serve.SHARE["error"])
                self.assertIsNone(serve.SHARE["tunnel"])

    def test_stopping_while_the_tunnel_comes_up_adds_no_host_and_kills_it(self):
        started, release = threading.Event(), threading.Event()
        made = []

        def slow_start(self, timeout=60.0):
            made.append(self)
            started.set()
            release.wait(5)
            return "https://late.example.com"

        with mock.patch.object(serve.Tunnel, "start", slow_start), mock.patch.object(serve.Tunnel, "stop") as stop, \
                mock.patch.object(serve, "pick_provider", return_value="cloudflared"):
            serve.share_start("auto", "d", 1)
            self.assertTrue(started.wait(5))
            self.assertIs(serve.SHARE["tunnel"], made[0])
            serve.share_disable()
            stopped_by_disable = stop.call_count
            release.set()
            deadline = time.monotonic() + 5
            while time.monotonic() < deadline and stop.call_count == stopped_by_disable:
                time.sleep(0.02)
        self.assertEqual(serve.SHARE["hosts"], set())
        self.assertFalse(serve.SHARE["on"])
        self.assertEqual(stop.call_count, stopped_by_disable + 1)  # the late thread stopped it again, harmlessly

    def test_a_new_share_is_not_touched_by_the_previous_ones_thread(self):
        gen = serve.share_enable("d", "local", 1)
        serve.share_disable()
        serve.share_enable("d", "local", 1)
        self.assertNotEqual(serve.SHARE["gen"], gen)


class RoomSecurity(SharedState):
    def setUp(self):
        super().setUp()
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.dir = Path(self.tmp.name)
        for name in ("main.tex", "ch.tex", "build.toml", ".latexmkrc"):
            (self.dir / name).write_text("x = 1\n", encoding="utf-8")
        other = self.dir / "other"
        other.mkdir()
        (other / "main.tex").write_text("other", encoding="utf-8")
        patch = mock.patch.dict(serve.DOCS, {"d": self.dir / "main.tex", "o": other / "main.tex"}, clear=True)
        patch.start()
        self.addCleanup(patch.stop)
        serve.SHARE["doc"] = "d"

    def join(self, cid, role="edit", doc="d", path="main.tex", **extra):
        serve.bind_client(cid, role)
        reply = serve.handle_client_message(
            {"type": "y-join", "data": {"doc": doc, "path": path, "aid": 1, **extra}}, cid, role)
        return reply["data"] if reply["type"] == "y-state" else reply

    def update(self, cid, room, u="AA", role="edit"):
        return serve.handle_client_message({"type": "y-update", "data": {"room": room, "u": u}}, cid, role)

    def test_build_config_rooms_are_for_the_owner_only(self):
        for name in ("build.toml", ".latexmkrc", "latexmkrc", "BUILD.TOML"):
            for role in ("edit", "view"):
                with self.subTest(name=name, role=role):
                    reply = self.join(f"{role}-{name}", role, path=name)
                    self.assertEqual((reply["type"], reply["data"]["status"]), ("error", 403))
        self.assertEqual(self.join("owner", "owner", path="build.toml")["leader"], "owner")
        # Even a room that exists already cannot be written to by an editor
        reply = self.update("e", "d\nbuild.toml")
        self.assertEqual(reply["data"]["status"], 403)
        serve.ROOMS["d\nbuild.toml"]["members"]["e"] = {"aid": 1, "role": "edit"}
        self.assertEqual(self.update("e", "d\nbuild.toml")["data"]["status"], 403)
        self.assertEqual(serve.ROOMS["d\nbuild.toml"]["log"], [])

    def test_viewers_join_only_existing_files_of_the_shared_document(self):
        self.assertEqual(self.join("v", "view", path="ch.tex")["leader"], None)
        self.assertEqual(self.join("v", "view", path="new.tex")["data"]["status"], 404)
        self.assertEqual(self.join("v", "view", doc="o")["data"]["status"], 404)
        self.assertEqual(self.join("e", "edit", path="new.tex")["data"]["status"], 404)  # new files: PUT, not rooms
        self.assertIn("room", self.join("o", "owner", path="new.tex"))

    def test_updates_for_another_document_are_refused_even_for_editors(self):
        self.join("owner", "owner", doc="o")  # the owner works on another document meanwhile
        serve.CLIENTS["e"] = {"role": "edit", "rooms": set(), "seen": time.monotonic(), "name": None,
                              "color": None, "path": None, "doc": None}
        serve.ROOMS["o\nmain.tex"]["members"]["e"] = {"aid": 2, "role": "edit"}
        self.assertEqual(self.update("e", "o\nmain.tex")["data"]["status"], 403)

    def test_bus_messages_of_other_documents_and_config_files_are_hidden_from_shared_roles(self):
        bus = [
            {"rev": 1, "topic": "y", "type": "y-update", "data": {"room": "o\nmain.tex", "u": "A"}},
            {"rev": 2, "topic": "y", "type": "y-update", "data": {"room": "d\nmain.tex", "u": "B"}},
            {"rev": 3, "topic": "y", "type": "y-aware", "data": {"room": "o\nmain.tex", "u": "A"}},
            {"rev": 4, "topic": "y", "type": "y-leader", "data": {"room": "o\nmain.tex", "leader": "x"}},
            {"rev": 5, "topic": "y", "type": "y-gone", "data": {"room": "o\nmain.tex", "cid": "x", "aid": 1}},
            {"rev": 6, "topic": "y", "type": "y-update", "data": {"room": "d\nbuild.toml", "u": "C"}},
            {"rev": 7, "topic": "y", "type": "mystery", "data": {"room": "d\nmain.tex"}},
            {"rev": 8, "topic": "y", "type": "y-aware", "data": {"room": "d\nmain.tex", "u": "D"}},
        ]
        for role in ("view", "edit"):
            self.assertEqual([m["rev"] for m in serve.visible(bus, role)], [2, 8])
        self.assertEqual(len(serve.visible(bus, "owner")), 8)

    def test_presence_shows_only_people_in_the_shared_document(self):
        for cid, role, doc, path in (("a", "edit", "d", "main.tex"), ("me", "owner", "o", "secret/notes.tex"),
                                     ("c", "owner", "d", "build.toml")):
            serve.bind_client(cid, role)
            serve.handle_client_message(
                {"type": "hello", "data": {"name": cid, "color": "#112233", "path": path, "doc": doc}}, cid, role)
        reply = serve.handle_client_message({"type": "hello", "data": {"name": "v", "doc": "d"}}, "v", "view")
        self.assertEqual([u["cid"] for u in reply["data"]["users"]], ["a", "c", "v"])
        self.assertIsNone(next(u for u in reply["data"]["users"] if u["cid"] == "c")["path"])
        (message,) = serve.visible([{"rev": 1, "topic": "sys", "type": "presence", "data": serve.presence()}], "view")
        self.assertEqual([u["cid"] for u in message["data"]["users"]], ["a", "c", "v"])
        self.assertEqual(len(serve.presence()["users"]), 4)  # the owner sees everybody

    def test_file_changed_while_the_room_had_no_leader_is_reported_to_the_next_leader(self):
        first = self.join("a", path="ch.tex")
        self.assertNotIn("stale", first)
        serve.handle_client_message({"type": "y-leave", "data": {"room": "d\nch.tex"}}, "a", "edit")
        (self.dir / "ch.tex").write_text("changed on disk, longer\n", encoding="utf-8")
        taken = self.join("b", path="ch.tex")
        self.assertEqual((taken["stale"], taken["base"], taken["gone"]), (True, "x = 1\n", False))
        leaders = [m["data"] for m in serve.BUS.since(0) if m["type"] == "y-leader" and m["data"]["leader"] == "b"]
        self.assertTrue(leaders[-1]["stale"])
        # Once a save went through, the room is in step again
        serve.note_write("d", "ch.tex", "merged\n", serve.version_of((self.dir / "ch.tex").stat()), "b")
        serve.handle_client_message({"type": "y-leave", "data": {"room": "d\nch.tex"}}, "b", "edit")
        self.assertNotIn("stale", self.join("c", path="ch.tex"))

    def test_a_file_deleted_behind_the_room_is_reported_as_gone(self):
        self.join("a", path="ch.tex")
        serve.handle_client_message({"type": "y-leave", "data": {"room": "d\nch.tex"}}, "a", "edit")
        (self.dir / "ch.tex").unlink()
        self.assertTrue(self.join("b", path="ch.tex")["gone"])

    def test_room_history_and_room_count_are_capped(self):
        self.join("a")
        with mock.patch.object(serve, "MAX_ROOM_BYTES", 10):
            self.assertIsNone(self.update("a", "d\nmain.tex", "x" * 6))
            reply = self.update("a", "d\nmain.tex", "y" * 6)
        self.assertEqual(reply["data"]["status"], 413)
        self.assertEqual(serve.ROOMS["d\nmain.tex"]["log"], ["x" * 6])
        for name in ("n1.tex", "n2.tex"):
            (self.dir / name).write_text("x", encoding="utf-8")
        with mock.patch.object(serve, "MAX_ROOMS_PER_CLIENT", 2):
            self.assertIn("room", self.join("a", path="ch.tex"))
            self.assertEqual(self.join("a", path="n1.tex")["data"]["status"], 429)
            self.assertIn("room", self.join("a", path="ch.tex"))  # rejoining a held room is fine
        with mock.patch.object(serve, "MAX_ROOMS", len(serve.ROOMS)):
            self.assertEqual(self.join("z", path="n2.tex")["data"]["status"], 503)
            self.assertIn("room", self.join("o", "owner", path="n2.tex"))  # the owner is never locked out

    def test_a_link_cannot_open_rooms_by_changing_client_ids_or_for_missing_files(self):
        for i in range(30):
            self.assertEqual(self.join(f"x{i}", path=f"ghost{i}.tex")["data"]["status"], 404)
        self.assertEqual(serve.ROOMS, {})
        for i in range(5):
            (self.dir / f"f{i}.tex").write_text("x", encoding="utf-8")
        with mock.patch.object(serve, "MAX_ROOMS_PER_ROLE", 3):
            for i in range(3):
                self.assertIn("room", self.join(f"y{i}", path=f"f{i}.tex"))
            self.assertEqual(self.join("y9", path="f4.tex")["data"]["status"], 429)
            self.assertIn("room", self.join("v", "view", path="f4.tex"))  # another role has its own budget
        with mock.patch.object(serve, "MAX_BYTES_PER_ROLE", 10):
            self.assertIsNone(self.update("y0", "d\nf0.tex", "x" * 6))
            self.assertEqual(self.update("y1", "d\nf1.tex", "y" * 6)["data"]["status"], 413)

    def test_a_save_from_outside_the_room_leaves_the_room_drifted(self):
        self.join("a", path="ch.tex")
        (self.dir / "ch.tex").write_text("plain tab edit\n", encoding="utf-8")
        serve.note_write("d", "ch.tex", "plain tab edit\n", serve.version_of((self.dir / "ch.tex").stat()), "tab")
        serve.handle_client_message({"type": "y-leave", "data": {"room": "d\nch.tex"}}, "a", "edit")
        taken = self.join("b", path="ch.tex")
        self.assertEqual((taken["stale"], taken["base"]), (True, "x = 1\n"))


class SharedHttpDetails(SharedState, ServerCase):
    def setUp(self):
        SharedState.setUp(self)
        ServerCase.setUp(self)
        other = self.base / "second"
        other.mkdir()
        (other / "main.tex").write_text("x", encoding="utf-8")
        (other / "private.tex").write_text("x", encoding="utf-8")
        mock.patch.dict(serve.DOCS, {"second": other / "main.tex"}).start()
        serve.SHARE["port"] = self.port
        self.tokens = self.share_on()

    def test_forward_does_not_reveal_which_files_exist(self):
        outside = str(self.base / "secret.tex")
        errors = set()
        elsewhere = str(self.base / "second" / "private.tex")
        for name in (outside, "/nonexistent/x.tex", "../secret.tex", "nope.tex", elsewhere):
            _, data = self.request(
                "GET", f"/forward?doc=demo&quiet=1&line=1&file={name.replace('/', '%2F')}",
                headers={"Cookie": f"{serve.cookie_name()}={self.tokens['view']}"})
            errors.add(data["error"].split(":")[0])
        self.assertEqual(errors, {"No such file in the document"})

    def test_handlers_time_out_stalled_sockets(self):
        self.assertTrue(0 < serve.Handler.timeout <= 75)

    def test_a_foreign_origin_on_the_websocket_gets_one_answer(self):
        import socket
        sock = socket.create_connection(("127.0.0.1", self.port), timeout=10)
        sock.sendall((
            f"GET /ws?cid=o1 HTTP/1.1\r\nHost: 127.0.0.1:{self.port}\r\nOrigin: http://evil.example\r\n"
            "Upgrade: websocket\r\nConnection: Upgrade\r\nSec-WebSocket-Key: dGhlIHNhbXBsZSBub25jZQ==\r\n"
            f"Sec-WebSocket-Version: 13\r\nCookie: {serve.cookie_name()}={self.tokens['view']}\r\n\r\n"
        ).encode())
        data = b""
        while chunk := sock.recv(4096):
            data += chunk
        sock.close()
        self.assertEqual(data.count(b"HTTP/1."), 1)
        self.assertIn(b" 403 ", data.split(b"\r\n", 1)[0])


class CollabClient(unittest.TestCase):
    """collab.js decisions that cannot be made server-side, run in node with the Yjs libraries stubbed."""

    @unittest.skipUnless(shutil.which("node"), "node not installed")
    def test_leader_takeover_never_saves_blind_and_never_recreates_deleted_files(self):
        script = (Path(__file__).parent / "collab_check.mjs").resolve()
        result = subprocess.run(
            ["node", str(script), str(UI_DIR / "collab.js")], capture_output=True, text=True, timeout=60,
        )
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)


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


class LogWarnings(ServerCase):
    LOG = (
        "Build of files/demo/main.tex: SUCCESS\n\n===== latexmk output =====\n"
        "LaTeX Warning: Reference `dup' on page 1 undefined on input line 99.\n\n"
        "===== LaTeX log (main.log) =====\nThis is XeTeX, Version 3.14 (TeX Live)\n"
        "(./main.tex (/usr/share/texlive/article.cls (/usr/share/texlive/size10.clo))\n"
        "(./Chapters/one.tex\nLaTeX Warning: Reference `fig:missing' on page 2 undefined on input line 7.\n\n"
        "Overfull \\hbox (31.2pt too wide) in paragraph at lines 12--13\n[]\\OT1/cmr/m/n/10 text\n\n"
        "Underfull \\hbox (badness 10000) in paragraph at lines 20--21\n\n"
        "Package natbib Warning: Citation `knuth' on page 3 undefined on input line 30.\n\n"
        "LaTeX Warning: Reference `fig:missing' on page 2 undefined on input line 7.\n\n)\n"
        "Package hyperref Warning: Token not allowed in a PDF string (Unicode):\n"
        "(hyperref)                removing `math shift' on input line 5.\n\n"
        "LaTeX Warning: Label(s) may have changed. Rerun to get cross-references right.\n\n"
        "Package siunitx Warning: odd thing.\n\n"
        "(/usr/share/texlive/x.sty\nLaTeX Warning: In a system file on input line 4.\n\n))\n"
        "Output written on main.pdf (3 pages, 1234 bytes).\n"
        "\n===== BibTeX log (main.blg) =====\nThis is BibTeX\nWarning--I didn't find a database entry for \"nobody\"\n"
    )

    def setUp(self):
        super().setUp()
        self.write("Chapters/one.tex", "chapter\n")
        self.log = self.root / "main.log"
        self.log.write_text(self.LOG, encoding="utf-8")
        for name in ("log_path_for",):
            mock.patch.object(build, name, return_value=self.log).start()

    def test_parses_warnings_with_files_lines_kinds_and_hints(self):
        found = serve.parse_warnings(self.LOG, self.root)
        by = {(w["kind"], w["line"]): w for w in found}
        self.assertEqual(by[("undefined", 7)]["file"], "Chapters/one.tex")
        self.assertIn("fig:missing", by[("undefined", 7)]["message"])
        self.assertIn("\\label", by[("undefined", 7)]["hint"])
        self.assertEqual((by[("overfull", 12)]["file"], by[("overfull", 12)]["level"]), ("Chapters/one.tex", "warning"))
        self.assertIn("margin", by[("overfull", 12)]["hint"])
        self.assertEqual(by[("underfull", 20)]["level"], "info")
        self.assertEqual(by[("undefined", 30)]["source"], "natbib")
        self.assertEqual(by[("package", 5)]["file"], "main.tex")  # after one.tex closed
        self.assertIn("removing `math shift'", by[("package", 5)]["message"])  # continuation joined, prefix dropped
        self.assertNotIn("(hyperref)", by[("package", 5)]["message"])

    def test_dedupes_and_ignores_the_latexmk_console_copy_and_system_files(self):
        found = serve.parse_warnings(self.LOG, self.root)
        self.assertEqual(sum("fig:missing" in w["message"] for w in found), 1)
        self.assertFalse(any("dup" in w["message"] for w in found))  # only the LaTeX log section counts
        system = next(w for w in found if "system file" in w["message"])
        self.assertEqual((system["file"], system["line"]), (None, None))  # not a file of this document: no link
        rerun = next(w for w in found if w["kind"] == "rerun")
        self.assertEqual((rerun["file"], rerun["line"], rerun["level"]), (None, None, "info"))

    def test_a_paragraph_that_ends_in_the_next_file_is_blamed_on_the_file_it_began_in(self):
        log = ("===== LaTeX log (main.log) =====\n(./main.tex (./Chapters/one.tex) (./Chapters/two.tex\n"
               "Overfull \\hbox (9.0pt too wide) in paragraph at lines 7--1\n[]\n\n)")
        box = serve.parse_warnings(log, self.root)[0]
        self.assertEqual((box["file"], box["line"]), ("Chapters/one.tex", 7))

    def test_bibtex_warnings_are_listed_and_files_cannot_escape_the_document(self):
        found = serve.parse_warnings(self.LOG, self.root)
        bib = [w for w in found if w["kind"] == "bibtex"]
        self.assertEqual(len(bib), 1)
        self.assertIn("nobody", bib[0]["message"])
        self.assertIsNone(serve.doc_relative(self.root, "../secret.tex"))
        self.assertIsNone(serve.doc_relative(self.root, str(self.base / "secret.tex")))
        self.assertEqual(serve.doc_relative(self.root, "./Chapters/one.tex"), "Chapters/one.tex")
        self.assertEqual(serve.doc_relative(self.root, str(self.root / "Chapters" / "one.tex")), "Chapters/one.tex")

    def test_endpoint_and_missing_log(self):
        status, data = self.request("GET", "/api/warnings?doc=demo")
        self.assertEqual(status, 200)
        self.assertTrue(any(w["kind"] == "overfull" for w in data["warnings"]))
        self.log.unlink()
        self.assertEqual(self.request("GET", "/api/warnings?doc=demo")[1], {"warnings": []})
        self.assertEqual(self.request("GET", "/api/warnings?doc=nope")[0], 404)

    def test_remember_build_merges_into_the_report_without_dropping_other_documents(self):
        report = self.root / "report"
        report.mkdir()
        (report / "build-report.json").write_text(json.dumps({"documents": [
            {"name": "other", "seconds": 1}, {"name": "demo", "seconds": 2}]}), encoding="utf-8")
        with mock.patch.object(build, "OUT_DIR", report):
            serve.remember_build({"name": "demo", "seconds": 9.5})
            docs = json.loads((report / "build-report.json").read_text())["documents"]
            names = {d["name"]: d["seconds"] for d in docs}
            self.assertEqual(names, {"other": 1, "demo": 9.5})
            (report / "build-report.json").write_text("garbage", encoding="utf-8")
            serve.remember_build({"name": "demo", "seconds": 3})
            self.assertEqual(json.loads((report / "build-report.json").read_text())["documents"][0]["seconds"], 3)

    def test_saved_result_restores_pages_warnings_and_time_after_a_restart(self):
        pdf = self.root / "main.pdf"
        pdf.write_bytes(b"%PDF")
        report = self.root / "report"
        report.mkdir()
        (report / "build-report.json").write_text(json.dumps({"documents": [
            {"name": "demo", "ok": True, "seconds": 4.5, "engine": "xelatex"}]}), encoding="utf-8")
        with mock.patch.object(build, "output_path_for", return_value=pdf), mock.patch.object(build, "OUT_DIR", report):
            state = serve.fresh_state("demo", self.root / "main.tex")
            self.assertEqual((state["status"], state["pages"], state["warnings"]), ("idle", 3, 7))
            self.assertEqual((state["seconds"], state["engine"]), (4.5, "xelatex"))
            self.assertIsNotNone(state["finished"])
            (report / "build-report.json").write_text("not json", encoding="utf-8")
            self.assertEqual(serve.fresh_state("demo", self.root / "main.tex")["engine"], "xelatex")  # from the log
            self.assertIsNone(serve.fresh_state("demo", self.root / "main.tex")["seconds"])
            self.log.write_text(self.LOG.replace(": SUCCESS", ": FAILED", 1), encoding="utf-8")
            self.assertIsNone(serve.fresh_state("demo", self.root / "main.tex")["pages"])  # a failed build says nothing
            self.log.write_text(self.LOG, encoding="utf-8")
            pdf.unlink()
            self.assertIsNone(serve.fresh_state("demo", self.root / "main.tex")["pages"])  # no PDF, no result


class FileOps(SharedState, ServerCase):
    """New file, new folder, rename and delete in the file tree."""

    def setUp(self):
        SharedState.setUp(self)
        ServerCase.setUp(self)
        self.write("Chapters/one.tex", "one")
        self.write("Chapters/two.tex", "two")
        self.write("refs.bib", "@a{b,}")

    def fs(self, op, path, to=None, method="POST"):
        extra = f"&to={to}" if to is not None else ""
        return self.request(method, f"/api/fs?doc=demo&op={op}&path={path}{extra}")

    def paths(self):
        return sorted(f["path"] for f in serve.list_files(self.root))

    def test_empty_folders_are_listed_so_a_new_folder_shows_up(self):
        self.assertEqual(serve.empty_dirs(self.root), [])
        self.assertEqual(self.fs("mkdir", "Figures")[0], 200)
        self.assertEqual(self.fs("mkdir", "a/b/c")[0], 200)
        (self.root / ".hidden").mkdir()
        self.assertEqual(sorted(serve.empty_dirs(self.root)), ["Figures", "a/b/c"])  # a, a/b hold a folder: not empty
        self.assertEqual(self.request("GET", "/api/files?doc=demo")[1]["dirs"], serve.empty_dirs(self.root))
        try:
            os.symlink(self.base, self.root / "out-link")
        except (OSError, NotImplementedError):
            return
        self.assertNotIn("out-link", serve.empty_dirs(self.root))

    def test_create_files_and_folders(self):
        self.assertEqual(self.fs("newfile", "Chapters/three.tex")[0], 200)
        self.assertEqual((self.root / "Chapters/three.tex").read_text(), "")
        self.assertEqual(self.fs("newfile", "deep/er/new.bib")[0], 200)  # parents are made
        self.assertEqual(self.fs("mkdir", "Figures")[0], 200)
        self.assertTrue((self.root / "Figures").is_dir())
        self.assertEqual(self.fs("newfile", "Chapters/three.tex")[0], 409)  # never overwrites
        self.assertEqual(self.fs("mkdir", "Chapters")[0], 409)
        self.assertEqual(self.fs("newfile", "pic.png")[0], 415)  # only text files
        self.assertEqual(self.fs("newfile", "refs.bib/x.tex")[0], 409)  # a file is in the way
        self.assertEqual((self.root / "refs.bib").read_text(), "@a{b,}")

    def test_bad_names_and_traversal_are_refused_everywhere(self):
        bad = ["../x.tex", "a/../../x.tex", "/etc/x.tex", "a//b.tex", "a\\b.tex", ".hidden.tex", "sub/.git/x.tex",
               ".git/config", "C:/x.tex", "a/b<c>.tex", "name?.tex", "trailing.tex.", "x" * 300 + ".tex",
               "a/" * 9 + "x.tex",
               "node_modules/x.tex"]
        for rel in bad:
            for op in ("newfile", "mkdir", "delete"):
                with self.subTest(op=op, rel=rel):
                    self.assertIn(self.fs(op, rel.replace("\\", "%5C"))[0], (400, 403))
            with self.subTest(op="rename-to", rel=rel):
                self.assertIn(self.fs("rename", "refs.bib", rel.replace("\\", "%5C"))[0], (400, 403))
            with self.subTest(op="rename-from", rel=rel):
                self.assertIn(self.fs("rename", rel.replace("\\", "%5C"), "ok.bib")[0], (400, 403, 404))
        self.assertEqual(self.paths(), ["Chapters/one.tex", "Chapters/two.tex", "fig.png", "main.tex", "refs.bib"])
        self.assertEqual((self.base / "secret.tex").read_text(), "secret")
        self.assertEqual(self.fs("delete", "refs.bib", method="GET")[0], 404)  # only POST is an operation
        self.assertTrue((self.root / "refs.bib").exists())

    def test_rename_moves_files_and_folders(self):
        self.assertEqual(self.fs("rename", "refs.bib", "bib/refs.bib")[0], 404)  # target folder must exist
        self.assertEqual(self.fs("rename", "refs.bib", "literature.bib")[0], 200)
        self.assertEqual(self.fs("rename", "Chapters", "Parts")[0], 200)
        self.assertEqual(self.paths(), ["Parts/one.tex", "Parts/two.tex", "fig.png", "literature.bib", "main.tex"])
        self.assertEqual((self.root / "Parts/one.tex").read_text(), "one")
        self.assertEqual(self.fs("rename", "Parts/one.tex", "Parts/two.tex")[0], 409)  # never overwrites
        self.assertEqual(self.fs("rename", "Parts", "Parts/inner")[0], 400)
        self.assertEqual(self.fs("rename", "Parts", "Parts")[0], 400)
        self.assertEqual(self.fs("rename", "nothing.tex", "x.tex")[0], 404)

    def test_delete_files_and_folders_but_never_main_tex(self):
        self.assertEqual(self.fs("delete", "refs.bib")[0], 200)
        self.assertEqual(self.fs("delete", "Chapters")[0], 200)
        self.assertEqual(self.paths(), ["fig.png", "main.tex"])
        self.assertEqual(self.fs("delete", "main.tex")[0], 409)
        self.assertEqual(self.fs("rename", "main.tex", "start.tex")[0], 409)
        self.assertEqual(self.fs("delete", "refs.bib")[0], 404)
        self.assertEqual(self.fs("explode", "main.tex")[0], 400)
        self.assertTrue((self.root / "main.tex").exists())

    def test_nested_documents_and_the_doc_root_are_protected(self):
        other = self.root / "sub"
        other.mkdir()
        (other / "main.tex").write_text("x", encoding="utf-8")
        with mock.patch.dict(serve.DOCS, {"demo/sub": other / "main.tex"}):
            self.assertEqual(self.fs("delete", "sub")[0], 409)
            self.assertEqual(self.fs("rename", "sub", "sub2")[0], 409)
            self.assertEqual(self.fs("rename", "refs.bib", "sub/refs.bib")[0], 409)
        self.assertTrue((other / "main.tex").exists())
        self.assertFalse((self.root / "sub/refs.bib").exists())

    def test_main_tex_is_found_by_identity_not_by_name(self):
        alias = self.root / "alias"
        try:
            os.symlink(self.root / "main.tex", alias)
        except (OSError, NotImplementedError):
            self.skipTest("symlinks not available")
        self.assertEqual(self.fs("delete", "alias")[0], 409)
        self.assertEqual(self.fs("rename", "alias", "x.tex")[0], 409)
        self.assertTrue((self.root / "main.tex").exists())

    def test_os_errors_become_api_errors_and_rooms_close(self):
        with mock.patch.object(serve.os, "rename", side_effect=PermissionError(13, "Permission denied")):
            self.assertEqual(self.fs("rename", "refs.bib", "r.bib")[0], 500)
        with mock.patch.object(serve.shutil, "rmtree", side_effect=PermissionError(13, "Permission denied")):
            status, body = self.fs("delete", "Chapters")
        self.assertEqual(status, 500)
        self.assertIn("Permission denied", body["error"])

    def test_symlinks_are_removed_themselves_and_never_followed_out_of_the_document(self):
        try:
            os.symlink(self.base / "secret.tex", self.root / "escape.tex")
            os.symlink(self.root / "refs.bib", self.root / "alias.bib")
        except (OSError, NotImplementedError):
            self.skipTest("symlinks not available")
        self.assertEqual(self.fs("delete", "escape.tex")[0], 403)
        self.assertEqual(self.fs("rename", "escape.tex", "mine.tex")[0], 403)
        self.assertEqual((self.base / "secret.tex").read_text(), "secret")
        self.assertEqual(self.fs("delete", "alias.bib")[0], 200)
        self.assertEqual((self.root / "refs.bib").read_text(), "@a{b,}")  # the link went, not its target

    def test_operations_tell_the_browsers_and_close_the_rooms_of_what_is_gone(self):
        def join(path, cid="c1"):
            serve.bind_client(cid, "owner")
            data = {"doc": "demo", "path": path, "aid": 1}
            serve.handle_client_message({"type": "y-join", "data": data}, cid, "owner")

        join("Chapters/one.tex")
        join("Chapters/two.tex", "c2")
        join("refs.bib")
        self.assertEqual(len(serve.ROOMS), 3)
        rev = serve.BUS.rev
        self.assertEqual(self.fs("rename", "Chapters", "Parts")[0], 200)
        self.assertEqual(sorted(r.split("\n")[1] for r in serve.ROOMS), ["refs.bib"])  # the old rooms are gone
        self.assertNotIn("demo\nChapters/one.tex", serve.CLIENTS["c1"]["rooms"])
        messages = serve.BUS.since(rev)
        closed = sorted(m["data"]["room"] for m in messages if m["type"] == "y-closed")
        self.assertEqual(closed, ["demo\nChapters/one.tex", "demo\nChapters/two.tex"])
        fs = next(m for m in messages if m["type"] == "fs")["data"]
        self.assertEqual(fs["doc"], "demo")
        self.assertEqual(sorted(fs["removed"]), ["Chapters/one.tex", "Chapters/two.tex"])
        self.assertEqual(sorted(fs["changed"]), ["Parts/one.tex", "Parts/two.tex"])
        self.assertEqual(self.fs("delete", "refs.bib")[0], 200)
        self.assertEqual(dict(serve.ROOMS), {})
        # A new file under a deleted name starts a fresh room, not the old room's replay.
        self.assertEqual(self.fs("newfile", "refs.bib")[0], 200)
        again = {"type": "y-join", "data": {"doc": "demo", "path": "refs.bib", "aid": 1}}
        self.assertEqual(serve.handle_client_message(again, "c1", "owner")["data"]["updates"], [])

    def test_a_shared_session_never_hears_about_other_documents_closed_rooms(self):
        self.share_on("demo")
        mine = {"rev": 1, "topic": "doc", "type": "y-closed", "data": {"room": "demo\nx.tex"}}
        other = {"rev": 2, "topic": "doc", "type": "y-closed", "data": {"room": "second\nx.tex"}}
        config = {"rev": 3, "topic": "doc", "type": "y-closed", "data": {"room": "demo\nbuild.toml"}}
        self.assertEqual([m["rev"] for m in serve.visible([mine, other, config], "edit")], [1])
        self.assertEqual([m["rev"] for m in serve.visible([mine, other, config], "view")], [1])


class Upload(SharedState, ServerCase):
    PNG = b"\x89PNG\r\n\x1a\n" + b"0" * 20

    def setUp(self):
        SharedState.setUp(self)
        ServerCase.setUp(self)

    def up(self, name, data, ctype="image/png"):
        conn = http.client.HTTPConnection("127.0.0.1", self.port, timeout=10)
        conn.request("POST", f"/api/upload?doc=demo&name={quote(name, safe='%')}", data,
                     {"Host": f"127.0.0.1:{self.port}", "Content-Type": ctype})
        res = conn.getresponse()
        body = json.loads(res.read())
        conn.close()
        return res.status, body

    def test_images_land_in_figures_when_it_exists_and_names_never_collide(self):
        status, body = self.up("plot.png", self.PNG)
        self.assertEqual((status, body["path"]), (200, "plot.png"))  # no Figures/ yet: the document directory
        (self.root / "Figures").mkdir()
        self.assertEqual(self.up("My Plot (1).PNG", self.PNG)[1]["path"], "Figures/My_Plot_1.png")
        self.assertEqual(self.up("My Plot (1).PNG", self.PNG)[1]["path"], "Figures/My_Plot_1-1.png")
        self.assertEqual(self.up("a.jpg", b"\xff\xd8\xff\xe0xx")[0], 200)
        self.assertEqual(self.up("d.pdf", b"%PDF-1.4 x")[0], 200)
        self.assertEqual((self.root / "Figures/My_Plot_1.png").read_bytes(), self.PNG)

    def test_only_real_png_jpg_pdf_and_safe_names(self):
        for name, data, status in (("x.svg", b"<svg onload=alert(1)>", 415), ("x.gif", b"GIF89a", 415),
                                   ("x.tex", b"\\x", 415),
                                   ("x.png", b"<html>", 415), ("x.pdf", self.PNG, 415), ("x.png", b"", 413),
                                   ("noext", self.PNG, 415)):
            with self.subTest(name=name):
                self.assertEqual(self.up(name, data)[0], status)
        with self.assertRaises(serve.ApiError) as big:  # The HTTP handler refuses by Content-Length before reading.
            serve.save_upload("demo", "x.png", self.PNG + b"0" * serve.MAX_UPLOAD)
        self.assertEqual(big.exception.status, 413)
        for name in ("../../evil.png", "..%2F..%2Fevil.png", "a%5C..%5Cevil.png", "%2Fetc%2Fevil.png",
                     ".latexmkrc.png"):
            with self.subTest(name=name):
                status, body = self.up(name, self.PNG)
                self.assertEqual(status, 200)
                self.assertNotIn("..", body["path"])
                self.assertNotIn("/", body["path"])
                self.assertTrue((self.root / body["path"]).is_file())
        self.assertFalse((self.base / "evil.png").exists())
        self.assertEqual(sorted(p.name for p in self.root.iterdir() if p.suffix == ".svg"), [])

    def test_upload_tells_browsers_and_never_overwrites(self):
        rev = serve.BUS.rev
        self.write("plot.png", "mine")
        status, body = self.up("plot.png", self.PNG)
        self.assertEqual(body["path"], "plot-1.png")
        self.assertEqual((self.root / "plot.png").read_text(), "mine")
        fs = [m["data"] for m in serve.BUS.since(rev) if m["type"] == "fs"]
        self.assertEqual(fs[-1]["changed"], ["plot-1.png"])


class FocusPreview(ServerCase):
    """Chapter preview: validation, state, and serving out/<name>.focus.pdf (LaTeX itself is mocked)."""

    def setUp(self):
        super().setUp()
        self.write("Chapters/one.tex", "chapter one\n")
        self.write("notes.txt", "x")
        self.map = self.root / "doc.focusmap"
        self.pdf, self.log = self.root / "demo.focus.pdf", self.root / "demo.focus.log"
        for target, value in (
            (serve, {"focus_map": lambda main: self.map}),
            (build, {"focus_paths": lambda main: (self.pdf, self.log)}),
        ):
            for name, fn in value.items():
                mock.patch.object(target, name, fn).start()
        mock.patch.object(build, "output_path_for", return_value=self.root / "demo.pdf").start()
        mock.patch.dict(serve.STATE, {"demo": {"name": "demo", "focus": None}}).start()
        self.addCleanup(serve.FOCUS_BUSY.clear)

    def tree(self):
        self.map.write_text("", encoding="utf-8")
        mock.patch.object(build.accel, "read_focusmap", return_value=[]).start()
        chapter = lambda entries, rel: "Chapters/one" if "one" in rel else None  # noqa: E731
        mock.patch.object(build.accel, "top_unit", side_effect=chapter).start()

    def test_bad_requests_are_refused_before_any_build_starts(self):
        self.tree()
        with mock.patch.object(serve, "run_focus") as run:
            for rel, status in (("notes.txt", 400), ("../secret.tex", 400), ("Chapters/none.tex", 404),
                                ("main.tex", 409), ("fig.tex", 404), ("", 400)):
                with self.subTest(rel=rel):
                    self.assertEqual(self.request("POST", f"/api/focus?doc=demo&path={rel}")[0], status)
            self.assertEqual(self.request("POST", "/api/focus?doc=nope&path=main.tex")[0], 404)
            self.write("loose.tex", "not input anywhere")
            self.assertEqual(self.request("POST", "/api/focus?doc=demo&path=loose.tex")[0], 409)
            run.assert_not_called()

    def test_a_chapter_starts_once_and_publishes_building_then_the_result(self):
        self.tree()
        gate = threading.Event()
        def slow_build(*args):
            gate.wait(5)
            self.pdf.write_bytes(b"%PDF")
            return True

        with mock.patch.object(build, "build_focus", side_effect=slow_build):
            status, reply = self.request("POST", "/api/focus?doc=demo&path=Chapters/one.tex")
            self.assertEqual(status, 200)
            # The browser tells this preview from an older one by its start time.
            self.assertEqual(serve.STATE["demo"]["focus"]["started"], reply["started"])
            again = self.request("POST", "/api/focus?doc=demo&path=Chapters/one.tex")
            self.assertEqual(again[0], 409)  # already running
            self.assertEqual(serve.STATE["demo"]["focus"]["status"], "building")
            gate.set()
            deadline = time.time() + 5
            while serve.FOCUS_BUSY and time.time() < deadline:
                time.sleep(0.02)
        focus = serve.STATE["demo"]["focus"]
        self.assertEqual((focus["status"], focus["target"], focus["path"]), ("ok", "Chapters/one", "Chapters/one.tex"))
        self.assertTrue(focus["version"])
        status, body = self.request("GET", "/pdf/demo?focus=1")
        self.assertEqual((status, body), (200, b"%PDF"))
        self.assertEqual(self.request("GET", "/pdf/demo")[0], 404)  # the full PDF is a different file

    def test_a_failed_preview_reports_the_first_error_and_frees_the_slot(self):
        self.tree()
        self.log.write_text("Focus build of x (y): FAILED\nfiles/demo/Chapters/one.tex:3: Undefined control sequence\n"
                            "\n===== LaTeX output =====\nnoise\n", encoding="utf-8")
        with mock.patch.object(build, "build_focus", return_value=False):
            serve.FOCUS_BUSY.add("demo")
            serve.run_focus("demo", "Chapters/one.tex")
        focus = serve.STATE["demo"]["focus"]
        self.assertEqual(focus["status"], "failed")
        self.assertIn("Undefined control sequence", focus["error"])
        self.assertEqual(self.request("GET", "/log/demo?focus=1")[1][:11], b"Focus build")
        self.assertNotIn("demo", serve.FOCUS_BUSY)

    def test_without_a_recorded_input_tree_the_full_build_comes_first_and_a_stranger_file_has_no_chapter(self):
        calls = []
        self.pdf.write_bytes(b"%PDF")
        def full_build(*args, **kwargs):
            calls.append((args[2], kwargs))
            self.tree()

        with mock.patch.object(serve, "run_build", side_effect=full_build), \
                mock.patch.object(build, "build_focus", return_value=True) as focus:
            serve.run_focus("demo", "Chapters/one.tex")
            self.assertEqual(calls, [(True, {"record": True})])
            focus.assert_called_once()
            serve.run_focus("demo", "stray.tex")
        self.assertEqual(serve.STATE["demo"]["focus"]["status"], "failed")
        self.assertIn("has no chapter", serve.STATE["demo"]["focus"]["error"])

    def test_a_crash_is_a_failed_preview_not_a_dead_thread(self):
        self.tree()
        with mock.patch.object(build, "build_focus", side_effect=RuntimeError("boom")):
            serve.run_focus("demo", "Chapters/one.tex")
        self.assertIn("boom", serve.STATE["demo"]["focus"]["error"])


class GrammarApi(SharedState, ServerCase):
    """POST /api/grammar: roles, sharing and the public-mode guard. LanguageTool itself is mocked."""

    def setUp(self):
        SharedState.setUp(self)
        ServerCase.setUp(self)
        import grammar
        self.grammar = grammar
        grammar.CACHE.clear()
        saved = dict(serve.GRAMMAR)
        self.addCleanup(lambda: (serve.GRAMMAR.clear(), serve.GRAMMAR.update(saved)))
        serve.GRAMMAR.update(mode="local", url=None, share_public=False)
        serve._PROBES.clear()

        def reply(url, fields, proxy=False):
            self.calls.append((url, proxy))
            text = fields["text"]
            at = text.find("bad")
            return {"matches": [] if at < 0 else [{
                "message": "Bad word", "offset": self.grammar.to_utf16(text, at), "length": 3,
                "replacements": [{"value": "good"}],
                "rule": {"id": "R", "category": {"id": "GRAMMAR"}}}]}

        self.calls = []
        mock.patch.object(grammar, "post_form", side_effect=reply).start()

    def check(self, text="A bad \\emph{word}. \U0001F600 bad", role=None, doc="demo"):
        hdrs = {"Cookie": f"{serve.cookie_name()}={self.tokens[role]}"} if role else {}
        return self.request("POST", f"/api/grammar?doc={doc}", {"text": text}, hdrs)

    def test_findings_are_positioned_in_utf16_units(self):
        status, body = self.check()
        self.assertEqual(status, 200)
        self.assertEqual(body["mode"], "local")
        first = body["findings"][0]
        self.assertEqual((first["from"], first["to"], first["line"], first["col"]), (2, 5, 1, 3))
        self.assertEqual(first["replacements"], ["good"])
        self.assertEqual(self.calls[0], ("http://localhost:8081/v2/check", False))

    def test_astral_text_before_a_finding_shifts_utf16_offsets(self):
        status, body = self.check("\U0001F600 bad")
        self.assertEqual((body["findings"][0]["from"], body["findings"][0]["to"]), (3, 6))

    def test_off_returns_a_hint_and_asks_nobody(self):
        serve.GRAMMAR["mode"] = "off"
        status, body = self.check()
        self.assertEqual((status, body["mode"], body["findings"]), (200, "off", []))
        self.assertEqual(self.calls, [])

    def test_auto_follows_the_probe_and_build_toml(self):
        serve.GRAMMAR["mode"] = "auto"
        with mock.patch.object(self.grammar, "probe", return_value=False):
            self.assertEqual(self.check()[1]["mode"], "off")
        serve._PROBES.clear()
        with mock.patch.object(self.grammar, "probe", return_value=True):
            self.assertEqual(self.check()[1]["mode"], "local")
        self.write("build.toml", 'grammar = "off"\n')
        self.assertEqual(self.check()[1]["mode"], "off")

    def test_public_mode_says_so_and_goes_through_the_proxy_path(self):
        serve.GRAMMAR["mode"] = "public"
        _, body = self.check()
        self.assertIn("languagetool.org", body["notice"])
        self.assertEqual(self.calls[0], (self.grammar.PUBLIC_URL, True))

    def test_bad_input(self):
        self.assertEqual(self.request("POST", "/api/grammar?doc=demo", {"text": 5})[0], 413)
        huge = {"text": "x" * (serve.GRAMMAR_MAX_CHARS + 1)}
        self.assertEqual(self.request("POST", "/api/grammar?doc=demo", huge)[0], 413)
        self.assertEqual(self.request("POST", "/api/grammar?doc=nope", {"text": "x"})[0], 404)
        self.grammar.post_form.side_effect = self.grammar.GrammarError("LanguageTool is not reachable: refused")
        status, body = self.check()
        self.assertEqual(status, 502)
        self.assertIn("not reachable", body["error"])

    def test_view_role_cannot_check_edit_role_can(self):
        self.share_on()
        self.tokens = serve.SHARE["tokens"]
        self.assertEqual(self.check(role="view")[0], 403)
        self.assertEqual(self.check(role="edit")[0], 200)
        self.assertEqual(self.check(role="edit", doc="demo2")[0], 403)  # not the shared document
        self.assertEqual(self.check(role="owner")[0], 200)

    def test_public_is_refused_while_sharing_unless_the_owner_allowed_it(self):
        self.tokens = self.share_on()
        serve.GRAMMAR["mode"] = "public"
        status, body = self.check(role="edit")
        self.assertEqual(status, 403)
        self.assertIn("while sharing", body["error"])
        self.assertEqual(self.check(role="owner")[0], 403)
        self.assertEqual(self.calls, [])
        serve.GRAMMAR["share_public"] = True
        self.assertEqual(self.check(role="edit")[0], 200)
        serve.GRAMMAR["mode"] = "local"  # local mode is not restricted
        serve.GRAMMAR["share_public"] = False
        self.assertEqual(self.check(role="edit")[0], 200)

    def test_build_toml_public_is_also_refused_while_sharing(self):
        self.tokens = self.share_on()
        serve.GRAMMAR["mode"] = "auto"
        self.write("build.toml", 'grammar = "public"\n')
        self.assertEqual(self.check(role="edit")[0], 403)

    def test_settings_are_owner_only_and_validated(self):
        self.tokens = self.share_on()
        body = {"mode": "public", "url": "http://localhost:9", "share_public": True}
        for role in ("view", "edit"):
            self.assertEqual(self.request("POST", "/api/grammar/settings", body,
                                          {"Cookie": f"{serve.cookie_name()}={self.tokens[role]}"})[0], 403)
        self.assertEqual(serve.GRAMMAR["mode"], "local")
        owner = {"Cookie": f"{serve.cookie_name()}={self.tokens['owner']}"}
        status, info = self.request("POST", "/api/grammar/settings", body, owner)
        self.assertEqual((status, info["mode"], info["url"], info["share_public"]),
                         (200, "public", "http://localhost:9", True))
        for bad in ({"mode": "cloud"}, {"mode": "local", "url": "file:///etc/passwd"}, {"url": 3}):
            self.assertEqual(self.request("POST", "/api/grammar/settings", bad, owner)[0], 400)

    def test_config_shows_the_mode_but_the_url_only_to_the_owner(self):
        self.tokens = self.share_on()
        serve.GRAMMAR["url"] = "http://secret-host:1"
        hdr = lambda role: {"Cookie": f"{serve.cookie_name()}={self.tokens[role]}"}  # noqa: E731
        self.assertEqual(self.request("GET", "/api/config", headers=hdr("owner"))[1]["grammar"]["url"], "http://secret-host:1")
        self.assertNotIn("url", self.request("GET", "/api/config", headers=hdr("view"))[1]["grammar"])

    def test_checks_are_rate_limited(self):
        self.assertEqual({self.check()[0] for _ in range(32)}, {200})  # the owner is not limited
        self.tokens = self.share_on()
        serve.RATE.pop("grammar", None)
        codes = [self.check(role="edit")[0] for _ in range(32)]
        self.assertEqual(codes.count(429), 2)

    def test_bus_messages_stay_whitelisted(self):
        """No grammar message type exists: anything unknown is dropped for shared roles."""
        msgs = [{"rev": 1, "topic": "x", "type": "grammar", "data": {"doc": "demo"}}]
        self.share_on()
        self.assertEqual(serve.visible(msgs, "edit"), [])


class UiWiring(unittest.TestCase):
    """No browser here: check that the scripts only reach for elements the page has, and the page stays accessible."""

    def test_every_id_the_script_uses_exists_in_the_page(self):
        page = (UI_DIR / "index.html").read_text(encoding="utf-8")
        ids = set(re.findall(r'\bid="([^"]+)"', page))
        used = set(re.findall(r'\$\("([A-Za-z][\w-]*)"\)', (UI_DIR / "app.js").read_text(encoding="utf-8")))
        # Built at runtime: palette rows, dynamic dialogs bodies and tree rows are created by app.js itself.
        runtime = {"emptyTime"}
        self.assertEqual(sorted(used - ids - runtime), [])

    def test_page_basics_for_screen_readers(self):
        page = (UI_DIR / "index.html").read_text(encoding="utf-8")
        self.assertIn("<h1", page)
        self.assertIn('id="live"', page)  # concise build announcements, not the whole pill
        self.assertIn('lang="en"', page)

    def test_build_state_carries_a_start_time_for_the_progress_bar(self):
        self.assertIn("started", serve.fresh_state("x", build.SOURCE_DIR / "x" / "main.tex"))


if __name__ == "__main__":
    unittest.main()
