"""
Hosted Git sync (host.py): connect a project to a remote repository over https, pull it in, push the project out.

The gateway runs git itself, never a worker: the token is the person's own (Account page, `git_tokens` in host.db,
replies say `has_key` only) and goes to git in the environment (GIT_CONFIG_COUNT, an http.extraHeader), never on a
command line, in a reply, to a worker or into the project. The repository lives in <data>/git/<project id>, outside
the project area a worker (and the LaTeX it runs) can write, so nobody but the gateway sets its config or hooks.
Each git run has no system or global config, no hooks, no fsmonitor, no symlinks (checked out as plain files), no
submodules, only the https protocol, no redirects, and the remote host pinned to the address checked here
(http.curloptResolve), which must be a public one (SSRF: no loopback, private, link-local or reserved address after
DNS resolution). Timeouts and a size cap stop a transfer; the project quota is checked before files are written.
Pulls never resolve conflicts: on one, the merge is undone and the files are listed. Stdlib only, Python 3.9.
"""

from __future__ import annotations

import base64
import hashlib
import ipaddress
import os
import re
import shutil
import socket
import subprocess
import threading
import time
from pathlib import Path
from urllib.parse import urlsplit

import gitops

H = None  # the host module, set by install()

BRANCH = re.compile(r"[A-Za-z0-9][A-Za-z0-9._/-]{0,99}")
TOKEN = re.compile(r"[\x21-\x7e]{8,1000}")
USERNAME = re.compile(r"[A-Za-z0-9._@+-]{1,100}")
MAX_URL = 500
NET_TIMEOUT = 120.0
SLOTS = threading.BoundedSemaphore(4)  # transfers in flight on the whole server; never wait for one
LOCKS: dict = {}
LOCKS_GUARD = threading.Lock()
SIZE_FACTOR = 2  # the repository (history included) may use this many times the project quota


def repo_dir(project: dict) -> Path:
    return H.APP.data / "git" / project["id"]


def base_env() -> dict:
    """No inherited config, proxy, credential or askpass from the gateway's environment."""
    env = {"PATH": os.environ.get("PATH", os.defpath), "GIT_CONFIG_NOSYSTEM": "1", "GIT_CONFIG_GLOBAL": os.devnull,
           "GIT_TERMINAL_PROMPT": "0", "GIT_LITERAL_PATHSPECS": "1", "GIT_PROTOCOL_FROM_USER": "0",
           "LC_ALL": "C", "LANGUAGE": "C", "HOME": os.devnull}
    if os.environ.get("SYSTEMROOT"):  # Windows cannot start processes without it (tests run there too)
        env["SYSTEMROOT"] = os.environ["SYSTEMROOT"]
    return env


def config_pairs(gitdir: Path) -> list:
    return [("core.bare", "false"), ("core.fsmonitor", "false"), ("core.hooksPath", str(gitdir / "no-hooks")),
            ("core.symlinks", "false"), ("protocol.allow", "never"), ("protocol.https.allow", "always"),
            ("http.followRedirects", "false"),
            ("submodule.recurse", "false"), ("fetch.recurseSubmodules", "false"), ("gc.auto", "0")]


def repo_env(project: dict, extra_pairs=(), author: dict | None = None) -> dict:
    gitdir = repo_dir(project)
    env = base_env()
    env.update({"GIT_DIR": str(gitdir), "GIT_WORK_TREE": str(H.APP.project_source(project))})
    pairs = config_pairs(gitdir) + list(extra_pairs)
    env["GIT_CONFIG_COUNT"] = str(len(pairs))
    for i, (key, value) in enumerate(pairs):
        env[f"GIT_CONFIG_KEY_{i}"], env[f"GIT_CONFIG_VALUE_{i}"] = key, value
    if author:
        name = re.sub(r"[<>\x00-\x1f\x7f]", "", author.get("name") or "").strip() or "Hosted editor"
        email = re.sub(r"[<>\s\x00-\x1f\x7f]", "", author.get("email") or "") or "editor@localhost"
        env.update({"GIT_AUTHOR_NAME": name, "GIT_AUTHOR_EMAIL": email, "GIT_COMMITTER_NAME": name,
                    "GIT_COMMITTER_EMAIL": email})
    return env


