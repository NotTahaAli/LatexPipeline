"""
Version history of a document's text files, for the editor (serve.py). Stdlib only.

One store per document, outside its directory (so it is never a build input, never in a zip export or a CI scan):
a content-addressed blob folder (zlib, deduplicated by SHA-256) and an sqlite index of versions. A version row is
one file's content at a moment (or its deletion); a label row names the state of the whole project and keeps its
own manifest {path: hash}, so pruning old rows never changes what a label restores.

Kinds: auto (saved in the editor; one person's saves of a file within COALESCE seconds merge into one row),
outside (the file changed outside the editor, recorded before it is overwritten), delete, rename, restore, label.
"""

from __future__ import annotations

import difflib
import hashlib
import json
import os
import sqlite3
import tempfile
import threading
import time
import zlib
from contextlib import closing
from pathlib import Path

COALESCE = 300.0  # seconds: one author's saves of one file merge into one version for this long
MAX_ROWS = 3000  # versions per document; the oldest unlabelled ones go first
MAX_LABELS = 300
MAX_BYTES = 64 * 1024 * 1024  # compressed blob bytes per document
MAX_DIFF_ROWS = 4000
LOCK = threading.RLock()  # ponytail: one lock for every store; history writes are small and rare enough

SCHEMA = """
CREATE TABLE IF NOT EXISTS versions (
    id INTEGER PRIMARY KEY AUTOINCREMENT, time REAL NOT NULL, started REAL NOT NULL,
    path TEXT, hash TEXT, size INTEGER NOT NULL DEFAULT 0, kind TEXT NOT NULL,
    authors TEXT NOT NULL DEFAULT '[]', label TEXT, manifest TEXT);
CREATE INDEX IF NOT EXISTS versions_path ON versions(path, id);
CREATE TABLE IF NOT EXISTS blobs (hash TEXT PRIMARY KEY, bytes INTEGER NOT NULL);
"""


class Full(Exception):
    """The store may not grow (the caller's quota said no)."""


