// Table editing: the tabular, tabularx or longtable under the cursor as a grid (rows, columns, cells, column
// spec, rule style) written back as one edit, and "Align &", which pads the columns in the source. Cells are LaTeX
// source and stay as typed (\multicolumn, comments, \rowcolor...); only the row and column layout is rewritten.
// The pure functions are checked by tests/tableedit_check.mjs; the dialog needs a browser.

export const TABLE_ENVS = ["tabular", "tabularx", "longtable"];

/** The text with every comment (unescaped % to the end of the line) blanked out; same length, so offsets carry over. */
export const maskComments = (text) => text.replace(/(?<!\\)((?:\\\\)*)%[^\n]*/g, (m, bs) => bs + " ".repeat(m.length - bs.length));

/** End (exclusive) of the balanced group opening at text[i] ("{" or "["), or -1. */
export function groupEnd(text, i, open = "{", close = "}") {
  if (text[i] !== open) return -1;
  for (let depth = 0; i < text.length; i++) {
    if (text[i] === "\\") { i++; continue; }
    if (text[i] === open) depth++;
    else if (text[i] === close && --depth === 0) return i + 1;
  }
  return -1;
}

const skipWs = (text, i) => { while (i < text.length && /\s/.test(text[i])) i++; return i; };

/**
 * The innermost \begin{name}...\end{name} (name in names) around pos, outside comments:
 * {from, to, name, headFrom (after \begin{name}), endFrom (at \end)} or null.
 */
export function findEnv(text, pos, names) {
  const src = maskComments(text), stacks = {};
  let best = null;
  for (const m of src.matchAll(/\\(begin|end)\s*\{([A-Za-z]+\*?)\}/g)) {
    if (!names.includes(m[2])) continue;
    const stack = (stacks[m[2]] ||= []);
    if (m[1] === "begin") { stack.push(m); continue; }
    const b = stack.pop();
    if (!b) continue;
    const from = b.index, to = m.index + m[0].length;
    if (from <= pos && pos <= to && (!best || from > best.from)) best = { from, to, name: m[2], headFrom: from + b[0].length, endFrom: m.index };
  }
  return best;
}

/** The table around pos: {from, to, name, specFrom, specTo, spec, bodyFrom, bodyTo} (offsets in text) or null. */
export function findTable(text, pos) {
  const env = findEnv(text, pos, TABLE_ENVS);
  if (!env) return null;
  const src = maskComments(text);
  let i = skipWs(src, env.headFrom);
  if (src[i] === "[") i = skipWs(src, groupEnd(src, i, "[", "]"));
  if (env.name === "tabularx") { const w = groupEnd(src, i); if (w < 0) return null; i = skipWs(src, w); }
  const end = groupEnd(src, i);
  if (end < 0 || end > env.endFrom) return null;
  return { ...env, specFrom: i + 1, specTo: end - 1, spec: text.slice(i + 1, end - 1), bodyFrom: end, bodyTo: env.endFrom };
}

// Rules and comment lines that sit between rows (they travel with the row below them).
const PRE = /^\s*(%[^\n]*(?:\n|$)|\\(?:hline|toprule|midrule|bottomrule|endhead|endfirsthead|endfoot|endlastfoot|morecmidrules)(?![A-Za-z])|\\(?:cline|hhline)\s*\{[^}]*\}|\\cmidrule\s*(?:\([^)]*\))?\s*(?:\[[^\]]*\])?\s*\{[^}]*\}|\\addlinespace(?:\s*\[[^\]]*\])?)/;

function splitPre(text) {
  const pre = [];
  for (let m; (m = PRE.exec(text));) { pre.push(m[1].trim()); text = text.slice(m[0].length); }
  return [pre, text];
}

/**
 * A table body as {rows: [{pre: [rule or comment lines], cells: [source], end: "\\\\" | "\\\\[2pt]" | ""}], tail: [...],
 * indent, close}. Cells split at & outside braces and nested environments; rows end at \\ (with * or [len]).
 */
