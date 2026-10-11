"""ai.py beyond the single-file tasks: OpenAI-compatible endpoints, whole-project questions, the claims scan and
images. The network is mocked (ai.post_json, ai.post_stream); nothing here reaches a model."""

from __future__ import annotations

import base64
import json
import os
import stat
import unittest
from unittest import mock

import _support  # noqa: F401 - puts scripts/ on sys.path
import ai
import grammar
import serve
import test_serve as ts

PNG = base64.b64encode(b"\x89PNG\r\n\x1a\n" + b"\0" * 64).decode()
OPENAI = {"provider": "openai", "model": "gpt-test", "base_url": "https://api.openai.com/v1", "key": "sk-other-1234"}


def chat(answer="Fine.", text="", stop="stop", usage=True, refusal=None, payload=None):
    content = payload if payload is not None else json.dumps({"answer": answer, "text": text, "edits": []})
    out = {"model": "gpt-test", "choices": [{"index": 0, "finish_reason": stop,
                                             "message": {"role": "assistant", "content": content, "refusal": refusal}}]}
    if usage:
        out["usage"] = {"prompt_tokens": 50, "completion_tokens": 7, "total_tokens": 57}
    return out


def chunks(answer="Hi \"there\".", done=True, usage=True, size=9):
    payload = json.dumps({"answer": answer, "text": "", "edits": []})
    items = [{"model": "gpt-test", "choices": [{"index": 0, "delta": {"role": "assistant", "content": ""}}]}]
    items += [{"model": "gpt-test", "choices": [{"index": 0, "delta": {"content": payload[i:i + 4]}}]}
              for i in range(0, len(payload), 4)]
    items.append({"model": "gpt-test", "choices": [{"index": 0, "delta": {}, "finish_reason": "stop"}]})
    if usage:
        items.append({"model": "gpt-test", "choices": [], "usage": {"prompt_tokens": 40, "completion_tokens": 9}})
    raw = "".join(f"data: {json.dumps(i)}\n\n" for i in items) + ("data: [DONE]\n\n" if done else "")
    raw = raw.encode()
    return [raw[i:i + size] for i in range(0, len(raw), size)]


class Limited(unittest.TestCase):
    def setUp(self):
        mock.patch.object(ai, "LIMITER", grammar.Limiter(requests=1000, size=10 ** 9)).start()
        self.addCleanup(mock.patch.stopall)


class BaseUrls(unittest.TestCase):
    def test_https_anywhere_http_only_to_this_computer_or_network(self):
        self.assertEqual(ai.check_base_url("https://api.openai.com/v1/"), "https://api.openai.com/v1")
        self.assertEqual(ai.check_base_url("http://localhost:11434/v1"), "http://localhost:11434/v1")
        self.assertEqual(ai.check_base_url("http://192.168.1.20:8080/v1"), "http://192.168.1.20:8080/v1")
        for bad in ("http://example.com/v1", "ftp://x/v1", "https://user:pw@example.com/v1", "https://x/v1?a=1",
                    "https://x/v1#f", "https://x:99999/v1", "", 5, "https://x/a b"):
            with self.subTest(bad=bad), self.assertRaises(ai.AiError):
                ai.check_base_url(bad)
        self.assertTrue(ai.is_local_url("http://127.0.0.1:1234/v1"))
        self.assertTrue(ai.is_local_url("http://[::1]:1234/v1"))
        self.assertFalse(ai.is_local_url("https://api.openai.com/v1"))
        self.assertEqual(ai.notice("anthropic"), ai.NOTICE)
        self.assertIn("localhost:11434", ai.notice("openai", "http://localhost:11434/v1"))

    def test_models_and_keys(self):
        self.assertEqual(ai.check_model(" org/model:7b ", "openai"), "org/model:7b")
        with self.assertRaises(ai.AiError):
            ai.check_model("gpt-test", "anthropic")
        with self.assertRaises(ai.AiError):
            ai.check_model("bad model", "openai")
        with self.assertRaises(ai.AiError):
            ai.check_other_key("has space in it")


