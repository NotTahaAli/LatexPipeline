"""host.py: accounts, sessions, 2FA, invites, tenants, projects, zip handling, the proxy to workers. No network."""

from __future__ import annotations

import base64
import http.client
import io
import json
import os
import shutil
import socket
import stat
import sys
import tempfile
import threading
import time
import unittest
import zipfile
from http.server import ThreadingHTTPServer
from pathlib import Path
from unittest import mock

import _support  # noqa: F401 - puts scripts/ on sys.path
import ai
import host
import serve


def make_zip(entries: dict, symlink: str | None = None) -> bytes:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as archive:
        for name, text in entries.items():
            archive.writestr(name, text)
        if symlink:
            info = zipfile.ZipInfo(symlink)
            info.external_attr = (stat.S_IFLNK | 0o777) << 16
            archive.writestr(info, "/etc/passwd")
    return buf.getvalue()


class Pure(unittest.TestCase):
    def test_password_hash_roundtrip(self):
        with mock.patch.dict(host.SCRYPT, {"n": 2 ** 10}):
            stored = host.hash_password("correct horse battery")
        self.assertTrue(stored.startswith("scrypt$1024$8$1$"))
        self.assertTrue(host.verify_password("correct horse battery", stored))
        self.assertFalse(host.verify_password("correct horse batterz", stored))
        self.assertFalse(host.verify_password("x", None))
        self.assertFalse(host.verify_password(None, stored))
        self.assertFalse(host.verify_password("x", "garbage"))
        self.assertNotEqual(stored, host.hash_password("correct horse battery"))  # salted

    def test_default_scrypt_cost(self):
        self.assertEqual(host.SCRYPT, {"n": 2 ** 15, "r": 8, "p": 1})

    def test_totp_matches_rfc6238_vectors_and_each_code_works_once(self):
        secret = base64.b32encode(b"12345678901234567890").decode()
        self.assertEqual(host.totp_code(secret, 59 // 30), "287082")
        self.assertEqual(host.totp_code(secret, 1111111109 // 30), "081804")
        step = host.totp_match(secret, "081804", 0, now=1111111109)
        self.assertEqual(step, 1111111109 // 30)
        self.assertIsNone(host.totp_match(secret, "081804", step, now=1111111109))  # replay
        self.assertEqual(host.totp_match(secret, "081804", 0, now=1111111109 + 30), step)  # one step late is fine
        self.assertIsNone(host.totp_match(secret, "081804", 0, now=1111111109 + 90))
        self.assertIsNone(host.totp_match(secret, "08180", 0, now=1111111109))

    def test_throttle_backs_off_exponentially(self):
        throttle = host.Throttle(3, window=900)
        for t in range(3):
            self.assertEqual(throttle.wait("k", now=t), 0)
            throttle.fail("k", now=t)
        self.assertAlmostEqual(throttle.wait("k", now=2), 30)
        throttle.fail("k", now=40)
        self.assertAlmostEqual(throttle.wait("k", now=40), 60)
        self.assertEqual(throttle.wait("k", now=2000), 0)  # the window passed
        self.assertEqual(throttle.wait("other", now=40), 0)

    def test_ip_keys_group_ipv6_by_64(self):
        self.assertEqual(host.ip_key("2001:db8:1:2:aaaa::1"), host.ip_key("2001:db8:1:2:bbbb::9"))
        self.assertNotEqual(host.ip_key("2001:db8:1:2::1"), host.ip_key("2001:db8:1:3::1"))
        self.assertEqual(host.ip_key("203.0.113.7"), "203.0.113.7")
        self.assertEqual(host.ip_key("::ffff:203.0.113.7"), "203.0.113.7")
        self.assertEqual(host.ip_key("not an ip"), "not an ip")
        throttle = host.Throttle(2, 3600)
        throttle.fail("k", now=0)
        self.assertFalse(throttle.full("k", now=1))
        throttle.fail("k", now=1)
        self.assertTrue(throttle.full("k", now=100))
        self.assertFalse(throttle.full("k", now=3602))

    def test_names_emails_and_slugs(self):
        self.assertEqual(host.norm_email("  Ada@Example.ORG "), "ada@example.org")
        for bad in ("", "ada", "a@b", "a b@c.d", "x" * 250 + "@a.bc"):
            with self.subTest(bad=bad), self.assertRaises(host.HttpError):
                host.norm_email(bad)
        self.assertEqual(host.slug_for("My Thesis (v2)"), "My-Thesis-v2")
        self.assertEqual(host.slug_for("../.."), "document")
        self.assertEqual(host.slug_for(".hidden"), "hidden")
        with self.assertRaises(host.HttpError):
            host.clean_name("bad\x00name")


class ZipSafety(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.base = Path(self.tmp.name)

    def unpack(self, data, limit=10 ** 6):
        target = self.base / "t"
        host.unpack_zip(data, target, limit)
        return sorted(p.relative_to(target).as_posix() for p in target.rglob("*") if p.is_file())

    def test_plain_and_single_folder_archives(self):
        self.assertEqual(self.unpack(make_zip({"main.tex": "x", "ch/a.tex": "y"})), ["ch/a.tex", "main.tex"])
        shutil.rmtree(self.base / "t")
        wrapped = make_zip({"Thesis/main.tex": "x", "Thesis/fig/a.png": "y", "__MACOSX/Thesis/._main.tex": "z"})
        self.assertEqual(self.unpack(wrapped), ["fig/a.png", "main.tex"])

    def test_unsafe_archives_are_refused_and_leave_nothing(self):
        cases = {
            "slip": make_zip({"main.tex": "x", "../evil.tex": "y"}),
            "nested slip": make_zip({"main.tex": "x", "a/../../evil.tex": "y"}),
            "absolute": make_zip({"main.tex": "x", "/etc/cron.d/x": "y"}),
            "backslash": make_zip({"main.tex": "x", "..\\evil.tex": "y"}),
            "drive": make_zip({"main.tex": "x", "C:/evil.tex": "y"}),
            "symlink": make_zip({"main.tex": "x"}, symlink="link.tex"),
            "no main.tex": make_zip({"thesis.tex": "x"}),
            "too big": make_zip({"main.tex": "x" * 2000}),
            "twice": make_zip({"main.tex": "x", "A.tex": "y", "a.tex": "z"}),
            "not a zip": b"PK\x03\x04garbage",
        }
        for name, data in cases.items():
            with self.subTest(name=name), self.assertRaises(host.HttpError):
                host.unpack_zip(data, self.base / "t", 1000)
            self.assertFalse((self.base / "t").exists(), name)
        self.assertFalse((self.base / "evil.tex").exists())

    def test_lying_sizes_are_caught_while_unpacking(self):
        data = bytearray(make_zip({"main.tex": "x" * 5000}))
        with zipfile.ZipFile(io.BytesIO(bytes(data))) as archive:
            info = archive.getinfo("main.tex")
        # Patch the central directory's uncompressed size down to 10 bytes; the real data stays 5000.
        central = bytes(data).rfind(b"PK\x01\x02")
        data[central + 24:central + 28] = (10).to_bytes(4, "little")
        self.assertEqual(info.file_size, 5000)
        with self.assertRaises(host.HttpError):
            host.unpack_zip(bytes(data), self.base / "t", 1000)
        self.assertFalse((self.base / "t").exists())

    def test_build_configuration_is_left_out(self):
        self.assertEqual(host.RC_NAMES, serve.RC_NAMES)
        target = self.base / "t"
        skipped = host.unpack_zip(make_zip({"P/main.tex": "x", "P/build.toml": "shell_escape = true",
                                            "P/.latexmkrc": "$x", "P/sub/LatexMkRc": "$y", "P/a.tex": "y"}),
                                  target, 10 ** 6)
        self.assertEqual(sorted(skipped), ["P/.latexmkrc", "P/build.toml", "P/sub/LatexMkRc"])
        self.assertEqual(sorted(p.relative_to(target).as_posix() for p in target.rglob("*") if p.is_file()),
                         ["a.tex", "main.tex"])

    def test_download_skips_symlinks_out(self):
        root = self.base / "doc"
        (root / "sub").mkdir(parents=True)
        (root / "main.tex").write_text("x")
        (root / "sub" / "a.tex").write_text("y")
        (self.base / "secret.txt").write_text("s")
        try:
            os.symlink(self.base / "secret.txt", root / "leak.txt")
        except (OSError, NotImplementedError):
            pass
        buf = io.BytesIO()
        host.zip_folder(root, buf)
        names = zipfile.ZipFile(io.BytesIO(buf.getvalue())).namelist()
        self.assertEqual(sorted(names), ["doc/main.tex", "doc/sub/a.tex"])


class Client:
    """A browser, roughly: a cookie jar (paths ignored), the CSRF token from the last JSON that had one."""

    def __init__(self, case: HostCase) -> None:
        self.case, self.jar, self.csrf = case, {}, None

    @property
    def cookie(self) -> str | None:
        return "; ".join(f"{k}={v}" for k, v in self.jar.items()) or None

    @cookie.setter
    def cookie(self, value: str | None) -> None:
        self.jar = dict(pair.split("=", 1) for pair in (value or "").split("; ") if "=" in pair)

    def call(self, method, path, body=None, raw=None, headers=None, origin=True, ctype="application/json"):
        conn = http.client.HTTPConnection("127.0.0.1", self.case.port, timeout=15)
        hdrs = {"Host": self.case.netloc}
        if origin and method != "GET":
            hdrs["Origin"] = self.case.origin if origin is True else origin
        if self.cookie:
            hdrs["Cookie"] = self.cookie
        if self.csrf and method != "GET":
            hdrs["X-CSRF-Token"] = self.csrf
        if body is not None:
            raw = json.dumps(body).encode()
        if raw is not None:
            hdrs["Content-Type"] = ctype
        hdrs.update(headers or {})
        try:
            conn.request(method, path, raw, hdrs)
        except (BrokenPipeError, ConnectionResetError):
            pass  # The server answered (e.g. 413 on Content-Length) and closed before reading the whole body.
        try:
            res = conn.getresponse()
            data = res.read()
        except (http.client.RemoteDisconnected, ConnectionResetError):
            # Closed before even the reply arrived: an early reject. Reported as 413 for the size checks.
            conn.close()
            return 413, {"error": "connection closed early"}, None
        conn.close()
        for cookie in res.headers.get_all("Set-Cookie") or []:
            name, value = cookie.split(";")[0].split("=", 1)
            if "Max-Age=0" in cookie:
                self.jar.pop(name, None)
            else:
                self.jar[name] = value
        try:
            data = json.loads(data)
        except ValueError:
            pass
        if isinstance(data, dict) and data.get("csrf"):
            self.csrf = data["csrf"]
        return res.status, data, res

    def login(self, email, password="password-123", code=None):
        status, data, _ = self.call("POST", "/api/login", {"email": email, "password": password})
        if status == 200 and data["stage"] == "mfa" and code:
            status, data, _ = self.call("POST", "/api/login/code", {"code": code})
        return status, data


class HostCase(unittest.TestCase):
    """A gateway on a temp data dir (fast scrypt), plus helpers to make users and workspaces."""

    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.data = Path(tmp.name).resolve()
        mock.patch.dict(host.SCRYPT, {"n": 2 ** 10}).start()
        self.addCleanup(mock.patch.stopall)
        self.server = host.Server(("127.0.0.1", 0), host.Handler, self.extra_config().get("max_connections", 256))
        self.port = self.server.server_address[1]
        self.netloc = f"127.0.0.1:{self.port}"
        self.origin = f"http://{self.netloc}"
        config = {**host.CONFIG_DEFAULTS, "public_url": self.origin, **self.extra_config()}
        host.APP = self.app = host.App(self.data, config, sandbox=False)
        threading.Thread(target=self.server.serve_forever, daemon=True).start()
        self.addCleanup(self.app.db.conn.close)
        self.addCleanup(self.app.workers.stop_all)
        self.addCleanup(self.server.server_close)
        self.addCleanup(self.server.shutdown)

    def extra_config(self) -> dict:
        return {}

    def user(self, email, role=None, tenant=None, password="password-123", **flags) -> int:
        uid = host.create_user(self.app, email, email.split("@")[0].title(), password, **flags)
        if role:
            self.app.db.run("INSERT INTO members VALUES (?, ?, ?)", tenant, uid, role)
        return uid

    def client(self, email=None, **kw) -> Client:
        client = Client(self)
        if email:
            status, _ = client.login(email, **kw)
            self.assertEqual(status, 200)
        return client


class Basics(HostCase):
    def test_migrations_are_versioned_and_run_once(self):
        self.assertEqual(self.app.db.version(), len(host.MIGRATIONS))
        again = host.DB(self.data / "host.db")
        self.assertEqual(again.version(), len(host.MIGRATIONS))
        again.conn.close()

    def test_security_headers(self):
        status, _, res = self.client().call("GET", "/")
        self.assertEqual(status, 200)
        csp = res.getheader("Content-Security-Policy")
        self.assertIn("frame-ancestors 'none'", csp)
        self.assertIn("script-src 'self';", csp)
        self.assertNotIn("unsafe", csp)
        self.assertEqual(res.getheader("X-Content-Type-Options"), "nosniff")
        self.assertEqual(res.getheader("Referrer-Policy"), "no-referrer")
        self.assertIsNone(res.getheader("Strict-Transport-Security"))  # plain http

    def test_unknown_host_and_static_traversal(self):
        client = self.client()
        self.assertEqual(client.call("GET", "/", headers={"Host": "evil.example"})[0], 421)
        for path in ("/ui/..%2fhost.db", "/ui/.hidden", "/ui/nope.js", "/host.db"):
            with self.subTest(path=path):
                self.assertEqual(client.call("GET", path)[0], 404)

    def test_fonts_from_an_allow_list_without_a_session(self):
        fonts = self.data / "fonts"
        fonts.mkdir()
        (fonts / "newsreader.woff2").write_bytes(b"wOF2font")
        (fonts / "other.woff2").write_bytes(b"x")
        mock.patch.object(host, "FONTS_DIR", fonts).start()
        self.addCleanup(mock.patch.stopall)
        client = self.client()
        status, body, res = client.call("GET", "/fonts/newsreader.woff2")
        self.assertEqual((status, body, res.getheader("Content-Type")), (200, b"wOF2font", "font/woff2"))
        self.assertIn("font-src 'self'", res.getheader("Content-Security-Policy"))
        for path in ("/fonts/source-sans-3.woff2", "/fonts/other.woff2", "/fonts/..%2fhost.db", "/fonts/../host.py",
                     "/fonts/%2e%2e%2fhost.py", "/fonts/"):
            with self.subTest(path=path):
                self.assertEqual(client.call("GET", path)[0], 404)


class Connections(HostCase):
    def extra_config(self):
        return {"max_connections": 8}

    def test_connections_beyond_the_limit_get_503(self):
        idle = [socket.create_connection(("127.0.0.1", self.port)) for _ in range(8)]
        for sock in idle:
            self.addCleanup(sock.close)
        time.sleep(0.2)
        status, _, res = self.client().call("GET", "/")
        self.assertEqual((status, res.getheader("Retry-After")), (503, "5"))
        for sock in idle:
            sock.close()
        deadline = time.monotonic() + 10
        while time.monotonic() < deadline:
            try:
                if self.client().call("GET", "/")[0] == 200:
                    break
            except OSError:
                pass
            time.sleep(0.1)
        else:
            self.fail("slots were not given back")


class Https(HostCase):
    def extra_config(self):
        return {"public_url": "https://latex.example.org"}

    def setUp(self):
        super().setUp()
        self.netloc = "latex.example.org"
        self.origin = "https://latex.example.org"

    def test_hsts_and_host_cookie(self):
        self.user("ada@example.org")
        client = self.client()
        _, _, res = client.call("GET", "/")
        self.assertEqual(res.getheader("Strict-Transport-Security"), "max-age=63072000")
        _, _, res = client.call("POST", "/api/login", {"email": "ada@example.org", "password": "password-123"})
        cookie = res.getheader("Set-Cookie")
        self.assertTrue(cookie.startswith("__Host-lp_session="))
        for flag in ("Secure", "HttpOnly", "SameSite=Lax", "Path=/"):
            self.assertIn(flag, cookie)


class Login(HostCase):
    def setUp(self):
        super().setUp()
        self.uid = self.user("ada@example.org")

    def test_login_logout_and_me(self):
        client = self.client()
        self.assertIsNone(client.call("GET", "/api/me")[1]["user"])
        status, data, res = client.call("POST", "/api/login", {"email": "ADA@example.org", "password": "password-123"})
        self.assertEqual((status, data["stage"]), (200, "full"))
        self.assertIn("HttpOnly", res.getheader("Set-Cookie"))
        self.assertEqual(client.call("GET", "/api/me")[1]["user"]["email"], "ada@example.org")
        self.assertEqual(client.call("POST", "/api/logout")[0], 200)
        self.assertEqual(list(client.jar), ["lp_device"])  # the session is gone; the browser stays known
        self.assertIsNone(client.call("GET", "/api/me")[1]["user"])

    def test_old_session_cookie_dies_on_logout(self):
        client = self.client("ada@example.org")
        stolen = client.cookie
        client.call("POST", "/api/logout")
        thief = Client(self)
        thief.cookie = stolen
        self.assertIsNone(thief.call("GET", "/api/me")[1]["user"])

    def test_wrong_password_unknown_email_and_rate_limit(self):
        client = self.client()
        wrong = client.call("POST", "/api/login", {"email": "ada@example.org", "password": "nope-nope-nope"})
        unknown = client.call("POST", "/api/login", {"email": "bob@example.org", "password": "nope-nope-nope"})
        self.assertEqual((wrong[0], unknown[0]), (401, 401))
        self.assertEqual(wrong[1]["error"], unknown[1]["error"])  # no account enumeration
        for _ in range(4):
            client.call("POST", "/api/login", {"email": "ada@example.org", "password": "nope-nope-nope"})
        status, data, res = client.call("POST", "/api/login", {"email": "ada@example.org", "password": "password-123"})
        self.assertEqual(status, 429)
        self.assertTrue(res.getheader("Retry-After"))
        actions = [r["action"] for r in self.app.db.all("SELECT action FROM audit")]
        self.assertEqual(actions.count("login_failed"), 6)

    def test_a_known_browser_is_not_locked_out_by_someone_else_guessing(self):
        mine = self.client("ada@example.org")
        self.assertIn("lp_device", mine.jar)
        mine.call("POST", "/api/logout")
        attacker = self.client()
        for _ in range(6):
            attacker.call("POST", "/api/login", {"email": "ada@example.org", "password": "nope-nope-nope"})
        self.assertEqual(attacker.login("ada@example.org")[0], 429)  # unknown browsers back off
        self.assertEqual(Client(self).login("ada@example.org")[0], 429)
        self.assertEqual(mine.login("ada@example.org")[0], 200)  # the owner's browser still gets in
        forged = Client(self)
        forged.jar["lp_device"] = mine.jar["lp_device"][:-2] + "xx"
        self.assertEqual(forged.login("ada@example.org")[0], 429)
        other = self.user("bob@example.org")
        self.assertTrue(other)
        stolen = Client(self)  # a device cookie is bound to its account
        stolen.jar["lp_device"] = mine.jar["lp_device"]
        for _ in range(5):
            stolen.call("POST", "/api/login", {"email": "bob@example.org", "password": "nope-nope-nope"})
        self.assertEqual(stolen.login("bob@example.org")[0], 429)
        for _ in range(5):  # a known browser with wrong passwords backs off on its own
            mine.call("POST", "/api/login", {"email": "ada@example.org", "password": "nope-nope-nope"})
        self.assertEqual(mine.login("ada@example.org")[0], 429)

    def test_csrf_needs_same_origin_and_token(self):
        client = self.client("ada@example.org")
        body = {"name": "Ada L"}
        self.assertEqual(client.call("POST", "/api/account", body, origin=False)[0], 403)
        self.assertEqual(client.call("POST", "/api/account", body, origin="https://evil.example")[0], 403)
        token, client.csrf = client.csrf, None
        self.assertEqual(client.call("POST", "/api/account", body)[0], 403)
        client.csrf = "x" * len(token)
        self.assertEqual(client.call("POST", "/api/account", body)[0], 403)
        client.csrf = token
        self.assertEqual(client.call("POST", "/api/account", body)[0], 200)
        # Anonymous logins are cross-site protected by Origin alone.
        self.assertEqual(Client(self).call("POST", "/api/login", {}, origin="https://evil.example")[0], 403)

    def test_session_expiry_and_idle_timeout(self):
        client = self.client("ada@example.org")
        self.app.db.run("UPDATE sessions SET expires = ?", time.time() - 1)
        self.assertIsNone(client.call("GET", "/api/me")[1]["user"])
        client = self.client("ada@example.org")
        self.app.db.run("UPDATE sessions SET seen = ?", time.time() - 13 * 3600)
        self.assertEqual(client.call("GET", "/api/account")[0], 401)
        self.assertEqual(self.app.db.one("SELECT COUNT(*) AS n FROM sessions")["n"], 0)

    def test_password_change_signs_out_other_sessions(self):
        first, second = self.client("ada@example.org"), self.client("ada@example.org")
        self.assertEqual(first.call("POST", "/api/account/password", {"current": "wrong-wrong", "password":
                                                                      "new-password-456"})[0], 403)
        short = {"current": "password-123", "password": "short"}
        self.assertEqual(first.call("POST", "/api/account/password", short)[0], 400)
        self.assertEqual(first.call("POST", "/api/account/password", {"current": "password-123", "password":
                                                                      "new-password-456"})[0], 200)
        self.assertEqual(first.call("GET", "/api/account")[0], 200)
        self.assertEqual(second.call("GET", "/api/account")[0], 401)
        self.assertEqual(self.client().login("ada@example.org", "new-password-456")[0], 200)

    def test_disabled_user_loses_sessions(self):
        admin = self.user("root@example.org", site_admin=True)
        ada = self.client("ada@example.org")
        root = self.client("root@example.org")
        self.assertEqual(root.call("POST", f"/api/admin/users/{self.uid}", {"disabled": True})[0], 200)
        self.assertEqual(ada.call("GET", "/api/account")[0], 401)
        self.assertEqual(self.client().login("ada@example.org")[0], 403)
        self.assertEqual(root.call("POST", f"/api/admin/users/{admin}", {"disabled": True})[0], 409)


class TwoFactor(HostCase):
    def setUp(self):
        super().setUp()
        self.uid = self.user("ada@example.org")
        self.ada = self.client("ada@example.org")

    def enable(self):
        _, data, _ = self.ada.call("POST", "/api/account/totp/start")
        self.assertIn("otpauth://totp/", data["uri"])
        secret = data["secret"]
        self.assertEqual(self.ada.call("POST", "/api/account/totp/enable", {"code": "000000"})[0], 400)
        code = host.totp_code(secret, int(time.time() // 30))
        status, data, _ = self.ada.call("POST", "/api/account/totp/enable", {"code": code})
        self.assertEqual(status, 200)
        self.assertEqual(len(data["codes"]), 10)
        return secret, data["codes"]

    def test_parallel_use_of_one_code_succeeds_once(self):
        secret, codes = self.enable()
        user = self.app.db.one("SELECT * FROM users WHERE id = ?", self.uid)  # both requests read the same row
        step = int(time.time() // 30) + 1  # a step not used by enable()
        code = host.totp_code(secret, step)
        def race(attempt):
            barrier, results = threading.Barrier(4), []

            def use():
                barrier.wait()
                results.append(host.check_second_factor(user, attempt))

            threads = [threading.Thread(target=use) for _ in range(4)]
            for thread in threads:
                thread.start()
            for thread in threads:
                thread.join(10)
            return sorted(results)

        with mock.patch.object(host.time, "time", return_value=step * 30.0):
            self.assertEqual(race(code), [False, False, False, True])
            self.assertEqual(race(host.norm_code(codes[0])), [False, False, False, True])

    def test_login_needs_the_code_after_the_password(self):
        secret, _ = self.enable()
        client = Client(self)
        status, data = client.login("ada@example.org")
        self.assertEqual((status, data["stage"]), (200, "mfa"))
        self.assertIsNone(client.call("GET", "/api/me")[1]["user"])
        self.assertEqual(client.call("GET", "/api/account")[0], 401)  # half signed in is not signed in
        self.assertEqual(client.call("POST", "/api/login/code", {"code": "123456"})[0], 401)
        # The code from enabling was used: the next one is needed (one step later is accepted).
        code = host.totp_code(secret, int(time.time() // 30) + 1)
        self.assertEqual(client.call("POST", "/api/login/code", {"code": code})[0], 200)
        self.assertEqual(client.call("GET", "/api/me")[1]["user"]["totp"], True)
        again = Client(self)
        again.login("ada@example.org")
        self.assertEqual(again.call("POST", "/api/login/code", {"code": code})[0], 401)  # replay

    def test_recovery_codes_work_once_and_wrong_codes_are_throttled(self):
        _, codes = self.enable()
        client = Client(self)
        client.login("ada@example.org")
        self.assertEqual(client.call("POST", "/api/login/code", {"code": codes[0].upper()})[0], 200)
        client = Client(self)
        client.login("ada@example.org")
        self.assertEqual(client.call("POST", "/api/login/code", {"code": codes[0]})[0], 401)
        for _ in range(5):
            client.call("POST", "/api/login/code", {"code": "000000"})
        self.assertEqual(client.call("POST", "/api/login/code", {"code": codes[1]})[0], 429)

    def test_disable_needs_password_and_code(self):
        secret, codes = self.enable()
        self.assertEqual(self.ada.call("POST", "/api/account/totp/disable", {"current": "password-123",
                                                                             "code": "000000"})[0], 403)
        self.assertEqual(self.ada.call("POST", "/api/account/totp/disable", {"current": "nope-nope-1",
                                                                             "code": codes[2]})[0], 403)
        self.assertEqual(self.ada.call("POST", "/api/account/totp/disable", {"current": "password-123",
                                                                             "code": codes[2]})[0], 200)
        self.assertEqual(Client(self).login("ada@example.org")[1]["stage"], "full")


class Workspaces(HostCase):
    def setUp(self):
        super().setUp()
        self.a = host.create_tenant(self.app, "Team A")
        self.b = host.create_tenant(self.app, "Team B")
        self.user("admin@a.org", "admin", self.a)
        self.user("editor@a.org", "editor", self.a)
        self.user("viewer@a.org", "viewer", self.a)
        self.user("admin@b.org", "admin", self.b)

    def make_project(self, client, tenant, name="Thesis", template="report"):
        status, data, _ = client.call("POST", f"/api/tenants/{tenant}/projects", {"name": name, "template": template})
        self.assertEqual(status, 200, data)
        return data["id"]

    def test_roles_inside_a_tenant(self):
        editor, viewer = self.client("editor@a.org"), self.client("viewer@a.org")
        pid = self.make_project(editor, self.a)
        project = self.app.db.one("SELECT * FROM projects WHERE id = ?", pid)
        source = self.app.project_source(project)
        self.assertTrue((source / "chapters" / "introduction.tex").is_file())
        self.assertIn("\\title{Thesis}", (source / "main.tex").read_text())
        self.assertEqual(viewer.call("POST", f"/api/tenants/{self.a}/projects", {"name": "X"})[0], 403)
        self.assertEqual(viewer.call("DELETE", f"/api/projects/{pid}")[0], 403)
        self.assertNotIn("members", viewer.call("GET", f"/api/tenants/{self.a}")[1])
        self.assertEqual(editor.call("POST", f"/api/tenants/{self.a}/invites", {"role": "viewer"})[0], 403)
        status, body, res = viewer.call("GET", f"/api/projects/{pid}/zip")
        self.assertEqual((status, res.getheader("Content-Type")), (200, "application/zip"))
        self.assertIn("Thesis/main.tex", zipfile.ZipFile(io.BytesIO(body)).namelist())
        self.assertEqual(editor.call("DELETE", f"/api/projects/{pid}")[0], 200)
        self.assertFalse(source.exists())

    def test_tenant_isolation_is_404(self):
        pid = self.make_project(self.client("editor@a.org"), self.a)
        other = self.client("admin@b.org")
        for method, path in [("GET", f"/api/tenants/{self.a}"), ("GET", f"/api/projects/{pid}/zip"),
                             ("DELETE", f"/api/projects/{pid}"), ("GET", f"/p/{pid}/"), ("GET", f"/p/{pid}/api/health"),
                             ("POST", f"/api/tenants/{self.a}/projects"), ("POST", f"/api/tenants/{self.a}/invites"),
                             ("GET", "/p/0000000000000000/"), ("GET", "/p/../../etc/passwd")]:
            with self.subTest(path=path):
                self.assertEqual(other.call(method, path, {} if method == "POST" else None)[0], 404)
        self.assertEqual(other.call("GET", "/api/admin")[0], 404)
        anonymous = Client(self)
        self.assertEqual(anonymous.call("GET", f"/p/{pid}/api/health")[0], 401)
        _, _, res = anonymous.call("GET", f"/p/{pid}/")
        self.assertEqual(res.getheader("Location"), f"/#next=/p/{pid}/")

    def test_invites_signup_and_reuse(self):
        admin = self.client("admin@a.org")
        status, data, _ = admin.call("POST", f"/api/tenants/{self.a}/invites", {"role": "editor"})
        token = data["link"].split("#invite=")[1]
        self.assertEqual(Client(self).call("GET", f"/api/invites/{token}")[1]["tenant"], "Team A")
        new = Client(self)
        body = {"email": "new@x.org", "name": "New", "password": "password-123", "invite": token}
        self.assertEqual(new.call("POST", "/api/signup", body)[0], 200)
        me = new.call("GET", "/api/me")[1]["user"]
        self.assertEqual([(t["name"], t["role"]) for t in me["tenants"]], [("Team A", "editor")])
        again = Client(self)
        self.assertEqual(again.call("POST", "/api/signup", {**body, "email": "other@x.org"})[0], 410)
        self.assertEqual(Client(self).call("GET", f"/api/invites/{token}")[0], 410)

    def test_email_bound_invite_and_existing_user_accept(self):
        admin = self.client("admin@b.org")
        _, data, _ = admin.call("POST", f"/api/tenants/{self.b}/invites", {"role": "viewer", "email": "Editor@A.org"})
        token = data["link"].split("#invite=")[1]
        wrong = self.client("viewer@a.org")
        self.assertEqual(wrong.call("POST", f"/api/invites/{token}/accept")[0], 403)
        right = self.client("editor@a.org")
        self.assertEqual(right.call("POST", f"/api/invites/{token}/accept")[0], 200)
        self.assertEqual(right.call("GET", f"/api/tenants/{self.b}")[1]["role"], "viewer")

    def test_signup_modes(self):
        body = {"email": "solo@y.org", "name": "Solo", "password": "password-123"}
        self.assertEqual(Client(self).call("POST", "/api/signup", body)[0], 403)  # invite_only default
        self.app.save_settings({"signup_mode": "open_domains", "signup_domains": ["y.org"]})
        self.assertEqual(Client(self).call("POST", "/api/signup", body)[0], 403)  # password emails are unverified
        self.app.save_settings({"signup_mode": "open"})
        solo = Client(self)
        new = solo.call("POST", "/api/signup", body)
        self.assertEqual(new[:2], (200, {"signin": True}))
        self.assertIsNone(solo.cookie)  # signs in next, like anyone
        self.assertEqual(solo.login("solo@y.org")[0], 200)
        tenants = solo.call("GET", "/api/me")[1]["user"]["tenants"]
        self.assertEqual([(t["name"], t["role"]) for t in tenants], [("Solo's workspace", "admin")])
        again = Client(self)
        taken = again.call("POST", "/api/signup", {**body, "password": "other-password"})
        self.assertEqual(taken[:2], new[:2])  # no account enumeration
        self.assertIsNone(again.cookie)
        self.assertEqual(again.login("solo@y.org", "other-password")[0], 401)
        self.assertEqual(self.app.db.one("SELECT COUNT(*) AS n FROM users WHERE email = 'solo@y.org'")["n"], 1)
        self.app.signups = host.Throttle(2, 3600)  # signups_per_ip_hour = 2
        for n in range(2):
            Client(self).call("POST", "/api/signup", {**body, "email": f"x{n}@y.org"})
        self.assertEqual(Client(self).call("POST", "/api/signup", {**body, "email": "x9@y.org"})[0], 429)
        with self.assertRaises(host.HttpError):  # one workspace per person
            host.personal_tenant(self.app, self.app.db.one("SELECT id FROM users WHERE email = 'x0@y.org'")["id"], "X")

    def test_members_roles_and_last_admin(self):
        admin = self.client("admin@a.org")
        members = {m["email"]: m["id"] for m in admin.call("GET", f"/api/tenants/{self.a}")[1]["members"]}
        me = members["admin@a.org"]
        self.assertEqual(admin.call("POST", f"/api/tenants/{self.a}/members/{me}", {"role": "editor"})[0], 409)
        self.assertEqual(admin.call("DELETE", f"/api/tenants/{self.a}/members/{me}")[0], 409)
        viewer = self.client("viewer@a.org")
        uid = members["viewer@a.org"]
        self.assertEqual(admin.call("POST", f"/api/tenants/{self.a}/members/{uid}", {"role": "owner"})[0], 400)
        self.assertEqual(admin.call("POST", f"/api/tenants/{self.a}/members/{uid}", {"role": "editor"})[0], 200)
        self.assertEqual(viewer.call("GET", f"/api/tenants/{self.a}")[1]["role"], "editor")
        self.assertEqual(admin.call("DELETE", f"/api/tenants/{self.a}/members/{uid}")[0], 200)
        self.assertEqual(viewer.call("GET", f"/api/tenants/{self.a}")[0], 404)
        # Another tenant's admin cannot touch these members.
        self.assertEqual(self.client("admin@b.org").call("DELETE", f"/api/tenants/{self.a}/members/{me}")[0], 404)

    def test_upload_zip_and_quotas(self):
        editor = self.client("editor@a.org")
        good = make_zip({"Paper/main.tex": "\\documentclass{article}", "Paper/refs.bib": ""})
        status, data, _ = editor.call("POST", f"/api/tenants/{self.a}/upload?name=Paper", raw=good,
                                      ctype="application/zip")
        self.assertEqual(status, 200, data)
        project = self.app.db.one("SELECT * FROM projects WHERE id = ?", data["id"])
        self.assertTrue((self.app.project_source(project) / "refs.bib").is_file())
        bad = make_zip({"main.tex": "x", "../../../escape.tex": "y"})
        self.assertEqual(editor.call("POST", f"/api/tenants/{self.a}/upload?name=Bad", raw=bad,
                                     ctype="application/zip")[0], 400)
        self.assertEqual(self.app.db.one("SELECT COUNT(*) AS n FROM projects")["n"], 1)
        self.assertEqual(sorted(p.name for p in (self.data / "projects" / self.a).iterdir()), [project["id"]])
        self.app.save_settings({"max_projects_per_tenant": 1})
        self.assertEqual(editor.call("POST", f"/api/tenants/{self.a}/projects", {"name": "Two"})[0], 403)
        self.app.save_settings({"max_projects_per_tenant": 5, "max_project_mb": 1})
        big = make_zip({"main.tex": "x" * (2 * 1024 * 1024)})
        self.assertEqual(editor.call("POST", f"/api/tenants/{self.a}/upload?name=Big", raw=big,
                                     ctype="application/zip")[0], 413)

    def test_upload_cap_and_concurrent_imports(self):
        editor = self.client("editor@a.org")
        self.app.config["max_upload_mb"] = 1
        big = os.urandom(1024 * 1024 + 1)
        self.assertEqual(editor.call("POST", f"/api/tenants/{self.a}/upload?name=Big", raw=big,
                                     ctype="application/zip")[0], 413)
        good = make_zip({"main.tex": "x"})
        with mock.patch.object(host.IMPORTS, "acquire", lambda timeout: False):  # two imports already running
            status, _, res = editor.call("POST", f"/api/tenants/{self.a}/upload?name=Busy", raw=good,
                                         ctype="application/zip")
        self.assertEqual((status, res.getheader("Retry-After")), (503, "30"))
        self.assertEqual(editor.call("POST", f"/api/tenants/{self.a}/upload?name=Fine", raw=good,
                                     ctype="application/zip")[0], 200)

    def test_site_admin(self):
        self.user("root@example.org", site_admin=True)
        root = self.client("root@example.org")
        data = root.call("GET", "/api/admin")[1]
        self.assertEqual({t["name"] for t in data["tenants"]}, {"Team A", "Team B"})
        self.assertEqual(root.call("POST", "/api/admin/settings", {"signup_mode": "anything"})[0], 400)
        self.assertEqual(root.call("POST", "/api/admin/settings", {"max_workers": 0})[0], 400)
        self.assertEqual(root.call("POST", "/api/admin/settings", {"shell": "rm -rf"})[0], 400)
        self.assertEqual(root.call("POST", "/api/admin/settings", {"signup_mode": "open", "max_workers": 4})[0], 200)
        self.assertEqual(self.app.settings()["max_workers"], 4)
        status, data, _ = root.call("POST", "/api/admin/tenants", {"name": "Team C"})
        self.assertEqual(status, 200)
        self.assertEqual(root.call("GET", f"/api/tenants/{data['id']}")[1]["role"], "admin")  # the operator
        pid = self.make_project(self.client("editor@a.org"), self.a)
        self.assertEqual(root.call("DELETE", f"/api/admin/tenants/{self.a}")[0], 200)
        self.assertFalse((self.data / "projects" / self.a).exists())
        self.assertIsNone(self.app.db.one("SELECT id FROM projects WHERE id = ?", pid))
        self.assertIn("tenant_deleted", [r["action"] for r in self.app.db.all("SELECT action FROM audit")])

    def test_admin_reset_link(self):
        self.user("root@example.org", site_admin=True)
        uid = self.app.db.one("SELECT id FROM users WHERE email = 'viewer@a.org'")["id"]
        viewer = self.client("viewer@a.org")
        _, data, _ = self.client("root@example.org").call("POST", f"/api/admin/users/{uid}/reset")
        token = data["link"].split("#reset=")[1]
        self.assertEqual(Client(self).call("POST", "/api/reset", {"token": token, "password": "brand-new-pass"})[0],
                         200)
        self.assertEqual(viewer.call("GET", "/api/account")[0], 401)
        self.assertEqual(Client(self).call("POST", "/api/reset", {"token": token, "password": "brand-new-pass"})[0],
                         410)
        self.assertEqual(self.client().login("viewer@a.org", "brand-new-pass")[0], 200)


class WorkerPool(HostCase):
    """Starting outside the lock, one start per project, and fair-share eviction when the server is full."""

    def setUp(self):
        super().setUp()
        self.spawned: list[str] = []
        self.gates: dict[str, threading.Event] = {}

        def spawn(project):
            self.spawned.append(project["id"])
            gate = self.gates.get(project["id"])
            if gate:
                gate.wait(10)
            return mock.Mock(poll=lambda: None, pid=0), 1, "s"

        mock.patch.object(self.app.workers, "_spawn", spawn).start()
        mock.patch.object(host.Workers, "_kill", staticmethod(lambda w: None)).start()

    @staticmethod
    def project(pid, tenant):
        return {"id": pid, "tenant_id": tenant, "slug": "doc"}

    def test_a_slow_start_blocks_only_its_own_project(self):
        self.gates["p1"] = threading.Event()
        got = []
        first = threading.Thread(target=lambda: got.append(self.app.workers.acquire(self.project("p1", "a"))))
        second = threading.Thread(target=lambda: got.append(self.app.workers.acquire(self.project("p1", "a"))))
        first.start()
        time.sleep(0.1)
        second.start()
        other = self.app.workers.acquire(self.project("p2", "a"))  # does not wait for p1
        self.assertEqual(other.project["id"], "p2")
        self.assertEqual(got, [])
        self.gates["p1"].set()
        first.join(5)
        second.join(5)
        self.assertEqual(len(got), 2)
        self.assertIs(got[0], got[1])
        self.assertEqual(self.spawned.count("p1"), 1)
        self.assertEqual(got[0].active, 2)

    def test_full_server_evicts_from_the_tenant_with_the_most_workers(self):
        self.app.save_settings({"max_workers": 2, "max_workers_per_tenant": 2})
        workers = self.app.workers
        a1 = workers.acquire(self.project("a1", "A"))
        workers.acquire(self.project("a2", "A"))
        a1.used -= 100  # least recently active
        workers.acquire(self.project("b1", "B"))  # busy everywhere: A gives one up
        self.assertEqual(sorted(workers.running), ["a2", "b1"])
        with self.assertRaises(host.HttpError) as busy:  # B has its share already; A keeps its last one
            workers.acquire(self.project("b2", "B"))
        self.assertEqual(busy.exception.status, 503)
        workers.acquire(self.project("c1", "C"))  # C has none: one of the others gives one up
        self.assertEqual(len(workers.running), 2)
        self.assertIn("c1", workers.running)
        self.assertIn("worker_evicted", [r["action"] for r in self.app.db.all("SELECT action FROM audit")])

    def test_idle_workers_go_first_and_a_failed_start_is_not_kept(self):
        self.app.save_settings({"max_workers": 1})
        idle = self.app.workers.acquire(self.project("a1", "A"))
        self.app.workers.release(idle)
        self.app.workers.acquire(self.project("b1", "B"))
        self.assertEqual(list(self.app.workers.running), ["b1"])
        with mock.patch.object(self.app.workers, "_spawn", side_effect=host.HttpError(502, "no")):
            with self.assertRaises(host.HttpError):
                self.app.workers.acquire(self.project("b2", "B"))  # evicts nothing: b1 is busy and B's only one
        with mock.patch.object(self.app.workers, "_spawn", side_effect=OSError("boom")), \
                mock.patch("traceback.print_exc"):
            self.app.save_settings({"max_workers": 2})
            with self.assertRaises(host.HttpError):
                self.app.workers.acquire(self.project("c1", "C"))
        self.assertEqual(list(self.app.workers.running), ["b1"])


class Upstream(threading.Thread):
    """A fake worker that answers every request with what it received."""

    def __init__(self):
        super().__init__(daemon=True)
        self.sock = socket.socket()
        self.sock.bind(("127.0.0.1", 0))
        self.sock.listen(5)
        self.port = self.sock.getsockname()[1]

    def run(self):
        while True:
            try:
                conn, _ = self.sock.accept()
            except OSError:
                return
            data = b""
            while b"\r\n\r\n" not in data:
                data += conn.recv(65536)
            head, body = data.split(b"\r\n\r\n", 1)
            rows = head.decode().split("\r\n")
            size = int(next((r.split(":")[1] for r in rows if r.lower().startswith("content-length")), "0"))
            while len(body) < size:
                body += conn.recv(65536)
            reply = json.dumps({"line": rows[0], "headers": rows[1:], "body": body.decode()}).encode()
            conn.sendall(b"HTTP/1.0 200 OK\r\nContent-Type: application/json\r\nSet-Cookie: evil=1\r\n"
                         b"Content-Length: " + str(len(reply)).encode() + b"\r\n\r\n" + reply)
            conn.close()


class Proxy(HostCase):
    def setUp(self):
        super().setUp()
        self.tenant = host.create_tenant(self.app, "Team")
        self.user("ed@x.org", "editor", self.tenant)
        self.user("vi@x.org", "viewer", self.tenant)
        self.pid = Workspaces.make_project(self, self.client("ed@x.org"), self.tenant, "Doc", "article")

    def fake_worker(self, port, secret="w" * 43):
        project = self.app.db.one("SELECT * FROM projects WHERE id = ?", self.pid)
        worker = host.Worker(project)
        worker.proc, worker.port, worker.secret = mock.Mock(poll=lambda: None), port, secret
        worker.ready.set()
        mock.patch.object(self.app.workers, "acquire", lambda p: worker).start()
        mock.patch.object(self.app.workers, "release", lambda w: None).start()
        return worker

    def test_headers_are_stripped_and_identity_added(self):
        upstream = Upstream()
        upstream.start()
        self.addCleanup(upstream.sock.close)
        self.fake_worker(upstream.port)
        viewer = self.client("vi@x.org")
        forged = {"X-Host-Role": "edit", "X-Host-Secret": "guess", "X-Host-User": "1;Admin",
                  "X-Forwarded-For": "1.2.3.4"}
        status, data, res = viewer.call("GET", f"/p/{self.pid}/api/file?doc=Doc&path=main.tex", headers=forged)
        self.assertEqual(status, 200)
        self.assertEqual(data["line"], "GET /api/file?doc=Doc&path=main.tex HTTP/1.1")
        headers = dict(h.split(": ", 1) for h in data["headers"])
        self.assertEqual(headers["X-Host-Role"], "view")
        self.assertEqual(headers["X-Host-Secret"], "w" * 43)
        self.assertTrue(headers["X-Host-User"].endswith(";Vi"))
        self.assertNotIn("Cookie", headers)
        self.assertNotIn("X-Forwarded-For", headers)
        self.assertEqual([h for h in data["headers"] if h.startswith("X-Host-Role")], ["X-Host-Role: view"])
        self.assertIsNone(res.getheader("Set-Cookie"))
        self.assertEqual(res.getheader("X-Frame-Options"), "DENY")
        self.assertEqual(res.getheader("X-Content-Type-Options"), "nosniff")
        self.assertEqual(res.getheader("Referrer-Policy"), "no-referrer")
        self.assertIn("frame-ancestors 'none'", res.getheader("Content-Security-Policy"))
        url = f"/p/{self.pid}/api/file?doc=Doc&path=main.tex"
        cases = [("cross-site", "no-cors", "image", 403), ("cross-site", "cors", "empty", 403),
                 ("cross-site", "navigate", "document", 200),  # a link to the project from another site
                 ("same-origin", "cors", "empty", 200)]
        for site, mode, dest, status in cases:
            fetch = {"Sec-Fetch-Site": site, "Sec-Fetch-Mode": mode, "Sec-Fetch-Dest": dest}
            with self.subTest(fetch=fetch):
                self.assertEqual(viewer.call("GET", url, headers=fetch)[0], status)
        status, data, _ = self.client("ed@x.org").call("PUT", f"/p/{self.pid}/api/file?doc=Doc&path=a.tex",
                                                       {"text": "hi"})
        self.assertEqual(json.loads(data["body"]), {"text": "hi"})
        self.assertIn("X-Host-Role: edit", data["headers"])

    def test_workspace_admins_are_marked_for_the_worker(self):
        upstream = Upstream()
        upstream.start()
        self.addCleanup(upstream.sock.close)
        self.fake_worker(upstream.port)
        self.user("ad@x.org", "admin", self.tenant)
        url = f"/p/{self.pid}/api/file?doc=Doc&path=main.tex"
        forged = {"X-Host-Admin": "1"}  # a client cannot say it: X-Host-* never passes the proxy
        for email, role, admin in (("ed@x.org", "edit", []), ("vi@x.org", "view", []),
                                   ("ad@x.org", "edit", ["X-Host-Admin: 1"])):
            with self.subTest(email=email):
                status, data, _ = self.client(email).call("GET", url, headers=forged)
                self.assertEqual(status, 200)
                self.assertIn(f"X-Host-Role: {role}", data["headers"])
                self.assertEqual([h for h in data["headers"] if h.startswith("X-Host-Admin")], admin)

    def test_writes_need_same_origin_and_respect_the_quota(self):
        self.fake_worker(1)
        editor = self.client("ed@x.org")
        self.assertEqual(editor.call("PUT", f"/p/{self.pid}/api/file?doc=Doc&path=a.tex", {"text": "x"},
                                     origin=False)[0], 403)
        self.app.save_settings({"max_project_mb": 1})
        big = "x" * (1024 * 1024 + 10)
        self.assertEqual(editor.call("PUT", f"/p/{self.pid}/api/file?doc=Doc&path=a.tex", {"text": big})[0], 507)

    def test_open_streams_per_user_are_capped(self):
        upstream = Upstream()
        upstream.start()
        self.addCleanup(upstream.sock.close)
        self.fake_worker(upstream.port)
        uid = self.app.db.one("SELECT id FROM users WHERE email = 'ed@x.org'")["id"]
        editor = self.client("ed@x.org")
        self.app.streams[uid] = self.app.config["max_streams_per_user"]
        self.assertEqual(editor.call("GET", f"/p/{self.pid}/api/poll?cid=a")[0], 429)
        self.assertEqual(editor.call("GET", f"/p/{self.pid}/api/files?doc=Doc")[0], 200)  # not a stream
        self.assertEqual(self.client("vi@x.org").call("GET", f"/p/{self.pid}/api/poll?cid=b")[0], 200)
        self.app.streams[uid] -= 1
        self.assertEqual(editor.call("GET", f"/p/{self.pid}/api/poll?cid=a")[0], 200)
        deadline = time.monotonic() + 5  # The handler gives its slot back just after the response.
        while self.app.streams[uid] != self.app.config["max_streams_per_user"] - 1 and time.monotonic() < deadline:
            time.sleep(0.02)
        self.assertEqual(self.app.streams[uid], self.app.config["max_streams_per_user"] - 1)

    def real_worker(self):
        """serve.py's gateway handler in this process, standing in for a worker."""
        project = self.app.db.one("SELECT * FROM projects WHERE id = ?", self.pid)
        mock.patch.dict(serve.GATEWAY, {"secret": b""}).start()
        mock.patch.dict(serve.SETTINGS, {"check_host": True}).start()
        saved = dict(serve.SHARE)
        self.addCleanup(lambda: (serve.SHARE.clear(), serve.SHARE.update(saved), serve.share_env(False)))
        mock.patch.dict(serve.DOCS, {"Doc": self.app.project_source(project) / "main.tex"}, clear=True).start()
        for table in (serve.ROOMS, serve.CLIENTS):
            mock.patch.dict(table, {}, clear=True).start()
        secret = "k" * 43
        serve.gateway_enable(secret, "Doc")
        server = ThreadingHTTPServer(("127.0.0.1", 0), serve.Handler)
        server.daemon_threads = True
        threading.Thread(target=server.serve_forever, daemon=True).start()
        self.addCleanup(server.server_close)
        self.addCleanup(server.shutdown)
        self.fake_worker(server.server_address[1], secret)

    def test_role_reaches_the_worker(self):
        self.real_worker()
        viewer, editor = self.client("vi@x.org"), self.client("ed@x.org")
        self.assertEqual(viewer.call("GET", f"/p/{self.pid}/api/config")[1]["role"], "view")
        self.assertEqual(viewer.call("PUT", f"/p/{self.pid}/api/file?doc=Doc&path=main.tex", {"text": "x"})[0], 403)
        self.assertEqual(editor.call("PUT", f"/p/{self.pid}/api/file?doc=Doc&path=main.tex", {"text": "x"})[0], 200)
        self.assertEqual(editor.call("PUT", f"/p/{self.pid}/api/file?doc=Doc&path=build.toml", {"text": "x"})[0], 403)
        status, page, res = viewer.call("GET", f"/p/{self.pid}/")
        self.assertEqual(status, 200)
        self.assertIn(b'src="ui/app.js"', page)  # relative: works under /p/<id>/
        policies = res.headers.get_all("Content-Security-Policy")
        self.assertTrue(any("ws://" + self.netloc in p for p in policies))  # the worker's own CSP is kept
        self.assertIn("frame-ancestors 'none'", policies)

    def test_websocket_through_the_gateway_and_revocation(self):
        self.real_worker()
        mock.patch.object(host, "RECHECK", 0.2).start()
        editor = self.client("ed@x.org")
        sock = socket.create_connection(("127.0.0.1", self.port), timeout=10)
        self.addCleanup(sock.close)
        sock.sendall((f"GET /p/{self.pid}/ws?cid=abc HTTP/1.1\r\nHost: {self.netloc}\r\nOrigin: {self.origin}\r\n"
                      f"Upgrade: websocket\r\nConnection: Upgrade\r\nSec-WebSocket-Key: dGhlIHNhbXBsZSBub25jZQ==\r\n"
                      f"Sec-WebSocket-Version: 13\r\nCookie: {editor.cookie}\r\n\r\n").encode())
        stream = sock.makefile("rb")
        self.assertIn(b" 101 ", stream.readline())
        while stream.readline() not in (b"\r\n", b""):
            pass
        sock.sendall(serve.ws_encode(0x1, json.dumps({"type": "ping", "data": 5}).encode(), b"\x01\x02\x03\x04"))
        for _ in range(10):
            frame = serve.ws_read(stream.read)
            if frame and json.loads(frame[1]).get("type") == "pong":
                break
        else:
            self.fail("no pong through the gateway")
        editor.call("POST", "/api/logout")  # The open socket notices within RECHECK seconds.
        deadline = time.monotonic() + 15
        closed = False
        while time.monotonic() < deadline and not closed:
            try:
                closed = not sock.recv(65536)
            except socket.timeout:
                break
        self.assertTrue(closed)

    @unittest.skipUnless(shutil.which("latexmk") and os.name != "nt", "needs latexmk to start a real worker")
    def test_real_worker_starts_serves_and_stops(self):
        editor = self.client("ed@x.org")
        status, data, _ = editor.call("GET", f"/p/{self.pid}/api/health")
        self.assertEqual((status, data["docs"]), (200, ["Doc"]))
        worker = self.app.workers.running[self.pid]
        self.assertEqual(self.client("vi@x.org").call("GET", f"/p/{self.pid}/api/config")[1]["role"], "view")
        self.app.save_settings({"worker_idle_minutes": 1})
        worker.used -= 120
        self.app.workers.reap()
        self.assertNotIn(self.pid, self.app.workers.running)
        self.assertIsNotNone(worker.proc.wait(10))
        self.assertTrue((self.app.project_root({"tenant_id": self.tenant, "id": self.pid}) / ".home").is_dir())


REAL_HTTP_JSON = host.http_json


def fake_jwt(claims: dict) -> str:
    enc = lambda d: base64.urlsafe_b64encode(json.dumps(d).encode()).decode().rstrip("=")  # noqa: E731
    return f"{enc({'alg': 'RS256'})}.{enc(claims)}.c2ln"


class Provider:
    """Stands in for http_json: an OIDC issuer at https://id.example and GitHub's API."""

    def __init__(self, case):
        self.case, self.calls = case, []
        self.claims: dict = {}
        self.meta = {"issuer": "https://id.example", "authorization_endpoint": "https://id.example/auth",
                     "token_endpoint": "https://id.example/token", "userinfo_endpoint": "https://id.example/userinfo"}
        self.github_emails = [{"email": "gh@x.org", "primary": True, "verified": True}]

    def __call__(self, method, url, form=None, headers=None):
        self.calls.append((method, url, form, headers))
        if url.endswith("/.well-known/openid-configuration"):
            return self.meta
        if url == "https://id.example/token":
            return {"id_token": fake_jwt(self.claims), "access_token": "at"}
        if url == "https://id.example/userinfo":
            return {"sub": self.claims["sub"], "email": "late@x.org", "email_verified": True}
        if url == host.GITHUB["token"]:
            return {"access_token": "gh-token"}
        if url == host.GITHUB["user"]:
            return {"id": 42, "login": "octo", "name": "Octo Cat"}
        if url == host.GITHUB["emails"]:
            return self.github_emails
        raise AssertionError(url)


class SignInProviders(HostCase):
    def extra_config(self):
        return {"providers": {
            "idp": {"type": "oidc", "issuer": "https://id.example", "client_id": "cid", "client_secret": "shh"},
            "github": {"type": "github", "client_id": "gid", "client_secret": "gsh"}}}

    def setUp(self):
        super().setUp()
        self.provider = Provider(self)
        self.overrides: dict = {}
        mock.patch.object(host, "http_json", self.provider).start()
        self.app.save_settings({"signup_mode": "open"})

    def test_discovery_works_on_a_freshly_booted_machine(self):
        with mock.patch.object(host.time, "monotonic", return_value=5.0):  # uptime under the cache hour
            self.assertEqual(host.discover("idp", self.app.config["providers"]["idp"]), self.provider.meta)

    def start(self, client, name="idp", query=""):
        status, _, res = client.call("GET", f"/auth/{name}/start{query}")
        self.assertEqual(status, 302)
        location = res.getheader("Location")
        params = {k: v[0] for k, v in host.parse_qs(host.urlsplit(location).query).items()}
        return location, params

    def finish(self, client, params, name="idp", code="the-code", state=None):
        nonce = params.get("nonce")
        self.provider.claims = {"iss": "https://id.example", "aud": "cid", "exp": time.time() + 300,
                                "iat": time.time(), "nonce": nonce, "sub": "user-1", "email": "Ann@X.org",
                                "email_verified": True, "name": "Ann", **self.overrides}
        self.overrides = {}
        status, _, res = client.call("GET", f"/auth/{name}/callback?code={code}&state={state or params['state']}")
        self.assertEqual(status, 302)
        client.call("GET", "/api/me")  # picks up the new CSRF token
        return res.getheader("Location")

    def test_oidc_sign_up_with_pkce_state_and_nonce(self):
        client = Client(self)
        location, params = self.start(client)
        self.assertTrue(location.startswith("https://id.example/auth?"))
        self.assertEqual((params["client_id"], params["code_challenge_method"], params["scope"]),
                         ("cid", "S256", "openid email profile"))
        self.assertEqual(params["redirect_uri"], f"{self.origin}/auth/idp/callback")
        self.assertTrue(params["nonce"] and params["state"])
        self.assertEqual(self.finish(client, params), "/")
        me = client.call("GET", "/api/me")[1]["user"]
        self.assertEqual((me["email"], me["name"], me["password"]), ("ann@x.org", "Ann", False))
        self.assertEqual([t["role"] for t in me["tenants"]], ["admin"])  # open sign-up: own workspace
        _, url, form, headers = next(c for c in self.provider.calls if c[1] == "https://id.example/token")
        verifier = form["code_verifier"]
        self.assertEqual(host.b64(host.hashlib.sha256(verifier.encode()).digest()), params["code_challenge"])
        self.assertEqual(headers["Authorization"], "Basic " + base64.b64encode(b"cid:shh").decode())
        # The same provider account signs in to the same user next time.
        again = Client(self)
        _, params = self.start(again)
        self.finish(again, params)
        self.assertEqual(again.call("GET", "/api/me")[1]["user"]["id"], me["id"])

    def test_state_must_match_the_browser_and_flows_are_single_use(self):
        client = Client(self)
        _, params = self.start(client)
        other = Client(self)  # e.g. an attacker's callback URL opened in the victim's browser
        self.assertTrue(self.finish(other, params).startswith("/#error="))
        self.assertIsNone(other.call("GET", "/api/me")[1]["user"])
        self.assertEqual(self.finish(client, params), "/")
        replay = Client(self)
        replay.jar["lp_oauth"] = params["state"]
        self.assertTrue(self.finish(replay, params).startswith("/#error="))  # the flow was used
        wrong = Client(self)
        _, fresh = self.start(wrong)
        self.assertTrue(self.finish(wrong, fresh, state="x" * 43).startswith("/#error="))
        self.assertEqual(self.app.db.one("SELECT COUNT(*) AS n FROM users")["n"], 1)

    def test_bad_claims_are_refused(self):
        bad = {"iss": {"iss": "https://evil.example"}, "aud": {"aud": "someone-else"},
               "azp": {"aud": ["cid", "other"], "azp": "other"}, "exp": {"exp": time.time() - 3600},
               "nonce": {"nonce": "replayed"}, "unverified": {"email_verified": False},
               "string false": {"email_verified": "false"}}
        for name, claims in bad.items():
            with self.subTest(name=name):
                client = Client(self)
                _, params = self.start(client)
                self.overrides = claims
                self.assertTrue(self.finish(client, params).startswith("/#error="), name)
                self.assertIsNone(client.call("GET", "/api/me")[1]["user"])
        self.assertEqual(self.app.db.one("SELECT COUNT(*) AS n FROM users")["n"], 0)

    def test_userinfo_fills_a_missing_email_and_discovery_issuer_is_checked(self):
        client = Client(self)
        _, params = self.start(client)
        claims = {"iss": "https://id.example", "aud": "cid", "exp": time.time() + 300, "nonce": params["nonce"],
                  "sub": "user-9"}
        with mock.patch.object(self.provider, "claims", claims):
            status, _, res = client.call("GET", f"/auth/idp/callback?code=c&state={params['state']}")
        self.assertEqual(res.getheader("Location"), "/")
        self.assertEqual(self.app.db.one("SELECT email FROM users")["email"], "late@x.org")
        self.app.discovery.clear()
        self.provider.meta = {**self.provider.meta, "issuer": "https://evil.example"}
        status, data, _ = Client(self).call("GET", "/auth/idp/start")
        self.assertEqual(status, 502)

    def test_existing_password_account_is_not_taken_over(self):
        uid = self.user("ann@x.org")
        client = Client(self)
        _, params = self.start(client)
        self.assertIn("already%20exists", self.finish(client, params))
        self.app.save_settings({"auto_link": True})  # still refused: that account's email was never verified
        client = Client(self)
        _, params = self.start(client)
        self.assertIn("already%20exists", self.finish(client, params))
        # Linking from the account page (signed in with the password) works, then the provider signs in.
        ann = self.client("ann@x.org")
        _, params = self.start(ann, query="?intent=link")
        self.assertEqual(self.finish(ann, params), "/#account")
        self.assertEqual([i["provider"] for i in ann.call("GET", "/api/account")[1]["identities"]], ["idp"])
        fresh = Client(self)
        _, params = self.start(fresh)
        self.finish(fresh, params)
        self.assertEqual(fresh.call("GET", "/api/me")[1]["user"]["id"], uid)
        self.assertEqual(Client(self).call("GET", "/auth/idp/start?intent=link")[0], 401)

    def test_auto_link_for_verified_accounts_and_two_step_still_applies(self):
        uid = self.user("ann@x.org", verified=True)
        secret = host.new_totp_secret()
        self.app.db.run("UPDATE users SET totp_secret = ? WHERE id = ?", secret, uid)
        self.app.save_settings({"auto_link": True})
        client = Client(self)
        _, params = self.start(client)
        self.assertEqual(self.finish(client, params), "/")
        self.assertEqual(client.call("GET", "/api/me")[1]["stage"], "mfa")
        code = host.totp_code(secret, int(time.time() // 30))
        self.assertEqual(client.call("POST", "/api/login/code", {"code": code})[0], 200)

    def test_invite_only_needs_an_invite(self):
        self.app.save_settings({"signup_mode": "invite_only"})
        client = Client(self)
        _, params = self.start(client)
        self.assertIn("invitation", self.finish(client, params))
        tenant = host.create_tenant(self.app, "Club")
        token = "t" * 32
        self.app.db.run("INSERT INTO invites(token_hash, tenant_id, role, created, expires) VALUES (?, ?, ?, ?, ?)",
                        host.token_hash(token), tenant, "viewer", time.time(), time.time() + 60)
        client = Client(self)
        _, params = self.start(client, query=f"?invite={token}")
        self.assertEqual(self.finish(client, params), "/")
        tenants = client.call("GET", "/api/me")[1]["user"]["tenants"]
        self.assertEqual([(t["name"], t["role"]) for t in tenants], [("Club", "viewer")])

    def test_open_domains_uses_the_verified_provider_email(self):
        self.app.save_settings({"signup_mode": "open_domains", "signup_domains": ["corp.org"]})
        client = Client(self)
        _, params = self.start(client)
        self.assertIn("domain", self.finish(client, params))
        client = Client(self)
        _, params = self.start(client)
        self.overrides = {"email": "ann@corp.org"}
        self.assertEqual(self.finish(client, params), "/")

    def test_github(self):
        client = Client(self)
        location, params = self.start(client, "github")
        self.assertTrue(location.startswith(host.GITHUB["authorize"]))
        self.assertNotIn("nonce", params)
        self.assertEqual(self.finish(client, params, "github"), "/")
        me = client.call("GET", "/api/me")[1]["user"]
        self.assertEqual((me["email"], me["name"]), ("gh@x.org", "Octo Cat"))
        form = next(c[2] for c in self.provider.calls if c[1] == host.GITHUB["token"])
        self.assertEqual((form["client_secret"], bool(form["code_verifier"])), ("gsh", True))
        self.provider.github_emails = [{"email": "x@y.org", "primary": True, "verified": False}]
        client = Client(self)
        _, params = self.start(client, "github")
        self.assertIn("verified", self.finish(client, params, "github"))

    def test_unlink_keeps_a_way_in(self):
        client = Client(self)
        _, params = self.start(client)
        self.finish(client, params)
        self.assertEqual(client.call("DELETE", "/api/account/identities/idp")[0], 409)  # no password yet
        self.assertEqual(client.call("POST", "/api/account/password", {"password": "now-a-password"})[0], 200)
        self.assertEqual(client.call("DELETE", "/api/account/identities/idp")[0], 200)

    def test_http_json_refuses_plain_http(self):
        with self.assertRaises(host.HttpError):
            REAL_HTTP_JSON("GET", "http://id.example/x")


def ai_reply(edits=()):
    payload = json.dumps({"answer": "Close the brace.", "text": "", "edits": list(edits)})
    return {"stop_reason": "end_turn", "model": "claude-opus-5-5", "usage": {"input_tokens": 100, "output_tokens": 20},
            "content": [{"type": "text", "text": payload}]}


class Assistant(HostCase):
    """/p/<id>/api/ai: the gateway makes the call with the operator's key; workers never see the key or the request."""

    KEY = "sk-ant-operator-" + "o" * 30
    AI = {"enabled": True, "api_key": KEY, "daily_per_user": 3, "daily_per_workspace": 100}

    def extra_config(self):
        return {"ai": dict(self.AI)}

    def setUp(self):
        super().setUp()
        self.tenant = host.create_tenant(self.app, "Team")
        self.other = host.create_tenant(self.app, "Other")
        self.user("ed@x.org", "editor", self.tenant)
        self.user("vi@x.org", "viewer", self.tenant)
        self.user("out@x.org", "editor", self.other)
        self.pid = Workspaces.make_project(self, self.client("ed@x.org"), self.tenant, "Doc", "article")
        mock.patch.object(self.app.workers, "acquire", side_effect=AssertionError("no worker for AI")).start()
        mock.patch.object(ai, "LIMITER", ai.grammar.Limiter(requests=1000, size=10 ** 9)).start()
        self.calls = []
        mock.patch.object(ai, "post_json", side_effect=lambda url, body, headers: self.calls.append((body, headers))
                          or ai_reply([{"file": "build.toml", "old": "a", "new": "b"},
                                       {"file": "main.tex", "old": "\\textbf{x", "new": "\\textbf{x}"}])).start()

    def ask(self, client, data=None, **kw):
        data = data or {"task": "explain", "error": {"message": "Runaway argument?"},
                        "files": [{"path": "main.tex", "text": "a \\textbf{x\n", "line": 1}]}
        return client.call("POST", f"/p/{self.pid}/api/ai", data, **kw)

    def test_editors_get_checked_answers_and_the_key_stays_on_the_gateway(self):
        editor = self.client("ed@x.org")
        status, out, _ = self.ask(editor)
        self.assertEqual(status, 200, out)
        self.assertEqual(out["answer"], "Close the brace.")
        self.assertEqual([e["file"] for e in out["edits"]], ["main.tex"])  # build.toml is owner-only: dropped
        self.assertEqual(self.calls[0][1]["x-api-key"], self.KEY)
        self.assertNotIn(self.KEY, json.dumps(out))
        status, info, _ = editor.call("GET", f"/p/{self.pid}/api/ai")
        self.assertEqual((info["enabled"], info["left"]), (True, 2))
        self.assertNotIn(self.KEY, json.dumps(info))

    def test_roles_origin_and_membership(self):
        viewer = self.client("vi@x.org")
        self.assertFalse(viewer.call("GET", f"/p/{self.pid}/api/ai")[1]["enabled"])
        self.assertEqual(self.ask(viewer)[0], 403)
        self.assertEqual(self.ask(self.client("out@x.org"))[0], 404)  # not their project: 404, not 403
        self.assertEqual(self.ask(Client(self))[0], 401)
        self.assertEqual(self.ask(self.client("ed@x.org"), origin=False)[0], 403)
        self.assertEqual(self.ask(self.client("ed@x.org"), origin="https://evil.example")[0], 403)
        self.assertEqual(self.calls, [])

    def test_off_without_the_operator(self):
        self.app.config["ai"] = {**self.AI, "enabled": False}
        editor = self.client("ed@x.org")
        self.assertFalse(editor.call("GET", f"/p/{self.pid}/api/ai")[1]["enabled"])
        self.assertEqual(self.ask(editor)[0], 403)
        self.app.config["ai"] = {**self.AI, "api_key": "", "api_key_env": "LP_NO_SUCH_KEY"}
        self.assertEqual(self.ask(editor)[0], 403)
        self.assertEqual(self.calls, [])

    def test_daily_limits_and_usage_for_the_site_admin(self):
        editor = self.client("ed@x.org")
        self.assertEqual([self.ask(editor)[0] for _ in range(4)], [200, 200, 200, 429])
        self.assertEqual(len(self.calls), 3)
        self.assertEqual(self.ask(editor, {"task": "nope"})[0], 429)  # used up before it is even read
        self.user("root@x.org", site_admin=True)
        status, data, _ = self.client("root@x.org").call("GET", "/api/admin")
        self.assertEqual(status, 200)
        row = data["ai"]["usage"][0]
        self.assertEqual((row["email"], row["workspace"], row["requests"], row["input_tokens"], row["output_tokens"]),
                         ("ed@x.org", "Team", 3, 300, 60))
        self.assertTrue(data["ai"]["enabled"])
        self.assertNotIn(self.KEY, json.dumps(data))
        self.assertEqual(editor.call("GET", "/api/admin")[0], 404)

    def test_server_wide_cap_and_token_budget(self):
        """Open sign-up gives everyone a workspace of their own, so the per-workspace cap alone is no spending cap."""
        self.app.config["ai"] = {**self.AI, "daily_per_user": 100, "daily_total": 2}
        editor = self.client("ed@x.org")
        self.assertEqual([self.ask(editor)[0] for _ in range(3)], [200, 200, 429])
        self.assertFalse(editor.call("GET", f"/p/{self.pid}/api/ai")[1]["enabled"])
        self.app.config["ai"] = {**self.AI, "daily_per_user": 100, "daily_tokens_per_user": 240}
        self.assertEqual(self.ask(editor)[0], 429)  # 2 x 120 tokens spent
        self.app.config["ai"] = {**self.AI, "daily_per_user": 100, "daily_tokens_per_user": 241}
        self.assertEqual(self.ask(editor)[0], 200)

    def test_billed_failures_are_recorded(self):
        refused = ai_reply()
        refused["stop_reason"] = "refusal"
        refused["usage"]["iterations"] = [{"input_tokens": 100, "output_tokens": 5},
                                          {"input_tokens": 100, "output_tokens": 7}]
        ai.post_json.side_effect = None
        ai.post_json.return_value = refused
        self.assertEqual(self.ask(self.client("ed@x.org"))[0], 422)
        row = self.app.db.one("SELECT requests, input_tokens, output_tokens FROM ai_usage")
        self.assertEqual((row["requests"], row["input_tokens"], row["output_tokens"]), (1, 200, 12))

    def test_build_configuration_is_never_sent(self):
        data = {"task": "explain", "error": {"message": "x"},
                "files": [{"path": ".latexmkrc", "text": "1;", "line": 1}]}
        self.assertEqual(self.ask(self.client("ed@x.org"), data)[0], 400)
        self.assertEqual(self.calls, [])

    def test_bad_requests_and_upstream_errors(self):
        editor = self.client("ed@x.org")
        self.assertEqual(self.ask(editor, {"task": "nope"})[0], 400)
        self.assertEqual(editor.call("POST", f"/p/{self.pid}/api/ai", raw=b"[1]")[0], 400)
        self.assertEqual(editor.call("POST", f"/p/{self.pid}/api/ai", raw=b"x" * (ai.MAX_BODY + 1))[0], 413)
        ai.post_json.side_effect = ai.AiError("Anthropic answered HTTP 500.", 502)
        self.assertEqual(self.ask(editor)[0], 502)

    def test_workers_never_get_the_key_in_their_environment(self):
        seen = {}

        def popen(argv, env, **kw):
            seen.update(env)
            return mock.Mock(stdout=io.StringIO("LP_GATEWAY_PORT=5\n"), pid=0, poll=lambda: None)

        project = self.app.db.one("SELECT * FROM projects WHERE id = ?", self.pid)
        with mock.patch.dict(os.environ, {"ANTHROPIC_API_KEY": "sk-env-should-stay"}), \
                mock.patch.object(host.subprocess, "Popen", popen):
            self.app.workers._spawn(project)
        self.assertTrue(seen)
        self.assertNotIn("ANTHROPIC_API_KEY", seen)
        self.assertNotIn("sk-env-should-stay", json.dumps(seen))
        self.assertNotIn(self.KEY, json.dumps(seen))

    def test_config_section_is_validated(self):
        with tempfile.TemporaryDirectory() as tmp:
            data = Path(tmp)
            for text in ('[ai]\nenabled = "yes"', '[ai]\nmodel = "gpt-4"', '[ai]\nmodel = "claude-3-haiku"',
                         '[ai]\ndaily_total = -1', '[ai]\ndaily_per_user = -1', '[ai]\nshell = "x"', 'ai = 3'):
                (data / "config.toml").write_text(text)
                with self.subTest(text=text), self.assertRaises(host.ConfigError):
                    host.load_config(data)
            (data / "config.toml").write_text('[ai]\nenabled = true\nmodel = "claude-haiku-5-5"')
            self.assertEqual(host.load_config(data)["ai"]["daily_per_user"], 50)


class Cli(unittest.TestCase):
    def test_init_creates_config_db_and_admin(self):
        env = {"LP_ADMIN_EMAIL": "Root@Example.org", "LP_ADMIN_PASSWORD": "password-123"}
        with tempfile.TemporaryDirectory() as tmp, mock.patch.dict(host.SCRYPT, {"n": 2 ** 10}), \
                mock.patch.dict(os.environ, env), \
                mock.patch.object(sys, "stdout", io.StringIO()) as out:
            data = Path(tmp) / "data"
            self.assertEqual(host.cmd_init(data), 0)
            self.assertTrue((data / "config.toml").is_file())
            if os.name != "nt":
                for name in ("config.toml", "host.db"):
                    self.assertEqual(stat.S_IMODE((data / name).stat().st_mode), 0o600, name)
            row = host.APP.db.one("SELECT email, site_admin FROM users")
            self.assertEqual((row["email"], row["site_admin"]), ("root@example.org", 1))
            host.APP.db.conn.close()
            self.assertEqual(host.cmd_init(data), 0)  # again: prints a reset link instead of a second admin
            self.assertIn("#reset=", out.getvalue())
            host.APP.db.conn.close()
            config = host.load_config(data)
            self.assertEqual(config["public_url"], "http://localhost:8080")

    def test_config_validation(self):
        with tempfile.TemporaryDirectory() as tmp:
            data = Path(tmp)
            for text in ('public_url = "ftp://x"', 'nope = 1', 'public_url = "https://x.org/sub"',
                         '[providers.x]\ntype = "saml"\nclient_id = "a"',
                         '[providers.g]\ntype = "oidc"\nclient_id = "a"\nissuer = "http://insecure"',
                         'max_connections = 0', 'max_upload_mb = "50"', 'max_streams_per_user = true'):
                (data / "config.toml").write_text(text)
                with self.subTest(text=text), self.assertRaises(host.ConfigError):
                    host.load_config(data)

    @unittest.skipUnless(sys.platform.startswith("linux"), "hosted serving is Linux only")
    def test_serve_refuses_without_a_working_sandbox(self):
        with tempfile.TemporaryDirectory() as tmp, mock.patch.object(host, "sandbox_works", lambda: False), \
                mock.patch.object(sys, "stderr", io.StringIO()):
            self.assertEqual(host.cmd_serve(Path(tmp), insecure=False), 2)

    def test_insecure_flag_warns_loudly(self):
        with tempfile.TemporaryDirectory() as tmp, mock.patch.object(sys, "stderr", io.StringIO()) as err:
            self.assertEqual(host.cmd_serve(Path(tmp), insecure=True), 2)  # no database yet
        self.assertIn("WITHOUT the bubblewrap sandbox", err.getvalue())


if __name__ == "__main__":
    unittest.main()
