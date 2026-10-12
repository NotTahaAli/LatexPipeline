"""
Hosted Git sync (host_git.py) and the local Git panel (gitops.py through serve.py's /api/git) against a real https Git
server on loopback: `git http-backend` behind a Python https server with a throwaway self-signed certificate, which
checks the Authorization header the person's token produces. Nothing on the way is mocked except where the test host
points: host_git.public_address answers 127.0.0.1 for git.test only (production refuses loopback; every other host
still goes through the real check) and the test certificate is added to git's config as http.sslCAInfo. The token
header (GIT_CONFIG_COUNT), curloptResolve pinning, no redirects, no hooks, symlinks or submodules, the size cap, the
timeout and the quota all run for real. Loopback only, no network. Skipped without git or openssl.
"""

from __future__ import annotations

import base64
import os
import shutil
import ssl
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from unittest import mock

import _support  # noqa: F401 - puts scripts/ on sys.path
import host
import host_git
import serve
import test_host
from test_serve import ServerCase, SharedState

HOST = "git.test"  # resolves nowhere: only the curloptResolve pin makes git reach the server
# Linux only: Git for Windows and Apple's git may use a TLS backend that ignores http.sslCAInfo, and the timeout
# test reads /proc. CI runs these on ubuntu-latest.
HAVE_TOOLS = bool(shutil.which("git") and shutil.which("openssl")) and sys.platform.startswith("linux")
IDENTITY = {"GIT_AUTHOR_NAME": "Seed", "GIT_AUTHOR_EMAIL": "seed@example.org", "GIT_COMMITTER_NAME": "Seed",
            "GIT_COMMITTER_EMAIL": "seed@example.org", "GIT_CONFIG_GLOBAL": os.devnull, "GIT_CONFIG_NOSYSTEM": "1",
            "NO_PROXY": "*", "no_proxy": "*"}  # loopback only, even where an https proxy is configured


# A machine's proxy or CA settings would send the test's git elsewhere or distrust the test certificate.
NOT_HERE = ("GIT_SSL_", "CURL_CA_BUNDLE", "SSL_CERT_", "HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY", "GIT_AUTHOR_",
            "GIT_COMMITTER_")


def local_env(extra=None) -> dict:
    env = {k: v for k, v in os.environ.items() if not k.upper().startswith(NOT_HERE)}
    return {**env, **(extra or {})}


def run_git(cwd, *args, check=True, env=None):
    return subprocess.run(["git", *args], cwd=str(cwd), check=check, capture_output=True, text=True,
                          env=local_env({**IDENTITY, **(env or {})}))


def make_cert(folder: Path) -> tuple:
    cert, key = folder / "cert.pem", folder / "key.pem"
    subprocess.run(["openssl", "req", "-x509", "-newkey", "ec", "-pkeyopt", "ec_paramgen_curve:prime256v1", "-nodes",
                    "-keyout", str(key), "-out", str(cert), "-days", "2", "-subj", f"/CN={HOST}",
                    "-addext", f"subjectAltName=DNS:{HOST},IP:127.0.0.1",
                    "-addext", "basicConstraints=critical,CA:TRUE"], check=True, capture_output=True)
    return cert, key


class Landing(BaseHTTPRequestHandler):
    """A plain http listener a redirect points at: any request here means git followed it."""

    def do_GET(self):
        self.server.hits.append(self.path)
        self.send_response(404)
        self.send_header("Content-Length", "0")
        self.end_headers()

    do_POST = do_GET

    def log_message(self, *args):
        pass


