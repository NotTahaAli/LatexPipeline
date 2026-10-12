"""
The network code itself, end to end over loopback HTTP: ai.post_json / ai.post_stream, zotero.get, bibfix.get_json
and grammar.post_form are NOT mocked here. Each outside service is a stub server on 127.0.0.1 (tests/netstubs.py)
that speaks the provider's documented wire format, and the scripts are pointed at it (ai.API_URL, an operator or
owner base_url, zotero.API/LOCAL, bibfix.API, a LanguageTool grammar_url). Nothing leaves this machine, so these
run in CI.

Optional extras, never in CI:
- LANGUAGETOOL_URL=http://127.0.0.1:8081 with a real LanguageTool server: RealLanguageTool drives /api/grammar.
- LP_LIVE_NET=1: Live makes one real Crossref DOI lookup and one public LanguageTool request (the explicit opt-in
  path, grammar mode "public"), through whatever proxy the environment sets.
"""

from __future__ import annotations

import http.client
import json
import os
import socket
import time
import unittest
import urllib.parse
from unittest import mock

import _support  # noqa: F401 - puts scripts/ on sys.path
import ai
import bibfix
import grammar
import host
import netstubs
import serve
import test_host as th
import test_serve as ts
import zotero

ANTHROPIC_KEY = "sk-ant-stub-" + "k" * 30
OPENAI_KEY = "sk-openai-stub-1234"
EXPLAIN = {"task": "explain", "error": {"message": "Runaway argument?"},
           "files": [{"path": "main.tex", "text": "a \\textbf{x\n", "line": 1}]}
REWRITE = {"task": "rewrite", "selection": {"text": "x"}}
DEAD_PROXY = {"http_proxy": "http://127.0.0.1:9", "HTTP_PROXY": "http://127.0.0.1:9",
              "https_proxy": "http://127.0.0.1:9", "HTTPS_PROXY": "http://127.0.0.1:9", "no_proxy": "", "NO_PROXY": ""}
PROXY_VARS = ("http_proxy", "HTTP_PROXY", "https_proxy", "HTTPS_PROXY", "all_proxy", "ALL_PROXY")


def stub(test: unittest.TestCase, kind, *args, **kwargs):
    server = kind(*args, **kwargs)
    test.addCleanup(server.close)
    return server


def dead_proxy(test: unittest.TestCase) -> None:
    """Every proxy points at a closed port: a loopback request that went through a proxy would fail."""
    mock.patch.dict(os.environ, DEAD_PROXY).start()


def no_proxy(test: unittest.TestCase) -> None:
    """No proxy at all, for stubs that stand in for https services (those requests honour the environment)."""
    patch = mock.patch.dict(os.environ)
    patch.start()
    for name in PROXY_VARS:
        os.environ.pop(name, None)


def ndjson(raw: bytes) -> list[dict]:
    return [json.loads(line) for line in raw.decode().splitlines()]


def stream_post(port: int, path: str, data: dict, headers: dict | None = None):
    conn = http.client.HTTPConnection("127.0.0.1", port, timeout=20)
    conn.request("POST", path, json.dumps(data), {"Host": f"127.0.0.1:{port}", "Content-Type": "application/json",
                                                  **(headers or {})})
    res = conn.getresponse()
    raw = res.read()
    conn.close()
    return res, raw


# ---------------------------------------------------------------------------
# ai.py over HTTP
# ---------------------------------------------------------------------------

class AiBase(unittest.TestCase):
    def setUp(self):
        self.addCleanup(mock.patch.stopall)
        mock.patch.object(ai, "LIMITER", grammar.Limiter(requests=1000, size=10 ** 9)).start()
        dead_proxy(self)
        self.anthropic = stub(self, netstubs.AnthropicStub, ANTHROPIC_KEY)
        mock.patch.object(ai, "API_URL", self.anthropic.url + "/v1/messages").start()
        self.openai = stub(self, netstubs.OpenAiStub, OPENAI_KEY)
        self.base_url = self.openai.url + "/v1"


