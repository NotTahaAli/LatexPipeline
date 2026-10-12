"""Review markup: build.toml review, the pretex, and the review PDF's source transformation (scripts/review_pdf.py)."""

from __future__ import annotations

import json
import shutil
import subprocess
import unittest
from unittest import mock

import _support  # noqa: F401 - puts scripts/ on sys.path
import review_pdf
from _support import build, fake_repo, write_doc

MAIN = "\\documentclass{article}\n\\newcommand\\x{old text}\n\\begin{document}\n%s\n\\end{document}\n"


def anchor(text: str, quote: str, nth: int = 0) -> dict:
    at = -1
    for _ in range(nth + 1):
        at = text.index(quote, at + 1)
    return {"from": at, "to": at + len(quote), "quote": quote, "prefix": text[max(0, at - 32):at],
            "suffix": text[at + len(quote):at + len(quote) + 32]}


def suggestion(text: str, quote: str, insert: str, sid: str = "s1", path: str = "main.tex", more=()) -> dict:
    item = {"id": sid, "path": path, "anchor": anchor(text, quote), "insert": insert}
    if more:
        item["more"] = [{"anchor": anchor(text, q), "insert": new} for q, new in more]
    return item


def run(files: dict, data: dict, **kw) -> dict:
    return review_pdf.plan(data, files.get, **kw)


