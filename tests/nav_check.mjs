// Node check for scripts/serve_ui/navcore.js (name under the cursor, label number spots, file graph layout, symbol
// search, crop of a typeset selection) and the symbol data. Usage: node nav_check.mjs <path to serve_ui>
import assert from "node:assert/strict";
import path from "node:path";
import { pathToFileURL } from "node:url";

const dir = path.resolve(process.argv[2] || "scripts/serve_ui");
const load = (name) => import(pathToFileURL(path.join(dir, name)).href);
const { symbolAt, numberSpots, layoutGraph, filterSymbols, cropBox, docUrl, goodLabel } = await load("navcore.js");
const { SYMBOLS, GROUPS } = await load("symbols.js");

// The name under the cursor.
const at = (line, col) => { const s = symbolAt(line, col); return s && [s.kind, s.name, line.slice(s.from, s.to)]; };
assert.deepEqual(at("See \\ref{sec:a} here", 10), ["label", "sec:a", "sec:a"]);
assert.deepEqual(at("See \\ref{sec:a} here", 5), ["label", "sec:a", "sec:a"]);   // on the command: its one key
assert.deepEqual(at("\\cref{a, bb}", 9), ["label", "bb", "bb"]);
assert.deepEqual(at("\\cref{a, bb}", 2), ["command", "cref", "\\cref"]);   // several keys: the command itself
assert.deepEqual(at("\\label{fig:x}", 8), ["label", "fig:x", "fig:x"]);
assert.deepEqual(at("\\citep[p.~2]{knuth84,lamport}", 22), ["cite", "lamport", "lamport"]);
assert.deepEqual(at("\\usepackage[utf8]{inputenc,amsmath}", 30), ["package", "amsmath", "amsmath"]);
assert.deepEqual(at("\\documentclass{article}", 17), ["package", "article", "article"]);
assert.deepEqual(at("\\begin{theorem} x", 9), ["env", "theorem", "theorem"]);
assert.deepEqual(at("a \\R b", 3), ["command", "R", "\\R"]);
assert.equal(symbolAt("plain words", 3), null);
assert.equal(symbolAt("\\cref{a, b}", 8), null);   // on the space between keys

// Label numbers go after the closing brace; never inside a comment.
assert.deepEqual(numberSpots("\\section{A}\\label{s} see \\eqref{e1} and \\cref{a,b} % \\ref{c}"),
  [{ at: 20, keys: ["s"] }, { at: 35, keys: ["e1"] }, { at: 50, keys: ["a", "b"] }]);
assert.deepEqual(numberSpots("50\\% of \\ref{x}"), [{ at: 15, keys: ["x"] }]);

// File graph: main.tex left, breadth first, unreachable files in a last column, missing files marked.
const g = layoutGraph([
  { from: "main.tex", to: "a.tex" }, { from: "main.tex", to: "refs.bib" }, { from: "a.tex", to: "fig.png" },
  { from: "a.tex", to: "gone", missing: true }, { from: "old.tex", to: "fig.png" },
]);
const where = Object.fromEntries(g.nodes.map((n) => [n.id, [n.col, n.row, n.missing]]));
assert.deepEqual(where, { "main.tex": [0, 0, false], "a.tex": [1, 0, false], "refs.bib": [1, 1, false], "fig.png": [2, 0, false], gone: [2, 1, true], "old.tex": [3, 0, false] });
assert.deepEqual([g.cols, g.rows], [4, 2]);

// Symbols: every group has data, search matches command, words and group.
assert.ok(GROUPS.every((grp) => SYMBOLS.some((s) => s.group === grp)));
assert.equal(new Set(SYMBOLS.map((s) => s.group + s.cmd)).size, SYMBOLS.length);
assert.ok(SYMBOLS.every((s) => s.cmd.startsWith("\\") && s.ch));
assert.deepEqual(filterSymbols(SYMBOLS, "alpha").map((s) => s.cmd), ["\\alpha"]);
assert.ok(filterSymbols(SYMBOLS, "implies").some((s) => s.cmd === "\\Rightarrow"));
assert.ok(filterSymbols(SYMBOLS, "arrows map").some((s) => s.cmd === "\\mapsto"));

// Crop: the box around non-white pixels, with a margin, inside the image.
const img = new Uint8ClampedArray(10 * 6 * 4).fill(255);
const ink = (x, y) => img.set([0, 0, 0, 255], (y * 10 + x) * 4);
assert.equal(cropBox(img, 10, 6), null);
ink(3, 2); ink(5, 3);
assert.deepEqual(cropBox(img, 10, 6, 1), { x: 2, y: 1, w: 5, h: 4 });
assert.deepEqual(cropBox(img, 10, 6, 20), { x: 0, y: 0, w: 10, h: 6 });

assert.equal(docUrl("amsmath"), "https://texdoc.org/pkg/amsmath");
assert.ok(goodLabel("sec:intro") && !goodLabel("a b") && !goodLabel("a,b") && !goodLabel("") && !goodLabel("\\x"));
console.log("nav_check: ok");
