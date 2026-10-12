"""gitops.py and serve.py's Git panel: a temp repository with a local bare remote; owner only; rooms merge pulls."""

from __future__ import annotations

import os
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import _support  # noqa: F401 - puts scripts/ on sys.path
import gitops
import serve
from test_serve import ServerCase, SharedState

HAVE_GIT = shutil.which("git") is not None
IDENTITY = {"GIT_AUTHOR_NAME": "Ada", "GIT_AUTHOR_EMAIL": "ada@example.org", "GIT_COMMITTER_NAME": "Ada",
            "GIT_COMMITTER_EMAIL": "ada@example.org", "GIT_CONFIG_GLOBAL": os.devnull, "GIT_CONFIG_NOSYSTEM": "1"}


def git(cwd, *args):
    return subprocess.run(["git", *args], cwd=str(cwd), check=True, capture_output=True, text=True).stdout


def make_repos(base: Path, doc: Path) -> Path:
    """doc becomes a clone of a bare remote that holds one commit (main.tex); returns the bare remote."""
    remote = base / "remote.git"
    git(base, "init", "-q", "--bare", "-b", "main", str(remote))
    seed = base / "seed"
    git(base, "clone", "-q", str(remote), str(seed))
    (seed / "main.tex").write_text("one\ntwo\nthree\n", encoding="utf-8")
    git(seed, "add", "main.tex")
    git(seed, "commit", "-q", "-m", "first")
    git(seed, "push", "-q", "origin", "HEAD:refs/heads/main")
    if doc.exists():
        shutil.rmtree(doc)
    git(base, "clone", "-q", str(remote), str(doc))
    return remote


