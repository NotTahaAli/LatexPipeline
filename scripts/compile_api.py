"""
Remote compile API (stdlib only, Python 3.9): POST /api/v1/compile takes a document as a zip, a tar (plain, gzip,
bzip2 or xz) or a JSON files map and answers the PDF, or the build log when the build fails.

- Locally serve.py answers it for the owner only (no tokens; check_permission's default deny keeps share links out),
  for scripting: curl --data-binary @paper.zip -H "Content-Type: application/zip" http://127.0.0.1:8000/api/v1/compile
- Hosted, host.py calls endpoint() from Handler.dispatch for /api/v1/*: a bearer API token with the compile scope
  (host_tokens.py, the one personal token system: hashed, scoped, revocable, optional expiry) instead of a session,
  no cookies, a foreign Origin refused; size cap, hourly rate and daily quota per person, one compile per person
  and a few server-wide at once, a per-person cache keyed by the SHA-256 of the request, and an audit row per
  compile. The gateway never unpacks or builds anything: it forwards the body to a worker of its own per person
  (serve.py --gateway on a placeholder project under projects/_api/, LATEX_SANDBOX=bwrap and the project quota
  like every worker), whose
  POST /api/v1/compile unpacks it into its cache and builds it through build.build_document (sandbox.spawn()).
  The account page's token API is host_tokens.py's; install() only hands this module the host module.

Threat model as in host.py: every token holder is hostile; uploads are untrusted archives.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import http.client
import io
import json
import re
import secrets
import shutil
import tarfile
import threading
import time
import zipfile
from pathlib import Path, PurePosixPath
from urllib.parse import parse_qs, quote

import build

H = None  # the host module, set by install()

MAX_FILES = 2000  # files in one upload
LOCAL_MAX_BYTES = 200 * 1024 * 1024  # serve.py for its owner: unpacked bytes of one upload
RC_NAMES = {".latexmkrc", "latexmkrc", "build.toml"}  # serve.RC_NAMES: build configuration that can run programs
MAIN = re.compile(r"[^/\\\x00-\x1f]{1,100}\.tex")
API_TENANT = "_api"  # projects/_api/<api<user id>>/compile: the per-person compile workers (never a workspace id)
PLACEHOLDER = "\\documentclass{article}\n\\begin{document}\nCompile API.\n\\end{document}\n"
DEFAULTS = {"enabled": False, "compiles_per_hour": 60, "compiles_per_day": 300, "max_upload_mb": 20,
            "concurrent": 2, "cache_per_user": 20}
RANGES = {"compiles_per_hour": (1, 100000), "compiles_per_day": (1, 10 ** 6), "max_upload_mb": (1, 1000),
          "concurrent": (1, 100), "cache_per_user": (0, 1000)}
CONFIG_TEMPLATE = """
# Remote compile API (POST /api/v1/compile with an API token from the account page). Off unless enabled.
# Builds run in a sandboxed worker per person, like a project's; the project size quota applies to each upload.
# [api]
# enabled = true
# compiles_per_hour = 60             # requests per person per hour (cache hits included)
# compiles_per_day = 300             # builds per person per day (UTC; cache hits not counted)
# max_upload_mb = 20                 # largest request body
# concurrent = 2                     # builds at once on the whole server (one per person)
# cache_per_user = 20                # results kept per person (same request, same answer, no build)
"""
STATE: dict = {"busy": set(), "slots": None, "lock": threading.Lock(), "throttles": {}}


class UploadError(ValueError):
    """The upload cannot be used; the message says why (shown to the client)."""


# ---------------------------------------------------------------------------
# Uploads: zip, tar or JSON -> {relative path: bytes} (used by serve.py, never by the gateway)
# ---------------------------------------------------------------------------

def clean_path(name: str) -> str | None:
    """A safe relative POSIX path for an archive member, or None for a directory entry. Raises UploadError."""
    name = name.replace("\\", "/")
    while name.startswith("./"):
        name = name[2:]
    if name.endswith("/") or not name:
        return None
    parts = PurePosixPath(name).parts
    if name.startswith("/") or re.match(r"[A-Za-z]:", name) or any(p in ("", ".", "..") for p in parts) \
            or len(name) > 255 or any(ord(c) < 32 for c in name):
        raise UploadError(f"Unsafe path in the upload: {name[:80]!r}")
    return "/".join(parts)


def unpack(body: bytes, content_type: str, max_bytes: int, keep_rc: bool = False,
           max_files: int = MAX_FILES) -> tuple[dict[str, bytes], list[str]]:
    """
    The files of an upload: a zip, a tar (any compression tarfile reads) or JSON {"files": {path: text or
    {"base64": ...}}}. Only regular files; at most max_files and max_bytes unpacked (counted while reading, so a
    lying header does not help); paths relative without "..", unique ignoring case. One folder around everything
    is taken off. Build configuration (RC_NAMES) is left out unless keep_rc (the local owner), like a hosted
    project upload. Returns (files, names left out).
    """
    files: dict[str, bytes] = {}
    skipped: list[str] = []
    seen: set[str] = set()
    total = 0
    entries = 0

    def add(name: str, data: bytes) -> None:
        nonlocal total
        path = clean_path(name)
        if path is None:
            return
        if not keep_rc and path.rsplit("/", 1)[-1].lower() in RC_NAMES:
            skipped.append(path)
            return
        total += len(data)
        if total > max_bytes:
            raise UploadError(f"The upload unpacks to more than {max_bytes // 1048576} MB.")
        if len(files) >= max_files:
            raise UploadError(f"The upload has more than {max_files} files.")
        if path.casefold() in seen:
            raise UploadError(f"{path} appears twice in the upload.")
        seen.add(path.casefold())
        files[path] = data

    kind = content_type.split(";")[0].strip().lower()
    if kind == "application/json" or kind.endswith("+json"):
        try:
            data = json.loads(body)
        except ValueError:
            raise UploadError("Bad JSON.")
        found = data.get("files") if isinstance(data, dict) else None
        if not isinstance(found, dict):
            raise UploadError('Expected {"files": {"main.tex": "...", ...}}.')
        for name, value in found.items():
            if isinstance(value, str):
                add(name, value.encode("utf-8"))
            elif isinstance(value, dict) and isinstance(value.get("base64"), str):
                try:
                    add(name, base64.b64decode(value["base64"], validate=True))
                except ValueError:
                    raise UploadError(f"{name}: bad base64.")
            else:
                raise UploadError(f"{name}: give text or {{\"base64\": ...}}.")
    elif body[:4] == b"PK\x03\x04" or kind in ("application/zip", "application/x-zip-compressed"):
        try:
            with zipfile.ZipFile(io.BytesIO(body)) as archive:
                infos = archive.infolist()
                if len(infos) > 2 * max_files:
                    raise UploadError(f"The upload has more than {max_files} files.")
                for info in infos:
                    if info.flag_bits & 0x1:
                        raise UploadError("Encrypted archives are not supported.")
                    if info.is_dir() or (info.external_attr >> 16) & 0o170000 not in (0, 0o100000):
                        continue  # directories, symbolic links and other special entries
                    with archive.open(info) as member:
                        add(info.filename, member.read(max_bytes - total + 1))
        except (zipfile.BadZipFile, zipfile.LargeZipFile, NotImplementedError, RuntimeError, EOFError) as exc:
            raise UploadError(f"Bad zip file: {exc}")
    else:
        try:
            with tarfile.open(fileobj=io.BytesIO(body), mode="r:*") as archive:
                for member in archive:
                    entries += 1
                    if entries > 2 * max_files:
                        raise UploadError(f"The upload has more than {max_files} files.")
                    if member.isfile():
                        reader = archive.extractfile(member)
                        add(member.name, reader.read(min(member.size, max_bytes - total + 1)) if reader else b"")
                    elif not member.isdir():
                        continue  # links, devices, FIFOs: never written
        except (tarfile.TarError, EOFError, OSError, ValueError) as exc:
            raise UploadError(f"Send a zip, a tar archive or JSON ({exc}).")
    tops = {path.split("/", 1)[0] for path in files}
    if len(tops) == 1 and all("/" in path for path in files):
        files = {path.split("/", 1)[1]: data for path, data in files.items()}
    if not files:
        raise UploadError("The upload holds no files.")
    return files, skipped


def compile_files(files: dict[str, bytes], main: str, latexmk: str, work_root: Path) -> dict:
    """
    Write files into a fresh folder under work_root and build main through build.build_document (sandbox.spawn()
    like every build; the caller holds the build lock), the PDF and log into build.OUT_DIR/.api-compile/ (a build's
    output must lie below OUT_DIR). Both are removed afterwards.
    Returns {ok, pdf (bytes or None), log, seconds, pages, errors}.
    """
    if main not in files:
        raise UploadError(f"{main} is not in the upload (name it with ?main=).")
    tag = secrets.token_hex(8)
    job, out = work_root / tag, build.OUT_DIR / ".api-compile" / f"{tag}.pdf"
    src = job / "doc"
    try:
        for rel, data in files.items():
            (src / rel).parent.mkdir(parents=True, exist_ok=True)
            (src / rel).write_bytes(data)
        try:
            entry, _ = build.build_document(src / main, latexmk, False, False, validate=False,
                                            target=(job / "build", out))
        except Exception as exc:  # noqa: BLE001 - like build_safely: the client gets the reason, the server lives on.
            entry = {"ok": False, "seconds": 0, "pages": None, "errors": [], "error": f"{type(exc).__name__}: {exc}"}
        log_file = out.with_suffix(".log")
        log = log_file.read_text(encoding="utf-8", errors="replace") if log_file.exists() else entry.get("error") or ""
        pdf = out.read_bytes() if entry["ok"] and out.exists() else None
        return {"ok": bool(entry["ok"] and pdf), "pdf": pdf, "log": log, "seconds": entry.get("seconds"),
                "pages": entry.get("pages"), "errors": entry.get("errors", [])[:50], "skipped": []}
    finally:
        shutil.rmtree(job, ignore_errors=True)
        for path in (out, out.with_suffix(".log")):
            path.unlink(missing_ok=True)


def main_name(query: dict) -> str:
    main = (query.get("main") or ["main.tex"])[0]
    if not MAIN.fullmatch(main.replace("\\", "/").split("/")[-1]) or clean_path(main) != main:
        raise UploadError("main must name a .tex file of the upload.")
    return main


def output_kind(query: dict) -> str:
    kind = (query.get("output") or ["pdf"])[0]
    if kind not in ("pdf", "log", "json"):
        raise UploadError("output is pdf (the default: the PDF, or the log when the build fails), log or json.")
    return kind


def summary(result: dict) -> dict:
    return {"ok": result["ok"], "seconds": result["seconds"], "pages": result["pages"],
            "errors": [{k: e.get(k) for k in ("file", "line", "message")} for e in result["errors"][:20]],
            "skipped": result.get("skipped", [])[:20]}


def reply_parts(result: dict, kind: str) -> tuple[int, bytes, str, dict]:
    """
    (status, body, content type, headers) for a build result: the PDF, else the log (422 if the build failed);
    output=json gives both in one JSON object (the PDF in base64). X-Compile carries the summary either way.
    """
    meta = summary(result)
    headers = {"X-Compile": json.dumps(meta, ensure_ascii=True)[:8000]}
    status = 200 if result["ok"] else 422
    if kind == "json":
        pdf = base64.b64encode(result["pdf"]).decode() if result["pdf"] else None
        return status, json.dumps({**meta, "log": result["log"], "pdf": pdf}).encode(), "application/json", headers
    if result["ok"] and kind == "pdf":
        return 200, result["pdf"], "application/pdf", headers
    return status, result["log"].encode("utf-8"), "text/plain; charset=utf-8", headers


# ---------------------------------------------------------------------------
# Hosted (host.py): configuration, tokens, the endpoint
# ---------------------------------------------------------------------------

def check_config(config: dict, path) -> None:
    """Validate [api] in config.toml (called by host.load_config); fills in the defaults."""
    found = config.get("api", {})
    if not isinstance(found, dict) or set(found) - set(DEFAULTS):
        raise H.ConfigError(f"{path}: [api] takes only {', '.join(DEFAULTS)}")
    config["api"] = cfg = {**DEFAULTS, **found}
    if not isinstance(cfg["enabled"], bool):
        raise H.ConfigError(f"{path}: [api] enabled must be true or false")
    for key, (low, high) in RANGES.items():
        if not (isinstance(cfg[key], int) and not isinstance(cfg[key], bool) and low <= cfg[key] <= high):
            raise H.ConfigError(f"{path}: [api] {key} must be a whole number from {low} to {high}")


def cfg() -> dict:
    return {**DEFAULTS, **H.APP.config.get("api", {})}


def throttle(name: str, limit: int, window: float):
    key = (name, limit, window)
    if key not in STATE["throttles"]:
        STATE["throttles"][key] = H.Throttle(limit, window)
    return STATE["throttles"][key]


def bearer(h, scope: str) -> tuple[dict, dict]:
    """(token, user) for the request's API token with this scope (host_tokens.py, the one token system); 401/403."""
    user = H.host_tokens.bearer(h, scope)
    if user is None:
        raise H.HttpError(401, "Send a valid API token: Authorization: Bearer lpat_... (account page, API tokens).",
                          {"WWW-Authenticate": 'Bearer realm="api"'})
    return {"id": user["token_id"]}, user