export function parseBody(body) {
  const m = maskComments(body), rows = [];
  let cells = [], start = 0, depth = 0, env = 0;
  const finish = (i, j) => {
    cells.push(body.slice(start, i));
    const [pre, first] = splitPre(cells[0]);
    rows.push({ pre, cells: [first, ...cells.slice(1)].map((c) => c.trim()), end: body.slice(i, j).trim() });
    cells = []; start = j;
  };
  for (let i = 0; i < m.length; i++) {
    const c = m[i];
    if (c === "\\") {
      const word = /^\\(?:[A-Za-z]+|.)/s.exec(m.slice(i, i + 40))[0];
      if (word === "\\begin") env++;
      else if (word === "\\end") env--;
      if (depth === 0 && env === 0 && (word === "\\\\" || word === "\\tabularnewline")) {
        let j = i + word.length;
        if (word === "\\\\" && m[j] === "*") j++;
        if (m[j] === "[") { const k = groupEnd(m, j, "[", "]"); if (k > 0) j = k; }
        finish(i, j); i = j - 1; continue;
      }
      i += word.length - 1;
    } else if (c === "{") depth++;
    else if (c === "}") depth--;
    else if (c === "&" && depth === 0 && env === 0) { cells.push(body.slice(start, i)); start = i + 1; }
  }
  cells.push(body.slice(start));
  const [tail, rest] = splitPre(cells[0]);
  if (cells.length > 1 || rest.trim()) rows.push({ pre: tail, cells: [rest, ...cells.slice(1)].map((c) => c.trim()), end: "" });
  const indent = /\n([ \t]*)\S/.exec(body)?.[1] ?? "  ";
  return { rows, tail: cells.length > 1 || rest.trim() ? [] : tail, indent, close: /\n([ \t]*)$/.exec(body)?.[1] ?? "" };
}

const hasComment = (s) => maskComments(s) !== s;

/** The body text again. pad: line the & up (cells on one line without comments only). */
export function serializeBody(model, { pad = false } = {}) {
  const { rows, tail, indent } = model, width = [];
  const plain = (r) => r.cells.every((c) => !c.includes("\n") && !hasComment(c));
  if (pad) for (const r of rows.filter(plain)) r.cells.slice(0, -1).forEach((c, j) => { width[j] = Math.max(width[j] || 0, c.length); });
  const lines = [];
  rows.forEach((r, k) => {
    for (const p of r.pre) lines.push(indent + p);
    const cells = r.cells.map((c, j) => (pad && plain(r) && j < r.cells.length - 1 ? c.padEnd(width[j]) : c));
    const end = r.end || (k < rows.length - 1 ? "\\\\" : "");
    lines.push(indent + cells.map((c) => (hasComment(c) ? c + "\n" + indent : c)).join(" & ") + (end ? " " + end : ""));
  });
  for (const p of tail) lines.push(indent + p);
  return "\n" + lines.join("\n") + "\n" + model.close;
}

/** Columns in the grid: the most cells in a row, at least the spec's columns. */
export const columnCount = (model, spec) => Math.max(1, parseSpec(spec || "")?.cols.length || 0, ...model.rows.map((r) => r.cells.length));

