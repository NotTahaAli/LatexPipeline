"""Pure logic in scripts/publish_release.py: asset names and release notes."""

import os
import unittest
from unittest import mock

from _support import build, fake_repo, publish_release, write_doc


class AssetNameTests(unittest.TestCase):
    def test_example(self):
        pdf = build.OUT_DIR / "reports" / "final_v2 report.pdf"
        self.assertEqual(publish_release.asset_name(pdf), "reports_2Ffinal_5Fv2_20report.pdf")

    def test_identity_with_build_escape_name(self):
        pdf = build.OUT_DIR / "reports" / "final_v2 report.pdf"
        self.assertEqual(publish_release.asset_name(pdf),
                         build.escape_name("reports/final_v2 report.pdf"))


class ReleaseNotesTests(unittest.TestCase):
    URL = "https://github.com/owner/repo/releases/download/pdfs/"

    def notes(self, previous, built, commit="newcommit1234"):
        with fake_repo() as root:
            write_doc(root, "a", "x")
            write_doc(root, "b c", "x")
            write_doc(root, "x--y", "x")
            with mock.patch.dict(os.environ, {"GITHUB_REPOSITORY": "owner/repo"}):
                return publish_release.release_notes(previous, built, commit)

    def test_failed_build_keeps_previous_pdf_and_state_round_trips(self):
        previous = {"a": {"pdf_commit": "oldsha1234", "pages": 3, "status": "ok",
                          "commit": "oldsha1234", "log": True}}
        notes = self.notes(previous, [{"name": "a", "ok": False, "pages": None}])

        self.assertIn(f"| a | [PDF]({self.URL}a.pdf) | 3 | oldsha1 | failed (newcomm) | "
                      f"[log]({self.URL}a.log) |", notes)
        self.assertIn("| b c | - | - | - | - | - |", notes)

        state = publish_release.read_state(notes)
        self.assertEqual(state["a"], {"pdf_commit": "oldsha1234", "pages": 3,
                                      "status": "failed", "commit": "newcommit1234", "log": True})
        self.assertEqual(state["b c"], {"log": False})
        self.assertEqual(state["x--y"], {"log": False})  # "--" survives the HTML comment

    def test_successful_build_moves_pdf_commit(self):
        notes = self.notes({}, [{"name": "a", "ok": True, "pages": 5}])
        state = publish_release.read_state(notes)
        self.assertEqual(state["a"]["pdf_commit"], "newcommit1234")
        self.assertEqual(state["a"]["status"], "ok")
        self.assertEqual(state["a"]["pages"], 5)

    def test_read_state_handles_missing_or_bad_state(self):
        self.assertEqual(publish_release.read_state(""), {})
        self.assertEqual(publish_release.read_state("<!-- latex-pipeline-state: {bad -->"), {})


class UploadSkipTests(unittest.TestCase):
    def test_unchanged_pdf_is_not_uploaded_again(self):
        import hashlib
        with fake_repo() as root:
            pdf = root / "out" / "a.pdf"
            pdf.parent.mkdir(parents=True)
            pdf.write_bytes(b"%PDF-1.5 same")
            digest = "sha256:" + hashlib.sha256(b"%PDF-1.5 same").hexdigest()
            self.assertFalse(publish_release.needs_upload(pdf, {"a.pdf": digest}))
            self.assertTrue(publish_release.needs_upload(pdf, {"a.pdf": "sha256:" + "0" * 64}))
            self.assertTrue(publish_release.needs_upload(pdf, {}))  # New, or no digest reported.

    def test_release_digests_parses_gh_output(self):
        with mock.patch.object(publish_release, "gh", return_value="a.pdf\tsha256:ab\nb.log\tsha256:cd\n"):
            self.assertEqual(publish_release.release_digests(), {"a.pdf": "sha256:ab", "b.log": "sha256:cd"})


if __name__ == "__main__":
    unittest.main()