def git(project: dict, args: list, check: bool = True, stdin: str | None = None, author: dict | None = None,
        timeout: float = 60.0) -> subprocess.CompletedProcess:
    return gitops.run(H.APP.project_source(project), args, timeout=timeout, stdin=stdin, check=check,
                      environ=repo_env(project, author=author))


def ensure_repo(project: dict, branch: str) -> None:
    gitdir = repo_dir(project)
    if (gitdir / "HEAD").is_file():
        return
    gitdir.parent.mkdir(parents=True, exist_ok=True)
    gitops.run(gitdir.parent, ["init", "-q", "--bare", "--template=", str(gitdir)], environ=base_env())
    git(project, ["symbolic-ref", "HEAD", f"refs/heads/{branch}"])


# --- checks --------------------------------------------------------------------------------------------------------

def check_branch(value) -> str:
    if not isinstance(value, str) or not BRANCH.fullmatch(value) or ".." in value or "//" in value \
            or value.endswith(("/", ".", ".lock")) or "/." in value:
        raise H.HttpError(400, "Not a valid branch name.")
    return value


def check_url(value) -> tuple[str, str, int]:
    """(url, host, port) of an https repository URL without credentials, query or fragment."""
    if not isinstance(value, str) or len(value) > MAX_URL or any(c.isspace() or ord(c) < 32 for c in value):
        raise H.HttpError(400, "Enter the repository's https URL.")
    try:
        parts = urlsplit(value)
        port = parts.port or 443
    except ValueError:
        raise H.HttpError(400, "Enter the repository's https URL.")
    if parts.scheme != "https" or not parts.hostname or parts.username or parts.password or parts.query \
            or parts.fragment or not parts.path.strip("/"):
        raise H.HttpError(400, "The repository URL must be https://host/path, without a name, password or query.")
    host = parts.hostname
    try:
        host.encode("ascii")
    except UnicodeError:
        try:
            host = host.encode("idna").decode("ascii")
        except UnicodeError:
            raise H.HttpError(400, "The repository's host name is not valid.")
    return value, host, port


def resolve(host: str, port: int) -> list:
    """Every address the host name has now (a seam the tests replace)."""
    return sorted({info[4][0] for info in socket.getaddrinfo(host, port, type=socket.SOCK_STREAM)})


def public_address(host: str, port: int) -> str:
    """The address git will use (pinned with http.curloptResolve), after refusing any non-public one."""
    try:
        found = resolve(host, port)
    except (OSError, UnicodeError):
        raise H.HttpError(502, "Could not find the repository's host.")
    if not found:
        raise H.HttpError(502, "Could not find the repository's host.")
    for address in found:
        ip = ipaddress.ip_address(address.split("%", 1)[0])
        if not ip.is_global or ip.is_multicast or getattr(ip, "ipv4_mapped", None):
            raise H.HttpError(400, "That repository's host is a local or private address; only public hosts work.")
    v4 = [a for a in found if ":" not in a]
    return v4[0] if v4 else f"[{found[0]}]"


# --- network -------------------------------------------------------------------------------------------------------

def transfer(project: dict, args: list, url: str, auth: str | None) -> subprocess.CompletedProcess:
    """
    One fetch or push to url, the only network git does here. The host is resolved and checked first and pinned for
    git; the run is stopped after NET_TIMEOUT or once the repository outgrows its cap.
    """
    _, host, port = check_url(url)
    pairs = [("http.curloptResolve", f"{host}:{port}:{public_address(host, port)}")]
    if auth:
        pairs.append(("http.extraHeader", f"Authorization: Basic {auth}"))
    env = repo_env(project, pairs)
    cap = H.APP.settings()["max_project_mb"] * 1024 * 1024 * SIZE_FACTOR
    gitdir = repo_dir(project)
    proc = subprocess.Popen([gitops.git_path(), *gitops.SAFE_CONFIG, *args], cwd=str(H.APP.project_source(project)),
                            stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.PIPE, env=env,
                            start_new_session=os.name == "posix")
    out: list = []
    reader = threading.Thread(target=lambda: out.append(proc.communicate()), daemon=True)
    reader.start()
    deadline, why = time.monotonic() + NET_TIMEOUT, None
    while reader.is_alive():
        reader.join(0.5)
        if not reader.is_alive():
            break
        if time.monotonic() > deadline:
            why = "The repository did not answer in time."
        elif H.folder_bytes(gitdir) > cap:
            why = "The repository is larger than this server allows."
        if why:
            proc.kill()
            reader.join(10)
            raise H.HttpError(507 if "larger" in why else 504, why)
    stdout, stderr = out[0] if out else (b"", b"")
    return subprocess.CompletedProcess(args, proc.returncode, stdout.decode("utf-8", "replace"),
                                       stderr.decode("utf-8", "replace"))