class GitHandler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, *args):
        pass

    def do_GET(self):
        srv = self.server.git
        path, _, query = self.path.partition("?")
        auth = self.headers.get("Authorization")
        srv.requests.append((self.command, path, auth))
        body = self.body()
        first, _, rest = path.lstrip("/").partition("/")
        suffix = f"/landed/{rest}?{query}"
        if first == "redirect-http":
            return self.reply(302, b"", {"Location": f"http://127.0.0.1:{srv.landing_port}{suffix}"})
        if first == "redirect-host":  # another host (an address literal, so no pin is needed to follow it)
            return self.reply(302, b"", {"Location": f"https://127.0.0.1:{srv.port}{suffix}"})
        if first == "hang":
            srv.stop.wait(60)
            return self.reply(503, b"")
        slow = first == "slow"
        if slow:
            path = "/" + rest
        user = srv.user_for(auth)
        repo = path.split(".git/", 1)[0] + ".git"
        if (repo.strip("/") in srv.private or "receive-pack" in path + query) and not user:
            return self.reply(401, b"Authentication required\n", {"WWW-Authenticate": 'Basic realm="git"'})
        env = {"PATH": os.environ.get("PATH", os.defpath), "GIT_PROJECT_ROOT": str(srv.root),
               "GIT_HTTP_EXPORT_ALL": "1", "REQUEST_METHOD": self.command, "PATH_INFO": path, "QUERY_STRING": query,
               "CONTENT_TYPE": self.headers.get("Content-Type", ""), "CONTENT_LENGTH": str(len(body)),
               "REMOTE_ADDR": "127.0.0.1", "GIT_CONFIG_NOSYSTEM": "1", "GIT_CONFIG_GLOBAL": os.devnull,
               "HOME": str(srv.root)}
        if user:
            env["REMOTE_USER"] = user  # http-backend allows receive-pack for an authenticated user only
        if self.headers.get("Git-Protocol"):
            env["GIT_PROTOCOL"] = self.headers["Git-Protocol"]
        if self.headers.get("Content-Encoding"):
            env["HTTP_CONTENT_ENCODING"] = self.headers["Content-Encoding"]
        done = subprocess.run(["git", "http-backend"], input=body, env=env, capture_output=True, timeout=120)
        head, _, payload = done.stdout.partition(b"\r\n\r\n")
        status, headers = 200, {}
        for line in head.decode("latin-1").split("\r\n"):
            name, _, value = line.partition(":")
            if name.lower() == "status":
                status = int(value.split()[0])
            elif name:
                headers[name] = value.strip()
        self.reply(status, payload, headers, slow=slow)

    do_POST = do_GET

    def body(self) -> bytes:
        if self.headers.get("Transfer-Encoding", "").lower() == "chunked":
            data = b""
            while True:
                size = int(self.rfile.readline().split(b";")[0], 16)
                if not size:
                    self.rfile.readline()
                    return data
                data += self.rfile.read(size)
                self.rfile.readline()
        return self.rfile.read(int(self.headers.get("Content-Length") or 0))

    def reply(self, status, payload, headers=None, slow=False):
        self.send_response(status)
        for name, value in (headers or {}).items():
            self.send_header(name, value)
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        if not slow:
            self.wfile.write(payload)
            return
        for i in range(0, len(payload), 32 * 1024):  # about 1 MB/s: the transfer is still running when it is checked
            if self.server.git.stop.is_set():
                return
            self.wfile.write(payload[i:i + 32 * 1024])
            time.sleep(0.03)


class Quiet(ThreadingHTTPServer):
    daemon_threads = True

    def handle_error(self, request, client_address):
        pass  # git hung up (a stopped transfer): expected here


class GitServer:
    """https://git.test:<port>/<repo>.git served from <base>/repos; users maps a user name to the token it accepts."""

    def __init__(self, base: Path):
        self.root = base / "repos"
        self.root.mkdir(parents=True)
        self.cert, key = make_cert(base)
        self.users: dict = {}
        self.private: set = set()
        self.requests: list = []
        self.stop = threading.Event()
        self.httpd = Quiet(("127.0.0.1", 0), GitHandler)
        self.httpd.git = self
        context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
        context.load_cert_chain(str(self.cert), str(key))
        self.httpd.socket = context.wrap_socket(self.httpd.socket, server_side=True)
        self.port = self.httpd.server_address[1]
        self.landing = Quiet(("127.0.0.1", 0), Landing)
        self.landing.hits = []
        self.landing_port = self.landing.server_address[1]
        for server in (self.httpd, self.landing):
            threading.Thread(target=server.serve_forever, daemon=True).start()

    def close(self):
        self.stop.set()
        for server in (self.httpd, self.landing):
            server.shutdown()
            server.server_close()

    def url(self, repo: str, prefix: str = "") -> str:
        return f"https://{HOST}:{self.port}/{prefix}{repo}"

    def user_for(self, auth):
        if not auth or not auth.startswith("Basic "):
            return None
        try:
            name, _, token = base64.b64decode(auth[6:]).decode().partition(":")
        except ValueError:
            return None
        return name if name and self.users.get(name) == token else None

    def repo(self, name: str, seed: Path) -> Path:
        """A new bare repository <name> on the server and a local clone of it at seed (pushes to it by file path)."""
        bare = self.root / name
        run_git(self.root, "init", "-q", "--bare", "-b", "main", str(bare))
        run_git(self.root, "clone", "-q", str(bare), str(seed))
        return bare

    def client_config(self) -> list:
        """git -c pairs for a plain git client of this server (the local panel's repository config)."""
        return [("http.sslCAInfo", str(self.cert)), ("http.curloptResolve", f"{HOST}:{self.port}:127.0.0.1")]