class AnthropicHttp(AiBase):
    def ask(self, data=None, key=ANTHROPIC_KEY, model="claude-haiku-5-5"):
        return ai.ask(data or EXPLAIN, key=key, model=model)

    def test_a_reply_goes_through_the_real_request_and_checks(self):
        out = self.ask(model="claude-opus-5-5")
        self.assertEqual((out["answer"], out["usage"]), ("Close the brace.", {"input": 100, "output": 20}))
        sent = self.anthropic.requests[0]
        self.assertEqual(sent["headers"]["x-api-key"], ANTHROPIC_KEY)
        self.assertEqual(sent["headers"]["anthropic-version"], ai.API_VERSION)
        self.assertEqual(sent["headers"]["anthropic-beta"], ai.FALLBACK_BETA)  # opus: server-side fallback
        self.assertEqual(sent["headers"]["content-type"], "application/json")
        body = json.loads(sent["body"])
        self.assertEqual((body["model"], body["max_tokens"], body["output_config"]["format"]["type"]),
                         ("claude-opus-5-5", ai.MAX_TOKENS, "json_schema"))
        self.assertNotIn("stream", body)

    def test_errors_and_billed_failures(self):
        free = {"input": 0, "output": 0}  # refused with an HTTP error: nothing billed
        cases = [("http401", 502, "refused the API key", free), ("http429", 429, "too many requests", free),
                 ("http500", 502, "HTTP 500", free), ("http529", 502, "HTTP 529", free),
                 ("refusal", 422, "declined", {"input": 100, "output": 20}),
                 ("max_tokens", 502, "cut off", {"input": 100, "output": 20}),
                 ("notjson", 502, "not the JSON", {"input": 100, "output": 20})]
        for mode, status, words, usage in cases:
            with self.subTest(mode=mode):
                self.anthropic.modes.append(mode)
                with self.assertRaises(ai.AiError) as caught:
                    self.ask()
                self.assertEqual(caught.exception.status, status)
                self.assertIn(words, str(caught.exception))
                self.assertEqual(getattr(caught.exception, "usage", None), usage)  # billed only with a reply
                self.assertNotIn(ANTHROPIC_KEY, str(caught.exception))
        with self.assertRaises(ai.AiError) as caught:
            self.ask(key="sk-ant-wrong-" + "x" * 30)
        self.assertIn("refused the API key", str(caught.exception))

    def test_nothing_listening(self):
        with socket.socket() as sock:
            sock.bind(("127.0.0.1", 0))
            port = sock.getsockname()[1]
        with mock.patch.object(ai, "API_URL", f"http://127.0.0.1:{port}/v1/messages"), \
                self.assertRaises(ai.AiError) as caught:
            self.ask()
        self.assertEqual(caught.exception.status, 502)
        self.assertIn("not reachable", str(caught.exception))

    def test_streaming(self):
        spent = {}
        events = list(ai.ask_stream(REWRITE, key=ANTHROPIC_KEY, model="claude-haiku-5-5", spent=spent))
        self.assertEqual(events[0]["type"], "start")
        self.assertIn({"type": "ping"}, events)
        self.assertEqual("".join(e["text"] for e in events if e["type"] == "delta"), "Close the brace.")
        self.assertEqual(events[-1]["reply"]["text"], "Better text.")
        self.assertEqual(spent, {"input": 100, "output": 20})
        sent = self.anthropic.requests[0]
        self.assertEqual(sent["headers"]["accept"], "text/event-stream")
        self.assertTrue(json.loads(sent["body"])["stream"])

    def test_stream_failures(self):
        for mode, words in (("stream_error", "stopped with an error"), ("stream_cut", "ended early"),
                            ("refusal", "declined"), ("http529", "HTTP 529")):
            with self.subTest(mode=mode):
                self.anthropic.modes.append(mode)
                spent = {}
                with self.assertRaises(ai.AiError) as caught:
                    list(ai.ask_stream(REWRITE, key=ANTHROPIC_KEY, model="claude-haiku-5-5", spent=spent))
                self.assertIn(words, str(caught.exception))
                if mode == "http529":
                    self.assertEqual(spent, {"input": 0, "output": 0})  # refused before a stream: not billed
                elif mode == "refusal":
                    self.assertEqual(spent, {"input": 100, "output": 20})
                else:
                    self.assertEqual(spent, {"input": 100, "output": ai.MAX_TOKENS})  # no final count: a whole reply

    def test_closing_the_stream_closes_the_connection(self):
        self.anthropic.reply_answer = "x" * 3000
        self.anthropic.modes.append("slow")
        events = ai.ask_stream(REWRITE, key=ANTHROPIC_KEY, model="claude-haiku-5-5")
        self.assertEqual(next(events)["type"], "start")
        events.close()
        for _ in range(100):
            if self.anthropic.dropped:
                break
            time.sleep(0.05)
        self.assertEqual(self.anthropic.dropped, 1)  # the stub saw the connection go: the model would stop


