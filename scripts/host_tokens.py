"""
Personal API tokens (host.py): for scripts, the VS Code extension and the compile API, which cannot sign in with a
browser session. One token system for all of them.

A token is made on the Account page, shown once, stored only as its SHA-256 (`api_tokens` in host.db), carries the
scopes picked when it was made (SCOPES), may expire and is revocable. It works only as `Authorization: Bearer
lpat_...`, never with cookies, so no browser sends it on its own (no CSRF, hence no Origin check for it):
- scope "projects": the project proxy (/p/<id>/...) of the projects picked for it and GET /api/token. Each request
  uses the person's current role in the project (removed or demoted people lose access at once) and never more than
  the token's projects. Not with a token: WebSockets, the AI assistant, Zotero and /api/mcp (they stay with the
  browser session or the MCP grant).
- scope "compile": only /api/v1/* (compile_api.py), and only while [api] is enabled.
A token without the scope a route needs is refused (403). A disabled account, a password change or reset ends all
of the person's tokens. Wrong tokens are throttled per address. Stdlib only, Python 3.9.
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
MAX_DAYS = 366
SCOPES = {"projects": "Open the chosen projects (VS Code extension, scripts)",
          "compile": "Compile documents with POST /api/v1/compile"}
NOT_WITH_TOKEN = ("/api/ai", "/api/zotero", "/api/zotero/fetch", "/api/zotero/preview")


def compile_enabled() -> bool:
    return bool(H.APP.config.get("api", {}).get("enabled"))


def scopes_offered() -> dict:
    return {k: v for k, v in SCOPES.items() if k != "compile" or compile_enabled()}


def bearer(h, scope: str) -> dict | None:
    """
    The session-like user for an `Authorization: Bearer` token with this scope, None without the header; an
    unknown, expired or revoked token is a 401 (throttled per address), a token without the scope a 403.
    """
    header = h.headers.get("Authorization") or ""
    if not header.lower().startswith("bearer "):
        return None
    guard = H.APP.api_token_tries
    if guard.full(h.ip_group):
        raise H.HttpError(429, "Too many failed attempts; wait a while.", {"Retry-After": "900"})
    token = header[7:].strip()
    row = H.APP.db.one("SELECT t.id AS token_id, t.projects AS token_projects, t.scopes AS token_scopes, "
                       "t.expires AS token_expires, t.used AS token_used, u.* FROM api_tokens t "
                       "JOIN users u ON u.id = t.user_id WHERE t.token_hash = ?",
                       H.token_hash(token)) if token.startswith(PREFIX) and len(token) <= 100 else None
    if not row or row["disabled"] or (row["token_expires"] and row["token_expires"] < time.time()):
        guard.fail(h.ip_group)
        raise H.HttpError(401, "Unknown, expired or revoked API token.", {"WWW-Authenticate": 'Bearer realm="api"'})
    if scope not in row["token_scopes"].split():
        raise H.HttpError(403, f"This API token lacks the {scope} scope.")
    if not row["token_used"] or time.time() - row["token_used"] > 60:
        H.APP.db.run("UPDATE api_tokens SET used = ? WHERE id = ?", time.time(), row["token_id"])
    return {**row, "token_projects": json.loads(row["token_projects"]), "stage": "full", "id_hash": None,
            "csrf": "", "api_token": True}


def project_url(pid: str) -> str:
    return f"{H.APP.config['public_url']}/p/{pid}/"


def api_list(h) -> None:
    rows = H.APP.db.all("SELECT id, name, projects, scopes, created, expires, used FROM api_tokens "
                        "WHERE user_id = ? ORDER BY created DESC", h.user["id"])
    for row in rows:
        ids = json.loads(row.pop("projects"))
        names = {p["id"]: p["name"] for p in H.APP.db.all(
            f"SELECT id, name FROM projects WHERE id IN ({','.join('?' * len(ids))})", *ids)} if ids else {}
        row["projects"] = [{"id": pid, "name": names.get(pid, "(deleted)")} for pid in ids]
        row["scopes"] = row["scopes"].split()
    h.ok({"tokens": rows, "scopes": scopes_offered(), "max": MAX_PER_USER,
          "compile_url": H.APP.config["public_url"] + "/api/v1/compile" if compile_enabled() else None})


def api_create(h) -> None:
    data = h.json_body()
    name = H.clean_name(data.get("name"), "Token name")
    scopes = data.get("scopes", ["projects"])
    if not isinstance(scopes, list) or not scopes or any(s not in scopes_offered() for s in scopes):
        raise H.HttpError(400, f"Scopes are some of: {', '.join(scopes_offered())}.")
    pids = data.get("projects") or []
    if "projects" in scopes:
        if not isinstance(pids, list) or not 1 <= len(pids) <= MAX_PROJECTS \
                or not all(isinstance(p, str) and re.fullmatch(r"[0-9a-f]{16}", p) for p in pids):
            raise H.HttpError(400, "Pick the projects this token may open.")
        for pid in pids:
            if not H.project_access(H.APP, h.user, pid):
                raise H.HttpError(404, "No such project.")
    elif pids:
        raise H.HttpError(400, "Projects go with the projects scope.")
    days = data.get("days", 90)
    if days is not None and not (isinstance(days, int) and not isinstance(days, bool) and 1 <= days <= MAX_DAYS):
        raise H.HttpError(400, f"days is a whole number from 1 to {MAX_DAYS}, or null for no expiry.")
    now = time.time()
    with H.APP.db.tx():
        if H.APP.db.one("SELECT COUNT(*) AS n FROM api_tokens WHERE user_id = ?", h.user["id"])["n"] >= MAX_PER_USER:
            raise H.HttpError(403, f"You have {MAX_PER_USER} tokens already. Revoke one first.")
        token, tid = PREFIX + secrets.token_urlsafe(32), secrets.token_hex(8)
        H.APP.db.run("INSERT INTO api_tokens(id, user_id, name, token_hash, projects, scopes, created, expires) "
                     "VALUES (?, ?, ?, ?, ?, ?, ?, ?)", tid, h.user["id"], name, H.token_hash(token),
                     json.dumps(sorted(set(pids))), " ".join(sorted(set(scopes))), now,
                     now + days * 86400 if days else None)
    H.audit_log(H.APP, "api_token_created", h.user["id"], h.ip,
                detail=f"{tid} {name} [{' '.join(sorted(set(scopes)))}]: {' '.join(sorted(set(pids)))}")
    h.ok({"id": tid, "name": name, "scopes": sorted(set(scopes)), "expires": now + days * 86400 if days else None,
          "token": token}, 201)  # the only time the token is shown


def api_revoke(h, tid: str) -> None:
    if not H.APP.db.change("DELETE FROM api_tokens WHERE id = ? AND user_id = ?", tid, h.user["id"]):
        raise H.HttpError(404, "No such token.")
    H.audit_log(H.APP, "api_token_revoked", h.user["id"], h.ip, detail=tid)
    h.ok()


def api_whoami(h) -> None:
    """GET /api/token with a projects token: who it belongs to and the projects it opens (that they can still open)."""
    user = bearer(h, "projects")
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