def endpoint(h, path: str) -> bool:
    """/api/v1/*: bearer token only (no cookies, no CSRF), a foreign Origin refused."""
    if not path.startswith("/api/v1/"):
        return False
    if not cfg()["enabled"] or path != "/api/v1/compile":
        raise H.HttpError(404, "Not found.")
    if h.command != "POST":
        raise H.HttpError(405, "POST the document to this endpoint.", {"Allow": "POST"})
    origin = h.headers.get("Origin")
    if origin is not None and not hmac.compare_digest(origin.rstrip("/"), H.APP.config["public_url"]):
        raise H.HttpError(403, "Requests from other web pages are refused.")
    compile_hosted(h)
    return True


def day() -> str:
    return time.strftime("%Y-%m-%d", time.gmtime())


def compile_hosted(h) -> None:
    token, user = bearer(h, "compile")
    conf, uid = cfg(), user["id"]
    size = h.body_size(conf["max_upload_mb"] * 1024 * 1024)
    try:
        query = parse_qs(h.path.partition("?")[2])
        main, kind = main_name(query), output_kind(query)
    except UploadError as exc:
        h.read_exact(size)
        raise H.HttpError(400, str(exc))
    hourly = throttle("api-compile", conf["compiles_per_hour"], 3600)
    if hourly.full(str(uid)):
        h.read_exact(size)
        raise H.HttpError(429, "Too many compiles this hour; wait a while.", {"Retry-After": "600"})
    hourly.fail(str(uid))
    body = h.read_exact(size)
    content_type = h.headers.get("Content-Type", "application/octet-stream")[:200]
    key = hashlib.sha256(f"{uid}\0{main}\0{content_type}\0".encode() + body).hexdigest()
    cache = H.APP.data / "api-cache" / str(uid)
    began = time.monotonic()
    hit = cached(cache, key)
    if hit is not None:
        record(uid, cached_hit=True)
        H.audit_log(H.APP, "api_compile", uid, h.ip, None,
                    f"token {token['id'][:8]} cache hit {key[:12]} ok={hit['ok']}")
        send(h, hit, kind, "hit", began)
        return
    with STATE["lock"]:
        if STATE["slots"] is None or STATE["slots"][0] != conf["concurrent"]:
            STATE["slots"] = (conf["concurrent"], threading.BoundedSemaphore(conf["concurrent"]))
        slots = STATE["slots"][1]
        if uid in STATE["busy"]:
            raise H.HttpError(429, "Your previous compile is still running.", {"Retry-After": "5"})
        if not slots.acquire(blocking=False):
            raise H.HttpError(503, "The server is compiling for others right now; try again shortly.",
                              {"Retry-After": "10"})
        STATE["busy"].add(uid)
    try:
        with H.APP.db.tx():
            used = H.APP.db.one("SELECT compiles FROM api_usage WHERE day = ? AND user_id = ?", day(), uid)
            if used and used["compiles"] >= conf["compiles_per_day"]:
                raise H.HttpError(429, f"Daily limit of {conf['compiles_per_day']} compiles reached (UTC day).")
            record(uid, cached_hit=False)
        result = run_on_worker(user, body, content_type, main)
    finally:
        with STATE["lock"]:
            STATE["busy"].discard(uid)
        slots.release()
    store(cache, key, result, conf["cache_per_user"])
    H.audit_log(H.APP, "api_compile", uid, h.ip, None,
                f"token {token['id'][:8]} built {key[:12]} {len(body)} bytes ok={result['ok']} {result['seconds']}s")
    send(h, result, kind, "miss", began)


