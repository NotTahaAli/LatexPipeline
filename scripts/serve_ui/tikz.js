// TikZ pictures as data for the visual editor (tikzedit.js). A subset is understood:
//   \node[opts] (name) at (x,y) {label};      \node[right=of a] (b) {label};      \coordinate (name) at (x,y);
//   \draw[opts] (a) -- (b) to[opts] (2,1) -| (c.north) -- node[above]{label} (d) -- cycle;   (\path alike)
// Everything else (other commands, \foreach, scopes, styles, comments, statements with polar or computed
// coordinates) stays an opaque "raw" item, kept verbatim and in order. Only items the editor changed are written
// out again, so serialize(parse(x)) === x. Pure: no DOM; checked by tests/tikz_check.mjs.

const PATH_CMDS = new Set(["draw", "path", "node", "coordinate", "fill", "filldraw", "shade", "shadedraw", "clip", "pattern", "useasboundingbox", "pic", "matrix", "graph"]);
const OPS = ["--", "-|", "|-", "to"];

// ---- statements ---------------------------------------------------------------------------------------------------
/** End of a path statement starting at i: after its top-level ";" (or the end of the text). */
function toSemicolon(s, i) {
  for (let depth = 0; i < s.length; i++) {
    const c = s[i];
    if (c === "\\") i++;
    else if (c === "%") { while (i < s.length && s[i] !== "\n") i++; }
    else if (c === "{") depth++;
    else if (c === "}") depth--;
    else if (c === ";" && depth <= 0) return i + 1;
  }
  return s.length;
}
/** End of any other statement: the end of the line, once its braces are closed. */
function toLineEnd(s, i) {
  for (let depth = 0; i < s.length; i++) {
    const c = s[i];
    if (c === "\\") i++;
    else if (c === "%") { while (i < s.length && s[i] !== "\n") i++; i--; }
    else if (c === "{") depth++;
    else if (c === "}") depth--;
    else if (c === "\n" && depth <= 0) return i;
  }
  return s.length;
}

/** The picture body as items, in order: raw text (white space, comments, anything not understood) and parsed statements. */
export function parseTikz(body) {
  const items = [];
  for (let i = 0; i < body.length;) {
    let j = i;
    while (j < body.length && (/\s/.test(body[j]) || body[j] === "%")) {
      if (body[j] === "%") while (j < body.length && body[j] !== "\n") j++;
      else j++;
    }
    if (j > i) { items.push({ kind: "raw", src: body.slice(i, j) }); i = j; continue; }
    const cmd = /^\\([A-Za-z]+)/.exec(body.slice(i, i + 40))?.[1];
    j = PATH_CMDS.has(cmd) ? toSemicolon(body, i) : toLineEnd(body, i);
    const src = body.slice(i, j);
    items.push({ ...(parseStatement(src) || { kind: "raw" }), src });
    i = j;
  }
  return { items };
}

export const serializeTikz = (model) => model.items.map((it) => (it.dirty ? statement(it) : it.src)).join("");

// ---- tokens of one statement --------------------------------------------------------------------------------------
function closing(s, i, open, close) {
  for (let depth = 0; i < s.length; i++) {
    if (s[i] === "\\") { i++; continue; }
    if (s[i] === open) depth++;
    else if (s[i] === close && --depth === 0) return i;
  }
  return -1;
}
function tokens(s) {
  const out = [];
  for (let i = 0; i < s.length;) {
    const c = s[i];
    if (/\s/.test(c)) { i++; continue; }
    let m;
    if ((m = /^\\[A-Za-z]+/.exec(s.slice(i)))) { out.push({ t: "cmd", v: m[0].slice(1) }); i += m[0].length; continue; }
    const pair = { "[": "]", "(": ")", "{": "}" }[c];
    if (pair) {
      const j = closing(s, i, c, pair);
      if (j < 0) return null;
      out.push({ t: c, v: s.slice(i + 1, j) }); i = j + 1; continue;
    }
    if (c === ";") { out.push({ t: ";" }); i++; continue; }
    if ((m = /^(--|-\||\|-)/.exec(s.slice(i)))) { out.push({ t: "op", v: m[0] }); i += 2; continue; }
    if ((m = /^[A-Za-z]+/.exec(s.slice(i)))) { out.push({ t: "word", v: m[0] }); i += m[0].length; continue; }
    return null;   // a comment, a computed coordinate, anything else: not ours
  }
  return out;
}

