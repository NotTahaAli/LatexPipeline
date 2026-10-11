// Markdown files in a document folder: highlighting (a small CodeMirror stream mode), a rendered preview built
// from DOM nodes and textContent only (never innerHTML of the file), and "Insert as LaTeX", which converts a
// Markdown selection (headings, emphasis, lists, links, code, quotes) to LaTeX. A small subset of CommonMark on
// purpose: no raw HTML, no images fetched (their alt text is shown). Pure parts are checked by tests/markdown_check.mjs.
import { SPECIAL } from "./tablepaste.js";

const esc = (s) => s.replace(/[\\&%$#_{}~^]/g, (c) => SPECIAL[c]);

export const isMarkdown = (path) => /\.(md|markdown)$/i.test(path || "");

// ---- inline -------------------------------------------------------------------------------------------------------
/** Inline Markdown as nodes: {t: "text"|"code"|"strong"|"em"|"link"|"image", ...}. */
export function inline(s) {
  const out = [];
  const text = (v) => { if (!v) return; const last = out[out.length - 1]; if (last?.t === "text") last.v += v; else out.push({ t: "text", v }); };
  for (let i = 0; i < s.length;) {
    const rest = s.slice(i);
    let m;
    if (rest[0] === "\\" && /^[\\`*_{}[\]()#+\-.!>|~]/.test(rest[1] || "")) { text(rest[1]); i += 2; continue; }
    if ((m = /^(`+)([\s\S]*?[^`])\1(?!`)/.exec(rest))) { out.push({ t: "code", v: m[2].replace(/^ (.*) $/, "$1") }); i += m[0].length; continue; }
    if ((m = /^!\[([^\]]*)\]\(([^)\s]*)(?:\s+"[^"]*")?\)/.exec(rest))) { out.push({ t: "image", alt: m[1], href: m[2] }); i += m[0].length; continue; }
    if ((m = /^\[((?:[^\][]|\[[^\]]*\])*)\]\(([^)\s]*)(?:\s+"[^"]*")?\)/.exec(rest))) { out.push({ t: "link", href: m[2], kids: inline(m[1]) }); i += m[0].length; continue; }
    if ((m = /^<(https?:\/\/[^\s>]+|mailto:[^\s>]+)>/.exec(rest))) { out.push({ t: "link", href: m[1], kids: [{ t: "text", v: m[1].replace(/^mailto:/, "") }] }); i += m[0].length; continue; }
    if ((m = /^(\*\*|__)(?=\S)([\s\S]*?\S)\1/.exec(rest))) { out.push({ t: "strong", kids: inline(m[2]) }); i += m[0].length; continue; }
    if ((m = /^(\*|_)(?=\S)([\s\S]*?\S)\1(?![*_])/.exec(rest)) && !(m[1] === "_" && /\w/.test(s[i - 1] || ""))) { out.push({ t: "em", kids: inline(m[2]) }); i += m[0].length; continue; }
    text(rest[0]); i++;
  }
  return out;
}

// ---- blocks -------------------------------------------------------------------------------------------------------
const LIST = /^(\s*)([-*+]|\d{1,9}[.)])\s+(.*)$/;
/** Markdown as blocks: heading, para, list (items are block lists), code, quote, hr. */
export function parse(src) {
  const lines = String(src).replace(/\r\n?/g, "\n").split("\n"), out = [];
  let i = 0;
  while (i < lines.length) {
    const line = lines[i];
    let m;
    if (!line.trim()) { i++; continue; }
    if ((m = /^\s{0,3}(```+|~~~+)\s*([\w+-]*)/.exec(line))) {
      const fence = m[1], body = [];
      for (i++; i < lines.length && !lines[i].trimStart().startsWith(fence); i++) body.push(lines[i]);
      i++;
      out.push({ t: "code", lang: m[2], v: body.join("\n") });
      continue;
    }
    if ((m = /^\s{0,3}(#{1,6})\s+(.*?)\s*#*\s*$/.exec(line))) { out.push({ t: "heading", level: m[1].length, kids: inline(m[2]) }); i++; continue; }
    if (/^\s{0,3}([-*_])(\s*\1){2,}\s*$/.test(line)) { out.push({ t: "hr" }); i++; continue; }
    if (/^\s{0,3}>/.test(line)) {
      const body = [];
      for (; i < lines.length && /^\s{0,3}>/.test(lines[i]); i++) body.push(lines[i].replace(/^\s{0,3}>\s?/, ""));
      out.push({ t: "quote", kids: parse(body.join("\n")) });
      continue;
    }
    if ((m = LIST.exec(line))) {
      const indent = m[1].length, ordered = /\d/.test(m[2]), items = [];
      while (i < lines.length) {
        const li = LIST.exec(lines[i]);
        if (!li || li[1].length !== indent || /\d/.test(li[2]) !== ordered) break;
        const body = [li[3]];
        for (i++; i < lines.length; i++) {   // continuation and nested lines: indented deeper than the marker
          const l = lines[i];
          if (!l.trim()) { if (i + 1 < lines.length && /^\s/.test(lines[i + 1]) && lines[i + 1].search(/\S/) > indent) { body.push(""); continue; } break; }
          if (l.search(/\S/) <= indent && (LIST.test(l) || !/^\s/.test(l))) break;
          body.push(l.slice(Math.min(l.search(/\S/), indent + li[2].length + 1)));
        }
        items.push(parse(body.join("\n")));
      }
      out.push({ t: "list", ordered, start: ordered ? parseInt(m[2], 10) : 1, items });
      continue;
    }
    const para = [];
    for (; i < lines.length && lines[i].trim() && !/^\s{0,3}(#{1,6}\s|```|~~~|>|([-*_])(\s*\2){2,}\s*$)/.test(lines[i]) && !(para.length && LIST.test(lines[i])); i++) para.push(lines[i].trim());
    out.push({ t: "para", kids: inline(para.join("\n")) });
  }
  return out;
}

// ---- preview: DOM only, text through textContent -----------------------------------------------------------------
/** A link target the preview may open: http(s), mailto, or a relative path; anything else (javascript:, data:) is not a link. */
export function safeHref(href) {
  href = String(href).replace(/[\u0000-\u0020\u007f]/g, "");   // browsers drop these, so "java\tscript:" is javascript:
  return /^(https?:|mailto:)/i.test(href) || (!/^[a-z][a-z0-9+.-]*:/i.test(href) && !/^[\\/]{2}/.test(href)) ? href : null;
}

/** Render Markdown into a DocumentFragment of `doc` (the page's document, or a stand-in in tests). */
export function render(src, doc = document) {
  const make = (tag, kids = [], text) => { const n = doc.createElement(tag); if (text !== undefined) n.textContent = text; for (const k of kids) n.appendChild(k); return n; };
  const inl = (nodes) => nodes.map((n) => {
    if (n.t === "text") return doc.createTextNode(n.v);
    if (n.t === "code") return make("code", [], n.v);
    if (n.t === "strong") return make("strong", inl(n.kids));
    if (n.t === "em") return make("em", inl(n.kids));
    if (n.t === "image") return make("span", [], `[image: ${n.alt || n.href}]`);
    const href = safeHref(n.href), a = make(href ? "a" : "span", inl(n.kids));
    if (href) { a.setAttribute("href", href); a.setAttribute("target", "_blank"); a.setAttribute("rel", "noopener noreferrer"); }
    return a;
  });
  const blocks = (list) => list.map((b) => {
    if (b.t === "heading") return make("h" + b.level, inl(b.kids));
    if (b.t === "para") return make("p", inl(b.kids));
    if (b.t === "hr") return make("hr");
    if (b.t === "code") return make("pre", [make("code", [], b.v)]);
    if (b.t === "quote") return make("blockquote", blocks(b.kids));
    const l = make(b.ordered ? "ol" : "ul", b.items.map((item) => make("li", blocks(item))));
    if (b.ordered && b.start !== 1) l.setAttribute("start", String(b.start));
    return l;
  });
  const frag = doc.createDocumentFragment();
  for (const n of blocks(parse(src))) frag.appendChild(n);
  return frag;
}

// ---- to LaTeX -----------------------------------------------------------------------------------------------------
const HEADINGS = ["section", "subsection", "subsubsection", "paragraph", "subparagraph", "subparagraph"];
const urlArg = (u) => u.replace(/[\\{}]/g, "").replace(/[%#]/g, "\\$&");
const texInline = (nodes) => nodes.map((n) => {
  if (n.t === "text") return esc(n.v.replace(/\n/g, " "));
  if (n.t === "code") return `\\texttt{${esc(n.v)}}`;
  if (n.t === "strong") return `\\textbf{${texInline(n.kids)}}`;
  if (n.t === "em") return `\\emph{${texInline(n.kids)}}`;
  if (n.t === "image") return esc(n.alt);
  const label = texInline(n.kids), url = urlArg(n.href);
  return /^(https?:|mailto:)/i.test(n.href) && (label === esc(n.href) || label === esc(n.href.replace(/^mailto:/, ""))) ? `\\url{${url}}` : `\\href{${url}}{${label}}`;
}).join("");

/** Markdown as LaTeX. A heading level `base` (0: # is \section) shifts every heading. Needs hyperref for links. */
export function toLatex(src, base = 0) {
  const blocks = (list, indent) => list.map((b) => {
    if (b.t === "heading") return `${indent}\\${HEADINGS[Math.min(b.level - 1 + base, HEADINGS.length - 1)]}{${texInline(b.kids)}}`;
    if (b.t === "para") return indent + texInline(b.kids);
    if (b.t === "hr") return `${indent}\\noindent\\rule{\\linewidth}{0.4pt}`;
    if (b.t === "code") return `\\begin{verbatim}\n${b.v.replace(/\\end\{verbatim\}/g, "\\end {verbatim}")}\n\\end{verbatim}`;
    if (b.t === "quote") return `${indent}\\begin{quote}\n${blocks(b.kids, indent + "  ")}\n${indent}\\end{quote}`;
    const env = b.ordered ? "enumerate" : "itemize";
    const start = b.ordered && b.start !== 1 ? `\n${indent}  \\setcounter{enumi}{${b.start - 1}}` : "";
    return `${indent}\\begin{${env}}${start}\n${b.items.map((item) => `${indent}  \\item ` + blocks(item, indent + "  ").replace(/^\s+/, "")).join("\n")}\n${indent}\\end{${env}}`;
  }).join("\n\n");
  return blocks(parse(src), "");
}

// ---- highlighting: a stream mode for StreamLanguage.define ---------------------------------------------------------
export const markdownMode = {
  name: "markdown",
  startState: () => ({ fence: null }),
  copyState: (s) => ({ ...s }),
  token(stream, state) {
    if (stream.sol()) {
      const fence = stream.match(/^\s*(```+|~~~+)/, false);
      if (fence) {
        const mark = /```+|~~~+/.exec(fence[0])[0];
        if (!state.fence) state.fence = mark; else if (mark.startsWith(state.fence)) state.fence = null;
        stream.skipToEnd(); return "meta";
      }
      if (state.fence) { stream.skipToEnd(); return "string"; }
      if (stream.match(/^\s{0,3}#{1,6}\s.*$/)) return "heading";
      if (stream.match(/^\s{0,3}>.*$/)) return "comment";
      if (stream.match(/^\s*([-*+]|\d+[.)])\s/)) return "meta";
    }
    if (state.fence) { stream.skipToEnd(); return "string"; }
    if (stream.match(/^`[^`]+`/)) return "string";
    if (stream.match(/^(\*\*|__)(?=\S).*?\S\1/)) return "strong";
    if (stream.match(/^(\*|_)(?=\S).*?\S\1/)) return "emphasis";
    if (stream.match(/^!?\[[^\]]*\]\([^)]*\)/)) return "link";
    if (stream.match(/^<https?:\/\/[^>\s]+>/)) return "link";
    stream.next();
    stream.eatWhile(/[^`*_![<\\]/);
    return null;
  },
};
