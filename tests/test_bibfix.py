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
        self.assertEqual([e.key for e in bibfix.parse("@article{a, title={oops\n@article{b, year=1}")], ["b"])


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
