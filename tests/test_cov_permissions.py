"""serve.check_permission, route by route: role, document scope, rate limit and the default deny.

Every literal path the function names must appear in ROUTES below, so a new endpoint fails here until it has a row.
"""

from __future__ import annotations

import inspect
import re
import unittest
from unittest import mock

import _support  # noqa: F401 - puts scripts/ on sys.path
import serve

DOC = "thesis"
# (method, path, need, rate limited). need: "any" (every role, scoped), "review", "edit" or "admin".
ROUTES = [
    ("POST", "/rebuild", "edit", True), ("POST", "/api/build/cancel", "edit", True),
    ("POST", "/api/fs", "edit", True), ("POST", "/api/replace", "edit", True),
    ("POST", "/api/symbols", "any", True), ("POST", "/api/rename", "edit", True),
    ("POST", "/api/typeset", "edit", True), ("POST", "/api/upload", "edit", True),
    ("POST", "/api/grammar", "edit", True), ("POST", "/api/ai", "edit", True),
    ("POST", "/api/bib", "any", False), ("POST", "/api/bib/edit", "edit", False),
    ("POST", "/api/bib/tidy", "edit", False), ("POST", "/api/bib/lookup", "edit", True),
    ("POST", "/api/bib/import", "edit", True), ("POST", "/api/bib/search", "edit", True),
    ("POST", "/api/bib/verify", "edit", True), ("POST", "/api/format", "edit", True),
    ("POST", "/api/zotero/apply", "edit", True), ("POST", "/api/lock", "admin", False),
    ("POST", "/api/linked", "edit", True), ("POST", "/api/latexdiff", "edit", True),
    ("POST", "/api/access/request", "any", True), ("POST", "/api/history/clean", "admin", True),
    ("POST", "/api/history/pending", "edit", True), ("POST", "/api/review", "review", True),
    ("POST", "/api/history/label", "edit", True), ("POST", "/api/history/restore", "edit", True),
    ("POST", "/api/mcp", "any", True), ("POST", "/api/focus", "edit", True),
    ("POST", "/api/review-pdf", "edit", True), ("PUT", "/api/file", "edit", True),
]
# Paths with their own tests below (gateway-only, preview, GET rules).
SPECIAL = {"/", "/ui/", "/api/config", "/api/health", "/ws", "/api/poll", "/api/access/status", "/api/ai",
           "/api/zotero", "/docx/", "/arxiv/", "/html/", "/pdf/", "/log/", "/api/history/pending",
           "/api/history", "/api/search", "/forward", "/api/send", "/api/zotero/preview", "/api/zotero/compare",
           "/api/preview", "/api/v1/compile"}
ROLES_BELOW = {"any": (), "review": ("view",), "edit": ("view", "review"), "admin": ("view", "review", "edit")}


def q(doc=DOC, **extra):
    query = {"doc": [doc]} if doc else {}
    query.update({key: [value] for key, value in extra.items()})
    return query


class Base(unittest.TestCase):
    def setUp(self):
        patches = [mock.patch.dict(serve.SHARE, {"doc": DOC}), mock.patch.dict(serve.GATEWAY, {"secret": b""}),
                   mock.patch.object(serve, "RATE", {})]
        for patch in patches:
            patch.start()
            self.addCleanup(patch.stop)

    def status(self, role, method, path, query, admin=False):
        try:
            serve.check_permission(role, method, path, query, admin=admin)
        except serve.ApiError as exc:
            return exc.status
        return None