// ---- column spec: {cols: [{pre: ">{...}", type: "p{3cm}", post: "<{...}"}], seps: [before col 0, ..., after the last]} ----
export function parseSpec(spec) {
  let i = 0, guard = 0, pending = "";
  const cols = [], seps = [""];
  while (i < spec.length) {
    const c = spec[i];
    if (/\s/.test(c)) { i++; continue; }
    if (c === "|") { seps[cols.length] += c; i++; continue; }
    if (c === "@" || c === "!" || c === ">" || c === "<") {
      const j = groupEnd(spec, skipWs(spec, i + 1));
      if (j < 0) return null;
      const part = c + spec.slice(skipWs(spec, i + 1), j);
      if (c === ">") pending += part;
      else if (c === "<" && cols.length) cols[cols.length - 1].post += part;
      else if (c === "<") return null;
      else seps[cols.length] += part;
      i = j; continue;
    }
    if (c === "*") {   // *{n}{cols}: written out
      const a = groupEnd(spec, skipWs(spec, i + 1)), b = a < 0 ? -1 : groupEnd(spec, skipWs(spec, a));
      const n = a < 0 ? NaN : Number(spec.slice(skipWs(spec, i + 1) + 1, a - 1).trim());
      if (b < 0 || !(n >= 0 && n <= 64) || ++guard > 64) return null;
      spec = spec.slice(0, i) + spec.slice(skipWs(spec, a) + 1, b - 1).repeat(n) + spec.slice(b);
      continue;
    }
    if (!/[A-Za-z]/.test(c)) return null;
    let j = i + 1;
    while (spec[j] === "{" || spec[j] === "[") { const k = groupEnd(spec, j, spec[j], spec[j] === "{" ? "}" : "]"); if (k < 0) return null; j = k; }
    cols.push({ pre: pending, type: spec.slice(i, j), post: "" });
    seps.push(""); pending = ""; i = j;
  }
  return pending ? null : { cols, seps };
}

export const serializeSpec = ({ cols, seps }) => seps[0] + cols.map((c, k) => c.pre + c.type + c.post + seps[k + 1]).join("");

/** Spec edits; a spec that cannot be read comes back unchanged. */
export function specInsert(spec, at, type = "l") {
  const s = parseSpec(spec);
  if (!s) return spec;
  const inner = s.seps.slice(1, -1), sep = inner.length && inner.every((x) => x === inner[0]) ? inner[0] : s.seps.length === 2 && s.seps[0] === s.seps[1] ? s.seps[0] : "";
  s.cols.splice(at, 0, { pre: "", type, post: "" });
  s.seps.splice(at >= s.cols.length - 1 ? at : at + 1, 0, sep);
  return serializeSpec(s);
}
export function specRemove(spec, i) {
  const s = parseSpec(spec);
  if (!s || !s.cols[i]) return spec;
  s.cols.splice(i, 1);
  s.seps.splice(i === s.cols.length ? i : i + 1, 1);
  return serializeSpec(s);
}
export function specMove(spec, i, j) {
  const s = parseSpec(spec);
  if (!s || !s.cols[i] || !s.cols[j]) return spec;
  [s.cols[i], s.cols[j]] = [s.cols[j], s.cols[i]];
  return serializeSpec(s);
}
export function specSetType(spec, i, type) {
  const s = parseSpec(spec);
  if (!s || !s.cols[i] || !/^[A-Za-z](\{[^{}]*\}|\[[^\]]*\])*$/.test(type)) return spec;
  s.cols[i].type = type;
  return serializeSpec(s);
}

// ---- rows and columns of a body model (in place) ------------------------------------------------------------------
export function insertRow(model, at, cols) {
  const prev = model.rows[at - 1];
  if (prev && !prev.end) prev.end = "\\\\";
  model.rows.splice(at, 0, { pre: [], cells: Array(cols).fill(""), end: "\\\\" });
}
/** Remove a row; its rules and comments go to the row below (a rule already there is not doubled). */
export function removeRow(model, i) {
  const [gone] = model.rows.splice(i, 1), next = model.rows[i]?.pre || model.tail;
  if (gone) next.unshift(...gone.pre.filter((p, k) => k < gone.pre.length - 1 || p !== next[0]));
}
export function moveRow(model, i, j) {
  const a = model.rows[i], b = model.rows[j];
  if (!a || !b) return;
  // The rules stay where they are (a header rule keeps sitting under the first row); the content swaps.
  [a.cells, b.cells] = [b.cells, a.cells];
}
export function insertColumn(model, at) {
  for (const r of model.rows) if (r.cells.length >= at && r.cells.length > 1) r.cells.splice(at, 0, "");
}
export function removeColumn(model, i) {
  for (const r of model.rows) if (r.cells.length > 1 && i < r.cells.length) r.cells.splice(i, 1);
}
export function moveColumn(model, i, j) {
  for (const r of model.rows) {
    if (r.cells.length < 2 || Math.max(i, j) >= r.cells.length) continue;
    [r.cells[i], r.cells[j]] = [r.cells[j], r.cells[i]];
  }
}

