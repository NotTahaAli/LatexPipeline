// Node check for scripts/serve_ui/reviewexport.js (the formatters; the download needs a browser).
// Usage: node tests/reviewexport_check.mjs
import assert from "node:assert/strict";
import { lineOf, reviewJson, reviewMarkdown } from "../scripts/serve_ui/reviewexport.js";

const text = "\\section{A}\nhello world\nmore text\n";
const anchor = (from, quote) => ({ from, to: from + quote.length, quote, prefix: text.slice(Math.max(0, from - 32), from), suffix: text.slice(from + quote.length, from + quote.length + 32) });
assert.equal(lineOf(text, anchor(12, "hello")), 2);
assert.equal(lineOf(text, { ...anchor(12, "hello"), from: 0, to: 5 }), 2);   // moved: found again from quote and context
assert.equal(lineOf(text, anchor(0, "gone text here")), null);
assert.equal(lineOf(undefined, anchor(12, "hello")), null);

const data = {
  threads: [{ path: "main.tex", anchor: anchor(24, "more"), resolved: true, resolved_by: "Bo", time: 0,
    comments: [{ name: "Ann", time: 0, text: "Why?\n# not a heading" }, { name: "Bo", time: 60, text: "Fixed." }] }],
  suggestions: [{ path: "main.tex", name: "Cy", time: 120, anchor: anchor(18, "world"), insert: "there",
    more: [{ anchor: anchor(29, "text"), insert: "" }] }],
};
const json = reviewJson("thesis", data, { "main.tex": text }, 0);
assert.deepEqual(json.threads[0], { path: "main.tex", line: 3, quote: "more", resolved: true, resolved_by: "Bo",
  comments: [{ author: "Ann", time: "1970-01-01T00:00:00.000Z", text: "Why?\n# not a heading" }, { author: "Bo", time: "1970-01-01T00:01:00.000Z", text: "Fixed." }] });
assert.deepEqual(json.suggestions[0].changes, [{ line: 2, old: "world", new: "there" }, { line: 3, old: "text", new: "" }]);
assert.equal(json.suggestions[0].author, "Cy");

const md = reviewMarkdown(json);
assert.match(md, /^# Review of thesis/);
assert.match(md, /### main\.tex:3 \(resolved by Bo\)\n\n> more/);
assert.match(md, /\*\*Ann\*\*, 1970-01-01T00:00:00\.000Z:\n\n> Why\?\n> # not a heading\n\n\*\*Bo\*\*/);
assert.match(md, /### main\.tex:2: Cy, .*\n\nLine 2:\n\nOld:\n\n> world\n\nNew:\n\n> there/);
assert.match(md, /New:\n\n> \(nothing\)/);
assert.match(reviewMarkdown(reviewJson("d", { threads: [], suggestions: [] }, {}, 0)), /## Comments\n\nNone\.\n\n## Suggestions\n\nNone\./);

console.log("reviewexport ok");
