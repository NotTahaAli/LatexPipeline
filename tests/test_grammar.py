# ruff: noqa: E501
"""grammar.py: LaTeX -> plain text offset mapping, chunking, throttle, cache, config. No network."""

import importlib
import os
import re
import unittest
from unittest import mock

import _support  # noqa: F401  (puts scripts/ on sys.path)

grammar = importlib.import_module("grammar")  # after _support put scripts/ on sys.path


def lt_reply(chunk, *needles, rule="TEST_RULE", category="GRAMMAR", replacements=("fixed",)):
    """A LanguageTool-shaped reply flagging each needle's first occurrence in chunk (UTF-16 offsets)."""
    matches = []
    spots = [(needle, m.start()) for needle in needles for m in re.finditer(re.escape(needle), chunk)]
    for needle, at in spots:
        matches.append({
            "message": f"msg {needle}", "offset": grammar.to_utf16(chunk, at), "length": grammar.utf16_length(needle),
            "replacements": [{"value": v} for v in replacements],
            "rule": {"id": rule, "category": {"id": category}},
        })
    return {"matches": matches}


class ExtractTests(unittest.TestCase):
    def assertMapped(self, source):
        """Every plain character is the source character it claims to come from (or a documented stand-in)."""
        got = grammar.extract(source)
        self.assertEqual(len(got.plain), len(got.src))
        self.assertEqual(got.src, sorted(got.src))
        for char, at in zip(got.plain, got.src):
            self.assertTrue(0 <= at < len(source))
            if char.isalnum() and char not in "X1":
                self.assertEqual(source[at], char, (char, at))
        return got

    def test_text_maps_to_exact_offsets(self):
        source = "Hello \\emph{big} world.\nSecond line."
        got = self.assertMapped(source)
        self.assertEqual(got.plain, "Hello big world. Second line.")
        self.assertEqual(source[got.src[got.plain.index("big")]], "b")
        self.assertEqual(source[got.src[got.plain.index("Second")]], "S")

    def test_comments_and_escaped_percent(self):
        got = self.assertMapped("100\\% sure % hidden \\emph{x}\nnext")
        self.assertEqual(got.plain, "100% sure next")

    def test_math_becomes_a_placeholder(self):
        for source in ("a $x^2 + y$ b", "a $$x$$ b", "a \\(x\\) b", "a \\[x\\] b", "a \\begin{align} x \\\\ y \\end{align} b"):
            self.assertEqual(grammar.extract(source).plain.replace("\n", " ").replace("  ", " "), "a X b", source)
        self.assertEqual(grammar.extract("costs \\$5 and \\$6").plain, "costs $5 and $6")
        self.assertEqual(grammar.extract("stray $ never\n\nswallows").plain, "stray never\n\nswallows")

    def test_keeps_headings_captions_footnotes_as_paragraphs(self):
        got = grammar.extract("\\section*{Title here}\nBody \\footnote{Note one.} goes on.\n\\caption[s]{A caption.}")
        self.assertEqual(got.plain, "Title here\n\nBody\n\nNote one.\n\ngoes on.\n\nA caption.")
        self.assertEqual(grammar.extract("\\section[Short]{Long} text").plain, "Long\n\ntext")

    def test_skips_code_and_drawing_environments(self):
        source = "Before.\n\\begin{lstlisting}[language=C]\nint a = 1; % x\n\\end{lstlisting}\nAfter.\n" \
                 "\\begin{tikzpicture}\\draw (0,0);\\end{tikzpicture}\nEnd. \\verb|a b| \\texttt{c_d}."
        self.assertEqual(grammar.extract(source).plain, "Before.\n\nAfter.\n\nEnd. X X.")

    def test_drops_label_ref_cite_and_graphics_arguments(self):
        got = grammar.extract("See Figure~\\ref{fig:a}\\label{x} and \\citep[p.~3]{k1,k2}.\\includegraphics[width=1cm]{pic} Done.")
        self.assertEqual(got.plain, "See Figure 1 and [1]. Done.")

    def test_preamble_and_end_of_document(self):
        source = "\\documentclass{article}\n\\newcommand{\\foo}[1]{bar #1}\n\\begin{document}\nText.\n\\end{document}\nIgnored."
        self.assertEqual(grammar.extract(source).plain, "Text.")

    def test_environment_arguments_are_not_text(self):
        got = grammar.extract("\\begin{tabular}{lcr}\nA & B \\\\\nC & D\n\\end{tabular}\\begin{minipage}{0.5\\textwidth}Hi\\end{minipage}")
        self.assertEqual(got.plain, "A\n\nB\n\nC\n\nD\n\nHi")

    def test_accents_quotes_and_symbols(self):
        self.assertEqual(grammar.extract("Caf\\'e na\\\"{\\i}ve Schr\\\"odinger \\c{c}a").plain, "Café naïve Schrödinger ça")
        self.assertEqual(grammar.extract("``Quoted'' \\LaTeX{} and\\ldots").plain, "\u201cQuoted\u201d LaTeX and…")

    def test_backslash_before_non_ascii_letter_does_not_crash(self):
        for src in ("Text \\\u00e9 more", "Text \\\u00fc more", "Text \\\u00df more"):
            grammar.extract(src)
        self.assertIn("more", grammar.extract("Text \\\u00e9 more").plain)
        self.assertEqual(grammar.extract("Caf\\'e and \\\"u").plain, "Caf\u00e9 and \u00fc")

    def test_line_and_column_of_a_mapped_character(self):
        source = "One.\n  \\textbf{Two} three.\n"
        got = grammar.extract(source)
        at = got.src[got.plain.index("Two")]
        self.assertEqual(grammar.LineIndex(source).locate(at), (2, 11))

    def test_items_and_blank_lines_split_paragraphs(self):
        got = grammar.extract("\\begin{itemize}\n\\item One\n\\item[x] Two\n\\end{itemize}\n\n\n\nNew.")
        self.assertEqual(got.plain, "One\n\nTwo\n\nNew.")

    def test_unbalanced_input_does_not_hang_or_raise(self):
        for source in ("\\emph{", "}", "\\begin{align", "\\", "$", "\\verb", "\\section{", "\\'", "\\begin{lstlisting} x"):
            grammar.extract(source)

    def test_utf16_helpers(self):
        text = "a\U0001F600b"
        self.assertEqual(grammar.to_utf16(text, 2), 3)
        self.assertEqual(grammar.from_utf16(text, 3), 2)
        self.assertEqual(grammar.utf16_length(text), 4)