def commit(seed: Path, files: dict, message="upstream", push=True) -> None:
    run_git(seed, "pull", "-q", "--no-rebase", "origin", "main", check=False)
    for name, data in files.items():
        target = seed / name
        target.parent.mkdir(parents=True, exist_ok=True)
        if isinstance(data, bytes):
            target.write_bytes(data)
        else:
            target.write_text(data, encoding="utf-8")
    run_git(seed, "add", "-A")
    run_git(seed, "commit", "-q", "-m", message)
    if push:
        run_git(seed, "push", "-q", "origin", "HEAD:refs/heads/main")


@unittest.skipUnless(HAVE_TOOLS, "needs git and openssl (Linux)")
class HostedLive(test_host.HostCase):
    """The gateway's Git sync over real https: connect, pull, push, conflicts, tokens, audit, hostile remotes."""

    TOKEN = "ghp_LiveTokenOfEd12345"

    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.TemporaryDirectory()
        cls.git = GitServer(Path(cls.tmp.name).resolve())

    @classmethod
    def tearDownClass(cls):
        cls.git.close()
        cls.tmp.cleanup()

    def setUp(self):
        super().setUp()
        mock.patch.dict(os.environ, IDENTITY).start()
        self.git.users.clear()
        self.git.private.clear()
        self.git.requests.clear()
        self.git.landing.hits.clear()
        self.tenant = host.create_tenant(self.app, "Team")
        self.user("ed@x.org", "editor", self.tenant)
        self.uid = self.app.db.one("SELECT id FROM users WHERE email = 'ed@x.org'")["id"]
        self.editor = self.client("ed@x.org")
        self.pid = test_host.Workspaces.make_project(self, self.editor, self.tenant, "Doc", "article")
        self.project = self.app.db.one("SELECT * FROM projects WHERE id = ?", self.pid)
        self.source = self.app.project_source(self.project)
        mock.patch.object(self.app.workers, "acquire", side_effect=AssertionError("no worker for Git")).start()
        real_check, real_pairs = host_git.public_address, host_git.config_pairs
        # Test only: the test host is loopback, which the real check refuses; anything else is checked for real.
        mock.patch.object(host_git, "public_address",
                          side_effect=lambda h, p: "127.0.0.1" if h == HOST else real_check(h, p)).start()
        mock.patch.object(host_git, "config_pairs",
                          side_effect=lambda d: real_pairs(d) + [("http.sslCAInfo", str(self.git.cert))]).start()
        name = self.id().rsplit(".", 1)[-1]
        self.repo_name = f"{name}/thesis.git"
        self.seed = self.data / "seed"
        self.bare = self.git.repo(self.repo_name, self.seed)
        self.url = self.git.url(self.repo_name)

    def sync(self, op, **extra):
        return self.editor.call("POST", f"/p/{self.pid}/api/git/remote?doc=Doc", {"op": op, **extra})

    def connect(self, url=None):
        status, data, _ = self.sync("connect", url=url or self.url, branch="main")
        self.assertEqual(status, 200, data)

    def set_token(self, token):
        status, data, _ = self.editor.call("POST", "/api/account/git", {"token": token, "username": "ed"})
        self.assertEqual(status, 200, data)

    def header(self, token):
        return "Basic " + base64.b64encode(f"ed:{token}".encode()).decode()

    def remote_file(self, name, ref="main"):
        return run_git(self.bare, "show", f"{ref}:{name}").stdout

    def actions(self):
        return [r["action"] for r in self.app.db.all("SELECT action FROM audit WHERE action LIKE 'git%' ORDER BY id")]

    def pulled_once(self, files=None):
        commit(self.seed, files or {"main.tex": "one\ntwo\nthree\nfour\nfive\n"})
        (self.source / "main.tex").unlink()
        self.connect()
        status, result, _ = self.sync("pull")
        self.assertEqual((status, result.get("ok")), (200, True), result)

    def test_connect_pull_edit_push_conflict_token_change_disconnect(self):
        commit(self.seed, {"main.tex": "one\ntwo\nthree\nfour\nfive\n", "ch1.tex": "Chapter\n"})
        self.git.private.add(self.repo_name)  # reading needs the token too
        self.git.users["ed"] = self.TOKEN
        self.connect()
        mine = (self.source / "main.tex").read_text(encoding="utf-8")
        status, data, _ = self.sync("pull")
        self.assertEqual(status, 502, data)  # private, and no token saved yet
        self.set_token(self.TOKEN)
        status, result, _ = self.sync("pull")
        self.assertEqual((status, result["ok"], result["conflicts"]), (200, False, ["main.tex"]))
        self.assertEqual((self.source / "main.tex").read_text(encoding="utf-8"), mine)  # never overwritten
        self.assertFalse((self.source / "ch1.tex").exists())
        (self.source / "main.tex").unlink()
        status, result, _ = self.sync("pull")
        self.assertEqual((status, result["ok"]), (200, True), result)
        self.assertEqual((self.source / "ch1.tex").read_text(encoding="utf-8"), "Chapter\n")
        sent = {auth for _, path, auth in self.git.requests if "upload-pack" in path}
        self.assertEqual(sent - {None}, {self.header(self.TOKEN)})  # the header from http.extraHeader

        (self.source / "ch2.tex").write_text("New chapter\n", encoding="utf-8")
        status, result, _ = self.sync("push")
        self.assertEqual((status, result["ok"]), (200, True), result)
        self.assertEqual(self.remote_file("ch2.tex"), "New chapter\n")
        self.assertEqual(run_git(self.bare, "log", "-1", "--format=%an <%ae>", "main").stdout.strip(), "Ed <ed@x.org>")
        pushes = [auth for method, path, auth in self.git.requests if method == "POST" and "receive-pack" in path]
        self.assertEqual(pushes, [self.header(self.TOKEN)])

        # Both sides change the same line: the merge is aborted, the file listed, the project left as it was.
        (self.source / "main.tex").write_text("one\nmine\nthree\nfour\nfive\n", encoding="utf-8")
        commit(self.seed, {"main.tex": "one\ntheirs\nthree\nfour\nfive\n"})
        status, result, _ = self.sync("pull")
        self.assertEqual((status, result["ok"], result["conflicts"]), (200, False, ["main.tex"]))
        self.assertEqual((self.source / "main.tex").read_text(encoding="utf-8"), "one\nmine\nthree\nfour\nfive\n")
        gitdir = host_git.repo_dir(self.project)
        self.assertFalse((gitdir / "MERGE_HEAD").exists())
        self.assertNotIn("<<<<<<<", "".join(p.read_text(errors="ignore") for p in self.source.rglob("*.tex")))
        status, data, _ = self.sync("push")
        self.assertEqual(status, 409, data)  # the repository moved on: pull first
        # The person makes them agree; the next pull merges.
        (self.source / "main.tex").write_text("one\ntheirs\nthree\nfour\nfive\n", encoding="utf-8")
        self.assertEqual(self.sync("pull")[1]["ok"], True)
        (self.source / "ch2.tex").write_text("Edited after the merge\n", encoding="utf-8")
        self.assertEqual(self.sync("push")[0], 200)
        self.assertEqual(self.remote_file("ch2.tex"), "Edited after the merge\n")

        # The host rotates the token: the old one is refused until the person saves the new one.
        new = "ghp_RotatedTokenOfEd678"
        self.git.users["ed"] = new
        (self.source / "ch2.tex").write_text("After rotation\n", encoding="utf-8")
        status, data, _ = self.sync("push")
        self.assertEqual(status, 502, data)
        self.set_token(new)
        status, result, _ = self.sync("push")
        # the refused push already committed the change here; this one delivers it
        self.assertEqual((status, result["ok"], result["message"]), (200, True, "Pushed."), result)
        self.assertEqual(self.remote_file("ch2.tex"), "After rotation\n")
        self.assertEqual(self.git.requests[-1][2], self.header(new))
        for token in (self.TOKEN, new):
            self.assertNotIn(token, str(data))
            self.assertFalse(any(token in p.read_text(errors="ignore") for p in self.source.rglob("*") if p.is_file()))
            self.assertFalse(any(token in p.read_text(errors="ignore") for p in gitdir.rglob("*") if p.is_file()))
        self.assertEqual(self.editor.call("POST", "/api/account/git", {"clear_token": True})[0], 200)
        self.assertEqual(self.sync("pull")[0], 502)  # private again without a token

        status, data, _ = self.sync("disconnect")
        self.assertEqual(status, 200, data)
        self.assertFalse(gitdir.exists())
        self.assertIsNone(self.app.db.one("SELECT 1 FROM git_remotes WHERE project_id = ?", self.pid))
        self.assertEqual(self.actions(), ["git_connected", "git_pull", "git_token_set", "git_pull", "git_pull",
                                          "git_push", "git_pull", "git_push", "git_pull", "git_push", "git_push",
                                          "git_token_set", "git_push", "git_token_removed", "git_pull",
                                          "git_disconnected"])
        details = [r["detail"].split(" ", 1)[1] for r in self.app.db.all(
            "SELECT detail FROM audit WHERE action IN ('git_pull', 'git_push') ORDER BY id")]
        self.assertEqual(details, ["failed: 502", "conflicts: main.tex", "ok", "ok", "conflicts: main.tex",
                                   "failed: 409", "ok", "ok", "failed: 502", "ok", "failed: 502"])
        self.assertTrue(all(r["user_id"] == self.uid for r in self.app.db.all(
            "SELECT user_id FROM audit WHERE action LIKE 'git%'")))

    def test_symlinks_arrive_as_plain_files(self):
        run_git(self.seed, "pull", "-q", "origin", "main", check=False)
        os.symlink("/etc/passwd", self.seed / "link.tex")
        os.symlink("/etc", self.seed / "etc")
        self.pulled_once()
        for name, target in (("link.tex", "/etc/passwd"), ("etc", "/etc")):
            self.assertFalse((self.source / name).is_symlink(), name)
            self.assertEqual((self.source / name).read_text(), target)

    def test_submodules_are_never_fetched(self):
        secret = self.git.repo("secret.git", self.data / "secret")
        commit(self.data / "secret", {"private.tex": "secret\n"})
        oid = run_git(secret, "rev-parse", "main").stdout.strip()
        commit(self.seed, {".gitmodules": f'[submodule "sub"]\n\tpath = sub\n\turl = {self.git.url("secret.git")}\n'
                                          '[submodule "etc"]\n\tpath = etc\n\turl = file:///etc\n'}, push=False)
        for path in ("sub", "etc"):
            run_git(self.seed, "update-index", "--add", "--cacheinfo", f"160000,{oid},{path}")
        run_git(self.seed, "commit", "-q", "-m", "submodules")
        run_git(self.seed, "push", "-q", "origin", "HEAD:refs/heads/main")
        self.pulled_once()
        self.assertFalse(any("secret.git" in path for _, path, _ in self.git.requests))
        self.assertEqual(list(self.source.rglob("private.tex")), [])
        self.assertFalse((self.source / "sub" / ".git").exists())
        commit(self.seed, {"more.tex": "x\n"})
        self.assertTrue(self.sync("pull")[1]["ok"])  # the merge does not recurse either
        self.assertFalse(any("secret.git" in path for _, path, _ in self.git.requests))

    def test_hooks_never_run(self):
        marker = self.data / "hook-ran"
        script = f"#!/bin/sh\ntouch {marker}\n"
        # A tree that would put a hook into a .git folder of the project: git refuses the path.
        hook =subprocess.run(["git", "hash-object", "-w", "--stdin"], input=script, cwd=str(self.bare),
                              capture_output=True, text=True, check=True).stdout.strip()
        main = subprocess.run(["git", "hash-object", "-w", "--stdin"], input="x\n", cwd=str(self.bare),
                              capture_output=True, text=True, check=True).stdout.strip()

        def tree(rows):
            text = "".join(f"{mode} {kind} {oid}\t{name}\n" for mode, kind, oid, name in rows)
            return subprocess.run(["git", "mktree"], input=text, cwd=str(self.bare), capture_output=True, text=True,
                                  check=True).stdout.strip()
        hooks = tree([("100755", "blob", hook, name) for name in ("post-checkout", "post-merge")])
        dotgit = tree([("040000", "tree", hooks, "hooks")])
        root = tree([("040000", "tree", dotgit, ".git"), ("100644", "blob", main, "main.tex")])
        head = run_git(self.bare, "commit-tree", root, "-m", "hook").stdout.strip()
        run_git(self.bare, "update-ref", "refs/heads/main", head)
        (self.source / "main.tex").unlink()
        self.connect()
        status, data, _ = self.sync("pull")
        self.assertEqual(status, 409, data)
        self.assertIn(".git", data["error"])
        self.assertFalse((self.source / ".git").exists())
        self.assertFalse(marker.exists())
        # A hook in the repository's own hooks folder (only the gateway could put one there) does not run either:
        # core.hooksPath points at a folder that does not exist.
        hidden = self.data / "clean"
        self.git.repo(f"{self.repo_name}-2", hidden)
        commit(hidden, {"main.tex": "one\n"})
        self.connect(self.git.url(f"{self.repo_name}-2"))
        host_git.remove(host_git.repo_dir(self.project))
        status, result, _ = self.sync("pull")
        self.assertEqual((status, result["ok"]), (200, True), result)
        planted = host_git.repo_dir(self.project) / "hooks"
        planted.mkdir(exist_ok=True)
        for name in ("pre-commit", "commit-msg", "post-commit", "post-merge", "pre-merge-commit", "pre-push",
                     "reference-transaction", "post-checkout", "pre-auto-gc"):
            (planted / name).write_text(script)
            (planted / name).chmod(0o755)
        commit(hidden, {"other.tex": "two\n"})
        (self.source / "mine.tex").write_text("local\n", encoding="utf-8")
        self.git.users["ed"] = self.TOKEN
        self.set_token(self.TOKEN)
        self.assertEqual(self.sync("pull")[1]["ok"], True)  # commit, merge
        self.assertEqual(self.sync("push")[0], 200)  # push
        self.assertTrue((self.source / "other.tex").is_file())
        self.assertFalse(marker.exists())

    def test_a_pack_larger_than_the_cap_is_stopped_while_it_arrives(self):
        self.app.save_settings({"max_project_mb": 1})  # the repository may grow to 2 MB
        commit(self.seed, {f"data/{i:03}.bin": os.urandom(30_000) for i in range(200)})  # about 6 MB, 200 objects
        (self.source / "main.tex").unlink()
        self.connect(self.git.url(self.repo_name, "slow/"))
        started = time.monotonic()
        status, data, _ = self.sync("pull")
        self.assertEqual(status, 507, data)
        self.assertIn("larger than this server allows", data["error"])
        self.assertLess(time.monotonic() - started, 4.5)  # stopped part-way, not after the whole 6 s download
        self.assertFalse((self.source / "data").exists())

    def test_quota_is_checked_before_files_are_written(self):
        self.app.save_settings({"max_project_mb": 1})
        commit(self.seed, {"big.bin": os.urandom(1_500_000)})  # under the 2 MB repository cap, over the 1 MB quota
        (self.source / "main.tex").unlink()
        self.connect()
        status, data, _ = self.sync("pull")
        self.assertEqual(status, 507, data)
        self.assertIn("quota", data["error"])
        self.assertFalse((self.source / "big.bin").exists())

    def test_redirects_are_not_followed(self):
        commit(self.seed, {"main.tex": "x\n"})
        for prefix in ("redirect-http/", "redirect-host/"):
            with self.subTest(prefix=prefix):
                self.git.requests.clear()
                self.connect(self.git.url(self.repo_name, prefix))
                status, data, _ = self.sync("pull")
                self.assertEqual(status, 502, data)
                self.assertTrue(self.git.requests, "git never asked")
                self.assertFalse([p for _, p, _ in self.git.requests if p.startswith("/landed/")])
        self.assertEqual(self.git.landing.hits, [])
        # The control: plain git with redirects allowed does follow the same answer, so the refusal is the config's.
        for prefix in ("redirect-http/", "redirect-host/"):
            config = [x for k, v in self.git.client_config() + [("http.followRedirects", "true")]
                      for x in ("-c", f"{k}={v}")]
            run_git(self.data, *config, "ls-remote", self.git.url(self.repo_name, prefix), check=False)
        landed = [p for _, p, _ in self.git.requests if p.startswith("/landed/")] + self.git.landing.hits
        self.assertTrue(landed, "the control did not follow the redirect")

    def test_a_remote_that_never_answers_times_out(self):
        self.git.users["ed"] = self.TOKEN
        self.set_token(self.TOKEN)
        self.connect(self.git.url(self.repo_name, "hang/"))
        seen = {}

        def watch():  # while git waits: the token is in no process's command line
            deadline = time.monotonic() + 3
            while time.monotonic() < deadline:
                for proc in Path("/proc").iterdir():
                    try:
                        line = (proc / "cmdline").read_bytes().replace(b"\0", b" ").decode(errors="replace")
                    except OSError:
                        continue
                    if "git" in line and "hang/" in line:
                        seen.setdefault("git", line)
                        seen["token"] = seen.get("token") or self.TOKEN in line or "Authorization" in line
                time.sleep(0.1)
        watcher = threading.Thread(target=watch)
        watcher.start()
        with mock.patch.object(host_git, "NET_TIMEOUT", 2.0):
            started = time.monotonic()
            status, data, _ = self.sync("pull")
        watcher.join()
        self.assertEqual(status, 504, data)
        self.assertLess(time.monotonic() - started, 15)
        self.assertIn("git", seen)
        self.assertFalse(seen.get("token"))
        self.assertIn(("GET", f"/hang/{self.repo_name}/info/refs", self.header(self.TOKEN)), self.git.requests)