class OpenAiCompatible(Limited):
    def test_request_shape_and_reply(self):
        calls = []
        mock.patch.object(ai, "post_json", side_effect=lambda url, body, headers: calls.append((url, body, headers))
                          or chat(text="x")).start()
        out = ai.ask({"task": "rewrite", "selection": {"text": "teh text"}}, **OPENAI)
        self.assertEqual((out["text"], out["usage"]), ("x", {"input": 50, "output": 7}))
        url, body, headers = calls[0]
        self.assertEqual(url, "https://api.openai.com/v1/chat/completions")
        self.assertEqual(headers, {"Authorization": "Bearer sk-other-1234"})
        self.assertEqual(body["max_completion_tokens"], ai.MAX_TOKENS)
        self.assertNotIn("max_tokens", body)
        self.assertEqual(body["response_format"]["json_schema"]["strict"], True)
        self.assertEqual([m["role"] for m in body["messages"]], ["system", "user"])
        self.assertNotIn("sk-other", json.dumps(body))
        # Another endpoint (Ollama's compatibility layer knows only max_tokens), with no key at all.
        ai.ask({"task": "rewrite", "selection": {"text": "x"}}, provider="openai", model="m",
               base_url="http://localhost:11434/v1", key=None)
        url, body, headers = calls[1]
        self.assertEqual((url, headers, body["max_tokens"]), ("http://localhost:11434/v1/chat/completions", {},
                                                              ai.MAX_TOKENS))

    def test_refusals_cut_offs_and_unknown_usage(self):
        for reply, status in ((chat(refusal="No."), 422), (chat(stop="length"), 502),
                              (chat(stop="content_filter"), 422), (chat(payload="not json"), 502)):
            with self.subTest(reply=reply), mock.patch.object(ai, "post_json", return_value=reply):
                with self.assertRaises(ai.AiError) as caught:
                    ai.ask({"task": "rewrite", "selection": {"text": "x"}}, **OPENAI)
                self.assertEqual(caught.exception.status, status)
        with mock.patch.object(ai, "post_json", return_value=chat(usage=False)):
            out = ai.ask({"task": "rewrite", "selection": {"text": "x"}}, **OPENAI)
        self.assertEqual(out["usage"], {"input": 0, "output": ai.MAX_TOKENS})  # never undercounted

    def stream(self, raw):
        calls = []

        def post_stream(url, body, headers):
            calls.append(body)
            yield from raw
        mock.patch.object(ai, "post_stream", side_effect=post_stream).start()
        spent, out = {}, []
        try:
            for event in ai.ask_stream({"task": "rewrite", "selection": {"text": "x"}}, spent=spent, **OPENAI):
                out.append(event)
        except ai.AiError as exc:
            out.append(exc)
        return out, spent, calls

    def test_streaming(self):
        out, spent, calls = self.stream(chunks())
        self.assertEqual(calls[0]["stream_options"], {"include_usage": True})
        self.assertTrue(calls[0]["stream"])
        self.assertEqual(out[0]["type"], "start")
        self.assertEqual("".join(e["text"] for e in out if e.get("type") == "delta"), 'Hi "there".')
        self.assertEqual(out[-1]["reply"]["answer"], 'Hi "there".')
        self.assertEqual(spent, {"input": 40, "output": 9})
        out, spent, _ = self.stream(chunks(usage=False))
        self.assertEqual((out[-1]["type"], spent["output"]), ("done", ai.MAX_TOKENS))
        out, spent, _ = self.stream(chunks(done=False))
        self.assertIsInstance(out[-1], ai.AiError)
        self.assertEqual(spent["output"], ai.MAX_TOKENS)
        error = [b'data: {"error": {"message": "overloaded"}}\n\n']
        self.assertIsInstance(self.stream(error)[0][-1], ai.AiError)


