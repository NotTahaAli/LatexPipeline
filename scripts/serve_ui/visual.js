// Visual mode: decorations over the LaTeX source. The source stays the truth; markup is
// hidden (or replaced by a widget) only while the cursor is elsewhere.
import { state as S, view as V, loadKatex } from "./libs.js";

const { Decoration, WidgetType, EditorView } = V;
const { StateField, Facet, RangeSetBuilder } = S;

/** Callbacks supplied by the app: refs lookup and image url. */
export const visualEnv = Facet.define({ combine: (v) => v[0] || {} });

const HEAD = /\\(part|chapter|section|subsection|subsubsection|paragraph)(\*?)(\[[^\]]*\])?\{/g;
const STYLE = /\\(textbf|emph|textit|textsl|underline|texttt|textsc)\{/g;
const REF = /\\(cite[a-z]*|ref|eqref|autoref|cref|Cref|pageref|nameref)\*?(?:\[[^\]]*\])*\{([^}]*)\}/g;
const IMG = /\\includegraphics\*?(?:\[[^\]]*\])?\{([^}]*)\}/g;
const ITEM = /^([ \t]*)\\item\b[ \t]?(\[[^\]]*\])?/gm;
const ENV = /\\(begin|end)\{(itemize|enumerate|description)\}[ \t]*/g;
const MATH_ENV = /\\begin\{(equation|align|gather|eqnarray|multline|displaymath)(\*?)\}([\s\S]*?)\\end\{\1\2\}/g;
const DISPLAY = /\\\[([\s\S]*?)\\\]|\$\$([\s\S]*?)\$\$/g;
const INLINE = /(?<![\\$])\$(?!\$)((?:\\.|[^$\\\n])+?)\$|\\\(([\s\S]*?)\\\)/g;
const STYLE_CLASS = { textbf: "v-bold", emph: "v-emph", textit: "v-emph", textsl: "v-emph", underline: "v-under", texttt: "v-mono", textsc: "v-caps" };
const HEAD_CLASS = { part: "v-h0", chapter: "v-h1", section: "v-h2", subsection: "v-h3", subsubsection: "v-h4", paragraph: "v-h5" };
// Commands and the arguments that name things (labels, keys, files) are not prose: the browser's spell checker skips them.
const CODE = /\\[A-Za-z@]+\*?(?:\[[^\]]*\])*(?:\{(?:[^{}]*)\})?(?<=\\(?:label|ref|eqref|autoref|cref|Cref|pageref|nameref|cite[a-z]*|input|include|includegraphics|usepackage|documentclass|begin|end|bibliography|bibliographystyle|url|href|newcommand|renewcommand|def|setlength|vspace|hspace)(?:\*|\[[^\]]*\])*\{[^{}]*\}|\\[A-Za-z@]+\*?)/g;
const NO_SPELL = Decoration.mark({ attributes: { spellcheck: "false" } });
const MAX_CHARS = 600000;

function matchBrace(text, open) {
  let depth = 0;
  for (let i = open; i < text.length; i++) {
    const c = text[i];
    if (c === "\\") { i++; continue; }
    if (c === "{") depth++;
    else if (c === "}" && --depth === 0) return i;
  }
  return -1;
}

class MathWidget extends WidgetType {
  constructor(tex, display, from) { super(); this.tex = tex; this.display = display; this.from = from; }
  eq(o) { return o.tex === this.tex && o.display === this.display; }
  toDOM(view) {
    const el = document.createElement(this.display ? "div" : "span");
    el.className = this.display ? "v-math v-math-block" : "v-math";
    el.title = "Click to edit";
    el.textContent = this.tex;
    loadKatex().then((katex) => {
      katex.render(this.tex, el, { displayMode: this.display, throwOnError: false, errorColor: "var(--ox)" });
    }).catch(() => {});
    el.addEventListener("mousedown", (e) => {
      e.preventDefault();
      view.dispatch({ selection: { anchor: Math.min(view.posAtDOM(el) + 1, view.state.doc.length) }, scrollIntoView: true });
      view.focus();
    });
    return el;
  }
  ignoreEvent() { return true; }
}

