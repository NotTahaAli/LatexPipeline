// Node check for scripts/serve_ui/tableedit.js (pure functions; the dialog needs a browser).
// Usage: node tests/tableedit_check.mjs
import assert from "node:assert/strict";
import {
  maskComments, findEnv, findTable, parseBody, serializeBody, parseSpec, serializeSpec, specInsert, specRemove, specMove, specSetType,
  insertRow, removeRow, moveRow, insertColumn, removeColumn, moveColumn, ruleStyle, setRuleStyle, tableText, alignTable, columnCount,
  tablesIn,
} from "../scripts/serve_ui/tableedit.js";

assert.equal(maskComments("a % b\n50\\% c %d"), "a    \n50\\% c   ");

const doc = [
  "Text % \\begin{tabular}{l} not a table",
  "\\begin{table}",
  "  \\begin{tabular}[t]{|l|c|r|}",
  "    \\hline",
  "    Name & Qty & {Cost, \\&} \\\\",
  "    \\hline",
  "    % a comment line",
  "    A & \\begin{tabular}{c} x \\\\ y \\end{tabular} & 1 \\\\[2pt]",
  "    \\multicolumn{2}{c}{Total} & 3 \\\\",
  "    \\hline",
  "  \\end{tabular}",
  "\\end{table}",
].join("\n");

// The innermost table around the cursor; the commented-out \begin does not count.
const at = (s) => doc.indexOf(s);
const outer = findTable(doc, at("Name"));
assert.equal(outer.name, "tabular");
assert.equal(outer.spec, "|l|c|r|");
assert.equal(doc.slice(outer.from, outer.from + 14), "\\begin{tabular");
assert.ok(doc.slice(outer.to - 13, outer.to) === "\\end{tabular}" && doc.slice(outer.to).startsWith("\n\\end{table}"));
assert.equal(findTable(doc, at("x \\\\ y")).spec, "c");
assert.equal(findTable(doc, at("Text")), null);
assert.equal(findEnv(doc, at("Text"), ["tabular"]), null);
assert.equal(findTable("\\begin{tabularx}{\\linewidth}{lX} a & b \\end{tabularx}", 30).spec, "lX");
assert.equal(findTable("\\begin{longtable}[c]{ll} a & b \\\\ \\end{longtable}", 25).name, "longtable");

// Rows split at \\ outside braces and nested environments; rules and comments travel with the row below.
const body = doc.slice(outer.bodyFrom, outer.bodyTo), model = parseBody(body);
assert.equal(model.rows.length, 3);
assert.deepEqual(model.rows[0], { pre: ["\\hline"], cells: ["Name", "Qty", "{Cost, \\&}"], end: "\\\\" });
assert.deepEqual(model.rows[1].pre, ["\\hline", "% a comment line"]);
assert.deepEqual(model.rows[1].cells, ["A", "\\begin{tabular}{c} x \\\\ y \\end{tabular}", "1"]);
assert.equal(model.rows[1].end, "\\\\[2pt]");
assert.deepEqual(model.tail, ["\\hline"]);
assert.equal(model.indent, "    ");
assert.equal(model.close, "  ");
assert.equal(columnCount(model, "|l|c|r|"), 3);

// Round trip: serialize(parse(x)) is stable and parses back to the same model; a tidy body comes back byte for byte.
const once = serializeBody(model);
assert.deepEqual(parseBody(once), model);
assert.equal(serializeBody(parseBody(once)), once);
assert.equal(once, body);
const messy = "\n a&b\\\\  c &d \\\\ \\bottomrule\n";
assert.equal(serializeBody(parseBody(messy)), "\n a & b \\\\\n c & d \\\\\n \\bottomrule\n");
// A last row without \\, and a comment inside a cell keeps its line break so it cannot swallow the &.
assert.deepEqual(parseBody("\n  a & b \\\\\n  c & d\n").rows[1], { pre: [], cells: ["c", "d"], end: "" });
const commented = parseBody("\n  a % note\n  & b \\\\\n");
assert.deepEqual(commented.rows[0].cells, ["a % note", "b"]);
assert.deepEqual(parseBody(serializeBody(commented)), commented);
// \tabularnewline ends a row too; longtable heads stay as lines of their own.
assert.deepEqual(parseBody("\n a & b \\tabularnewline\n").rows[0].end, "\\tabularnewline");
assert.deepEqual(parseBody("\n H & I \\\\ \\hline \\endhead\n a & b \\\\\n").rows[1].pre, ["\\hline", "\\endhead"]);

// Align &: columns padded; rows with comments or line breaks in cells are left as they are.
const aligned = alignTable("\\begin{tabular}{ll}\n  Long name & x \\\\\n  a & yy \\\\\n\\end{tabular}", 25);
assert.equal(aligned.insert, "\n  Long name & x \\\\\n  a         & yy \\\\\n");
assert.equal(alignTable("no table", 2), null);