class OpenAiHttp(AiBase):
    def ask(self, data=None, key=OPENAI_KEY):
        return ai.ask(data or EXPLAIN, key=key, model="org/model:7b", provider="openai", base_url=self.base_url)

    def test_a_reply_with_and_without_usage(self):
        out = self.ask()
        self.assertEqual((out["answer"], out["usage"]), ("Close the brace.", {"input": 50, "output": 7}))
        sent = self.openai.requests[0]
        self.assertEqual(sent["path"], "/v1/chat/completions")
        self.assertEqual(sent["headers"]["authorization"], f"Bearer {OPENAI_KEY}")
        body = json.loads(sent["body"])
        self.assertEqual(body["max_tokens"], ai.MAX_TOKENS)  # not api.openai.com: the older name
        self.assertEqual(body["response_format"]["type"], "json_schema")
        self.openai.modes.append("nousage")
        self.assertEqual(self.ask()["usage"], {"input": 0, "output": ai.MAX_TOKENS})  # unknown: a whole reply

    def test_a_local_server_without_a_key(self):
        keyless = stub(self, netstubs.OpenAiStub, None)
        out = ai.ask(EXPLAIN, key=None, model="llama3", provider="openai", base_url=keyless.url + "/v1")
        self.assertEqual(out["answer"], "Close the brace.")
        self.assertNotIn("authorization", keyless.requests[0]["headers"])

    def test_errors(self):
        for mode, status, words in (("http401", 502, "refused the API key"), ("http429", 429, "too many"),
                                    ("http500", 502, "HTTP 500"), ("refusal", 422, "declined"),
                                    ("length", 502, "cut off")):
            with self.subTest(mode=mode):
                self.openai.modes.append(mode)
                with self.assertRaises(ai.AiError) as caught:
                    self.ask()
                self.assertEqual(caught.exception.status, status)
                self.assertIn(words, str(caught.exception))
                if mode == "http500":
                    self.assertIn("127.0.0.1", str(caught.exception))  # who answered: the host, never the URL

    def test_streaming(self):
        spent = {}
        events = list(ai.ask_stream(REWRITE, key=OPENAI_KEY, model="m", spent=spent, provider="openai",
                                    base_url=self.base_url))
        self.assertEqual("".join(e["text"] for e in events if e["type"] == "delta"), "Close the brace.")
        self.assertEqual(spent, {"input": 50, "output": 7})
        body = json.loads(self.openai.requests[0]["body"])
        self.assertEqual((body["stream"], body["stream_options"]), (True, {"include_usage": True}))
        for mode, words in (("stream_cut", "ended early"), ("stream_error", "Stub stream error"),
                            ("refusal", "declined")):
            with self.subTest(mode=mode):
                self.openai.modes.append(mode)
                with self.assertRaises(ai.AiError) as caught:
                    list(ai.ask_stream(REWRITE, key=OPENAI_KEY, model="m", provider="openai",
                                       base_url=self.base_url))
                self.assertIn(words, str(caught.exception))


