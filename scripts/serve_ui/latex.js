// LaTeX source helpers for the editor: folding, the live code check, hover cards and completion matching.
// No imports: the CodeMirror modules come in as arguments, so tests/latex_check.mjs runs the pure parts in Node.

const HEADS = ["part", "chapter", "section", "subsection", "subsubsection", "paragraph", "subparagraph"];
const HEAD = /^\s*\\(part|chapter|section|subsection|subsubsection|paragraph|subparagraph)\*?\s*[[{]/;
// Contents are not LaTeX to check (or to fold inside).
const VERBATIM = new Set(["verbatim", "verbatim*", "Verbatim", "BVerbatim", "lstlisting", "minted", "comment", "alltt", "filecontents", "filecontents*"]);
const MATH_ENVS = new Set(["equation", "equation*", "align", "align*", "gather", "gather*", "multline", "multline*", "eqnarray", "eqnarray*", "displaymath", "math", "flalign", "flalign*", "alignat", "alignat*"]);
// Their bodies may hold half an environment or a lone $ on purpose (\newcommand{\be}{\begin{equation}}).
const DEFS = new Set(["newcommand", "renewcommand", "providecommand", "newenvironment", "renewenvironment", "def", "gdef", "edef", "xdef",
  "NewDocumentCommand", "RenewDocumentCommand", "ProvideDocumentCommand", "DeclareDocumentCommand", "NewDocumentEnvironment",
  "RenewDocumentEnvironment", "DeclareRobustCommand", "DeclareMathOperator"]);
const RAW_ARG = new Set(["url", "path", "nolinkurl", "href"]);   // a URL may hold % $ # unescaped
export const CHECKED = /\.(tex|ltx|sty|cls|tikz)$/i;

/** Code part of a line: everything before an unescaped %. */
const code = (line) => { const m = /(?<!\\)(?:\\\\)*%/.exec(line); return m ? line.slice(0, m.index + m[0].length - 1) : line; };

/** Section, environment, preamble and % region folds (for foldService). */
export function foldRange(doc, from, to) {
  const line = doc.sliceString(from, to), first = doc.lineAt(from).number, last = doc.lines;
  const scan = (test) => { for (let n = first + 1; n <= last; n++) { const r = test(doc.line(n)); if (r != null) return r; } return null; };
  const span = (end) => (end != null && end > to ? { from: to, to: end } : null);
  if (/^\s*%\s*region\b/.test(line)) {
    let depth = 1;
    return span(scan((l) => (/^\s*%\s*region\b/.test(l.text) ? (depth++, null) : /^\s*%\s*endregion\b/.test(l.text) && !--depth ? l.from - 1 : null)));
  }
  const text = code(line);
  if (/^\s*\\documentclass\b/.test(text)) return span(scan((l) => (/^\s*\\begin\{document\}/.test(code(l.text)) ? l.from - 1 : null)));
  const head = HEAD.exec(text);
  if (head) {
    const level = HEADS.indexOf(head[1]);
    const end = scan((l) => { const t = code(l.text), h = HEAD.exec(t); return (h && HEADS.indexOf(h[1]) <= level) || /\\end\{document\}|^\s*\\(?:appendix|backmatter)\b/.test(t) ? l.from - 1 : null; });
    return span(end ?? doc.length);
  }
  const begin = /\\begin\{([^}]+)\}/.exec(text);
  if (begin) {
    const name = begin[1], after = text.slice(begin.index + begin[0].length);
    const tag = new RegExp(`\\\\(begin|end)\\{${name.replace(/[.*+?^${}()|[\]\\]/g, "\\$&")}\\}`, "g");
    let depth = 1;
    for (const m of after.matchAll(tag)) if ((depth += m[1] === "begin" ? 1 : -1) === 0) return null;   // closed on its own line
    return span(scan((l) => {
      for (const m of (VERBATIM.has(name) ? l.text : code(l.text)).matchAll(tag)) if ((depth += m[1] === "begin" ? 1 : -1) === 0) return l.from + m.index;
      return null;
    }));
  }
  return null;
}

/**
 * One pass over the text: math spans (for hover previews) and unbalanced groups (for the code check).
 * Skips comments, verbatim-like environments, \verb, URL arguments, definition bodies and %%begin/%%end novalidate regions.
 */
export function scan(text) {
  const math = [], problems = [], stack = [];
  const off = /^%%novalidate[ \t]*$/m.test(text);
  const report = (from, to, message) => problems.push({ from, to, message });
  const unclosed = (o) => report(o.from, o.to, o.env ? `\\begin{${o.name}} is never closed` : `${o.label} is never closed`);
  const isMath = (o) => o.kind === "math";
  // Close the nearest opener that passes `want`, reporting what is still open above it; stop at `wall`.
  const closeTo = (want, wall) => {
    for (let k = stack.length - 1; k >= 0; k--) {
      if (want(stack[k])) { const [o, ...above] = stack.splice(k); above.forEach(unclosed); return o; }
      if (wall(stack[k])) return null;
    }
    return null;
  };
  const n = text.length;
  let i = 0, def = -1;   // def: stack depth a definition started at, or -1
  const skipGroup = (j) => { let d = 0; for (; j < n; j++) { const c = text[j]; if (c === "\\") j++; else if (c === "{") d++; else if (c === "}" && --d === 0) return j + 1; } return n; };
  while (i < n) {
    const c = text[i];
    if (c === "%") {
      const eol = text.indexOf("\n", i), end = eol < 0 ? n : eol;
      if (/^%%begin novalidate\b/.test(text.slice(i, end))) {
        const stop = text.slice(end).search(/^%%end novalidate\b/m);
        i = stop < 0 ? n : end + stop + 1;
        continue;
      }
      i = end; continue;
    }
    if (c === "\n") {   // A blank line ends a paragraph, and math may not run across one.
      const next = /^[ \t]*\n/.exec(text.slice(i + 1, i + 200));
      if (next && def < 0 && stack.some((o) => isMath(o) && !o.env)) {
        while (stack.length && stack.some((o) => isMath(o) && !o.env)) { const o = stack.pop(); unclosed(o); }
      }
      i++; continue;
    }
    if (c === "\\") {
      const m = /^\\(?:([A-Za-z@]+)\*?|.)/s.exec(text.slice(i, i + 64));
      const name = m[1], tokEnd = i + m[0].length;
      if (!name) {
        const s = text[i + 1];
        if (def < 0 && (s === "(" || s === "[")) stack.push({ kind: "math", sym: s, label: `\\${s}`, from: i, to: tokEnd, inner: tokEnd, display: s === "[" });
        else if (def < 0 && (s === ")" || s === "]")) {
          const want = s === ")" ? "(" : "[";
          const o = closeTo((x) => isMath(x) && x.sym === want, (x) => x.kind === "env" || x.kind === "brace");
          if (o) math.push({ from: o.from, to: tokEnd, tex: text.slice(o.inner, i), display: o.display });
          else report(i, tokEnd, s === ")" ? "\\) without \\(" : "\\] without \\[");
        }
        i = tokEnd; continue;
      }
      if (name === "verb" || name === "lstinline") {
        let j = text[tokEnd] === "*" ? tokEnd + 1 : tokEnd;
        if (name === "lstinline" && text[j] === "[") j = text.indexOf("]", j) + 1 || n;
        const d = text[j];
        i = d === "{" ? skipGroup(j) : (text.indexOf(d, j + 1) + 1 || n);
        continue;
      }
      if (RAW_ARG.has(name)) { const j = /^\s*/.exec(text.slice(tokEnd, tokEnd + 20))[0].length + tokEnd; i = text[j] === "{" ? skipGroup(j) : tokEnd; continue; }
      if (def < 0 && DEFS.has(name)) { def = stack.length; i = tokEnd; continue; }
      if (def >= 0) { i = tokEnd; continue; }
      if (name === "begin" || name === "end") {
        const arg = /^\s*\{([^{}\n]*)\}/.exec(text.slice(tokEnd, tokEnd + 100));
        if (!arg) { i = tokEnd; continue; }
        const env = arg[1].trim(), to = tokEnd + arg[0].length;
        if (name === "begin") {
          if (VERBATIM.has(env)) {
            const stop = text.indexOf(`\\end{${env}}`, to);
            if (stop < 0) { report(i, to, `\\begin{${env}} is never closed`); i = n; } else i = stop + env.length + 6;
            continue;
          }
          stack.push({ kind: MATH_ENVS.has(env) ? "math" : "env", env: true, name: env, from: i, to, inner: to, display: true });
        } else {
          const o = closeTo((x) => x.env && x.name === env, () => false);
          if (o?.kind === "math") math.push({ from: o.from, to, tex: mathTex(env, text.slice(o.inner, i)), display: true });
          if (!o) {
            const top = [...stack].reverse().find((x) => x.env);
            report(i, to, top ? `\\end{${env}} does not match \\begin{${top.name}}` : `\\end{${env}} without \\begin{${env}}`);
          }
          if (env === "document") { i = n; continue; }   // TeX reads nothing after it
        }
        i = to; continue;
      }
      if (name === "left") {
        const d = /^\s*(\\[A-Za-z]+|\\.|.)/s.exec(text.slice(tokEnd, tokEnd + 40));
        stack.push({ kind: "left", label: "\\left", from: i, to: tokEnd });
        i = tokEnd + (d ? d[0].length : 0); continue;
      }
      if (name === "right") {
        if (!closeTo((x) => x.kind === "left", (x) => x.kind !== "left")) report(i, tokEnd, "\\right without \\left");
        const d = /^\s*(\\[A-Za-z]+|\\.|.)/s.exec(text.slice(tokEnd, tokEnd + 40));
        i = tokEnd + (d ? d[0].length : 0); continue;
      }
      i = tokEnd; continue;
    }
    if (c === "{") { stack.push({ kind: "brace", label: "{", from: i, to: i + 1 }); i++; continue; }
    if (c === "}") {
      if (def >= 0 && stack.length > def) {
        stack.pop();
        if (stack.length === def) {   // the definition goes on while more {..} or [..] follow
          const next = /^\s*(.)/s.exec(text.slice(i + 1, i + 200));
          if (!next || (next[1] !== "{" && next[1] !== "[")) def = -1;
        }
        i++; continue;
      }
      if (!closeTo((x) => x.kind === "brace", (x) => x.env === true)) report(i, i + 1, "} without {");
      i++; continue;
    }
    if (c === "$" && def < 0) {
      const dbl = text[i + 1] === "$", sym = dbl ? "$$" : "$", to = i + sym.length;
      // A $ inside {..} opens new math (\text{$x$}); one after an open \left closes the math and reports the \left.
      const top = closeTo((x) => isMath(x) && x.sym === sym, (x) => x.kind !== "left");
      if (top) math.push({ from: top.from, to, tex: text.slice(top.inner, i), display: dbl });
      else stack.push({ kind: "math", sym, label: sym, from: i, to, inner: to, display: dbl });
      i = to; continue;
    }
    i++;
  }
  stack.forEach(unclosed);
  return { math, problems: off ? [] : problems.sort((a, b) => a.from - b.from) };
}

/** Math body as KaTeX takes it: labels dropped, AMS display environments mapped to their inner forms. */
export function mathTex(kind, body) {
  kind = (kind || "").replace(/\*$/, "");
  let tex = body.replace(/\\label\{[^}]*\}|\\nonumber|\\notag/g, "").trim();
  if (kind === "align" || kind === "eqnarray" || kind === "flalign" || kind === "alignat") tex = `\\begin{aligned}${tex.replace(/&=&/g, "&=")}\\end{aligned}`;
  else if (kind === "gather" || kind === "multline") tex = `\\begin{gathered}${tex}\\end{gathered}`;
  return tex;
}