/** The rule style of a body: "booktabs", "hline", "mixed" or "none". */
export function ruleStyle(model) {
  const all = [...model.rows.flatMap((r) => r.pre), ...model.tail];
  const b = all.some((p) => /^\\(?:toprule|midrule|bottomrule|cmidrule)/.test(p)), h = all.some((p) => /^\\(?:hline|cline)/.test(p));
  return b && h ? "mixed" : b ? "booktabs" : h ? "hline" : "none";
}

/** Rewrite the horizontal rules as booktabs (\toprule, \midrule, \bottomrule, \cmidrule) or \hline and \cline. */
export function setRuleStyle(model, style) {
  const spots = [...model.rows.map((r) => r.pre), model.tail];
  const rules = spots.flatMap((list) => list.map((p, k) => ({ list, k })).filter(({ list: l, k }) => /^\\(?:hline|toprule|midrule|bottomrule)$/.test(l[k])));
  if (!rules.length && model.rows.length) {   // no rules yet: a frame and a header rule
    model.rows[0].pre.unshift("\\hline");
    if (model.rows.length > 1) model.rows[1].pre.unshift("\\hline");
    model.tail.push("\\hline");
    return setRuleStyle(model, style);
  }
  rules.forEach(({ list, k }, n) => {
    list[k] = style === "hline" ? "\\hline" : n === 0 ? "\\toprule" : n === rules.length - 1 && rules.length > 1 ? "\\bottomrule" : "\\midrule";
  });
  for (const list of spots) list.forEach((p, k) => {
    list[k] = style === "hline" ? p.replace(/^\\cmidrule\s*(?:\([^)]*\))?\s*(?:\[[^\]]*\])?\s*/, "\\cline") : p.replace(/^\\cline\s*/, "\\cmidrule");
  });
}

/** The whole \begin..\end text of a table with a new spec and body. */
export function tableText(text, t, spec, model, opts) {
  return text.slice(t.from, t.specFrom) + spec + text.slice(t.specTo, t.bodyFrom) + serializeBody(model, opts) + text.slice(t.bodyTo, t.to);
}

/** "Align &": the table around pos with its columns padded, as a change {from, to, insert}, or null. */
export function alignTable(text, pos) {
  const t = findTable(text, pos);
  if (!t) return null;
  const insert = serializeBody(parseBody(text.slice(t.bodyFrom, t.bodyTo)), { pad: true });
  return { from: t.bodyFrom, to: t.bodyTo, insert };
}

// ---- the dialog -------------------------------------------------------------------------------------------------
const lab = (n, label) => { n.setAttribute("aria-label", label); return n; };
const ALIGNS = [["l", "left"], ["c", "centre"], ["r", "right"], ["X", "X (fill, tabularx)"], ["p{3cm}", "paragraph p{}"]];

/**
 * Open the grid editor for the table around the cursor. ctx = {el, toast(msg), view(), preamble() -> Promise<string>}.
 * Writes back one change; refuses when the table changed in the editor meanwhile.
 */