class ServeAi(ts.SharedState, ts.ServerCase):
    """serve.py /api/ai with the owner's settings, streaming included, over real HTTP to the stubs."""

    def setUp(self):
        ts.SharedState.setUp(self)
        ts.ServerCase.setUp(self)
        dead_proxy(self)
        mock.patch.object(ai, "SETTINGS_FILE", self.root / "config" / "ai.json").start()
        mock.patch.dict(ai._ENV_KEYS, {"ANTHROPIC_API_KEY": None, ai.OPENAI_KEY_ENV: None}).start()
        mock.patch.object(ai, "LIMITER", grammar.Limiter(requests=1000, size=10 ** 9)).start()
        mock.patch.dict(serve.AI, {"guest_used": 0}).start()
        mock.patch.object(serve, "ai_sandboxed", return_value=True).start()
        self.anthropic = stub(self, netstubs.AnthropicStub, ANTHROPIC_KEY)
        mock.patch.object(ai, "API_URL", self.anthropic.url + "/v1/messages").start()
        self.openai = stub(self, netstubs.OpenAiStub, OPENAI_KEY)

    def test_anthropic_plain_and_streamed(self):
        self.assertEqual(self.request("POST", "/api/ai?doc=demo", REWRITE)[0], 403)  # off
        status, info = self.request("POST", "/api/ai/settings", {"enabled": True, "key": ANTHROPIC_KEY,
                                                                 "model": "claude-haiku-5-5"})
        self.assertEqual(status, 200, info)
        status, out = self.request("POST", "/api/ai?doc=demo", REWRITE)
        self.assertEqual((status, out["text"], out["usage"]), (200, "Better text.", {"input": 100, "output": 20}))
        res, raw = stream_post(self.port, "/api/ai?doc=demo&stream=1", REWRITE)
        self.assertEqual((res.status, res.getheader("Content-Type")), (200, "application/x-ndjson"))
        lines = ndjson(raw)
        self.assertEqual("".join(e["text"] for e in lines if e["type"] == "delta"), "Close the brace.")
        self.assertEqual(lines[-1]["type"], "done")
        self.assertNotIn(ANTHROPIC_KEY.encode(), raw)
        self.anthropic.modes.append("stream_error")
        res, raw = stream_post(self.port, "/api/ai?doc=demo&stream=1", REWRITE)
        self.assertEqual((res.status, ndjson(raw)[-1]["type"], ndjson(raw)[-1]["status"]), (200, "error", 502))
        self.anthropic.modes.append("http401")
        status, out = self.request("POST", "/api/ai?doc=demo", REWRITE)
        self.assertEqual(status, 502)
        self.assertIn("refused the API key", out["error"])
        self.anthropic.modes.append("http429")
        res, raw = stream_post(self.port, "/api/ai?doc=demo&stream=1", REWRITE)
        self.assertEqual(res.status, 429)  # before the stream opened: a plain HTTP error
        self.assertEqual({r["headers"]["x-api-key"] for r in self.anthropic.requests}, {ANTHROPIC_KEY})

    def test_a_local_openai_compatible_server_is_for_the_owner_only(self):
        status, info = self.request("POST", "/api/ai/settings", {
            "enabled": True, "share": True, "provider": "openai", "openai_model": "llama3:8b",
            "base_url": self.openai.url + "/v1/", "openai_key": OPENAI_KEY})
        self.assertEqual(status, 200, info)
        status, out = self.request("POST", "/api/ai?doc=demo", EXPLAIN)
        self.assertEqual((status, out["answer"]), (200, "Close the brace."))
        res, raw = stream_post(self.port, "/api/ai?doc=demo&stream=1", REWRITE)
        self.assertEqual(ndjson(raw)[-1]["reply"]["answer"], "Close the brace.")
        self.assertEqual(len(self.openai.requests), 2)
        tokens = self.share_on()
        guest = {"Cookie": f"{serve.cookie_name()}={tokens['edit']}"}
        status, out = self.request("POST", "/api/ai?doc=demo", REWRITE, guest)
        self.assertEqual(status, 403, out)  # a model server on this computer never serves a share link
        self.assertEqual(len(self.openai.requests), 2)