class ChipWidget extends WidgetType {
  constructor(kind, label, tip) { super(); this.kind = kind; this.label = label; this.tip = tip; }
  eq(o) { return o.kind === this.kind && o.label === this.label && o.tip === this.tip; }
  toDOM(view) {
    const el = document.createElement("span");
    el.className = `v-chip v-chip-${this.kind}`;
    el.textContent = this.label;
    el.title = this.tip;
    el.tabIndex = 0;
    el.setAttribute("role", "note");
    el.addEventListener("mousedown", (e) => {
      e.preventDefault();
      view.dispatch({ selection: { anchor: view.posAtDOM(el) + 1 } });
      view.focus();
    });
    return el;
  }
  ignoreEvent() { return true; }
}

class BulletWidget extends WidgetType {
  constructor(label) { super(); this.label = label; }
  eq(o) { return o.label === this.label; }
  toDOM() { const el = document.createElement("span"); el.className = "v-bullet"; el.textContent = this.label; return el; }
}

class ImageWidget extends WidgetType {
  constructor(url, name) { super(); this.url = url; this.name = name; }
  eq(o) { return o.url === this.url; }
  toDOM() {
    const box = document.createElement("span");
    box.className = "v-image";
    const img = document.createElement("img");
    img.src = this.url; img.alt = this.name; img.loading = "lazy";
    img.onerror = () => { box.textContent = `Image: ${this.name}`; box.classList.add("v-image-missing"); };
    box.append(img);
    return box;
  }
}

function mathTex(kind, body) {
  let tex = body.replace(/\\label\{[^}]*\}|\\nonumber|\\notag/g, "").trim();
  if (kind === "align" || kind === "eqnarray") tex = `\\begin{aligned}${tex.replace(/&=&/g, "&=")}\\end{aligned}`;
  else if (kind === "gather") tex = `\\begin{gathered}${tex}\\end{gathered}`;
  else if (kind === "multline") tex = `\\begin{gathered}${tex}\\end{gathered}`;
  return tex;
}

function refTip(env, kind, key) {
  const refs = env.refs?.() || {};
  if (kind.startsWith("cite")) {
    const e = refs.bib?.[key];
    if (!e) return `Unknown reference: ${key}`;
    return [e.author, e.year && `(${e.year})`, e.title, e.journal || e.booktitle].filter(Boolean).join(". ");
  }
  const l = refs.labels?.[key];
  return l ? `${key}: ${l.context}  [${l.file}:${l.line}]` : `Unknown label: ${key}`;
}