class PlanTests(unittest.TestCase):
    def test_replacement_insertion_and_deletion(self):
        text = MAIN % "The quick brown fox jumps over the lazy dog."
        data = {"suggestions": [suggestion(text, "quick", "slow", "a"), suggestion(text, "lazy ", "", "b"),
                                {"id": "c", "path": "main.tex", "insert": " today",
                                 "anchor": {**anchor(text, "dog"), "quote": "", "from": text.index("dog") + 3,
                                            "prefix": text[text.index("dog") - 20:text.index("dog") + 3],
                                            "suffix": text[text.index("dog") + 3:text.index("dog") + 20]}}]}
        result = run({"main.tex": text}, data)
        self.assertEqual(result["applied"], 3, result["skipped"])
        body = result["texts"]["main.tex"]
        self.assertIn("The \\lpdel{quick}\\lpadd{slow} brown", body)
        self.assertIn("over the \\lpdel{lazy }dog\\lpadd{ today}.", body)

    def test_anchor_found_after_the_text_moved_and_skipped_when_gone(self):
        text = MAIN % "One sentence here. Another sentence there."
        data = {"suggestions": [suggestion(text, "Another sentence", "A second one", "a"),
                                suggestion(text, "One sentence", "Uno", "b")]}
        moved = text.replace("\\begin{document}\n", "\\begin{document}\nA new first line.\n")
        gone = moved.replace("One sentence here.", "Rewritten.")
        result = run({"main.tex": gone}, data)
        self.assertEqual(result["applied"], 1)
        self.assertIn("\\lpdel{Another sentence}\\lpadd{A second one}", result["texts"]["main.tex"])
        self.assertEqual([(s["id"], s["reason"]) for s in result["skipped"]],
                         [("b", "its text is no longer in the file")])

    def test_math_verbatim_comments_arguments_and_preamble_are_skipped(self):
        body = ("Prose $a + b$ more.\n\\section{Title words}\n% a comment line\n\\begin{verbatim}\nverb text\n"
                "\\end{verbatim}\n\\begin{equation}\nx = y\n\\end{equation}\n\\includegraphics[width=5cm]{img}\n"
                "Keep \\emph{this word} fine.")
        text = MAIN % body
        quotes = ["a + b", "Title", "comment", "verb text", "x = y", "width=5cm", "old text"]
        data = {"suggestions": [suggestion(text, q, "new", q) for q in quotes]
                + [suggestion(text, "this word", "that word", "emph")]}
        result = run({"main.tex": text}, data)
        self.assertEqual({s["id"] for s in result["skipped"]}, set(quotes))
        self.assertTrue(all(s["reason"] == review_pdf.NOT_PLAIN for s in result["skipped"]))
        self.assertIn("\\emph{\\lpdel{this word}\\lpadd{that word}}", result["texts"]["main.tex"])

    def test_new_text_with_specials_or_a_paragraph_break_is_skipped_escaped_ones_pass(self):
        text = MAIN % "Costs rose by fifty percent last year."
        data = {"suggestions": [suggestion(text, "fifty percent", "50\\%", "ok"),
                                suggestion(text, "rose", "fell} \\bad{", "brace"),
                                suggestion(text, "last", "100% sure", "comment"),
                                suggestion(text, "year", "year.\n\nNew paragraph", "par")]}
        result = run({"main.tex": text}, data)
        self.assertEqual([s["id"] for s in result["skipped"]], ["brace", "comment", "par"])
        self.assertIn("\\lpdel{fifty percent}\\lpadd{50\\%}", result["texts"]["main.tex"])

    def test_insertion_glued_to_a_command_name_is_skipped(self):
        text = MAIN % "We use \\LaTeX daily."
        at = text.index("\\LaTeX") + len("\\LaTeX")
        item = {"id": "a", "path": "main.tex", "insert": "2e", "anchor": {
            "from": at, "to": at, "quote": "", "prefix": text[at - 12:at], "suffix": text[at:at + 12]}}
        self.assertEqual(run({"main.tex": text}, {"suggestions": [item]})["skipped"][0]["reason"], review_pdf.NOT_PLAIN)

    def test_overlapping_suggestions_first_wins(self):
        text = MAIN % "Alpha beta gamma delta."
        data = {"suggestions": [suggestion(text, "beta gamma", "B G", "first"),
                                suggestion(text, "gamma delta", "G D", "second")]}
        result = run({"main.tex": text}, data)
        self.assertEqual(result["applied"], 1)
        self.assertEqual(result["skipped"], [{"id": "second", "kind": "suggestion", "path": "main.tex",
                                              "reason": "overlaps another suggestion"}])

    def test_grouped_suggestion_goes_in_whole_or_not_at_all(self):
        text = MAIN % "Red apple, red pear, and $red$ cherry."
        whole = suggestion(text, "Red apple", "Green apple", "g1", more=[("red pear", "green pear")])
        result = run({"main.tex": text}, {"suggestions": [whole]})
        self.assertEqual(result["applied"], 1)
        self.assertIn("\\lpdel{red pear}\\lpadd{green pear}", result["texts"]["main.tex"])
        broken = suggestion(text, "Red apple", "Green apple", "g2", more=[("red pear", "green pear")])
        math_at = text.index("$red$") + 1
        broken["more"].append({"insert": "green", "anchor": {"from": math_at, "to": math_at + 3, "quote": "red",
                                                             "prefix": text[math_at - 20:math_at],
                                                             "suffix": text[math_at + 3:math_at + 20]}})
        result = run({"main.tex": text}, {"suggestions": [broken]})
        self.assertEqual((result["applied"], result["texts"]), (0, {}))
        self.assertEqual(result["skipped"][0]["id"], "g2")

    def test_files_other_than_tex_and_preamble_files_are_skipped_chapters_work(self):
        chapter = "Chapter prose with a typo teh end.\n"
        preamble = "\\usepackage{amsmath}\nSome words.\n"
        files = {"main.tex": MAIN % "\\input{ch}", "ch.tex": chapter, "pre.tex": preamble, "refs.bib": "@book{x,}"}
        data = {"suggestions": [suggestion(chapter, "teh", "the", "ch", "ch.tex"),
                                suggestion(preamble, "Some words", "x", "pre", "pre.tex"),
                                {"id": "bib", "path": "refs.bib", "anchor": anchor("@book{x,}", "book"),
                                 "insert": "article"}]}
        result = run(files, data)
        self.assertEqual(result["texts"], {"ch.tex": "Chapter prose with a typo \\lpdel{teh}\\lpadd{the} end.\n"})
        self.assertEqual({s["id"]: s["reason"] for s in result["skipped"]},
                         {"pre": review_pdf.NOT_PLAIN, "bib": "not a .tex file of the document"})

    def test_unresolved_comments_become_escaped_notes(self):
        text = MAIN % "A claim that needs a source. Fine text."
        threads = [{"id": "t1", "path": "main.tex", "anchor": anchor(text, "needs a source"), "resolved": False,
                    "comments": [{"name": "Ann", "text": "Cite 50% & $x_1$ {here}\n\nplease"},
                                 {"name": "Zoë", "text": "agreed \\o/"}]},
                   {"id": "t2", "path": "main.tex", "anchor": anchor(text, "Fine"), "resolved": True,
                    "comments": [{"name": "B", "text": "done"}]}]
        result = run({"main.tex": text}, {"threads": threads}, ascii_only=True)
        self.assertEqual(result["notes"], 1)
        self.assertIn("needs a source\\lpnote{Ann: Cite 50\\% \\& \\$x\\_1\\$ \\{here\\} please / "
                      "Zoe: agreed \\textbackslash{}o/}.", result["texts"]["main.tex"])
        self.assertEqual(run({"main.tex": text}, {"threads": threads}, notes=False)["texts"], {})

    def test_utf16_offsets(self):
        text = MAIN % "Emoji \U0001F600 then the word target here."
        a = anchor(text, "target")
        a["from"] += 1  # one emoji before it: one more UTF-16 unit than code points
        self.assertEqual(review_pdf.locate(text, a), (text.index("target"), text.index("target") + 6))


