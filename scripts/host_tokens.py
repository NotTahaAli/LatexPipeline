"""
Personal API tokens (host.py): for scripts and the VS Code extension, which cannot sign in with a browser session.

A token is made on the Account page, shown once, stored only as its SHA-256 (`api_tokens` in host.db), scoped to the
projects picked when it was made and revocable. It works only as `Authorization: Bearer lpat_...` on the project
proxy (/p/<id>/...) and on GET /api/token, never with cookies, so no browser sends it on its own (no CSRF, hence no
Origin check for it). Each request uses the person's current role in the project (removed or demoted people lose
access at once) and never more than the token's projects; a disabled account, a password change or reset ends all
of its tokens. Not with a token: WebSockets, the AI assistant and Zotero (they stay with the browser session).
Stdlib only, Python 3.9.
"""

from __future__ import annotations

import json
import re
import secrets
import time

H = None  # the host module, set by install()

PREFIX = "lpat_"
MAX_PER_USER = 20
MAX_PROJECTS = 50
NOT_WITH_TOKEN = ("/api/ai", "/api/zotero", "/api/zotero/fetch", "/api/zotero/preview")


def bearer(h) -> dict | None:
    """The session-like user for an `Authorization: Bearer` token, None without one; a bad token is a 401."""
    header = h.headers.get("Authorization") or ""
    if not header.startswith("Bearer "):
        return None
    token = header[7:].strip()
    if not token.startswith(PREFIX) or len(token) > 100:
        raise H.HttpError(401, "Unknown or revoked API token.")
    row = H.APP.db.one("SELECT t.id AS token_id, t.projects AS token_projects, t.used AS token_used, u.* "
                       "FROM api_tokens t JOIN users u ON u.id = t.user_id WHERE t.token_hash = ?", H.token_hash(token))
    if not row or row["disabled"]:
        raise H.HttpError(401, "Unknown or revoked API token.")
    if not row["token_used"] or time.time() - row["token_used"] > 60:
        H.APP.db.run("UPDATE api_tokens SET used = ? WHERE id = ?", time.time(), row["token_id"])
    return {**row, "token_projects": json.loads(row["token_projects"]), "stage": "full", "id_hash": None,
            "csrf": "", "api_token": True}


def project_url(pid: str) -> str:
    return f"{H.APP.config['public_url']}/p/{pid}/"


def api_list(h) -> None:
    rows = H.APP.db.all("SELECT id, name, projects, created, used FROM api_tokens WHERE user_id = ? "
                        "ORDER BY created DESC", h.user["id"])
    for row in rows:
        ids = json.loads(row.pop("projects"))
        names = {p["id"]: p["name"] for p in H.APP.db.all(
            f"SELECT id, name FROM projects WHERE id IN ({','.join('?' * len(ids))})", *ids)} if ids else {}
        row["projects"] = [{"id": pid, "name": names.get(pid, "(deleted)")} for pid in ids]
    h.ok({"tokens": rows})


def api_create(h) -> None:
    data = h.json_body()
    name = H.clean_name(data.get("name"), "Token name")
    pids = data.get("projects")
    if not isinstance(pids, list) or not 1 <= len(pids) <= MAX_PROJECTS \
            or not all(isinstance(p, str) and re.fullmatch(r"[0-9a-f]{16}", p) for p in pids):
        raise H.HttpError(400, "Pick the projects this token may open.")
    for pid in pids:
        if not H.project_access(H.APP, h.user, pid):
            raise H.HttpError(404, "No such project.")
    with H.APP.db.tx():
        if H.APP.db.one("SELECT COUNT(*) AS n FROM api_tokens WHERE user_id = ?", h.user["id"])["n"] >= MAX_PER_USER:
            raise H.HttpError(403, f"You have {MAX_PER_USER} tokens already. Revoke one first.")
        token, tid = PREFIX + secrets.token_urlsafe(32), secrets.token_hex(8)
        H.APP.db.run("INSERT INTO api_tokens(id, user_id, name, token_hash, projects, created) "
                     "VALUES (?, ?, ?, ?, ?, ?)", tid, h.user["id"], name, H.token_hash(token),
                     json.dumps(sorted(set(pids))), time.time())
    H.audit_log(H.APP, "api_token_created", h.user["id"], h.ip, detail=f"{tid} {name}: {' '.join(sorted(set(pids)))}")
    h.ok({"id": tid, "name": name, "token": token})  # the only time the token is shown


def api_revoke(h, tid: str) -> None:
    if not H.APP.db.change("DELETE FROM api_tokens WHERE id = ? AND user_id = ?", tid, h.user["id"]):
        raise H.HttpError(404, "No such token.")
    H.audit_log(H.APP, "api_token_revoked", h.user["id"], h.ip, detail=tid)
    h.ok()


def api_whoami(h) -> None:
    """GET /api/token with the token: who it belongs to and the projects it opens (that the person can still open)."""
    user = bearer(h)
    if user is None:
        raise H.HttpError(401, "Send an API token (Authorization: Bearer ...).")
    projects = []
    for pid in user["token_projects"]:
        found = H.project_access(H.APP, user, pid)
        if found:
            projects.append({"id": pid, "name": found[0]["name"], "url": project_url(pid),
                             "role": H.worker_role(found[1])})
    h.ok({"name": user["name"], "projects": projects})


def install(module) -> None:
    global H
    H = module
    module.route("GET", r"/api/account/tokens")(api_list)
    module.route("POST", r"/api/account/tokens")(api_create)
    module.route("DELETE", r"/api/account/tokens/([0-9a-f]{16})")(api_revoke)
    module.route("GET", r"/api/token", "anon")(api_whoami)
