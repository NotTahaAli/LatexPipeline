// Node check for scripts/serve_ui/figure.js (pure functions; the dialog needs a browser).
// Usage: node tests/figure_check.mjs
import assert from "node:assert/strict";
import { defaultLabel, figureLatex, folderList, defaultFolder, withExtension } from "../scripts/serve_ui/figure.js";

assert.equal(defaultLabel("My Plot (1).png"), "fig:my-plot-1");
assert.equal(defaultLabel(".png"), "fig:figure");

assert.equal(figureLatex({ path: "figures/plot.png", caption: "A plot of $x^2$.", label: "fig:plot" }), [
  "\\begin{figure}[htbp]", "  \\centering", "  \\includegraphics[width=0.8\\linewidth]{figures/plot.png}",
  "  \\caption{A plot of $x^2$.}", "  \\label{fig:plot}", "\\end{figure}", ""].join("\n"));
assert.equal(figureLatex({ path: "a.pdf", width: "5cm", placement: "H", label: "" }).split("\n")[0], "\\begin{figure}[H]");
assert.doesNotMatch(figureLatex({ path: "a.pdf", label: "" }), /\\label/);
assert.equal(figureLatex({ path: "a.pdf", placement: "]evil" }).split("\n")[0], "\\begin{figure}");
assert.equal(figureLatex({ path: "a.png", width: "3cm]{x}\\input{y", figure: false }), "\\includegraphics[width=3cmx\\inputy]{a.png}\n");   // cannot leave the [width=...] option
assert.equal(figureLatex({ path: "a.png", width: "", figure: false }), "\\includegraphics{a.png}\n");
assert.match(figureLatex({ path: "a.png", label: "fig:a}\\x" }), /\\label\{fig:ax\}/);

assert.deepEqual(folderList(["main.tex", "figs/a.png", "ch/one/two.tex"], ["empty/"]), ["", "ch", "ch/one", "empty", "figs"]);
assert.equal(defaultFolder(["", "ch", "Figures"]), "Figures");
assert.equal(defaultFolder(["", "ch"]), "");

assert.equal(withExtension("plot", "image.png"), "plot.png");
assert.equal(withExtension("plot.PNG", "x.png"), "plot.PNG");
assert.equal(withExtension("photo.jpg", "x.jpeg"), "photo.jpg");
assert.equal(withExtension("", "x.pdf"), "x.pdf");

console.log("figure ok");
