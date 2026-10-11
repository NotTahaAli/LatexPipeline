// Pure text logic of the editor's editing tools (edittools.js is the CodeMirror glue): math prefix shortcuts,
// bold/italic toggles and wrapping, structural edits on the surrounding environment, command or \left/\right pair,
// paragraph rewrapping, Unicode to LaTeX, .editorconfig, path completion and user snippets. Every function takes
// plain strings and returns plain data ({from, to, insert} changes against the text given), so
// tests/edits_check.mjs runs them in node.

const escaped = (text, i) => { let n = 0; while (i - n - 1 >= 0 && text[i - n - 1] === "\\") n++; return n % 2 === 1; };
const MATH_ENV = /^(?:equation|align|gather|multline|eqnarray|displaymath|math|flalign|alignat|dmath)\*?$/;
const TEXT_IN_MATH = /^(?:text|textrm|textit|textbf|textsf|texttt|mbox|hbox|intertext|shortintertext|operatorname)$/;

/** True when pos (in text) is in math mode: $..$, $$..$$, \(..\), \[..\], a display math environment; \text{..} inside math is text. */
export function inMath(text, pos) {
  // Math cannot run across a blank line, so the paragraph is enough (a display environment's \begin is in it).
  let i = 0;
  for (const m of text.slice(Math.max(0, pos - 20000), pos).matchAll(/\n[ \t]*\n/g)) i = Math.max(0, pos - 20000) + m.index + m[0].length;
  if (pos - 20000 > 0 && !i) i = pos - 20000;
  const stack = [];   // {k: "math", close} | {k: "text"} | {k: "brace"}
  const mode = () => { for (let k = stack.length - 1; k >= 0; k--) if (stack[k].k !== "brace") return stack[k].k; return "text"; };
  while (i < pos) {
    const c = text[i];
    if (c === "%") { const e = text.indexOf("\n", i); if (e < 0 || e >= pos) return false; i = e + 1; continue; }
    if (c === "\\") {
      const m = /^\\(?:([A-Za-z]+)|.)/s.exec(text.slice(i, i + 64)), name = m[1], s = text[i + 1];
      if (!name && (s === "(" || s === "[")) stack.push({ k: "math", close: "\\" + (s === "(" ? ")" : "]") });
      else if (!name && (s === ")" || s === "]")) { const k = stack.findLastIndex((x) => x.close === "\\" + s); if (k >= 0) stack.splice(k); }
      else if (name === "begin" || name === "end") {
        const arg = /^\s*\{([^{}\n]*)\}/.exec(text.slice(i + m[0].length, i + m[0].length + 64));
        if (arg && MATH_ENV.test(arg[1].trim())) {
          if (name === "begin") stack.push({ k: "math", close: arg[1].trim() });
          else { const k = stack.findLastIndex((x) => x.close === arg[1].trim()); if (k >= 0) stack.splice(k); }
        }
        if (arg) { i += m[0].length + arg[0].length; continue; }
      } else if (name && TEXT_IN_MATH.test(name) && mode() === "math") {
        const open = /^\s*\{/.exec(text.slice(i + m[0].length, i + m[0].length + 20));
        if (open) { stack.push({ k: "text" }); i += m[0].length + open[0].length; continue; }
      }
      i += m[0].length; continue;
    }
    if (c === "{") stack.push({ k: "brace" });
    else if (c === "}") { const k = stack.findLastIndex((x) => x.k === "brace" || x.k === "text"); if (k >= 0) stack.splice(k); }
    else if (c === "$") {
      const sym = text[i + 1] === "$" ? "$$" : "$";
      const k = stack.findLastIndex((x) => x.close === sym);
      if (k >= 0 && mode() === "math") stack.splice(k); else stack.push({ k: "math", close: sym });
      i += sym.length; continue;
    }
    i++;
  }
  return mode() === "math";
}

/**
 * Math prefix shortcuts, typed in math mode: @ and a key. The table follows LaTeX Workshop's at-suggestions
 * (https://github.com/James-Yu/LaTeX-Workshop/blob/master/data/at-suggestions.json, MIT), symbols only.
 */
export const MATH_PREFIX = {
  "@a": "\\alpha", "@b": "\\beta", "@c": "\\chi", "@d": "\\delta", "@e": "\\epsilon", "@ve": "\\varepsilon", "@f": "\\phi",
  "@vf": "\\varphi", "@g": "\\gamma", "@h": "\\eta", "@i": "\\iota", "@k": "\\kappa", "@l": "\\lambda", "@m": "\\mu",
  "@n": "\\nu", "@p": "\\pi", "@vp": "\\varpi", "@q": "\\theta", "@vq": "\\vartheta", "@r": "\\rho", "@vr": "\\varrho",
  "@s": "\\sigma", "@vs": "\\varsigma", "@t": "\\tau", "@u": "\\upsilon", "@o": "\\omega", "@x": "\\xi", "@y": "\\psi",
  "@z": "\\zeta", "@D": "\\Delta", "@F": "\\Phi", "@G": "\\Gamma", "@Q": "\\Theta", "@L": "\\Lambda", "@P": "\\Pi",
  "@X": "\\Xi", "@Y": "\\Psi", "@S": "\\Sigma", "@U": "\\Upsilon", "@W": "\\Omega",
  "@.": "\\cdot", "@8": "\\infty", "@6": "\\partial", "@@": "\\circ", "@0": "^\\circ", "@=": "\\equiv", "@*": "\\times",
  "@<": "\\leq", "@>": "\\geq", "@+": "\\bigcup", "@-": "\\bigcap", "@&": "\\wedge", "@,": "\\nonumber",
};

/** The shortcut that typing ch completes, given the text just before the cursor: {len, insert} (len characters before the cursor go), or null. */
export function prefixExpansion(before, ch) {
  for (const n of [2, 1]) {
    const key = before.slice(-n) + ch;
    if (before.length >= n && key[0] === "@" && MATH_PREFIX[key] && !escaped(before, before.length - n)) return { len: n, insert: MATH_PREFIX[key] };
  }
  return null;
}

/** Index of the brace matching the { at open, or -1. */
function closeOf(text, open) {
  let d = 0;
  for (let j = open; j < text.length; j++) {
    const c = text[j];
    if (c === "\\") { j++; continue; }
    if (c === "{") d++;
    else if (c === "}" && --d === 0) return j;
  }
  return -1;
}

/**
 * The innermost \name[..]{..} group around pos (pos between its braces): {name, from, open, close} with from at the
 * backslash, open/close at the braces. names limits which commands count.
 */
export function enclosingCommand(text, pos, names) {
  let depth = 0;
  for (let i = pos - 1; i >= Math.max(0, pos - 20000); i--) {
    const c = text[i];
    if ((c !== "{" && c !== "}") || escaped(text, i)) continue;
    if (c === "}") { depth++; continue; }
    if (depth > 0) { depth--; continue; }
    const head = /\\([A-Za-z]+)\*?((?:\[[^\]\n]*\])*)\s*$/.exec(text.slice(Math.max(0, i - 200), i));
    if (!head || (names && !names.includes(head[1])) || head[1] === "begin" || head[1] === "end") continue;
    const close = closeOf(text, i);
    if (close >= pos) return { name: head[1], from: i - head[0].length, open: i, close };
  }
  return null;
}