class EveryRoute(Base):
    def test_table_names_every_route_of_check_permission(self):
        source = inspect.getsource(serve.check_permission)
        named = set(re.findall(r'"(/[^"]*)"', source))
        listed = {path for _m, path, _n, _r in ROUTES} | SPECIAL
        self.assertEqual(sorted(named - listed), [], "add a ROUTES row for each new endpoint")

    def test_owner_may_do_anything(self):
        self.assertIsNone(self.status("owner", "POST", "/api/git/commit", {}))

    def test_role_scope_and_rate_limit_of_each_route(self):
        for method, path, need, limited in ROUTES:
            role = "review" if need == "review" else "view" if need == "any" else "edit"
            admin = need == "admin"
            with self.subTest(path=path):
                for low in ROLES_BELOW[need]:
                    self.assertEqual(self.status(low, method, path, q(), admin=False), 403, low)
                self.assertEqual(self.status(role, method, path, q("other"), admin=admin), 403, "other document")
                self.assertIsNone(self.status(role, method, path, q(), admin=admin))
                if limited:
                    with mock.patch.object(serve, "rate_ok", return_value=False):
                        self.assertEqual(self.status(role, method, path, q(), admin=admin), 429)

    def test_build_config_files_are_refused_to_editors(self):
        for name in ("build.toml", ".latexmkrc", "sub/latexmkrc"):
            with self.subTest(name=name):
                self.assertEqual(self.status("edit", "PUT", "/api/file", q(path=name)), 403)
                self.assertEqual(self.status("edit", "POST", "/api/upload", q(path=name)), 403)
                self.assertEqual(self.status("edit", "POST", "/api/fs", q(path="a.tex", to=name)), 403)
                self.assertEqual(self.status("edit", "POST", "/api/fs", q(path=name)), 403)
        self.assertIsNone(self.status("edit", "PUT", "/api/file", q(path="main.tex")))

    def test_unknown_and_owner_only_routes_are_denied(self):
        for method, path in [("POST", "/api/git/commit"), ("POST", "/api/docx"), ("POST", "/api/arxiv"),
                             ("POST", "/api/html"), ("POST", "/api/ai/settings"), ("POST", "/api/zotero/settings"),
                             ("POST", "/api/library"), ("POST", "/api/share/links"), ("DELETE", "/api/file"),
                             ("GET", "/api/git"), ("GET", "/api/ai/settings"), ("GET", "/api/library"),
                             ("PUT", "/api/other"), ("POST", "/api/v1/compile")]:
            with self.subTest(method=method, path=path):
                self.assertEqual(self.status("edit", method, path, q(), admin=True), 403)


class GetRules(Base):
    def test_public_paths(self):
        for path in ("/", "/ui/app.js", "/api/config", "/api/health", "/ws", "/api/poll", "/api/access/status",
                     "/api/ai", "/api/zotero"):
            self.assertIsNone(self.status("view", "GET", path, {}), path)
        with mock.patch.dict(serve.GATEWAY, {"secret": b"s"}):  # workers never answer Zotero info
            self.assertEqual(self.status("view", "GET", "/api/zotero", {}), 403)

    def test_exports_are_owner_only(self):
        for path in ("/docx/thesis", "/arxiv/thesis", "/html/thesis/index.html"):
            self.assertEqual(self.status("edit", "GET", path, q()), 403, path)

    def test_pdf_and_log_are_scoped_by_path(self):
        self.assertIsNone(self.status("view", "GET", "/pdf/thesis", {}))
        self.assertIsNone(self.status("view", "GET", "/log/thesis", {}))
        self.assertEqual(self.status("view", "GET", "/pdf/other", {}), 403)
        self.assertEqual(self.status("view", "GET", "/log/other", {}), 403)

    def test_read_api_scoped_and_limited(self):
        for path in sorted(serve.READ_API):
            with self.subTest(path=path):
                self.assertIsNone(self.status("view", "GET", path, q()))
                self.assertEqual(self.status("view", "GET", path, q("other")), 403)
        with mock.patch.object(serve, "rate_ok", return_value=False):
            for path in ("/api/history/pending", "/api/history", "/api/history/diff", "/api/search"):
                self.assertEqual(self.status("view", "GET", path, q()), 429, path)
            self.assertIsNone(self.status("view", "GET", "/api/files", q()))  # plain reads are not limited

    def test_forward_only_quiet(self):
        self.assertIsNone(self.status("view", "GET", "/forward", {"quiet": ["1"]}))
        self.assertEqual(self.status("view", "GET", "/forward", {}), 403)
        self.assertEqual(self.status("view", "GET", "/api/other", q()), 403)

    def test_send_is_open_to_every_role(self):
        self.assertIsNone(self.status("view", "POST", "/api/send", {}))