def record(uid: int, cached_hit: bool) -> None:
    column = "cached" if cached_hit else "compiles"
    H.APP.db.run(f"INSERT INTO api_usage(day, user_id, {column}) VALUES (?, ?, 1) ON CONFLICT(day, user_id) "
                 f"DO UPDATE SET {column} = {column} + 1", day(), uid)


def send(h, result: dict, kind: str, cache_state: str, began: float) -> None:
    status, body, content_type, headers = reply_parts(result, kind)
    headers.update({"X-Compile-Cache": cache_state, "X-Compile-Seconds": f"{time.monotonic() - began:.2f}"})
    h.send(status, body, content_type, headers)


def api_project(uid: int) -> dict:
    """The placeholder project a person's compile worker serves (Workers keys and caps workers by these fields)."""
    project = {"id": f"api{uid}", "tenant_id": API_TENANT, "slug": "compile", "name": "compile"}
    main = H.APP.project_source(project) / "main.tex"
    if not main.exists():
        main.parent.mkdir(parents=True, exist_ok=True)
        main.write_text(PLACEHOLDER, encoding="utf-8")
    return project


def run_on_worker(user: dict, body: bytes, content_type: str, main: str) -> dict:
    """Forward the upload to the person's compile worker; its answer as a result dict."""
    project = api_project(user["id"])
    worker = H.APP.workers.acquire(project)
    conn = None
    try:
        conn = http.client.HTTPConnection("127.0.0.1", worker.port, timeout=H.APP.settings()["build_timeout"] + 60)
        name = re.sub(r"[\x00-\x1f]", "", user["name"])[:60]
        conn.request("POST", f"/api/v1/compile?doc={quote(project['slug'])}&main={quote(main)}&output=json", body,
                     {"Host": f"127.0.0.1:{worker.port}", "Content-Type": content_type,
                      "X-Host-Secret": worker.secret, "X-Host-Role": "edit",
                      "X-Host-User": quote(f"{user['id']}-api;{name} (API)", ";")})
        res = conn.getresponse()
        raw = res.read(256 * 1024 * 1024)
    except (OSError, http.client.HTTPException):
        raise H.HttpError(502, "The compile worker did not answer; try again in a moment.")
    finally:
        if conn is not None:
            conn.close()
        H.APP.workers.release(worker)
        H.APP.sizes.pop(project["id"], None)
    try:
        data = json.loads(raw)
    except ValueError:
        data = None
    if res.status not in (200, 422) or not isinstance(data, dict) or "log" not in data:
        message = data.get("error") if isinstance(data, dict) else None
        raise H.HttpError(res.status if res.status in (400, 413, 507) else 502,
                          str(message or f"The compile worker refused the request ({res.status})."))
    return {"ok": bool(data["ok"]), "pdf": base64.b64decode(data["pdf"]) if data.get("pdf") else None,
            "log": str(data["log"]), "seconds": data.get("seconds"), "pages": data.get("pages"),
            "errors": data.get("errors") or [], "skipped": data.get("skipped") or []}