/** Bold/italic toggle: unwrap \name{..} when the selection is in or is one, else wrap the selection. {changes, anchor, head}. */
export function toggleCommand(text, from, to, name) {
  const open = `\\${name}{`;
  if (from < to && text.slice(from - open.length, from) === open && text[to] === "}" && closeOf(text, from - 1) === to)
    return { changes: [{ from: from - open.length, to: from, insert: "" }, { from: to, to: to + 1, insert: "" }], anchor: from - open.length, head: to - open.length };
  if (from < to && text.startsWith(open, from) && closeOf(text, from + open.length - 1) === to - 1)
    return { changes: [{ from, to: from + open.length, insert: "" }, { from: to - 1, to, insert: "" }], anchor: from, head: to - 1 - open.length };
  const around = enclosingCommand(text, from, [name]);
  if (around && around.close >= to && around.from + open.length - 1 === around.open)
    return { changes: [{ from: around.from, to: around.open + 1, insert: "" }, { from: around.close, to: around.close + 1, insert: "" }], anchor: from - open.length, head: to - open.length };
  return wrapCommand(text, from, to, name);
}

export const wrapCommand = (text, from, to, name) =>
  ({ changes: [{ from, insert: `\\${name}{` }, { from: to, insert: "}" }], anchor: from + name.length + 2, head: to + name.length + 2 });

