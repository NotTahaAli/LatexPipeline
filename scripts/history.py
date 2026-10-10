"""
Version history of a document's text files, for the editor (serve.py). Stdlib only.

One store per document, outside its directory (so it is never a build input, never in a zip export or a CI scan):
a content-addressed blob folder (zlib, deduplicated by SHA-256) and an sqlite index of versions. A version row is
one file's content at a moment (or its deletion); a label row names the state of the whole project and keeps its
own manifest {path: hash}, so pruning old rows never changes what a label restores.

Kinds: auto (saved in the editor; one person's saves of a file within COALESCE seconds merge into one row),
outside (the file changed outside the editor, recorded before it is overwritten), delete, rename, restore, label.

Unnamed versions thin out with age (THIN): every new version thins the older ones a little (at most THIN_BATCH rows),
so the store keeps a long, sparse past instead of filling up. Named versions, each file's newest row and the text
before a deletion are never thinned; blobs no row refers to any more are deleted. The caps (MAX_ROWS, max_bytes,
the caller's room()) stay as a last resort: past them history raises Full and stops rather than forget. A pending
restore (pend) is text a restore must still put into a file that is open in a co-editing room.
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
MAX_ROWS = 3000  # file versions per document (labels not counted); the oldest superseded ones go first
MAX_LABELS = 300  # named versions per document; never evicted, refused past this
MAX_SHARED_LABELS = 100  # of which a shared role (not the owner) may make this many
MAX_BYTES = 64 * 1024 * 1024  # compressed blob bytes per document
MAX_DIFF_ROWS = 4000
MAX_DIFF_LINES = 2000  # lines of the changed middle (after the common start and end) the diff matcher looks at
BLOCK = 4096  # a quota charges at least one disk block per file
ROW_BYTES = 256  # what one index row is charged against room() (the sqlite file counts toward a quota too)
# (age in seconds, one version per this many seconds and file): newer than a day all, a week hourly, 90 days daily,
# then weekly. A bucket keeps its newest version.
THIN = ((86400.0, 0.0), (7 * 86400.0, 3600.0), (90 * 86400.0, 86400.0), (float("inf"), 7 * 86400.0))
THIN_BATCH = 500  # versions one record may thin, so a write never does unbounded work
CLAIM_TTL = 30.0  # seconds a client's claim on a pending restore holds before another editor may take it over
LOCK = threading.RLock()  # ponytail: one lock for every store; history writes are small and rare enough

SCHEMA = """
CREATE TABLE IF NOT EXISTS versions (
    id INTEGER PRIMARY KEY AUTOINCREMENT, time REAL NOT NULL, started REAL NOT NULL,
    path TEXT, hash TEXT, size INTEGER NOT NULL DEFAULT 0, kind TEXT NOT NULL,
    authors TEXT NOT NULL DEFAULT '[]', label TEXT, manifest TEXT, role TEXT);
CREATE INDEX IF NOT EXISTS versions_path ON versions(path, id);
CREATE TABLE IF NOT EXISTS blobs (hash TEXT PRIMARY KEY, bytes INTEGER NOT NULL);
CREATE TABLE IF NOT EXISTS pending (path TEXT PRIMARY KEY, hash TEXT NOT NULL, token TEXT NOT NULL,
    time REAL NOT NULL, claim TEXT, claimed REAL, base TEXT);
"""


FILE_ROWS = "SELECT id, path, time, hash FROM versions WHERE path IS NOT NULL ORDER BY path, id"


class Full(Exception):
    """The store may not grow: over its caps, or the caller's quota said no. Nothing was recorded."""


class Refused(Full):
    """Too many named versions; the caller should say so rather than fail quietly."""


