// Node check for scripts/serve_ui/tikz.js (TikZ subset parser and writer; the canvas needs a browser).
// Usage: node tests/tikz_check.mjs
import assert from "node:assert/strict";
import {
  parseTikz, serializeTikz, statement, splitOptions, parsePoint, layout, pathPoints, plainLabel, moveNode, movePoint,
  addNode, addEdge, removeItem, removeNode, renameNode, arrowOf, setArrow, setOptions, setLabel, isPositioning, fmt, uniqueName,
} from "../scripts/serve_ui/tikz.js";

const body = String.raw`
  % a comment; with a semicolon
  \tikzset{box/.style={draw, rounded corners}}
  \node[box, fill=blue!20] (a) at (0,0) {Start $x_1$};
  \node[box, right=2cm of a] (b) {End};
  \node (c) [circle,draw] at (1.5cm, -2) {C};
  \coordinate (o) at (3,1);
  \draw[->, thick] (a) -- (b);
  \draw (a.south) |- node[below] {via} (c) -- (o) -- cycle;
  \path[draw] (0,0) to[bend left] (1,1);
  \draw (0,0) circle (1);
  \foreach \x in {1,2,3} {
    \draw (\x,0) -- (\x,1);
  }
  \draw[red] (30:1) -- (2,2);
  \node at (b.east) {anchored};
  \begin{scope}[xshift=1cm]
  \end{scope}
`;

// Round trip: nothing changed, nothing rewritten, byte for byte.
const model = parseTikz(body);
assert.equal(serializeTikz(model), body);
const kinds = model.items.filter((it) => it.kind !== "raw" || /\S/.test(it.src)).map((it) => it.kind);
assert.deepEqual(kinds, ["raw", "raw", "node", "node", "node", "coordinate", "path", "path", "path", "raw", "raw", "raw", "raw", "raw", "raw"]);

const [a, b, c] = model.items.filter((it) => it.kind === "node");
assert.deepEqual([a.name, a.at, a.opts, a.label], ["a", { x: 0, y: 0 }, ["box", "fill=blue!20"], "Start $x_1$"]);
assert.deepEqual([b.at, b.opts], [null, ["box", "right=2cm of a"]]);
assert.deepEqual([c.name, c.at, c.opts], ["c", { x: 1.5, y: -2 }, ["circle", "draw"]]);
const paths = model.items.filter((it) => it.kind === "path");
assert.deepEqual(paths[1].steps.map((s) => s.op), [null, "|-", "--", "--"]);
assert.deepEqual(paths[1].steps[0].pt, { ref: "a", anchor: "south" });
assert.deepEqual(paths[1].steps[1].label, { opts: ["below"], text: "via" });
assert.deepEqual(paths[1].steps[3].pt, { cycle: true });
assert.deepEqual(paths[2].steps[1].opts, ["bend left"]);

// Every parsed statement written out again parses to the same thing, and the writer is stable.
const all = parseTikz(body);
for (const it of all.items) if (it.kind !== "raw") it.dirty = true;
const once = serializeTikz(all), again = parseTikz(once);
const strip = (m) => m.items.filter((it) => it.kind !== "raw").map(({ src, dirty, ...rest }) => rest);
assert.deepEqual(strip(again), strip(all));
for (const it of again.items) if (it.kind !== "raw") it.dirty = true;
assert.equal(serializeTikz(again), once);
assert.match(once, /\\node\[circle, draw\] \(c\) at \(1\.5,-2\) \{C\};/);
assert.match(once, /\\draw \(a\.south\) \|- node\[below\] \{via\} \(c\) -- \(o\) -- cycle;/);
assert.match(once, /\\path\[draw\] \(0,0\) to\[bend left\] \(1,1\);/);
assert.match(once, /\\foreach \\x in \{1,2,3\} \{\n {4}\\draw \(\\x,0\) -- \(\\x,1\);\n {2}\}/);   // raw, untouched

// Pieces.
assert.deepEqual(splitOptions("draw, label={a, b}, , fill=red!20"), ["draw", "label={a, b}", "fill=red!20"]);
assert.deepEqual(parsePoint(" -1.5 , .25cm"), { x: -1.5, y: 0.25 });
assert.equal(parsePoint("$(a)+(1,0)$"), null);
assert.equal(fmt(1 / 3), "0.333");
assert.equal(fmt(-0.0001), "0");
assert.equal(plainLabel("\\textbf{Start} $x_1$\\\\two"), "Start x_1 two");
assert.ok(isPositioning("right=2cm of a") && isPositioning("below of=b") && isPositioning("above left=of n-1") && !isPositioning("draw"));
assert.equal(parseTikz("\\node[draw] (a) at (0,0) {A} edge (b);").items[0].kind, "raw");
assert.equal(parseTikz("\\draw (0,0) -- ++(1,0);").items[0].kind, "raw");
assert.equal(parseTikz("\\node (a) {unclosed").items[0].kind, "raw");