class SettingsTests(unittest.TestCase):
    def test_review_key_default_values_and_cli_override(self):
        with fake_repo() as root:
            main = write_doc(root, "a", "x")
            self.assertEqual(build.read_settings(main)["review"], "off")
            self.assertEqual(build.review_pretex(build.read_settings(main)), "")
            write_doc(root, "a", "x", 'review = "show"\n')
            self.assertIn("{ulem}", build.review_pretex(build.read_settings(main)))
            with mock.patch.object(build, "REVIEW_MODE", "clean"):
                self.assertEqual(build.read_settings(main)["review"], "clean")
            write_doc(root, "a", "x", 'review = "yes"\n')
            with self.assertRaises(build.ConfigError):
                build.read_settings(main)

    def test_pretex_is_one_line(self):
        for text in build.REVIEW_PRETEX.values():
            self.assertNotIn("\n", text)

    def test_switching_review_mode_makes_the_pdf_stale(self):
        with fake_repo() as root:
            main = write_doc(root, "a", "x")
            pdf = build.output_path_for(main)
            pdf.parent.mkdir(parents=True)
            pdf.write_bytes(b"%PDF")
            cache = build.cache_dir_for(main)
            cache.mkdir(parents=True)
            (cache / ".mode").write_text("plain")
            self.assertFalse(build.is_stale(main))
            with mock.patch.object(build, "REVIEW_MODE", "show"):
                self.assertTrue(build.is_stale(main))
            (cache / ".mode").write_text("plain" + build.REVIEW_PRETEX["show"])
            self.assertTrue(build.is_stale(main))  # built with --review show, now off again


class ReviewPdfBuildTests(unittest.TestCase):
    """build_review_pdf with build_document mocked: the copy, the paths, and the real files untouched."""

    def test_copy_is_built_and_the_document_is_not_changed(self):
        with fake_repo() as root:
            text = MAIN % "Some plain words here."
            main = write_doc(root, "a", text)
            review = build.review_file_for(main)
            review.parent.mkdir(parents=True)
            review.write_text(json.dumps({"suggestions": [suggestion(text, "plain", "simple")]}), "utf-8")
            seen = {}

            def fake(copy_main, latexmk, live, force, **kw):
                seen.update(kw, text=copy_main.read_text("utf-8"), dir=copy_main.parent)
                kw["target"][1].parent.mkdir(parents=True, exist_ok=True)
                kw["target"][1].write_bytes(b"%PDF")
                kw["target"][1].with_suffix(".log").write_text("LOG", "utf-8")
                return {"ok": True, "error": None, "errors": []}, ""

            with mock.patch.object(build, "build_document", side_effect=fake):
                result = build.build_review_pdf(main, "latexmk", live=False)
            self.assertTrue(result["ok"])
            self.assertEqual(main.read_text("utf-8"), text)
            self.assertIn("\\lpdel{plain}\\lpadd{simple}", seen["text"])
            self.assertEqual(seen["review"], "show")
            self.assertFalse(seen["dir"].exists())  # the copy is gone afterwards
            pdf, log = build.review_paths(main)
            self.assertEqual(pdf, root / "out" / "a.review.pdf")
            self.assertTrue(log.read_text("utf-8").startswith("Review PDF of files/a/main.tex: 1 suggestion(s)"))
            build.prune(build.all_documents())
            self.assertTrue(pdf.exists())