class HostedAi(th.HostCase):
    """host.py /p/<id>/api/ai with the operator's key: quotas, billing and streams over real HTTP to the stubs."""

    AI = {"enabled": True, "api_key": ANTHROPIC_KEY, "daily_per_user": 4, "daily_per_workspace": 100}

    def extra_config(self):
        return {"ai": dict(self.AI)}

    def setUp(self):
        super().setUp()
        dead_proxy(self)
        self.tenant = host.create_tenant(self.app, "Team")
        self.user("ed@x.org", "editor", self.tenant)
        self.pid = th.Workspaces.make_project(self, self.client("ed@x.org"), self.tenant, "Doc", "article")
        mock.patch.object(self.app.workers, "acquire", side_effect=AssertionError("no worker for AI")).start()
        mock.patch.object(ai, "LIMITER", grammar.Limiter(requests=1000, size=10 ** 9)).start()
        self.anthropic = stub(self, netstubs.AnthropicStub, ANTHROPIC_KEY)
        mock.patch.object(ai, "API_URL", self.anthropic.url + "/v1/messages").start()
        self.editor = self.client("ed@x.org")

    def usage(self):
        row = self.app.db.one("SELECT requests, input_tokens, output_tokens FROM ai_usage")
        return (row["requests"], row["input_tokens"], row["output_tokens"]) if row else None

    def ask(self, data=None, stream=False):
        return self.editor.call("POST", f"/p/{self.pid}/api/ai" + ("?stream=1" if stream else ""), data or EXPLAIN)

    def test_quota_billing_and_streams(self):
        self.assertEqual(self.ask()[0], 200)
        self.assertEqual(self.usage(), (1, 100, 20))
        self.anthropic.modes.append("http500")  # not billed: given back
        self.assertEqual(self.ask()[0], 502)
        self.assertEqual(self.usage(), (1, 100, 20))
        self.anthropic.modes.append("refusal")  # billed
        self.assertEqual(self.ask()[0], 422)
        self.assertEqual(self.usage(), (2, 200, 40))
        status, raw, res = self.ask(REWRITE, stream=True)
        self.assertEqual((status, res.getheader("Content-Type")), (200, "application/x-ndjson"))
        self.assertEqual(ndjson(raw)[-1]["reply"]["text"], "Better text.")
        self.assertEqual(self.usage(), (3, 300, 60))
        self.anthropic.modes.append("stream_cut")  # no final count: a whole reply is charged
        status, raw, _ = self.ask(REWRITE, stream=True)
        self.assertEqual((status, ndjson(raw)[-1]["type"]), (200, "error"))
        self.assertEqual(self.usage(), (4, 400, 60 + ai.MAX_TOKENS))
        self.assertEqual(self.ask()[0], 429)  # daily_per_user
        self.assertEqual(len(self.anthropic.requests), 5)
        self.assertEqual({r["headers"]["x-api-key"] for r in self.anthropic.requests}, {ANTHROPIC_KEY})

    def test_an_openai_compatible_endpoint_set_by_the_operator(self):
        openai = stub(self, netstubs.OpenAiStub, OPENAI_KEY)
        self.app.config["ai"] = {**self.AI, "provider": "openai", "model": "m-large", "api_key": OPENAI_KEY,
                                 "base_url": openai.url + "/v1"}
        status, out, _ = self.ask()
        self.assertEqual((status, out["answer"]), (200, "Close the brace."))
        openai.modes.append("nousage")
        self.assertEqual(self.ask()[0], 200)
        self.assertEqual(self.usage(), (2, 50, 7 + ai.MAX_TOKENS))
        status, raw, _ = self.ask(REWRITE, stream=True)
        self.assertEqual(ndjson(raw)[-1]["type"], "done")
        self.assertEqual(self.usage(), (3, 100, 14 + ai.MAX_TOKENS))
        self.assertEqual(openai.requests[0]["headers"]["authorization"], f"Bearer {OPENAI_KEY}")


# ---------------------------------------------------------------------------
# Zotero web API and Better BibTeX
# ---------------------------------------------------------------------------

ZKEY = "ZoteroStubKey12345"


def zentry(i: int) -> str:
    return f"@article{{item{i},\n  title = {{Paper number {i} on a topic}},\n  author = {{Writer, A.}},\n" \
           f"  year = {{2020}},\n  doi = {{10.1000/z{i}}}\n}}\n"


