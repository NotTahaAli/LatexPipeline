// Pure helpers of the navigation features (nav.js, navpanels.js): what name is under the cursor, where label numbers
// go, the file graph's layout, symbol search and the crop of a typeset selection. No imports and no DOM, so
// tests/nav_check.mjs runs them in Node.

const REF = "ref|eqref|cref|Cref|autoref|pageref|nameref|vref|Vref|vpageref|labelcref|cpageref|Cpageref|zref";
const CITE = "cite[A-Za-z]*|[Cc]itep?|[Cc]itet|[Pp]arencite|[Tt]extcite|[Aa]utocite|[Ff]ootcite|nocite|smartcite|supercite|fullcite|footfullcite";
const KEYED = new RegExp(String.raw`\\(?:(label)|(${REF})|(${CITE})|(usepackage|RequirePackage|documentclass|LoadClass))\*?\s*(?:\[[^\]]*\]\s*)*\{([^{}]*)\}`, "g");
const ENV = /\\(?:begin|end)\s*\{([^{}]*)\}/g;
const NUMBERED = new RegExp(String.raw`\\(?:label|${REF})\*?\s*\{([^{}]*)\}`, "g");

/** Each comma-separated key of a group that starts at `at`: {key, from, to}. */
function split(keys, at) {
  const out = [];
  for (const part of keys.split(",")) {
    const key = part.trim();
    if (key) out.push({ key, from: at + part.indexOf(key), to: at + part.indexOf(key) + key.length });
    at += part.length + 1;
  }
  return out;
}

/**
 * The name under column `col` of a source line: {kind: "label"|"cite"|"package"|"env"|"command", name, from, to}
 * (columns in the line), or null. \ref keys are labels; \documentclass names count as packages (texdoc has both).
 */
export function symbolAt(line, col) {
  for (const m of line.matchAll(KEYED)) {
    if (col < m.index || col > m.index + m[0].length) continue;
    const kind = m[1] || m[2] ? "label" : m[3] ? "cite" : "package";
    const keys = split(m[5], m.index + m[0].length - 1 - m[5].length);
    const hit = keys.find((k) => col >= k.from && col <= k.to) || (keys.length === 1 ? keys[0] : null);
    if (hit) return { kind, name: hit.key, from: hit.from, to: hit.to };
    if (col > m.index + m[0].length - 2 - m[5].length) return null;   // in the braces, between keys
  }
  for (const m of line.matchAll(ENV)) {
    const name = m[1].trim(), from = m.index + m[0].indexOf("{") + 1;
    if (name && col >= m.index && col <= m.index + m[0].length) return { kind: "env", name, from, to: from + m[1].length };
  }
  for (const m of line.matchAll(/\\([A-Za-z]+)/g)) {
    if (col >= m.index && col <= m.index + m[0].length) return { kind: "command", name: m[1], from: m.index, to: m.index + m[0].length };
  }
  return null;
}

/** Where label numbers go on a line: after each \label{..} and \ref-like command, {at, keys} (at: column after the }). */
export function numberSpots(line) {
  const out = [];
  const code = line.replace(/(?<!\\)((?:\\\\)*)%.*$/, "$1");   // not in a comment
  for (const m of code.matchAll(NUMBERED)) {
    const keys = split(m[1], 0).map((k) => k.key);
    if (keys.length) out.push({ at: m.index + m[0].length, keys });
  }
  return out;
}

/**
 * Columns for the file graph: main.tex in column 0, each file one column right of the first file that reaches
 * it (breadth first), files nothing reaches in a last column. Returns {nodes: [{id, col, row, missing}], cols, rows}.
 */
export function layoutGraph(edges, root = "main.tex") {
  const out = new Map(), missing = new Set();
  for (const e of edges) { out.set(e.from, [...(out.get(e.from) || []), e.to]); if (e.missing) missing.add(e.to); }
  const col = new Map([[root, 0]]), queue = [root];
  while (queue.length) {
    const at = queue.shift();
    for (const to of out.get(at) || []) if (!col.has(to)) { col.set(to, col.get(at) + 1); queue.push(to); }
  }
  const last = Math.max(0, ...col.values()) + 1;
  for (const e of edges) for (const id of [e.from, e.to]) if (!col.has(id)) col.set(id, last);
  const rows = new Map(), nodes = [];
  for (const [id, c] of col) { const r = rows.get(c) || 0; rows.set(c, r + 1); nodes.push({ id, col: c, row: r, missing: missing.has(id) }); }
  return { nodes, cols: Math.max(...col.values()) + 1, rows: Math.max(1, ...rows.values()) };
}

/** Symbols whose name, command or group holds every word typed. */
export function filterSymbols(list, query) {
  const words = query.toLowerCase().split(/\s+/).filter(Boolean);
  return list.filter((s) => { const hay = `${s.name} ${s.cmd} ${s.group}`.toLowerCase(); return words.every((w) => hay.includes(w)); });
}

/** Bounding box of the non-white pixels of an RGBA image, plus a margin; null for a blank image. */
export function cropBox(data, width, height, margin = 8) {
  let x0 = width, y0 = height, x1 = -1, y1 = -1;
  for (let y = 0; y < height; y++) {
    for (let x = 0; x < width; x++) {
      const i = (y * width + x) * 4;
      if (data[i + 3] > 16 && (data[i] < 240 || data[i + 1] < 240 || data[i + 2] < 240)) {
        if (x < x0) x0 = x; if (x > x1) x1 = x; if (y < y0) y0 = y; if (y > y1) y1 = y;
      }
    }
  }
  if (x1 < 0) return null;
  const x = Math.max(0, x0 - margin), y = Math.max(0, y0 - margin);
  return { x, y, w: Math.min(width, x1 + margin + 1) - x, h: Math.min(height, y1 + margin + 1) - y };
}

/** texdoc.org's documented shortcut to a package's first documentation file (/pkg/<name> -> /serve/<name>/0). */
export const docUrl = (name) => `https://texdoc.org/pkg/${encodeURIComponent(name)}`;

/** A label name the server's rename accepts (navigate.good_label). */
export const goodLabel = (name) => typeof name === "string" && /^[^\s{}\\,%#]{1,200}$/.test(name);