class CheckTests(unittest.TestCase):
    def setUp(self):
        grammar.CACHE.clear()
        patcher = mock.patch.object(grammar, "post_form")
        self.post = patcher.start()
        self.addCleanup(patcher.stop)

    def test_finding_position_and_fix(self):
        self.post.side_effect = lambda url, fields, proxy=False: lt_reply(fields["text"], "are a test")
        source = "Intro.\n\nThis \\emph{x} are a test here.\n"
        (found,) = grammar.check_source(source, "main.tex", url="http://x/v2/check", public=False, lang="en-US")
        self.assertEqual((found.file, found.line, found.col, found.rule), ("main.tex", 3, 15, "TEST_RULE"))
        self.assertEqual(source[found.offset:found.offset + found.length], "are a test")
        self.assertEqual(found.replacements, ("fixed",))
        fields = self.post.call_args.args[1]
        self.assertEqual(fields["language"], "en-US")
        self.assertIn("WHITESPACE_RULE", fields["disabledRules"])
        self.assertIn("TYPOGRAPHY", fields["disabledCategories"])

    def test_no_quick_fix_when_the_span_holds_commands(self):
        self.post.side_effect = lambda url, fields, proxy=False: lt_reply(fields["text"], "a big dog")
        (found,) = grammar.check_source("It is a \\emph{big} dog.", "m.tex", url="u", public=False)
        self.assertEqual(found.replacements, ())

    def test_at_most_three_replacements(self):
        self.post.side_effect = lambda url, fields, proxy=False: lt_reply(fields["text"], "teh", replacements=tuple("abcde"))
        (found,) = grammar.check_source("Fix teh word.", "m.tex", url="u", public=False)
        self.assertEqual(found.replacements, ("a", "b", "c"))

    def test_ignored_and_disabled_rules_are_dropped(self):
        self.post.side_effect = lambda url, fields, proxy=False: {
            **lt_reply(fields["text"], "one", rule="WHITESPACE_RULE"),
            "matches": lt_reply(fields["text"], "one", rule="WHITESPACE_RULE")["matches"]
            + lt_reply(fields["text"], "two", rule="MINE")["matches"]
            + lt_reply(fields["text"], "three", rule="OK", category="TYPOGRAPHY")["matches"]
            + lt_reply(fields["text"], "four", rule="OK")["matches"],
        }
        found = grammar.check_source("one two three four", "m.tex", url="u", public=False, disabled=["MINE"])
        self.assertEqual([f.text for f in found], ["four"])
        self.assertIn("MINE", self.post.call_args.args[1]["disabledRules"])

    def test_astral_characters_keep_offsets_right(self):
        self.post.side_effect = lambda url, fields, proxy=False: lt_reply(fields["text"], "bad")
        source = "Fun \U0001F600\U0001F600 is bad here."
        (found,) = grammar.check_source(source, "m.tex", url="u", public=False)
        self.assertEqual(source[found.offset:found.offset + found.length], "bad")

    def test_paragraphs_are_batched_cached_and_split_back(self):
        self.post.side_effect = lambda url, fields, proxy=False: lt_reply(fields["text"], "bad")
        source = "First bad.\n\nSecond bad.\n\nThird fine."
        found = grammar.check_source(source, "m.tex", url="u", public=True)
        self.assertEqual([(f.line, f.col) for f in found], [(1, 7), (3, 8)])
        self.assertEqual(self.post.call_count, 1)  # one request for three paragraphs
        again = grammar.check_source(source + "\n\nFourth bad.", "m.tex", url="u", public=True)
        self.assertEqual(len(again), 3)
        self.assertEqual(self.post.call_count, 2)  # only the new paragraph went out
        self.assertEqual(self.post.call_args.args[1]["text"], "Fourth bad.")

    def test_chunks_stay_below_the_request_limit(self):
        self.post.return_value = {"matches": []}
        paragraph = ("word " * 400).strip()  # 2 KB
        grammar.check_source("\n\n".join(f"{paragraph} {n}" for n in range(30)), "m.tex", url="u", public=False)
        self.assertGreater(self.post.call_count, 3)
        for call in self.post.call_args_list:
            self.assertLessEqual(len(call.args[1]["text"].encode()), grammar.CHUNK_BYTES)

    def test_one_huge_paragraph_is_cut(self):
        self.post.return_value = {"matches": []}
        grammar.check_source("word " * 20000, "m.tex", url="u", public=False)
        for call in self.post.call_args_list:
            self.assertLessEqual(len(call.args[1]["text"].encode()), grammar.CHUNK_BYTES)

    def test_no_character_is_lost_when_a_cut_finds_no_space(self):
        text = "x" * 40000
        pieces = grammar.split_paragraphs(text)
        self.assertEqual("".join(t for _, t in pieces), text)
        self.assertTrue(all(text[s:s + len(t)] == t for s, t in pieces))

    def test_many_matches_are_capped_and_fast(self):
        import time
        text = "the the " * 50000

        def reply(url, fields, proxy=False):
            return {"matches": [{"message": "m", "offset": 4 * k, "length": 3, "replacements": [],
                                 "rule": {"id": "R", "category": {"id": "GRAMMAR"}}} for k in range(1000)]}
        self.post.side_effect = reply
        began = time.monotonic()
        found = grammar.check_source(text, "m.tex", url="u", public=False)
        self.assertLess(time.monotonic() - began, 3)
        self.assertTrue(found)
        self.assertLessEqual(len(found), grammar.MAX_MATCHES * len(self.post.call_args_list))

    def test_utf16_map_matches_from_utf16(self):
        text = "a\U0001F600b\U0001F600\U0001F600c"
        mapper = grammar.Utf16Map(text)
        for unit in range(grammar.utf16_length(text) + 2):
            self.assertEqual(mapper.index(unit), grammar.from_utf16(text, unit), unit)

    def test_quote_ligature_quick_fix_covers_the_whole_source(self):
        self.post.side_effect = lambda url, fields, proxy=False: lt_reply(fields["text"], "\u201chello\u201d")
        source = "Say ``hello'' now."
        (found,) = grammar.check_source(source, "m.tex", url="u", public=False)
        self.assertEqual(source[found.offset:found.offset + found.length], "``hello''")
        self.assertEqual(found.replacements, ("fixed",))

    def test_replacements_dropped_when_the_span_is_not_the_plain_text(self):
        self.post.side_effect = lambda url, fields, proxy=False: lt_reply(fields["text"], "a 1 b")
        (found,) = grammar.check_source("It is a \\ref{x} b.", "m.tex", url="u", public=False)
        self.assertEqual(found.replacements, ())

    def test_local_requests_bypass_proxies_public_ones_do_not(self):
        self.post.return_value = {"matches": []}
        grammar.check_source("Some text.", "m.tex", url="u", public=False)
        self.assertIs(self.post.call_args.kwargs["proxy"], False)
        grammar.CACHE.clear()
        grammar.check_source("Some text.", "m.tex", url="u", public=True, max_wait=0)
        self.assertIs(self.post.call_args.kwargs["proxy"], True)


