// Node check for scripts/serve_ui/edits.js (the editing tools' pure logic; edittools.js needs a browser).
// Usage: node tests/edits_check.mjs
import assert from "node:assert/strict";
import * as E from "../scripts/serve_ui/edits.js";

// Apply {from, to, insert} changes given against the original text (CodeMirror's semantics).
const apply = (text, changes) => changes.map((c, i) => ({ ...c, i })).sort((a, b) => b.from - a.from || b.i - a.i).reduce((t, c) => t.slice(0, c.from) + c.insert + t.slice(c.to ?? c.from), text);
const at = (marked) => ({ text: marked.replace("|", ""), pos: marked.indexOf("|") });

// Math mode.
const math = (m) => { const { text, pos } = at(m); return E.inMath(text, pos); };
assert.ok(math("a $x| y$"));
assert.ok(!math("a $x$ |y"));
assert.ok(math("\\[ a |"));
assert.ok(!math("\\[ a \\] |"));
assert.ok(math("\\begin{align*}\n x &= |"));
assert.ok(!math("\\begin{align*} x \\end{align*} |"));
assert.ok(!math("$x \\text{and |} y$"));
assert.ok(math("$x \\text{and $|$} y$"));   // math inside \text inside math
assert.ok(!math("50\\$ and |"));
assert.ok(!math("% $ comment\nplain |"));
assert.ok(!math("$x\n\nnew paragraph |"));   // math never runs across a blank line
assert.ok(math("$$ a |"));

// Prefix shortcuts.
assert.deepEqual(E.prefixExpansion("x + @", "a"), { len: 1, insert: "\\alpha" });
assert.deepEqual(E.prefixExpansion("x + @v", "e"), { len: 2, insert: "\\varepsilon" });
assert.deepEqual(E.prefixExpansion("@", "8"), { len: 1, insert: "\\infty" });
assert.equal(E.prefixExpansion("\\@", "a"), null);   // \@ is a command
assert.equal(E.prefixExpansion("x", "a"), null);
assert.equal(E.prefixExpansion("@", "j"), null);

// Bold toggle: wrap, then unwrap from inside, from the selected group, and with the cursor in it.
let t = "say hello now";
let r = E.toggleCommand(t, 4, 9, "textbf");
t = apply(t, r.changes);
assert.equal(t, "say \\textbf{hello} now");
assert.equal(t.slice(r.anchor, r.head), "hello");
r = E.toggleCommand(t, r.anchor, r.head, "textbf");
assert.equal(apply(t, r.changes), "say hello now");
assert.equal(apply(t, E.toggleCommand(t, 4, 18, "textbf").changes), "say hello now");
assert.equal(apply(t, E.toggleCommand(t, 14, 14, "textbf").changes), "say hello now");
assert.equal(apply("a \\emph{b} c", E.toggleCommand("a \\emph{b} c", 9, 9, "textbf").changes), "a \\emph{b\\textbf{}} c");
assert.equal(apply("x", E.wrapEnv("x", 0, 1, "center").changes), "\\begin{center}\nx\n\\end{center}");

// Surrounding environment: rename, star, delete.
const doc = "\\begin{figure}[htbp]\n  \\begin{center}\n    body\n  \\end{center}\n\\end{figure}\n";
let env = E.surroundingEnv(doc, doc.indexOf("body"));
assert.equal(env.name, "center");
assert.equal(apply(doc, E.renameEnv(env, "flushleft")), doc.replace(/center/g, "flushleft"));
assert.equal(apply(doc, E.toggleStar(E.surroundingEnv(doc, 2))), doc.replace(/figure/g, "figure*"));
assert.equal(apply(doc, E.deleteEnv(doc, env)), "\\begin{figure}[htbp]\n    body\n\\end{figure}\n");
assert.equal(apply(doc, E.deleteEnv(doc, E.surroundingEnv(doc, 3))), "  \\begin{center}\n    body\n  \\end{center}\n");
assert.equal(E.surroundingEnv("% \\begin{x}\nno", 13), null);
assert.equal(E.surroundingEnv(doc, doc.length), null);
const star = "\\begin{equation*} a \\end{equation*}";
assert.equal(apply(star, E.toggleStar(E.surroundingEnv(star, 19))), "\\begin{equation} a \\end{equation}");

// Delete the surrounding command, keeping its argument.
assert.equal(apply("a \\footnote[2]{b {c} d} e", E.deleteCommand("a \\footnote[2]{b {c} d} e", 16)), "a b {c} d e");
assert.equal(apply("\\textbf{x \\emph{y} z}", E.deleteCommand("\\textbf{x \\emph{y} z}", 16)), "\\textbf{x y z}");
assert.equal(E.deleteCommand("{plain group}", 3), null);
assert.equal(E.deleteCommand("\\begin{x} y", 8), null);

