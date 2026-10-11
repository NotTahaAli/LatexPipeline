// Node check for scripts/serve_ui/tablepaste.js (pure functions; the editor extension needs a browser).
// Usage: node tests/tablepaste_check.mjs
import assert from "node:assert/strict";
import { escapeLatex, rowsFromTsv, tableRows, clipboardTable, toLatex, usesBooktabs } from "../scripts/serve_ui/tablepaste.js";

assert.equal(escapeLatex("50% of $x_1 & {y}# ~a^b \\c"), "50\\% of \\$x\\_1 \\& \\{y\\}\\# \\textasciitilde{}a\\textasciicircum{}b \\textbackslash{}c");
assert.equal(escapeLatex("  two\nlines\t "), "two lines");

assert.deepEqual(rowsFromTsv("a\tb\r\n1\t2\r\n"), [["a", "b"], ["1", "2"]]);
assert.deepEqual(rowsFromTsv('"x\ty"\t"say ""hi"""\n"line\nbreak"\t\n'), [["x\ty", 'say "hi"'], ["line\nbreak", ""]]);
assert.deepEqual(rowsFromTsv('"open\tb'), [['"open', "b"]]);   // an unclosed quote is just text

assert.equal(tableRows([["a", "b"]]), null);                     // one row
assert.equal(tableRows([["a"], ["b"]]), null);                   // one column
assert.deepEqual(tableRows([["a", "b", "c"], ["1"], [" ", ""]]), [["a", "b", "c"], ["1", "", ""]]);   // padded, blank rows dropped

assert.equal(clipboardTable({ text: "just text\nmore" }), null);
assert.deepEqual(clipboardTable({ text: "a\tb\n1\t2" }), [["a", "b"], ["1", "2"]]);
// HTML wins when it holds a table; the parser is injected here (DOMParser in the browser).
assert.deepEqual(clipboardTable({ html: "<table><tr><td>h</td></tr></table>", text: "x" }, () => [["h", "i"], ["1", "2"]]), [["h", "i"], ["1", "2"]]);
assert.equal(clipboardTable({ html: "<p>no table</p>", text: "plain" }, () => assert.fail("not parsed")), null);

const rows = [["Name", "Cost $"], ["A&B", "10%"]];
assert.equal(toLatex(rows, true), [
  "\\begin{table}[htbp]", "  \\centering", "  \\begin{tabular}{ll}", "    \\toprule", "    Name & Cost \\$ \\\\", "    \\midrule",
  "    A\\&B & 10\\% \\\\", "    \\bottomrule", "  \\end{tabular}", "  \\caption{}", "\\end{table}", ""].join("\n"));
assert.match(toLatex(rows, false), /\\hline\n {4}Name/);
assert.doesNotMatch(toLatex(rows, false), /rule/);

assert.ok(usesBooktabs("\\usepackage[x]{graphicx,booktabs}"));
assert.ok(usesBooktabs("\\usepackage{booktabs}"));
assert.ok(!usesBooktabs("% no tables\n\\usepackage{array}"));

console.log("tablepaste ok");