class LimiterTests(unittest.TestCase):
    def test_waits_for_the_window(self):
        now = [0.0]
        slept = []
        limiter = grammar.Limiter(requests=2, size=100, window=60, clock=lambda: now[0],
                                  sleep=lambda s: (slept.append(s), now.__setitem__(0, now[0] + s)))
        limiter.acquire(10)
        limiter.acquire(10)
        limiter.acquire(10)  # third request waits for the first to age out
        self.assertEqual(len(slept), 1)
        self.assertAlmostEqual(slept[0], 60)

    def test_size_budget(self):
        now = [0.0]
        limiter = grammar.Limiter(requests=20, size=100, window=60, clock=lambda: now[0], sleep=lambda s: now.__setitem__(0, now[0] + s))
        limiter.acquire(80)
        with self.assertRaises(grammar.GrammarError):
            limiter.acquire(30, max_wait=5)
        limiter.acquire(30)  # unlimited patience: sleeps, then goes
        self.assertGreaterEqual(now[0], 60)

    def test_public_check_raises_instead_of_blocking_the_editor(self):
        grammar.CACHE.clear()
        limiter = grammar.Limiter(requests=1)
        limiter.acquire(1)
        with mock.patch.object(grammar, "PUBLIC_LIMITER", limiter), mock.patch.object(grammar, "post_form") as post:
            with self.assertRaises(grammar.GrammarError):
                grammar.check_source("Text here.", "m.tex", url="u", public=True, max_wait=1)
            post.assert_not_called()


