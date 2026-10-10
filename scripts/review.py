"""
Review comments and suggested edits for the editor (serve.py). Stdlib only.

One JSON file per document, beside its version history (outside the document directory). Items are anchored to
text by offsets plus the quoted text and a little context on each side (see serve_ui/anchors.js), so an anchor
survives edits elsewhere and is found again after the text moves. Nothing here touches the document: accepting a
suggestion only claims it (removes it, once); the editor that claimed it applies the edit through its open
document, like every other edit, so co-editing rooms stay consistent.
"""

from __future__ import annotations

import json
import os
import secrets
import tempfile
import threading
import time
from pathlib import Path

MAX_THREADS = 1000
MAX_SUGGESTIONS = 2000
MAX_COMMENTS = 200  # per thread
MAX_TEXT = 4000  # a comment
MAX_INSERT = 20000  # a suggestion's new text
MAX_QUOTE = 20000  # the anchored text
MAX_CONTEXT = 64
MAX_OFFSET = 4 * 1024 * 1024
LOCK = threading.Lock()  # ponytail: one lock for every document's file; review writes are rare and tiny


class ReviewError(Exception):
    def __init__(self, message: str, status: int = 400) -> None:
        super().__init__(message)
        self.status = status


def _text(value, limit: int, what: str, empty: bool = False) -> str:
    if not isinstance(value, str) or len(value) > limit or "\0" in value or (not empty and not value.strip()):
        raise ReviewError(f"Bad {what} (up to {limit} characters).")
    return value


def anchor(value) -> dict:
    """A validated anchor {from, to, quote, prefix, suffix} (UTF-16 offsets, like the editor counts)."""
    if not isinstance(value, dict):
        raise ReviewError("Bad anchor.")
    start, end = value.get("from"), value.get("to")
    if not all(isinstance(n, int) and not isinstance(n, bool) for n in (start, end)) \
            or not 0 <= start <= end <= MAX_OFFSET:
        raise ReviewError("Bad anchor.")
    return {"from": start, "to": end, "quote": _text(value.get("quote"), MAX_QUOTE, "selection", True),
            "prefix": _text(value.get("prefix", ""), MAX_CONTEXT, "anchor", True),
            "suffix": _text(value.get("suffix", ""), MAX_CONTEXT, "anchor", True)}