export function openTableEditor(ctx) {
  const view = ctx.view(), { el } = ctx;
  const text = view.state.doc.toString(), t = findTable(text, view.state.selection.main.head);
  if (!t) { ctx.toast("Put the cursor inside a tabular, tabularx or longtable first."); return; }
  const original = text.slice(t.from, t.to), model = parseBody(text.slice(t.bodyFrom, t.bodyTo));
  let spec = t.spec, style = "keep", sel = [0, 0];
  const dlg = el("dialog", { className: "tbl-dlg" });
  dlg.setAttribute("aria-labelledby", "tblTitle");
  const grid = el("table", { className: "tbl-grid" }), specIn = el("input", { type: "text", id: "tblSpec", value: spec, spellcheck: false, autocomplete: "off" });
  const styleSel = el("select", { id: "tblStyle" }, ...[["keep", "As it is"], ["booktabs", "booktabs (\\toprule, \\midrule, \\bottomrule)"], ["hline", "\\hline"]].map(([v, l]) => el("option", { value: v, textContent: l })));
  styleSel.onchange = () => { style = styleSel.value; };
  specIn.onchange = () => { spec = specIn.value; render(); };
  const cols = () => columnCount(model, spec);
  const btn = (label, title, run) => el("button", { type: "button", className: "mini", textContent: label, title, onclick: () => { run(); render(); } });
  const tools = lab(el("div", { className: "tbl-tools", role: "toolbar" }, 
    el("span", { className: "mute", textContent: "Row" }),
    btn("+ above", "Insert a row above", () => { insertRow(model, sel[0], cols()); }),
    btn("+ below", "Insert a row below", () => { insertRow(model, sel[0] + 1, cols()); sel[0]++; }),
    btn("Remove", "Remove this row", () => { if (model.rows.length > 1) { removeRow(model, sel[0]); sel[0] = Math.min(sel[0], model.rows.length - 1); } }),
    btn("↑", "Move this row up", () => { if (sel[0] > 0) { moveRow(model, sel[0], sel[0] - 1); sel[0]--; } }),
    btn("↓", "Move this row down", () => { if (sel[0] < model.rows.length - 1) { moveRow(model, sel[0], sel[0] + 1); sel[0]++; } }),
    el("span", { className: "mute", textContent: "Column" }),
    btn("+ left", "Insert a column to the left", () => { insertColumn(model, sel[1]); spec = specInsert(spec, sel[1]); }),
    btn("+ right", "Insert a column to the right", () => { insertColumn(model, sel[1] + 1); spec = specInsert(spec, sel[1] + 1); sel[1]++; }),
    btn("Remove", "Remove this column", () => { if (cols() > 1) { removeColumn(model, sel[1]); spec = specRemove(spec, sel[1]); sel[1] = Math.min(sel[1], cols() - 1); } }),
    btn("←", "Move this column left", () => { if (sel[1] > 0) { moveColumn(model, sel[1], sel[1] - 1); spec = specMove(spec, sel[1], sel[1] - 1); sel[1]--; } }),
    btn("→", "Move this column right", () => { if (sel[1] < cols() - 1) { moveColumn(model, sel[1], sel[1] + 1); spec = specMove(spec, sel[1], sel[1] + 1); sel[1]++; } })), "Rows and columns");
  function render() {
    specIn.value = spec;
    const n = cols(), parsed = parseSpec(spec);
    const head = el("tr", {}, el("td"), ...Array.from({ length: n }, (_, j) => {
      const s = lab(el("select", { disabled: !parsed?.cols[j] }), `Column ${j + 1} alignment`);
      const type = parsed?.cols[j]?.type || "";
      for (const [v, l] of [...ALIGNS, ...(ALIGNS.some(([v]) => v === type) || !type ? [] : [[type, type]])]) s.append(el("option", { value: v, textContent: l, selected: v === type }));
      s.onchange = () => { spec = specSetType(spec, j, s.value); render(); };
      return el("th", { scope: "col" }, s);
    }));
    const body = model.rows.map((r, i) => el("tr", {}, el("th", { scope: "row", textContent: String(i + 1), title: r.pre.join(" ") }),
      ...Array.from({ length: n }, (_, j) => {
        const inp = lab(el("input", { type: "text", value: r.cells[j] ?? "", spellcheck: false }), `Row ${i + 1}, column ${j + 1}`);
        if (j >= r.cells.length) inp.placeholder = "·";
        if (i === sel[0] && j === sel[1]) inp.className = "sel";
        inp.onfocus = () => { sel = [i, j]; for (const x of grid.querySelectorAll("input.sel")) x.className = ""; inp.className = "sel"; };
        inp.oninput = () => { while (r.cells.length <= j) r.cells.push(""); r.cells[j] = inp.value; };
        return el("td", {}, inp);
      })));
    grid.replaceChildren(el("thead", {}, head), el("tbody", {}, ...body));
  }
  const apply = el("button", { type: "submit", className: "btn primary", textContent: "Apply" });
  const close = () => { dlg.close(); dlg.remove(); view.focus(); };
  dlg.append(el("form", { method: "dialog", onsubmit: async (e) => {
    e.preventDefault();
    const now = view.state.doc.toString();
    if (now.slice(t.from, t.to) !== original) { ctx.toast("The table changed in the editor meanwhile; open the editor again."); close(); return; }
    if (style !== "keep") setRuleStyle(model, style);
    const insert = tableText(now, t, spec, model);
    view.dispatch({ changes: { from: t.from, to: t.to, insert }, selection: { anchor: t.from }, scrollIntoView: true, userEvent: "input.table" });
    if (style === "booktabs" && !/\\usepackage\s*(?:\[[^\]]*\])?\s*\{[^}]*\bbooktabs\b/.test(await ctx.preamble().catch(() => ""))) ctx.toast("booktabs rules need \\usepackage{booktabs} in the preamble.");
    close();
  } },
  el("div", { className: "dlg-head" }, el("h2", { id: "tblTitle", textContent: `Edit ${t.name}` }), lab(el("button", { type: "button", className: "icon", textContent: "×", onclick: close }), "Close")),
  el("div", { className: "share-body" },
    el("div", { className: "tbl-row" }, el("label", { htmlFor: "tblSpec", textContent: "Column spec" }), specIn, el("label", { htmlFor: "tblStyle", textContent: "Rules" }), styleSel),
    tools, el("div", { className: "tbl-scroll" }, grid),
    el("p", { className: "mute", textContent: "Cells are LaTeX source. Rules and comments between rows stay with the row below them." }),
    el("div", { className: "actions" }, el("button", { type: "button", className: "btn", textContent: "Cancel", onclick: close }), apply))));
  dlg.addEventListener("cancel", (e) => { e.preventDefault(); close(); });
  document.body.append(dlg);
  render();
  dlg.showModal();
  grid.querySelector("input.sel")?.focus();
}

