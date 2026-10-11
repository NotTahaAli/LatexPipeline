"""Editor navigation: the index (scripts/navigate.py), label rename, typeset selection (scripts/fragment.py) and their
endpoints POST /api/symbols, /api/rename and /api/typeset with the view, review and edit roles."""

from __future__ import annotations

import shutil
import subprocess
import unittest
from unittest import mock

import _support  # noqa: F401 - puts scripts/ on sys.path
import fragment
import navigate
import serve
import test_serve as ts

MAIN = r"""\documentclass{article}
\usepackage{graphicx}\graphicspath{{img/}}
\usepackage{mine}
\newcommand{\R}{\mathbb{R}} \def\foo#1{x}
\newtheorem{thm}{Theorem}
\begin{document}
\section{A}\label{sec:a} % TODO fix this
See \ref{sec:a} and \cref{x,sec:a} and \cref{sec:ab}. $\R$ \cite[p.~2]{knuth84, other}
50\% TODO not a note
\begin{figure}\includegraphics{x}\caption{A \emph{nice} plot\label{fig:x}}\end{figure}
\todo[inline]{write {more}}
\input{ch/a}\include{missing}\bibliography{refs}
\begin{thm} x \end{thm} % \label{commented}
\end{document}
"""
BIB = "@string{x = 1}\n@article{knuth84,\n  title = {T}\n}\n"


class Index(unittest.TestCase):
    def setUp(self):
        self.data = navigate.index({"main.tex": MAIN, "refs.bib": BIB, "mine.sty": "\\newcommand\\Z{z}\n"},
                                   ["main.tex", "refs.bib", "mine.sty", "ch/a.tex", "img/x.png"])

    def test_definitions_with_positions(self):
        got = {(d["kind"], d["name"]): (d["file"], d["line"], d["col"]) for d in self.data["defs"]}
        self.assertEqual(got, {
            ("command", "R"): ("main.tex", 4, 12), ("command", "foo"): ("main.tex", 4, 32),
            ("command", "Z"): ("mine.sty", 1, 11), ("env", "thm"): ("main.tex", 5, 12),
            ("label", "sec:a"): ("main.tex", 7, 18), ("label", "fig:x"): ("main.tex", 10, 67),
            ("cite", "knuth84"): ("refs.bib", 2, 9),   # @string is not an entry; a commented \label is no label
        })

    def test_floats_todos_and_graph(self):
        self.assertEqual([(f["kind"], f["caption"], f["label"], f["line"]) for f in self.data["floats"]],
                         [("figure", "A nice plot", "fig:x", 10)])
        self.assertEqual([(t["kind"], t["text"], t["line"]) for t in self.data["todos"]],
                         [("TODO", "fix this", 7), ("todo", "write more", 11)])   # \% TODO is text, not a note
        self.assertEqual([(e["to"], e["kind"], e["missing"]) for e in self.data["edges"]], [
            ("ch/a.tex", "input", False), ("missing", "include", True), ("refs.bib", "bibliography", False),
            ("img/x.png", "graphics", False), ("mine.sty", "package", False)])

    def test_uses_of_one_name_in_utf16_columns(self):
        def uses(what):
            return [(u["line"], u["col"]) for u in navigate.index({"main.tex": MAIN}, [], uses=what)["uses"]]
        self.assertEqual(uses("label:sec:a"), [(8, 9), (8, 28)])   # not \cref{sec:ab}
        self.assertEqual(uses("cite:knuth84"), [(8, 71)])
        self.assertEqual(uses("command:R"), [(8, 55)])   # the definition itself is not a use
        self.assertEqual(uses("env:thm"), [(13, 0)])
        emoji = navigate.index({"a.tex": "\U0001F600 \\ref{k}"}, [], uses="label:k")["uses"][0]
        self.assertEqual((emoji["col"], emoji["len"]), (8, 1))

    def test_caps(self):
        with mock.patch.object(navigate, "MAX_ITEMS", 1):
            data = navigate.index({"main.tex": MAIN}, [], uses="label:sec:a")
        self.assertEqual((len(data["defs"]), data["truncated"], len(data["uses"]), data["uses_truncated"]),
                         (1, True, 1, True))

    def test_aux_numbers(self):
        aux = (r"\newlabel{sec:a}{{1.2}{3}{Intro}{section.1.2}{}}"
               r"\newlabel{sec:a@cref}{{[section][2][1]1.2}{[1][3][]3}}" "\n" r"\newlabel{eq}{{\relax 4}{5}}")
        self.assertEqual(navigate.aux_numbers([aux]), {"sec:a": {"number": "1.2", "page": "3"},
                                                        "eq": {"number": "4", "page": "5"}})

    def test_rename_pattern(self):
        text = r"\label{a} \ref{a} \cref{b, a} \eqref{ab} \cite{a} \pageref*{a}"
        new = navigate.rename_pattern("a").sub(lambda m: m.expand(navigate.rename_template("c:d")), text)
        self.assertEqual(new, r"\label{c:d} \ref{c:d} \cref{b, c:d} \eqref{ab} \cite{a} \pageref*{c:d}")
        for bad in ("", "a b", "a,b", "a}", "\\x", None, 3):
            self.assertFalse(navigate.good_label(bad))