def cached(cache: Path, key: str) -> dict | None:
    meta = cache / f"{key}.json"
    try:
        info = json.loads(meta.read_text(encoding="utf-8"))
        log = (cache / f"{key}.log").read_text(encoding="utf-8", errors="replace")
        pdf = (cache / f"{key}.pdf").read_bytes() if info["ok"] else None
    except (OSError, ValueError, KeyError):
        return None
    meta.touch()
    return {**info, "log": log, "pdf": pdf}


def store(cache: Path, key: str, result: dict, keep: int) -> None:
    if keep <= 0:
        return
    cache.mkdir(parents=True, exist_ok=True)
    if result["pdf"] is not None:
        (cache / f"{key}.pdf").write_bytes(result["pdf"])
    (cache / f"{key}.log").write_text(result["log"], encoding="utf-8")
    info = {k: result.get(k) for k in ("ok", "seconds", "pages", "errors", "skipped")}
    (cache / f"{key}.json").write_text(json.dumps(info), encoding="utf-8")
    entries = sorted(cache.glob("*.json"), key=lambda p: p.stat().st_mtime, reverse=True)
    for old in entries[keep:]:
        for suffix in (".json", ".log", ".pdf"):
            old.with_suffix(suffix).unlink(missing_ok=True)


def install(module) -> None:
    global H
    H = module