/** Top-level comma list of an option string, trimmed, empty entries dropped. */
export function splitOptions(s) {
  const out = [];
  let depth = 0, cur = "";
  for (let i = 0; i < s.length; i++) {
    const c = s[i];
    if (c === "\\") { cur += c + (s[i + 1] ?? ""); i++; continue; }
    if (c === "{") depth++;
    if (c === "}") depth--;
    if (c === "," && depth === 0) { out.push(cur); cur = ""; } else cur += c;
  }
  out.push(cur);
  return out.map((o) => o.trim()).filter(Boolean);
}

const NUM = String.raw`(-?(?:\d+\.?\d*|\.\d+))\s*(?:cm)?`;
const COORD = new RegExp(String.raw`^\s*${NUM}\s*,\s*${NUM}\s*$`);
const NAME = /^[A-Za-z0-9_-]+$/;
/** (x,y) in cm or a node name with an optional anchor; null when it is anything else. */
export function parsePoint(v) {
  const c = COORD.exec(v);
  if (c) return { x: Number(c[1]), y: Number(c[2]) };
  const m = /^\s*([A-Za-z0-9_-]+)(?:\.([A-Za-z0-9 ]+))?\s*$/.exec(v);
  if (m) return m[2] ? { ref: m[1], anchor: m[2].trim() } : { ref: m[1] };
  return null;
}

function parseStatement(src) {
  const tk = tokens(src);
  if (!tk || tk[0]?.t !== "cmd" || tk[tk.length - 1].t !== ";") return null;
  const cmd = tk[0].v, rest = tk.slice(1, -1);
  let k = 0;
  const take = (t, v) => (rest[k]?.t === t && (v === undefined || rest[k].v === v) ? rest[k++] : null);
  if (cmd === "node" || cmd === "coordinate") {
    const opts = [];
    let name = null, at = null, label = null;
    for (let guard = 0; k < rest.length && guard < 8; guard++) {
      let t;
      if ((t = take("["))) opts.push(...splitOptions(t.v));
      else if (!name && (t = take("("))) { if (!NAME.test(t.v.trim())) return null; name = t.v.trim(); }
      else if (!at && take("word", "at")) { const p = take("("); at = p && parsePoint(p.v); if (!at || at.ref) return null; }
      else if (cmd === "node" && label === null && (t = take("{"))) label = t.v;
      else return null;
    }
    if (k < rest.length) return null;
    if (cmd === "node") return label === null ? null : { kind: "node", cmd, opts, name, at, label };
    return name && at ? { kind: "coordinate", cmd, opts, name, at } : null;
  }
  if (cmd !== "draw" && cmd !== "path") return null;
  const opts = [];
  for (let t; (t = take("["));) opts.push(...splitOptions(t.v));
  const first = take("("), steps = [];
  if (!first || !parsePoint(first.v)) return null;
  steps.push({ op: null, pt: parsePoint(first.v) });
  while (k < rest.length) {
    const op = take("op") || take("word", "to");
    if (!op) return null;
    const step = { op: op.v, opts: [] };
    if (op.v === "to") for (let t; (t = take("["));) step.opts.push(...splitOptions(t.v));
    if (take("word", "node")) {
      const lo = [];
      for (let t; (t = take("["));) lo.push(...splitOptions(t.v));
      const text = take("{");
      if (!text) return null;
      step.label = { opts: lo, text: text.v };
    }
    if (take("word", "cycle")) step.pt = { cycle: true };
    else { const p = take("("); step.pt = p && parsePoint(p.v); if (!step.pt) return null; }
    steps.push(step);
  }
  return steps.length > 1 ? { kind: "path", cmd, opts, steps } : null;
}

// ---- writing --------------------------------------------------------------------------------------------------------
export const fmt = (n) => { const s = String(Math.round(n * 1000) / 1000); return s === "-0" ? "0" : s; };
const optText = (opts) => (opts.length ? `[${opts.join(", ")}]` : "");
const pointText = (p) => (p.cycle ? "cycle" : p.ref ? `(${p.ref}${p.anchor ? "." + p.anchor : ""})` : `(${fmt(p.x)},${fmt(p.y)})`);