/** "Align &" on the table around the cursor, as one edit. */
export function alignAmpersands(ctx) {
  const view = ctx.view(), change = alignTable(view.state.doc.toString(), view.state.selection.main.head);
  if (!change) { ctx.toast("Put the cursor inside a tabular, tabularx or longtable first."); return; }
  if (view.state.sliceDoc(change.from, change.to) !== change.insert) view.dispatch({ changes: change, userEvent: "input.table" });
}

// ---- Visual mode: the tables of a text as rows of cells to draw (visual.js renders them) -------------------------

const RULE = /^\\(toprule|midrule|bottomrule|hline|cline|cmidrule)/;

/** The rules above a row: {kind: "thick" | "thin" | "double" | null, parts: [[first, last] columns, 1-based]}. */
function rulesOf(pre) {
  let kind = null, hlines = 0;
  const parts = [];
  for (const p of pre) {
    const m = RULE.exec(p);
    if (!m) continue;
    if (m[1] === "cline" || m[1] === "cmidrule") {
      const r = /\{\s*(\d+)\s*-\s*(\d+)\s*\}/.exec(p);
      if (r) parts.push([+r[1], +r[2]]);
    } else if (m[1] === "hline") kind = ++hlines > 1 ? "double" : kind === "thick" ? "thick" : "thin";
    else kind = m[1] === "midrule" ? (kind || "thin") : "thick";
  }
  return { kind, parts };
}

const alignOf = (type) => ({ c: "center", r: "right" }[(type || "l")[0]] || "left");

const SECTION = /^\\end(firsthead|head|foot|lastfoot)$/;

/**
 * A longtable body as it reads on its first and last page: the first head (\endfirsthead, else \endhead), the rows
 * and the last foot (\endlastfoot, else \endfoot); a \caption row becomes caption. Other bodies come back as they are.
 */
