#!/usr/bin/env python3
"""
Hosted LaTeX editor for a VPS: accounts, organisations (tenants) and projects in front of serve.py.

    python scripts/host.py init  --data /var/lib/latex-host   # database, config.toml, first site admin
    python scripts/host.py serve --data /var/lib/latex-host   # Linux only; needs bubblewrap (bwrap)

The gateway owns logins (password + optional TOTP, OIDC / GitHub), sessions, tenants, members, invites,
projects and the admin pages (host_ui/). Each open project runs in its own worker,
`serve.py --gateway --source <project>`, on loopback; requests to /p/<project id>/... are proxied to it
(HTTP, long-poll and WebSocket) with a per-worker secret and the user's role in X-Host-* headers.

Security model: every logged-in user, tenant admins included, is untrusted; LaTeX source is code. Only the
operator (shell access, config.toml, the site admin account) is trusted. Stdlib only, Python 3.9+.
"""

from __future__ import annotations

import argparse
import base64
import collections
import contextlib
import getpass
import hashlib
import hmac
import io
import ipaddress
import json
import os
import re
import secrets
import select
import shutil
import socket
import sqlite3
import struct
import subprocess
import sys
import tempfile
import threading
import time
import traceback
import urllib.error
import urllib.request
import zipfile
import zlib
from http.cookies import SimpleCookie
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, quote, urlencode, urlsplit

import ai
import build
import host_mcp
import zotero

SCRIPT_DIR = Path(__file__).resolve().parent
UI_DIR = SCRIPT_DIR / "host_ui"
FONTS_DIR = SCRIPT_DIR / "fonts"
FONT_TYPES = {"newsreader.woff2": "font/woff2", "newsreader-italic.woff2": "font/woff2",
              "source-sans-3.woff2": "font/woff2", "ibm-plex-mono-400.woff2": "font/woff2",
              "ibm-plex-mono-500.woff2": "font/woff2", "OFL.txt": "text/plain; charset=utf-8",
              "fonts.css": "text/css; charset=utf-8"}
SERVE = SCRIPT_DIR / "serve.py"

# ---------------------------------------------------------------------------
# Configuration: config.toml (operator only) and site settings (site admin, in the database)
# ---------------------------------------------------------------------------

CONFIG_DEFAULTS: dict = {
    "public_url": "http://localhost:8080", "listen": "127.0.0.1", "port": 8080, "trust_proxy": False,
    "site_name": "LaTeX Studio", "session_days": 14, "session_idle_hours": 12, "providers": {},
    "max_connections": 256, "max_upload_mb": 50, "max_streams_per_user": 8, "signups_per_ip_hour": 10, "ai": {},
    "mcp": {},
}
AI_DEFAULTS: dict = {"enabled": False, "model": ai.DEFAULT_MODEL, "api_key_env": "ANTHROPIC_API_KEY", "api_key": "",
                     "daily_per_user": 50, "daily_per_workspace": 500, "daily_total": 2000,
                     "daily_tokens_per_user": 1_000_000}
CONFIG_RANGES = {"max_connections": (8, 100000), "max_upload_mb": (1, 10000), "max_streams_per_user": (1, 1000),
                 "signups_per_ip_hour": (1, 100000)}
CONFIG_TEMPLATE = """\
# scripts/host.py settings. Restart host.py after editing. Site settings (sign-up, quotas) are on the admin page.
public_url = "http://localhost:8080"  # the URL people open; https:// turns on Secure cookies and HSTS
listen = "127.0.0.1"                  # keep loopback and put a TLS proxy (Caddy) in front
port = 8080
trust_proxy = false                   # true behind Caddy on this machine: client IPs from X-Forwarded-For
site_name = "LaTeX Studio"
session_days = 14                     # a login lasts at most this long
session_idle_hours = 12               # and ends after this long without a request
max_connections = 256                 # open client connections at once; more get 503 at once
max_upload_mb = 50                    # largest zip upload (unpacked size is capped by the project quota)
max_streams_per_user = 8              # open editor connections (WebSocket, long-poll) per account
signups_per_ip_hour = 10              # account sign-ups from one address (IPv6: one /64) per hour

# Sign-in providers (optional). Put secrets in the environment: client_secret_env names the variable.
# Redirect URI to register with the provider: <public_url>/auth/<name>/callback
# [providers.google]
# type = "oidc"
# label = "Google"
# issuer = "https://accounts.google.com"
# client_id = "..."
# client_secret_env = "GOOGLE_CLIENT_SECRET"
#
# [providers.github]
# type = "github"
# label = "GitHub"
# client_id = "..."
# client_secret_env = "GITHUB_CLIENT_SECRET"

# AI assistant in the editor (optional). Off unless enabled. The gateway makes the Anthropic API calls itself;
# project workers never get the key. Editors' text, logs and questions are sent to api.anthropic.com.
# [ai]
# enabled = true
# model = "claude-opus-5-5"            # or claude-sonnet-5-5, claude-haiku-5-5
# api_key_env = "ANTHROPIC_API_KEY"    # the environment variable holding the key
# daily_per_user = 50                  # requests per person per day (UTC)
# daily_per_workspace = 500            # requests per workspace per day
# daily_total = 2000                   # requests for the whole server per day (the spending cap)
# daily_tokens_per_user = 1000000      # input + output tokens per person per day
""" + host_mcp.CONFIG_TEMPLATE

SIGNUP_MODES = ("invite_only", "open", "open_domains")
SETTING_DEFAULTS: dict = {
    "signup_mode": "invite_only", "signup_domains": [], "auto_link": False, "max_tenants": 100,
    "max_projects_per_tenant": 50, "max_project_mb": 200, "max_workers": 8, "max_workers_per_tenant": 3,
    "worker_idle_minutes": 15, "build_timeout": 300,
}
SETTING_RANGES = {
    "max_tenants": (0, 100000), "max_projects_per_tenant": (1, 10000), "max_project_mb": (1, 10000),
    "max_workers": (1, 500), "max_workers_per_tenant": (1, 500), "worker_idle_minutes": (1, 1440),
    "build_timeout": (10, 3600),
}
ROLES = ("admin", "editor", "viewer")
EDIT_ROLES = ("admin", "editor")


class ConfigError(Exception):
    pass


def read_toml(path: Path) -> dict:
    try:
        import tomllib
    except ImportError:
        try:
            import tomli as tomllib
        except ImportError:
            raise ConfigError("config.toml needs Python 3.11+ or the 'tomli' package.") from None
    try:
        return tomllib.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise ConfigError(f"{path}: {exc}") from None


def load_config(data: Path) -> dict:
    path = data / "config.toml"
    found = read_toml(path) if path.exists() else {}
    unknown = sorted(set(found) - set(CONFIG_DEFAULTS))
    if unknown:
        raise ConfigError(f"{path}: unknown key(s): {', '.join(unknown)}")
    config = {**CONFIG_DEFAULTS, **found}
    url = urlsplit(str(config["public_url"]))
    if url.scheme not in ("http", "https") or not url.netloc or url.path not in ("", "/"):
        raise ConfigError(f"{path}: public_url must look like https://latex.example.org (no path)")
    config["public_url"] = f"{url.scheme}://{url.netloc}"
    for key, (low, high) in CONFIG_RANGES.items():
        if not (isinstance(config[key], int) and not isinstance(config[key], bool) and low <= config[key] <= high):
            raise ConfigError(f"{path}: {key} must be a whole number from {low} to {high}")
    for name, provider in config["providers"].items():
        if not re.fullmatch(r"[a-z0-9-]{1,30}", name) or not isinstance(provider, dict):
            raise ConfigError(f"{path}: provider names are lower-case letters, digits and dashes")
        if provider.get("type") not in ("oidc", "github") or not provider.get("client_id"):
            raise ConfigError(f"{path}: provider {name} needs type = \"oidc\" or \"github\" and a client_id")
        if provider["type"] == "oidc" and not str(provider.get("issuer", "")).startswith("https://"):
            raise ConfigError(f"{path}: provider {name} needs an https:// issuer")
    if not isinstance(config["ai"], dict) or set(config["ai"]) - set(AI_DEFAULTS):
        raise ConfigError(f"{path}: [ai] takes only {', '.join(AI_DEFAULTS)}")
    config["ai"] = cfg = {**AI_DEFAULTS, **config["ai"]}
    if not isinstance(cfg["enabled"], bool) or cfg["model"] not in ai.MODELS \
            or not all(isinstance(cfg[k], str) for k in ("api_key_env", "api_key")):
        raise ConfigError(f"{path}: [ai] needs enabled = true/false, a model from {ai.MODELS} and text for the keys")
    for key in ("daily_per_user", "daily_per_workspace", "daily_total", "daily_tokens_per_user"):
        if not (isinstance(cfg[key], int) and not isinstance(cfg[key], bool) and 0 <= cfg[key] <= 10 ** 9):
            raise ConfigError(f"{path}: [ai] {key} must be a whole number from 0 to 1000000000")
    host_mcp.check_config(config, path)
    return config


def ai_config() -> dict:
    return {**AI_DEFAULTS, **APP.config.get("ai", {})}


def ai_key() -> str | None:
    """The operator's Anthropic key: config.toml api_key, else the variable api_key_env names (taken out of this
    process's environment by ai.env_key, so nothing it starts inherits it)."""
    cfg = ai_config()
    return cfg["api_key"] or (ai.env_key(cfg["api_key_env"]) if cfg["api_key_env"] else None)


# ---------------------------------------------------------------------------
# Database: sqlite3 (WAL), one connection behind one lock
# ---------------------------------------------------------------------------

MIGRATIONS = [
    """
    CREATE TABLE users(
        id INTEGER PRIMARY KEY, email TEXT NOT NULL UNIQUE, name TEXT NOT NULL, pw_hash TEXT,
        email_verified INTEGER NOT NULL DEFAULT 0, site_admin INTEGER NOT NULL DEFAULT 0,
        disabled INTEGER NOT NULL DEFAULT 0, totp_secret TEXT, totp_pending TEXT,
        totp_step INTEGER NOT NULL DEFAULT 0, created REAL NOT NULL);
    CREATE TABLE recovery_codes(
        user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE, code_hash TEXT NOT NULL,
        PRIMARY KEY(user_id, code_hash));
    CREATE TABLE sessions(
        id_hash TEXT PRIMARY KEY, user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
        csrf TEXT NOT NULL, stage TEXT NOT NULL, created REAL NOT NULL, seen REAL NOT NULL, expires REAL NOT NULL,
        ip TEXT);
    CREATE TABLE identities(
        provider TEXT NOT NULL, subject TEXT NOT NULL, user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
        email TEXT, created REAL NOT NULL, PRIMARY KEY(provider, subject));
    CREATE TABLE tenants(id TEXT PRIMARY KEY, name TEXT NOT NULL, created REAL NOT NULL);
    CREATE TABLE members(
        tenant_id TEXT NOT NULL REFERENCES tenants(id) ON DELETE CASCADE,
        user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
        role TEXT NOT NULL CHECK(role IN ('admin', 'editor', 'viewer')), PRIMARY KEY(tenant_id, user_id));
    CREATE TABLE invites(
        id INTEGER PRIMARY KEY, token_hash TEXT NOT NULL UNIQUE,
        tenant_id TEXT NOT NULL REFERENCES tenants(id) ON DELETE CASCADE,
        role TEXT NOT NULL CHECK(role IN ('admin', 'editor', 'viewer')), email TEXT, created_by INTEGER,
        created REAL NOT NULL, expires REAL NOT NULL, used_by INTEGER, used REAL);
    CREATE TABLE projects(
        id TEXT PRIMARY KEY, tenant_id TEXT NOT NULL REFERENCES tenants(id) ON DELETE CASCADE, name TEXT NOT NULL,
        slug TEXT NOT NULL, created_by INTEGER, created REAL NOT NULL);
    CREATE TABLE resets(
        token_hash TEXT PRIMARY KEY, user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
        expires REAL NOT NULL);
    CREATE TABLE oauth_flows(
        state_hash TEXT PRIMARY KEY, provider TEXT NOT NULL, verifier TEXT NOT NULL, nonce TEXT NOT NULL,
        intent TEXT NOT NULL, user_id INTEGER, invite TEXT, created REAL NOT NULL);
    CREATE TABLE settings(key TEXT PRIMARY KEY, value TEXT NOT NULL);
    CREATE TABLE audit(
        id INTEGER PRIMARY KEY, at REAL NOT NULL, user_id INTEGER, ip TEXT, action TEXT NOT NULL, tenant_id TEXT,
        detail TEXT);
    CREATE INDEX audit_at ON audit(at);
    """,
    """
    CREATE TABLE ai_usage(
        day TEXT NOT NULL, user_id INTEGER NOT NULL, tenant_id TEXT NOT NULL, requests INTEGER NOT NULL DEFAULT 0,
        input_tokens INTEGER NOT NULL DEFAULT 0, output_tokens INTEGER NOT NULL DEFAULT 0,
        PRIMARY KEY(day, user_id, tenant_id));
    """,
    """
    CREATE TABLE zotero_settings(
        user_id INTEGER PRIMARY KEY REFERENCES users(id) ON DELETE CASCADE, library_type TEXT NOT NULL,
        library_id TEXT NOT NULL, collection TEXT NOT NULL, format TEXT NOT NULL, api_key TEXT);
    """,
    # AI clients over MCP (host_mcp.py): OAuth clients (RFC 7591), pending authorizations, grants, codes, tokens.
    """
    CREATE TABLE oauth_clients(
        id TEXT PRIMARY KEY, name TEXT NOT NULL, redirect_uris TEXT NOT NULL, created REAL NOT NULL, ip TEXT);
    CREATE TABLE oauth_requests(
        id_hash TEXT PRIMARY KEY, client_id TEXT NOT NULL REFERENCES oauth_clients(id) ON DELETE CASCADE,
        redirect_uri TEXT NOT NULL, state TEXT, challenge TEXT NOT NULL, scope TEXT NOT NULL, created REAL NOT NULL);
    CREATE TABLE oauth_grants(
        id TEXT PRIMARY KEY, user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
        client_id TEXT NOT NULL REFERENCES oauth_clients(id) ON DELETE CASCADE, scope TEXT NOT NULL,
        tenants TEXT NOT NULL, projects TEXT NOT NULL, created REAL NOT NULL, used REAL);
    CREATE INDEX oauth_grants_user ON oauth_grants(user_id);
    CREATE TABLE oauth_codes(
        code_hash TEXT PRIMARY KEY, grant_id TEXT NOT NULL REFERENCES oauth_grants(id) ON DELETE CASCADE,
        redirect_uri TEXT NOT NULL, challenge TEXT NOT NULL, expires REAL NOT NULL, used INTEGER NOT NULL DEFAULT 0);
    CREATE TABLE oauth_tokens(
        token_hash TEXT PRIMARY KEY, grant_id TEXT NOT NULL REFERENCES oauth_grants(id) ON DELETE CASCADE,
        kind TEXT NOT NULL CHECK(kind IN ('access', 'refresh')), resource TEXT NOT NULL, expires REAL NOT NULL,
        used REAL);
    CREATE INDEX oauth_tokens_grant ON oauth_tokens(grant_id);
    """,
]