def credentials(user_id: int) -> str | None:
    row = H.APP.db.one("SELECT username, token FROM git_tokens WHERE user_id = ?", user_id)
    return base64.b64encode(f"{row['username']}:{row['token']}".encode()).decode() if row else None


def scrub(text: str) -> str:
    """git's message, its last part, never an Authorization header (git does not print one, but still)."""
    text = re.sub(r"(?i)authorization:[^\n]*", "Authorization: [hidden]", text or "")
    return text.strip()[-2000:]


# --- the work tree -------------------------------------------------------------------------------------------------

def tree_bytes(project: dict, rev: str) -> int:
    total = 0
    for row in git(project, ["ls-tree", "-r", "-l", "-z", rev]).stdout.split("\0"):
        meta = row.split("\t", 1)[0].split()
        if len(meta) == 4 and meta[1] == "blob":
            total += max(int(meta[3]) if meta[3].isdigit() else 0, H.ENTRY_BYTES)
    return total


def check_quota(project: dict, rev: str) -> None:
    """The files of rev must fit the project quota beside the build output. shortcut: counts rev's tree, not the
    merge result (files only here add to it); the worker refuses saves once the project is over quota anyway."""
    limit = H.APP.settings()["max_project_mb"] * 1024 * 1024
    others = H.folder_bytes(H.APP.project_root(project)) - H.folder_bytes(H.APP.project_source(project))
    if others + tree_bytes(project, rev) > limit:
        raise H.HttpError(507, f"The repository's files do not fit this project's {limit // 1048576} MB quota.")


def has_head(project: dict) -> bool:
    return git(project, ["rev-parse", "-q", "--verify", "HEAD"], check=False).returncode == 0


def snapshot(project: dict, author: dict) -> bool:
    """Commit the project's files as they are now (one commit by the person syncing); False when nothing changed."""
    git(project, ["add", "-A"])
    if has_head(project) and git(project, ["diff", "--cached", "--quiet"], check=False).returncode == 0:
        return False
    git(project, ["commit", "-q", "--no-verify", "-F", "-"], stdin=f"Update from the hosted editor ({project['name']})",
        author=author)
    return True


def first_checkout(project: dict, branch: str, rev: str) -> list:
    """The project had no commit yet: take the repository's files, but never overwrite a file that differs."""
    rows = [r for r in git(project, ["ls-tree", "-r", "-z", rev]).stdout.split("\0") if r]
    source, clash = H.APP.project_source(project), []
    blobs = []
    for row in rows:
        meta, path = row.split("\t", 1)
        if meta.split()[1] == "blob":
            blobs.append((meta.split()[2], path))
    for oid, path in blobs:
        target = source / path
        if not (target.exists() or target.is_symlink()):
            continue
        try:  # git's blob id; anything else in the way (a folder, a link, other bytes) is a clash
            data = target.read_bytes() if target.is_file() and not target.is_symlink() else None
        except OSError:
            data = None
        if data is None or hashlib.sha1(b"blob %d\0" % len(data) + data).hexdigest() != oid:
            clash.append(path)
    if clash:
        return clash
    git(project, ["symbolic-ref", "HEAD", f"refs/heads/{branch}"])
    git(project, ["reset", "-q", rev])
    git(project, ["checkout-index", "-a", "-f"])
    return []


# --- operations ----------------------------------------------------------------------------------------------------

def remote_row(project: dict) -> dict | None:
    return H.APP.db.one("SELECT * FROM git_remotes WHERE project_id = ?", project["id"])