class Api(ts.SharedState, ts.ServerCase):
    def setUp(self):
        ts.SharedState.setUp(self)
        ts.ServerCase.setUp(self)
        self.write("main.tex", "\\section{A}\\label{sec:a}\nSee \\ref{sec:a}.\n\\input{ch}\n")
        self.write("ch.tex", "Also \\cref{sec:a,x}.\n")
        self.write("build.toml", "# \\label{sec:a}\n")
        mock.patch.dict(serve.HISTORY, {"dir": self.base / ".hist"}).start()
        self.tokens = {}

    def call(self, path, body, role=None):
        hdrs = {"Cookie": f"{serve.cookie_name()}={self.tokens[role]}"} if role else {}
        return self.request("POST", path, body, hdrs)

    def test_symbols_reads_disk_or_the_sent_text(self):
        status, data = self.call("/api/symbols?doc=demo", {"uses": "label:sec:a"})
        self.assertEqual(status, 200)
        self.assertEqual([(u["file"], u["line"]) for u in data["uses"]], [("ch.tex", 1), ("main.tex", 2)])
        texts = {"main.tex": "\n\n\\label{new}\n", "x.tex": "\\label{q}"}
        status, data = self.call("/api/symbols?doc=demo", {"texts": texts})
        self.assertEqual([(d["name"], d["line"]) for d in data["defs"]], [("new", 3)])   # x.tex is not a file here
        self.assertEqual(self.call("/api/symbols?doc=demo", {"texts": {"main.tex": 3}})[0], 400)

    def test_label_numbers_from_the_build_or_while_building_the_preview_snapshot(self):
        cache = self.base / "cache"
        mock.patch.object(serve.build, "cache_dir_for", lambda main: cache).start()
        (cache / "ch").mkdir(parents=True)
        (cache / "main.aux").write_text("\\newlabel{sec:a}{{2}{1}}\n\\@input{ch/one.aux}\n\\@input{../x.aux}\n")
        (cache / "ch" / "one.aux").write_text("\\newlabel{eq}{{3}{1}}")
        (self.base / "x.aux").write_text("\\newlabel{outside}{{9}{9}}")
        self.assertEqual(self.call("/api/symbols?doc=demo", {})[1]["numbers"],
                         {"sec:a": {"number": "2", "page": "1"}, "eq": {"number": "3", "page": "1"}})
        (cache / "preview" / "aux").mkdir(parents=True)
        (cache / "preview" / "aux" / "main.aux").write_text("\\newlabel{sec:a}{{1}{1}}")
        with mock.patch.dict(serve.STATE, {"demo": {"status": "building"}}):
            self.assertEqual(self.call("/api/symbols?doc=demo", {})[1]["numbers"],
                             {"sec:a": {"number": "1", "page": "1"}})

    def test_rename_writes_with_history_and_leaves_rooms_to_the_editor(self):
        with mock.patch.object(serve, "room_open", lambda doc, path: path == "main.tex"):
            status, done = self.call("/api/rename?doc=demo", {"old": "sec:a", "new": "sec:intro"})
        self.assertEqual((status, done["rooms"], done["count"]), (200, ["main.tex"], 2))   # build.toml too (owner)
        self.assertEqual((self.root / "ch.tex").read_text(encoding="utf-8"), "Also \\cref{sec:intro,x}.\n")
        self.assertIn("\\label{sec:a}", (self.root / "main.tex").read_text(encoding="utf-8"))
        status, out = self.call("/api/rename?doc=demo", {"old": "sec:a", "new": "sec:intro", "path": "main.tex",
                                                         "text": "\\label{sec:a} \\ref{sec:a}"})
        self.assertEqual((status, out["text"], out["count"]), (200, "\\label{sec:intro} \\ref{sec:intro}", 2))
        self.assertEqual(self.call("/api/rename?doc=demo", {"old": "sec:a", "new": "bad name"})[0], 400)
        self.assertEqual(self.call("/api/rename?doc=demo", {"old": "x", "new": "x"})[0], 400)

    def test_roles(self):
        self.tokens = self.share_on()
        status, data = self.call("/api/symbols?doc=demo", {"uses": "label:sec:a"}, role="view")
        self.assertEqual(status, 200)
        self.assertNotIn("build.toml", [u["file"] for u in data["uses"]])
        self.assertEqual(self.call("/api/rename?doc=demo", {"old": "sec:a", "new": "b"}, role="view")[0], 403)
        self.assertEqual(self.call("/api/typeset?doc=demo", {"text": "x"}, role="view")[0], 403)
        status, done = self.call("/api/rename?doc=demo", {"old": "sec:a", "new": "b"}, role="edit")
        self.assertEqual((status, done["count"]), (200, 3))
        self.assertIn("sec:a", (self.root / "build.toml").read_text(encoding="utf-8"))   # never for a guest
        self.assertEqual(self.call("/api/symbols?doc=other", {}, role="edit")[0], 403)
        query = {"doc": ["demo"]}
        serve.check_permission("review", "POST", "/api/symbols", query)
        for path in ("/api/rename", "/api/typeset"):
            with self.assertRaises(serve.ApiError) as ctx:
                serve.check_permission("review", "POST", path, query)
            self.assertEqual(ctx.exception.status, 403)
        codes = [self.call("/api/typeset?doc=demo", {"text": ""}, role="edit")[0] for _ in range(21)]
        self.assertEqual((codes.count(400), codes.count(429)), (20, 1))

    def test_typeset_returns_the_pdf(self):
        with mock.patch.object(fragment, "typeset", return_value=b"%PDF-1.5 x") as run:
            status, body = self.call("/api/typeset?doc=demo", {"text": "$x$"})
        self.assertEqual((status, body), (200, b"%PDF-1.5 x"))
        self.assertEqual(run.call_args[0], (serve.DOCS["demo"], "$x$"))
        with mock.patch.object(fragment, "typeset", side_effect=fragment.FragmentError("Undefined control sequence.")):
            self.assertEqual(self.call("/api/typeset?doc=demo", {"text": "\\nope"}),
                             (422, {"error": "Undefined control sequence."}))