class ConfigTests(unittest.TestCase):
    def test_endpoint(self):
        self.assertEqual(grammar.endpoint("http://localhost:8081"), "http://localhost:8081/v2/check")
        self.assertEqual(grammar.endpoint("https://lt.example/api/"), "https://lt.example/api/v2/check")
        self.assertEqual(grammar.endpoint("http://h/v2/check"), "http://h/v2/check")
        for bad in ("file:///etc/passwd", "ftp://x", "localhost:8081", ""):
            with self.assertRaises(grammar.GrammarError):
                grammar.endpoint(bad)

    def test_resolve(self):
        up, down = (lambda base: True), (lambda base: False)
        self.assertEqual(grammar.resolve(None, environ={}, probe_fn=down), ("off", None))
        self.assertEqual(grammar.resolve("auto", environ={}, probe_fn=up), ("local", "http://localhost:8081/v2/check"))
        self.assertEqual(grammar.resolve(None, environ={"LANGUAGETOOL_URL": "http://e:1"}, probe_fn=up), ("local", "http://e:1/v2/check"))
        self.assertEqual(grammar.resolve(None, "http://s:2", {"LANGUAGETOOL_URL": "http://e:1"}, up), ("local", "http://s:2/v2/check"))
        self.assertEqual(grammar.resolve("off", environ={}, probe_fn=up), ("off", None))
        self.assertEqual(grammar.resolve("local", environ={}, probe_fn=down)[0], "local")  # asked for: errors show up later
        self.assertEqual(grammar.resolve("public", probe_fn=up), ("public", grammar.PUBLIC_URL))
        with self.assertRaises(grammar.GrammarError):
            grammar.resolve("cloud")

    def test_public_is_never_chosen_by_auto(self):
        self.assertEqual(grammar.resolve("auto", environ={}, probe_fn=lambda b: False)[0], "off")

    def test_probe_is_false_when_nothing_listens(self):
        self.assertFalse(grammar.probe("http://127.0.0.1:9", timeout=0.2))
        self.assertFalse(grammar.probe("not a url"))

    def test_validate_settings(self):
        grammar.validate_settings({"grammar": "public", "grammar_url": "http://h:1", "disabled_rules": ["A"]})
        for bad in ({"grammar": "cloud"}, {"grammar_url": "ftp://x"}, {"disabled_rules": "A"}, {"grammar_url": 3}):
            with self.assertRaises(grammar.GrammarError):
                grammar.validate_settings(bad)


@unittest.skipUnless(os.environ.get("LANGUAGETOOL_URL"), "set LANGUAGETOOL_URL to a running LanguageTool server")
class LiveTests(unittest.TestCase):
    def test_against_a_real_server(self):
        grammar.CACHE.clear()
        mode, url = grammar.resolve("local")
        found = grammar.check_source("This are a \\emph{test} of the $x$ grammer.\n", "m.tex", url=url, public=False, lang="en-US")
        self.assertTrue(any("are" in f.text for f in found), found)
        are = next(f for f in found if f.text == "are")
        self.assertEqual((are.line, are.col), (1, 6))


if __name__ == "__main__":
    unittest.main()
