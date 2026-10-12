"""publish_release.main against a fake gh: first release, unreadable digests, deletions and the tag move."""

from __future__ import annotations

import contextlib
import hashlib
import io
import os
import subprocess
import unittest
from unittest import mock

from _support import fake_repo, publish_release, write_doc


class FakeGh:
    """A release that may not exist yet, holding `assets` ({name: bytes digest})."""

    def __init__(self, exists=True, assets=None, digests_fail=False):
        self.exists, self.assets, self.digests_fail, self.calls = exists, dict(assets or {}), digests_fail, []

    def __call__(self, *args):
        self.calls.append(args)
        if args[:2] == ("release", "view") and "body" in args:
            if not self.exists:
                raise subprocess.CalledProcessError(1, "gh")
            return "<!-- latex-pipeline-state: {} -->"
        if args[:2] == ("release", "view"):
            return "\n".join(self.assets)
        if args[0] == "api" and "--jq" in args:
            if self.digests_fail:
                raise subprocess.CalledProcessError(1, "gh")
            return "".join(f"{name}\t{digest}\n" for name, digest in self.assets.items())
        return ""

    def named(self, *prefix):
        return [c for c in self.calls if c[:len(prefix)] == prefix]


def run(gh, env):
    out = io.StringIO()
    with mock.patch.object(publish_release, "gh", gh), mock.patch.dict(os.environ, env), \
            mock.patch.object(publish_release, "load_documents", return_value=[]), contextlib.redirect_stdout(out):
        code = publish_release.main()
    return code, out.getvalue()


class Main(unittest.TestCase):
    def test_first_release_after_a_failed_build_starts_at_the_root_commit(self):
        with fake_repo() as root:
            write_doc(root, "a", "x")
            gh = FakeGh(exists=False)
            root_commit = subprocess.CompletedProcess([], 0, "rootsha\n", "")
            with mock.patch.object(publish_release.subprocess, "run", return_value=root_commit) as git:
                run(gh, {"GITHUB_SHA": "headsha", "BUILD_OK": "false"})
            self.assertEqual(git.call_args[0][0][:2], ["git", "rev-list"])
        create = gh.named("release", "create")[0]
        self.assertEqual(create[-2:], ("--target", "rootsha"))
        self.assertEqual(gh.named("api", "--method", "PATCH"), [])  # a failed build never moves the tag

    def test_first_release_after_a_good_build_targets_the_commit_and_moves_the_tag(self):
        with fake_repo() as root:
            write_doc(root, "a", "x")
            gh = FakeGh(exists=False)
            code, text = run(gh, {"GITHUB_SHA": "headsha", "BUILD_OK": "true"})
        self.assertEqual(code, 0)
        self.assertEqual(gh.named("release", "create")[0][-2:], ("--target", "headsha"))
        self.assertIn("sha=headsha", gh.named("api", "--method", "PATCH")[0])
        self.assertIn("Moving tag pdfs to headsha", text)

    def test_deletes_only_assets_no_document_owns_and_skips_unchanged(self):
        with fake_repo() as root:
            write_doc(root, "a", "x")
            out = root / "out"
            out.mkdir()
            (out / "a.pdf").write_bytes(b"%PDF same")
            (out / "a.log").write_bytes(b"new log")
            same = "sha256:" + hashlib.sha256(b"%PDF same").hexdigest()
            gh = FakeGh(assets={"a.pdf": same, "a.log": "sha256:old", "gone.pdf": "sha256:x", "gone.log": "sha256:y"})
            code, text = run(gh, {"GITHUB_SHA": "c", "BUILD_OK": "true"})
        self.assertEqual(code, 0)
        self.assertIn("Unchanged: a.pdf", text)
        self.assertEqual([c[3].rpartition("#")[2] for c in gh.named("release", "upload")], ["a.log"])
        self.assertEqual(sorted(c[3] for c in gh.named("release", "delete-asset")), ["gone.log", "gone.pdf"])
        self.assertEqual(len(gh.named("release", "edit")), 1)

    def test_unreadable_digests_upload_everything(self):
        with fake_repo() as root:
            write_doc(root, "a", "x")
            out = root / "out"
            out.mkdir()
            (out / "a.pdf").write_bytes(b"%PDF")
            gh = FakeGh(assets={"a.pdf": "sha256:" + hashlib.sha256(b"%PDF").hexdigest()}, digests_fail=True)
            code, text = run(gh, {"GITHUB_SHA": "c", "BUILD_OK": "true"})
        self.assertIn("uploading every file", text)
        self.assertEqual([c[3].rpartition("#")[2] for c in gh.named("release", "upload")], ["a.pdf"])


if __name__ == "__main__":
    unittest.main()