class DB:
    # ponytail: one connection and one lock; plenty for a small team's gateway. A pool if it ever shows up in profiles.
    def __init__(self, path: Path) -> None:
        self.conn = sqlite3.connect(str(path), check_same_thread=False, isolation_level=None, timeout=10)
        self.conn.row_factory = sqlite3.Row
        self.lock = threading.RLock()
        self.conn.execute("PRAGMA journal_mode=WAL")
        self.conn.execute("PRAGMA foreign_keys=ON")
        self.migrate()

    def migrate(self) -> None:
        with self.lock:
            self.conn.execute("CREATE TABLE IF NOT EXISTS schema_version(version INTEGER NOT NULL)")
            row = self.conn.execute("SELECT version FROM schema_version").fetchone()
            if row is None:
                self.conn.execute("INSERT INTO schema_version VALUES (0)")
            version = row[0] if row else 0
            for number, script in enumerate(MIGRATIONS[version:], version + 1):
                self.conn.executescript(f"BEGIN;\n{script}\nUPDATE schema_version SET version = {number};\nCOMMIT;")

    def version(self) -> int:
        return self.one("SELECT version FROM schema_version")["version"]

    def all(self, sql: str, *args) -> list[dict]:
        with self.lock:
            return [dict(row) for row in self.conn.execute(sql, args)]

    def one(self, sql: str, *args) -> dict | None:
        rows = self.all(sql, *args)
        return rows[0] if rows else None

    def run(self, sql: str, *args) -> int:
        with self.lock:
            return self.conn.execute(sql, args).lastrowid

    def change(self, sql: str, *args) -> int:
        """Run an UPDATE or DELETE; the number of rows it changed (1 = this caller won a race)."""
        with self.lock:
            return self.conn.execute(sql, args).rowcount

    @contextlib.contextmanager
    def tx(self):
        with self.lock:
            self.conn.execute("BEGIN IMMEDIATE")
            try:
                yield self
            except BaseException:
                self.conn.execute("ROLLBACK")
                raise
            self.conn.execute("COMMIT")


# ---------------------------------------------------------------------------
# Secrets: passwords (scrypt), tokens, TOTP (RFC 6238), recovery codes
# ---------------------------------------------------------------------------

SCRYPT = {"n": 2 ** 15, "r": 8, "p": 1}
MIN_PASSWORD, MAX_PASSWORD = 10, 1024
_DUMMY: list[str] = []


def b64(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).decode().rstrip("=")


def unb64(text: str) -> bytes:
    return base64.urlsafe_b64decode(text + "=" * (-len(text) % 4))


def hash_password(password: str) -> str:
    salt = os.urandom(16)
    n, r, p = SCRYPT["n"], SCRYPT["r"], SCRYPT["p"]
    key = hashlib.scrypt(password.encode(), salt=salt, n=n, r=r, p=p, maxmem=256 * 1024 * 1024, dklen=32)
    return f"scrypt${n}${r}${p}${b64(salt)}${b64(key)}"


def verify_password(password, stored: str | None) -> bool:
    """Constant-ish time: with no stored hash it still runs scrypt once, so unknown emails take as long."""
    if not _DUMMY:
        _DUMMY.append(hash_password(secrets.token_hex(8)))
    good = isinstance(password, str) and 0 < len(password) <= MAX_PASSWORD and stored is not None
    try:
        _, n, r, p, salt, key = (stored if good else _DUMMY[0]).split("$")
        test = hashlib.scrypt(str(password or "").encode()[:MAX_PASSWORD * 4], salt=unb64(salt), n=int(n), r=int(r),
                              p=int(p), maxmem=256 * 1024 * 1024, dklen=32)
    except (ValueError, TypeError):
        return False
    return hmac.compare_digest(test, unb64(key)) and good


def check_password_rules(password) -> str:
    if not isinstance(password, str) or not MIN_PASSWORD <= len(password) <= MAX_PASSWORD:
        raise HttpError(400, f"Passwords need at least {MIN_PASSWORD} characters.")
    return password


