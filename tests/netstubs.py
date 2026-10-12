"""
Loopback HTTP servers that answer like the outside services the scripts talk to, so ai.post_json/post_stream,
zotero.get, bibfix.get_json and grammar.post_form run their real HTTP code in tests without the network.

Each stub is a ThreadingHTTPServer on 127.0.0.1 (port 0), records every request (method, path, headers, body) and
answers from its `modes` queue (one entry per request, then "ok"). The wire formats follow the providers' own
documentation (checked 2026-10-12):

- Anthropic Messages API, https://platform.claude.com/docs/en/api/messages and .../build-with-claude/streaming:
  x-api-key and anthropic-version headers; a message object; errors {"type": "error", "error": {type, message}};
  streams of `event:`/`data:` pairs: message_start, content_block_start, ping, content_block_delta (text_delta),
  content_block_stop, message_delta (stop_reason, cumulative usage), message_stop; an `error` event mid-stream.
- OpenAI Chat Completions, https://developers.openai.com/api/reference/resources/chat/subresources/completions:
  Authorization: Bearer; a chat.completion object; streams of `data:` chat.completion.chunk objects ending with
  `data: [DONE]`; with stream_options.include_usage a last chunk with empty choices carries usage (null before).
- Zotero Web API v3, https://www.zotero.org/support/dev/web_api/v3/basics and .../syncing: Zotero-API-Key header,
  limit/start paging, Total-Results, Last-Modified-Version, If-Modified-Since-Version -> 304, 403 for a bad key,
  429 with Retry-After. Better BibTeX pull export: GET /better-bibtex/export/library?/1/library.bibtex.
- Crossref REST API, https://api.crossref.org/swagger-ui/index.html: /works/{doi} -> {"message": {...}},
  /works?query.bibliographic=...&rows=N -> {"message": {"items": [...]}}, 404 "Resource not found." for a DOI.
- LanguageTool HTTP API, https://languagetool.org/http-api/: POST /v2/check (form: text or data, language),
  GET /v2/languages.
"""

from __future__ import annotations

import json
import re
import threading
import time
import urllib.parse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer


class Stub:
    """A loopback server; subclasses implement answer(request) -> (status, headers, body bytes or iterable)."""

    def __init__(self) -> None:
        self.requests: list[dict] = []
        self.modes: list[str] = []
        self.lock = threading.Lock()
        stub = self

        class Handler(BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.0"  # a stream ends when the connection closes, like a chunked reply's end

            def log_message(self, *args):  # quiet tests
                pass

            def handle_one(self):
                length = int(self.headers.get("Content-Length") or 0)
                body = self.rfile.read(length) if length else b""
                url = urllib.parse.urlsplit(self.path)
                request = {"method": self.command, "path": url.path, "query": url.query, "body": body,
                           "headers": {k.lower(): v for k, v in self.headers.items()}}
                with stub.lock:
                    stub.requests.append(request)
                    request["mode"] = stub.modes.pop(0) if stub.modes else "ok"
                status, headers, payload = stub.answer(request)
                self.send_response(status)
                for key, value in headers.items():
                    self.send_header(key, value)
                if isinstance(payload, (bytes, bytearray)):
                    self.send_header("Content-Length", str(len(payload)))
                    self.end_headers()
                    self.wfile.write(payload)
                    return
                self.end_headers()
                try:
                    for chunk in payload:
                        self.wfile.write(chunk)
                        self.wfile.flush()
                except (BrokenPipeError, ConnectionResetError):
                    with stub.lock:
                        stub.dropped += 1

            do_GET = do_POST = handle_one

        self.dropped = 0
        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.server.daemon_threads = True
        self.url = f"http://127.0.0.1:{self.server.server_address[1]}"
        threading.Thread(target=self.server.serve_forever, daemon=True).start()

    def close(self) -> None:
        self.server.shutdown()
        self.server.server_close()

    def answer(self, request: dict):  # pragma: no cover - every stub overrides it
        raise NotImplementedError


def _json(status: int, data, headers: dict | None = None):
    return status, {"Content-Type": "application/json", **(headers or {})}, json.dumps(data).encode()


def _sse(events, pause: float = 0.0, size: int = 0):
    """Server-sent events as bytes, written in pieces of `size` bytes (0: one event per write)."""
    for event in events:
        raw = event.encode("utf-8")
        for i in range(0, len(raw), size or len(raw) or 1):
            if pause:
                time.sleep(pause)
            yield raw[i:i + size] if size else raw


# ---------------------------------------------------------------------------
# Model providers
# ---------------------------------------------------------------------------

class AnthropicStub(Stub):
    """POST /v1/messages. Modes: ok, refusal, max_tokens, notjson, http401, http429, http500, http529,
    stream_error (an error event after the first text), stream_cut (no message_delta/message_stop), slow."""

    def __init__(self, key: str, answer: str = "Close the brace.", usage=(100, 20)) -> None:
        super().__init__()
        self.key, self.reply_answer, self.usage = key, answer, usage

    def payload(self, body: dict) -> str:
        schema = (((body.get("output_config") or {}).get("format") or {}).get("schema") or {})
        if "claims" in (schema.get("properties") or {}):
            return json.dumps({"answer": self.reply_answer, "claims": []})
        return json.dumps({"answer": self.reply_answer, "text": "Better text.", "edits": []})

    def answer(self, request):
        mode = request["mode"]
        if request["method"] != "POST" or request["path"] != "/v1/messages":
            return _json(404, {"type": "error", "error": {"type": "not_found_error", "message": "Not found"}})
        if request["headers"].get("x-api-key") != self.key or mode == "http401":
            return _json(401, {"type": "error", "error": {"type": "authentication_error",
                                                          "message": "invalid x-api-key"}})
        if not request["headers"].get("anthropic-version"):
            return _json(400, {"type": "error", "error": {"type": "invalid_request_error",
                                                          "message": "anthropic-version header is required"}})
        status = {"http429": (429, "rate_limit_error"), "http500": (500, "api_error"),
                  "http529": (529, "overloaded_error")}.get(mode)
        if status:
            return _json(status[0], {"type": "error", "error": {"type": status[1], "message": "Stub " + mode}},
                         {"retry-after": "1"} if status[0] == 429 else None)
        body = json.loads(request["body"])
        for field in ("model", "max_tokens", "messages"):
            if field not in body:
                return _json(400, {"type": "error", "error": {"type": "invalid_request_error",
                                                              "message": f"{field}: Field required"}})
        stop = {"refusal": "refusal", "max_tokens": "max_tokens"}.get(mode, "end_turn")
        text = "not json at all" if mode == "notjson" else self.payload(body)
        model, (tokens_in, tokens_out) = body["model"], self.usage
        if not body.get("stream"):
            return _json(200, {"id": "msg_stub", "type": "message", "role": "assistant", "model": model,
                               "content": [{"type": "text", "text": text}], "stop_reason": stop,
                               "stop_sequence": None,
                               "usage": {"input_tokens": tokens_in, "output_tokens": tokens_out}})
        events = [("message_start", {"type": "message_start", "message": {
                      "id": "msg_stub", "type": "message", "role": "assistant", "model": model, "content": [],
                      "stop_reason": None, "stop_sequence": None,
                      "usage": {"input_tokens": tokens_in, "output_tokens": 1}}}),
                  ("content_block_start", {"type": "content_block_start", "index": 0,
                                           "content_block": {"type": "text", "text": ""}}),
                  ("ping", {"type": "ping"})]
        pieces = [text[i:i + 7] for i in range(0, len(text), 7)]
        for n, piece in enumerate(pieces):
            events.append(("content_block_delta", {"type": "content_block_delta", "index": 0,
                                                   "delta": {"type": "text_delta", "text": piece}}))
            if mode == "stream_error" and n == 1:
                events.append(("error", {"type": "error", "error": {"type": "overloaded_error",
                                                                    "message": "Overloaded"}}))
                break
        else:
            if mode != "stream_cut":
                events += [("content_block_stop", {"type": "content_block_stop", "index": 0}),
                           ("message_delta", {"type": "message_delta",
                                              "delta": {"stop_reason": stop, "stop_sequence": None},
                                              "usage": {"output_tokens": tokens_out}}),
                           ("message_stop", {"type": "message_stop"})]
        raw = [f"event: {name}\ndata: {json.dumps(data)}\n\n" for name, data in events]
        return 200, {"Content-Type": "text/event-stream", "Cache-Control": "no-cache"}, \
            _sse(raw, pause=0.02 if mode == "slow" else 0.0, size=11)


class OpenAiStub(Stub):
    """POST /v1/chat/completions. Modes: ok, refusal, length, nousage, http401, http429, http500, stream_error,
    stream_cut (no [DONE])."""

    def __init__(self, key: str | None, answer: str = "Close the brace.", usage=(50, 7)) -> None:
        super().__init__()
        self.key, self.reply_answer, self.usage = key, answer, usage

    def answer(self, request):
        mode = request["mode"]
        if request["method"] != "POST" or request["path"] != "/v1/chat/completions":
            return _json(404, {"error": {"message": "Unknown path", "type": "invalid_request_error"}})
        if (self.key and request["headers"].get("authorization") != f"Bearer {self.key}") or mode == "http401":
            return _json(401, {"error": {"message": "Incorrect API key provided.", "type": "invalid_request_error",
                                         "code": "invalid_api_key"}})
        status = {"http429": 429, "http500": 500}.get(mode)
        if status:
            return _json(status, {"error": {"message": "Stub " + mode, "type": "server_error"}})
        body = json.loads(request["body"])
        if "model" not in body or "messages" not in body:
            return _json(400, {"error": {"message": "model and messages are required"}})
        fmt = ((body.get("response_format") or {}).get("json_schema") or {}).get("schema") or {}
        text = json.dumps({"answer": self.reply_answer, "claims": []} if "claims" in (fmt.get("properties") or {})
                          else {"answer": self.reply_answer, "text": "Better text.", "edits": []})
        finish = "length" if mode == "length" else "stop"
        usage = None if mode == "nousage" else {"prompt_tokens": self.usage[0], "completion_tokens": self.usage[1],
                                                "total_tokens": sum(self.usage)}
        base = {"id": "chatcmpl-stub", "created": 1760000000, "model": body["model"],
                "system_fingerprint": "fp_stub"}
        if not body.get("stream"):
            message = {"role": "assistant", "content": None if mode == "refusal" else text,
                       "refusal": "I can't help with that." if mode == "refusal" else None}
            reply = {**base, "object": "chat.completion",
                     "choices": [{"index": 0, "message": message, "logprobs": None, "finish_reason": finish}]}
            if usage:
                reply["usage"] = usage
            return _json(200, reply)
        include = bool((body.get("stream_options") or {}).get("include_usage"))

        def chunk(choices, use=None):
            item = {**base, "object": "chat.completion.chunk", "choices": choices, "obfuscation": "x1"}
            if include:
                item["usage"] = use
            return "data: " + json.dumps(item) + "\n\n"
        raw = [chunk([{"index": 0, "delta": {"role": "assistant", "content": ""}, "logprobs": None,
                       "finish_reason": None}])]
        if mode == "refusal":
            raw.append(chunk([{"index": 0, "delta": {"refusal": "I can't"}, "finish_reason": None}]))
        else:
            for n, i in enumerate(range(0, len(text), 6)):
                raw.append(chunk([{"index": 0, "delta": {"content": text[i:i + 6]}, "logprobs": None,
                                   "finish_reason": None}]))
                if mode == "stream_error" and n == 1:
                    raw.append("data: " + json.dumps({"error": {"message": "Stub stream error"}}) + "\n\n")
                    break
        if mode != "stream_error":
            raw.append(chunk([{"index": 0, "delta": {}, "logprobs": None, "finish_reason": finish}]))
            if include and usage:
                raw.append(chunk([], usage))
            if mode != "stream_cut":
                raw.append("data: [DONE]\n\n")
        return 200, {"Content-Type": "text/event-stream"}, _sse(raw, size=13)


# ---------------------------------------------------------------------------
# Zotero web API and Better BibTeX
# ---------------------------------------------------------------------------

class ZoteroStub(Stub):
    """GET /{users|groups}/{id}[/collections/{key}]/items/top?format=bibtex&limit=&start= and the Better BibTeX
    pull export. Modes: ok, http429, http503. `entries` is the library (BibTeX strings), `version` its version."""

    def __init__(self, key: str, entries: list[str], library: str = "users/12345") -> None:
        super().__init__()
        self.key, self.entries, self.library, self.version = key, list(entries), library, 7

    def answer(self, request):
        mode, path = request["mode"], request["path"]
        if path.startswith("/better-bibtex/export/"):
            if request["headers"].get("zotero-api-key"):
                return 400, {}, b"no key expected"
            if not re.fullmatch(r"/1/(library|[A-Z0-9]+)\.(bibtex|biblatex)", urllib.parse.unquote(request["query"])):
                return 404, {}, b"Not found"
            return 200, {"Content-Type": "text/plain; charset=utf-8"}, "\n".join(self.entries).encode()
        match = re.fullmatch(rf"/{self.library}(?:/collections/[A-Z0-9]{{8}})?/items/top", path)
        if not match:
            return 404, {"Content-Type": "text/plain"}, b"Not found"
        if request["headers"].get("zotero-api-key") != self.key:
            return 403, {"Content-Type": "text/plain"}, b"Forbidden"
        if mode == "http429":
            return 429, {"Retry-After": "5"}, b"Too many requests"
        if mode == "http503":
            return 503, {"Retry-After": "9"}, b"Service unavailable"
        if request["headers"].get("zotero-api-version") != "3":
            return 400, {}, b"Zotero-API-Version 3 expected"
        query = dict(urllib.parse.parse_qsl(request["query"]))
        since = request["headers"].get("if-modified-since-version")
        head = {"Last-Modified-Version": str(self.version), "Total-Results": str(len(self.entries))}
        if since and int(since) >= self.version:
            return 304, head, b""
        limit, start = min(int(query.get("limit", 25)), 100), int(query.get("start", 0))
        if query.get("format") not in ("bibtex", "biblatex"):
            return 400, {}, b"format"
        return 200, {"Content-Type": "text/plain; charset=utf-8", **head}, \
            "\n".join(self.entries[start:start + limit]).encode()


# ---------------------------------------------------------------------------
# Crossref and LanguageTool
# ---------------------------------------------------------------------------

class CrossrefStub(Stub):
    """GET /works/{doi} and /works?query.bibliographic=. `works` maps DOI -> message. Modes: ok, http500."""

    def __init__(self, works: dict) -> None:
        super().__init__()
        self.works = works

    def answer(self, request):
        if request["mode"] == "http500":
            return 500, {"Content-Type": "text/plain"}, b"Internal error"
        path = urllib.parse.unquote(request["path"])
        if path.startswith("/works/"):
            item = self.works.get(path[len("/works/"):].lower())
            if not item:
                return 404, {"Content-Type": "text/plain"}, b"Resource not found."
            return _json(200, {"status": "ok", "message-type": "work", "message-version": "1.0.0", "message": item})
        if path == "/works":
            query = dict(urllib.parse.parse_qsl(request["query"]))
            words = set(re.findall(r"\w+", query.get("query.bibliographic", "").lower()))
            items = [w for w in self.works.values() if words & set(re.findall(r"\w+", " ".join(w["title"]).lower()))]
            return _json(200, {"status": "ok", "message-type": "work-list", "message": {
                "total-results": len(items), "items": items[:int(query.get("rows", 20))]}})
        return 404, {}, b"Resource not found."


class LanguageToolStub(Stub):
    """POST /v2/check: one match for every "This are"; GET /v2/languages."""

    def answer(self, request):
        if request["path"] == "/v2/languages":
            return _json(200, [{"name": "English (US)", "code": "en", "longCode": "en-US"}])
        if request["path"] != "/v2/check" or request["method"] != "POST":
            return 404, {}, b""
        form = dict(urllib.parse.parse_qsl(request["body"].decode()))
        if "data" in form:
            text = "".join(a.get("text", "") or a.get("markup", "") for a in json.loads(form["data"])["annotation"])
        else:
            text = form.get("text", "")
        matches = [{"message": "The verb 'are' does not agree with 'This'.", "shortMessage": "Agreement",
                    "replacements": [{"value": "is"}], "offset": m.start(1), "length": 3,
                    "context": {"text": text[:40], "offset": 0, "length": 3}, "sentence": text[:80],
                    "rule": {"id": "THIS_NNS_VB", "description": "this + plural", "issueType": "grammar",
                             "category": {"id": "GRAMMAR", "name": "Grammar"}}}
                   for m in re.finditer(r"\bThis (are)\b", text)]
        return _json(200, {"software": {"name": "LanguageTool", "version": "stub", "apiVersion": 1},
                           "language": {"name": "English (US)", "code": form.get("language", "en-US")},
                           "matches": matches})
