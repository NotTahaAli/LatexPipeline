// Node check for scripts/serve_ui/latex.js (folding, code check, hover lookup, completion matching).
// Usage: node latex_check.mjs <path to latex.js>
import assert from "node:assert/strict";
import path from "node:path";
import { pathToFileURL } from "node:url";

const { scan, foldRange, keyAt, matchEntries, mathTex } = await import(pathToFileURL(path.resolve(process.argv[2])).href);

// A minimal stand-in for CodeMirror's Text: lines, lineAt, line(n), sliceString.
function doc(text) {
  const starts = [0];
  for (let i = 0; i < text.length; i++) if (text[i] === "\n") starts.push(i + 1);
  const line = (n) => { const from = starts[n - 1], to = n < starts.length ? starts[n] - 1 : text.length; return { number: n, from, to, text: text.slice(from, to) }; };
  return { length: text.length, lines: starts.length, line, sliceString: (a, b) => text.slice(a, b),
    lineAt: (pos) => { let n = starts.length; while (starts[n - 1] > pos) n--; return line(n); } };
}
const messages = (t) => scan(t).problems.map((p) => p.message);
const fold = (text, n) => { const d = doc(text), l = d.line(n), r = foldRange(d, l.from, l.to); return r && text.slice(r.from, r.to); };

// Code check: what is balanced passes.
const good = String.raw`\documentclass{article}
\newcommand{\be}{\begin{equation}}
\newenvironment{half}{\begin{center}\left(}{\right)\end{center}}
\def\dollar#1{$#1}
\begin{document}
Text $a^{b}$ and \(x\) and $$y$$ and \[ \left\{ z \right. \] 50\% \{ \$ % $ { \begin{x}
\verb|{$| and \url{http://x/%7B$} \\[2pt]
\begin{verbatim}
\end{itemize} { $
\end{verbatim}
\begin{align} a &= b \\ c \end{align}
%%begin novalidate
\begin{broken} {
%%end novalidate
\end{document}
} after the end
`;
assert.deepEqual(messages(good), []);

// Each kind of problem is reported once, at the right place.
assert.deepEqual(messages("\\begin{itemize}\n\\item a\n"), ["\\begin{itemize} is never closed"]);
assert.deepEqual(messages("\\begin{align}\na\n"), ["\\begin{align} is never closed"]);
assert.deepEqual(messages("\\begin{itemize}\n\\end{enumerate}\n\\end{itemize}"), ["\\end{enumerate} does not match \\begin{itemize}"]);
assert.deepEqual(messages("a } b"), ["} without {"]);
assert.deepEqual(messages("a { b"), ["{ is never closed"]);
assert.deepEqual(messages("$\\left( x$"), ["\\left is never closed"]);
assert.deepEqual(messages("$ x \\right)$"), ["\\right without \\left"]);
assert.deepEqual(messages("a $b\n\nc"), ["$ is never closed"]);   // math may not cross a paragraph
assert.deepEqual(messages("\\[ x"), ["\\[ is never closed"]);
assert.deepEqual(messages("x \\)"), ["\\) without \\("]);
const p = scan("ok\n\\begin{center}\n").problems[0];
assert.equal("ok\n\\begin{center}\n".slice(p.from, p.to), "\\begin{center}");
assert.deepEqual(messages("%%novalidate\n\\begin{x}"), []);

// Math spans for the hover preview.
const m = scan("a $x^2$ b \\begin{align*} a &= b \\label{e} \\end{align*}").math;
assert.deepEqual(m.map((x) => [x.tex, x.display]), [["x^2", false], ["\\begin{aligned}a &= b\\end{aligned}", true]]);
assert.equal(mathTex("gather", "a \\\\ b"), "\\begin{gathered}a \\\\ b\\end{gathered}");

// Folding: sections run to the next heading of the same or a higher level.
const text = String.raw`\documentclass{article}
\usepackage{x}
\begin{document}
\section{A}
one
\subsection{A1}
two
\section{B}
\begin{itemize}
  \item x % \end{itemize}
\end{itemize}
% region notes
hidden
% endregion
\end{document}`;
assert.equal(fold(text, 1), "\n\\usepackage{x}");
assert.equal(fold(text, 4), "\none\n\\subsection{A1}\ntwo");
assert.equal(fold(text, 6), "\ntwo");
assert.equal(fold(text, 9), "\n  \\item x % \\end{itemize}\n");
assert.equal(fold(text, 12), "\nhidden");
assert.equal(fold(text, 5), null);
assert.equal(fold("\\begin{center} x \\end{center}\n", 1), null);

// Hover lookup of \cite keys and \ref labels.
const line = "see \\cite[p.~2]{smith20, doe21} and \\ref{fig:a}";
assert.deepEqual(keyAt(line, 100, 100 + line.indexOf("doe")), { kind: "cite", key: "doe21", from: 100 + line.indexOf("doe"), to: 100 + line.indexOf("doe") + 5 });
assert.equal(keyAt(line, 0, line.indexOf("fig") + 2).key, "fig:a");
assert.equal(keyAt(line, 0, 1), null);

// Completion matches any part of the key, author or title.
const bib = { smith20: { author: "Smith, A.", title: "Neural fields" }, doe21: { author: "Doe, J.", title: "Graph Smithing" } };
const text2 = (v) => `${v.author} ${v.title}`;
assert.deepEqual(matchEntries(bib, "smith", text2).map((e) => e.key), ["smith20", "doe21"]);
assert.deepEqual(matchEntries(bib, "fields neu", text2).map((e) => e.key), ["smith20"]);
assert.deepEqual(matchEntries(bib, "e21", text2).map((e) => e.key), ["doe21"]);
assert.equal(matchEntries(bib, "", text2).length, 2);

// Large input stays fast (the check runs after every pause in typing).
const big = "\\section{S}\nSome $x$ text {with} \\begin{itemize}\\item a\\end{itemize}\n".repeat(20000);
const t0 = performance.now();
assert.deepEqual(scan(big).problems, []);
assert.ok(performance.now() - t0 < 2000, "scan of 1.4 MB took too long");
console.log("latex_check: ok");