class Review:
    def __init__(self, path: Path, allow=None) -> None:
        self.path = path
        self.allow = allow or (lambda size: True)  # allow(new bytes) -> bool, e.g. a project quota

    def load(self) -> dict:
        try:
            data = json.loads(self.path.read_text("utf-8"))
            if isinstance(data, dict):
                return {"threads": list(data.get("threads") or []), "suggestions": list(data.get("suggestions") or [])}
        except (OSError, ValueError):
            pass
        return {"threads": [], "suggestions": []}

    def _save(self, data: dict) -> None:
        raw = json.dumps(data, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
        try:
            old = self.path.stat().st_size
        except OSError:
            old = 0
        if len(raw) > old and not self.allow(len(raw) - old):
            raise ReviewError("This project is over its quota; delete files to make room.", 507)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        handle, tmp = tempfile.mkstemp(dir=self.path.parent, prefix=".review.")
        try:
            with os.fdopen(handle, "wb") as out:
                out.write(raw)
            os.replace(tmp, self.path)
        except BaseException:
            try:
                os.unlink(tmp)
            except OSError:
                pass
            raise

    def apply(self, op: str, args: dict, who: dict, moderator: bool = False, now: float | None = None) -> dict:
        """
        Run one operation. who = {"key": stable author id, "name": display name}. moderator (the local owner) may
        delete anyone's comment. Returns {"item": ...} or {"removed": [...]}; raises ReviewError.
        """
        now = time.time() if now is None else now
        with LOCK:
            data = self.load()
            threads, suggestions = data["threads"], data["suggestions"]

            def thread(tid) -> dict:
                found = next((t for t in threads if t.get("id") == tid), None)
                if found is None:
                    raise ReviewError("That comment is gone; someone deleted it.", 404)
                return found

            def comment(text) -> dict:
                return {"id": secrets.token_hex(6), "author": who["key"], "name": who["name"],
                        "text": _text(text, MAX_TEXT, "comment"), "time": now}

            result: dict
            if op == "comment":
                if len(threads) >= MAX_THREADS:
                    raise ReviewError(f"This document has {MAX_THREADS} comment threads; delete some first.", 409)
                item = {"id": secrets.token_hex(6), "path": args["path"], "anchor": anchor(args.get("anchor")),
                        "resolved": False, "resolved_by": None, "time": now, "comments": [comment(args.get("text"))]}
                threads.append(item)
                result = {"item": item}
            elif op == "reply":
                item = thread(args.get("thread"))
                if len(item["comments"]) >= MAX_COMMENTS:
                    raise ReviewError("This thread is full; start a new one.", 409)
                item["comments"].append(comment(args.get("text")))
                result = {"item": item}
            elif op == "resolve":
                item = thread(args.get("thread"))
                item["resolved"] = bool(args.get("resolved", True))
                item["resolved_by"] = who["name"] if item["resolved"] else None
                result = {"item": item}
            elif op == "delete":
                item = thread(args.get("thread"))
                target = next((c for c in item["comments"] if c.get("id") == args.get("comment")), None)
                if target is None:
                    raise ReviewError("That comment is gone.", 404)
                if target.get("author") != who["key"] and not moderator:
                    raise ReviewError("You can only delete your own comments.", 403)
                item["comments"].remove(target)
                if not item["comments"]:
                    threads.remove(item)
                result = {"item": item if item["comments"] else None}
            elif op == "suggest":
                if len(suggestions) >= MAX_SUGGESTIONS:
                    raise ReviewError(f"This document has {MAX_SUGGESTIONS} open suggestions; accept or reject some.",
                                      409)
                spot = anchor(args.get("anchor"))
                insert = _text(args.get("insert", ""), MAX_INSERT, "suggestion", True)
                if spot["quote"] == insert:
                    raise ReviewError("That suggestion changes nothing.")
                item = {"id": secrets.token_hex(6), "path": args["path"], "anchor": spot, "insert": insert,
                        "author": who["key"], "name": who["name"], "time": now}
                suggestions.append(item)
                result = {"item": item}
            elif op in ("accept", "reject"):
                ids = args.get("ids")
                if not isinstance(ids, list) or not ids or len(ids) > MAX_SUGGESTIONS:
                    raise ReviewError("Bad suggestion list.")
                removed = [s for s in suggestions if s.get("id") in ids]
                if not removed:
                    raise ReviewError("Those suggestions were already accepted or rejected.", 409)
                data["suggestions"] = [s for s in suggestions if s not in removed]
                result = {"removed": removed}
            elif op == "reanchor":
                items = args.get("items")
                if not isinstance(items, list) or len(items) > MAX_THREADS + MAX_SUGGESTIONS:
                    raise ReviewError("Bad anchor list.")
                by_id = {x.get("id"): x for x in [*threads, *suggestions]}
                moved = 0
                for entry in items:
                    target = by_id.get(entry.get("id")) if isinstance(entry, dict) else None
                    if target is not None:
                        target["anchor"] = anchor(entry.get("anchor"))
                        moved += 1
                result = {"moved": moved}
            else:
                raise ReviewError("Unknown review operation.")
            self._save(data)
            return result

    def rename(self, old: str, new: str) -> bool:
        """Items on old (a file, or everything below a folder) follow it to new. True when anything moved."""
        with LOCK:
            data = self.load()
            moved = False
            for item in [*data["threads"], *data["suggestions"]]:
                path = item.get("path") or ""
                if path == old or path.startswith(old + "/"):
                    item["path"] = new + path[len(old):]
                    moved = True
            if moved:
                self._save(data)
            return moved
