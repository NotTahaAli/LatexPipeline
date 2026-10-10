// Prose <-> LaTeX conversion for the rich-text paragraph panel. Pure functions, no DOM,
// so the same code runs in the browser and under node (tests/test_serve.py).
//
// Editable subset: plain text, \textbf{} \textit{} \emph{} (nested), and the escapes
// \% \& \$ \# \_ \{ \}. Anything else (macros, math, comments, ~, ^, unbalanced braces)
// makes the paragraph read-only, because converting it back could not be lossless.

const ESCAPES = { "%": "%", "&": "&", "$": "$", "#": "#", "_": "_", "{": "{", "}": "}" };
const WRAP = { textbf: "b", textit: "i", emph: "em" };
const TAG_TO_CMD = { b: "textbf", strong: "textbf", i: "textit", em: "emph" };

/** LaTeX source -> {nodes} | {error}. Node: {text} | {tag, children}. */
export function parse(src) {
  let i = 0;
  function run(closing) {
    const nodes = [];
    let buf = "";
    const flush = () => { if (buf) nodes.push({ text: buf }); buf = ""; };
    while (i < src.length) {
      const c = src[i];
      if (c === "\\") {
        const next = src[i + 1];
        if (next && ESCAPES[next] !== undefined) { buf += ESCAPES[next]; i += 2; continue; }
        const m = /^\\([A-Za-z]+)\{/.exec(src.slice(i));
        if (m && WRAP[m[1]]) {
          flush(); i += m[0].length;
          nodes.push({ tag: WRAP[m[1]], children: run(true) });
          continue;
        }
        throw new Error(m ? `\\${m[1]}` : `\\${next ?? ""}`);
      }
      if (c === "}") {
        if (!closing) throw new Error("unbalanced }");
        i++; flush(); return nodes;
      }
      if ("{$%&#_^~".includes(c)) throw new Error(c === "~" ? "~ (non-breaking space)" : c);
      buf += c; i++;
    }
    if (closing) throw new Error("unbalanced {");
    flush();
    return nodes;
  }
  try { return { nodes: run(false) }; } catch (e) { return { error: e.message }; }
}

const escapeText = (s) => s.replace(/[%&$#_{}]/g, (c) => "\\" + c);
const escapeHtml = (s) => s.replace(/&/g, "&amp;").replace(/</g, "&lt;").replace(/>/g, "&gt;");

export function toHtml(nodes) {
  return nodes.map((n) => n.text !== undefined ? escapeHtml(n.text) : `<${n.tag}>${toHtml(n.children)}</${n.tag}>`).join("");
}

export function toLatex(nodes) {
  return nodes.map((n) => n.text !== undefined ? escapeText(n.text)
    : `\\${n.tag === "b" ? "textbf" : n.tag === "i" ? "textit" : "emph"}{${toLatex(n.children)}}`).join("");
}

/** Editor HTML (innerHTML of the contentEditable) -> LaTeX. Unknown tags are transparent. */
export function htmlToLatex(html) {
  const out = [];
  const stack = [];
  const re = /<(\/?)([a-zA-Z0-9]+)[^>]*>|([^<]+)/g;
  let m;
  while ((m = re.exec(html))) {
    if (m[3] !== undefined) {
      out.push(escapeText(m[3].replace(/&nbsp;/g, " ").replace(/&lt;/g, "<").replace(/&gt;/g, ">").replace(/&quot;/g, '"').replace(/&amp;/g, "&")));
      continue;
    }
    const tag = m[2].toLowerCase();
    if (tag === "br") { out.push("\n"); continue; }
    const cmd = TAG_TO_CMD[tag];
    if (m[1]) { if (stack.pop()) out.push("}"); }
    else if (!/\/>$/.test(m[0]) && !["p", "hr", "img"].includes(tag)) { stack.push(cmd || null); if (cmd) out.push(`\\${cmd}{`); }
  }
  return out.join("");
}

/** {html} when the paragraph is editable and survives the round trip, else {error}. */
export function toEditor(src) {
  const parsed = parse(src);
  if (parsed.error) return { error: `Contains ${parsed.error}, which the rich-text panel cannot edit without changing it.` };
  if (toLatex(parsed.nodes) !== src) return { error: "Cannot be converted back to LaTeX unchanged." };
  return { html: toHtml(parsed.nodes) };
}

/** Paragraph around `pos`: the run of non-blank lines. -> {from, to, text} (text may be ""). */
export function paragraphAt(doc, pos) {
  let from = pos, to = pos;
  const blank = /\n[ \t]*\n/;
  const before = doc.slice(0, pos), after = doc.slice(pos);
  const b = [...before.matchAll(/\n[ \t]*\n/g)].pop();
  from = b ? b.index + b[0].length : 0;
  const a = blank.exec(after);
  to = a ? pos + a.index : doc.length;
  return { from, to, text: doc.slice(from, to) };
}