def digest(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


class Store:
    def __init__(self, folder: Path, max_bytes: int = MAX_BYTES, room=None) -> None:
        self.folder = folder
        self.max_bytes = max_bytes
        self.room = room or (lambda: None)  # room() -> bytes the store may still add (e.g. a quota), None: no limit

    # --- storage -----------------------------------------------------------------------------------------------

    def _db(self) -> sqlite3.Connection:
        self.folder.mkdir(parents=True, exist_ok=True)
        con = sqlite3.connect(str(self.folder / "index.sqlite3"), timeout=10)
        con.row_factory = sqlite3.Row
        con.executescript(SCHEMA)
        if "role" not in {r[1] for r in con.execute("PRAGMA table_info(versions)")}:  # stores made before it
            con.execute("ALTER TABLE versions ADD COLUMN role TEXT")
        if "base" not in {r[1] for r in con.execute("PRAGMA table_info(pending)")}:
            con.execute("ALTER TABLE pending ADD COLUMN base TEXT")
        return con

    def _blob_path(self, h: str) -> Path:
        return self.folder / "blobs" / h[:2] / h

    def _put(self, con: sqlite3.Connection, text: str, budget: list, written: list) -> str:
        """Store a blob; budget = [bytes left or None], measured once per record; written collects new files."""
        h = digest(text)
        if con.execute("SELECT 1 FROM blobs WHERE hash = ?", (h,)).fetchone() and self._blob_path(h).is_file():
            return h
        data = zlib.compress(text.encode("utf-8"), 6)
        self._charge(budget, max(len(data), BLOCK))
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
        written.append(h)
        con.execute("INSERT OR REPLACE INTO blobs (hash, bytes) VALUES (?, ?)", (h, len(data)))
        return h

    @staticmethod
    def _charge(budget: list, cost: int) -> None:
        if budget[0] is not None:
            if cost > budget[0]:
                raise Full("no room for history")
            budget[0] -= cost

    def _write(self, work, vacuum: bool = False):
        """
        Run work(con, put, charge) in one transaction; on any failure nothing stays, the new blob files included.
        charge(n) bills n bytes of index growth against room(); vacuum shrinks the index file afterwards.
        """
        written: list = []
        with LOCK, closing(self._db()) as con:
            self._doomed = []
            budget = [self.room()]
            try:
                with con:
                    result = work(con, lambda text: self._put(con, text, budget, written),
                                  lambda cost: self._charge(budget, cost))
            except BaseException:
                gone = written
                raise
            else:
                gone = self._doomed
            finally:
                for h in gone:
                    try:
                        self._blob_path(h).unlink()
                    except OSError:
                        pass
            if vacuum:
                con.execute("VACUUM")
            return result

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
        was stored (same content as the file's latest version, or merged into it). Raises Full when the store is at
        its caps and nothing old may go: history then stops instead of forgetting.
        """
        now = time.time() if now is None else now
        names = json.dumps(sorted({str(a)[:80] for a in authors if a}))

        def work(con, put, charge):
            last = con.execute("SELECT * FROM versions WHERE path = ? ORDER BY id DESC LIMIT 1", (path,)).fetchone()
            h = None if text is None else digest(text)
            if last is not None and last["hash"] == h and kind in ("auto", "outside", "delete"):
                return None
            if last is None and text is None:
                return None  # A file we never saw went away: nothing to remember.
            if text is not None:
                put(text)
            size = 0 if text is None else len(text)
            merge = (kind == "auto" and last is not None and last["kind"] == "auto" and last["hash"] is not None
                     and last["authors"] == names and now - last["started"] < COALESCE
                     and not con.execute("SELECT 1 FROM versions WHERE id > ? AND kind IN ('label', 'restore')",
                                         (last["id"],)).fetchone())
            if merge:
                con.execute("UPDATE versions SET time = ?, hash = ?, size = ? WHERE id = ?", (now, h, size, last["id"]))
                self._collect(con)
                self._prune(con)
                return None
            charge(ROW_BYTES + len(path) + len(names) + len(label or ""))
            cur = con.execute(
                "INSERT INTO versions (time, started, path, hash, size, kind, authors, label) VALUES (?,?,?,?,?,?,?,?)",
                (now, now, path, h, size, kind, names, label))
            self._thin(con, now)
            self._prune(con)
            return cur.lastrowid

        return self._write(work)

    def label(self, name: str, authors, texts: dict[str, str], now: float | None = None, role: str = "owner") -> int:
        """Name the current state of the project (texts: every text file's content). Raises Refused past the caps."""
        now = time.time() if now is None else now

        def work(con, put, charge):
            total, shared = con.execute("SELECT COUNT(*), COALESCE(SUM(role != 'owner'), 0) FROM versions "
                                        "WHERE kind = 'label'").fetchone()
            if total >= MAX_LABELS or (role != "owner" and shared >= MAX_SHARED_LABELS):
                raise Refused(f"This document has {total} named versions, the most it keeps.")
            manifest = {path: put(text) for path, text in sorted(texts.items())}
            charge(ROW_BYTES + len(json.dumps(manifest)) + len(name))
            cur = con.execute(
                "INSERT INTO versions (time, started, path, hash, size, kind, authors, label, manifest, role) "
                "VALUES (?, ?, NULL, NULL, ?, 'label', ?, ?, ?, ?)",
                (now, now, sum(len(t) for t in texts.values()), json.dumps(sorted({str(a)[:80] for a in authors})),
                 name, json.dumps(manifest), role))
            self._prune(con)
            return cur.lastrowid

        return self._write(work)

    def _prune(self, con: sqlite3.Connection) -> None:
        """
        Keep MAX_ROWS file versions and max_bytes of blobs by dropping the oldest superseded versions (a version
        with a newer one holding text). Labels, each file's newest text and the text before a deletion always stay;
        when that is not enough, raise Full (the caller's transaction rolls back).
        """
        while (con.execute("SELECT COUNT(*) FROM versions WHERE kind != 'label'").fetchone()[0] > MAX_ROWS
               or con.execute("SELECT COALESCE(SUM(bytes), 0) FROM blobs").fetchone()[0] > self.max_bytes):
            victims = [r[0] for r in con.execute(
                "SELECT v.id FROM versions v WHERE v.path IS NOT NULL AND EXISTS (SELECT 1 FROM versions w "
                "WHERE w.path = v.path AND w.id > v.id AND w.hash IS NOT NULL) ORDER BY v.id LIMIT 50")]
            if not victims:
                raise Full("history is full")
            con.execute(f"DELETE FROM versions WHERE id IN ({','.join('?' * len(victims))})", victims)
            self._collect(con)

    @staticmethod
    def _kept(rows: list) -> set:
        """Ids never thinned or cleared: each file's newest row, and the last text before a deletion or rename."""
        keep = set()
        for i, r in enumerate(rows):  # rows ordered by path, id
            newest = i + 1 == len(rows) or rows[i + 1]["path"] != r["path"]
            if newest or (r["hash"] is not None and rows[i + 1]["hash"] is None):
                keep.add(r["id"])
        return keep

    def _thin(self, con: sqlite3.Connection, now: float) -> None:
        """
        Thin unnamed versions by age (THIN): in each bucket of one file the newest version stays. Deletion and rename
        markers stay (they say a file was gone). At most THIN_BATCH rows go per call; the rest go on later records.
        """
        rows = con.execute(FILE_ROWS).fetchall()
        keep, seen, victims = self._kept(rows), set(), []
        for r in reversed(rows):  # newest first, so each bucket keeps its newest version
            step = next(s for limit, s in THIN if now - r["time"] < limit)
            if not step or r["hash"] is None:
                continue
            key = (r["path"], step, int(r["time"] // step))
            if r["id"] not in keep and key in seen:
                victims.append(r["id"])
                if len(victims) >= THIN_BATCH:
                    break
            seen.add(key)
        if victims:
            con.execute(f"DELETE FROM versions WHERE id IN ({','.join('?' * len(victims))})", victims)
            self._collect(con)

    def _collect(self, con: sqlite3.Connection) -> None:
        """Forget blobs no row, label manifest or pending restore refers to; their files go once the transaction
        commits."""
        used = {r[0] for r in con.execute("SELECT hash FROM versions WHERE hash IS NOT NULL UNION "
                                          "SELECT hash FROM pending UNION SELECT base FROM pending")}
        for (manifest,) in con.execute("SELECT manifest FROM versions WHERE manifest IS NOT NULL"):
            used.update(json.loads(manifest).values())
        for (h,) in con.execute("SELECT hash FROM blobs").fetchall():
            if h not in used:
                con.execute("DELETE FROM blobs WHERE hash = ?", (h,))
                self._doomed.append(h)

    # --- cleaning up (the local owner, or a workspace admin behind the gateway) --------------------------------

    def drop_label(self, vid: int) -> bool:
        """Delete a named version; its blobs go unless another version still uses them."""
        def work(con, put, charge):
            gone = con.execute("DELETE FROM versions WHERE id = ? AND kind = 'label'", (vid,)).rowcount
            self._collect(con)
            return bool(gone)

        return self._write(work, vacuum=True)

    def clear(self, before: float, named: bool = False) -> int:
        """
        Delete versions older than `before` (named ones only with named=True). Each file's newest version and the
        text before a deletion stay, so the current state and deleted files can still be restored. Returns the count.
        """
        def work(con, put, charge):
            rows = con.execute(FILE_ROWS).fetchall()
            keep = self._kept(rows)
            ids = [r[0] for r in con.execute("SELECT id FROM versions WHERE time < ? AND (path IS NOT NULL OR ?)",
                                              (before, int(named))) if r[0] not in keep]
            for at in range(0, len(ids), 500):
                chunk = ids[at:at + 500]
                con.execute(f"DELETE FROM versions WHERE id IN ({','.join('?' * len(chunk))})", chunk)
            self._collect(con)
            return len(ids)

        return self._write(work, vacuum=True)

    def usage(self) -> dict:
        with LOCK, closing(self._db()) as con:
            rows, labels = con.execute("SELECT COALESCE(SUM(kind != 'label'), 0), COALESCE(SUM(kind = 'label'), 0) "
                                       "FROM versions").fetchone()
            size = con.execute("SELECT COALESCE(SUM(bytes), 0) FROM blobs").fetchone()[0]
        return {"rows": rows, "labels": labels, "bytes": size, "max_rows": MAX_ROWS, "max_labels": MAX_LABELS,
                "max_bytes": self.max_bytes}

    # --- pending restores (files open in a co-editing room) ----------------------------------------------------

    def pend(self, path: str, text: str, token: str, base: str, now: float | None = None) -> None:
        """
        Remember that `path` must still go from `base` (its text on disk when the restore was asked for) to `text`.
        A newer restore of the same file replaces both but keeps a claim on it: the claimer may be applying the older
        one right now, and the next claim must see that first.
        """
        def work(con, put, charge):
            h, b = put(text), put(base)
            charge(ROW_BYTES + len(path))
            con.execute("INSERT INTO pending (path, hash, token, time, base) VALUES (?, ?, ?, ?, ?) ON CONFLICT(path) "
                        "DO UPDATE SET hash = excluded.hash, token = excluded.token, time = excluded.time, "
                        "base = excluded.base", (path, h, token, time.time() if now is None else now, b))
            self._collect(con)  # the text a replaced pending restore held

        self._write(work)

    def pending(self) -> list[dict]:
        if not (self.folder / "index.sqlite3").is_file():
            return []  # never create a store just to look
        with LOCK, closing(self._db()) as con:
            return [dict(r) for r in con.execute("SELECT * FROM pending ORDER BY path")]

    def claim(self, path: str, cid: str, alive, now: float | None = None) -> dict | None:
        """
        Claim the pending restore of `path` for client cid: one claimer at a time, so two editors never apply it
        concurrently. A claim lapses after CLAIM_TTL or once alive(claimer) is False. None: nothing pending, or
        another live client holds it. Returns {token, text, base} (base: the text the restore replaces, None when
        unknown).
        """
        now = time.time() if now is None else now
        with LOCK, closing(self._db()) as con:
            with con:
                r = con.execute("SELECT * FROM pending WHERE path = ?", (path,)).fetchone()
                if r is None or (r["claim"] and r["claim"] != cid and now - r["claimed"] < CLAIM_TTL
                                 and alive(r["claim"])):
                    return None
                con.execute("UPDATE pending SET claim = ?, claimed = ? WHERE path = ?", (cid, now, path))
            return {"token": r["token"], "text": self.blob(r["hash"]),
                    "base": self.blob(r["base"]) if r["base"] else None}

    def has(self, path: str, h: str) -> bool:
        """Whether some version of `path` holds the text with hash h (so overwriting that text loses nothing)."""
        with LOCK, closing(self._db()) as con:
            return con.execute("SELECT 1 FROM versions WHERE path = ? AND hash = ? LIMIT 1", (path, h)).fetchone() \
                is not None

    def release(self, path: str, cid: str) -> bool:
        """The claimer could not apply it (or went away): another editor may take it."""
        with LOCK, closing(self._db()) as con:
            with con:
                return bool(con.execute("UPDATE pending SET claim = NULL, claimed = NULL WHERE path = ? AND claim = ?",
                                        (path, cid)).rowcount)

    def finish(self, path: str, token: str, cid: str | None = None) -> bool:
        """
        The restore reached the file: through the room by its claimer cid, or written by the server (cid None).
        True when that was the pending one; a claimer's older token (a newer restore came meanwhile) only ends its
        claim, so the newer text gets claimed and applied after it.
        """
        def work(con, put, charge):
            sql, args = "DELETE FROM pending WHERE path = ? AND token = ?", [path, token]
            if cid is not None:
                sql, args = sql + " AND claim = ?", [*args, cid]
            gone = con.execute(sql, args).rowcount
            if not gone and cid is not None:
                con.execute("UPDATE pending SET claim = NULL, claimed = NULL WHERE path = ? AND claim = ?", (path, cid))
            self._collect(con)
            return bool(gone)

        return self._write(work)

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
    """
    Line diff a -> b as hunks of rows [op, old line, new line, text]; op is ' ', '-' or '+'. The common start and end
    are cut first; the matcher (quadratic at worst) sees at most MAX_DIFF_LINES lines of each changed middle, and
    "truncated" says when that or MAX_DIFF_ROWS cut the diff short.
    """
    old, new = a.split("\n"), b.split("\n")
    head = 0
    while head < min(len(old), len(new)) and old[head] == new[head]:
        head += 1
    tail = 0
    while tail < min(len(old), len(new)) - head and old[-1 - tail] == new[-1 - tail]:
        tail += 1
    start = max(0, head - context)
    a_mid, b_mid = old[start:len(old) - max(0, tail - context)], new[start:len(new) - max(0, tail - context)]
    cut = len(a_mid) > MAX_DIFF_LINES + 2 * context or len(b_mid) > MAX_DIFF_LINES + 2 * context
    a_mid, b_mid = a_mid[:MAX_DIFF_LINES + 2 * context], b_mid[:MAX_DIFF_LINES + 2 * context]
    matcher = difflib.SequenceMatcher(None, a_mid, b_mid)  # autojunk: very common lines anchor nothing (fast)
    hunks, added, removed, rows_out = [], 0, 0, 0
    for group in matcher.get_grouped_opcodes(context):
        rows: list[list] = []
        for tag, i1, i2, j1, j2 in group:
            if tag == "equal":
                rows += [[" ", start + i + 1, start + j1 + (i - i1) + 1, a_mid[i]] for i in range(i1, i2)]
                continue
            if tag in ("replace", "delete"):
                rows += [["-", start + i + 1, None, a_mid[i]] for i in range(i1, i2)]
                removed += i2 - i1
            if tag in ("replace", "insert"):
                rows += [["+", None, start + j + 1, b_mid[j]] for j in range(j1, j2)]
                added += j2 - j1
        if rows_out + len(rows) > MAX_DIFF_ROWS:
            cut = True
            break
        rows_out += len(rows)
        hunks.append(rows)
    return {"hunks": hunks, "added": added, "removed": removed, "truncated": cut}