def pull(project: dict, remote: dict, user: dict) -> dict:
    branch = remote["branch"]
    ensure_repo(project, branch)
    tracking = f"refs/remotes/origin/{branch}"
    done = transfer(project, ["fetch", "-q", "--no-tags", "--no-recurse-submodules", remote["url"],
                              f"+refs/heads/{branch}:{tracking}"], remote["url"], credentials(user["id"]))
    if done.returncode:
        if "couldn't find remote ref" in done.stderr:
            return {"ok": True, "conflicts": [],
                    "message": f"The repository has no branch {branch} yet: push to make it."}
        raise H.HttpError(502, "Could not fetch from the repository: " + scrub(done.stderr))
    check_quota(project, tracking)
    author = {"name": user["name"], "email": user["email"]}
    if not has_head(project):
        clash = first_checkout(project, branch, tracking)
        if clash:
            return {"ok": False, "conflicts": clash, "message": "Some files differ here and in the repository."}
        return {"ok": True, "conflicts": [], "message": "Pulled the repository's files."}
    snapshot(project, author)
    done = git(project, ["merge", "-q", "--no-edit", "--no-verify", tracking], check=False, author=author)
    if done.returncode:
        found = [p for p in git(project, ["diff", "--name-only", "-z", "--diff-filter=U"], check=False).stdout
                 .split("\0") if p]
        git(project, ["merge", "--abort"], check=False)
        if found:
            return {"ok": False, "conflicts": found, "message": "Nothing was merged."}
        raise H.HttpError(409, "Could not merge: " + scrub(done.stdout + done.stderr))
    return {"ok": True, "conflicts": [], "message": "Pulled."}


def push(project: dict, remote: dict, user: dict) -> dict:
    auth = credentials(user["id"])
    if not auth:
        raise H.HttpError(400, "Add a Git access token on your Account page first.")
    branch = remote["branch"]
    ensure_repo(project, branch)
    if not has_head(project):
        git(project, ["symbolic-ref", "HEAD", f"refs/heads/{branch}"])
    changed = snapshot(project, {"name": user["name"], "email": user["email"]})
    if not has_head(project):
        return {"ok": True, "message": "Nothing to push."}
    done = transfer(project, ["push", "-q", "--recurse-submodules=no", "--no-verify", remote["url"],
                              f"HEAD:refs/heads/{branch}"], remote["url"], auth)
    if done.returncode:
        if re.search(r"rejected|non-fast-forward|fetch first", done.stderr):
            raise H.HttpError(409, "The repository has commits this project does not have yet: pull first.")
        raise H.HttpError(502, "Could not push to the repository: " + scrub(done.stderr))
    return {"ok": True, "message": "Pushed." if changed else "Pushed (no new changes)."}


def project_lock(pid: str) -> threading.Lock:
    with LOCKS_GUARD:
        return LOCKS.setdefault(pid, threading.Lock())