// Column spec.
assert.deepEqual(parseSpec(">{\\bfseries}l|p{3cm}<{x}@{}c"), { cols: [{ pre: ">{\\bfseries}", type: "l", post: "" }, { pre: "", type: "p{3cm}", post: "<{x}" }, { pre: "", type: "c", post: "" }], seps: ["", "|", "@{}", ""] });
assert.equal(serializeSpec(parseSpec("|l|c|")), "|l|c|");
assert.equal(parseSpec("*{3}{c|}").cols.length, 3);
assert.equal(parseSpec("l}"), null);
assert.equal(parseSpec("S[table-format=2.1]l").cols[0].type, "S[table-format=2.1]");
assert.equal(specInsert("|l|c|", 1), "|l|l|c|");
assert.equal(specInsert("|l|c|", 2, "r"), "|l|c|r|");
assert.equal(specInsert("|ll|", 2), "|lll|");
assert.equal(specInsert("|ll|", 0), "|lll|");
assert.equal(specRemove("|l|c|r|", 1), "|l|r|");
assert.equal(specRemove("|l|c|r|", 2), "|l|c|");
assert.equal(specMove("l|c|r", 0, 1), "c|l|r");
assert.equal(specSetType("lcr", 2, "p{2cm}"), "lcp{2cm}");
assert.equal(specSetType("lcr", 2, "}evil{"), "lcr");
assert.equal(specInsert("l}", 0), "l}");   // unreadable: unchanged

// Rows and columns.
const m = parseBody("\n  \\toprule\n  h1 & h2 \\\\\n  \\midrule\n  a & b \\\\\n  c & d \\\\\n  \\bottomrule\n");
insertRow(m, 2, 2);
assert.deepEqual(m.rows.map((r) => r.cells), [["h1", "h2"], ["a", "b"], ["", ""], ["c", "d"]]);
removeRow(m, 2);
moveRow(m, 1, 2);
assert.deepEqual(m.rows.map((r) => r.cells), [["h1", "h2"], ["c", "d"], ["a", "b"]]);
assert.deepEqual(m.rows[1].pre, ["\\midrule"]);   // the header rule stays under the header
insertColumn(m, 1);
assert.deepEqual(m.rows[0].cells, ["h1", "", "h2"]);
moveColumn(m, 0, 2);
assert.deepEqual(m.rows[0].cells, ["h2", "", "h1"]);
removeColumn(m, 1);
assert.deepEqual(m.rows.map((r) => r.cells), [["h2", "h1"], ["d", "c"], ["b", "a"]]);
removeRow(m, 1);   // its \midrule moves to the row below
assert.deepEqual(m.rows[1].pre, ["\\midrule"]);
assert.deepEqual(parseBody(serializeBody(m)), m);

// Rule styles.
assert.equal(ruleStyle(m), "booktabs");
setRuleStyle(m, "hline");
assert.equal(ruleStyle(m), "hline");
assert.deepEqual([m.rows[0].pre, m.rows[1].pre, m.tail], [["\\hline"], ["\\hline"], ["\\hline"]]);
setRuleStyle(m, "booktabs");
assert.deepEqual([m.rows[0].pre, m.rows[1].pre, m.tail], [["\\toprule"], ["\\midrule"], ["\\bottomrule"]]);
const bare = parseBody("\n a & b \\\\\n c & d \\\\\n");
assert.equal(ruleStyle(bare), "none");
setRuleStyle(bare, "booktabs");
assert.deepEqual([bare.rows[0].pre, bare.rows[1].pre, bare.tail], [["\\toprule"], ["\\midrule"], ["\\bottomrule"]]);
const cl = parseBody("\n \\cline{1-2} a & b \\\\\n");
setRuleStyle(cl, "booktabs");
assert.deepEqual(cl.rows[0].pre, ["\\toprule", "\\cmidrule{1-2}"]);   // no rules yet: a frame was added

// The whole table again with a new spec: everything outside the body and spec is unchanged.
const t = findTable(doc, at("Name")), tm = parseBody(doc.slice(t.bodyFrom, t.bodyTo));
insertColumn(tm, 3);
const out = tableText(doc, t, specInsert(t.spec, 3), tm);
assert.ok(out.startsWith("\\begin{tabular}[t]{|l|c|r|l|}\n    \\hline\n    Name & Qty & {Cost, \\&} &  \\\\"));
assert.ok(out.endsWith("\\end{tabular}"));

// Visual mode: every outermost table, with column alignment, rules and \multicolumn spans.
const shown = tablesIn(doc);
assert.equal(shown.length, 1);   // the nested tabular and the commented-out one are not tables of their own
assert.equal(doc.slice(shown[0].from, shown[0].to).split("\n").length, 9);
assert.deepEqual(shown[0].cols.map((c) => [c.align, c.left, c.right]), [["left", true, true], ["center", true, true], ["right", true, true]]);
assert.deepEqual(shown[0].rows.map((r) => r.cells.map((c) => [c.span, c.align])), [[[1, "left"], [1, "center"], [1, "right"]], [[1, "left"], [1, "center"], [1, "right"]], [[2, "center"], [1, "right"]]]);
assert.equal(shown[0].rows[2].cells[0].src, "Total");
assert.deepEqual(shown[0].rows.map((r) => r.rule.kind), ["thin", "thin", null]);
assert.equal(shown[0].bottom.kind, "thin");
const bk = tablesIn("\\begin{tabularx}{\\linewidth}{lX}\\toprule a & b \\\\ \\cmidrule(lr){2-2} c & d \\\\ \\bottomrule\\end{tabularx} \\begin{tabular*}{5cm}{r}x\\end{tabular*}");
assert.deepEqual(bk.map((t) => t.rows.length), [2, 1]);
assert.deepEqual([bk[0].rows[0].rule.kind, bk[0].rows[1].rule, bk[0].bottom.kind], ["thick", { kind: null, parts: [[2, 2]] }, "thick"]);
assert.equal(bk[1].cols[0].align, "right");
assert.equal(tablesIn("\\begin{tabular}{l} open").length, 0);

console.log("tableedit ok");