export function wrapEnv(text, from, to, name) {
  const b = `\\begin{${name}}\n`, e = `${from < to && text[to - 1] === "\n" ? "" : "\n"}\\end{${name}}`;
  return { changes: [{ from, insert: b }, { from: to, insert: e }], anchor: from + b.length, head: to + b.length };
}

/** The innermost \begin{x}..\end{x} pair around pos: {name, begin: {from, to, nameFrom, nameTo}, end: {...}}, or null. */
export function surroundingEnv(text, pos) {
  const stack = [];
  for (const m of text.matchAll(/\\(begin|end)\s*\{([^{}\n]*)\}/g)) {
    const line = text.lastIndexOf("\n", m.index) + 1;
    if (/(?<!\\)(?:\\\\)*%/.test(text.slice(line, m.index))) continue;   // commented out
    const nameFrom = m.index + m[0].indexOf("{") + 1, tok = { name: m[2], from: m.index, to: m.index + m[0].length, nameFrom, nameTo: nameFrom + m[2].length };
    if (m[1] === "begin") { stack.push(tok); continue; }
    const k = stack.findLastIndex((b) => b.name === tok.name);
    if (k < 0) continue;
    const begin = stack[k];
    stack.splice(k);
    if (begin.from <= pos && pos <= tok.to) return { name: tok.name, begin, end: tok };
  }
  return null;
}

export function renameEnv(env, name) {
  return [{ from: env.begin.nameFrom, to: env.begin.nameTo, insert: name }, { from: env.end.nameFrom, to: env.end.nameTo, insert: name }];
}

export const toggleStar = (env) => renameEnv(env, env.name.endsWith("*") ? env.name.slice(0, -1) : env.name + "*");

/** Remove \begin{x}[opt] and \end{x}, keeping the body; a line left blank by either goes too. */
export function deleteEnv(text, env) {
  const opt = /^(?:\[[^\]\n]*\])*/.exec(text.slice(env.begin.to))[0];
  const cut = (from, to) => {
    const ls = text.lastIndexOf("\n", from - 1) + 1, le = text.indexOf("\n", to) < 0 ? text.length : text.indexOf("\n", to);
    if (!text.slice(ls, from).trim() && !text.slice(to, le).trim()) return { from: ls, to: Math.min(le + 1, text.length), insert: "" };
    return { from, to, insert: "" };
  };
  return [cut(env.begin.from, env.begin.to + opt.length), cut(env.end.from, env.end.to)];
}

/** Remove \name[..]{ and its } around pos, keeping the argument. */
export function deleteCommand(text, pos) {
  const c = enclosingCommand(text, pos);
  return c && [{ from: c.from, to: c.open + 1, insert: "" }, { from: c.close, to: c.close + 1, insert: "" }];
}

const OPEN = { "(": ")", "[": "]", "\\{": "\\}" }, CLOSE = { ")": "(", "]": "[", "\\}": "\\{" };
const delimAt = (text, i) => {   // "(", ")", "[", "]", "\{", "\}" at i, or null; \( \[ are math, not brackets
  const c = text[i];
  if ((c === "{" || c === "}") && text[i - 1] === "\\" && !escaped(text, i - 1)) return "\\" + c;
  if ("()[]".includes(c) && !escaped(text, i)) return c;
  return null;
};

/** \left/\right: drop them from the pair around pos, or add them to the innermost bracket pair around pos. */
export function toggleLeftRight(text, pos) {
  const stack = [];
  for (const m of text.matchAll(/\\(left|right)(?![A-Za-z])\s*(\\[A-Za-z]+|\\.|.)?/g)) {
    if (m[1] === "left") { stack.push(m); continue; }
    const l = stack.pop();
    if (!l || l.index > pos || pos > m.index + m[0].length) continue;
    const drop = (x) => ({ from: x.index, to: x.index + x[0].length - (x[2] === "." ? 0 : (x[2] || "").length), insert: "" });
    return [drop(l), drop(m)];
  }
  // Add: the innermost bracket pair around pos (pairs close inner first, so the first one found is the innermost).
  const opens = [];
  for (let i = Math.max(0, pos - 20000); i < Math.min(text.length, pos + 20000); i++) {
    const d = delimAt(text, i);
    if (!d) continue;
    const at = d.length === 2 ? i - 1 : i;
    if (OPEN[d]) { opens.push({ at, d }); continue; }
    if (opens[opens.length - 1]?.d !== CLOSE[d]) continue;   // unbalanced: skip
    const o = opens.pop();
    if (o.at < pos && pos <= at) return [{ from: o.at, insert: "\\left" }, { from: at, insert: "\\right" }];
  }
  return null;
}