/** One parsed item as TikZ. */
export function statement(it) {
  if (it.kind === "node") return `\\node${optText(it.opts)}${it.name ? ` (${it.name})` : ""}${it.at ? ` at ${pointText(it.at)}` : ""} {${it.label}};`;
  if (it.kind === "coordinate") return `\\coordinate${optText(it.opts)} (${it.name}) at ${pointText(it.at)};`;
  if (it.kind === "path") {
    return `\\${it.cmd}${optText(it.opts)} ` + it.steps.map((s, i) => (i === 0 ? pointText(s.pt)
      : `${s.op}${optText(s.opts || [])}${s.label ? ` node${optText(s.label.opts)} {${s.label.text}}` : ""} ${pointText(s.pt)}`)).join(" ") + ";";
  }
  return it.src;
}

// ---- where things are (for the canvas) --------------------------------------------------------------------------
const POSITIONING = /^(above left|above right|below left|below right|above|below|left|right)(\s+of)?\s*=\s*(?:(-?[\d.]+)\s*(?:cm)?\s+)?(?:of\s+)?([A-Za-z0-9_-]+)(?:\.[A-Za-z ]+)?$/;
const DIRS = { above: [0, 1], below: [0, -1], left: [-1, 0], right: [1, 0], "above left": [-1, 1], "above right": [1, 1], "below left": [-1, -1], "below right": [1, -1] };
export const isPositioning = (o) => POSITIONING.test(o);

/**
 * Centres of nodes and coordinates: Map item -> {x, y}, and names -> {x, y}. Relative placement (right=of a,
 * right of=a) is approximated: new syntax as distance + 0.8 between centres, old syntax as the distance.
 */
export function layout(model) {
  const byItem = new Map(), byName = new Map(), nodes = model.items.filter((it) => it.kind === "node" || it.kind === "coordinate");
  for (let pass = 0; pass < 8 && byItem.size < nodes.length; pass++) {
    for (const it of nodes) {
      if (byItem.has(it)) continue;
      let p = it.at;
      if (!p) {
        const rel = it.opts.map((o) => POSITIONING.exec(o)).find(Boolean);
        if (rel) {
          const base = byName.get(rel[4]);
          if (!base) continue;   // placed after its reference
          const [dx, dy] = DIRS[rel[1]], d = rel[3] != null ? Number(rel[3]) : 1, gap = rel[2] ? d : d + 0.8;
          p = { x: base.x + dx * gap, y: base.y + dy * gap };
        } else p = { x: 0, y: 0 };
      }
      byItem.set(it, p);
      if (it.name) byName.set(it.name, p);
    }
  }
  for (const it of nodes) if (!byItem.has(it)) byItem.set(it, { x: 0, y: 0 });   // a reference that never resolves
  return { byItem, byName };
}

/** The points of a path for drawing: [[x, y], ...] per segment run, elbows (-| and |-) included; unknown names skipped. */
export function pathPoints(it, byName) {
  const at = (p, start) => (p.cycle ? start : p.ref ? byName.get(p.ref) : p);
  const out = [];
  let prev = null, start = null;
  for (const s of it.steps) {
    const p = at(s.pt, start);
    if (!p) { prev = null; continue; }
    if (!prev) { out.push([[p.x, p.y]]); start ||= p; }
    else {
      const run = out[out.length - 1];
      if (s.op === "-|") run.push([p.x, prev.y]);
      if (s.op === "|-") run.push([prev.x, p.y]);
      run.push([p.x, p.y]);
    }
    prev = p;
  }
  return out.filter((r) => r.length > 1);
}

/** A label as plain text for the canvas: math dollars, braces and command names dropped. */
export const plainLabel = (label) => String(label).replace(/\\\\/g, " ").replace(/\\[A-Za-z]+\*?\s*/g, "").replace(/[{}$]/g, "").replace(/\s+/g, " ").trim();

// ---- edits (in place; changed items are marked dirty) -------------------------------------------------------------
const touch = (it) => { it.dirty = true; return it; };
const indentOf = (model) => {
  const first = model.items.findIndex((it) => it.kind !== "raw");
  const raw = model.items[first - 1];
  return raw?.kind === "raw" ? /([ \t]*)$/.exec(raw.src)[1] : "  ";
};

