# ruff: noqa: E501
"""bibfix: parsing, inserting fields byte-for-byte, and the Crossref mapping. The network is always mocked."""

import importlib
import json
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from _support import ci_report

bibfix = importlib.import_module("bibfix")  # after _support put scripts/ on sys.path

WORK = json.loads((Path(__file__).parent / "fixtures" / "crossref_work.json").read_text(encoding="utf-8"))


class ParseTests(unittest.TestCase):
    def test_skips_comments_strings_preamble_and_finds_fields(self):
        text = ('Free text here\n@comment{@article{c, a={b}}}\n@string{me = "Ann {B}"}\n'
                '@preamble{"\\newcommand{\\x}{y}"}\n@Book{k1,\n  author = me # " and Bo",\n  title = {A {nested {deep}} = x},\n'
                '  year = 2001\n}\n@misc(k2, note="a, } b")\n')
        entries = bibfix.parse(text)
        self.assertEqual([(e.kind, e.key) for e in entries], [("book", "k1"), ("misc", "k2")])
        fields = {f.name: f.value for f in entries[0].fields}
        self.assertEqual(fields["title"], "A {nested {deep}} = x")
        self.assertEqual(fields["year"], "2001")
        self.assertEqual(fields["author"], 'me # " and Bo"')
        self.assertEqual(entries[1].fields[0].value, "a, } b")

    def test_unbalanced_entry_is_skipped(self):
        # BibTeX swallows the rest of the file after an unterminated value, and so does parse (in linear time).
        self.assertEqual([e.key for e in bibfix.parse("@article{z, year=1}\n@article{a, title={oops\n@article{b, year=1}")], ["z"])


class InsertTests(unittest.TestCase):
    NEW = {"year": "2019", "doi": "10.1/x"}

    def test_matches_indent_and_alignment_without_trailing_comma(self):
        text = "@article{k,\n\tauthor  = {A},\n\ttitle   = {T}\n}\n@misc{o, x={y}}\n"
        got = bibfix.insert_fields(text, "k", self.NEW)
        self.assertEqual(got, "@article{k,\n\tauthor  = {A},\n\ttitle   = {T},\n\tyear    = {2019},\n\tdoi     = {10.1/x}\n}\n@misc{o, x={y}}\n")

    def test_trailing_comma_is_kept(self):
        got = bibfix.insert_fields("@article{k,\n  title = {T},\n}\n", "k", {"year": "1"})
        self.assertEqual(got, "@article{k,\n  title = {T},\n  year  = {1},\n}\n")

    def test_quote_style_and_uppercase_names(self):
        got = bibfix.insert_fields('@article{k,\n  TITLE="T"\n}', "k", {"year": '2"1'})
        self.assertEqual(got, '@article{k,\n  TITLE="T",\n  YEAR="2\'\'1"\n}')

    def test_unterminated_braces_parse_in_linear_time(self):
        import time
        started = time.monotonic()
        self.assertEqual(bibfix.parse("@a{k,f={" * 8192), [])  # 64 KB
        self.assertLess(time.monotonic() - started, 1.0)

    def test_inline_entry_is_not_column_aligned(self):
        text = "@article{k, title = {A study}, year = {2000}}"
        self.assertEqual(bibfix.insert_fields(text, "k", {"doi": "10.1/x"}),
                         "@article{k, title = {A study}, year = {2000}, doi = {10.1/x}}")

    def test_year_only_from_a_plausible_int(self):
        for parts in ([[2020]], [[2020, 5]]):
            self.assertEqual(bibfix._year({"issued": {"date-parts": parts}}), "2020")
        for parts in ([["2020"]], [[True]], [[20200]], [[0]], [[None]], [], ["x"], [[2020.5]]):
            self.assertEqual(bibfix._year({"issued": {"date-parts": parts}}), "", parts)

    def test_inline_entry(self):
        self.assertEqual(bibfix.insert_fields("@misc{k, title={T}}", "k", {"year": "1"}), "@misc{k, title={T}, year={1}}")

    def test_empty_entry(self):
        self.assertEqual(bibfix.insert_fields("@misc{k,\n}", "k", {"year": "1"}), "@misc{k,\n  year = {1},\n}")
        self.assertEqual(bibfix.insert_fields("@misc{k}", "k", {"year": "1"}), "@misc{k,\n  year = {1},}")

    def test_crlf_and_other_entries_untouched(self):
        a, b = "@misc{a,\r\n  title = {A}\r\n}\r\n", "% note\r\n@misc{b,\r\n  title = {B {x}}\r\n}\r\n"
        got = bibfix.insert_fields(a + b, "a", {"year": "1"})
        self.assertEqual(got, "@misc{a,\r\n  title = {A},\r\n  year  = {1}\r\n}\r\n" + b)

    def test_existing_fields_are_never_replaced_and_unknown_key_is_a_noop(self):
        text = "@misc{k,\n  Year = {1999}\n}\n"
        self.assertEqual(bibfix.insert_fields(text, "k", {"year": "2000"}), text)
        self.assertEqual(bibfix.insert_fields(text, "zz", {"doi": "x"}), text)

    def test_value_with_comma_and_braces_before_the_end(self):
        text = "@misc{k,\n  note = {a, {b}, c},\n  title = {x = {y}}\n}\n"
        self.assertEqual(bibfix.insert_fields(text, "k", {"doi": "d"}),
                         "@misc{k,\n  note = {a, {b}, c},\n  title = {x = {y}},\n  doi   = {d}\n}\n")

    def test_edit_offsets_are_utf16(self):
        text = "@misc{k,\n  doi = {\U0001F600}\n}"
        with mock.patch.object(bibfix, "get_json", return_value=WORK):
            got = bibfix.suggest_for(text, "k")
        self.assertEqual(got["at"], text.index("}\n}") + 1 + 1)  # one extra unit for the emoji