/** A paragraph rewrapped to width columns (the first line's indent on every line), or null when it holds comments or verbatim-like code. */
export function rewrap(par, width) {
  if (/(?<!\\)(?:\\\\)*%|\\verb|\\begin\s*\{|\\end\s*\{|\\item\b|\\(?:part|chapter|(?:sub)*section|paragraph)\b/.test(par)) return null;
  const indent = /^[ \t]*/.exec(par)[0], words = par.split(/\s+/).filter(Boolean), lines = [];
  let line = "";
  for (const w of words) {
    if (line && indent.length + line.length + 1 + w.length > width) { lines.push(indent + line); line = w; }
    else line = line ? line + " " + w : w;
  }
  if (line) lines.push(indent + line);
  return lines.join("\n") + (/\n$/.test(par) ? "\n" : "");
}

// ---- Unicode to LaTeX ------------------------------------------------------------------------------------------
const TEXT_CHARS = { "\u201c": "``", "\u201d": "''", "\u201e": ",,", "\u2018": "`", "\u2019": "'", "\u2013": "--", "\u2014": "---", "\u2026": "\\dots{}", "\u00a0": "~" };
const PDF_CHARS = { "ß": "\\ss{}", "æ": "\\ae{}", "Æ": "\\AE{}", "œ": "\\oe{}", "Œ": "\\OE{}", "ø": "\\o{}", "Ø": "\\O{}", "ł": "\\l{}", "Ł": "\\L{}", "ı": "\\i{}", "€": "\\texteuro{}", "§": "\\S{}", "°": "\\textdegree{}" };
// Combining accents: symbol accents take the letter directly (\'e), letter accents a group (\c{c}).
const ACCENTS = { "\u0300": "\\`", "\u0301": "\\'", "\u0302": "\\^", "\u0303": "\\~", "\u0304": "\\=", "\u0307": "\\.", "\u0308": '\\"',
  "\u0306": "\\u", "\u030a": "\\r", "\u030b": "\\H", "\u030c": "\\v", "\u0327": "\\c", "\u0328": "\\k" };
const GREEK = "alpha beta gamma delta epsilon zeta eta theta iota kappa lambda mu nu xi o pi rho varsigma sigma tau upsilon phi chi psi omega".split(" ");
const MATH_CHARS = {
  ...Object.fromEntries(GREEK.map((g, i) => [String.fromCharCode(0x3b1 + i), g === "o" ? "o" : "\\" + g])),
  ...Object.fromEntries(["Gamma", "Delta", "Theta", "Lambda", "Xi", "Pi", "Sigma", "Upsilon", "Phi", "Psi", "Omega"].map((g) => [String.fromCharCode({ Gamma: 0x393, Delta: 0x394, Theta: 0x398, Lambda: 0x39b, Xi: 0x39e, Pi: 0x3a0, Sigma: 0x3a3, Upsilon: 0x3a5, Phi: 0x3a6, Psi: 0x3a8, Omega: 0x3a9 }[g]), "\\" + g])),
  "ϕ": "\\phi", "ε": "\\varepsilon", "ϵ": "\\epsilon", "≤": "\\leq", "≥": "\\geq", "≠": "\\neq", "≈": "\\approx", "≡": "\\equiv", "±": "\\pm", "×": "\\times", "÷": "\\div",
  "·": "\\cdot", "∞": "\\infty", "∂": "\\partial", "∇": "\\nabla", "∑": "\\sum", "∏": "\\prod", "∫": "\\int", "√": "\\sqrt", "∈": "\\in", "∉": "\\notin",
  "⊂": "\\subset", "⊆": "\\subseteq", "∪": "\\cup", "∩": "\\cap", "∅": "\\emptyset", "∀": "\\forall", "∃": "\\exists", "¬": "\\neg", "∧": "\\wedge", "∨": "\\vee",
  "→": "\\to", "←": "\\leftarrow", "⇒": "\\Rightarrow", "⇔": "\\Leftrightarrow", "↦": "\\mapsto", "ℝ": "\\mathbb{R}", "ℕ": "\\mathbb{N}", "ℤ": "\\mathbb{Z}", "ℚ": "\\mathbb{Q}", "ℂ": "\\mathbb{C}",
  "°": "^\\circ", "′": "'", "−": "-",
};

/**
 * Pasted text with Unicode turned into LaTeX. In math: Greek and symbols. In text: quotes, dashes and ellipsis always;
 * accented letters and a few specials only for pdflatex (xelatex and lualatex read UTF-8 and use the font's letters).
 */
export function unicodeToLatex(s, { math = false, pdflatex = true } = {}) {
  let out = "";
  const chars = [...s.normalize("NFC")];
  for (let i = 0; i < chars.length; i++) {
    const c = chars[i], next = chars[i + 1] || "";
    let r = null;
    if (math) r = MATH_CHARS[c] ?? null;
    else if (TEXT_CHARS[c]) r = TEXT_CHARS[c];
    else if (pdflatex && PDF_CHARS[c]) r = PDF_CHARS[c];
    else if (pdflatex && c.charCodeAt(0) > 127) {
      const [base, ...marks] = [...c.normalize("NFD")];
      if (marks.length === 1 && ACCENTS[marks[0]] && /^[A-Za-z]$/.test(base)) {
        const a = ACCENTS[marks[0]], b = base === "i" ? "\\i" : base;
        r = /[A-Za-z]$/.test(a) || b.length > 1 ? `${a}{${b}}` : a + b;
      }
    }
    if (r === null) { out += c; continue; }
    out += r + (/\\[A-Za-z]+$/.test(r) && /^[A-Za-z]/.test(next) ? " " : "");
  }
  return out;
}

// ---- .editorconfig ----------------------------------------------------------------------------------------------
/** .editorconfig text as [{glob, props}] (keys and values lower-cased, as the spec says). */
export function parseEditorconfig(text) {
  const sections = [];
  let cur = null;
  for (const raw of String(text || "").split(/\r?\n/)) {
    const line = raw.trim();
    if (!line || line[0] === "#" || line[0] === ";") continue;
    const sec = /^\[(.*)\]$/.exec(line);
    if (sec) { cur = { glob: sec[1], props: {} }; sections.push(cur); continue; }
    const kv = /^([^=:]+)[=:](.*)$/.exec(line);
    if (kv && cur) cur.props[kv[1].trim().toLowerCase()] = kv[2].trim().toLowerCase();
  }
  return sections;
}

/** An EditorConfig glob as a RegExp over paths relative to the folder holding the file. */
export function globRegex(glob) {
  let re = "", i = 0, brace = 0;
  if (!glob.includes("/")) re = "(?:.*/)?";
  else if (glob[0] === "/") i = 1;
  for (; i < glob.length; i++) {
    const c = glob[i];
    if (c === "\\" && i + 1 < glob.length) re += "\\" + glob[++i];
    else if (c === "*") { if (glob[i + 1] === "*") { re += ".*"; i++; } else re += "[^/]*"; }
    else if (c === "?") re += "[^/]";
    else if (c === "[") { const e = glob.indexOf("]", i); if (e < 0) re += "\\["; else { re += "[" + glob.slice(i + 1, e).replace(/^!/, "^").replace(/\\/g, "\\\\") + "]"; i = e; } }
    else if (c === "{" && glob.indexOf("}", i) > 0) { re += "(?:"; brace++; }
    else if (c === "}" && brace) { re += ")"; brace--; }
    else if (c === "," && brace) re += "|";
    else re += c.replace(/[.+^$()|{}\]]/g, "\\$&");
  }
  return new RegExp("^" + re + "$");
}

/** What the editor applies for path: {indentUnit, tabSize, trim, finalNewline} (only keys the file sets). */
export function editorconfigFor(text, path) {
  const p = {};
  for (const s of parseEditorconfig(text)) { try { if (globRegex(s.glob).test(path)) Object.assign(p, s.props); } catch { /* a glob we cannot read */ } }
  const out = {}, size = +p.indent_size || (p.indent_size === "tab" ? +p.tab_width : 0), tab = +p.tab_width || 0;
  if (tab) out.tabSize = tab;
  if (p.indent_style === "tab") out.indentUnit = "\t";
  else if (p.indent_style === "space") out.indentUnit = " ".repeat(size || tab || 2);
  else if (size) out.indentUnit = " ".repeat(size);
  if (p.trim_trailing_whitespace === "true") out.trim = true;
  if (p.insert_final_newline === "true") out.finalNewline = true;
  return out;
}

/** Changes for a save: trailing white space trimmed (except on the given line numbers, where people type), and a final newline. */
export function saveFixes(text, { trim, finalNewline }, keepLines = new Set()) {
  const changes = [];
  if (trim) {
    let from = 0, n = 1;
    for (const line of text.split("\n")) {
      const t = /[ \t]+$/.exec(line);
      if (t && !keepLines.has(n)) changes.push({ from: from + t.index, to: from + line.length, insert: "" });
      from += line.length + 1; n++;
    }
  }
  if (finalNewline && text && !text.endsWith("\n")) changes.push({ from: text.length, to: text.length, insert: "\n" });
  return changes;
}

// ---- path completion -------------------------------------------------------------------------------------------
export const PATH_CMD = /\\(includegraphics|input|include|includeonly|subfile|bibliography|addbibresource)\*?(?:\[[^\]]*\])*\{([^}]*)$/;