@unittest.skipUnless(HAVE_GIT, "git is not installed")
class GitOps(unittest.TestCase):
    def setUp(self):
        mock.patch.dict(os.environ, IDENTITY).start()
        self.addCleanup(mock.patch.stopall)
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.base = Path(tmp.name).resolve()
        self.doc = self.base / "doc"
        self.remote = make_repos(self.base, self.doc)

    def upstream_commit(self, text):
        seed = self.base / "seed"
        git(seed, "pull", "-q", "--no-rebase")
        (seed / "main.tex").write_text(text, encoding="utf-8")
        git(seed, "commit", "-q", "-am", "upstream")
        git(seed, "push", "-q", "origin", "HEAD:refs/heads/main")

    def test_not_a_repository(self):
        plain = self.base / "plain"
        plain.mkdir()
        self.assertEqual(gitops.status(plain), {"repo": False})
        with self.assertRaises(gitops.GitError) as ctx:
            gitops.log(plain)
        self.assertEqual(ctx.exception.status, 404)

    def test_status_stage_diff_commit_log(self):
        (self.doc / "main.tex").write_text("one\nTWO\nthree\n", encoding="utf-8")
        (self.doc / "new file.tex").write_bytes(b"new\n")  # no CRLF on Windows
        info = gitops.status(self.doc)
        self.assertEqual((info["branch"], info["upstream"], info["remotes"]), ("main", "origin/main", ["origin"]))
        self.assertEqual({f["path"]: f["worktree"] for f in info["files"]}, {"main.tex": "M", "new file.tex": "?"})
        self.assertIn("+TWO", gitops.diff(self.doc, "main.tex", False)["diff"])
        self.assertEqual(gitops.diff(self.doc, "new file.tex", False), {"path": "new file.tex", "untracked": True,
                                                                        "text": "new\n"})
        gitops.stage(self.doc, ["main.tex", "new file.tex"])
        self.assertIn("+TWO", gitops.diff(self.doc, "main.tex", True)["diff"])
        gitops.unstage(self.doc, ["new file.tex"])
        self.assertEqual({f["path"]: f["index"] for f in gitops.status(self.doc)["files"]},
                         {"main.tex": "M", "new file.tex": "?"})
        with self.assertRaises(gitops.GitError):
            gitops.commit(self.doc, "  ")
        head = gitops.commit(self.doc, "-n looks like an option\n\nbody")["head"]
        commits = gitops.log(self.doc)["commits"]
        self.assertEqual((commits[0]["id"], commits[0]["subject"], commits[0]["author"]),
                         (head, "-n looks like an option", "Ada"))
        self.assertEqual(gitops.status(self.doc)["ahead"], 1)

    def test_requests_cannot_smuggle_options_or_other_paths(self):
        (self.doc / "main.tex").write_text("changed\n", encoding="utf-8")
        for bad in (["--all"], ["*"], ["../remote.git"], [".git/config"], "main.tex", [], [1]):
            with self.subTest(paths=bad), self.assertRaises(gitops.GitError):
                gitops.stage(self.doc, bad)
        for bad in ("-f", "--orphan=x", "a..b", "", None, "x" * 300):
            with self.subTest(name=bad), self.assertRaises(gitops.GitError):
                gitops.create(self.doc, bad)
        with self.assertRaises(gitops.GitError) as ctx:
            gitops.switch(self.doc, "--detach")
        self.assertEqual(ctx.exception.status, 404)
        self.assertTrue(gitops.push(self.doc, "--receive-pack=evil")["ok"])  # has an upstream: the remote is unused
        gitops.create(self.doc, "feature")
        with self.assertRaises(gitops.GitError):
            gitops.push(self.doc, "--receive-pack=evil")
        with self.assertRaises(gitops.GitError):
            gitops.operate(self.doc, {"op": "gc"})

    def test_branches_switch_create_push_pull(self):
        gitops.create(self.doc, "draft")
        (self.doc / "main.tex").write_text("draft\n", encoding="utf-8")
        gitops.stage(self.doc, ["main.tex"])
        gitops.commit(self.doc, "draft")
        self.assertTrue(gitops.push(self.doc)["ok"])  # one remote: first push sets the upstream
        listed = gitops.branches(self.doc)
        self.assertIn("origin/draft", listed["remote"])
        self.assertEqual([b["name"] for b in listed["local"] if b["current"]], ["draft"])
        gitops.switch(self.doc, "main")
        self.assertEqual((self.doc / "main.tex").read_text(encoding="utf-8"), "one\ntwo\nthree\n")
        self.upstream_commit("one\ntwo\nthree\nfour\n")
        result = gitops.pull(self.doc)
        self.assertEqual((result["ok"], result["conflicts"]), (True, []))
        self.assertEqual((self.doc / "main.tex").read_text(encoding="utf-8"), "one\ntwo\nthree\nfour\n")

    def test_unborn_branch_is_listed(self):
        fresh = self.base / "fresh"
        git(self.base, "init", "-q", "-b", "start", str(fresh))
        listed = gitops.branches(fresh)
        self.assertEqual(listed["local"], [{"name": "start", "current": True, "upstream": None}])
        self.assertEqual(gitops.status(fresh)["branch"], "start")

    def test_conflicts_are_shown_and_never_resolved(self):
        self.upstream_commit("one\nREMOTE\nthree\n")
        (self.doc / "main.tex").write_text("one\nLOCAL\nthree\n", encoding="utf-8")
        gitops.stage(self.doc, ["main.tex"])
        gitops.commit(self.doc, "local")
        result = gitops.pull(self.doc)
        self.assertEqual((result["ok"], result["conflicts"]), (False, ["main.tex"]))
        info = gitops.status(self.doc)
        self.assertTrue(info["merging"])
        self.assertTrue(any(f.get("conflict") for f in info["files"]))
        self.assertIn("<<<<<<<", (self.doc / "main.tex").read_text(encoding="utf-8"))
        gitops.abort_merge(self.doc)
        self.assertEqual((self.doc / "main.tex").read_text(encoding="utf-8"), "one\nLOCAL\nthree\n")

    def test_timeouts_are_reported(self):
        with mock.patch.object(gitops.subprocess, "run",
                               side_effect=subprocess.TimeoutExpired("git", 1)), \
                self.assertRaises(gitops.GitError) as ctx:
            gitops.status(self.doc)
        self.assertEqual(ctx.exception.status, 504)