const CITE = /\\(cite[A-Za-z]*|[Cc]itep?|[Cc]itet|parencite|textcite|autocite|footcite|nocite)\*?(?:\[[^\]]*\])*\{([^}]*)\}|\\(ref|eqref|cref|Cref|autoref|pageref|nameref|vref)\*?\{([^}]*)\}/g;

/** The \cite key or \ref label under pos on this line: {kind: "cite"|"ref", key, from, to} or null. */
export function keyAt(line, lineFrom, pos) {
  for (const m of line.matchAll(CITE)) {
    const keys = m[2] ?? m[4], start = m.index + m[0].length - 1 - keys.length;
    if (pos < lineFrom + m.index || pos > lineFrom + m.index + m[0].length) continue;
    let at = start;
    const parts = keys.split(",").map((k) => { const r = { k, from: at }; at += k.length + 1; return r; });
    const rel = pos - lineFrom;
    const hit = parts.find((p) => rel >= p.from && rel <= p.from + p.k.length) || (keys.includes(",") ? null : parts[0]);
    if (!hit || !hit.k.trim()) return null;
    const lead = hit.k.length - hit.k.trimStart().length;
    return { kind: m[2] != null ? "cite" : "ref", key: hit.k.trim(), from: lineFrom + hit.from + lead, to: lineFrom + hit.from + lead + hit.k.trim().length };
  }
  return null;
}