/** The file names a path command may take, from the document's file list [{path}]. */
export function pathOptions(cmd, files) {
  const paths = files.map((f) => f.path);
  if (cmd === "includegraphics") return paths.filter((p) => /\.(png|jpe?g|pdf|eps)$/i.test(p));
  if (cmd === "bibliography") return paths.filter((p) => /\.bib$/i.test(p)).map((p) => p.slice(0, -4));
  if (cmd === "addbibresource") return paths.filter((p) => /\.bib$/i.test(p));
  return paths.filter((p) => /\.tex$/i.test(p) && p !== "main.tex").map((p) => p.slice(0, -4));
}

// ---- user snippets ----------------------------------------------------------------------------------------------
export const DEFAULT_SNIPPETS = [
  { trigger: "fig", body: "\\begin{figure}[htbp]\n\t\\centering\n\t\\includegraphics[width=0.8\\linewidth]{${1:file}}\n\t\\caption{${2:caption}}\n\t\\label{fig:${3:label}}\n\\end{figure}" },
  { trigger: "eq", body: "\\begin{equation}\n\t${1}\n\t\\label{eq:${2:label}}\n\\end{equation}" },
  { trigger: "itm", body: "\\begin{itemize}\n\t\\item ${1}\n\\end{itemize}" },
  { trigger: "sec", body: "\\section{${1:title}}\n\\label{sec:${2:label}}" },
];

/**
 * A snippet body (LaTeX with ${1:default} tab stops) as a template for CodeMirror's snippet(): that parser reads
 * \{ and \} as escaped braces and #{ as a field, so LaTeX's own \{ \} and #{ are escaped once more.
 */
export const snippetTemplate = (body) => body.replace(/\\([{}])/g, "\\\\$1").replace(/#\{/g, "#\\{");

/** The snippet whose trigger is the word just before the cursor (not a command name), or null. */
export function snippetAt(before, snippets) {
  const m = /(?:^|[^\\A-Za-z0-9])([A-Za-z][A-Za-z0-9]*)$/.exec(before);
  return (m && snippets.find((s) => s.trigger === m[1])) || null;
}

/** Stored snippets, cleaned: [{trigger, body}] with a word trigger and a body, at most 200. */
export const cleanSnippets = (list) => (Array.isArray(list) ? list : [])
  .filter((s) => s && typeof s.trigger === "string" && /^[A-Za-z][A-Za-z0-9]*$/.test(s.trigger) && typeof s.body === "string" && s.body)
  .slice(0, 200).map(({ trigger, body }) => ({ trigger, body: body.slice(0, 5000) }));
