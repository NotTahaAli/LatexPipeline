"""
MCP for hosted projects (host.py): an OAuth 2.1 authorization server and the Streamable HTTP endpoint /mcp, so
people can work on their projects from their own AI client (Claude, ChatGPT, Codex, Cursor, VS Code, Gemini CLI,
Windsurf ...) with their own subscription. The gateway makes no AI calls for this. Stdlib only, Python 3.9+.

host.py imports this module, has its tables in MIGRATIONS, calls endpoint() from Handler.dispatch for /mcp,
/oauth/* and /.well-known/oauth-*, and install() registers the signed-in API (consent, connected clients).

- Discovery: protected resource metadata (RFC 9728) for <public_url>/mcp, authorization server metadata (RFC 8414,
  issuer = public_url). Clients register with Dynamic Client Registration (RFC 7591): public clients only
  (token_endpoint_auth_method none), redirect URIs https, loopback http (any port, RFC 8252) or a private-use app
  scheme (cursor://, vscode://).
- /oauth/authorize checks the request, then the person signs in with the normal pages (password, 2FA, providers)
  and sees a consent page (#oauth=<id>) to pick workspaces or projects and read / write / review. PKCE S256 is
  required, codes are single-use (a second use revokes the grant) and live 2 minutes, `state` is echoed and `iss`
  (RFC 9207) added.
- Tokens are opaque, stored as SHA-256 hashes: access tokens last an hour and are bound to the /mcp resource
  (RFC 8707); refresh tokens rotate on every use and a reused one revokes the whole grant.
- /mcp takes only a bearer token (cookies are ignored), refuses a foreign Origin, and runs each tool with the
  person's role in that workspace narrowed by the grant: list_projects/search/fetch in the gateway (reading the
  project folder), everything else in the project's worker (POST /api/mcp, X-Host-* like the proxy).

Threat model as in host.py: every signed-in user, the AI client and the model are untrusted.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import http.client
import json
import re
import secrets
import time
from urllib.parse import parse_qs, quote, urlencode, urlsplit

import mcp_tools

H = None  # the host module, set by install()

# The tables are the MIGRATIONS entry "AI clients over MCP" in host.py.
DEFAULTS = {"enabled": False, "calls_per_minute": 60, "registrations_per_ip_hour": 20, "max_clients": 5000}
RANGES = {"calls_per_minute": (1, 10000), "registrations_per_ip_hour": (1, 10000), "max_clients": (10, 10 ** 6)}
CONFIG_TEMPLATE = """
# Connect AI clients (MCP). Off unless enabled. People connect Claude, ChatGPT, Cursor, VS Code, Codex or Gemini CLI
# to <public_url>/mcp with their own subscription: they sign in here and choose projects and permissions.
# [mcp]
# enabled = true
# calls_per_minute = 60              # tool calls per connected client
# registrations_per_ip_hour = 20     # new client registrations (RFC 7591) per address
# max_clients = 5000                 # registered clients kept at most (unused ones are deleted after a day)
"""
ACCESS_SECONDS = 3600
REFRESH_SECONDS = 30 * 86400
CODE_SECONDS = 120
REQUEST_SECONDS = 600
MAX_GRANTS_PER_USER = 50
MAX_BODY = 8 * 1024 * 1024
LOOPBACK = {"127.0.0.1", "localhost", "::1"}
BAD_SCHEMES = {"javascript", "data", "file", "vbscript", "blob", "about", "ftp", "ws", "wss", "view-source",
               "filesystem", "intent", "chrome", "jar", "mailto", "tel", "sms"}
SCOPE_TEXT = {"read": "Read files, outline, build results and PDF pages",
              "write": "Change files and run builds", "review": "Add comments and suggested edits"}
WRITE_TOOLS = {t["name"] for t in mcp_tools.TOOLS if t["_scope"] != "read"}
THROTTLES: dict = {}


def check_config(config: dict, path) -> None:
    """Validate [mcp] in config.toml (called by host.load_config); fills in the defaults."""
    found = config.get("mcp", {})
    if not isinstance(found, dict) or set(found) - set(DEFAULTS):
        raise H.ConfigError(f"{path}: [mcp] takes only {', '.join(DEFAULTS)}")
    config["mcp"] = cfg = {**DEFAULTS, **found}
    if not isinstance(cfg["enabled"], bool):
        raise H.ConfigError(f"{path}: [mcp] enabled must be true or false")
    for key, (low, high) in RANGES.items():
        if not (isinstance(cfg[key], int) and not isinstance(cfg[key], bool) and low <= cfg[key] <= high):
            raise H.ConfigError(f"{path}: [mcp] {key} must be a whole number from {low} to {high}")


def cfg() -> dict:
    return {**DEFAULTS, **H.APP.config.get("mcp", {})}


def throttle(name: str, limit: int, window: float):
    key = (name, limit, window)
    if key not in THROTTLES:
        THROTTLES[key] = H.Throttle(limit, window)
    return THROTTLES[key]


def issuer() -> str:
    return H.APP.config["public_url"]


def resource() -> str:
    return issuer() + "/mcp"


def metadata_url() -> str:
    return issuer() + "/.well-known/oauth-protected-resource/mcp"


def same_resource(value: str) -> bool:
    """RFC 8707 resource: our /mcp URL (scheme and host case-insensitive, one trailing slash allowed)."""
    def norm(url: str) -> str:
        parts = urlsplit(url)
        return f"{parts.scheme.lower()}://{parts.netloc.lower()}{parts.path.rstrip('/')}"
    parts = urlsplit(value or "")
    return not (parts.fragment or parts.query) and hmac.compare_digest(norm(value or ""), norm(resource()))


# ---------------------------------------------------------------------------
# Redirect URIs
# ---------------------------------------------------------------------------

def redirect_kind(uri) -> str:
    """web (https), loopback (http to this computer, any port) or app (a private-use scheme); raises ValueError."""
    if not isinstance(uri, str) or not 0 < len(uri) <= 2000 or re.search(r"[\x00-\x20\x7f#\\]", uri):
        raise ValueError("Redirect URIs are absolute URLs without spaces or a #fragment.")
    parts = urlsplit(uri)
    scheme = parts.scheme.lower()
    if scheme == "https":
        if not parts.hostname or "@" in parts.netloc:
            raise ValueError("An https redirect URI needs a host (and no user name).")
        return "web"
    if scheme == "http":
        if (parts.hostname or "").lower() not in LOOPBACK or "@" in parts.netloc:
            raise ValueError("http redirect URIs must point to this computer (127.0.0.1, [::1] or localhost); "
                             "use https otherwise.")
        return "loopback"
    if re.fullmatch(r"[a-z][a-z0-9+.-]{1,62}", scheme) and scheme not in BAD_SCHEMES and ":" in uri:
        return "app"
    raise ValueError(f"Redirect URIs with {scheme or 'no'} scheme are not allowed.")


def redirect_matches(registered: str, sent: str) -> bool:
    """Exact match, except that loopback http URIs match on any port (RFC 8252 7.3; clients pick a free port)."""
    if registered == sent:
        return True
    a, b = urlsplit(registered), urlsplit(sent)
    try:
        if redirect_kind(registered) != "loopback" or redirect_kind(sent) != "loopback":
            return False
        a.port, b.port  # noqa: B018 - raises ValueError for a bad port
    except ValueError:
        return False
    return (a.scheme.lower(), (a.hostname or "").lower(), a.path, a.query) == (
        b.scheme.lower(), (b.hostname or "").lower(), b.path, b.query)


def redirect_label(uri: str) -> str:
    kind, parts = redirect_kind(uri), urlsplit(uri)
    if kind == "web":
        return parts.hostname
    if kind == "loopback":
        return "an app on your computer (http://" + (parts.hostname or "") + ")"
    return f"the app that handles {parts.scheme}:// links on your computer"


def with_params(uri: str, params: dict) -> str:
    return uri + ("&" if "?" in uri else "?") + urlencode({k: v for k, v in params.items() if v is not None})


# ---------------------------------------------------------------------------
# HTTP entry point (anonymous / bearer endpoints; called from Handler.dispatch before cookies matter)
# ---------------------------------------------------------------------------

def endpoint(h, path: str) -> bool:
    if not (path == "/mcp" or path.startswith(("/oauth/", "/.well-known/oauth-"))):
        return False
    if not cfg()["enabled"]:
        raise H.HttpError(404, "Not found.")
    routes = {
        ("GET", "/.well-known/oauth-protected-resource"): protected_resource,
        ("GET", "/.well-known/oauth-protected-resource/mcp"): protected_resource,
        ("GET", "/.well-known/oauth-authorization-server"): server_metadata,
        ("GET", "/oauth/authorize"): authorize,
        ("POST", "/oauth/register"): register,
        ("POST", "/oauth/token"): token,
        ("POST", "/oauth/revoke"): revoke,
    }
    if path == "/mcp":
        mcp(h)
    elif (h.command, path) in routes:
        routes[(h.command, path)](h)
    elif any(p == path for _, p in routes):
        raise H.HttpError(405, "Method not allowed.")
    else:
        raise H.HttpError(404, "Not found.")
    return True


def protected_resource(h) -> None:
    h.send_json({"resource": resource(), "authorization_servers": [issuer()],
                 "scopes_supported": list(mcp_tools.SCOPES), "bearer_methods_supported": ["header"],
                 "resource_name": H.APP.config["site_name"]})


def server_metadata(h) -> None:
    base = issuer()
    h.send_json({
        "issuer": base, "authorization_endpoint": base + "/oauth/authorize", "token_endpoint": base + "/oauth/token",
        "registration_endpoint": base + "/oauth/register", "revocation_endpoint": base + "/oauth/revoke",
        "response_types_supported": ["code"], "grant_types_supported": ["authorization_code", "refresh_token"],
        "code_challenge_methods_supported": ["S256"], "token_endpoint_auth_methods_supported": ["none"],
        "revocation_endpoint_auth_methods_supported": ["none"], "scopes_supported": list(mcp_tools.SCOPES),
        "authorization_response_iss_parameter_supported": True, "response_modes_supported": ["query"],
    })


def form_body(h) -> dict:
    size = h.body_size(64 * 1024)
    if "application/x-www-form-urlencoded" not in h.headers.get("Content-Type", ""):
        h.read_exact(size)
        raise OAuthError(400, "invalid_request", "Send application/x-www-form-urlencoded.")
    raw = parse_qs(h.read_exact(size).decode("utf-8", "replace"), keep_blank_values=True)
    if any(len(v) > 1 for v in raw.values()):
        raise OAuthError(400, "invalid_request", "A parameter was given twice.")
    data = {k: v[0] for k, v in raw.items()}
    auth = h.headers.get("Authorization", "")
    if auth.lower().startswith("basic "):  # a client_secret_basic client: we use only its client_id
        try:
            data.setdefault("client_id", base64.b64decode(auth[6:]).decode().partition(":")[0])
        except (ValueError, UnicodeDecodeError):
            raise OAuthError(401, "invalid_client", "Bad Basic authorization.")
    return data


class OAuthError(Exception):
    def __init__(self, status: int, code: str, description: str) -> None:
        super().__init__(description)
        self.status, self.code, self.description = status, code, description


def oauth_json(h, fn) -> None:
    try:
        data = fn()
        status = 201 if "client_id_issued_at" in data else 200
    except OAuthError as exc:
        data, status = {"error": exc.code, "error_description": exc.description}, exc.status
    h.send_json(data, status, [("Pragma", "no-cache")])


# --- registration (RFC 7591) ----------------------------------------------------------------------------------------

def register(h) -> None:
    def run() -> dict:
        db, settings = H.APP.db, cfg()
        if throttle("register", settings["registrations_per_ip_hour"], 3600).full(h.ip_group):
            raise OAuthError(429, "invalid_client_metadata", "Too many registrations from your network; try later.")
        try:
            data = h.json_body()
        except H.HttpError as exc:
            raise OAuthError(400, "invalid_client_metadata", exc.message)
        uris = data.get("redirect_uris")
        if not isinstance(uris, list) or not 1 <= len(uris) <= 10:
            raise OAuthError(400, "invalid_redirect_uri", "Give 1 to 10 redirect_uris.")
        for uri in uris:
            try:
                redirect_kind(uri)
            except ValueError as exc:
                raise OAuthError(400, "invalid_redirect_uri", str(exc))
        grants = data.get("grant_types", ["authorization_code"])
        if not isinstance(grants, list) or not set(grants) <= {"authorization_code", "refresh_token"} \
                or "authorization_code" not in grants:
            raise OAuthError(400, "invalid_client_metadata", "Only authorization_code and refresh_token grants.")
        if data.get("response_types", ["code"]) != ["code"]:
            raise OAuthError(400, "invalid_client_metadata", "Only the code response type.")
        name = mcp_tools.client_label({"name": data.get("client_name")}, "Unnamed AI client")
        throttle("register", settings["registrations_per_ip_hour"], 3600).fail(h.ip_group)
        if db.one("SELECT COUNT(*) AS n FROM oauth_clients")["n"] >= settings["max_clients"]:
            cleanup(stale_clients=60.0)
            if db.one("SELECT COUNT(*) AS n FROM oauth_clients")["n"] >= settings["max_clients"]:
                raise OAuthError(503, "temporarily_unavailable", "Too many registered clients; try again later.")
        cid = "mcp_" + secrets.token_hex(16)
        now = time.time()
        db.run("INSERT INTO oauth_clients VALUES (?, ?, ?, ?, ?)", cid, name, json.dumps(uris), now, h.ip)
        hosts = ", ".join(f"{urlsplit(u).scheme}://{urlsplit(u).hostname or ''}" for u in uris)
        H.audit_log(H.APP, "mcp_client_registered", None, h.ip, detail=f"{name}: {hosts}")
        return {"client_id": cid, "client_id_issued_at": int(now), "client_name": name, "redirect_uris": uris,
                "grant_types": ["authorization_code", "refresh_token"], "response_types": ["code"],
                "token_endpoint_auth_method": "none"}
    oauth_json(h, run)


# --- authorization ----------------------------------------------------------------------------------------------

def authorize(h) -> None:
    query = h.query
    if any(len(v) > 1 for v in query.values()):
        h.redirect("/#oauth-error=" + quote("The app sent a malformed request (a parameter twice)."))
        return
    q = {k: v[0] for k, v in query.items()}
    client = H.APP.db.one("SELECT * FROM oauth_clients WHERE id = ?", q.get("client_id", "")[:100])
    if not client:
        h.redirect("/#oauth-error=" + quote("This app is not registered here (or its registration expired). "
                                            "Remove the connection in the app and add it again."))
        return
    registered = json.loads(client["redirect_uris"])
    sent = q.get("redirect_uri") or (registered[0] if len(registered) == 1 else "")
    if not any(redirect_matches(r, sent) for r in registered):
        h.redirect("/#oauth-error=" + quote("The app asked to return to an address it did not register."))
        return
    state = q.get("state")

    def fail(code: str, text: str) -> None:
        h.redirect(with_params(sent, {"error": code, "error_description": text, "state": state, "iss": issuer()}))

    if q.get("response_type") != "code":
        return fail("unsupported_response_type", "Only response_type=code.")
    challenge = q.get("code_challenge", "")
    if q.get("code_challenge_method") != "S256" or not re.fullmatch(r"[A-Za-z0-9_-]{43}", challenge):
        return fail("invalid_request", "PKCE with code_challenge_method=S256 is required.")
    if "resource" in q and not same_resource(q["resource"]):
        return fail("invalid_target", f"This server issues tokens only for {resource()}.")
    if state is not None and len(state) > 1000:
        return fail("invalid_request", "state is too long.")
    if throttle("authorize", 60, 600).full(h.ip_group):
        return fail("temporarily_unavailable", "Too many sign-in attempts from your network; try later.")
    throttle("authorize", 60, 600).fail(h.ip_group)
    asked = [s for s in (q.get("scope") or "").split() if s in mcp_tools.SCOPES] or list(mcp_tools.SCOPES)
    rid = secrets.token_urlsafe(24)
    H.APP.db.run("INSERT INTO oauth_requests VALUES (?, ?, ?, ?, ?, ?, ?)", H.token_hash(rid), client["id"], sent,
                 state, challenge, " ".join(sorted(set(asked) | {"read"})), time.time())
    h.redirect("/#oauth=" + rid)


def pending(rid: str) -> dict:
    row = H.APP.db.one("SELECT r.*, c.name AS client_name FROM oauth_requests r JOIN oauth_clients c "
                       "ON c.id = r.client_id WHERE r.id_hash = ?", H.token_hash(rid))
    if not row or row["created"] < time.time() - REQUEST_SECONDS:
        raise H.HttpError(410, "This connection request expired or was used already. Start again from the app.")
    return row


def member_workspaces(user: dict) -> list[dict]:
    """The workspaces the person is a member of (not the ones a site admin could reach as operator)."""
    tenants = H.APP.db.all("SELECT t.id, t.name, m.role FROM members m JOIN tenants t ON t.id = m.tenant_id "
                           "WHERE m.user_id = ? ORDER BY t.name", user["id"])
    for t in tenants:
        t["projects"] = H.APP.db.all("SELECT id, name FROM projects WHERE tenant_id = ? ORDER BY name", t["id"])
    return tenants


def need_enabled() -> None:
    if not cfg()["enabled"]:
        raise H.HttpError(404, "Not found.")


def api_request(h, rid: str) -> None:
    need_enabled()
    row = pending(rid)
    kind = redirect_kind(row["redirect_uri"])
    h.ok({"client": row["client_name"], "returns_to": redirect_label(row["redirect_uri"]), "native": kind != "web",
          "scopes": [{"id": s, "text": SCOPE_TEXT[s]} for s in mcp_tools.SCOPES if s in row["scope"].split()],
          "workspaces": member_workspaces(h.user), "site": H.APP.config["site_name"]})


def api_decide(h, rid: str) -> None:
    need_enabled()
    data = h.json_body()
    row = pending(rid)
    user = h.user
    if data.get("allow") is not True:
        if H.APP.db.change("DELETE FROM oauth_requests WHERE id_hash = ?", row["id_hash"]) != 1:
            raise H.HttpError(410, "This connection request was used already.")
        H.audit_log(H.APP, "mcp_denied", user["id"], h.ip, detail=row["client_name"])
        h.ok({"redirect": with_params(row["redirect_uri"], {"error": "access_denied", "state": row["state"],
                                                            "error_description": "The person declined.",
                                                            "iss": issuer()})})
        return
    asked = row["scope"].split()
    chosen = data.get("scopes") if isinstance(data.get("scopes"), list) else []
    scope = ["read"] + [s for s in ("write", "review") if s in chosen and s in asked]
    spaces = {t["id"]: t for t in member_workspaces(user)}
    tenants, projects = data.get("tenants") or [], data.get("projects") or []
    if not isinstance(tenants, list) or not isinstance(projects, list) or len(tenants) + len(projects) > 500:
        raise H.HttpError(400, "Bad selection.")
    known = {p["id"]: tid for tid, t in spaces.items() for p in t["projects"]}
    if any(t not in spaces for t in tenants) or any(p not in known for p in projects):
        raise H.HttpError(404, "No such workspace or project.")
    projects = [p for p in projects if known[p] not in tenants]
    if not tenants and not projects:
        raise H.HttpError(400, "Choose at least one workspace or project.")
    code, gid, now = secrets.token_urlsafe(32), secrets.token_hex(16), time.time()
    with H.APP.db.tx():
        if H.APP.db.change("DELETE FROM oauth_requests WHERE id_hash = ?", row["id_hash"]) != 1:
            raise H.HttpError(410, "This connection request was used already.")
        old = H.APP.db.all("SELECT id FROM oauth_grants WHERE user_id = ? ORDER BY COALESCE(used, created) DESC",
                           user["id"])
        for extra in old[MAX_GRANTS_PER_USER - 1:]:  # the least recently used connections go first
            H.APP.db.run("DELETE FROM oauth_grants WHERE id = ?", extra["id"])
        H.APP.db.run("INSERT INTO oauth_grants VALUES (?, ?, ?, ?, ?, ?, ?, NULL)", gid, user["id"],
                     row["client_id"], " ".join(scope), json.dumps(sorted(set(tenants))),
                     json.dumps(sorted(set(projects))), now)
        H.APP.db.run("INSERT INTO oauth_codes VALUES (?, ?, ?, ?, ?, 0)", H.token_hash(code), gid,
                     row["redirect_uri"], row["challenge"], now + CODE_SECONDS)
    H.audit_log(H.APP, "mcp_authorized", user["id"], h.ip,
                detail=f"{row['client_name']}: {' '.join(scope)}; {len(tenants)} workspace(s), "
                       f"{len(projects)} project(s)")
    h.ok({"redirect": with_params(row["redirect_uri"], {"code": code, "state": row["state"], "iss": issuer()})})


# --- tokens -----------------------------------------------------------------------------------------------------

def issue(gid: str) -> dict:
    access, refresh, now = "mcpa_" + secrets.token_urlsafe(32), "mcpr_" + secrets.token_urlsafe(32), time.time()
    H.APP.db.run("INSERT INTO oauth_tokens VALUES (?, ?, 'access', ?, ?, NULL)", H.token_hash(access), gid,
                 resource(), now + ACCESS_SECONDS)
    H.APP.db.run("INSERT INTO oauth_tokens VALUES (?, ?, 'refresh', ?, ?, NULL)", H.token_hash(refresh), gid,
                 resource(), now + REFRESH_SECONDS)
    scope = H.APP.db.one("SELECT scope FROM oauth_grants WHERE id = ?", gid)["scope"]
    return {"access_token": access, "token_type": "Bearer", "expires_in": ACCESS_SECONDS, "refresh_token": refresh,
            "scope": scope}


def revoke_grant(gid: str, why: str, user_id=None, ip=None) -> None:
    if H.APP.db.change("DELETE FROM oauth_grants WHERE id = ?", gid):
        H.audit_log(H.APP, why, user_id, ip, detail=gid)


def token(h) -> None:
    def run() -> dict:
        if throttle("token", 120, 60).full(h.ip_group):
            raise OAuthError(429, "slow_down", "Too many token requests; wait a minute.")
        throttle("token", 120, 60).fail(h.ip_group)
        data = form_body(h)
        client_id = data.get("client_id", "")
        if "resource" in data and not same_resource(data["resource"]):
            raise OAuthError(400, "invalid_target", f"This server issues tokens only for {resource()}.")
        kind = data.get("grant_type")
        db, now = H.APP.db, time.time()
        if kind in ("authorization_code", "refresh_token") and not db.one(
                "SELECT 1 FROM oauth_clients WHERE id = ?", client_id[:100]):
            raise OAuthError(401, "invalid_client", "Unknown client (its registration may have expired): register "
                                                    "again.")
        if kind == "authorization_code":
            with db.tx():
                row = db.one("SELECT c.*, g.client_id, g.user_id FROM oauth_codes c JOIN oauth_grants g "
                             "ON g.id = c.grant_id WHERE c.code_hash = ?", H.token_hash(data.get("code", "")))
                if row and not row["used"]:
                    db.run("UPDATE oauth_codes SET used = 1 WHERE code_hash = ?", row["code_hash"])
            if not row:
                raise OAuthError(400, "invalid_grant", "Unknown or expired code.")
            if row["used"]:  # a replayed code: whoever holds it, the tokens it gave are revoked
                revoke_grant(row["grant_id"], "mcp_code_reused", row["user_id"], h.ip)
                raise OAuthError(400, "invalid_grant", "This code was used already; the connection was revoked.")
            if row["expires"] < now or not hmac.compare_digest(row["client_id"], client_id):
                raise OAuthError(400, "invalid_grant", "The code expired or belongs to another client.")
            if "redirect_uri" in data and data["redirect_uri"] != row["redirect_uri"]:
                raise OAuthError(400, "invalid_grant", "redirect_uri differs from the authorization request.")
            verifier = data.get("code_verifier", "")
            if not re.fullmatch(r"[A-Za-z0-9._~-]{43,128}", verifier) or not hmac.compare_digest(
                    H.b64(hashlib.sha256(verifier.encode()).digest()), row["challenge"]):
                raise OAuthError(400, "invalid_grant", "PKCE check failed (code_verifier).")
            if not user_ok(row["user_id"]):
                raise OAuthError(400, "invalid_grant", "The account is disabled.")
            return issue(row["grant_id"])
        if kind == "refresh_token":
            with db.tx():
                row = db.one("SELECT t.*, g.client_id, g.user_id FROM oauth_tokens t JOIN oauth_grants g "
                             "ON g.id = t.grant_id WHERE t.token_hash = ? AND t.kind = 'refresh'",
                             H.token_hash(data.get("refresh_token", "")))
                if row and row["used"] is None:
                    db.run("UPDATE oauth_tokens SET used = ? WHERE token_hash = ?", now, row["token_hash"])
            if not row:
                raise OAuthError(400, "invalid_grant", "Unknown or expired refresh token.")
            if row["used"] is not None:  # rotation: an old refresh token came back, so one copy was stolen
                revoke_grant(row["grant_id"], "mcp_refresh_reused", row["user_id"], h.ip)
                raise OAuthError(400, "invalid_grant", "This refresh token was used already; the connection was "
                                                       "revoked. Connect again.")
            if row["expires"] < now or not hmac.compare_digest(row["client_id"], client_id):
                raise OAuthError(400, "invalid_grant", "The refresh token expired or belongs to another client.")
            if not user_ok(row["user_id"]):
                raise OAuthError(400, "invalid_grant", "The account is disabled.")
            return issue(row["grant_id"])
        raise OAuthError(400, "unsupported_grant_type", "Use authorization_code or refresh_token.")
    oauth_json(h, run)


def user_ok(uid) -> bool:
    user = H.APP.db.one("SELECT disabled FROM users WHERE id = ?", uid)
    return bool(user) and not user["disabled"]


def revoke(h) -> None:
    """RFC 7009: revoking any token of a connection ends the whole connection. Always 200."""
    def run() -> dict:
        data = form_body(h)
        row = H.APP.db.one("SELECT t.grant_id, g.client_id, g.user_id FROM oauth_tokens t JOIN oauth_grants g "
                           "ON g.id = t.grant_id WHERE t.token_hash = ?", H.token_hash(data.get("token", "")))
        if row and hmac.compare_digest(row["client_id"], data.get("client_id", "")):
            revoke_grant(row["grant_id"], "mcp_revoked", row["user_id"], h.ip)
        return {}
    oauth_json(h, run)


# ---------------------------------------------------------------------------
# /mcp: Streamable HTTP, bearer tokens only
# ---------------------------------------------------------------------------

def challenge(error: str | None = None) -> dict:
    extra = f'error="{error}", ' if error else ""
    return {"WWW-Authenticate": f'Bearer {extra}resource_metadata="{metadata_url()}", '
                                f'scope="{" ".join(mcp_tools.SCOPES)}"'}


def bearer(h) -> tuple[dict, dict]:
    """(token row joined with its grant and client, the user) for the request's bearer token, else 401."""
    auth = h.headers.get("Authorization", "")
    if not auth.lower().startswith("bearer "):
        raise H.HttpError(401, "Connect first: this endpoint needs an OAuth access token.", challenge())
    row = H.APP.db.one(
        "SELECT t.expires, t.resource, g.*, c.name AS client_name FROM oauth_tokens t JOIN oauth_grants g "
        "ON g.id = t.grant_id JOIN oauth_clients c ON c.id = g.client_id WHERE t.token_hash = ? AND t.kind = 'access'",
        H.token_hash(auth[7:].strip()))
    user = H.APP.db.one("SELECT * FROM users WHERE id = ?", row["user_id"]) if row else None
    if not row or row["expires"] < time.time() or row["resource"] != resource() or not user or user["disabled"]:
        raise H.HttpError(401, "The access token is invalid or expired.", challenge("invalid_token"))
    return row, user