class Fragment(ts.TempDoc):
    def setUp(self):
        super().setUp()
        self.main = self.write("main.tex", "\\documentclass{article}\n\\usepackage{amsmath}\n"
                                           "\\begin{document}\nBody\n\\end{document}\n")
        mock.patch.object(fragment.build, "cache_dir_for", lambda main: self.base / "cache").start()
        self.addCleanup(mock.patch.stopall)

    def test_bad_input_and_engines(self):
        for bad in ("", "  ", None, "x" * (fragment.MAX_CHARS + 1)):
            with self.assertRaises(fragment.FragmentError):
                fragment.typeset(self.main, bad)
        with mock.patch.object(fragment.build, "read_settings", return_value={"engine": "latex"}), \
                self.assertRaises(fragment.FragmentError):
            fragment.typeset(self.main, "x")

    def test_runs_through_the_sandbox_with_the_preamble_and_cleans_up(self):
        seen = {}

        def run(**spec):
            work = self.base / "cache" / "fragment"
            [folder] = [p for p in work.iterdir() if p.name != "texmf-var"]
            seen["source"] = (folder / "fragment.tex").read_text(encoding="utf-8")
            seen["args"] = spec["args"]
            (folder / "fragment.pdf").write_bytes(b"%PDF")
            return subprocess.CompletedProcess(spec["args"], 0)

        with mock.patch.object(fragment.subprocess, "run", side_effect=run), \
                mock.patch.object(fragment.sandbox, "spawn", wraps=fragment.sandbox.spawn) as spawn, \
                mock.patch.object(fragment.sandbox, "scrub") as scrub:
            self.assertEqual(fragment.typeset(self.main, "$a^2$"), b"%PDF")
        self.assertTrue(seen["source"].startswith("\\documentclass{article}\n\\usepackage{amsmath}\n\\begin{document}"))
        self.assertIn("$a^2$\n\\end{document}", seen["source"])
        self.assertNotIn("Body", seen["source"])
        self.assertNotIn("-shell-escape", seen["args"])
        self.assertEqual(spawn.call_args.kwargs["work"].parent, self.base / "cache" / "fragment")
        scrub.assert_called_once()
        self.assertEqual([p.name for p in (self.base / "cache" / "fragment").iterdir() if p.name != "texmf-var"], [])

    def test_reports_the_first_error_and_one_run_at_a_time(self):
        def run(**spec):
            [folder] = [p for p in (self.base / "cache" / "fragment").iterdir() if p.name != "texmf-var"]
            (folder / "fragment.log").write_text("This is TeX\n./fragment.tex:5: Undefined control sequence.\n")
            return subprocess.CompletedProcess(spec["args"], 1)

        with mock.patch.object(fragment.subprocess, "run", side_effect=run), \
                self.assertRaisesRegex(fragment.FragmentError, "^Undefined control sequence.$"):
            fragment.typeset(self.main, "\\nope")
        with fragment.LOCK, self.assertRaises(fragment.FragmentError) as ctx:
            fragment.typeset(self.main, "x")
        self.assertEqual(ctx.exception.status, 409)

    @unittest.skipUnless(shutil.which("pdflatex"), "needs pdflatex")
    def test_real_run(self):
        self.assertTrue(fragment.typeset(self.main, "$\\sum_{i=1}^n i$").startswith(b"%PDF"))


if __name__ == "__main__":
    unittest.main()