def token_hash(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()


def totp_code(secret_b32: str, step: int) -> str:
    key = base64.b32decode(secret_b32 + "=" * (-len(secret_b32) % 8))
    digest = hmac.new(key, struct.pack(">Q", step), hashlib.sha1).digest()
    offset = digest[-1] & 0x0F
    return f"{(struct.unpack('>I', digest[offset:offset + 4])[0] & 0x7FFFFFFF) % 1000000:06d}"


def totp_match(secret_b32: str, code: str, last_step: int, now: float | None = None) -> int | None:
    """The time step the code belongs to (now, or one step either side), or None. Steps at or before last_step
    were used already: a code works once."""
    if not re.fullmatch(r"\d{6}", code or ""):
        return None
    current = int((time.time() if now is None else now) // 30)
    for step in (current - 1, current, current + 1):
        if step > last_step and hmac.compare_digest(totp_code(secret_b32, step), code):
            return step
    return None


def new_totp_secret() -> str:
    return base64.b32encode(os.urandom(20)).decode().rstrip("=")


def new_recovery_codes() -> list[str]:
    return [f"{secrets.token_hex(3)}-{secrets.token_hex(3)}" for _ in range(10)]


def norm_code(code) -> str:
    return re.sub(r"[\s-]", "", str(code or "")).lower()


# ---------------------------------------------------------------------------
# Throttling: failed logins per IP and per account, with exponential backoff
# ---------------------------------------------------------------------------

class Throttle:
    """After `limit` failures inside `window` seconds, each further try waits 30 s, 60 s, 120 s ... (max window)."""

    def __init__(self, limit: int, window: float = 900.0) -> None:
        self.limit, self.window = limit, window
        self.hits: dict[str, collections.deque] = {}
        self.lock = threading.Lock()

    def _recent(self, key: str, now: float) -> collections.deque:
        hits = self.hits.setdefault(key, collections.deque())
        while hits and now - hits[0] > self.window:
            hits.popleft()
        return hits

    def wait(self, key: str, now: float | None = None) -> float:
        now = time.monotonic() if now is None else now
        with self.lock:
            hits = self._recent(key, now)
            if len(hits) < self.limit:
                return 0.0
            backoff = min(self.window, 30.0 * 2 ** (len(hits) - self.limit))
            return max(0.0, hits[-1] + backoff - now)

    def fail(self, key: str, now: float | None = None) -> None:
        now = time.monotonic() if now is None else now
        with self.lock:
            self._recent(key, now).append(now)
            if len(self.hits) > 100000:  # Memory stays bounded under a spray of addresses.
                self.hits = {k: v for k, v in self.hits.items() if v}

    def full(self, key: str, now: float | None = None) -> bool:
        """`limit` hits inside the window already: a hard cap, no backoff."""
        now = time.monotonic() if now is None else now
        with self.lock:
            return len(self._recent(key, now)) >= self.limit

    def clear(self, key: str) -> None:
        with self.lock:
            self.hits.pop(key, None)


def ip_key(ip: str) -> str:
    """The throttling key of a client address: IPv6 by /64 (one subscriber usually holds a whole /64)."""
    try:
        addr = ipaddress.ip_address(ip)
    except ValueError:
        return ip
    if addr.version == 6 and addr.ipv4_mapped is None:
        return str(ipaddress.ip_network(f"{addr}/64", strict=False))
    return str(getattr(addr, "ipv4_mapped", None) or addr)


# ---------------------------------------------------------------------------
# HTTP errors and small validators
# ---------------------------------------------------------------------------

class HttpError(Exception):
    def __init__(self, status: int, message: str, headers: dict | None = None) -> None:
        super().__init__(message)
        self.status, self.message, self.headers = status, message, headers or {}


def norm_email(value) -> str:
    email = str(value or "").strip().casefold()
    if len(email) > 254 or not re.fullmatch(r"[^@\s]+@[^@\s]+\.[^@\s.]+", email):
        raise HttpError(400, "Enter a valid email address.")
    return email


def clean_name(value, what: str = "Name") -> str:
    name = re.sub(r"\s+", " ", str(value or "")).strip()
    if not 1 <= len(name) <= 80 or re.search(r"[\x00-\x1f\x7f]", name):
        raise HttpError(400, f"{what} must be 1 to 80 characters.")
    return name


def slug_for(name: str) -> str:
    return re.sub(r"[^A-Za-z0-9._-]+", "-", name).strip("-.")[:60].strip("-.") or "document"


# ---------------------------------------------------------------------------
# Zip import and export
# ---------------------------------------------------------------------------

MAX_ZIP_ENTRIES = 5000
SKIP_IN_ZIP = re.compile(r"(^|/)(__MACOSX|\.git|\.DS_Store|Thumbs\.db)(/|$)")
RC_NAMES = {".latexmkrc", "latexmkrc", "build.toml"}  # serve.RC_NAMES: build configuration that can run programs


def zip_members(archive: zipfile.ZipFile, max_bytes: int) -> tuple[list[tuple[zipfile.ZipInfo, str]], list[str]]:
    """
    Validate an uploaded archive and map its files to paths inside the project. Refuses absolute paths, "..",
    backslashes, control characters, symlinks, encrypted entries, too many entries and too many bytes; a single
    top-level folder around main.tex is dropped. Build configuration files (RC_NAMES) are left out: workers ignore
    them anyway, and the browser cannot edit them. Returns ([(info, path inside the project)], [skipped names]).
    """
    infos = [i for i in archive.infolist() if not SKIP_IN_ZIP.search(i.filename)]
    if len(infos) > MAX_ZIP_ENTRIES:
        raise HttpError(413, f"The archive has more than {MAX_ZIP_ENTRIES} entries.")
    files: list[tuple[zipfile.ZipInfo, str]] = []
    skipped: list[str] = []
    total = 0
    for info in infos:
        name = info.filename
        if info.flag_bits & 0x1:
            raise HttpError(400, "Encrypted archives are not supported.")
        if (info.external_attr >> 16) & 0o170000 == 0o120000:
            raise HttpError(400, f"The archive contains a symbolic link ({name[:80]}); remove it and try again.")
        if "\\" in name or name.startswith("/") or re.match(r"[A-Za-z]:", name) or re.search(r"[\x00-\x1f]", name) \
                or len(name) > 240:
            raise HttpError(400, f"Unsafe path in the archive: {name[:80]!r}")
        parts = name.rstrip("/").split("/")
        if any(part in ("", ".", "..") for part in parts) or len(parts) > 20:
            raise HttpError(400, f"Unsafe path in the archive: {name[:80]!r}")
        if name.endswith("/"):
            continue
        if parts[-1].lower() in RC_NAMES:
            skipped.append(name[:240])
            continue
        total += info.file_size
        if total > max_bytes:
            raise HttpError(413, f"The unpacked project would be larger than {max_bytes // (1024 * 1024)} MB.")
        files.append((info, name))
    names = [rel for _, rel in files]
    if "main.tex" not in names:
        tops = {rel.split("/", 1)[0] for rel in names}
        if len(tops) == 1 and all("/" in rel for rel in names) and f"{next(iter(tops))}/main.tex" in names:
            cut = len(next(iter(tops))) + 1
            files = [(info, rel[cut:]) for info, rel in files]
        else:
            raise HttpError(400, "The archive needs a main.tex at its top level (or inside one top-level folder).")
    seen: set[str] = set()
    for _, rel in files:
        if rel.casefold() in seen:
            raise HttpError(400, f"The archive lists {rel[:80]!r} twice.")
        seen.add(rel.casefold())
    return files, skipped


def unpack_zip(data, target: Path, max_bytes: int) -> list[str]:
    """
    Unpack bytes or a seekable file into target (which must not exist). Counts real bytes, not claimed sizes.
    Returns the build configuration files left out.
    """
    try:
        archive = zipfile.ZipFile(io.BytesIO(data) if isinstance(data, bytes) else data)
    except (zipfile.BadZipFile, ValueError, OSError):
        raise HttpError(400, "That file is not a zip archive.")
    files, skipped = zip_members(archive, max_bytes)
    target.mkdir(parents=True)
    base = target.resolve()
    written = 0
    try:
        for info, rel in files:
            dest = base / rel
            if base not in dest.resolve().parents:
                raise HttpError(400, f"Unsafe path in the archive: {rel[:80]!r}")
            dest.parent.mkdir(parents=True, exist_ok=True)
            with archive.open(info) as src, open(dest, "xb") as out:
                while True:
                    chunk = src.read(1024 * 1024)
                    if not chunk:
                        break
                    written += len(chunk)
                    if written > max_bytes:
                        raise HttpError(413, f"The unpacked project would be larger than {max_bytes // 1048576} MB.")
                    out.write(chunk)
    except (zipfile.BadZipFile, OSError, RuntimeError, EOFError, zlib.error) as exc:
        shutil.rmtree(target, ignore_errors=True)
        raise HttpError(400, f"Could not unpack the archive: {exc}")
    except BaseException:
        shutil.rmtree(target, ignore_errors=True)
        raise
    return skipped


def zip_folder(root: Path, out) -> None:
    """Every regular file below root (no symlinks, nothing that resolves outside) into a zip written to out."""
    base = root.resolve()
    with zipfile.ZipFile(out, "w", zipfile.ZIP_DEFLATED) as archive:
        for current, dirs, names in os.walk(base):
            dirs[:] = sorted(d for d in dirs if not os.path.islink(os.path.join(current, d)))
            for name in sorted(names):
                path = Path(current, name)
                if path.is_symlink() or not path.is_file() or base not in path.resolve().parents:
                    continue
                archive.write(path, f"{root.name}/{path.relative_to(base).as_posix()}")


ENTRY_BYTES = 4096  # quota charge per file or folder: one disk block


def folder_bytes(root: Path) -> int:
    total = 0
    for current, dirs, names in os.walk(root):
        dirs[:] = [d for d in dirs if not os.path.islink(os.path.join(current, d))]
        total += ENTRY_BYTES * len(dirs)
        for name in names:
            try:  # Every entry costs at least ENTRY_BYTES, so thousands of empty files count too.
                total += max(os.lstat(os.path.join(current, name)).st_size, ENTRY_BYTES)
            except OSError:
                pass
    return total


# ---------------------------------------------------------------------------
# Workers: one serve.py --gateway per open project
# ---------------------------------------------------------------------------

class Worker:
    def __init__(self, project: dict) -> None:
        self.project, self.proc, self.port, self.secret = project, None, 0, ""
        self.active = 0
        self.used = time.monotonic()
        self.ready = threading.Event()  # set once started (or failed: error)
        self.error: HttpError | None = None


class Workers:
    """
    Start on first use, stop after the idle timeout, cap how many run (globally and per tenant). A worker builds
    one document at a time, so these caps also bound concurrent builds. Starting and stopping happen outside the
    lock: a placeholder entry makes other requests for the same project wait for that start only.
    """

    def __init__(self, app: App) -> None:
        self.app = app
        self.lock = threading.Lock()
        self.running: dict[str, Worker] = {}

    def acquire(self, project: dict) -> Worker:
        victims: list[Worker] = []
        with self.lock:
            worker = self.running.get(project["id"])
            if worker is not None and worker.ready.is_set() and (worker.error or worker.proc.poll() is not None):
                del self.running[project["id"]]
                worker = None
            start = worker is None
            if start:
                victims = self._make_room(project["tenant_id"])
                worker = self.running[project["id"]] = Worker(project)
            worker.active += 1
            worker.used = time.monotonic()
        for victim in victims:
            self._kill(victim)
        if start:
            try:
                worker.proc, worker.port, worker.secret = self._spawn(project)
            except HttpError as exc:
                worker.error = exc
            except Exception:  # noqa: BLE001 - waiters must never see a half-made worker.
                traceback.print_exc()
                worker.error = HttpError(502, "The project's editor did not start. The server log says why.")
            finally:
                worker.ready.set()
            with self.lock:
                closed = worker.error is None and self.running.get(project["id"]) is not worker
                if closed:
                    worker.error = HttpError(503, "The project was closed while it started. Try again.")
                elif worker.error is not None and self.running.get(project["id"]) is worker:
                    del self.running[project["id"]]
            if closed:
                self._kill(worker)
        elif not worker.ready.wait(40):
            self.release(worker)
            raise HttpError(503, "The project's editor is still starting. Try again in a moment.")
        if worker.error is not None:
            self.release(worker)
            raise worker.error
        return worker

    def release(self, worker: Worker) -> None:
        with self.lock:
            worker.active -= 1
            worker.used = time.monotonic()

    def _make_room(self, tenant: str) -> list[Worker]:
        """Take workers out of `running` until one more fits; returns them for the caller to stop (lock held)."""
        settings = self.app.settings()
        victims: list[Worker] = []

        def pool(scope: str | None) -> list[Worker]:
            return [w for w in self.running.values() if scope is None or w.project["tenant_id"] == scope]

        def evict(worker: Worker) -> None:
            victims.append(self.running.pop(worker.project["id"]))

        while len(pool(tenant)) >= settings["max_workers_per_tenant"]:
            idle = sorted((w for w in pool(tenant) if w.active == 0), key=lambda w: w.used)
            if not idle:
                raise HttpError(503, "Your workspace has too many projects open right now. Try again in a minute.")
            evict(idle[0])
        while len(pool(None)) >= settings["max_workers"]:
            idle = sorted((w for w in pool(None) if w.active == 0), key=lambda w: w.used)
            if idle:
                evict(idle[0])
                continue
            # Everything is busy. Fair share: the workspace holding the most workers gives up its least recently
            # active one, but a workspace that already has one never takes another's last one.
            started = [w for w in pool(None) if w.ready.is_set()]
            counts = collections.Counter(w.project["tenant_id"] for w in started)
            if not counts:
                raise HttpError(503, "The server is busy (too many open projects). Try again in a minute.")
            top = max(counts, key=lambda t: (counts[t], t != tenant))
            if counts[top] <= 1 and counts.get(tenant, 0) >= 1:
                raise HttpError(503, "The server is busy (too many open projects). Try again in a minute.")
            evict(min((w for w in started if w.project["tenant_id"] == top), key=lambda w: w.used))
            audit_log(self.app, "worker_evicted", None, None, top, "server full; fair share")
        return victims

    def _spawn(self, project: dict) -> tuple[subprocess.Popen, int, str]:
        root = self.app.project_root(project)
        home = root / ".home"
        home.mkdir(parents=True, exist_ok=True)
        secret = secrets.token_urlsafe(32)
        env = {"PATH": os.environ.get("PATH", "/usr/local/bin:/usr/bin:/bin"), "LANG": "C.UTF-8", "HOME": str(home),
               "LP_HOST_SECRET": secret, "PYTHONDONTWRITEBYTECODE": "1", "PYTHONUNBUFFERED": "1",
               "LP_QUOTA_BYTES": str(self.app.settings()["max_project_mb"] * 1024 * 1024)}
        for key in ("SYSTEMROOT", "TEMP", "TMP"):  # Windows needs these to start Python at all.
            if key in os.environ and os.name == "nt":
                env[key] = os.environ[key]
        if self.app.sandbox:
            env["LATEX_SANDBOX"] = "bwrap"
        argv = [sys.executable, "-E", "-s", str(SERVE), "--gateway", "--source", str(root / project["slug"]),
                "--host", "127.0.0.1", "--port", "0", "--no-open",
                "--build-timeout", str(self.app.settings()["build_timeout"])]
        proc = subprocess.Popen(argv, env=env, cwd=str(root), stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
                                stderr=subprocess.STDOUT, text=True, errors="replace", **build.NEW_GROUP)
        found: dict = {}
        ready = threading.Event()

        def drain() -> None:
            for line in proc.stdout:
                match = re.match(r"LP_GATEWAY_PORT=(\d+)$", line.strip())
                if match and not ready.is_set():
                    found["port"] = int(match.group(1))
                    ready.set()
                    continue
                sys.stderr.write(f"[{project['id']}] {line}")
            proc.stdout.close()
            ready.set()

        threading.Thread(target=drain, daemon=True).start()
        if not ready.wait(30) or "port" not in found:
            build.kill_tree(proc)
            raise HttpError(502, "The project's editor did not start. The server log says why.")
        audit_log(self.app, "worker_started", None, None, project["tenant_id"], project["id"])
        return proc, found["port"], secret

    @staticmethod
    def _kill(worker: Worker) -> None:
        if worker.proc is not None and worker.proc.poll() is None:
            if os.name != "nt":
                try:
                    os.killpg(worker.proc.pid, 15)
                    worker.proc.wait(5)
                except (OSError, subprocess.TimeoutExpired):
                    pass
            build.kill_tree(worker.proc)

    def _stop_where(self, test) -> None:
        with self.lock:
            gone = [self.running.pop(pid) for pid, w in list(self.running.items()) if test(w)]
        for worker in gone:
            self._kill(worker)

    def stop(self, project_id: str) -> None:
        self._stop_where(lambda w: w.project["id"] == project_id)

    def stop_all(self) -> None:
        self._stop_where(lambda w: True)

    def reap(self) -> None:
        idle = self.app.settings()["worker_idle_minutes"] * 60
        now = time.monotonic()
        self._stop_where(lambda w: w.ready.is_set() and (
            w.error is not None or w.proc.poll() is not None or (w.active == 0 and now - w.used > idle)))


# ---------------------------------------------------------------------------
# The application: state shared by every request
# ---------------------------------------------------------------------------

class App:
    def __init__(self, data: Path, config: dict, sandbox: bool = True) -> None:
        self.data, self.config, self.sandbox = data, config, sandbox
        (data / "projects").mkdir(parents=True, exist_ok=True)
        self.db = DB(data / "host.db")
        self.workers = Workers(self)
        self.public = urlsplit(config["public_url"])
        self.https = self.public.scheme == "https"
        self.cookie = "__Host-lp_session" if self.https else "lp_session"
        self.device_cookie = "__Host-lp_device" if self.https else "lp_device"
        self.device_key = device_key(self.db)
        self.login_ip, self.login_account = Throttle(20), Throttle(5)
        self.codes = Throttle(5)  # TOTP / recovery codes per account
        self.signups = Throttle(config["signups_per_ip_hour"], 3600)  # sign-ups per address (hard cap)
        self.invite_tries = Throttle(10, 3600)  # wrong invite links per address
        self.zotero_syncs = Throttle(10, 600)  # Zotero previews per account (hard cap)
        self.streams: collections.Counter = collections.Counter()  # open WebSockets / long-polls per user id
        self.streams_lock = threading.Lock()
        self.sizes: dict[str, tuple[float, int]] = {}
        self.discovery: dict[str, tuple[float, dict]] = {}

    # --- settings ---------------------------------------------------------------------------------------------

    def settings(self) -> dict:
        found = {row["key"]: json.loads(row["value"]) for row in self.db.all("SELECT key, value FROM settings")}
        return {**SETTING_DEFAULTS, **{k: v for k, v in found.items() if k in SETTING_DEFAULTS}}

    def save_settings(self, data: dict) -> dict:
        clean: dict = {}
        for key, value in data.items():
            if key == "signup_mode":
                if value not in SIGNUP_MODES:
                    raise HttpError(400, "Unknown sign-up mode.")
            elif key == "signup_domains":
                items = value.split(",") if isinstance(value, str) else value
                if not isinstance(items, list):
                    raise HttpError(400, "Domains must be a list.")
                value = sorted({str(d).strip().lower().lstrip("@") for d in items if str(d).strip()})
                if any(not re.fullmatch(r"[a-z0-9-]+(\.[a-z0-9-]+)+", d) for d in value):
                    raise HttpError(400, "Domains look like example.org.")
            elif key == "auto_link":
                if not isinstance(value, bool):
                    raise HttpError(400, "auto_link must be true or false.")
            elif key in SETTING_RANGES:
                low, high = SETTING_RANGES[key]
                if not (isinstance(value, int) and not isinstance(value, bool) and low <= value <= high):
                    raise HttpError(400, f"{key} must be a whole number from {low} to {high}.")
            else:
                raise HttpError(400, f"Unknown setting {key!r}.")
            clean[key] = value
        with self.db.tx():
            for key, value in clean.items():
                self.db.run("INSERT OR REPLACE INTO settings VALUES (?, ?)", key, json.dumps(value))
        return self.settings()

    # --- projects ---------------------------------------------------------------------------------------------

    def project_root(self, project: dict) -> Path:
        return self.data / "projects" / project["tenant_id"] / project["id"]

    def project_source(self, project: dict) -> Path:
        return self.project_root(project) / project["slug"]

    def project_bytes(self, project: dict) -> int:
        """
        Size of the project area (document, .out, .cache, .home), cached for a few seconds: only a cheap early
        reject. The worker enforces the quota itself on every write, under its write lock, with a fresh size.
        """
        when, size = self.sizes.get(project["id"], (None, 0))
        if when is None or time.monotonic() - when > 5:
            size = folder_bytes(self.project_root(project))
            self.sizes[project["id"]] = (time.monotonic(), size)
        return size


def device_key(db: DB) -> bytes:
    """The server secret that signs known-device cookies; made once, kept in the settings table (never shown)."""
    db.run("INSERT OR IGNORE INTO settings VALUES ('_device_key', ?)", json.dumps(secrets.token_hex(32)))
    return bytes.fromhex(json.loads(db.one("SELECT value FROM settings WHERE key = '_device_key'")["value"]))


def device_mac(user_id: int, nonce: str) -> str:
    return b64(hmac.new(APP.device_key, f"{user_id}.{nonce}".encode(), hashlib.sha256).digest())


def audit_log(app: App, action: str, user_id: int | None, ip: str | None, tenant: str | None = None,
              detail: str | None = None) -> None:
    app.db.run("INSERT INTO audit(at, user_id, ip, action, tenant_id, detail) VALUES (?, ?, ?, ?, ?, ?)",
               time.time(), user_id, ip, action, tenant, (detail or "")[:500] or None)


def create_user(app: App, email: str, name: str, password: str | None, verified: bool = False,
                site_admin: bool = False) -> int:
    if app.db.one("SELECT id FROM users WHERE email = ?", email):
        raise HttpError(409, "An account with this email already exists. Sign in instead.")
    return app.db.run(
        "INSERT INTO users(email, name, pw_hash, email_verified, site_admin, created) VALUES (?, ?, ?, ?, ?, ?)",
        email, name, hash_password(password) if password else None, int(verified), int(site_admin), time.time())


def create_tenant(app: App, name: str, admin: int | None = None) -> str:
    tid = secrets.token_hex(8)
    with app.db.tx():
        app.db.run("INSERT INTO tenants VALUES (?, ?, ?)", tid, name, time.time())
        if admin is not None:
            app.db.run("INSERT INTO members VALUES (?, ?, 'admin')", tid, admin)
    return tid


def personal_tenant(app: App, user_id: int, name: str) -> str:
    """A new account's own workspace: one per person, and only for someone in no workspace yet."""
    if app.db.one("SELECT 1 FROM members WHERE user_id = ?", user_id):
        raise HttpError(409, "You already have a workspace.")
    tenant_room(app)
    return create_tenant(app, f"{name}'s workspace", user_id)


def tenant_room(app: App) -> None:
    limit = app.settings()["max_tenants"]
    if limit and app.db.one("SELECT COUNT(*) AS n FROM tenants")["n"] >= limit:
        raise HttpError(403, "This server has reached its limit of workspaces. Ask the administrator for an invite.")


def signup_allowed(app: App, email: str, verified: bool) -> None:
    """Raise unless sign-up without an invite is open to this email. Domain rules need a verified email."""
    settings = app.settings()
    mode = settings["signup_mode"]
    if mode == "open":
        return
    if mode == "open_domains":
        if not verified:
            raise HttpError(403, "Sign-up is limited to some email domains: use a sign-in provider that confirms "
                                 "your email, or ask for an invite.")
        if email.rsplit("@", 1)[1] in settings["signup_domains"]:
            return
        raise HttpError(403, "Sign-up is not open to this email domain. Ask for an invite.")
    raise HttpError(403, "Sign-up is by invitation only.")


def tenant_role(app: App, user: dict, tenant_id: str) -> str | None:
    """The user's role in a tenant; site admins (the trusted operator) act as admins of every tenant."""
    if not app.db.one("SELECT id FROM tenants WHERE id = ?", tenant_id):
        return None
    if user["site_admin"]:
        return "admin"
    row = app.db.one("SELECT role FROM members WHERE tenant_id = ? AND user_id = ?", tenant_id, user["id"])
    return row["role"] if row else None


def project_access(app: App, user: dict, project_id: str) -> tuple[dict, str] | None:
    """(project, tenant role) when the user may open the project, else None (callers answer 404, not 403)."""
    project = app.db.one("SELECT * FROM projects WHERE id = ?", project_id) if re.fullmatch(r"[0-9a-f]{16}",
                                                                                            project_id) else None
    role = tenant_role(app, user, project["tenant_id"]) if project else None
    return (project, role) if role else None


def use_invite(app: App, token: str, user: dict, ip: str) -> dict:
    """Join the invite's tenant. One use; expired, used, or meant for another email -> refused."""
    with app.db.tx():
        invite = app.db.one("SELECT * FROM invites WHERE token_hash = ?", token_hash(str(token or "")))
        if not invite or invite["used"] or invite["expires"] < time.time():
            raise HttpError(410, "This invite link is not valid any more. Ask for a new one.")
        if invite["email"] and invite["email"] != user["email"]:
            raise HttpError(403, f"This invite is for {invite['email']}. Sign in with that account.")
        if app.db.one("SELECT 1 FROM members WHERE tenant_id = ? AND user_id = ?", invite["tenant_id"], user["id"]):
            raise HttpError(409, "You are already a member of this workspace.")
        app.db.run("INSERT INTO members VALUES (?, ?, ?)", invite["tenant_id"], user["id"], invite["role"])
        app.db.run("UPDATE invites SET used = ?, used_by = ? WHERE id = ?", time.time(), user["id"], invite["id"])
    audit_log(app, "invite_accepted", user["id"], ip, invite["tenant_id"], invite["role"])
    return invite


# ---------------------------------------------------------------------------
# HTTP
# ---------------------------------------------------------------------------

APP: App | None = None
ROUTES: list[tuple[str, re.Pattern, str, object]] = []
STATIC_TYPES = {".html": "text/html; charset=utf-8", ".js": "text/javascript; charset=utf-8",
                ".css": "text/css; charset=utf-8", ".svg": "image/svg+xml"}
MAX_JSON = 64 * 1024
MAX_PROXY_BODY = 20 * 1024 * 1024
PROXY_TIMEOUT = 75.0  # longer than serve.py's 25 s long-poll
WS_IDLE = 120.0  # the editor pings every 20 s
DEVICE_SECONDS = 365 * 86400  # a known-device cookie lasts this long
RECHECK = 15.0  # an open WebSocket re-checks the session and role this often
CSP = ("default-src 'none'; script-src 'self'; style-src 'self'; img-src 'self' data:; font-src 'self'; "
       "connect-src 'self'; form-action 'self'; frame-ancestors 'none'; base-uri 'none'")
# Request headers passed to a worker; everything else (Cookie, Authorization, X-Host-*, X-Forwarded-*) stays here.
FORWARD = ("Host", "Origin", "Content-Type", "Content-Length", "Accept", "Accept-Language", "User-Agent",
           "Sec-WebSocket-Key", "Sec-WebSocket-Version", "Sec-WebSocket-Protocol", "If-None-Match")
DROP_RESPONSE = {"set-cookie", "connection", "keep-alive", "transfer-encoding", "server", "date",
                 "strict-transport-security", "x-frame-options", "x-content-type-options", "referrer-policy"}
# Added to every proxied response. The worker's own CSP stays; a second CSP header is enforced as well (browsers
# apply both), so this can only narrow it.
PROXY_HEADERS = ["X-Frame-Options: DENY", "X-Content-Type-Options: nosniff", "Referrer-Policy: no-referrer",
                 "Content-Security-Policy: frame-ancestors 'none'"]
QUOTA_PATHS = {"/api/file", "/api/upload", "/api/fs"}
ZOTERO_SLOTS = threading.BoundedSemaphore(4)  # Zotero fetches in flight on the whole server


def route(method: str, pattern: str, need: str = "user"):
    """need: anon (anyone), mfa (password done, code pending), user (signed in), admin (site admin)."""
    def register(fn):
        ROUTES.append((method, re.compile(pattern), need, fn))
        return fn
    return register


class Server(ThreadingHTTPServer):
    """A thread per connection, at most `limit` at once: further connections get an immediate 503."""

    daemon_threads = True

    def __init__(self, address, handler, limit: int) -> None:
        self.slots = threading.BoundedSemaphore(limit)
        super().__init__(address, handler)

    def process_request(self, request, client_address) -> None:
        if not self.slots.acquire(blocking=False):
            try:
                request.sendall(b"HTTP/1.0 503 Service Unavailable\r\nRetry-After: 5\r\nContent-Length: 0\r\n"
                                b"Connection: close\r\n\r\n")
            except OSError:
                pass
            self.shutdown_request(request)
            return
        try:
            super().process_request(request, client_address)
        except BaseException:
            self.slots.release()
            raise

    def process_request_thread(self, request, client_address) -> None:
        try:
            super().process_request_thread(request, client_address)
        finally:
            self.slots.release()


class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.0"
    rbufsize = 0  # Unbuffered: after a WebSocket upgrade no client bytes may be stuck in our buffer.
    timeout = 60
    server_version = "latex-host"
    sys_version = ""

    def log_message(self, *args) -> None:
        pass

    # --- plumbing ---------------------------------------------------------------------------------------------

    def headers_out(self, extra) -> list[tuple[str, str]]:
        found = [("X-Content-Type-Options", "nosniff"), ("Referrer-Policy", "no-referrer"),
                 ("X-Frame-Options", "DENY"), ("Cross-Origin-Opener-Policy", "same-origin"),
                 ("Content-Security-Policy", CSP), ("Cache-Control", "no-store")]
        if APP.https:
            found.append(("Strict-Transport-Security", "max-age=63072000"))
        return found + list(extra.items() if isinstance(extra, dict) else extra or [])

    def send(self, status: int, body: bytes, kind: str, extra=None) -> None:
        self.send_response(status)
        self.send_header("Content-Type", kind)
        self.send_header("Content-Length", str(len(body)))
        for key, value in self.headers_out(extra):
            self.send_header(key, value)
        self.end_headers()
        self.wfile.write(body)

    def send_json(self, data, status: int = 200, extra=None) -> None:
        self.send(status, json.dumps(data).encode(), "application/json", extra)

    def redirect(self, location: str, extra=None) -> None:
        self.send(302, b"", "text/plain", [("Location", location), *(extra or [])])

    def linger(self) -> None:
        """After refusing a body without reading it: end our side, then drain a little of what the client still sends,
        so closing does not reset the connection (Windows drops the reply on a reset)."""
        self.close_connection = True
        self.wfile.flush()
        self.connection.shutdown(socket.SHUT_WR)
        self.connection.settimeout(2)
        deadline, drained = time.monotonic() + 2, 0
        while time.monotonic() < deadline and drained < 64 * 1024 * 1024:
            chunk = self.rfile.read1(65536) if hasattr(self.rfile, "read1") else self.rfile.read(65536)
            if not chunk:
                break
            drained += len(chunk)

    def read_exact(self, size: int) -> bytes:
        data = bytearray()
        while len(data) < size:
            chunk = self.rfile.read(min(65536, size - len(data)))
            if not chunk:
                raise HttpError(400, "The request body ended early.")
            data += chunk
        return bytes(data)

    def body_size(self, limit: int) -> int:
        if self.headers.get("Transfer-Encoding"):
            raise HttpError(411, "Send a Content-Length.")
        try:
            size = int(self.headers.get("Content-Length") or 0)
        except ValueError:
            size = -1
        if not 0 <= size <= limit:
            raise HttpError(413, "The request is too large.")
        return size

    def json_body(self) -> dict:
        size = self.body_size(MAX_JSON)
        if "json" not in self.headers.get("Content-Type", ""):
            raise HttpError(415, "Expected application/json.")
        try:
            data = json.loads(self.read_exact(size) or b"{}")
        except ValueError:
            raise HttpError(400, "Bad JSON.")
        if not isinstance(data, dict):
            raise HttpError(400, "Expected a JSON object.")
        return data

    def client_ip(self) -> str:
        if APP.config["trust_proxy"]:
            forwarded = self.headers.get("X-Forwarded-For", "")
            if forwarded:
                return forwarded.split(",")[-1].strip()[:64]  # The proxy appends the address it saw.
        return self.client_address[0]

    def origin_ok(self) -> bool:
        """State-changing requests must come from our own pages (CSRF), or from no browser page at all is refused."""
        origin = self.headers.get("Origin")
        return origin is not None and hmac.compare_digest(origin.rstrip("/"), APP.config["public_url"])

    def host_ok(self) -> bool:
        return (self.headers.get("Host") or "").lower() == APP.public.netloc.lower()

    def cookie_value(self, name: str) -> str | None:
        jar = SimpleCookie()
        try:
            jar.load(self.headers.get("Cookie", ""))
        except Exception:  # noqa: BLE001 - a malformed Cookie header is "no cookie".
            return None
        morsel = jar.get(name)
        return morsel.value if morsel else None

    def set_cookie(self, name: str, value: str, max_age: int, path: str = "/") -> tuple[str, str]:
        secure = "; Secure" if APP.https else ""
        return "Set-Cookie", f"{name}={value}; Path={path}; Max-Age={max_age}; HttpOnly; SameSite=Lax{secure}"

    # --- sessions ---------------------------------------------------------------------------------------------

    def load_session(self) -> dict | None:
        token = self.cookie_value(APP.cookie)
        if not token or len(token) > 100:
            return None
        return session_for(token_hash(token))

    def known_device(self, user: dict | None) -> str | None:
        """The device id from a valid known-device cookie of this user, else None."""
        parts = (self.cookie_value(APP.device_cookie) or "").split(".")
        if user and len(parts) == 3 and parts[0] == str(user["id"]) and len(parts[1]) <= 64 \
                and hmac.compare_digest(parts[2].encode(), device_mac(user["id"], parts[1]).encode()):
            return parts[1]
        return None

    def start_session(self, user: dict, stage: str) -> dict:
        """A new session id at every step up (password, then code): an old cookie never gains rights."""
        if self.session:
            APP.db.run("DELETE FROM sessions WHERE id_hash = ?", self.session["id_hash"])
        token, csrf, now = secrets.token_urlsafe(32), secrets.token_urlsafe(24), time.time()
        lifetime = APP.config["session_days"] * 86400 if stage == "full" else 600
        APP.db.run("INSERT INTO sessions VALUES (?, ?, ?, ?, ?, ?, ?, ?)", token_hash(token), user["id"], csrf, stage,
                   now, now, now + lifetime, self.ip)
        self.pending_cookies.append(self.set_cookie(APP.cookie, token, int(lifetime)))
        self.session = session_for(token_hash(token))
        if stage == "full":
            audit_log(APP, "login", user["id"], self.ip)
            if not self.known_device(user):  # This browser signed in: wrong passwords from others never lock it out.
                nonce = secrets.token_urlsafe(16)
                self.pending_cookies.append(self.set_cookie(
                    APP.device_cookie, f"{user['id']}.{nonce}.{device_mac(user['id'], nonce)}", DEVICE_SECONDS))
        return {"stage": stage, "csrf": csrf}

    # --- dispatch ---------------------------------------------------------------------------------------------

    def do_GET(self) -> None:
        self.dispatch()

    def do_POST(self) -> None:
        self.dispatch()

    def do_PUT(self) -> None:
        self.dispatch()

    def do_DELETE(self) -> None:
        self.dispatch()

    def dispatch(self) -> None:
        self.pending_cookies: list[tuple[str, str]] = []
        try:
            if not self.host_ok():
                raise HttpError(421, "Unknown host name (check public_url in config.toml).")
            url = urlsplit(self.path)
            self.query = parse_qs(url.query)
            self.ip = self.client_ip()
            self.ip_group = ip_key(self.ip)
            self.session = self.load_session()
            if host_mcp.endpoint(self, url.path):  # /mcp, /oauth/*, /.well-known/oauth-*: no cookies, no CSRF
                return
            if url.path.startswith("/p/"):
                self.project_proxy(url.path, url.query)
                return
            if self.command != "GET" and not self.origin_ok():
                raise HttpError(403, "Cross-site request refused.")
            found = next(((need, fn, match) for method, pattern, need, fn in ROUTES
                          if method == self.command for match in [pattern.fullmatch(url.path)] if match), None)
            if found is None:
                raise HttpError(404, "Not found.")
            need, fn, match = found
            self.check_need(need)
            if self.command != "GET" and self.session and url.path.startswith("/api/"):
                sent = self.headers.get("X-CSRF-Token") or ""
                if not hmac.compare_digest(sent.encode(), self.session["csrf"].encode()):
                    raise HttpError(403, "Your page is out of date: reload it and try again.")
            fn(self, *match.groups())
        except HttpError as exc:
            try:
                self.send_json({"error": exc.message}, exc.status, exc.headers)
                if exc.status in (411, 413, 507):
                    self.linger()
            except OSError:
                pass
        except (OSError, socket.timeout):
            pass
        except Exception:  # noqa: BLE001 - one bad request must not take the gateway down.
            traceback.print_exc()
            try:
                self.send_json({"error": "Internal error."}, 500)
            except OSError:
                pass

    def check_need(self, need: str) -> None:
        stage = self.session["stage"] if self.session else None
        if need == "anon":
            return
        if need == "mfa":
            if stage != "mfa":
                raise HttpError(401, "Sign in with your password first.")
            return
        if stage != "full":
            raise HttpError(401, "Sign in first.")
        if need == "admin" and not self.session["site_admin"]:
            raise HttpError(404, "Not found.")

    @property
    def user(self) -> dict:
        return self.session

    def ok(self, data=None, status: int = 200) -> None:
        self.send_json({"ok": True} if data is None else data, status,
                       self.pending_cookies)

    # --- static -----------------------------------------------------------------------------------------------

    def static(self, name: str) -> None:
        target = UI_DIR / name
        if "/" in name or "\\" in name or name.startswith(".") or not target.is_file() \
                or target.suffix not in STATIC_TYPES:
            raise HttpError(404, "Not found.")
        self.send(200, target.read_bytes(), STATIC_TYPES[target.suffix])

    # --- reverse proxy to the project's worker ----------------------------------------------------------------

    def project_proxy(self, path: str, query: str) -> None:
        match = re.fullmatch(r"/p/([^/]+)(/.*)?", path)
        if not match:
            raise HttpError(404, "Not found.")
        pid, rest = match.group(1), match.group(2)
        upgrade = self.headers.get("Upgrade", "").lower() == "websocket"
        if self.command == "GET" and not upgrade and self.headers.get("Sec-Fetch-Site") == "cross-site" and not (
                self.headers.get("Sec-Fetch-Mode") == "navigate" and self.headers.get("Sec-Fetch-Dest") == "document"):
            raise HttpError(403, "Cross-site request refused.")  # Another site's <img>, fetch or <script>.
        if not self.session or self.session["stage"] != "full":
            if self.command == "GET" and rest in (None, "/"):
                self.redirect("/#next=" + quote(f"/p/{pid}/"))
                return
            raise HttpError(401, "Sign in first.")
        found = project_access(APP, self.session, pid)
        if not found:
            raise HttpError(404, "No such project.")
        project, tenant_role_ = found
        if rest is None:
            self.redirect(f"/p/{pid}/")
            return
        role = "edit" if tenant_role_ in EDIT_ROLES else "view"
        if self.command not in ("GET", "POST", "PUT"):
            raise HttpError(405, "Method not allowed.")
        if (self.command != "GET" or upgrade) and not self.origin_ok():
            raise HttpError(403, "Cross-site request refused.")
        size = self.body_size(MAX_PROXY_BODY)
        if rest == "/api/ai":
            self.assistant(project, role, size)
            return
        if rest in ("/api/zotero", "/api/zotero/fetch", "/api/zotero/preview"):
            self.zotero_sync(rest, role, size)
            return
        if self.command != "GET" and rest in QUOTA_PATHS and role == "edit":
            limit = APP.settings()["max_project_mb"] * 1024 * 1024
            if APP.project_bytes(project) + size > limit:
                self.read_exact(size)  # read the accepted body so the client sees the reply, not a reset (Windows)
                raise HttpError(507, f"This project is over its {limit // 1048576} MB quota. Delete files first.")
        stream = upgrade or rest in ("/api/poll", "/events")
        if stream:
            with APP.streams_lock:
                if APP.streams[self.session["id"]] >= APP.config["max_streams_per_user"]:
                    raise HttpError(429, "Too many open editor tabs. Close some and reload.")
                APP.streams[self.session["id"]] += 1
        try:
            self.relay(project, pid, rest, query, role, upgrade, size, tenant_role_ == "admin")
        finally:
            if stream:
                with APP.streams_lock:
                    APP.streams[self.session["id"]] -= 1
                    if APP.streams[self.session["id"]] <= 0:
                        del APP.streams[self.session["id"]]

    def assistant(self, project: dict, role: str, size: int) -> None:
        """/p/<id>/api/ai stops here: the gateway holds the operator's key and the daily quotas, so the worker (and
        the LaTeX it runs) never sees either. GET says whether the editor may show the assistant."""
        cfg, key = ai_config(), ai_key()
        on = bool(cfg["enabled"] and key)
        uid, tid, day = self.session["id"], project["tenant_id"], time.strftime("%Y-%m-%d", time.gmtime())

        def left() -> int:
            """Requests left today: per person, per workspace, for the whole server (open sign-up makes one
            workspace per account), and none once the person's token budget is spent."""
            n = APP.db.one(
                "SELECT COALESCE(SUM(CASE WHEN user_id = ? THEN requests END), 0) AS mine, "
                "COALESCE(SUM(CASE WHEN user_id = ? THEN input_tokens + output_tokens END), 0) AS tokens, "
                "COALESCE(SUM(CASE WHEN tenant_id = ? THEN requests END), 0) AS team, "
                "COALESCE(SUM(requests), 0) AS total FROM ai_usage WHERE day = ?", uid, uid, tid, day)
            if n["tokens"] >= cfg["daily_tokens_per_user"]:
                return 0
            return max(0, min(cfg["daily_per_user"] - n["mine"], cfg["daily_per_workspace"] - n["team"],
                              cfg["daily_total"] - n["total"]))

        def record(usage: dict) -> None:
            """Settle the reservation made with the count: the billed tokens replace ai.MAX_TOKENS of output. A
            request Anthropic billed nothing for (refused before an answer, unreachable, bad request, limits) is
            given back whole, so it does not use up the daily count."""
            billed = bool(usage.get("input") or usage.get("output"))
            APP.db.run("UPDATE ai_usage SET requests = MAX(0, requests - ?), input_tokens = input_tokens + ?, "
                       "output_tokens = MAX(0, output_tokens + ?) WHERE day = ? AND user_id = ? AND tenant_id = ?",
                       0 if billed else 1, usage.get("input", 0), usage.get("output", 0) - ai.MAX_TOKENS,
                       day, uid, tid)

        reason = ("View-only members cannot use the assistant." if role != "edit"
                  else "The AI assistant is off on this server." if not on
                  else "The daily AI limit is reached; try again tomorrow." if not left() else None)
        if self.command == "GET":
            self.send_json({"enabled": reason is None, "model": cfg["model"], "notice": ai.NOTICE, "hosted": True,
                            "left": left() if on else 0, "reason": reason})
            return
        if self.command != "POST":
            raise HttpError(405, "Method not allowed.")
        if reason:
            raise HttpError(429 if role == "edit" and on else 403, reason)
        if size > ai.MAX_BODY:
            raise HttpError(413, "The request is too large for the assistant.")
        if "json" not in self.headers.get("Content-Type", ""):
            raise HttpError(415, "Expected application/json.")
        try:
            data = json.loads(self.read_exact(size) or b"{}")
        except ValueError:
            raise HttpError(400, "Bad JSON.")
        if not isinstance(data, dict):
            raise HttpError(400, "Expected a JSON object.")
        with APP.db.tx():  # count first: a slow request uses up its share while it runs (record() may give it back)
            if not left():
                raise HttpError(429, "The daily AI limit is reached; try again tomorrow.")
            # Reserve a whole reply's output while it runs, so parallel requests cannot overrun the token budget.
            APP.db.run("INSERT INTO ai_usage(day, user_id, tenant_id, requests, output_tokens) VALUES (?, ?, ?, 1, ?) "
                       "ON CONFLICT(day, user_id, tenant_id) DO UPDATE SET requests = requests + 1, "
                       "output_tokens = output_tokens + excluded.output_tokens", day, uid, tid, ai.MAX_TOKENS)
        if "stream" in self.query:
            self.assistant_stream(data, key, cfg["model"], record)
            return
        try:
            result = ai.ask(data, key=key, model=cfg["model"])
        except ai.AiError as exc:
            record(exc.usage)  # a refusal, cut-off or malformed answer is billed too
            raise HttpError(exc.status, str(exc))
        record(result["usage"])
        self.send_json(result)

    def zotero_sync(self, rest: str, role: str, size: int) -> None:
        """/p/<id>/api/zotero[/fetch] stops here: each person's own Zotero key (Account page) lives in host.db and
        never reaches a worker. GET says whether the editor may offer a sync; POST /fetch reads that person's
        library (at most HOSTED_PAGES pages) and returns its BibTeX. Comparing (CPU) and applying happen in the
        project's worker (/api/zotero/compare and /apply, no network), never in this shared process."""
        cfg = zotero_settings(self.session["id"])
        allowed = role == "edit" and zotero.configured(cfg)
        reason = ("View-only members cannot sync from Zotero." if role != "edit" else None if allowed
                  else "Add your Zotero library and API key on the Account page first.")
        if self.command == "GET" and rest == "/api/zotero":
            self.send_json({**zotero_public(cfg), "hosted": True, "can_sync": allowed, "reason": reason})
            return
        if self.command != "POST" or rest != "/api/zotero/fetch":
            raise HttpError(405, "Method not allowed.")
        if reason:
            raise HttpError(403 if role != "edit" else 400, reason)
        if size > MAX_JSON:
            raise HttpError(413, "Too large.")
        self.read_exact(size)  # nothing is needed from the body
        uid = self.session["id"]
        if APP.zotero_syncs.full(f"zotero:{uid}"):
            raise HttpError(429, "Too many Zotero syncs; wait a few minutes.")
        if not ZOTERO_SLOTS.acquire(blocking=False):  # a sync may wait a minute for Zotero: never pile threads up
            raise HttpError(503, "The server is busy syncing with Zotero; try again in a minute.",
                            {"Retry-After": "30"})
        try:
            with APP.streams_lock:  # a sync holds a thread like an open editor tab
                if APP.streams[uid] >= APP.config["max_streams_per_user"]:
                    raise HttpError(429, "Too many open editor tabs or syncs. Close some and try again.")
                APP.streams[uid] += 1
            try:
                APP.zotero_syncs.fail(f"zotero:{uid}")  # counted before the fetch: a slow one still counts
                text, version, cached = zotero.fetch(cfg, False, max_pages=zotero.HOSTED_PAGES,
                                                     max_total=zotero.HOSTED_TOTAL)
            finally:
                with APP.streams_lock:
                    APP.streams[uid] -= 1
                    if APP.streams[uid] <= 0:
                        del APP.streams[uid]
        except zotero.ZoteroError as exc:
            raise HttpError(exc.status, str(exc))
        finally:
            ZOTERO_SLOTS.release()
        self.send_json({"text": text, "version": version, "cached": cached, "source": "web"})

    def assistant_stream(self, data: dict, key: str, model: str, record) -> None:
        """?stream: the gateway reads Anthropic's stream itself and relays the answer as NDJSON (ai.relay); usage is
        recorded however it ends, also when the browser stops it or goes away."""
        spent = {"input": 0, "output": 0}

        def begin() -> None:
            self.send_response(200)
            self.send_header("Content-Type", "application/x-ndjson")
            for name, value in self.headers_out([("X-Accel-Buffering", "no")]):
                self.send_header(name, value)
            self.end_headers()

        def write(line: bytes) -> None:
            self.wfile.write(line)
            self.wfile.flush()

        try:
            ai.relay(ai.ask_stream(data, key=key, model=model, spent=spent), begin, write)
        except ai.AiError as exc:
            raise HttpError(exc.status, str(exc))
        finally:
            record(spent)

    def relay(self, project: dict, pid: str, rest: str, query: str, role: str, upgrade: bool, size: int,
              admin: bool = False) -> None:
        worker = APP.workers.acquire(project)
        upstream = None
        try:
            upstream = socket.create_connection(("127.0.0.1", worker.port), timeout=10)
            upstream.settimeout(PROXY_TIMEOUT)
            user = f"{self.session['id']};{self.session['name']}"
            lines = [f"{self.command} {rest}{'?' + query if query else ''} HTTP/1.1"]
            lines += [f"{name}: {self.headers[name]}" for name in FORWARD if self.headers.get(name) is not None]
            lines += ["Connection: Upgrade", "Upgrade: websocket"] if upgrade else ["Connection: close"]
            lines += [f"X-Host-Secret: {worker.secret}", f"X-Host-Role: {role}", f"X-Host-User: {quote(user, ';')}"]
            if admin:  # workspace admins may clean up version history; the worker trusts it only with the secret
                lines.append("X-Host-Admin: 1")
            upstream.sendall(("\r\n".join(lines) + "\r\n\r\n").encode("latin-1", "replace"))
            left = size
            while left:
                chunk = self.rfile.read(min(65536, left))
                if not chunk:
                    return
                upstream.sendall(chunk)
                left -= len(chunk)
            head = b""
            while b"\r\n\r\n" not in head:
                chunk = upstream.recv(65536)
                if not chunk or len(head) > 65536:
                    raise HttpError(502, "The project's editor did not answer.")
                head += chunk
            head, body = head.split(b"\r\n\r\n", 1)
            rows = head.decode("latin-1").split("\r\n")
            status = rows[0].split(" ", 2)
            switching = len(status) > 1 and status[1] == "101" and upgrade
            out = [rows[0]] + [r for r in rows[1:] if r.split(":", 1)[0].strip().lower() not in DROP_RESPONSE]
            out.append("Connection: Upgrade" if switching else "Connection: close")
            if not switching:
                out += PROXY_HEADERS
            if APP.https:
                out.append("Strict-Transport-Security: max-age=63072000")
            self.wfile.write(("\r\n".join(out) + "\r\n\r\n").encode("latin-1") + body)
            if switching:
                self.pump(upstream, pid, role, worker)
                return
            while True:
                chunk = upstream.recv(65536)
                if not chunk:
                    break
                self.wfile.write(chunk)
        except (OSError, socket.timeout):
            pass
        finally:
            if upstream is not None:
                upstream.close()
            APP.workers.release(worker)
            if self.command != "GET" and rest in QUOTA_PATHS:
                APP.sizes.pop(project["id"], None)

    def pump(self, upstream: socket.socket, pid: str, role: str, worker: Worker) -> None:
        """
        Relay a WebSocket both ways until either side closes. Blocking sendall is the backpressure: while one side
        does not read, we stop reading from the other. Every RECHECK seconds the session and role are checked again,
        so logging out, being removed or demoted closes the socket.
        """
        client = self.connection
        client.settimeout(30)
        upstream.settimeout(30)
        checked = traffic = time.monotonic()
        session = self.session["id_hash"]
        while True:
            ready, _, _ = select.select([client, upstream], [], [], 5.0)
            now = time.monotonic()
            if now - checked > RECHECK:
                checked = now
                current = session_for(session)
                found = project_access(APP, current, pid) if current and current["stage"] == "full" else None
                if not found or ("edit" if found[1] in EDIT_ROLES else "view") != role:
                    return
            if not ready:
                if now - traffic > WS_IDLE:
                    return
                continue
            for sock in ready:
                data = sock.recv(65536)
                if not data:
                    return
                (upstream if sock is client else client).sendall(data)
                traffic = worker.used = now  # Fair-share eviction picks the least recently active worker.


def session_for(id_hash: str) -> dict | None:
    """The session row joined with its user, or None when unknown, expired, idle too long or the user is disabled."""
    row = APP.db.one(
        "SELECT s.id_hash, s.csrf, s.stage, s.seen, s.expires, u.* FROM sessions s JOIN users u ON u.id = s.user_id "
        "WHERE s.id_hash = ?", id_hash)
    now = time.time()
    if not row:
        return None
    if row["disabled"] or row["expires"] < now or now - row["seen"] > APP.config["session_idle_hours"] * 3600:
        APP.db.run("DELETE FROM sessions WHERE id_hash = ?", id_hash)
        return None
    if now - row["seen"] > 60:
        APP.db.run("UPDATE sessions SET seen = ? WHERE id_hash = ?", now, id_hash)
    return row


# --- pages ------------------------------------------------------------------------------------------------------

@route("GET", r"/", "anon")
def page_index(h: Handler) -> None:
    h.static("index.html")


@route("GET", r"/ui/([^/]+)", "anon")
def page_static(h: Handler, name: str) -> None:
    h.static(name)


@route("GET", r"/fonts/([^/]+)", "anon")
def page_font(h: Handler, name: str) -> None:
    target = FONTS_DIR / name
    if name not in FONT_TYPES or not target.is_file():  # an exact allow-list: no traversal, no other files
        raise HttpError(404, "Not found.")
    h.send(200, target.read_bytes(), FONT_TYPES[name])


# --- session API ------------------------------------------------------------------------------------------------

def user_summary(user: dict) -> dict:
    tenants = APP.db.all(
        "SELECT t.id, t.name, m.role FROM members m JOIN tenants t ON t.id = m.tenant_id WHERE m.user_id = ? "
        "ORDER BY t.name", user["id"])
    if user["site_admin"]:
        mine = {t["id"] for t in tenants}
        every = APP.db.all("SELECT id, name FROM tenants ORDER BY name")
        tenants += [{**t, "role": "admin", "operator": True} for t in every if t["id"] not in mine]
    return {"id": user["id"], "email": user["email"], "name": user["name"], "site_admin": bool(user["site_admin"]),
            "totp": bool(user["totp_secret"]), "password": bool(user["pw_hash"]), "tenants": tenants}


@route("GET", r"/api/me", "anon")
def api_me(h: Handler) -> None:
    settings = APP.settings()
    session = h.session
    h.ok({
        "user": user_summary(session) if session and session["stage"] == "full" else None,
        "stage": session["stage"] if session else None, "csrf": session["csrf"] if session else None,
        "site": APP.config["site_name"], "signup": settings["signup_mode"],
        "providers": [{"id": name, "label": p.get("label") or name.title()}
                      for name, p in sorted(APP.config["providers"].items())],
        "templates": ["article", *build.TEMPLATES], "min_password": MIN_PASSWORD,
    })


@route("POST", r"/api/login", "anon")
def api_login(h: Handler) -> None:
    data = h.json_body()
    email = str(data.get("email") or "").strip().casefold()[:254]
    user = APP.db.one("SELECT * FROM users WHERE email = ?", email)
    # A browser this account signed in from before has its own backoff, so someone guessing the password
    # elsewhere cannot lock the owner out; failures from unknown browsers share the account's.
    device = h.known_device(user)
    account = f"{email}#{device}" if device else email
    wait = max(APP.login_ip.wait(h.ip_group), APP.login_account.wait(account))
    if wait:
        raise HttpError(429, f"Too many attempts. Try again in {int(wait) + 1} seconds.",
                        {"Retry-After": str(int(wait) + 1)})
    if not verify_password(data.get("password"), user["pw_hash"] if user else None):
        APP.login_ip.fail(h.ip_group)
        APP.login_account.fail(account)
        audit_log(APP, "login_failed", user["id"] if user else None, h.ip, detail=email)
        raise HttpError(401, "Wrong email or password.")
    if user["disabled"]:
        raise HttpError(403, "This account is disabled.")
    APP.login_account.clear(account)
    h.ok(h.start_session(user, "mfa" if user["totp_secret"] else "full"))


@route("POST", r"/api/login/code", "mfa")
def api_login_code(h: Handler) -> None:
    user = APP.db.one("SELECT * FROM users WHERE id = ?", h.session["id"])
    key = f"code:{user['id']}"
    wait = APP.codes.wait(key)
    if wait:
        raise HttpError(429, f"Too many wrong codes. Try again in {int(wait) + 1} seconds.",
                        {"Retry-After": str(int(wait) + 1)})
    code = norm_code(h.json_body().get("code"))
    if not check_second_factor(user, code):
        APP.codes.fail(key)
        audit_log(APP, "login_code_failed", user["id"], h.ip)
        raise HttpError(401, "That code is not right.")
    APP.codes.clear(key)
    h.ok(h.start_session(user, "full"))


def check_second_factor(user: dict, code: str) -> bool:
    """A current TOTP code (each one works once) or an unused recovery code (spent here)."""
    step = totp_match(user["totp_secret"], code, user["totp_step"]) if user["totp_secret"] else None
    if step is not None:  # Only the request whose UPDATE moved the step on wins; a parallel replay changes 0 rows.
        return APP.db.change("UPDATE users SET totp_step = ? WHERE id = ? AND totp_step < ?",
                             step, user["id"], step) == 1
    if len(code) == 12 and APP.db.change("DELETE FROM recovery_codes WHERE user_id = ? AND code_hash = ?",
                                         user["id"], token_hash(code)) == 1:
        audit_log(APP, "recovery_code_used", user["id"], None)
        return True
    return False


@route("POST", r"/api/logout", "anon")
def api_logout(h: Handler) -> None:
    if h.session:
        APP.db.run("DELETE FROM sessions WHERE id_hash = ?", h.session["id_hash"])
        audit_log(APP, "logout", h.session["id"], h.ip)
    h.send_json({"ok": True}, 200, [h.set_cookie(APP.cookie, "", 0)])


@route("POST", r"/api/signup", "anon")
def api_signup(h: Handler) -> None:
    data = h.json_body()
    if APP.signups.full(h.ip_group):
        raise HttpError(429, "Too many sign-ups from your network. Try again later.")
    email, name = norm_email(data.get("email")), clean_name(data.get("name"))
    password = check_password_rules(data.get("password"))
    invite = data.get("invite")
    if invite:
        found = APP.db.one("SELECT * FROM invites WHERE token_hash = ?", token_hash(str(invite)))
        if not found or found["used"] or found["expires"] < time.time():
            raise HttpError(410, "This invite link is not valid any more. Ask for a new one.")
        if found["email"] and found["email"] != email:
            raise HttpError(403, f"This invite is for {found['email']}.")
    else:
        # Open sign-up answers the same whether or not the address has an account (status, body, about the same
        # time, no session): the browser signs in next, and a wrong password there says only "wrong email or
        # password". Everything that can refuse comes before the lookup.
        signup_allowed(APP, email, verified=False)
        tenant_room(APP)
        APP.signups.fail(h.ip_group)
        if APP.db.one("SELECT id FROM users WHERE email = ?", email):
            hash_password(password)  # The time a new account takes.
            audit_log(APP, "signup_existing_email", None, h.ip, detail=email)
        else:
            uid = create_user(APP, email, name, password)
            audit_log(APP, "signup", uid, h.ip, detail=email)
            personal_tenant(APP, uid, name)
        h.ok({"signin": True})
        return
    APP.signups.fail(h.ip_group)
    # An invite sent to this exact address counts as proof of it (for linking sign-in providers later).
    uid = create_user(APP, email, name, password, verified=bool(found["email"]))
    user = APP.db.one("SELECT * FROM users WHERE id = ?", uid)
    audit_log(APP, "signup", uid, h.ip, detail=email)
    use_invite(APP, invite, user, h.ip)
    h.ok(h.start_session(user, "full"))


@route("POST", r"/api/reset", "anon")
def api_reset(h: Handler) -> None:
    data = h.json_body()
    password = check_password_rules(data.get("password"))
    with APP.db.tx():
        row = APP.db.one("SELECT * FROM resets WHERE token_hash = ?", token_hash(str(data.get("token") or "")))
        if not row or row["expires"] < time.time():
            raise HttpError(410, "This reset link is not valid any more. Ask the administrator for a new one.")
        APP.db.run("DELETE FROM resets WHERE user_id = ?", row["user_id"])
        APP.db.run("UPDATE users SET pw_hash = ? WHERE id = ?", hash_password(password), row["user_id"])
        APP.db.run("DELETE FROM sessions WHERE user_id = ?", row["user_id"])
    audit_log(APP, "password_reset", row["user_id"], h.ip)
    h.ok()


# --- invites ------------------------------------------------------------------------------------------------------

@route("GET", r"/api/invites/([A-Za-z0-9_-]{10,100})", "anon")
def api_invite_info(h: Handler, token: str) -> None:
    if APP.invite_tries.wait(h.ip_group):
        raise HttpError(429, "Too many tries. Try again later.")
    row = APP.db.one("SELECT i.*, t.name AS tenant FROM invites i JOIN tenants t ON t.id = i.tenant_id "
                     "WHERE token_hash = ?", token_hash(token))
    if not row or row["used"] or row["expires"] < time.time():
        APP.invite_tries.fail(h.ip_group)
        raise HttpError(410, "This invite link is not valid any more. Ask for a new one.")
    h.ok({"tenant": row["tenant"], "role": row["role"], "email": row["email"]})


@route("POST", r"/api/invites/([A-Za-z0-9_-]{10,100})/accept")
def api_invite_accept(h: Handler, token: str) -> None:
    invite = use_invite(APP, token, h.user, h.ip)
    h.ok({"tenant": invite["tenant_id"]})


# --- account ------------------------------------------------------------------------------------------------------

@route("GET", r"/api/account")
def api_account(h: Handler) -> None:
    user = h.user
    identities = APP.db.all("SELECT provider, email, created FROM identities WHERE user_id = ?", user["id"])
    left = APP.db.one("SELECT COUNT(*) AS n FROM recovery_codes WHERE user_id = ?", user["id"])["n"]
    h.ok({**user_summary(user), "identities": identities, "recovery_left": left})


@route("POST", r"/api/account")
def api_account_update(h: Handler) -> None:
    name = clean_name(h.json_body().get("name"))
    APP.db.run("UPDATE users SET name = ? WHERE id = ?", name, h.user["id"])
    h.ok()


def zotero_settings(user_id: int) -> dict:
    """The person's Zotero settings as zotero.fetch takes them, key included: for the gateway's own fetch only."""
    row = APP.db.one("SELECT * FROM zotero_settings WHERE user_id = ?", user_id) or {}
    return {"mode": "web", "library_type": row.get("library_type") or "users",
            "library_id": row.get("library_id") or "",
            "collection": row.get("collection") or "", "format": row.get("format") or "bibtex",
            "key": row.get("api_key") or ""}


def zotero_public(cfg: dict) -> dict:
    """What a reply may hold: never the key, only whether there is one."""
    shown = {k: cfg[k] for k in ("mode", "library_type", "library_id", "collection", "format")}
    return {**shown, "has_key": bool(cfg["key"]), "configured": zotero.configured(cfg), "local_ok": False}


@route("GET", r"/api/account/zotero")
def api_zotero(h: Handler) -> None:
    h.ok(zotero_public(zotero_settings(h.user["id"])))


@route("POST", r"/api/account/zotero")
def api_zotero_update(h: Handler) -> None:
    data = h.json_body()
    try:
        fields, key = zotero.check_settings({**data, "mode": "web"}, local=False), zotero.check_key(data.get("key"))
    except zotero.ZoteroError as exc:
        raise HttpError(exc.status, str(exc))
    old = zotero_settings(h.user["id"])["key"]
    key = None if data.get("clear_key") else key or old or None
    APP.db.run("INSERT INTO zotero_settings(user_id, library_type, library_id, collection, format, api_key) "
               "VALUES (?, ?, ?, ?, ?, ?) ON CONFLICT(user_id) DO UPDATE SET library_type = excluded.library_type, "
               "library_id = excluded.library_id, collection = excluded.collection, format = excluded.format, "
               "api_key = excluded.api_key", h.user["id"], fields["library_type"], fields["library_id"],
               fields["collection"], fields["format"], key)
    if key != old:
        audit_log(APP, "zotero_key_set" if key else "zotero_key_removed", h.user["id"], h.ip)
    h.ok(zotero_public(zotero_settings(h.user["id"])))


def require_password(h: Handler, data: dict) -> None:
    """Sensitive account changes ask for the password again (when the account has one)."""
    key = f"reauth:{h.user['id']}"
    if APP.codes.wait(key):
        raise HttpError(429, "Too many wrong passwords. Try again later.")
    if h.user["pw_hash"] and not verify_password(data.get("current"), h.user["pw_hash"]):
        APP.codes.fail(key)
        raise HttpError(403, "Your current password is not right.")


@route("POST", r"/api/account/password")
def api_password(h: Handler) -> None:
    data = h.json_body()
    require_password(h, data)
    password = check_password_rules(data.get("password"))
    with APP.db.tx():
        APP.db.run("UPDATE users SET pw_hash = ? WHERE id = ?", hash_password(password), h.user["id"])
        APP.db.run("DELETE FROM sessions WHERE user_id = ? AND id_hash != ?", h.user["id"], h.session["id_hash"])
    audit_log(APP, "password_changed", h.user["id"], h.ip)
    h.ok()


@route("POST", r"/api/account/totp/start")
def api_totp_start(h: Handler) -> None:
    secret = new_totp_secret()
    APP.db.run("UPDATE users SET totp_pending = ? WHERE id = ?", secret, h.user["id"])
    issuer = APP.config["site_name"]
    label = quote(f"{issuer}:{h.user['email']}")
    uri = f"otpauth://totp/{label}?{urlencode({'secret': secret, 'issuer': issuer, 'digits': 6, 'period': 30})}"
    h.ok({"secret": secret, "uri": uri})


@route("POST", r"/api/account/totp/enable")
def api_totp_enable(h: Handler) -> None:
    user = APP.db.one("SELECT * FROM users WHERE id = ?", h.user["id"])
    step = totp_match(user["totp_pending"], norm_code(h.json_body().get("code")), 0) if user["totp_pending"] else None
    if step is None:
        raise HttpError(400, "That code is not right. Check the time on your phone and try the next code.")
    codes = new_recovery_codes()
    with APP.db.tx():
        APP.db.run("UPDATE users SET totp_secret = totp_pending, totp_pending = NULL, totp_step = ? WHERE id = ?",
                   step, user["id"])
        APP.db.run("DELETE FROM recovery_codes WHERE user_id = ?", user["id"])
        for code in codes:
            APP.db.run("INSERT INTO recovery_codes VALUES (?, ?)", user["id"], token_hash(norm_code(code)))
    audit_log(APP, "totp_enabled", user["id"], h.ip)
    h.ok({"codes": codes})


@route("POST", r"/api/account/totp/disable")
def api_totp_disable(h: Handler) -> None:
    data = h.json_body()
    require_password(h, data)
    user = APP.db.one("SELECT * FROM users WHERE id = ?", h.user["id"])
    if user["totp_secret"] and not check_second_factor(user, norm_code(data.get("code"))):
        raise HttpError(403, "That code is not right.")
    with APP.db.tx():
        APP.db.run("UPDATE users SET totp_secret = NULL, totp_pending = NULL WHERE id = ?", user["id"])
        APP.db.run("DELETE FROM recovery_codes WHERE user_id = ?", user["id"])
    audit_log(APP, "totp_disabled", user["id"], h.ip)
    h.ok()


@route("POST", r"/api/account/recovery")
def api_recovery(h: Handler) -> None:
    require_password(h, h.json_body())
    if not h.user["totp_secret"]:
        raise HttpError(409, "Turn on two-step sign-in first.")
    codes = new_recovery_codes()
    with APP.db.tx():
        APP.db.run("DELETE FROM recovery_codes WHERE user_id = ?", h.user["id"])
        for code in codes:
            APP.db.run("INSERT INTO recovery_codes VALUES (?, ?)", h.user["id"], token_hash(norm_code(code)))
    audit_log(APP, "recovery_codes_renewed", h.user["id"], h.ip)
    h.ok({"codes": codes})


# --- tenants, members, invites ----------------------------------------------------------------------------------

def need_role(h: Handler, tenant_id: str, allowed: tuple) -> str:
    role = tenant_role(APP, h.user, tenant_id)
    if role is None:
        raise HttpError(404, "No such workspace.")
    if role not in allowed:
        raise HttpError(403, "Your role in this workspace does not allow that.")
    return role


@route("GET", r"/api/tenants/([0-9a-f]{16})")
def api_tenant(h: Handler, tid: str) -> None:
    role = need_role(h, tid, ROLES)
    tenant = APP.db.one("SELECT * FROM tenants WHERE id = ?", tid)
    projects = APP.db.all("SELECT p.id, p.name, p.created, u.name AS creator FROM projects p "
                          "LEFT JOIN users u ON u.id = p.created_by WHERE tenant_id = ? ORDER BY p.name", tid)
    out = {"id": tid, "name": tenant["name"], "role": role, "projects": projects,
           "limits": {k: APP.settings()[k] for k in ("max_projects_per_tenant", "max_project_mb")}}
    if role == "admin":
        out["members"] = APP.db.all("SELECT u.id, u.email, u.name, m.role FROM members m JOIN users u "
                                    "ON u.id = m.user_id WHERE m.tenant_id = ? ORDER BY u.name", tid)
        out["invites"] = APP.db.all("SELECT id, role, email, created, expires FROM invites WHERE tenant_id = ? "
                                    "AND used IS NULL AND expires > ? ORDER BY created DESC", tid, time.time())
    h.ok(out)


@route("POST", r"/api/tenants/([0-9a-f]{16})/invites")
def api_invite_create(h: Handler, tid: str) -> None:
    need_role(h, tid, ("admin",))
    data = h.json_body()
    role = data.get("role")
    if role not in ROLES:
        raise HttpError(400, "Pick a role: admin, editor or viewer.")
    email = norm_email(data.get("email")) if data.get("email") else None
    days = data.get("days", 7)
    if not (isinstance(days, int) and 1 <= days <= 30):
        raise HttpError(400, "Invites last 1 to 30 days.")
    token = secrets.token_urlsafe(24)
    APP.db.run("INSERT INTO invites(token_hash, tenant_id, role, email, created_by, created, expires) "
               "VALUES (?, ?, ?, ?, ?, ?, ?)", token_hash(token), tid, role, email, h.user["id"], time.time(),
               time.time() + days * 86400)
    audit_log(APP, "invite_created", h.user["id"], h.ip, tid, f"{role} {email or 'anyone'}")
    h.ok({"link": f"{APP.config['public_url']}/#invite={token}"})


@route("DELETE", r"/api/tenants/([0-9a-f]{16})/invites/(\d+)")
def api_invite_revoke(h: Handler, tid: str, iid: str) -> None:
    need_role(h, tid, ("admin",))
    APP.db.run("DELETE FROM invites WHERE id = ? AND tenant_id = ?", int(iid), tid)
    audit_log(APP, "invite_revoked", h.user["id"], h.ip, tid, iid)
    h.ok()


def admins_left(tid: str, without: int) -> int:
    return APP.db.one("SELECT COUNT(*) AS n FROM members WHERE tenant_id = ? AND role = 'admin' AND user_id != ?",
                      tid, without)["n"]


@route("POST", r"/api/tenants/([0-9a-f]{16})/members/(\d+)")
def api_member_role(h: Handler, tid: str, uid: str) -> None:
    need_role(h, tid, ("admin",))
    role = h.json_body().get("role")
    if role not in ROLES:
        raise HttpError(400, "Pick a role: admin, editor or viewer.")
    with APP.db.tx():
        member = APP.db.one("SELECT role FROM members WHERE tenant_id = ? AND user_id = ?", tid, int(uid))
        if not member:
            raise HttpError(404, "No such member.")
        if member["role"] == "admin" and role != "admin" and not admins_left(tid, int(uid)):
            raise HttpError(409, "A workspace needs at least one admin.")
        APP.db.run("UPDATE members SET role = ? WHERE tenant_id = ? AND user_id = ?", role, tid, int(uid))
    audit_log(APP, "role_changed", h.user["id"], h.ip, tid, f"user {uid}: {member['role']} -> {role}")
    h.ok()


@route("DELETE", r"/api/tenants/([0-9a-f]{16})/members/(\d+)")
def api_member_remove(h: Handler, tid: str, uid: str) -> None:
    need_role(h, tid, ("admin",))
    with APP.db.tx():
        member = APP.db.one("SELECT role FROM members WHERE tenant_id = ? AND user_id = ?", tid, int(uid))
        if not member:
            raise HttpError(404, "No such member.")
        if member["role"] == "admin" and not admins_left(tid, int(uid)):
            raise HttpError(409, "A workspace needs at least one admin.")
        APP.db.run("DELETE FROM members WHERE tenant_id = ? AND user_id = ?", tid, int(uid))
    audit_log(APP, "member_removed", h.user["id"], h.ip, tid, f"user {uid}")
    h.ok()


# --- projects ---------------------------------------------------------------------------------------------------

def new_project(h: Handler, tid: str, name: str, fill) -> dict:
    """Make the folder with fill(target), then the row; a failure leaves neither."""
    settings = APP.settings()
    count = APP.db.one("SELECT COUNT(*) AS n FROM projects WHERE tenant_id = ?", tid)["n"]
    if count >= settings["max_projects_per_tenant"]:
        raise HttpError(403, f"This workspace has its maximum of {count} projects. Delete one first.")
    project = {"id": secrets.token_hex(8), "tenant_id": tid, "name": name, "slug": slug_for(name)}
    root = APP.project_root(project)
    try:
        fill(root / project["slug"])
        APP.db.run("INSERT INTO projects VALUES (?, ?, ?, ?, ?, ?)", project["id"], tid, name, project["slug"],
                   h.user["id"], time.time())
    except BaseException:
        shutil.rmtree(root, ignore_errors=True)
        raise
    return project


@route("POST", r"/api/tenants/([0-9a-f]{16})/projects")
def api_project_create(h: Handler, tid: str) -> None:
    need_role(h, tid, EDIT_ROLES)
    data = h.json_body()
    name = clean_name(data.get("name"), "Project name")
    template = data.get("template") or "article"
    if template != "article" and template not in build.TEMPLATES:
        raise HttpError(400, "Unknown template.")

    def fill(target: Path) -> None:
        for rel, text in build.template_files(name, template).items():
            (target / rel).parent.mkdir(parents=True, exist_ok=True)
            (target / rel).write_text(text, encoding="utf-8")

    project = new_project(h, tid, name, fill)
    audit_log(APP, "project_created", h.user["id"], h.ip, tid, f"{project['id']} {name} ({template})")
    h.ok({"id": project["id"]})


IMPORTS = threading.BoundedSemaphore(2)  # zip uploads spooled and unpacked at once


@route("POST", r"/api/tenants/([0-9a-f]{16})/upload")
def api_project_upload(h: Handler, tid: str) -> None:
    need_role(h, tid, EDIT_ROLES)
    limit = APP.settings()["max_project_mb"] * 1024 * 1024
    size = h.body_size(APP.config["max_upload_mb"] * 1024 * 1024)
    name = clean_name((h.query.get("name") or [""])[0], "Project name")
    if not IMPORTS.acquire(timeout=5):
        raise HttpError(503, "Other uploads are being unpacked. Try again in a minute.", {"Retry-After": "30"})
    try:
        with tempfile.TemporaryFile(dir=APP.data) as spool:  # On disk, never the whole upload in memory.
            left = size
            while left:
                chunk = h.rfile.read(min(65536, left))
                if not chunk:
                    raise HttpError(400, "The request body ended early.")
                spool.write(chunk)
                left -= len(chunk)
            spool.seek(0)
            skipped: list[str] = []
            project = new_project(h, tid, name, lambda target: skipped.extend(unpack_zip(spool, target, limit)))
    finally:
        IMPORTS.release()
    audit_log(APP, "project_uploaded", h.user["id"], h.ip, tid, f"{project['id']} {name}")
    h.ok({"id": project["id"], "skipped": skipped})


@route("GET", r"/api/projects/([0-9a-f]{16})/zip")
def api_project_zip(h: Handler, pid: str) -> None:
    found = project_access(APP, h.user, pid)
    if not found:
        raise HttpError(404, "No such project.")
    project = found[0]
    with tempfile.TemporaryFile() as spool:
        zip_folder(APP.project_source(project), spool)
        size = spool.tell()
        spool.seek(0)
        h.send_response(200)
        h.send_header("Content-Type", "application/zip")
        h.send_header("Content-Length", str(size))
        ascii_name = re.sub(r"[^A-Za-z0-9._-]", "_", project["slug"])
        h.send_header("Content-Disposition", f'attachment; filename="{ascii_name}.zip"')
        for key, value in h.headers_out(None):
            h.send_header(key, value)
        h.end_headers()
        shutil.copyfileobj(spool, h.wfile, 65536)


@route("DELETE", r"/api/projects/([0-9a-f]{16})")
def api_project_delete(h: Handler, pid: str) -> None:
    found = project_access(APP, h.user, pid)
    if not found:
        raise HttpError(404, "No such project.")
    project, role = found
    if role not in EDIT_ROLES:
        raise HttpError(403, "Viewers cannot delete projects.")
    delete_project(project)
    audit_log(APP, "project_deleted", h.user["id"], h.ip, project["tenant_id"], f"{pid} {project['name']}")
    h.ok()


def delete_project(project: dict) -> None:
    APP.workers.stop(project["id"])
    APP.db.run("DELETE FROM projects WHERE id = ?", project["id"])
    shutil.rmtree(APP.project_root(project), ignore_errors=True)


# --- site admin -------------------------------------------------------------------------------------------------

@route("GET", r"/api/admin", "admin")
def api_admin(h: Handler) -> None:
    tenants = APP.db.all(
        "SELECT t.id, t.name, t.created, (SELECT COUNT(*) FROM members m WHERE m.tenant_id = t.id) AS members, "
        "(SELECT COUNT(*) FROM projects p WHERE p.tenant_id = t.id) AS projects FROM tenants t ORDER BY t.name")
    users = APP.db.all("SELECT id, email, name, site_admin, disabled, totp_secret IS NOT NULL AS totp, created "
                       "FROM users ORDER BY email")
    audit = APP.db.all("SELECT a.id, a.at, a.ip, a.action, a.tenant_id, a.detail, u.email FROM audit a "
                       "LEFT JOIN users u ON u.id = a.user_id ORDER BY a.id DESC LIMIT 300")
    with APP.workers.lock:
        workers = len(APP.workers.running)
    cfg = ai_config()
    usage = APP.db.all(
        "SELECT u.email, t.name AS workspace, SUM(a.requests) AS requests, SUM(a.input_tokens) AS input_tokens, "
        "SUM(a.output_tokens) AS output_tokens, MAX(a.day) AS last_day FROM ai_usage a "
        "LEFT JOIN users u ON u.id = a.user_id LEFT JOIN tenants t ON t.id = a.tenant_id WHERE a.day >= ? "
        "GROUP BY a.user_id, a.tenant_id ORDER BY requests DESC LIMIT 200",
        time.strftime("%Y-%m-%d", time.gmtime(time.time() - 29 * 86400)))
    h.ok({"settings": APP.settings(), "tenants": tenants, "users": users, "audit": audit, "workers": workers,
          "sandbox": APP.sandbox, "modes": SIGNUP_MODES,
          "ai": {"enabled": bool(cfg["enabled"] and ai_key()), "configured": cfg["enabled"], "model": cfg["model"],
                 "daily_per_user": cfg["daily_per_user"], "daily_per_workspace": cfg["daily_per_workspace"],
                 "daily_total": cfg["daily_total"], "daily_tokens_per_user": cfg["daily_tokens_per_user"],
                 "usage": usage}})


@route("POST", r"/api/admin/settings", "admin")
def api_admin_settings(h: Handler) -> None:
    data = h.json_body()
    settings = APP.save_settings(data)
    audit_log(APP, "settings_changed", h.user["id"], h.ip, detail=json.dumps(data)[:500])
    h.ok({"settings": settings})


@route("POST", r"/api/admin/tenants", "admin")
def api_admin_tenant_create(h: Handler) -> None:
    name = clean_name(h.json_body().get("name"), "Workspace name")
    tid = create_tenant(APP, name)
    audit_log(APP, "tenant_created", h.user["id"], h.ip, tid, name)
    h.ok({"id": tid})


@route("DELETE", r"/api/admin/tenants/([0-9a-f]{16})", "admin")
def api_admin_tenant_delete(h: Handler, tid: str) -> None:
    for project in APP.db.all("SELECT * FROM projects WHERE tenant_id = ?", tid):
        delete_project(project)
    APP.db.run("DELETE FROM tenants WHERE id = ?", tid)
    shutil.rmtree(APP.data / "projects" / tid, ignore_errors=True)
    audit_log(APP, "tenant_deleted", h.user["id"], h.ip, tid)
    h.ok()


@route("POST", r"/api/admin/users/(\d+)", "admin")
def api_admin_user(h: Handler, uid: str) -> None:
    data = h.json_body()
    if int(uid) == h.user["id"]:
        raise HttpError(409, "You cannot change your own admin rights or disable yourself.")
    if not APP.db.one("SELECT id FROM users WHERE id = ?", int(uid)):
        raise HttpError(404, "No such user.")
    with APP.db.tx():
        for key in ("disabled", "site_admin"):
            if key in data:
                if not isinstance(data[key], bool):
                    raise HttpError(400, f"{key} must be true or false.")
                APP.db.run(f"UPDATE users SET {key} = ? WHERE id = ?", int(data[key]), int(uid))
        if data.get("disabled"):
            APP.db.run("DELETE FROM sessions WHERE user_id = ?", int(uid))
    audit_log(APP, "user_changed", h.user["id"], h.ip, detail=f"user {uid}: {json.dumps(data)[:200]}")
    h.ok()


@route("POST", r"/api/admin/users/(\d+)/reset", "admin")
def api_admin_reset(h: Handler, uid: str) -> None:
    if not APP.db.one("SELECT id FROM users WHERE id = ?", int(uid)):
        raise HttpError(404, "No such user.")
    h.ok({"link": reset_link(int(uid))})
    audit_log(APP, "reset_link_created", h.user["id"], h.ip, detail=f"user {uid}")


def reset_link(uid: int) -> str:
    token = secrets.token_urlsafe(24)
    APP.db.run("INSERT INTO resets VALUES (?, ?, ?)", token_hash(token), uid, time.time() + 86400)
    return f"{APP.config['public_url']}/#reset={token}"


@route("POST", r"/api/admin/users/(\d+)/totp-off", "admin")
def api_admin_totp_off(h: Handler, uid: str) -> None:
    with APP.db.tx():
        APP.db.run("UPDATE users SET totp_secret = NULL, totp_pending = NULL WHERE id = ?", int(uid))
        APP.db.run("DELETE FROM recovery_codes WHERE user_id = ?", int(uid))
    audit_log(APP, "totp_removed_by_admin", h.user["id"], h.ip, detail=f"user {uid}")
    h.ok()


# ---------------------------------------------------------------------------
# Sign-in with OIDC providers (Google, any issuer) and GitHub: authorization code + PKCE + state (+ nonce)
# ---------------------------------------------------------------------------
#
# The ID token comes straight from the token endpoint over TLS (certificate and host name checked), which OIDC
# Core 3.1.3.7 (6) accepts instead of checking its signature; iss, aud (azp), exp, iat and nonce are checked here.

GITHUB = {"authorize": "https://github.com/login/oauth/authorize", "token": "https://github.com/login/oauth/access_token",
          "user": "https://api.github.com/user", "emails": "https://api.github.com/user/emails"}
FLOW_COOKIE = "lp_oauth"
FLOW_SECONDS = 600


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *args, **kwargs):
        return None  # A provider endpoint that redirects is an error, never a hop to somewhere else.


def http_json(method: str, url: str, form: dict | None = None, headers: dict | None = None):
    """One HTTPS request to a sign-in provider, JSON back. The only outbound network call host.py makes."""
    if not url.startswith("https://"):
        raise HttpError(502, "Sign-in provider endpoints must use https.")
    request = urllib.request.Request(url, data=urlencode(form).encode() if form is not None else None, method=method,
                                     headers={"Accept": "application/json", "User-Agent": "latex-host",
                                              **(headers or {})})
    try:
        with urllib.request.build_opener(_NoRedirect).open(request, timeout=10) as res:
            raw = res.read(1024 * 1024 + 1)
    except urllib.error.HTTPError as exc:
        raise HttpError(502, f"The sign-in provider refused the request (HTTP {exc.code}).")
    except (urllib.error.URLError, OSError, ValueError) as exc:
        raise HttpError(502, f"Could not reach the sign-in provider: {exc}")
    if len(raw) > 1024 * 1024:
        raise HttpError(502, "The sign-in provider sent too much data.")
    try:
        return json.loads(raw)
    except ValueError:
        raise HttpError(502, "The sign-in provider did not answer with JSON.")


def provider_secret(name: str, provider: dict) -> str:
    secret = provider.get("client_secret") or os.environ.get(str(provider.get("client_secret_env") or ""), "")
    if not secret:
        raise HttpError(500, f"Sign-in with {name} has no client secret (client_secret_env in config.toml).")
    return secret


def discover(name: str, provider: dict) -> dict:
    """The issuer's /.well-known/openid-configuration, cached for an hour; its issuer must be the configured one."""
    cached = APP.discovery.get(name)  # (when, meta); no default timestamp: monotonic() starts near 0 at boot
    if cached and time.monotonic() - cached[0] < 3600:
        return cached[1]
    issuer = provider["issuer"]
    meta = http_json("GET", issuer.rstrip("/") + "/.well-known/openid-configuration")
    if not isinstance(meta, dict) or meta.get("issuer") != issuer:
        raise HttpError(502, f"{name}: the discovery document names another issuer.")
    for key in ("authorization_endpoint", "token_endpoint"):
        if not str(meta.get(key, "")).startswith("https://"):
            raise HttpError(502, f"{name}: the discovery document has no https {key}.")
    APP.discovery[name] = (time.monotonic(), meta)
    return meta


def jwt_claims(token) -> dict:
    try:
        claims = json.loads(unb64(str(token).split(".")[1]))
    except (IndexError, ValueError):
        raise HttpError(502, "The sign-in provider sent a malformed ID token.")
    if not isinstance(claims, dict):
        raise HttpError(502, "The sign-in provider sent a malformed ID token.")
    return claims


def check_claims(claims: dict, issuer: str, client_id: str, nonce: str, now: float | None = None) -> None:
    now = time.time() if now is None else now
    audience = claims.get("aud")
    audiences = [audience] if isinstance(audience, str) else audience if isinstance(audience, list) else []
    problems = [
        (claims.get("iss") != issuer, "issuer"),
        (client_id not in audiences or (len(audiences) > 1 and claims.get("azp") != client_id), "audience"),
        (not isinstance(claims.get("exp"), (int, float)) or claims["exp"] < now - 60, "expiry"),
        (isinstance(claims.get("iat"), (int, float)) and claims["iat"] > now + 300, "issue time"),
        (not hmac.compare_digest(str(claims.get("nonce", "")).encode(), nonce.encode()), "nonce"),
        (not isinstance(claims.get("sub"), str) or not 0 < len(claims["sub"]) <= 255, "subject"),
    ]
    bad = [what for failed, what in problems if failed]
    if bad:
        raise HttpError(400, f"The sign-in could not be verified ({', '.join(bad)}). Try again.")


def oidc_identity(name: str, provider: dict, flow: dict, code: str) -> dict:
    meta = discover(name, provider)
    form = {"grant_type": "authorization_code", "code": code, "redirect_uri": redirect_uri(name),
            "code_verifier": flow["verifier"]}
    secret = provider_secret(name, provider)
    methods = meta.get("token_endpoint_auth_methods_supported") or ["client_secret_basic"]
    headers = {}
    if "client_secret_basic" in methods:
        pair = f"{quote(provider['client_id'], safe='')}:{quote(secret, safe='')}"
        headers["Authorization"] = "Basic " + base64.b64encode(pair.encode()).decode()
    else:
        form.update(client_id=provider["client_id"], client_secret=secret)
    tokens = http_json("POST", meta["token_endpoint"], form, headers)
    if not isinstance(tokens, dict) or not tokens.get("id_token"):
        raise HttpError(502, f"{name} sent no ID token.")
    claims = jwt_claims(tokens["id_token"])
    check_claims(claims, meta["issuer"], provider["client_id"], flow["nonce"])
    if "email" not in claims or "email_verified" not in claims:
        if not str(meta.get("userinfo_endpoint", "")).startswith("https://") or not tokens.get("access_token"):
            raise HttpError(502, f"{name} did not say which email address you use.")
        info = http_json("GET", meta["userinfo_endpoint"], None,
                         {"Authorization": f"Bearer {tokens['access_token']}"})
        if not isinstance(info, dict) or info.get("sub") != claims["sub"]:
            raise HttpError(502, f"{name}: the user info belongs to someone else.")
        claims = {**info, **{k: v for k, v in claims.items() if k in ("sub",)}}
    return {"subject": claims["sub"], "email": claims.get("email"),
            "verified": claims.get("email_verified") in (True, "true"), "name": claims.get("name")}


def github_identity(name: str, provider: dict, flow: dict, code: str) -> dict:
    tokens = http_json("POST", GITHUB["token"], {
        "client_id": provider["client_id"], "client_secret": provider_secret(name, provider), "code": code,
        "redirect_uri": redirect_uri(name), "code_verifier": flow["verifier"]})
    if not isinstance(tokens, dict) or not tokens.get("access_token"):
        raise HttpError(400, f"GitHub refused the sign-in: {str((tokens or {}).get('error_description', ''))[:200]}")
    auth = {"Authorization": f"Bearer {tokens['access_token']}", "Accept": "application/vnd.github+json"}
    user = http_json("GET", GITHUB["user"], None, auth)
    emails = http_json("GET", GITHUB["emails"], None, auth)
    primary = next((e for e in emails if isinstance(e, dict) and e.get("primary") and e.get("verified") is True),
                   None) if isinstance(emails, list) else None
    if not isinstance(user, dict) or not isinstance(user.get("id"), int) or not primary:
        raise HttpError(403, "Your GitHub account needs a verified primary email address.")
    return {"subject": str(user["id"]), "email": primary.get("email"), "verified": True,
            "name": user.get("name") or user.get("login")}


def redirect_uri(name: str) -> str:
    return f"{APP.config['public_url']}/auth/{name}/callback"


@route("GET", r"/auth/([a-z0-9-]{1,30})/start", "anon")
def auth_start(h: Handler, name: str) -> None:
    provider = APP.config["providers"].get(name)
    if not provider:
        raise HttpError(404, "Unknown sign-in provider.")
    intent = (h.query.get("intent") or ["login"])[0]
    invite = (h.query.get("invite") or [""])[0]
    if intent not in ("login", "link") or (invite and not re.fullmatch(r"[A-Za-z0-9_-]{10,100}", invite)):
        raise HttpError(400, "Bad sign-in request.")
    if intent == "link" and not (h.session and h.session["stage"] == "full"):
        raise HttpError(401, "Sign in first.")
    state, nonce, verifier = secrets.token_urlsafe(32), secrets.token_urlsafe(16), secrets.token_urlsafe(48)
    APP.db.run("INSERT INTO oauth_flows VALUES (?, ?, ?, ?, ?, ?, ?, ?)", token_hash(state), name, verifier, nonce,
               intent, h.session["id"] if intent == "link" else None, invite or None, time.time())
    params = {"response_type": "code", "client_id": provider["client_id"], "redirect_uri": redirect_uri(name),
              "state": state, "code_challenge": b64(hashlib.sha256(verifier.encode()).digest()),
              "code_challenge_method": "S256"}
    if provider["type"] == "github":
        endpoint, params["scope"] = GITHUB["authorize"], "read:user user:email"
    else:
        endpoint = discover(name, provider)["authorization_endpoint"]
        params.update(scope="openid email profile", nonce=nonce)
    joiner = "&" if "?" in endpoint else "?"
    h.redirect(endpoint + joiner + urlencode(params), [h.set_cookie(FLOW_COOKIE, state, FLOW_SECONDS, "/auth/")])


@route("GET", r"/auth/([a-z0-9-]{1,30})/callback", "anon")
def auth_callback(h: Handler, name: str) -> None:
    clear = h.set_cookie(FLOW_COOKIE, "", 0, "/auth/")
    try:
        target = finish_sign_in(h, name)
    except HttpError as exc:
        audit_log(APP, "oauth_failed", h.session["id"] if h.session else None, h.ip, detail=f"{name}: {exc.message}")
        h.redirect("/#error=" + quote(exc.message[:300]), [clear])
        return
    h.redirect(target, [clear, *h.pending_cookies])


def finish_sign_in(h: Handler, name: str) -> str:
    provider = APP.config["providers"].get(name)
    if not provider:
        raise HttpError(404, "Unknown sign-in provider.")
    if h.query.get("error"):
        raise HttpError(400, f"{name} said: {h.query['error'][0][:100]}")
    state, code = (h.query.get("state") or [""])[0], (h.query.get("code") or [""])[0]
    expected = h.cookie_value(FLOW_COOKIE) or ""
    if not state or not code or not hmac.compare_digest(state.encode(), expected.encode()):
        raise HttpError(400, "This sign-in was started in another browser or has expired. Try again.")
    with APP.db.tx():  # One use only.
        flow = APP.db.one("SELECT * FROM oauth_flows WHERE state_hash = ? AND provider = ?", token_hash(state), name)
        APP.db.run("DELETE FROM oauth_flows WHERE state_hash = ?", token_hash(state))
    if not flow or flow["created"] < time.time() - FLOW_SECONDS:
        raise HttpError(400, "This sign-in has expired. Try again.")
    ident = (github_identity if provider["type"] == "github" else oidc_identity)(name, provider, flow, code)
    if not ident.get("verified"):
        raise HttpError(403, f"Your email address at {name} is not verified. Verify it there first.")
    email = norm_email(ident.get("email"))
    linked = APP.db.one("SELECT user_id FROM identities WHERE provider = ? AND subject = ?", name, ident["subject"])
    if flow["intent"] == "link":
        if not h.session or h.session["stage"] != "full" or h.session["id"] != flow["user_id"]:
            raise HttpError(403, "Sign in again, then connect the account.")
        if linked and linked["user_id"] != h.session["id"]:
            raise HttpError(409, f"This {name} account is already connected to another user.")
        if not linked:
            add_identity(name, ident["subject"], h.session["id"], email, h.ip)
        return "/#account"
    if linked:
        user = APP.db.one("SELECT * FROM users WHERE id = ?", linked["user_id"])
    else:
        user = APP.db.one("SELECT * FROM users WHERE email = ?", email)
        if user:
            if not (APP.settings()["auto_link"] and user["email_verified"]):
                raise HttpError(409, f"An account for {email} already exists. Sign in with its password, then "
                                     f"connect {name} on your account page.")
            add_identity(name, ident["subject"], user["id"], email, h.ip)
        else:
            user = oauth_signup(h, name, flow, ident, email)
    if user["disabled"]:
        raise HttpError(403, "This account is disabled.")
    h.start_session(user, "mfa" if user["totp_secret"] else "full")
    return "/"


def add_identity(name: str, subject: str, user_id: int, email: str, ip: str) -> None:
    APP.db.run("INSERT INTO identities VALUES (?, ?, ?, ?, ?)", name, subject, user_id, email, time.time())
    audit_log(APP, "identity_linked", user_id, ip, detail=name)


def oauth_signup(h: Handler, name: str, flow: dict, ident: dict, email: str) -> dict:
    """A new account from a provider: through the invite the flow carried, else as the sign-up mode allows."""
    invite = flow["invite"]
    if invite:
        found = APP.db.one("SELECT * FROM invites WHERE token_hash = ?", token_hash(invite))
        if not found or found["used"] or found["expires"] < time.time():
            raise HttpError(410, "This invite link is not valid any more. Ask for a new one.")
    else:
        signup_allowed(APP, email, verified=True)
    if APP.signups.full(h.ip_group):
        raise HttpError(429, "Too many sign-ups from your network. Try again later.")
    APP.signups.fail(h.ip_group)
    try:
        display = clean_name(ident.get("name") or email.split("@")[0])
    except HttpError:
        display = email.split("@")[0][:80]
    uid = create_user(APP, email, display, None, verified=True)
    add_identity(name, ident["subject"], uid, email, h.ip)
    audit_log(APP, "signup", uid, h.ip, detail=f"{email} via {name}")
    user = APP.db.one("SELECT * FROM users WHERE id = ?", uid)
    if invite:
        use_invite(APP, invite, user, h.ip)
    else:
        personal_tenant(APP, uid, display)
    return user


@route("DELETE", r"/api/account/identities/([a-z0-9-]{1,30})")
def api_unlink(h: Handler, name: str) -> None:
    others = APP.db.one("SELECT COUNT(*) AS n FROM identities WHERE user_id = ? AND provider != ?", h.user["id"],
                        name)["n"]
    if not h.user["pw_hash"] and not others:
        raise HttpError(409, "Set a password first: otherwise you could not sign in any more.")
    APP.db.run("DELETE FROM identities WHERE user_id = ? AND provider = ?", h.user["id"], name)
    audit_log(APP, "identity_unlinked", h.user["id"], h.ip, detail=name)
    h.ok()


host_mcp.install(sys.modules[__name__])  # the consent and connected-clients API


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def housekeeping(stop: threading.Event) -> None:
    while not stop.wait(30):
        try:
            APP.workers.reap()
            now = time.time()
            APP.db.run("DELETE FROM sessions WHERE expires < ?", now)
            APP.db.run("DELETE FROM resets WHERE expires < ?", now)
            APP.db.run("DELETE FROM oauth_flows WHERE created < ?", now - 900)
            host_mcp.cleanup()
        except Exception:  # noqa: BLE001 - keep reaping.
            traceback.print_exc()


def sandbox_works() -> bool:
    if not shutil.which("bwrap"):
        return False
    try:
        return subprocess.run(["bwrap", "--unshare-all", "--ro-bind", "/", "/", "true"], capture_output=True,
                              timeout=20).returncode == 0
    except (OSError, subprocess.TimeoutExpired):
        return False


def private(*paths: Path) -> None:
    """Owner-only (600): config.toml may hold client secrets, host.db password hashes. SQLite gives its -wal and
    -shm files the database's mode."""
    for path in paths:
        try:
            if os.name != "nt" and path.stat().st_mode & 0o077:
                os.chmod(path, 0o600)
        except OSError:
            pass


def cmd_init(data: Path) -> int:
    data.mkdir(parents=True, exist_ok=True)
    if os.name != "nt":
        os.chmod(data, 0o700)
    config = data / "config.toml"
    if not config.exists():
        config.write_text(CONFIG_TEMPLATE, encoding="utf-8")
        print(f"Wrote {config}: set public_url (and providers) before serving.")
    global APP
    APP = App(data, load_config(data), sandbox=False)
    private(config, data / "host.db")
    print(f"Database {data / 'host.db'} at schema version {APP.db.version()}.")
    email = os.environ.get("LP_ADMIN_EMAIL") or (input("Site admin email: ") if sys.stdin.isatty() else "")
    if not email:
        print("No site admin created (set LP_ADMIN_EMAIL and LP_ADMIN_PASSWORD, or run interactively).")
        return 0
    email = norm_email(email)
    existing = APP.db.one("SELECT id FROM users WHERE email = ?", email)
    if existing:
        APP.db.run("UPDATE users SET site_admin = 1, disabled = 0 WHERE id = ?", existing["id"])
        print(f"{email} is a site admin. One-time password reset link (24 h): {reset_link(existing['id'])}")
        return 0
    password = os.environ.get("LP_ADMIN_PASSWORD") or getpass.getpass("Password (10+ characters): ")
    name = os.environ.get("LP_ADMIN_NAME") or "Admin"
    check_password_rules(password)
    uid = create_user(APP, email, clean_name(name), password, site_admin=True)
    audit_log(APP, "site_admin_created", uid, None, detail=email)
    print(f"Created site admin {email}.")
    return 0


def cmd_serve(data: Path, insecure: bool) -> int:
    global APP
    if not insecure:
        if not sys.platform.startswith("linux"):
            build.error("host.py serve runs on Linux only (builds are isolated with bubblewrap).")
            return 2
        if not sandbox_works():
            build.error("bubblewrap does not work here (`bwrap --unshare-all --ro-bind / / true` failed). Install "
                        "it (apt install bubblewrap) and allow unprivileged user namespaces. Refusing to serve.")
            return 2
    else:
        print("!" * 78 + "\nWARNING: --insecure-no-sandbox. LaTeX runs WITHOUT the bubblewrap sandbox: any user who "
              "can edit\na project can read and write files as this server's user. Use it for development only.\n"
              + "!" * 78, file=sys.stderr)
    if not (data / "host.db").exists():
        build.error(f"No database in {data}: run `host.py init --data {data}` first.")
        return 2
    APP = App(data, load_config(data), sandbox=not insecure)
    private(data / "config.toml", data / "host.db")
    host, port = APP.config["listen"], APP.config["port"]
    server = Server((host, port), Handler, APP.config["max_connections"])
    stop = threading.Event()
    threading.Thread(target=housekeeping, args=(stop,), daemon=True).start()
    import signal
    signal.signal(signal.SIGTERM, lambda *_: threading.Thread(target=server.shutdown).start())
    build.info(f"Gateway on http://{host}:{server.server_address[1]}/ for {APP.config['public_url']}")
    if host not in ("127.0.0.1", "::1", "localhost"):
        build.error(f"listen = {host!r}: the gateway should sit behind a TLS proxy (Caddy) on 127.0.0.1; it has no "
                    "TLS and no slow-client timeouts of its own.")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        stop.set()
        APP.workers.stop_all()
        server.server_close()
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description="Hosted LaTeX editor: accounts, workspaces and projects.")
    parser.add_argument("command", choices=("init", "serve"))
    parser.add_argument("--data", required=True, metavar="DIR", help="Database, config.toml and projects.")
    parser.add_argument("--insecure-no-sandbox", action="store_true",
                        help="Development only: run LaTeX without bubblewrap (and on any OS).")
    args = parser.parse_args()
    data = Path(args.data).resolve()
    try:
        if args.command == "init":
            return cmd_init(data)
        return cmd_serve(data, args.insecure_no_sandbox)
    except (ConfigError, HttpError) as exc:
        build.error(str(exc))
        return 2


if __name__ == "__main__":
    sys.exit(main())