def handle(h, project: dict, role: str) -> None:
    """/p/<id>/api/git/remote (GET: anyone in the project; POST: editors). Called by host.Handler.project_proxy."""
    row = remote_row(project)
    has_token = bool(H.APP.db.one("SELECT 1 FROM git_tokens WHERE user_id = ?", h.session["id"]))
    if h.command == "GET":
        h.send_json({"connected": bool(row), "url": row and row["url"], "branch": row and row["branch"],
                     "synced": row and row["synced"], "commit": row and row["commit_id"], "has_token": has_token})
        return
    if h.command != "POST":
        raise H.HttpError(405, "Method not allowed.")
    if role != "edit":
        raise H.HttpError(403, "Only editors can sync the project with Git.")
    data = h.json_body()
    op, uid, tid = data.get("op"), h.session["id"], project["tenant_id"]
    if op == "connect":
        url, host, port = check_url(data.get("url"))
        public_address(host, port)  # refused now, not only on the first pull
        branch = check_branch(data.get("branch") or "main")
        H.APP.db.run("INSERT INTO git_remotes(project_id, url, branch, user_id, created) VALUES (?, ?, ?, ?, ?) "
                     "ON CONFLICT(project_id) DO UPDATE SET url = excluded.url, branch = excluded.branch, "
                     "user_id = excluded.user_id, created = excluded.created, synced = NULL, commit_id = NULL",
                     project["id"], url, branch, uid, time.time())
        if row and row["branch"] != branch and (repo_dir(project) / "HEAD").is_file():
            shutil.rmtree(repo_dir(project), ignore_errors=True)  # a fresh start for the other branch
        H.audit_log(H.APP, "git_connected", uid, h.ip, tid, f"{project['id']} {url} {branch}")
        h.send_json({"ok": True})
        return
    if not row:
        raise H.HttpError(409, "Connect the project to a repository first.")
    if op == "disconnect":
        H.APP.db.run("DELETE FROM git_remotes WHERE project_id = ?", project["id"])
        with project_lock(project["id"]):
            shutil.rmtree(repo_dir(project), ignore_errors=True)
        H.audit_log(H.APP, "git_disconnected", uid, h.ip, tid, project["id"])
        h.send_json({"ok": True})
        return
    if op not in ("pull", "push"):
        raise H.HttpError(400, "Unknown Git operation.")
    if H.APP.git_syncs.full(f"git:{uid}"):
        raise H.HttpError(429, "Too many Git syncs; wait a few minutes.")
    lock = project_lock(project["id"])
    if not lock.acquire(blocking=False):
        raise H.HttpError(409, "Another Git sync of this project is running.")
    try:
        if not SLOTS.acquire(blocking=False):
            raise H.HttpError(503, "The server is busy with other Git syncs; try again in a minute.",
                              {"Retry-After": "30"})
        try:
            H.APP.git_syncs.fail(f"git:{uid}")  # counted before the transfer: a slow one still counts
            user = H.APP.db.one("SELECT id, name, email FROM users WHERE id = ?", uid)
            result = (pull if op == "pull" else push)(project, row, user)
        finally:
            SLOTS.release()
        head = git(project, ["rev-parse", "-q", "--verify", "HEAD"], check=False).stdout.strip() or None
        H.APP.db.run("UPDATE git_remotes SET synced = ?, commit_id = ? WHERE project_id = ?", time.time(), head,
                     project["id"])
        clash = " ".join(result.get("conflicts") or [])[:300]
        H.audit_log(H.APP, f"git_{op}", uid, h.ip, tid, f"{project['id']} {'conflicts: ' + clash if clash else 'ok'}")
    except gitops.GitError as exc:
        raise H.HttpError(exc.status, "Git: " + scrub(str(exc))[-1000:])
    finally:
        lock.release()
    h.send_json(result)


def forget(project: dict) -> None:
    """The project is deleted: its repository too."""
    shutil.rmtree(repo_dir(project), ignore_errors=True)


# --- the person's token (Account page) -----------------------------------------------------------------------------

def api_token(h) -> None:
    row = H.APP.db.one("SELECT username FROM git_tokens WHERE user_id = ?", h.user["id"])
    h.ok({"has_key": bool(row), "username": row["username"] if row else ""})


def api_token_update(h) -> None:
    data = h.json_body()
    uid = h.user["id"]
    if data.get("clear_token"):
        if H.APP.db.change("DELETE FROM git_tokens WHERE user_id = ?", uid):
            H.audit_log(H.APP, "git_token_removed", uid, h.ip)
        h.ok({"has_key": False, "username": ""})
        return
    token, username = data.get("token"), data.get("username")
    if not isinstance(token, str) or not TOKEN.fullmatch(token):
        raise H.HttpError(400, "Paste the access token (8 or more characters, no spaces).")
    if not isinstance(username, str) or not USERNAME.fullmatch(username):
        raise H.HttpError(400, "Enter your user name on the Git host.")
    H.APP.db.run("INSERT INTO git_tokens(user_id, username, token) VALUES (?, ?, ?) ON CONFLICT(user_id) DO UPDATE "
                 "SET username = excluded.username, token = excluded.token", uid, username, token)
    H.audit_log(H.APP, "git_token_set", uid, h.ip)
    h.ok({"has_key": True, "username": username})


def install(module) -> None:
    global H
    H = module
    module.route("GET", r"/api/account/git")(api_token)
    module.route("POST", r"/api/account/git")(api_token_update)