class CrossrefTests(unittest.TestCase):
    def setUp(self):
        bibfix.CACHE.clear()

    def test_mapping(self):
        got = bibfix.map_item(WORK["message"], "article")
        self.assertEqual(got["author"], "Lovelace, Ada and Turing, Alan and {The Example Consortium}")
        self.assertEqual(got["title"], "Deep Learning \\& Beyond: A 100\\% Study")
        self.assertEqual((got["journal"], got["year"], got["volume"], got["number"], got["pages"]),
                         ("Journal of Examples", "2019", "12", "3", "123--145"))
        self.assertEqual((got["publisher"], got["doi"]), ("Example Press \\& Co.", "10.1000/xyz_123"))
        self.assertNotIn("journal", bibfix.map_item(WORK["message"], "book"))
        self.assertIn("booktitle", bibfix.map_item(WORK["message"], "inproceedings"))

    def test_doi_lookup_suggests_only_missing_fields(self):
        with mock.patch.object(bibfix, "get_json", return_value=WORK) as fetch:
            got = bibfix.suggest("article", {"doi": "https://doi.org/10.1000/xyz_123", "title": "Mine", "date": "2019"})
        self.assertEqual(fetch.call_args[0][0], "https://api.crossref.org/works/10.1000/xyz_123")
        self.assertEqual(sorted(got["fields"]), ["author", "journal", "number", "pages", "publisher", "volume"])
        self.assertEqual(got["source"], "doi")

    def test_title_lookup_accepts_a_close_match_and_adds_the_doi(self):
        reply = {"message": {"items": [WORK["message"]]}}
        with mock.patch.object(bibfix, "get_json", return_value=reply) as fetch:
            got = bibfix.suggest("article", {"title": "Deep learning and beyond: a 100% study", "year": "2020"})
        self.assertIn("query.bibliographic=Deep+learning", fetch.call_args[0][0])
        self.assertEqual(got["fields"]["doi"], "10.1000/xyz_123")
        self.assertEqual(got["source"], "title")

    def test_title_lookup_refuses_a_loose_match_or_wrong_year(self):
        reply = {"message": {"items": [WORK["message"]]}}
        for fields in ({"title": "Deep learning"}, {"title": "Deep Learning & Beyond: A 100% Study", "year": "2010"}):
            with self.subTest(fields=fields), mock.patch.object(bibfix, "get_json", return_value=reply):
                with self.assertRaises(bibfix.BibLookupError):
                    bibfix.suggest("article", fields)
        with mock.patch.object(bibfix, "get_json", return_value={"message": {"items": []}}):
            with self.assertRaises(bibfix.BibLookupError):
                bibfix.suggest("article", {"title": "Anything"})

    def test_nothing_to_look_up(self):
        with self.assertRaises(bibfix.BibLookupError):
            bibfix.suggest("article", {"year": "2020"})

    def test_suggest_for_builds_the_edit(self):
        text = "@article{k,\n  doi = {10.1000/xyz_123},\n  year = {2019}\n}\n"
        with mock.patch.object(bibfix, "get_json", return_value=WORK):
            got = bibfix.suggest_for(text, "k")
        new = text[:got["at"]] + got["insert"] + text[got["at"]:]
        self.assertEqual(new, bibfix.insert_fields(text, "k", got["fields"]))
        self.assertIn("\n  journal = {Journal of Examples}", new)
        with self.assertRaises(bibfix.BibLookupError):
            bibfix.suggest_for(text, "nope")