def mcp(h) -> None:
    if h.command != "POST":
        raise H.HttpError(405, "POST JSON-RPC messages to this endpoint (no SSE stream, no sessions).",
                          {"Allow": "POST"})
    origin = h.headers.get("Origin")
    if origin is not None and not hmac.compare_digest(origin.rstrip("/"), issuer()):
        raise H.HttpError(403, "Requests from other web pages are refused.")
    grant, user = bearer(h)
    limit = throttle("mcp", cfg()["calls_per_minute"], 60)
    if limit.full(grant["id"]):
        raise H.HttpError(429, "Too many requests from this connection; wait a minute.", {"Retry-After": "30"})
    limit.fail(grant["id"])
    size = h.body_size(MAX_BODY)
    if "json" not in h.headers.get("Content-Type", ""):
        h.read_exact(size)
        raise H.HttpError(415, "Expected application/json.")
    try:
        message = json.loads(h.read_exact(size))
    except ValueError:
        h.send_json({"jsonrpc": "2.0", "id": None, "error": {"code": -32700, "message": "Parse error"}}, 400)
        return
    if not grant["used"] or grant["used"] < time.time() - 60:
        H.APP.db.run("UPDATE oauth_grants SET used = ? WHERE id = ?", time.time(), grant["id"])
    backend = Hosted(h, grant, user)
    headers = {"version": h.headers.get("MCP-Protocol-Version"), "method": h.headers.get("Mcp-Method"),
               "name": h.headers.get("Mcp-Name")}
    response, status, extra = mcp_tools.handle(message, backend, http=headers)
    if response is None:
        h.send(202, b"", "text/plain")
    else:
        h.send_json(response, status, extra)


