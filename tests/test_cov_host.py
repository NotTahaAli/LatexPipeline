"""host.py: every registered route's sign-in guard, workspace scoping, and the error branches of bodies and zips."""

from __future__ import annotations

import io
import re
import tempfile
import time
import unittest
import zipfile
from pathlib import Path
from unittest import mock

import _support  # noqa: F401 - puts scripts/ on sys.path
import host
from test_host import HostCase, make_zip

SAMPLES = {
    r"([0-9a-f]{16})": "0123456789abcdef", r"(\d+)": "1", r"([A-Za-z0-9_-]{10,100})": "a" * 20,
    r"([A-Za-z0-9_-]{20,64})": "a" * 24, r"([0-9a-f]{32})": "0" * 32, r"([a-z0-9-]{1,30})": "google",
    r"([^/]+)": "x.css",
}


def sample(pattern: re.Pattern) -> str:
    path = pattern.pattern
    for group, value in SAMPLES.items():
        path = path.replace(group, value)
    path = path.replace("\\", "")
    assert pattern.fullmatch(path), (pattern.pattern, path)
    return path


class RouteGuards(HostCase):
    def setUp(self):
        super().setUp()
        self.other = host.create_tenant(self.app, "Other")
        self.mine = host.create_tenant(self.app, "Mine")
        self.user("member@x.org", "admin", self.mine)
        self.user("stranger@x.org")

    def test_every_route_has_a_sample(self):
        for _method, pattern, need, _fn in host.ROUTES:
            with self.subTest(pattern=pattern.pattern):
                sample(pattern)
                self.assertIn(need, ("anon", "mfa", "user", "admin"))

    def test_signed_out_callers_get_401(self):
        anon = self.client()
        anon.call("GET", "/api/me")
        for method, pattern, need, _fn in host.ROUTES:
            if need == "anon":
                continue
            with self.subTest(method=method, path=pattern.pattern):
                self.assertEqual(anon.call(method, sample(pattern), {})[0], 401)

    def test_site_admin_routes_are_404_for_members(self):
        member = self.client("member@x.org")
        member.call("GET", "/api/me")
        for method, pattern, need, _fn in host.ROUTES:
            if need != "admin":
                continue
            with self.subTest(method=method, path=pattern.pattern):
                self.assertEqual(member.call(method, sample(pattern), {})[0], 404)

    def test_workspace_routes_are_404_outside_the_workspace(self):
        stranger = self.client("stranger@x.org")
        stranger.call("GET", "/api/me")
        admin = self.client("member@x.org")
        admin.call("GET", "/api/me")
        status, made, _ = admin.call("POST", f"/api/tenants/{self.mine}/projects", {"name": "P", "template": "report"})
        self.assertEqual(status, 200, made)
        project = made["id"]  # a real project the stranger is not a member of
        checked = 0
        for method, pattern, need, _fn in host.ROUTES:
            path = sample(pattern)
            if not path.startswith(("/api/tenants/", "/api/projects/")) or need != "user":
                continue
            path = path.replace("0123456789abcdef", self.other if "/tenants/" in path else project, 1)
            with self.subTest(method=method, path=path):
                self.assertEqual(stranger.call(method, path, {})[0], 404)
                checked += 1
        self.assertGreater(checked, 8)