@unittest.skipUnless(HAVE_GIT, "git is not installed")
class GitPanelApi(SharedState, ServerCase):
    def setUp(self):
        SharedState.setUp(self)
        ServerCase.setUp(self)
        mock.patch.dict(os.environ, IDENTITY).start()
        make_repos(self.base, self.root)
        mock.patch.dict(serve.DOCS, {"demo": self.root / "main.tex"}, clear=True).start()

    def call(self, method, path, body=None, token=None):
        hdrs = {"Cookie": f"{serve.cookie_name()}={token}"} if token else {}
        return self.request(method, f"{path}{'&' if '?' in path else '?'}doc=demo", body, hdrs)

    def test_owner_uses_the_panel(self):
        (self.root / "main.tex").write_text("changed\n", encoding="utf-8")
        status, info = self.call("GET", "/api/git")
        self.assertEqual((status, info["repo"], info["branch"]), (200, True, "main"))
        self.assertEqual(self.call("GET", "/api/git/diff?path=main.tex")[0], 200)
        self.assertEqual(self.call("POST", "/api/git", {"op": "stage", "paths": ["main.tex"]})[0], 200)
        self.assertEqual(self.call("POST", "/api/git", {"op": "commit", "message": "edit"})[0], 200)
        head = self.call("GET", "/api/git/log")[1]["commits"][0]
        self.assertEqual(head["subject"], "edit")
        status, shown = self.call("GET", f"/api/git/show?id={head['id']}")
        self.assertEqual(status, 200)
        self.assertIn("+changed", shown["diff"])
        self.assertEqual(self.call("GET", "/api/git/show?id=HEAD~1")[0], 404)  # only ids the log lists
        self.assertEqual(self.call("GET", "/api/git/branches")[1]["local"][0]["name"], "main")
        status, body = self.call("POST", "/api/git", {"op": "stage", "paths": ["--all"]})
        self.assertEqual(status, 400, body)
        self.assertEqual(self.call("POST", "/api/git", {"op": "commit", "message": "x"},
                                   )[0], 409)  # nothing staged: git's own message

    def test_share_links_never_reach_git(self):
        tokens = self.share_on()
        for role in [r for r in tokens if r != "owner"]:
            for method, path, body in (("GET", "/api/git", None), ("GET", "/api/git/log", None),
                                       ("GET", "/api/git/diff?path=main.tex", None), ("GET", "/api/git/branches", None),
                                       ("GET", "/api/git/show?id=0", None),
                                       ("POST", "/api/git", {"op": "pull"}), ("POST", "/api/git", {"op": "push"})):
                with self.subTest(role=role, path=path, method=method):
                    self.assertEqual(self.call(method, path, body, tokens[role])[0], 403)

    def test_gateway_workers_refuse(self):
        with self.assertRaises(serve.ApiError) as ctx, mock.patch.dict(serve.GATEWAY, {"secret": b"x"}):
            serve.git_panel("demo", "/api/git", {})
        self.assertEqual(ctx.exception.status, 404)
        for role in ("edit", "review", "view"):
            with self.subTest(role=role), self.assertRaises(serve.ApiError):
                serve.check_permission(role, "GET", "/api/git", {"doc": ["demo"]})


@unittest.skipUnless(HAVE_GIT, "git is not installed")
class PullIntoOpenRoom(SharedState):
    """A pull rewrites a file open in a co-editing room: the existing disk-change merge path picks it up."""

    def setUp(self):
        super().setUp()
        mock.patch.dict(os.environ, IDENTITY).start()
        self.addCleanup(mock.patch.stopall)
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.base = Path(tmp.name).resolve()
        self.doc = self.base / "doc"
        make_repos(self.base, self.doc)
        mock.patch.dict(serve.DOCS, {"d": self.doc / "main.tex"}, clear=True).start()
        serve.SHARE["doc"] = "d"

    def test_leader_save_after_pull_conflicts_and_a_new_leader_sees_drift(self):
        loaded = serve.read_text_file(self.doc, "main.tex")
        serve.bind_client("lead", "owner")
        joined = serve.handle_client_message({"type": "y-join", "data": {"doc": "d", "path": "main.tex", "aid": 1}},
                                             "lead", "owner")
        self.assertEqual(joined["data"]["leader"], "lead")
        GitOps.upstream_commit(self, "one\ntwo\nthree\nfrom upstream\n")
        self.assertTrue(gitops.operate(self.doc, {"op": "pull"}, serve.WRITE_LOCK)["ok"])
        # The leader's save still carries the version it loaded: refused, so the editor merges (collab.mergeDisk).
        with self.assertRaises(serve.ApiError) as ctx:
            serve.write_text_file(self.doc, "main.tex", "one\ntwo\nthree\n", loaded["version"])
        self.assertEqual(ctx.exception.status, 409)
        room = serve.ROOMS[serve.room_id("d", "main.tex")]
        drift = serve.room_drift(room)  # a leader taking over merges room["saved"] -> disk first
        self.assertEqual((drift["stale"], drift["base"]), (True, "one\ntwo\nthree\n"))
        self.assertTrue(serve.read_text_file(self.doc, "main.tex")["text"].endswith("from upstream\n"))

    def test_pull_waits_for_a_save_in_progress(self):
        GitOps.upstream_commit(self, "one\ntwo\nthree\nlater\n")
        with serve.WRITE_LOCK:  # a save holds the lock: the merge cannot write half-way through it
            result = {}
            import threading
            worker = threading.Thread(target=lambda: result.update(gitops.operate(self.doc, {"op": "pull"},
                                                                                  serve.WRITE_LOCK)))
            worker.start()
            worker.join(3)
            self.assertTrue(worker.is_alive())
            self.assertEqual((self.doc / "main.tex").read_text(encoding="utf-8"), "one\ntwo\nthree\n")
        worker.join(30)
        self.assertTrue(result["ok"])


if __name__ == "__main__":
    unittest.main()