/** Put a node or coordinate at (x, y); relative placement goes, the position is now explicit. */
export function moveNode(it, x, y) {
  it.at = { x, y };
  it.opts = it.opts.filter((o) => !isPositioning(o));
  touch(it);
}
export function movePoint(it, step, x, y) { it.steps[step].pt = { x, y }; touch(it); }

function insert(model, it) {
  let at = model.items.length;
  while (at > 0 && model.items[at - 1].kind === "raw" && !/\S/.test(model.items[at - 1].src)) at--;
  const ws = indentOf(model);
  model.items.splice(at, 0, { kind: "raw", src: "\n" + ws }, touch(it));
  if (at + 2 >= model.items.length) model.items.push({ kind: "raw", src: "\n" });
  return it;
}
export const uniqueName = (model, base = "n") => {
  const used = new Set(model.items.map((it) => it.name).filter(Boolean));
  for (let i = 1; ; i++) if (!used.has(base + i)) return base + i;
};
export const addNode = (model, x, y, label = "", opts = ["draw"]) => insert(model, { kind: "node", cmd: "node", opts: [...opts], name: uniqueName(model), at: { x, y }, label });
export const addEdge = (model, a, b, opts = ["->"]) => insert(model, { kind: "path", cmd: "draw", opts: [...opts], steps: [{ op: null, pt: { ref: a } }, { op: "--", opts: [], pt: { ref: b } }] });

/** Remove an item and the line break and indent in front of it. */
export function removeItem(model, it) {
  const i = model.items.indexOf(it);
  if (i < 0) return;
  const before = model.items[i - 1];
  if (before?.kind === "raw" && !/\S/.test(before.src) && /\n/.test(before.src)) model.items.splice(i - 1, 2);
  else model.items.splice(i, 1);
}

/**
 * Remove a node or coordinate. Paths that use it go too (they would no longer compile) and nodes placed relative
 * to it get explicit positions. Returns how many paths were removed.
 */
export function removeNode(model, it) {
  const { byItem } = layout(model);
  let paths = 0;
  if (it.name) {
    for (const p of model.items.filter((x) => x.kind === "path" && x.steps.some((s) => s.pt.ref === it.name))) { removeItem(model, p); paths++; }
    for (const n of model.items.filter((x) => (x.kind === "node" || x.kind === "coordinate") && x.opts.some((o) => POSITIONING.exec(o)?.[4] === it.name))) {
      const p = byItem.get(n);
      moveNode(n, p.x, p.y);
    }
  }
  removeItem(model, it);
  return paths;
}

/** Rename a node or coordinate, and every parsed reference to it. Returns false when the name is taken or not valid. */
export function renameNode(model, it, name) {
  if (!NAME.test(name) || model.items.some((x) => x !== it && x.name === name)) return false;
  const old = it.name;
  it.name = name; touch(it);
  if (!old) return true;
  for (const x of model.items) {
    if (x.kind === "path") for (const s of x.steps) if (s.pt.ref === old) { s.pt.ref = name; touch(x); }
    if (x.kind === "node" || x.kind === "coordinate") {
      x.opts = x.opts.map((o) => (POSITIONING.exec(o)?.[4] === old ? (touch(x), o.replace(new RegExp(`(of\\s+|=\\s*)${old}(?![\\w-])`), `$1${name}`)) : o));
    }
  }
  return true;
}

// Arrow tips: an option without "=" made of tip names around one "-" (->, <-, <->, -latex, |-stealth, ...).
const ARROW = /^[<>|A-Za-z]*-[<>|A-Za-z]*$/;
export const isArrow = (o) => ARROW.test(o) && o !== "-";
/** "->", "<-", "<->" or "" for the canvas. */
export function arrowOf(opts) {
  const a = opts.find(isArrow);
  if (!a) return "";
  const [l, r] = a.split("-");
  return (l ? "<" : "") + "-" + (r ? ">" : "");
}
export function setArrow(it, arrow) {
  const i = it.opts.findIndex(isArrow);
  if (i >= 0) it.opts.splice(i, 1);
  if (arrow) it.opts.unshift(arrow);
  touch(it);
}
export function setOptions(it, text) { it.opts = splitOptions(text); touch(it); }
export function setLabel(it, text) {
  if (it.kind === "node") it.label = text;
  else if (it.kind === "path") {
    const s = it.steps[1];
    if (text) s.label = { opts: s.label?.opts || ["midway", "above"], text };
    else delete s.label;
  }
  touch(it);
}