class EntryEditTests(unittest.TestCase):
    TEXT = ('% my refs\n@string{jx = "J. X"}\n\n@article{a,\n    author  = {Ann {B}},\n    title   = "A {T} = q",\n'
            '    journal = jx,\n    year    = 2001,\n}\n\n@book{b, title={B}, year={1999}}\n\n@misc{c,\n  note = {x}\n}')

    @staticmethod
    def splice(text, edit):
        return text[:edit[0]] + edit[2] + text[edit[1]:]

    def test_replace_keeps_unchanged_fields_and_delimiters(self):
        edit = bibfix.replace_entry(self.TEXT, "a", "article", "a", {
            "author": "Ann {B}", "title": "New", "journal": "jy", "year": "2002", "doi": "10.1/x"})
        got = self.splice(self.TEXT, edit)
        self.assertEqual(got, self.TEXT.replace('"A {T} = q"', '"New"').replace("jx,\n    year    = 2001,",
                         "jy,\n    year    = 2002,\n    doi     = {10.1/x},"))

    def test_replace_removes_fields_renames_and_retypes(self):
        got = self.splice(self.TEXT, bibfix.replace_entry(self.TEXT, "a", "inproceedings", "a2", {"author": "Ann {B}", "year": "2001"}))
        self.assertIn("@inproceedings{a2,\n    author  = {Ann {B}},\n    year    = 2001,\n}\n\n@book", got)
        inline = self.splice(self.TEXT, bibfix.replace_entry(self.TEXT, "b", "book", "b", {"year": "1999"}))
        self.assertIn("@book{b, year={1999}}", inline)
        last = self.splice(self.TEXT, bibfix.replace_entry(self.TEXT, "c", "misc", "c", {"title": "T"}))
        self.assertTrue(last.endswith("@misc{c,\n  title = {T},\n}"), last[-40:])

    def test_replace_crlf_and_bad_input(self):
        text = self.TEXT.replace("\n", "\r\n")
        got = self.splice(text, bibfix.replace_entry(text, "a", "article", "a", {"author": "Ann {B}", "year": "2001", "url": "u"}))
        self.assertNotIn("\n", got.replace("\r\n", ""))
        self.assertIn("year    = 2001,\r\n    url     = {u},\r\n}", got)
        for kind, key, fields in (("article", "a b", {}), ("string", "a", {}), ("article", "a", {"title": "x}"}), ("article", "a", {"bad name": "x"})):
            with self.subTest(key=key), self.assertRaises(ValueError):
                bibfix.replace_entry(self.TEXT, "a", kind, key, fields)
        with self.assertRaises(KeyError):
            bibfix.replace_entry(self.TEXT, "zz", "misc", "zz", {})

    def test_delete_takes_its_line_and_one_blank_line(self):
        start, end = bibfix.delete_entry(self.TEXT, "b")
        self.assertEqual(self.TEXT[:start] + self.TEXT[end:], self.TEXT.replace("@book{b, title={B}, year={1999}}\n\n", ""))
        start, end = bibfix.delete_entry(self.TEXT, "c")  # last entry, no trailing newline
        self.assertTrue((self.TEXT[:start] + self.TEXT[end:]).endswith("year={1999}}\n\n"))
        crlf = "@misc{x, a={1}}\r\n\r\n@misc{y, a={2}}\r\n"
        start, end = bibfix.delete_entry(crlf, "x")
        self.assertEqual(crlf[:start] + crlf[end:], "@misc{y, a={2}}\r\n")

    def test_append_matches_eol_and_indent(self):
        at, insert = bibfix.append_entry(self.TEXT, "online", "w", {"title": "{W} x", "url": "http://e", "year": "2020", "note": " "})
        self.assertEqual(at, len(self.TEXT))
        self.assertEqual(insert, "\n\n@online{w,\n  title = {{W} x},\n  url = {http://e},\n  year = {2020},\n}\n")
        at, insert = bibfix.append_entry("@misc{x,\r\n\tnote = {n},\r\n}\r\n", "misc", "y", {"note": "m"})
        self.assertEqual(insert, "\r\n@misc{y,\r\n\tnote = {m},\r\n}\r\n")
        self.assertEqual(bibfix.append_entry("", "misc", "z", {})[1], "@misc{z,\n}\n")
        for text in (self.TEXT, ""):
            at, insert = bibfix.append_entry(text, "misc", "new", {"title": "T"})
            self.assertIn("new", [e.key for e in bibfix.parse(text + insert)])

    def test_entry_for_doi(self):
        with mock.patch.object(bibfix, "get_json", return_value=WORK):
            got = bibfix.entry_for_doi("doi:10.1000/xyz_123")
        self.assertEqual((got["type"], got["fields"]["journal"]), ("article", "Journal of Examples"))
        with self.assertRaises(bibfix.BibLookupError):
            bibfix.entry_for_doi("not a doi")


class LintTests(unittest.TestCase):
    def test_bib_lookup_adds_an_info_finding_and_is_quiet_on_failure(self):
        with tempfile.TemporaryDirectory() as tmp:
            (Path(tmp) / "refs.bib").write_text("@article{k, doi={10.1000/xyz_123}, title={T}}\n@article{z, title={Q}}\n", encoding="utf-8")
            missing = [ci_report.Finding("missing-bib-field", "refs.bib", 1, "x", key) for key in ("k", "z")]
            with mock.patch.object(bibfix, "get_json", side_effect=[WORK, {"message": {"items": []}}]):
                got = ci_report.bib_lookup_findings(Path(tmp), missing)
        self.assertEqual([(f.kind, f.subject, f.level) for f in got], [("bib-suggestion", "k", "info")])
        self.assertIn("year = {2019}", got[0].message)


if __name__ == "__main__":
    unittest.main()