/** Completion matching that looks at any part of the key, author and title: every word typed must appear. */
export function matchEntries(entries, query, text) {
  const words = query.toLowerCase().split(/\s+/).filter(Boolean);
  const q = query.trim().toLowerCase();
  const out = [];
  for (const [key, v] of Object.entries(entries)) {
    const k = key.toLowerCase(), hay = k + " " + text(v).toLowerCase();
    if (!words.every((w) => hay.includes(w))) continue;
    out.push({ key, v, rank: !q ? 0 : k.startsWith(q) ? 0 : k.includes(q) ? 1 : 2 });
  }
  return out.sort((a, b) => a.rank - b.rank || a.key.localeCompare(b.key));
}

export const BUILTIN_ENVS = ["abstract", "align", "align*", "alignat", "array", "bmatrix", "cases", "center", "corollary", "definition",
  "description", "document", "enumerate", "equation", "equation*", "example", "figure", "figure*", "flushleft", "flushright", "frame",
  "gather", "gather*", "itemize", "lemma", "longtable", "lstlisting", "minipage", "minted", "multline", "multline*", "pmatrix",
  "proof", "proposition", "quotation", "quote", "remark", "split", "subfigure", "table", "table*", "tabular", "tabularx",
  "thebibliography", "theorem", "tikzpicture", "verbatim", "vmatrix"];