function longtableParts(body) {
  const sections = {}, cur = { pre: [], rows: [] };
  let caption = "";
  const take = (pre) => {
    for (const [k, p] of pre.entries()) {
      const s = SECTION.exec(p);
      if (!s) continue;
      sections[s[1]] = { rows: cur.rows.splice(0), trail: [...cur.pre, ...pre.slice(0, k)] };
      cur.pre = [];
      return take(pre.slice(k + 1));
    }
    cur.pre.push(...pre);
  };
  for (const r of body.rows) {
    take(r.pre);
    const c = /^\\caption\s*(?:\[[^\]]*\]\s*)?\{/.exec(r.cells.length === 1 ? r.cells[0] : "");
    if (c) { const e = groupEnd(r.cells[0], c[0].length - 1); caption = e > 0 ? r.cells[0].slice(c[0].length, e - 1) : ""; continue; }
    cur.rows.push({ ...r, pre: cur.pre.splice(0) });
  }
  take(body.tail);
  if (!Object.keys(sections).length) return { ...body, caption };
  const out = [];
  let carry = [];
  const add = (part) => {
    if (!part) return;
    for (const r of part.rows) { out.push({ ...r, pre: [...carry, ...r.pre] }); carry = []; }
    carry.push(...part.trail);
  };
  add(sections.firsthead || sections.head);
  add({ rows: cur.rows, trail: [] });
  add(sections.lastfoot || sections.foot);
  return { ...body, rows: out, tail: [...carry, ...cur.pre], caption };
}

/**
 * Every outermost tabular, tabular*, tabularx and longtable of text: {from, to, cols: [{align, left, right}],
 * rows: [{rule, cells: [{src, span, align}]}], bottom}. Cell src is LaTeX; nested tables stay inside it.
 */
export function tablesIn(text) {
  const src = maskComments(text), out = [], open = [];
  for (const m of src.matchAll(/\\(begin|end)\s*\{(tabular\*?|tabularx|longtable)\}/g)) {
    if (m[1] === "begin") { open.push(m); continue; }
    const b = open.pop();
    if (!b || b[2] !== m[2] || open.length) continue;   // only the outermost is drawn
    let i = skipWs(src, b.index + b[0].length);
    if (src[i] === "[") i = skipWs(src, groupEnd(src, i, "[", "]"));
    if (b[2] === "tabularx" || b[2] === "tabular*") { const w = groupEnd(src, i); if (w < 0) continue; i = skipWs(src, w); }
    const end = groupEnd(src, i);
    if (end < 0 || end > m.index) continue;
    const spec = parseSpec(text.slice(i + 1, end - 1));
    const body = longtableParts(parseBody(text.slice(end, m.index)));
    const cols = (spec?.cols || []).map((c, k) => ({ align: alignOf(c.type), left: spec.seps[k].includes("|"), right: spec.seps[k + 1].includes("|") }));
    const rows = body.rows.filter((r) => r.cells.length > 1 || r.cells[0]).map((r) => {
      let col = 0;
      const cells = r.cells.map((c) => {
        const mc = /^\\multicolumn\s*\{\s*(\d+)\s*\}\s*/.exec(c);
        let cell = { src: c, span: 1, align: cols[col]?.align || "left" };
        if (mc) {
          const a = mc[0].length, b2 = groupEnd(c, a), c3 = b2 < 0 ? -1 : groupEnd(c, skipWs(c, b2));
          if (c3 > 0) cell = { src: c.slice(skipWs(c, b2) + 1, c3 - 1), span: Math.max(1, +mc[1]), align: alignOf(c.slice(a + 1, b2 - 1).replace(/[|\s]/g, "")) };
        }
        col += cell.span;
        return cell;
      });
      return { rule: rulesOf(r.pre), cells };
    });
    const lastPre = body.rows.filter((r) => !(r.cells.length > 1 || r.cells[0])).flatMap((r) => r.pre);
    out.push({ from: b.index, to: m.index + m[0].length, cols, rows, bottom: rulesOf([...lastPre, ...body.tail]), caption: body.caption });
  }
  return out;
}