class Tasks(Limited):
    FILES = [{"path": "main.tex", "text": "\\input{ch1}\n\\input{ch2}\n"},
             {"path": "ch1.tex", "text": "Bananas are yellow.\n" * 3},
             {"path": "ch2.tex", "text": "The turbine efficiency reached 41 percent in the wind tunnel.\n"}]

    def test_project_questions_send_the_best_passages_only(self):
        found = ai.passages("What turbine efficiency did the tunnel show?", self.FILES[1:])
        self.assertEqual([p["path"] for p in found], ["ch2.tex"])
        self.assertEqual(ai.passages("the and", self.FILES), [])
        task, prompt, files = ai.build_prompt({"task": "ask", "project": True, "prompt": "turbine efficiency?",
                                               "files": self.FILES})
        self.assertIn('<passage path="ch2.tex" lines="1-2">', prompt)
        self.assertNotIn("Bananas", prompt)
        self.assertEqual([f["path"] for f in files], ["main.tex"])
        with self.assertRaises(ai.AiError):  # without "project", one file as before
            ai.build_prompt({"task": "ask", "prompt": "q", "files": self.FILES * 2})
        with self.assertRaises(ai.AiError):  # never build configuration
            ai.build_prompt({"task": "ask", "project": True, "prompt": "q",
                             "files": [*self.FILES, {"path": "build.toml", "text": ""}]})

    def test_claims_are_placed_in_the_file_and_name_no_reference(self):
        text = "\U0001F600 Most bridges fail from fatigue. We show X.\n"
        payload = json.dumps({"answer": "One claim.", "claims": [
            {"quote": "Most bridges fail from fatigue", "reason": "A general finding.", "query": "bridge\nfatigue"},
            {"quote": "not in the file", "reason": "r", "query": "q"}, {"quote": 5}]})
        reply = {"model": "m", "stop_reason": "end_turn", "usage": {}, "content": [{"type": "text", "text": payload}]}
        with mock.patch.object(ai, "post_json", return_value=reply) as post:
            out = ai.ask({"task": "claims", "files": [{"path": "main.tex", "text": text}]}, key="k" * 30,
                         model="claude-opus-5-5")
        self.assertEqual(post.call_args[0][1]["output_config"]["format"]["schema"], ai.CLAIMS_SCHEMA)
        self.assertEqual(len(out["claims"]), 1)
        claim = out["claims"][0]
        self.assertEqual((claim["from"], claim["to"], claim["line"], claim["query"]), (3, 33, 1, "bridge fatigue"))
        self.assertEqual(len(out["notes"]), 2)

    def test_images(self):
        data = {"task": "image", "image": {"media_type": "image/png", "data": PNG}, "selection": {}}
        calls = []
        latex = json.dumps({"answer": "", "text": "\\[ x^2 \\]", "edits": []})
        mock.patch.object(ai, "post_json", side_effect=lambda url, body, headers: calls.append(body) or (
            chat(payload=latex) if "chat/completions" in url
            else {"stop_reason": "end_turn", "usage": {}, "content": [{"type": "text", "text": latex}]})).start()
        self.assertEqual(ai.ask(data, key="k" * 30, model="claude-opus-5-5")["text"], "\\[ x^2 \\]")
        first = calls[0]["messages"][0]["content"][0]
        self.assertEqual((first["type"], first["source"]["media_type"]), ("image", "image/png"))
        ai.ask(data, **OPENAI)
        part = calls[1]["messages"][1]["content"][0]
        self.assertEqual(part["image_url"]["url"], "data:image/png;base64," + PNG)
        for image, status in (({"media_type": "image/jpeg", "data": PNG}, 415),
                              ({"media_type": "image/svg+xml", "data": PNG}, 415),
                              ({"media_type": "image/png", "data": "!!"}, 400),
                              ({"media_type": "image/png", "data": "A" * (ai.MAX_IMAGE + 4)}, 413)):
            with self.subTest(status=status), self.assertRaises(ai.AiError) as caught:
                ai.ask({**data, "image": image}, **OPENAI)
            self.assertEqual(caught.exception.status, status)
        self.assertEqual(len(calls), 2)