// \left / \right.
const lr = (m) => { const { text, pos } = at(m); const c = E.toggleLeftRight(text, pos); return c && apply(text, c); };
assert.equal(lr("$f(x + (|y))$"), "$f(x + \\left(y\\right))$");
assert.equal(lr("$\\left( a | \\right)$"), "$( a  )$");
assert.equal(lr("$\\left\\{ a| \\right.$"), "$\\{ a $");
assert.equal(lr("$\\{ a | \\}$"), "$\\left\\{ a  \\right\\}$");
assert.equal(lr("\\[ a [b|] \\]"), "\\[ a \\left[b\\right] \\]");
assert.equal(lr("no brackets |"), null);
assert.equal(lr("\\leftarrow ( | )"), "\\leftarrow \\left(  \\right)");

// Rewrap.
assert.equal(E.rewrap("  one two three four five six\n  seven\n", 16), "  one two three\n  four five six\n  seven\n");
assert.equal(E.rewrap("a % comment\nb", 80), null);
assert.equal(E.rewrap("50\\% of it", 80), "50\\% of it");
assert.equal(E.rewrap("\\begin{x}\ny", 80), null);
assert.equal(E.rewrap("averyveryverylongword x", 5), "averyveryverylongword\nx");

// Unicode to LaTeX.
assert.equal(E.unicodeToLatex("“Café” — naïve…"), "``Caf\\'e'' --- na\\\"\\i ve\\dots{}".replace("\\\"\\i ve", "\\\"{\\i}ve"));
assert.equal(E.unicodeToLatex("Café", { pdflatex: false }), "Café");
assert.equal(E.unicodeToLatex("‘ok’ – fine", { pdflatex: false }), "`ok' -- fine");
assert.equal(E.unicodeToLatex("ça Straße"), "\\c{c}a Stra\\ss{}e");
assert.equal(E.unicodeToLatex("α+βx ≤ ∞", { math: true }), "\\alpha+\\beta x \\leq \\infty");
assert.equal(E.unicodeToLatex("“x”", { math: true }), "“x”");   // quotes are not math
assert.equal(E.unicodeToLatex("plain ascii"), "plain ascii");

// .editorconfig.
const ec = "root = true\n# c\n[*]\nindent_style = space\nindent_size = 4\ninsert_final_newline = true\n[*.{tex,bib}]\ntrim_trailing_whitespace = true\n[Makefile]\nindent_style = tab\n[chapters/**.tex]\nindent_size = 2\n";
assert.deepEqual(E.editorconfigFor(ec, "main.tex"), { indentUnit: "    ", trim: true, finalNewline: true });
assert.deepEqual(E.editorconfigFor(ec, "chapters/a/b.tex"), { indentUnit: "  ", trim: true, finalNewline: true });
assert.deepEqual(E.editorconfigFor(ec, "x/Makefile"), { indentUnit: "\t", finalNewline: true });
assert.deepEqual(E.editorconfigFor("", "main.tex"), {});
assert.deepEqual(E.editorconfigFor("[*.tex]\nindent_style=tab\ntab_width=8", "a.tex"), { indentUnit: "\t", tabSize: 8 });
assert.ok(E.globRegex("/top.tex").test("top.tex") && !E.globRegex("/top.tex").test("a/top.tex"));
assert.ok(E.globRegex("a?c.[ch]").test("d/abc.h"));
assert.deepEqual(E.saveFixes("a  \nb\t\nc", { trim: true, finalNewline: true }, new Set([2])), [{ from: 1, to: 3, insert: "" }, { from: 8, to: 8, insert: "\n" }]);
assert.deepEqual(E.saveFixes("x\n", { trim: true, finalNewline: true }), []);

// Path completion.
const files = [{ path: "main.tex" }, { path: "ch/intro.tex" }, { path: "fig/a.png" }, { path: "fig/b.pdf" }, { path: "refs.bib" }, { path: "notes.txt" }];
assert.deepEqual(E.pathOptions("includegraphics", files), ["fig/a.png", "fig/b.pdf"]);
assert.deepEqual(E.pathOptions("input", files), ["ch/intro"]);
assert.deepEqual(E.pathOptions("bibliography", files), ["refs"]);
assert.deepEqual(E.pathOptions("addbibresource", files), ["refs.bib"]);
assert.equal(E.PATH_CMD.exec("\\includegraphics[width=3cm]{fig/")[1], "includegraphics");
assert.equal(E.PATH_CMD.exec("\\bibliography{a,b")[2], "a,b");
assert.equal(E.PATH_CMD.exec("\\input{x} more"), null);

// Snippets: LaTeX's own \{ and #{ survive CodeMirror's template parser.
assert.equal(E.snippetTemplate("\\{${1:x}\\} #{"), "\\\\{${1:x}\\\\} #\\{");
assert.equal(E.snippetAt("text fig", E.DEFAULT_SNIPPETS).trigger, "fig");
assert.equal(E.snippetAt("\\fig", E.DEFAULT_SNIPPETS), null);
assert.equal(E.snippetAt("config", E.DEFAULT_SNIPPETS), null);
assert.deepEqual(E.cleanSnippets([{ trigger: "ok", body: "b" }, { trigger: "\\bad", body: "b" }, { trigger: "x" }, null]), [{ trigger: "ok", body: "b" }]);

console.log("edits ok");