class SpecialPosts(Base):
    def test_zotero_preview_needs_owner_consent_and_sandbox(self):
        info = {"share_editors": True}
        with mock.patch.object(serve.zotero, "info", return_value=info), \
                mock.patch.object(serve, "ai_sandboxed", return_value=True):
            self.assertIsNone(self.status("edit", "POST", "/api/zotero/preview", q()))
            self.assertEqual(self.status("view", "POST", "/api/zotero/preview", q()), 403)
            with mock.patch.object(serve, "rate_ok", return_value=False):
                self.assertEqual(self.status("edit", "POST", "/api/zotero/preview", q()), 429)
        with mock.patch.object(serve.zotero, "info", return_value=info), \
                mock.patch.object(serve, "ai_sandboxed", return_value=False):
            self.assertEqual(self.status("edit", "POST", "/api/zotero/preview", q()), 403)
        with mock.patch.object(serve.zotero, "info", return_value={"share_editors": False}), \
                mock.patch.object(serve, "ai_sandboxed", return_value=True):
            self.assertEqual(self.status("edit", "POST", "/api/zotero/preview", q()), 403)
        with mock.patch.dict(serve.GATEWAY, {"secret": b"s"}):  # workers never fetch from Zotero
            self.assertEqual(self.status("edit", "POST", "/api/zotero/preview", q()), 403)

    def test_zotero_compare_only_behind_gateway(self):
        self.assertEqual(self.status("edit", "POST", "/api/zotero/compare", q()), 403)
        with mock.patch.dict(serve.GATEWAY, {"secret": b"s"}):
            self.assertIsNone(self.status("edit", "POST", "/api/zotero/compare", q()))
            self.assertEqual(self.status("view", "POST", "/api/zotero/compare", q()), 403)
            with mock.patch.object(serve, "rate_ok", return_value=False):
                self.assertEqual(self.status("edit", "POST", "/api/zotero/compare", q()), 429)

    def test_compile_only_behind_gateway_for_editors(self):
        self.assertEqual(self.status("edit", "POST", "/api/v1/compile", {}), 403)
        with mock.patch.dict(serve.GATEWAY, {"secret": b"s"}):
            self.assertIsNone(self.status("edit", "POST", "/api/v1/compile", {}))
            self.assertEqual(self.status("review", "POST", "/api/v1/compile", {}), 403)
            with mock.patch.object(serve, "rate_ok", return_value=False):
                self.assertEqual(self.status("edit", "POST", "/api/v1/compile", {}), 429)

    def test_preview_counts_a_cold_start_as_a_build(self):
        self.assertEqual(self.status("view", "POST", "/api/preview", q()), 403)
        self.assertEqual(self.status("edit", "POST", "/api/preview", q("other")), 403)
        with mock.patch.dict(serve.DOCS, {DOC: object()}), \
                mock.patch.object(serve.preview, "has_snapshot", side_effect=ValueError):
            self.assertIsNone(self.status("edit", "POST", "/api/preview", q()))
            calls = []
            with mock.patch.object(serve, "rate_ok", side_effect=lambda key, *a: calls.append(key) or key != "rebuild"):
                self.assertEqual(self.status("edit", "POST", "/api/preview", q()), 429)
            self.assertEqual(calls, ["preview", "rebuild"])
        with mock.patch.dict(serve.DOCS, {DOC: object()}), \
                mock.patch.object(serve.preview, "has_snapshot", return_value=True), \
                mock.patch.object(serve, "rate_ok", side_effect=lambda key, *a: key != "rebuild"):
            self.assertIsNone(self.status("edit", "POST", "/api/preview", q()))  # warm: no build to count
        with mock.patch.object(serve, "rate_ok", return_value=False):
            self.assertEqual(self.status("edit", "POST", "/api/preview", q()), 429)

    def test_review_role_messages(self):
        self.assertEqual(self.status("review", "POST", "/rebuild", q()), 403)
        try:
            serve.check_permission("review", "POST", "/rebuild", q())
        except serve.ApiError as exc:
            self.assertIn("review link", str(exc))


class RateLimit(unittest.TestCase):
    def test_window_slides(self):
        clock = mock.patch.object(serve.time, "monotonic", side_effect=[0, 1, 2, 70])
        with mock.patch.object(serve, "RATE", {}), clock:
            self.assertTrue(serve.rate_ok("k", 2, 60.0))
            self.assertTrue(serve.rate_ok("k", 2, 60.0))
            self.assertFalse(serve.rate_ok("k", 2, 60.0))
            self.assertTrue(serve.rate_ok("k", 2, 60.0))  # both old hits fell out of the window


if __name__ == "__main__":
    unittest.main()