@unittest.skipUnless(HAVE_TOOLS, "needs git and openssl (Linux)")
class LocalPanelLive(SharedState, ServerCase):
    """serve.py's /api/git with the owner's repository cloned from the https server (credentials in its config)."""

    TOKEN = "local-token-123456"

    @classmethod
    def setUpClass(cls):
        cls.tmp_server = tempfile.TemporaryDirectory()
        cls.git = GitServer(Path(cls.tmp_server.name).resolve())

    @classmethod
    def tearDownClass(cls):
        cls.git.close()
        cls.tmp_server.cleanup()

    def setUp(self):
        SharedState.setUp(self)
        ServerCase.setUp(self)
        self.git.users.clear()
        self.git.users["ada"] = self.TOKEN
        self.git.requests.clear()
        name = self.id().rsplit(".", 1)[-1] + ".git"
        self.seed = self.base / "seed"
        self.bare = self.git.repo(name, self.seed)
        commit(self.seed, {"main.tex": "one\ntwo\nthree\n"})
        # The owner's git config (the panel uses it as is): the test CA, the pin and the credentials.
        config = self.base / "gitconfig"
        auth = base64.b64encode(f"ada:{self.TOKEN}".encode()).decode()
        lines = [f"[{k.split('.')[0]}]\n\t{k.split('.')[1]} = {v}\n" for k, v in self.git.client_config()]
        config.write_text("".join(lines) + f"[http]\n\textraHeader = Authorization: Basic {auth}\n"
                          "[user]\n\tname = Ada\n\temail = ada@example.org\n[init]\n\tdefaultBranch = main\n")
        mock.patch.dict(os.environ, local_env({"GIT_CONFIG_GLOBAL": str(config), "GIT_CONFIG_NOSYSTEM": "1",
                                               "NO_PROXY": "*", "no_proxy": "*"}), clear=True).start()
        shutil.rmtree(self.root)
        subprocess.run(["git", "clone", "-q", self.git.url(name), str(self.root)], check=True, capture_output=True)
        mock.patch.dict(serve.DOCS, {"demo": self.root / "main.tex"}, clear=True).start()

    def call(self, method, path, body=None):
        return self.request(method, f"{path}{'&' if '?' in path else '?'}doc=demo", body)

    def op(self, **body):
        status, data = self.call("POST", "/api/git", body)
        self.assertEqual(status, 200, data)
        return data

    def test_every_panel_operation_against_the_https_remote(self):
        (self.root / "main.tex").write_text("one\nTWO\nthree\n", encoding="utf-8")
        (self.root / "new.tex").write_text("new\n", encoding="utf-8")
        status, info = self.call("GET", "/api/git")
        self.assertEqual((status, info["upstream"], sorted(f["path"] for f in info["files"])),
                         (200, "origin/main", ["main.tex", "new.tex"]))
        self.assertIn("+TWO", self.call("GET", "/api/git/diff?path=main.tex")[1]["diff"])
        self.assertEqual(self.call("GET", "/api/git/diff?path=new.tex")[1]["text"], "new\n")
        self.op(op="stage", paths=["main.tex", "new.tex"])
        self.assertIn("+TWO", self.call("GET", "/api/git/diff?path=main.tex&staged=1")[1]["diff"])
        self.op(op="unstage", paths=["new.tex"])
        self.assertEqual({f["path"]: f["index"] for f in self.call("GET", "/api/git")[1]["files"]},
                         {"main.tex": "M", "new.tex": "?"})
        self.op(op="stage", paths=["new.tex"])
        self.op(op="commit", message="Edit over https")
        self.assertEqual(self.call("GET", "/api/git")[1]["ahead"], 1)
        self.op(op="push")
        self.assertEqual(run_git(self.bare, "show", "main:main.tex").stdout, "one\nTWO\nthree\n")
        pushed = [a for m, p, a in self.git.requests if m == "POST" and "receive-pack" in p]
        self.assertEqual(len(pushed), 1)
        self.assertEqual(self.git.user_for(pushed[0]), "ada")

        commit(self.seed, {"main.tex": "one\nTWO\nthree\nfour\n"})
        self.assertEqual(self.op(op="pull")["conflicts"], [])
        self.assertEqual((self.root / "main.tex").read_text(encoding="utf-8"), "one\nTWO\nthree\nfour\n")

        # A conflicting pull: listed, the merge left for the person, then aborted.
        commit(self.seed, {"main.tex": "one\ntheirs\nthree\nfour\n"})
        (self.root / "main.tex").write_text("one\nmine\nthree\nfour\n", encoding="utf-8")
        self.op(op="stage", paths=["main.tex"])
        self.op(op="commit", message="mine")
        result = self.op(op="pull")
        self.assertEqual((result["ok"], result["conflicts"]), (False, ["main.tex"]))
        info = self.call("GET", "/api/git")[1]
        self.assertTrue(info["merging"])
        self.assertEqual(info["files"][0].get("conflict"), True)
        self.op(op="abort")
        self.assertFalse(self.call("GET", "/api/git")[1]["merging"])
        self.assertEqual((self.root / "main.tex").read_text(encoding="utf-8"), "one\nmine\nthree\nfour\n")
        status, data = self.call("POST", "/api/git", {"op": "push"})
        self.assertEqual(status, 409, data)  # rejected: the remote has a commit this branch lacks

        # Branches: create and push one (sets its upstream), make another remotely and switch to it.
        self.op(op="create", name="draft")
        self.op(op="push")
        self.assertEqual(run_git(self.bare, "show", "draft:main.tex").stdout, "one\nmine\nthree\nfour\n")
        run_git(self.seed, "push", "-q", "origin", "main:refs/heads/remote-only")
        self.op(op="pull")  # fetches the new branch (and merges draft's upstream: nothing new)
        branches = self.call("GET", "/api/git/branches")[1]
        self.assertIn("origin/remote-only", branches["remote"])
        self.op(op="switch", name="origin/remote-only")
        self.assertEqual(self.call("GET", "/api/git")[1]["branch"], "remote-only")
        self.op(op="switch", name="draft")
        log = self.call("GET", "/api/git/log")[1]["commits"]
        self.assertEqual([c["subject"] for c in log[:2]], ["mine", "upstream"])
        self.assertEqual(log[0]["author"], "Ada")
        self.assertNotIn(self.TOKEN, str(self.call("GET", "/api/git")[1]))

    def test_a_wrong_token_is_refused_with_gits_message(self):
        self.git.users["ada"] = "rotated-token-999999"
        (self.root / "x.tex").write_text("x\n", encoding="utf-8")
        self.op(op="stage", paths=["x.tex"])
        self.op(op="commit", message="x")
        status, data = self.call("POST", "/api/git", {"op": "push"})
        self.assertEqual(status, 409, data)
        self.assertIn("terminal prompts disabled", data["error"])  # refused (401) without waiting for a prompt
        self.assertIsNotNone(self.git.requests[-1][2])  # a header was sent: the wrong one
        self.assertIsNone(self.git.user_for(self.git.requests[-1][2]))


if __name__ == "__main__":
    unittest.main()