class ServeProviders(ts.SharedState, ts.ServerCase):
    """serve.py with an OpenAI-compatible endpoint: owner-only settings, keys never sent, local servers owner-only."""

    KEY = "sk-other-" + "q" * 30

    def setUp(self):
        ts.SharedState.setUp(self)
        ts.ServerCase.setUp(self)
        self.settings = self.root / "config" / "ai.json"
        mock.patch.object(ai, "SETTINGS_FILE", self.settings).start()
        mock.patch.dict(ai._ENV_KEYS, {"ANTHROPIC_API_KEY": None, ai.OPENAI_KEY_ENV: None}).start()
        mock.patch.object(ai, "LIMITER", grammar.Limiter(requests=1000, size=10 ** 9)).start()
        mock.patch.dict(serve.AI, {"guest_used": 0}).start()
        mock.patch.object(serve, "ai_sandboxed", return_value=True).start()
        self.calls = []
        mock.patch.object(ai, "post_json", side_effect=lambda url, body, headers: self.calls.append((url, headers))
                          or chat(text="Better.")).start()
        serve.SHARE["port"] = self.port
        self.tokens = dict(self.share_on())
        status, info = self.request("POST", "/api/share/links", {"name": "Bea", "role": "review"},
                                    self.hdr("owner"))
        self.assertEqual(status, 200)
        link = next(x for x in info["named"] if x["name"] == "Bea")
        self.tokens["review"] = link["url"].split("token=")[1].split("#")[0]

    def hdr(self, role):
        return {"Cookie": f"{serve.cookie_name()}={self.tokens[role]}"}

    def settings_as(self, role, body):
        return self.request("POST", "/api/ai/settings", body, self.hdr(role))

    def run_ai(self, role):
        return self.request("POST", "/api/ai?doc=demo", {"task": "rewrite", "selection": {"text": "x"}}, self.hdr(role))

    def test_local_model_server_is_for_the_owner_only(self):
        for role in ("edit", "review", "view"):
            self.assertEqual(self.settings_as(role, {"provider": "openai"})[0], 403)
        status, info = self.settings_as("owner", {"enabled": True, "share": True, "provider": "openai",
                                                  "base_url": "http://localhost:11434/v1", "openai_model": "local"})
        self.assertEqual((status, info["provider"], info["local"], info["openai_key"]), (200, "openai", True, None))
        self.assertEqual(self.run_ai("owner")[0], 200)
        self.assertEqual(self.calls[-1], ("http://localhost:11434/v1/chat/completions", {}))
        status, out = self.run_ai("edit")
        self.assertEqual(status, 403)
        self.assertIn("own network", out["error"])
        for role in ("review", "view"):
            self.assertEqual(self.run_ai(role)[0], 403)

    def test_hosted_endpoint_for_shared_editors_and_the_key_stays_here(self):
        self.settings_as("owner", {"enabled": True, "share": True, "provider": "openai", "openai_key": self.KEY,
                                   "base_url": "https://llm.example.org/v1", "openai_model": "m-large"})
        status, out = self.run_ai("edit")
        self.assertEqual((status, out["text"]), (200, "Better."))
        self.assertEqual(self.calls[-1][1]["Authorization"], "Bearer " + self.KEY)
        for role in ("owner", "edit", "review", "view"):
            for path in ("/api/ai", "/api/config"):
                self.assertNotIn(self.KEY, json.dumps(self.request("GET", path, headers=self.hdr(role))[1]))
        self.assertEqual(self.run_ai("review")[0], 403)
        if os.name != "nt":
            self.assertEqual(stat.S_IMODE(self.settings.stat().st_mode), 0o600)
        forgot = self.settings_as("owner", {"forget_openai_key": True})[1]
        self.assertIsNone(forgot["openai_key"])
        for bad in ({"provider": "other"}, {"base_url": "http://llm.example.org/v1"}, {"openai_model": "a b"},
                    {"openai_key": "x"}):
            self.assertEqual(self.settings_as("owner", bad)[0], 400)


if __name__ == "__main__":
    unittest.main()
