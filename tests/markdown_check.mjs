// Node check for scripts/serve_ui/markdown.js (parser, safe preview, LaTeX converter, highlighting mode).
// Usage: node tests/markdown_check.mjs
import assert from "node:assert/strict";
import { isMarkdown, inline, parse, render, safeHref, toLatex, markdownMode } from "../scripts/serve_ui/markdown.js";

assert.ok(isMarkdown("notes/README.md") && isMarkdown("a.MARKDOWN") && !isMarkdown("main.tex"));

// Inline.
assert.deepEqual(inline("a **b *c* d** `x*y` [l](http://e.org) _d_ snake_case \\*no\\*"), [
  { t: "text", v: "a " }, { t: "strong", kids: [{ t: "text", v: "b " }, { t: "em", kids: [{ t: "text", v: "c" }] }, { t: "text", v: " d" }] }, { t: "text", v: " " },
  { t: "code", v: "x*y" }, { t: "text", v: " " }, { t: "link", href: "http://e.org", kids: [{ t: "text", v: "l" }] }, { t: "text", v: " " },
  { t: "em", kids: [{ t: "text", v: "d" }] }, { t: "text", v: " snake_case *no*" }]);
assert.deepEqual(inline("![a cat](cat.png)"), [{ t: "image", alt: "a cat", href: "cat.png" }]);
assert.deepEqual(inline("<https://x.org/a>")[0].href, "https://x.org/a");

// Blocks.
const src = [
  "# Title", "", "Some *text*", "on two lines.", "", "- one", "- two", "  - nested", "- three", "", "3. third", "4. fourth", "",
  "> quoted **bold**", "", "```python", "print('<b>')", "```", "", "---", "", "<script>alert(1)</script>",
].join("\n");
const blocks = parse(src);
assert.deepEqual(blocks.map((b) => b.t), ["heading", "para", "list", "list", "quote", "code", "hr", "para"]);
assert.equal(blocks[2].items.length, 3);
assert.deepEqual(blocks[2].items[1].map((b) => b.t), ["para", "list"]);   // "two" with its nested list
assert.deepEqual([blocks[3].ordered, blocks[3].start], [true, 3]);
assert.deepEqual(blocks[5], { t: "code", lang: "python", v: "print('<b>')" });

// Preview: a stand-in document records what the renderer does; it never parses HTML.
class Node {
  constructor(tag) { this.tag = tag; this.kids = []; this.attrs = {}; this._text = ""; }
  appendChild(n) { this.kids.push(n); return n; }
  setAttribute(k, v) { this.attrs[k] = v; }
  set textContent(v) { this._text = v; }
  set innerHTML(v) { throw new Error("innerHTML used"); }
  get text() { return this._text + this.kids.map((k) => k.text).join(""); }
}
const doc = {
  createElement: (t) => new Node(t),
  createTextNode: (v) => Object.assign(new Node("#text"), { _text: v }),
  createDocumentFragment: () => new Node("#frag"),
};
const frag = render(src + "\n\n[x](javascript:alert(1)) [z](https://ok.example/) [w](notes.md)", doc);
assert.deepEqual(frag.kids.map((k) => k.tag), ["h1", "p", "ul", "ol", "blockquote", "pre", "hr", "p", "p"]);
assert.equal(frag.kids[7].text, "<script>alert(1)</script>");   // shown as text
assert.equal(frag.kids[3].attrs.start, "3");
const links = frag.kids[8].kids.filter((k) => k.tag !== "#text");
assert.deepEqual(links.map((k) => [k.tag, k.attrs.href]), [["span", undefined], ["a", "https://ok.example/"], ["a", "notes.md"]]);
assert.equal(links[2].attrs.rel, "noopener noreferrer");
assert.equal(safeHref("//evil.example"), null);
assert.equal(safeHref("java\tscript:alert(1)"), null);
assert.equal(safeHref("\x01javascript:x"), null);
assert.equal(safeHref("data:text/html,x"), null);
assert.equal(safeHref("MAILTO:a@b.c"), "MAILTO:a@b.c");

// To LaTeX.
assert.equal(toLatex("# Intro\n\nCosts 50% & *more* than `a_b`, see [docs](https://e.org/a#b%20c)."),
  "\\section{Intro}\n\nCosts 50\\% \\& \\emph{more} than \\texttt{a\\_b}, see \\href{https://e.org/a\\#b\\%20c}{docs}.");
assert.equal(toLatex("## Sub\n### Subsub\n#### Para"), "\\subsection{Sub}\n\n\\subsubsection{Subsub}\n\n\\paragraph{Para}");
assert.equal(toLatex("# A", 1), "\\subsection{A}");
assert.equal(toLatex("- **one**\n- two\n  1. a\n  2. b"),
  "\\begin{itemize}\n  \\item \\textbf{one}\n  \\item two\n\n  \\begin{enumerate}\n    \\item a\n    \\item b\n  \\end{enumerate}\n\\end{itemize}");
assert.equal(toLatex("5. five\n6. six"), "\\begin{enumerate}\n  \\setcounter{enumi}{4}\n  \\item five\n  \\item six\n\\end{enumerate}");
assert.equal(toLatex("```\n\\end{verbatim} $x$\n```"), "\\begin{verbatim}\n\\end {verbatim} $x$\n\\end{verbatim}");
assert.equal(toLatex("> a quote"), "\\begin{quote}\n  a quote\n\\end{quote}");
assert.equal(toLatex("<https://e.org/x_y>"), "\\url{https://e.org/x_y}");
assert.equal(toLatex("a\\b {c} ~ ^"), "a\\textbackslash{}b \\{c\\} \\textasciitilde{} \\textasciicircum{}");
assert.equal(toLatex("***"), "\\noindent\\rule{\\linewidth}{0.4pt}");

// Highlighting: the stream mode's token names (a minimal StringStream stand-in).
function tokensOf(lines) {
  const state = markdownMode.startState(), out = [];
  for (const line of lines) {
    const stream = {
      string: line, pos: 0, start: 0,
      sol() { return this.pos === 0; }, eol() { return this.pos >= this.string.length; },
      match(re, consume = true) { const m = this.string.slice(this.pos).match(re); if (!m || m.index > 0) return null; if (consume) this.pos += m[0].length; return m; },
      skipToEnd() { this.pos = this.string.length; }, next() { return this.string[this.pos++]; },
      eatWhile(re) { const s = this.pos; while (this.pos < this.string.length && re.test(this.string[this.pos])) this.pos++; return this.pos > s; },
    };
    while (!stream.eol()) { stream.start = stream.pos; const t = markdownMode.token(stream, state); out.push([line.slice(stream.start, stream.pos), t]); }
  }
  return out.filter(([, t]) => t);
}
assert.deepEqual(tokensOf(["# Head", "a **b** and *c* `d` [e](f)", "```", "# not a head", "```", "- item"]), [
  ["# Head", "heading"], ["**b**", "strong"], ["*c*", "emphasis"], ["`d`", "string"], ["[e](f)", "link"],
  ["```", "meta"], ["# not a head", "string"], ["```", "meta"], ["- ", "meta"]]);

console.log("markdown ok");