class ZoteroHttp(unittest.TestCase):
    def setUp(self):
        self.addCleanup(mock.patch.stopall)
        no_proxy(self)
        self.web = stub(self, netstubs.ZoteroStub, ZKEY, [zentry(i) for i in range(250)])
        mock.patch.object(zotero, "API", self.web.url).start()
        mock.patch.object(zotero, "LOCAL", self.web.url + "/better-bibtex/export/").start()
        mock.patch.object(zotero, "LIMITER", grammar.Limiter(requests=1000, size=10 ** 9)).start()
        zotero.CACHE.clear()
        self.cfg = {"mode": "web", "library_type": "users", "library_id": "12345", "collection": "",
                    "format": "bibtex", "key": ZKEY}

    def test_paged_fetch_then_not_modified(self):
        text, version, cached = zotero.fetch(self.cfg, True)
        self.assertEqual((version, cached, text.count("@article")), ("7", False, 250))
        self.assertEqual([r["query"].split("start=")[1] for r in self.web.requests], ["0", "100", "200"])
        self.assertEqual(self.web.requests[0]["headers"]["zotero-api-key"], ZKEY)
        self.assertNotIn("If-Modified-Since-Version".lower(), self.web.requests[0]["headers"])
        again = zotero.fetch(self.cfg, True)
        self.assertEqual(again, (text, "7", True))  # 304: the cached text
        self.assertEqual(self.web.requests[-1]["headers"]["if-modified-since-version"], "7")
        self.assertEqual(len(self.web.requests), 4)
        self.web.version = 8
        self.web.entries.append(zentry(999))
        self.assertIn("item999", zotero.fetch(self.cfg, True)[0])
        collection = zotero.fetch({**self.cfg, "collection": "ABCD2345"}, True)
        self.assertIn("/collections/ABCD2345/items/top", self.web.requests[-1]["path"])
        self.assertFalse(collection[2])

    def test_errors_name_neither_the_url_nor_the_key(self):
        for mode, words, status in (("http429", "wait 5 s", 429), ("http503", "try again in 9 s", 429)):
            self.web.modes.append(mode)
            with self.assertRaises(zotero.ZoteroError) as caught:
                zotero.fetch(self.cfg, True)
            self.assertIn(words, str(caught.exception))
            self.assertEqual(caught.exception.status, status)
        with self.assertRaises(zotero.ZoteroError) as caught:
            zotero.fetch({**self.cfg, "key": "WrongKey123456789"}, True)
        self.assertIn("refused the API key", str(caught.exception))
        with self.assertRaises(zotero.ZoteroError) as caught:
            zotero.fetch({**self.cfg, "library_id": "999"}, True)
        self.assertIn("does not know this library", str(caught.exception))
        for exc in (caught.exception,):
            self.assertNotIn(self.web.url, str(exc))
            self.assertNotIn("WrongKey", str(exc))

    def test_better_bibtex_pull_export(self):
        dead_proxy(self)  # 127.0.0.1:23119 is never reached through a proxy
        text, version, cached = zotero.fetch({**self.cfg, "mode": "local", "key": ""}, True)
        self.assertEqual(text.count("@article"), 250)
        sent = self.web.requests[-1]
        self.assertEqual((sent["path"], sent["query"]), ("/better-bibtex/export/library", "/1/library.bibtex"))
        self.assertNotIn("zotero-api-key", sent["headers"])
        with self.assertRaises(zotero.ZoteroError):
            zotero.fetch({**self.cfg, "mode": "local"}, False)  # off while sharing


class ServeZotero(ts.SharedState, ts.ServerCase):
    def setUp(self):
        ts.SharedState.setUp(self)
        ts.ServerCase.setUp(self)
        no_proxy(self)
        self.web = stub(self, netstubs.ZoteroStub, ZKEY, [zentry(1), zentry(2)])
        mock.patch.object(zotero, "API", self.web.url).start()
        mock.patch.object(zotero, "config_path", return_value=self.root / "config" / "zotero.json").start()
        mock.patch.dict(zotero._ENV, {"key": ""}).start()
        zotero.CACHE.clear()

    def test_owner_preview_and_apply(self):
        status, info = self.request("POST", "/api/zotero/settings", {"library_type": "users",
                                                                    "library_id": "12345", "key": ZKEY})
        self.assertEqual(status, 200, info)
        self.assertNotIn(ZKEY, json.dumps(info))
        local = zentry(1).replace("Paper number 1", "Old title")
        status, out = self.request("POST", "/api/zotero/preview?doc=demo", {"text": local, "taken": ["item1"]})
        self.assertEqual(status, 200, out)
        self.assertEqual(out["source"], "web")
        self.assertEqual(len(self.web.requests), 1)
        self.assertNotIn(ZKEY, json.dumps(out))