// Layout: explicit positions, relative ones from their reference, paths through node centres.
const { byItem, byName } = layout(model);
assert.deepEqual(byItem.get(b), { x: 2.8, y: 0 });   // right=2cm of a: 2 + 0.8 between centres (approximation)
assert.deepEqual(byName.get("o"), { x: 3, y: 1 });
assert.deepEqual(pathPoints(paths[0], byName), [[[0, 0], [2.8, 0]]]);
assert.deepEqual(pathPoints(paths[1], byName), [[[0, 0], [0, -2], [1.5, -2], [3, 1], [0, 0]]]);
assert.deepEqual(layout(parseTikz("\\node[right of=a] (b) {B};\\node (a) at (1,1) {A};")).byName.get("b"), { x: 2, y: 1 });

// Edits: only the touched statements change.
const m = parseTikz(body);
const nb = m.items.find((it) => it.name === "b");
moveNode(nb, 4, 0.5);
const moved = serializeTikz(m);
assert.match(moved, /\\node\[box\] \(b\) at \(4,0\.5\) \{End\};/);
assert.equal(moved.replace(/\\node\[box\] \(b\) at \(4,0\.5\) \{End\};/, "\\node[box, right=2cm of a] (b) {End};"), body);

const p = m.items.find((it) => it.kind === "path" && it.cmd === "path");
movePoint(p, 1, 2, 2);
assert.match(serializeTikz(m), /\\path\[draw\] \(0,0\) to\[bend left\] \(2,2\);/);

const n = addNode(m, 1, 2, "New");
assert.equal(n.name, uniqueName(parseTikz(""), "n"));
const e = addEdge(m, "a", n.name);
assert.match(serializeTikz(m), /\\end\{scope\}\n {2}\\node\[draw\] \(n1\) at \(1,2\) \{New\};\n {2}\\draw\[->\] \(a\) -- \(n1\);\n$/);
assert.equal(arrowOf(e.opts), "->");
setArrow(e, "<->");
assert.deepEqual(e.opts, ["<->"]);
setArrow(e, "");
assert.equal(arrowOf(e.opts), "");
assert.equal(arrowOf(["thick", "-latex"]), "->");
assert.equal(arrowOf(["Stealth-"]), "<-");
setOptions(e, "dashed, ->");
setLabel(e, "x");
assert.equal(statement(e), "\\draw[dashed, ->] (a) -- node[midway, above] {x} (n1);");
setLabel(e, "");
assert.equal(statement(e), "\\draw[dashed, ->] (a) -- (n1);");

// Rename follows references in paths and relative positions.
assert.equal(renameNode(m, m.items.find((it) => it.name === "a"), "start"), true);
assert.equal(renameNode(m, n, "start"), false);   // taken
assert.equal(renameNode(m, n, "bad name"), false);
const renamed = serializeTikz(m);
assert.match(renamed, /\\draw\[->, thick\] \(start\) -- \(b\);/);
assert.match(renamed, /\\draw \(start\.south\) \|-/);
assert.doesNotMatch(renamed, /\(a\)/);
const rel = parseTikz("\\node (a) at (0,0) {A};\n\\node[right=of a] (b) {B};");
renameNode(rel, rel.items[0], "z");
assert.equal(serializeTikz(rel), "\\node (z) at (0,0) {A};\n\\node[right=of z] (b) {B};");

// Remove a node: the paths through it go, nodes placed relative to it get explicit positions.
const r = parseTikz("\n  \\node (a) at (0,0) {A};\n  \\node[right=of a] (b) {B};\n  \\draw (a) -- (b);\n  \\draw (b) -- (1,1);\n");
assert.equal(removeNode(r, r.items[1]), 1);
assert.equal(serializeTikz(r), "\n  \\node (b) at (1.8,0) {B};\n  \\draw (b) -- (1,1);\n");
removeItem(r, r.items.find((it) => it.kind === "path"));
assert.equal(serializeTikz(r), "\n  \\node (b) at (1.8,0) {B};\n");

// Adding to an empty picture.
const empty = parseTikz("");
addNode(empty, 0, 0, "A");
assert.equal(serializeTikz(empty), "\n  \\node[draw] (n1) at (0,0) {A};\n");

console.log("tikz ok");
