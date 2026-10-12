"""host_mcp.py: the OAuth error branches (form bodies, redirect URIs, registration cap, codes, refresh tokens,
consent selections) and the /mcp body checks."""

from __future__ import annotations

import base64
import unittest
from unittest import mock
from urllib.parse import urlencode

import _support  # noqa: F401 - puts scripts/ on sys.path
import host_mcp
import test_host as th


class McpErrors(th.HostCase):
    extra_config = th.Mcp.extra_config

    def setUp(self):
        super().setUp()
        host_mcp.THROTTLES.clear()
        self.addCleanup(host_mcp.THROTTLES.clear)
        self.tenant = th.host.create_tenant(self.app, "Team")
        self.other = th.host.create_tenant(self.app, "Other")
        self.user("ed@x.org", "editor", self.tenant)
        self.user("ot@x.org", "admin", self.other)
        self.pid = th.Workspaces.make_project(self, self.client("ed@x.org"), self.tenant, "Doc", "article")
        self.opid = th.Workspaces.make_project(self, self.client("ot@x.org"), self.other, "Secret", "article")
        self.resource = self.origin + "/mcp"
    anon, register, authorize, consent, exchange, connect, mcp = (
        th.Mcp.anon, th.Mcp.register, th.Mcp.authorize, th.Mcp.consent, th.Mcp.exchange, th.Mcp.connect, th.Mcp.mcp)

    def token(self, form, headers=None):
        return self.anon("POST", "/oauth/token", form=form, headers=headers)

    def test_form_body_rules(self):
        status, data, _ = self.anon("POST", "/oauth/token", {"grant_type": "x"})  # JSON, not a form
        self.assertEqual((status, data["error"]), (400, "invalid_request"))
        raw = b"grant_type=a&grant_type=b"
        status, data, _ = th.Client(self).call("POST", "/oauth/token", raw=raw, origin=False,
                                               ctype="application/x-www-form-urlencoded")
        self.assertEqual((status, data["error"]), (400, "invalid_request"))
        status, data, _ = self.token({"grant_type": "refresh_token"}, {"Authorization": "Basic %%%"})
        self.assertEqual((status, data["error"]), (401, "invalid_client"))
        cid = self.register()
        basic = base64.b64encode(f"{cid}:ignored".encode()).decode()
        status, data, _ = self.token({"grant_type": "refresh_token", "refresh_token": "nope"},
                                     {"Authorization": "Basic " + basic})
        self.assertEqual((status, data["error"]), (400, "invalid_grant"))  # client known through Basic

    def test_unknown_and_wrong_method_routes(self):
        self.assertEqual(self.anon("GET", "/oauth/token")[0], 405)
        self.assertEqual(self.anon("GET", "/oauth/nothing")[0], 404)

    def test_registration_rules_and_cap(self):
        status, data, _ = self.anon("POST", "/oauth/register", {"client_name": "x", "redirect_uris": [th.CALLBACK],
                                                                "response_types": ["token"]})
        self.assertEqual((status, data["error"]), (400, "invalid_client_metadata"))
        status, data, _ = self.anon("POST", "/oauth/register", {"client_name": "x", "redirect_uris": ["ftp://x"]})
        self.assertEqual((status, data["error"]), (400, "invalid_redirect_uri"))
        status, data, _ = th.Client(self).call("POST", "/oauth/register", raw=b"[1]", origin=False)
        self.assertEqual((status, data["error"]), (400, "invalid_client_metadata"))
        self.register()
        with mock.patch.dict(self.app.config["mcp"], {"max_clients": 1}):
            status, data, _ = self.anon("POST", "/oauth/register", {"client_name": "y",
                                                                    "redirect_uris": [th.CALLBACK]})
        self.assertEqual((status, data["error"]), (503, "temporarily_unavailable"))

    def test_authorize_refuses_malformed_requests_on_our_page(self):
        cid = self.register()
        query = urlencode([("client_id", cid), ("client_id", cid)])
        status, _, res = self.anon("GET", "/oauth/authorize?" + query)
        self.assertEqual(status, 302)
        self.assertTrue(res.getheader("Location").startswith("/#oauth-error="))
        location = self.authorize(cid, state="s" * 1001)
        self.assertTrue(location.startswith("/#oauth-error="), location)  # never redirected before consent
        self.assertIn("invalid_request", location)
        with mock.patch.object(th.host.Throttle, "full", return_value=True):
            location = self.authorize(cid)
        self.assertIn("temporarily_unavailable", location)
        self.assertFalse(self.app.db.one("SELECT 1 FROM oauth_requests"))

    def test_codes_and_refresh_tokens(self):
        cid = self.register()
        status, data, _ = self.exchange(cid, "unknown-code")
        self.assertEqual((status, data["error"]), (400, "invalid_grant"))
        cid, tokens = self.connect()
        other = self.register(name="Other")
        status, data, _ = self.token({"grant_type": "refresh_token", "refresh_token": tokens["refresh_token"],
                                      "client_id": other})
        self.assertEqual((status, data["error"]), (400, "invalid_grant"))  # another client's refresh token
        self.app.db.run("UPDATE oauth_tokens SET used = NULL")  # undo the failed attempt's use for the next check
        self.app.db.run("UPDATE users SET disabled = 1 WHERE email = 'ed@x.org'")
        status, data, _ = self.token({"grant_type": "refresh_token", "refresh_token": tokens["refresh_token"],
                                      "client_id": cid})
        self.assertEqual((status, data["error"]), (400, "invalid_grant"))
        self.assertIn("disabled", data["error_description"])

    def test_disabled_account_cannot_redeem_a_code(self):
        cid = self.register()
        query = self.consent(self.authorize(cid))
        self.app.db.run("UPDATE users SET disabled = 1 WHERE email = 'ed@x.org'")
        status, data, _ = self.exchange(cid, query["code"][0])
        self.assertEqual((status, data["error"]), (400, "invalid_grant"))
        self.assertIn("disabled", data["error_description"])

    def test_token_endpoint_is_throttled(self):
        with mock.patch.object(th.host.Throttle, "full", return_value=True):
            status, data, _ = self.token({"grant_type": "refresh_token"})
        self.assertEqual((status, data["error"]), (429, "slow_down"))

    def test_consent_selection_errors_and_reuse(self):
        cid = self.register()
        rid = self.authorize(cid).split("#oauth=", 1)[1]
        browser = self.client("ed@x.org")
        path = f"/api/oauth/requests/{rid}"
        self.assertEqual(browser.call("POST", path, {"allow": True, "tenants": "x"})[0], 400)
        self.assertEqual(browser.call("POST", path, {"allow": True, "tenants": [], "projects": []})[0], 400)
        self.assertEqual(browser.call("POST", path, {"allow": True, "projects": [self.opid]})[0], 404)
        with mock.patch.object(self.app.db, "change", return_value=0):
            self.assertEqual(browser.call("POST", path, {"allow": True, "projects": [self.pid]})[0], 410)
            self.assertEqual(browser.call("POST", path, {"allow": False})[0], 410)

    def test_oldest_connection_goes_at_the_cap(self):
        with mock.patch.object(host_mcp, "MAX_GRANTS_PER_USER", 1):
            self.connect()
            self.connect()
        self.assertEqual(self.app.db.one("SELECT COUNT(*) AS n FROM oauth_grants")["n"], 1)

    def test_mcp_body_checks(self):
        _cid, tokens = self.connect()
        hdrs = {"Authorization": f"Bearer {tokens['access_token']}", "Accept": "application/json, text/event-stream"}
        status, _, _ = th.Client(self).call("POST", "/mcp", raw=b"{}", headers=hdrs, origin=False,
                                            ctype="text/plain")
        self.assertEqual(status, 415)
        status, data, _ = th.Client(self).call("POST", "/mcp", raw=b"{nope", headers=hdrs, origin=False)
        self.assertEqual((status, data["error"]["code"]), (400, -32700))