function build(st) {
  const doc = st.doc;
  const text = doc.toString();
  if (text.length > MAX_CHARS) return Decoration.none;
  const env = st.facet(visualEnv);
  const ranges = [];  // {from,to,deco}
  const sel = st.selection.ranges;
  const touched = (from, to) => sel.some((r) => r.from <= to && r.to >= from);
  const add = (from, to, deco) => ranges.push({ from, to, deco });
  const hide = (from, to) => from < to && add(from, to, Decoration.replace({}));
  const taken = [];  // spans consumed by math (so nothing is decorated inside them)
  const inTaken = (p) => taken.some(([a, b]) => p >= a && p < b);

  // Math first: it wins over everything inside it.
  const math = (re, pick) => {
    for (const m of text.matchAll(re)) {
      const from = m.index, to = from + m[0].length;
      if (inTaken(from)) continue;
      taken.push([from, to]);
      if (touched(from, to)) { add(from, to, Decoration.mark({ class: "v-math-src", attributes: { spellcheck: "false" } })); continue; }
      const [tex, display] = pick(m);
      add(from, to, Decoration.replace({ widget: new MathWidget(tex, display, from), block: display }));
    }
  };
  math(MATH_ENV, (m) => [mathTex(m[1], m[3]), true]);
  math(DISPLAY, (m) => [(m[1] ?? m[2]).trim(), true]);
  math(INLINE, (m) => [(m[1] ?? m[2]).trim(), false]);

  for (const m of text.matchAll(HEAD)) {
    if (inTaken(m.index)) continue;
    const open = m.index + m[0].length - 1, close = matchBrace(text, open);
    if (close < 0) continue;
    const line = doc.lineAt(m.index);
    add(line.from, line.from, Decoration.line({ class: `v-heading ${HEAD_CLASS[m[1]]}` }));
    if (!touched(line.from, line.to)) { hide(m.index, open + 1); hide(close, close + 1); }
    add(open + 1, close, Decoration.mark({ class: "v-heading-text" }));
  }

  for (const m of text.matchAll(STYLE)) {
    if (inTaken(m.index)) continue;
    const open = m.index + m[0].length - 1, close = matchBrace(text, open);
    if (close < 0) continue;
    add(open + 1, close, Decoration.mark({ class: STYLE_CLASS[m[1]] }));
    if (!touched(m.index, close + 1)) { hide(m.index, open + 1); hide(close, close + 1); }
  }

  // Lists: number items by walking the environments in order.
  const stack = [];
  const events = [];
  for (const m of text.matchAll(ENV)) events.push({ at: m.index, end: m.index + m[0].length, env: m, kind: "env" });
  for (const m of text.matchAll(ITEM)) events.push({ at: m.index + m[1].length, end: m.index + m[0].length, item: m, kind: "item" });
  events.sort((a, b) => a.at - b.at);
  for (const ev of events) {
    if (inTaken(ev.at)) continue;
    if (ev.kind === "env") {
      const [, what, type] = ev.env;
      if (what === "begin") stack.push({ type, n: 0 }); else stack.pop();
      const line = doc.lineAt(ev.at);
      if (!touched(line.from, line.to) && text.slice(line.from, line.to).trim() === ev.env[0].trim()) {
        add(line.from, line.from, Decoration.line({ class: "v-env-line" }));
        hide(ev.at, ev.end);
      } else add(ev.at, ev.end, Decoration.mark({ class: "v-dim" }));
    } else {
      const top = stack[stack.length - 1];
      if (!top) continue;
      top.n++;
      const depth = stack.length - 1;
      const label = ev.item[2] ? ev.item[2].slice(1, -1) : top.type === "enumerate" ? `${top.n}.` : ["•", "–", "▪"][depth % 3];
      const line = doc.lineAt(ev.at);
      add(line.from, line.from, Decoration.line({ class: "v-item", attributes: { style: `--depth:${depth}` } }));
      if (!touched(line.from, ev.end)) add(ev.at, ev.end, Decoration.replace({ widget: new BulletWidget(label) }));
    }
  }

  for (const m of text.matchAll(REF)) {
    if (inTaken(m.index)) continue;
    const from = m.index, to = from + m[0].length;
    if (touched(from, to)) continue;
    const kind = m[1], key = m[2].split(",")[0].trim();
    const cite = kind.startsWith("cite");
    const label = cite ? m[2].split(",").map((k) => k.trim()).join(", ") : key;
    add(from, to, Decoration.replace({ widget: new ChipWidget(cite ? "cite" : "ref", label, m[2].split(",").map((k) => refTip(env, kind, k.trim())).join("\n")) }));
  }

  for (const m of text.matchAll(IMG)) {
    if (inTaken(m.index)) continue;
    const to = m.index + m[0].length;
    const url = env.imageUrl?.(m[1]);
    if (url) add(to, to, Decoration.widget({ widget: new ImageWidget(url, m[1]), side: 1 }));
  }

  for (const m of text.matchAll(CODE)) if (!inTaken(m.index)) add(m.index, m.index + m[0].length, NO_SPELL);

  ranges.sort((a, b) => a.from - b.from || (a.deco.startSide - b.deco.startSide) || a.to - b.to);
  const builder = new RangeSetBuilder();
  for (const r of ranges) {
    try { builder.add(r.from, r.to, r.deco); } catch { /* overlapping replace; skip */ }
  }
  return builder.finish();
}

export const visualField = StateField.define({
  create: (st) => safe(st),
  update: (value, tr) => (tr.docChanged || tr.selection || tr.reconfigured || tr.effects.some((e) => e.is(refresh))) ? safe(tr.state) : value,
  provide: (f) => EditorView.decorations.from(f),
});
export const refresh = S.StateEffect.define();

function safe(st) {
  try { return build(st); } catch (e) { console.warn("visual mode", e); return Decoration.none; }
}

export const visualTheme = EditorView.baseTheme({
  ".v-heading": { fontWeight: "650", lineHeight: "1.4" },
  ".v-h0": { fontSize: "1.9em" }, ".v-h1": { fontSize: "1.7em" }, ".v-h2": { fontSize: "1.4em" },
  ".v-h3": { fontSize: "1.2em" }, ".v-h4": { fontSize: "1.05em" }, ".v-h5": { fontSize: "1em", fontStyle: "italic" },
  ".v-bold": { fontWeight: "700" }, ".v-emph": { fontStyle: "italic" }, ".v-under": { textDecoration: "underline" },
  ".v-mono": { fontFamily: "var(--mono)" }, ".v-caps": { fontVariant: "small-caps" },
});