class Hosted:
    """mcp_tools.handle's backend for one bearer token: its user, client and grant."""

    def __init__(self, h, grant: dict, user: dict) -> None:
        self.h, self.grant, self.user = h, grant, user
        self.scopes = grant["scope"].split()

    def tools(self) -> list[dict]:
        return mcp_tools.tools_for(self.scopes)

    def challenge(self, scope: str) -> dict:
        wanted = " ".join(s for s in mcp_tools.SCOPES if s in self.scopes or s == scope)
        return {"WWW-Authenticate": f'Bearer error="insufficient_scope", scope="{wanted}", '
                                    f'resource_metadata="{metadata_url()}", '
                                    f'error_description="This needs the {scope} permission"'}

    def projects(self) -> dict:
        """project id -> (project, effective role edit/view): granted, and reachable with the person's role now."""
        tenants, projects = json.loads(self.grant["tenants"]), json.loads(self.grant["projects"])
        marks = ",".join("?" * len(tenants)) or "NULL"
        rows = H.APP.db.all(f"SELECT id FROM projects WHERE tenant_id IN ({marks}) ORDER BY name", *tenants)
        found = {}
        for pid in [r["id"] for r in rows] + projects:
            access = H.project_access(H.APP, self.user, pid)
            if access:
                found[pid] = (access[0], "edit" if access[1] in H.EDIT_ROLES else "view")
        return found

    def access(self, role: str) -> list[str]:
        return ["read"] + [s for s in ("write", "review") if s in self.scopes and role == "edit"]

    def call(self, name: str, args: dict) -> dict:
        reachable = self.projects()
        if name == "list_projects":
            spaces = {t["id"]: t["name"] for t in H.APP.db.all("SELECT id, name FROM tenants")}
            return mcp_tools.result({"projects": [
                {"project": pid, "name": p["name"], "workspace": spaces.get(p["tenant_id"]), "access": self.access(r),
                 "pdf": (H.APP.project_root(p) / ".out" / f"{p['slug']}.pdf").is_file()}
                for pid, (p, r) in reachable.items()]})
        if name == "search":
            budget, found = [mcp_tools.SEARCH_BYTES], []
            for pid, (p, _) in list(reachable.items())[:50]:
                found += [(score, pid, p["name"], rel) for score, rel in
                          mcp_tools.search_tree(H.APP.project_source(p), args["query"], budget)]
            found.sort(key=lambda f: (-f[0], f[2], f[3]))
            return mcp_tools.result({"results": [{"id": f"{pid}::{rel}", "title": f"{pname}: {rel}",
                                                  "url": f"{issuer()}/p/{pid}/"} for _, pid, pname, rel in found[:20]]})
        if name == "fetch":
            pid, _, rel = args["id"].partition("::")
            if pid not in reachable:
                raise mcp_tools.ToolError("No such file (ids come from search).")
            project = reachable[pid][0]
            text, cut = mcp_tools.read_for_fetch(H.APP.project_source(project), rel)
            return mcp_tools.result({"id": args["id"], "title": f"{project['name']}: {rel}", "text": text,
                                     "url": f"{issuer()}/p/{pid}/",
                                     "metadata": {"project": pid, "path": rel, "truncated": cut}})
        pid = args.pop("project")
        if pid not in reachable:
            raise mcp_tools.ToolError(f"No project {pid!r} in this connection. list_projects shows the ones you "
                                      "can use.")
        project, role = reachable[pid]
        if name in WRITE_TOOLS:
            if role != "edit":
                raise mcp_tools.ToolError("Your role in this workspace is viewer: you can read but not change it.")
            H.audit_log(H.APP, "mcp_tool", self.user["id"], self.h.ip, project["tenant_id"],
                        f"{self.grant['client_name']}: {name} {pid}")
        return self.worker(project, "edit" if name in WRITE_TOOLS else "view", name, args)

    def worker(self, project: dict, role: str, name: str, args: dict) -> dict:
        who = f"{self.user['name']} via {self.grant['client_name']}"[:80]
        worker = H.APP.workers.acquire(project)
        conn = None
        try:
            conn = http.client.HTTPConnection("127.0.0.1", worker.port, timeout=H.APP.settings()["build_timeout"] + 60)
            conn.request("POST", f"/api/mcp?doc={quote(project['slug'])}",
                         json.dumps({"tool": name, "arguments": args}).encode(),
                         {"Host": f"127.0.0.1:{worker.port}", "Content-Type": "application/json",
                          "X-Host-Secret": worker.secret, "X-Host-Role": role,
                          "X-Host-User": quote(f"{self.user['id']};{who}", ";")})
            res = conn.getresponse()
            raw = res.read(16 * 1024 * 1024)
        except (OSError, http.client.HTTPException):
            raise mcp_tools.ToolError("The project's editor did not answer; try again in a moment.")
        finally:
            if conn is not None:
                conn.close()
            H.APP.workers.release(worker)
            if name in WRITE_TOOLS:
                H.APP.sizes.pop(project["id"], None)
        try:
            data = json.loads(raw)
        except ValueError:
            data = None
        if res.status != 200 or not isinstance(data, dict) or not isinstance(data.get("content"), list):
            message = data.get("error") if isinstance(data, dict) else None
            raise mcp_tools.ToolError(str(message or f"The project's editor refused the request ({res.status})."))
        return data