@unittest.skipUnless(shutil.which("latexmk"), "needs latexmk")
class RealBuildTests(unittest.TestCase):
    """Hand markup and a review PDF typeset for real, with each engine on PATH."""

    BODY = ("\\section{Intro \\lpadd{plus}}\nKeep \\lpdel{gone words}\\lpadd{new words} and \\lpedit{this}{that}"
            " with a \\lpnote{remark}.\n\n\\begin{lpaddblock}\nAdded block.\n\\end{lpaddblock}\n\n"
            "\\begin{lpdelblock}\nRemoved block.\n\\end{lpdelblock}\n\n\\begin{lpnoteblock}\nA note block.\n"
            "\\end{lpnoteblock}\nEnd.")

    def text_of(self, pdf) -> str:
        if not shutil.which("pdftotext"):
            self.skipTest("needs pdftotext")
        return subprocess.run(["pdftotext", str(pdf), "-"], capture_output=True, text=True, check=True).stdout

    def test_show_and_clean_with_each_engine(self):
        for engine in ("pdflatex", "xelatex", "lualatex"):
            if not shutil.which(engine):
                continue
            for mode in ("show", "clean"):
                with self.subTest(engine=engine, mode=mode), fake_repo() as root:
                    main = write_doc(root, "d", MAIN % self.BODY, f'engine = "{engine}"\nreview = "{mode}"\n')
                    entry, _ = build.build_document(main, shutil.which("latexmk"), live=False, validate=False)
                    self.assertTrue(entry["ok"], build.log_path_for(main).read_text("utf-8")[-3000:])
                    words = " ".join(self.text_of(build.output_path_for(main)).split())
                    self.assertIn("Removed block" if mode == "show" else "End.", words)
                    if mode == "clean":
                        self.assertNotIn("gone words", words)
                        self.assertNotIn("remark", words)
                        self.assertNotIn("Removed block", words)
                        self.assertIn("Keep new words and that with a .", words)

    def test_review_pdf(self):
        with fake_repo() as root:
            text = MAIN % "A sentence with a typo teh here, and $x$ math."
            main = write_doc(root, "r", text)
            review = build.review_file_for(main)
            review.parent.mkdir(parents=True)
            review.write_text(json.dumps({
                "suggestions": [suggestion(text, "teh", "the"), suggestion(text, "x", "y", "math")],
                "threads": [{"id": "t", "path": "main.tex", "anchor": anchor(text, "sentence"), "resolved": False,
                             "comments": [{"name": "Ann", "text": "Fine 100%"}]}]}), "utf-8")
            result = build.build_review_pdf(main, shutil.which("latexmk"), live=False)
            self.assertTrue(result["ok"], result)
            self.assertEqual((result["applied"], result["notes"], [s["id"] for s in result["skipped"]]),
                             (1, 1, ["math"]))
            words = " ".join(self.text_of(build.review_paths(main)[0]).split())
            self.assertIn("tehthe here", words)  # pdftotext: no space between the deletion and the addition
            self.assertIn("[Ann: Fine 100%]", words)
            self.assertEqual(main.read_text("utf-8"), text)
            self.assertFalse(build.output_path_for(main).exists())


if __name__ == "__main__":
    unittest.main()