class HostedZotero(th.HostCase):
    def setUp(self):
        super().setUp()
        no_proxy(self)
        self.tenant = host.create_tenant(self.app, "Team")
        self.user("ed@x.org", "editor", self.tenant)
        self.pid = th.Workspaces.make_project(self, self.client("ed@x.org"), self.tenant, "Doc", "article")
        mock.patch.object(self.app.workers, "acquire", side_effect=AssertionError("no worker for Zotero")).start()
        self.web = stub(self, netstubs.ZoteroStub, ZKEY, [zentry(i) for i in range(3)], library="users/42")
        mock.patch.object(host.zotero, "API", self.web.url).start()
        host.zotero.CACHE.clear()

    def test_the_gateway_fetches_with_the_persons_key(self):
        editor = self.client("ed@x.org")
        status, _, _ = editor.call("POST", "/api/account/zotero", {"library_type": "users", "library_id": "42",
                                                                   "format": "bibtex", "key": ZKEY})
        self.assertEqual(status, 200)
        status, out, _ = editor.call("POST", f"/p/{self.pid}/api/zotero/fetch?doc=Doc")
        self.assertEqual(status, 200, out)
        self.assertEqual((out["text"].count("@article"), out["version"], out["cached"]), (3, "7", False))
        self.assertEqual(self.web.requests[0]["headers"]["zotero-api-key"], ZKEY)
        status, out, _ = editor.call("POST", f"/p/{self.pid}/api/zotero/fetch?doc=Doc")
        self.assertTrue(out["cached"])


# ---------------------------------------------------------------------------
# Crossref
# ---------------------------------------------------------------------------

WORK = {"DOI": "10.1000/xyz123", "type": "journal-article", "title": ["Deep Stub Learning"],
        "author": [{"given": "Ada", "family": "Lovelace"}, {"given": "Alan", "family": "Turing"}],
        "container-title": ["Journal of Stubs"], "volume": "12", "issue": "3", "page": "45-67",
        "published": {"date-parts": [[2021, 5]]}, "publisher": "Stub Press"}


class CrossrefHttp(unittest.TestCase):
    def setUp(self):
        self.addCleanup(mock.patch.stopall)
        no_proxy(self)
        self.crossref = stub(self, netstubs.CrossrefStub, {"10.1000/xyz123": WORK})
        mock.patch.object(bibfix, "API", self.crossref.url + "/works").start()
        mock.patch.object(bibfix, "LIMITER", grammar.Limiter(requests=1000, size=10 ** 9)).start()
        bibfix.CACHE.clear()
        self.addCleanup(bibfix.CACHE.clear)

    def test_doi_lookup_search_and_cache(self):
        found = bibfix.entry_for_doi("https://doi.org/10.1000/XYZ123")
        self.assertEqual(found["type"], "article")
        self.assertEqual(found["fields"]["title"], "Deep Stub Learning")
        self.assertIn("Lovelace", found["fields"]["author"])
        agent = self.crossref.requests[0]["headers"]["user-agent"]
        self.assertNotIn("@", agent)  # no e-mail address leaves the machine
        self.assertEqual(self.crossref.requests[0]["headers"]["accept"], "application/json")
        bibfix.entry_for_doi("10.1000/XYZ123")
        self.assertEqual(len(self.crossref.requests), 1)  # cached
        hits = bibfix.search("deep stub learning")
        self.assertEqual(hits[0]["fields"]["title"], "Deep Stub Learning")
        with self.assertRaises(bibfix.BibLookupError):
            bibfix.entry_for_doi("10.1000/unknown")
        self.crossref.modes.append("http500")
        with self.assertRaises(bibfix.BibLookupError) as caught:
            bibfix.entry_for_doi("10.1000/other")
        self.assertIn("HTTP 500", str(caught.exception))

    def test_serve_lookup_returns_an_edit_and_writes_nothing(self):
        bib = "@article{lov21,\n  title = {Deep Stub Learning},\n  doi = {10.1000/xyz123}\n}\n"
        out = serve.bib_lookup({"text": bib, "key": "lov21"})
        self.assertNotIn("error", out)
        self.assertIn("journal", out["fields"])
        self.assertEqual(serve.bib_search({"query": "stub learning"})["candidates"][0]["fields"]["year"], "2021")


