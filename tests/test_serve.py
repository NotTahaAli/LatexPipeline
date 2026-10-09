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
                self.assertEqual(serve.build.read_settings(main)["latexmk_args"], [fine])


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
              "/api/outline?doc=demo", "/api/raw?doc=demo&path=fig.png"]
        for path in ok:
            with self.subTest(path=path):
                self.assertEqual(self.get("GET", path, "view")[0], 200)
        _, config = self.get("GET", "/api/config", "view")
        self.assertEqual(config["role"], "view")
        self.assertEqual(self.get("GET", "/api/health", "view")[1]["docs"], ["demo"])  # no 'second'
        denied = [
            ("GET", "/api/files?doc=second"), ("GET", "/api/file?doc=second&path=main.tex"), ("GET", "/pdf/second"),
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