/**
 * Editor extensions: fold service + gutter, the code check (as lint diagnostics) and hover cards.
 * deps: {S, V, L, LINT, loadKatex}; env: {refs(), visual(), path()}.
 */
export function latexSupport({ V, L, LINT, loadKatex }, env) {
  const folding = [L.foldService.of((state, from, to) => (CHECKED.test(env.path()) ? foldRange(state.doc, from, to) : null)), L.foldGutter()];
  const cache = new WeakMap();
  const scanned = (doc) => { let r = cache.get(doc); if (!r) { r = scan(doc.toString()); cache.set(doc, r); } return r; };
  const check = [
    LINT.linter((view) => (CHECKED.test(env.path()) ? scanned(view.state.doc).problems.map((p) => ({ ...p, severity: "error", source: "Code check" })) : []), { delay: 600 }),
  ];
  const card = (...kids) => { const d = document.createElement("div"); d.className = "cm-texHover"; d.append(...kids); return d; };
  const line = (text, cls) => { const s = document.createElement("div"); if (cls) s.className = cls; s.textContent = text; return s; };
  const hover = V.hoverTooltip((view, pos) => {
    if (!CHECKED.test(env.path())) return null;
    const l = view.state.doc.lineAt(pos), hit = keyAt(l.text, l.from, pos);
    if (hit) {
      const refs = env.refs() || {};
      return { pos: hit.from, end: hit.to, above: true, create: () => {
        if (hit.kind === "cite") {
          const e = refs.bib?.[hit.key];
          if (!e) return { dom: card(line(`${hit.key}: not found in the document's .bib files`)) };
          return { dom: card(line([e.author, e.year && `(${e.year})`].filter(Boolean).join(" ") || hit.key, "cm-texHover-head"), line(e.title || ""), line([e.journal || e.booktitle, hit.key].filter(Boolean).join(" · "), "cm-texHover-mute")) };
        }
        const r = refs.labels?.[hit.key];
        if (!r) return { dom: card(line(`${hit.key}: no \\label with this name`)) };
        return { dom: card(line(r.context), line(`${r.file}:${r.line}`, "cm-texHover-mute")) };
      } };
    }
    if (env.visual()) return null;   // visual mode renders the math in place
    if (view.state.doc.length > 2_000_000) return null;
    const m = scanned(view.state.doc).math.find((x) => x.from <= pos && pos <= x.to);
    if (!m || !m.tex.trim()) return null;
    return { pos: m.from, end: m.to, above: true, create: () => {
      const box = card();
      box.classList.add("cm-mathHover");
      box.textContent = m.tex;
      loadKatex().then((katex) => katex.render(m.tex, box, { displayMode: m.display, throwOnError: false, errorColor: "var(--ox)" })).catch(() => {});
      return { dom: box };
    } };
  });
  return { folding, check, hover };
}