# ---------------------------------------------------------------------------
# LanguageTool
# ---------------------------------------------------------------------------

class GrammarHttp(ts.SharedState, ts.ServerCase):
    def setUp(self):
        ts.SharedState.setUp(self)
        ts.ServerCase.setUp(self)
        dead_proxy(self)  # a local server is never reached through a proxy
        grammar.CACHE.clear()
        self.addCleanup(grammar.CACHE.clear)
        self.lt = stub(self, netstubs.LanguageToolStub)
        mock.patch.dict(serve.GRAMMAR, {"mode": "local", "url": self.lt.url, "share_public": False}).start()

    def test_check_through_the_editor_endpoint(self):
        text = "\\section{A}\nThis are \\emph{fine}.\n"
        status, out = self.request("POST", "/api/grammar?doc=demo", {"text": text})
        self.assertEqual(status, 200, out)
        self.assertEqual(out["mode"], "local")
        finding = out["findings"][0]
        self.assertEqual((finding["line"], finding["rule"], finding["replacements"]), (2, "THIS_NNS_VB", ["is"]))
        self.assertEqual(text[finding["from"]:finding["to"]], "are")
        form = dict(urllib.parse.parse_qsl(self.lt.requests[0]["body"].decode()))
        self.assertEqual(form["language"], "en-US")
        self.assertNotIn("section", form["text"])  # LaTeX markup stays here
        self.assertEqual(self.request("POST", "/api/grammar?doc=demo", {"text": text})[0], 200)
        self.assertEqual(len(self.lt.requests), 1)  # paragraph cache
        self.assertTrue(grammar.probe(self.lt.url))


@unittest.skipUnless(os.environ.get("LANGUAGETOOL_URL"), "set LANGUAGETOOL_URL to a running LanguageTool server")
class RealLanguageTool(ts.SharedState, ts.ServerCase):
    """The editor endpoint against a real LanguageTool server on this machine (scripts never start one)."""

    def setUp(self):
        ts.SharedState.setUp(self)
        ts.ServerCase.setUp(self)

    def test_editor_endpoint(self):
        grammar.CACHE.clear()
        mock.patch.dict(serve.GRAMMAR, {"mode": "local", "url": os.environ["LANGUAGETOOL_URL"]}).start()
        text = "This are a \\emph{test} of the $x$ grammer.\n"
        status, out = self.request("POST", "/api/grammar?doc=demo", {"text": text})
        self.assertEqual(status, 200, out)
        spans = {text[f["from"]:f["to"]] for f in out["findings"]}
        self.assertIn("are", spans)
        self.assertIn("grammer", spans)


# ---------------------------------------------------------------------------
# Live (opt-in, never in CI)
# ---------------------------------------------------------------------------

@unittest.skipUnless(os.environ.get("LP_LIVE_NET") == "1", "set LP_LIVE_NET=1 for live Crossref and LanguageTool")
class Live(unittest.TestCase):
    def test_crossref_doi(self):
        bibfix.CACHE.clear()
        found = bibfix.entry_for_doi("10.1038/nature14539")  # LeCun, Bengio, Hinton: Deep learning (Nature 2015)
        self.assertEqual(found["type"], "article")
        self.assertIn("LeCun", found["fields"]["author"])
        self.assertEqual(found["fields"]["year"], "2015")

    def test_public_languagetool_through_the_opt_in_path(self):
        grammar.CACHE.clear()
        mode, url = grammar.resolve("public")  # never chosen by "auto"
        self.assertEqual((mode, url), ("public", grammar.PUBLIC_URL))
        found = grammar.check_source("This are a test.\n", "m.tex", url=url, public=True, lang="en-US", max_wait=60)
        self.assertTrue(any(f.text == "are" or "are" in f.text for f in found), found)


if __name__ == "__main__":
    unittest.main()