class RedirectUris(unittest.TestCase):
    def test_kinds_labels_and_matching(self):
        with self.assertRaises(ValueError):
            host_mcp.redirect_kind("https://" + "a" * 70 + ".example/cb")  # label too long for IDNA
        self.assertEqual(host_mcp.redirect_label("http://127.0.0.1:8123/cb"), "an app on your computer (http://127.0.0.1)")
        self.assertEqual(host_mcp.redirect_label("com.example.app:/cb"),
                         "the app that handles com.example.app:// links on your computer")
        self.assertEqual(host_mcp.redirect_label("https://bücher.example/cb"), "xn--bcher-kva.example")
        self.assertTrue(host_mcp.redirect_matches("http://127.0.0.1:1/cb", "http://127.0.0.1:2/cb"))
        self.assertFalse(host_mcp.redirect_matches("http://127.0.0.1:1/cb", "http://127.0.0.1:99999/cb"))
        self.assertFalse(host_mcp.redirect_matches("http://127.0.0.1/cb", "http://127.0.0.1/other"))
        self.assertFalse(host_mcp.redirect_matches("https://a.example/cb", "https://a.example:444/cb"))
        self.assertFalse(host_mcp.redirect_matches("http://127.0.0.1/cb", "ftp://x"))


if __name__ == "__main__":
    unittest.main()