# ---------------------------------------------------------------------------
# Signed-in API: consent and connected clients (registered by install(); session + CSRF like every /api/ route)
# ---------------------------------------------------------------------------

def api_grants(h) -> None:
    need_enabled()
    rows = H.APP.db.all("SELECT g.*, c.name AS client_name, c.redirect_uris FROM oauth_grants g JOIN oauth_clients c "
                        "ON c.id = g.client_id WHERE g.user_id = ? ORDER BY g.created DESC", h.user["id"])
    names = {"t": {r["id"]: r["name"] for r in H.APP.db.all("SELECT id, name FROM tenants")},
             "p": {r["id"]: r["name"] for r in H.APP.db.all("SELECT id, name FROM projects")}}
    out = []
    for row in rows:
        tenants = sorted(names["t"].get(t, "(deleted)") for t in json.loads(row["tenants"]))
        projects = sorted(names["p"].get(p, "(deleted)") for p in json.loads(row["projects"]))
        uris = json.loads(row["redirect_uris"])
        out.append({"id": row["id"], "client": row["client_name"], "returns_to": redirect_label(uris[0]),
                    "scopes": row["scope"].split(), "workspaces": tenants, "projects": projects,
                    "created": row["created"], "used": row["used"]})
    h.ok({"grants": out, "endpoint": resource()})