class AccountBranches(HostCase):
    def setUp(self):
        super().setUp()
        self.tenant = host.create_tenant(self.app, "T")
        self.uid = self.user("ada@x.org", "admin", self.tenant)
        self.ada = self.client("ada@x.org")
        self.ada.call("GET", "/api/me")

    def test_recovery_codes_need_two_step_and_the_password(self):
        self.assertEqual(self.ada.call("POST", "/api/account/recovery", {"current": "wrong"})[0], 403)
        self.assertEqual(self.ada.call("POST", "/api/account/recovery", {"current": "password-123"})[0], 409)
        _, data, _ = self.ada.call("POST", "/api/account/totp/start")
        code = host.totp_code(data["secret"], int(time.time() // 30))
        self.assertEqual(self.ada.call("POST", "/api/account/totp/enable", {"code": code})[0], 200)
        status, data, _ = self.ada.call("POST", "/api/account/recovery", {"current": "password-123"})
        self.assertEqual(status, 200)
        self.assertEqual(len(data["codes"]), 10)
        rows = self.app.db.all("SELECT * FROM recovery_codes WHERE user_id = ?", self.uid)
        self.assertEqual(len(rows), 10)
        self.assertIsNotNone(self.app.db.one("SELECT 1 FROM audit WHERE action = 'recovery_codes_renewed'"))

    def test_invite_revoke_is_admin_only(self):
        status, data, _ = self.ada.call("POST", f"/api/tenants/{self.tenant}/invites", {"role": "editor"})
        self.assertEqual(status, 200, data)
        iid = self.app.db.one("SELECT id FROM invites WHERE tenant_id = ?", self.tenant)["id"]
        self.user("ed@x.org", "editor", self.tenant)
        editor = self.client("ed@x.org")
        editor.call("GET", "/api/me")
        self.assertEqual(editor.call("DELETE", f"/api/tenants/{self.tenant}/invites/{iid}")[0], 403)
        self.assertIsNotNone(self.app.db.one("SELECT id FROM invites WHERE id = ?", iid))
        self.assertEqual(self.ada.call("DELETE", f"/api/tenants/{self.tenant}/invites/{iid}")[0], 200)
        self.assertIsNone(self.app.db.one("SELECT id FROM invites WHERE id = ?", iid))

    def test_site_admin_turns_off_two_step(self):
        target = self.user("bob@x.org")
        self.app.db.run("UPDATE users SET totp_secret = 'S' WHERE id = ?", target)
        self.app.db.run("INSERT INTO recovery_codes VALUES (?, ?)", target, "h")
        self.assertEqual(self.ada.call("POST", f"/api/admin/users/{target}/totp-off")[0], 404)  # not a site admin
        self.user("root@x.org", site_admin=1)
        root = self.client("root@x.org")
        root.call("GET", "/api/me")
        self.assertEqual(root.call("POST", f"/api/admin/users/{target}/totp-off")[0], 200)
        self.assertIsNone(self.app.db.one("SELECT totp_secret FROM users WHERE id = ?", target)["totp_secret"])
        self.assertIsNone(self.app.db.one("SELECT * FROM recovery_codes WHERE user_id = ?", target))


class RawBodies(HostCase):
    def setUp(self):
        super().setUp()
        self.user("ada@x.org")
        self.ada = self.client("ada@x.org")
        self.ada.call("GET", "/api/me")

    def test_json_body_errors(self):
        path = "/api/account"
        self.assertEqual(self.ada.call("POST", path, raw=b"{}", ctype="text/plain")[0], 415)
        self.assertEqual(self.ada.call("POST", path, raw=b"{nope")[0], 400)
        self.assertEqual(self.ada.call("POST", path, raw=b"[1]")[0], 400)
        self.assertEqual(self.ada.call("POST", path, raw=b"{}", headers={"Transfer-Encoding": "chunked"})[0], 411)
        self.assertIn(self.ada.call("POST", path, raw=b"{}", headers={"Content-Length": "x"})[0], (400, 413))

    def test_project_proxy_edges(self):
        self.assertEqual(self.ada.call("GET", "/p/nothex/")[0], 404)
        self.assertEqual(self.ada.call("GET", "/p/0123456789abcdef/")[0], 404)  # not a member: like missing


class ClientIp(HostCase):
    def extra_config(self):
        return {"trust_proxy": True}

    def test_last_forwarded_address_is_used(self):
        seen = []
        real = host.Handler.client_ip

        def spy(handler):
            seen.append(real(handler))
            return seen[-1]

        with mock.patch.object(host.Handler, "client_ip", spy):
            self.client().call("GET", "/api/me", headers={"X-Forwarded-For": "1.1.1.1, 2.2.2.2"})
            self.client().call("GET", "/api/me")
        self.assertEqual(seen, ["2.2.2.2", "127.0.0.1"])


class Zips(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.base = Path(tmp.name)

    def members(self, data, limit=10 ** 6):
        return host.zip_members(zipfile.ZipFile(io.BytesIO(data)), limit)

    def test_too_many_entries_and_encrypted(self):
        with mock.patch.object(host, "MAX_ZIP_ENTRIES", 2), self.assertRaises(host.HttpError) as ctx:
            self.members(make_zip({"main.tex": "x", "a.tex": "y", "b.tex": "z"}))
        self.assertEqual(ctx.exception.status, 413)
        data = bytearray(make_zip({"main.tex": "x"}))
        archive = zipfile.ZipFile(io.BytesIO(bytes(data)))
        archive.infolist()[0].flag_bits |= 0x1
        with self.assertRaises(host.HttpError) as ctx:
            host.zip_members(archive, 10 ** 6)
        self.assertEqual(ctx.exception.status, 400)

    def test_directory_entries_are_skipped(self):
        buf = io.BytesIO()
        with zipfile.ZipFile(buf, "w") as archive:
            archive.writestr("figs/", "")
            archive.writestr("main.tex", "x")
        files, skipped = self.members(buf.getvalue())
        self.assertEqual([rel for _, rel in files], ["main.tex"])

    def test_real_bytes_and_escapes_are_checked_again_while_writing(self):
        data = make_zip({"main.tex": "x" * 100})
        archive = zipfile.ZipFile(io.BytesIO(data))
        info = archive.infolist()[0]
        with mock.patch.object(host, "zip_members", return_value=([(info, "main.tex")], [])), \
                self.assertRaises(host.HttpError) as ctx:
            host.unpack_zip(data, self.base / "t", 10)
        self.assertEqual(ctx.exception.status, 413)
        self.assertFalse((self.base / "t").exists())
        with mock.patch.object(host, "zip_members", return_value=([(info, "../evil.tex")], [])), \
                self.assertRaises(host.HttpError) as ctx:
            host.unpack_zip(data, self.base / "u", 10 ** 6)
        self.assertEqual(ctx.exception.status, 400)
        self.assertFalse((self.base / "u").exists())
        self.assertFalse((self.base / "evil.tex").exists())

    def test_not_a_zip(self):
        with self.assertRaises(host.HttpError) as ctx:
            host.unpack_zip(b"plain", self.base / "t", 10)
        self.assertEqual(ctx.exception.status, 400)


class Toml(unittest.TestCase):
    def test_errors_name_the_file(self):
        with tempfile.TemporaryDirectory() as tmp:
            bad = Path(tmp) / "config.toml"
            bad.write_text("x = = 1", encoding="utf-8")
            for path in (bad, Path(tmp) / "missing.toml"):
                with self.assertRaises(host.ConfigError) as ctx:
                    host.read_toml(path)
                self.assertIn(path.name, str(ctx.exception))
            bad.write_text("x = 1\n", encoding="utf-8")
            self.assertEqual(host.read_toml(bad), {"x": 1})


if __name__ == "__main__":
    unittest.main()