def digest(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


class Store:
    def __init__(self, folder: Path, max_bytes: int = MAX_BYTES, allow=None) -> None:
        self.folder = folder
        self.max_bytes = max_bytes
        self.allow = allow or (lambda size: True)  # allow(new bytes) -> bool, e.g. a project quota

    # --- storage -----------------------------------------------------------------------------------------------

    def _db(self) -> sqlite3.Connection:
        self.folder.mkdir(parents=True, exist_ok=True)
        con = sqlite3.connect(str(self.folder / "index.sqlite3"), timeout=10)
        con.row_factory = sqlite3.Row
        con.executescript(SCHEMA)
        return con

    def _blob_path(self, h: str) -> Path:
        return self.folder / "blobs" / h[:2] / h

    def _put(self, con: sqlite3.Connection, text: str) -> str:
        h = digest(text)
        if con.execute("SELECT 1 FROM blobs WHERE hash = ?", (h,)).fetchone() and self._blob_path(h).is_file():
            return h
        data = zlib.compress(text.encode("utf-8"), 6)
        if not self.allow(len(data)):
            raise Full("no room for history")
        path = self._blob_path(h)
        path.parent.mkdir(parents=True, exist_ok=True)
        handle, tmp = tempfile.mkstemp(dir=path.parent, prefix=".blob.")
        try:
            with os.fdopen(handle, "wb") as out:
                out.write(data)
            os.replace(tmp, path)
        except BaseException:
            try:
                os.unlink(tmp)
            except OSError:
                pass
            raise
        con.execute("INSERT OR REPLACE INTO blobs (hash, bytes) VALUES (?, ?)", (h, len(data)))
        return h

    def blob(self, h: str) -> str:
        try:
            return zlib.decompress(self._blob_path(h).read_bytes()).decode("utf-8")
        except (OSError, zlib.error, UnicodeDecodeError, ValueError):
            raise KeyError(h)

    # --- writing ----------------------------------------------------------------------------------------------

    def record(self, path: str, text: str | None, authors=(), kind: str = "auto", label: str | None = None,
               now: float | None = None) -> int | None:
        """
        Add a version of one file (text None: it was deleted). Returns the new row's id, or None when nothing new
        was stored (same content as the file's latest version, or merged into it).
        """
        now = time.time() if now is None else now
        names = json.dumps(sorted({str(a)[:80] for a in authors if a}))
        with LOCK, closing(self._db()) as con, con:
            last = con.execute("SELECT * FROM versions WHERE path = ? ORDER BY id DESC LIMIT 1", (path,)).fetchone()
            h = None if text is None else digest(text)
            if last is not None and last["hash"] == h and kind in ("auto", "outside", "delete"):
                return None
            if last is None and text is None:
                return None  # A file we never saw went away: nothing to remember.
            if text is not None:
                self._put(con, text)
            size = 0 if text is None else len(text)
            merge = (kind == "auto" and last is not None and last["kind"] == "auto" and last["hash"] is not None
                     and last["authors"] == names and now - last["started"] < COALESCE
                     and not con.execute("SELECT 1 FROM versions WHERE id > ? AND kind IN ('label', 'restore')",
                                         (last["id"],)).fetchone())
            if merge:
                con.execute("UPDATE versions SET time = ?, hash = ?, size = ? WHERE id = ?", (now, h, size, last["id"]))
                self._prune(con, orphaned=True)
                return None
            cur = con.execute(
                "INSERT INTO versions (time, started, path, hash, size, kind, authors, label) VALUES (?,?,?,?,?,?,?,?)",
                (now, now, path, h, size, kind, names, label))
            self._prune(con)
            return cur.lastrowid

    def label(self, name: str, authors, texts: dict[str, str], now: float | None = None) -> int:
        """Name the current state of the project (texts: every text file's content)."""
        now = time.time() if now is None else now
        with LOCK, closing(self._db()) as con, con:
            manifest = {path: self._put(con, text) for path, text in sorted(texts.items())}
            cur = con.execute(
                "INSERT INTO versions (time, started, path, hash, size, kind, authors, label, manifest) "
                "VALUES (?, ?, NULL, NULL, ?, 'label', ?, ?, ?)",
                (now, now, sum(len(t) for t in texts.values()), json.dumps(sorted({str(a)[:80] for a in authors})),
                 name, json.dumps(manifest)))
            self._prune(con)
            return cur.lastrowid

    def _prune(self, con: sqlite3.Connection, orphaned: bool = False) -> None:
        """Keep MAX_ROWS rows, MAX_LABELS labels and max_bytes of blobs. A file's newest row always stays."""
        labels = con.execute("SELECT COUNT(*) FROM versions WHERE kind = 'label'").fetchone()[0]
        if labels > MAX_LABELS:
            con.execute("DELETE FROM versions WHERE id IN (SELECT id FROM versions WHERE kind = 'label' "
                        "ORDER BY id LIMIT ?)", (labels - MAX_LABELS,))
            orphaned = True
        if orphaned:
            self._collect(con)
        while (con.execute("SELECT COUNT(*) FROM versions").fetchone()[0] > MAX_ROWS
               or con.execute("SELECT COALESCE(SUM(bytes), 0) FROM blobs").fetchone()[0] > self.max_bytes):
            victims = [r[0] for r in con.execute(
                "SELECT v.id FROM versions v WHERE v.path IS NOT NULL AND EXISTS "
                "(SELECT 1 FROM versions w WHERE w.path = v.path AND w.id > v.id) ORDER BY v.id LIMIT 50")]
            victims = victims or [r[0] for r in con.execute(
                "SELECT id FROM versions WHERE kind = 'label' ORDER BY id LIMIT 1")]
            if not victims:
                break
            con.execute(f"DELETE FROM versions WHERE id IN ({','.join('?' * len(victims))})", victims)
            self._collect(con)

    def _collect(self, con: sqlite3.Connection) -> None:
        """Delete blobs no row and no label manifest refers to."""
        used = {r[0] for r in con.execute("SELECT hash FROM versions WHERE hash IS NOT NULL")}
        for (manifest,) in con.execute("SELECT manifest FROM versions WHERE manifest IS NOT NULL"):
            used.update(json.loads(manifest).values())
        for (h,) in con.execute("SELECT hash FROM blobs").fetchall():
            if h not in used:
                con.execute("DELETE FROM blobs WHERE hash = ?", (h,))
                try:
                    self._blob_path(h).unlink()
                except OSError:
                    pass

    # --- reading ----------------------------------------------------------------------------------------------

    @staticmethod
    def _row(r: sqlite3.Row) -> dict:
        out = {"id": r["id"], "time": r["time"], "path": r["path"], "kind": r["kind"],
               "authors": json.loads(r["authors"]), "label": r["label"], "size": r["size"],
               "deleted": r["path"] is not None and r["hash"] is None}
        if r["manifest"] is not None:
            out["files"] = sorted(json.loads(r["manifest"]))
        return out

    def versions(self, path: str | None = None, before: int | None = None, limit: int = 200) -> list[dict]:
        """Newest first. With a path: that file's versions plus every label."""
        where, args = ["1"], []
        if path is not None:
            where.append("(path = ? OR kind = 'label')")
            args.append(path)
        if before is not None:
            where.append("id < ?")
            args.append(before)
        with LOCK, closing(self._db()) as con:
            rows = con.execute(f"SELECT * FROM versions WHERE {' AND '.join(where)} ORDER BY id DESC LIMIT ?",
                               (*args, max(1, min(limit, 500)))).fetchall()
        return [self._row(r) for r in rows]

    def get(self, vid: int) -> dict:
        with LOCK, closing(self._db()) as con:
            r = con.execute("SELECT * FROM versions WHERE id = ?", (vid,)).fetchone()
        if r is None:
            raise KeyError(vid)
        return {**self._row(r), "hash": r["hash"], "manifest": json.loads(r["manifest"] or "null")}

    def state_at(self, vid: int) -> dict[str, str]:
        """{path: hash} of the project at a version: a label's manifest, else each file's newest row up to it."""
        row = self.get(vid)
        if row["manifest"] is not None:
            return dict(row["manifest"])
        with LOCK, closing(self._db()) as con:
            rows = con.execute("SELECT path, hash FROM versions WHERE id IN (SELECT MAX(id) FROM versions "
                               "WHERE id <= ? AND path IS NOT NULL GROUP BY path)", (vid,)).fetchall()
        return {r["path"]: r["hash"] for r in rows if r["hash"] is not None}

    def text_at(self, vid: int, path: str) -> str | None:
        """A file's text at a version (None: it did not exist then). Raises KeyError for an unknown version."""
        row = self.get(vid)
        if row["manifest"] is not None:
            h = row["manifest"].get(path)
        elif row["path"] == path:
            h = row["hash"]
        else:
            h = self.state_at(vid).get(path)
        return None if h is None else self.blob(h)

    def previous(self, vid: int, path: str) -> int | None:
        """The version of `path` before row vid (labels not counted)."""
        with LOCK, closing(self._db()) as con:
            r = con.execute("SELECT MAX(id) FROM versions WHERE path = ? AND id < ?", (path, vid)).fetchone()
        return r[0]

    def latest(self, path: str) -> dict | None:
        with LOCK, closing(self._db()) as con:
            r = con.execute("SELECT * FROM versions WHERE path = ? ORDER BY id DESC LIMIT 1", (path,)).fetchone()
        return None if r is None else {**self._row(r), "hash": r["hash"]}


def diff(a: str, b: str, context: int = 3) -> dict:
    """Line diff a -> b as hunks of rows [op, old line, new line, text]; op is ' ', '-' or '+'."""
    old, new = a.split("\n"), b.split("\n")
    matcher = difflib.SequenceMatcher(None, old, new, autojunk=len(old) + len(new) > 40000)
    hunks, added, removed, rows_out, cut = [], 0, 0, 0, False
    for group in matcher.get_grouped_opcodes(context):
        rows: list[list] = []
        for tag, i1, i2, j1, j2 in group:
            if tag == "equal":
                rows += [[" ", i + 1, j1 + (i - i1) + 1, old[i]] for i in range(i1, i2)]
                continue
            if tag in ("replace", "delete"):
                rows += [["-", i + 1, None, old[i]] for i in range(i1, i2)]
                removed += i2 - i1
            if tag in ("replace", "insert"):
                rows += [["+", None, j + 1, new[j]] for j in range(j1, j2)]
                added += j2 - j1
        if rows_out + len(rows) > MAX_DIFF_ROWS:
            cut = True
            break
        rows_out += len(rows)
        hunks.append(rows)
    return {"hunks": hunks, "added": added, "removed": removed, "truncated": cut}