def api_grant_revoke(h, gid: str) -> None:
    need_enabled()
    if H.APP.db.change("DELETE FROM oauth_grants WHERE id = ? AND user_id = ?", gid, h.user["id"]) != 1:
        raise H.HttpError(404, "No such connection.")
    H.audit_log(H.APP, "mcp_revoked", h.user["id"], h.ip, detail=gid)
    h.ok()


def cleanup(stale_clients: float = 86400.0) -> None:
    """Housekeeping: expired requests, codes and tokens; connections with no live token; unused registrations."""
    db, now = H.APP.db, time.time()
    db.run("DELETE FROM oauth_requests WHERE created < ?", now - REQUEST_SECONDS)
    db.run("DELETE FROM oauth_codes WHERE expires < ?", now - 3600)  # kept an hour: a replay still revokes
    db.run("DELETE FROM oauth_tokens WHERE expires < ?", now)
    db.run("DELETE FROM oauth_grants WHERE created < ? AND NOT EXISTS (SELECT 1 FROM oauth_tokens t WHERE "
           "t.grant_id = oauth_grants.id) AND NOT EXISTS (SELECT 1 FROM oauth_codes c WHERE "
           "c.grant_id = oauth_grants.id AND c.used = 0)", now - 3600)
    db.run("DELETE FROM oauth_clients WHERE created < ? AND NOT EXISTS (SELECT 1 FROM oauth_grants g WHERE "
           "g.client_id = oauth_clients.id)", now - stale_clients)


def install(module) -> None:
    global H
    H = module
    module.route("GET", r"/api/oauth/requests/([A-Za-z0-9_-]{20,64})")(api_request)
    module.route("POST", r"/api/oauth/requests/([A-Za-z0-9_-]{20,64})")(api_decide)
    module.route("GET", r"/api/oauth/grants")(api_grants)
    module.route("DELETE", r"/api/oauth/grants/([0-9a-f]{32})")(api_grant_revoke)
