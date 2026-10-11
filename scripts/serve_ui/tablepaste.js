// Paste a table from a spreadsheet, Word or a web page as a LaTeX tabular. The paste itself stays plain text; when the
// clipboard holds a table (an HTML <table>, or tab-separated text of at least 2 rows and 2 columns) a toast offers
// "Paste as table", which swaps the pasted text for a table environment. HTML is only parsed (DOMParser, never put
// into the page) and only cell text is read. The pure functions are checked by tests/tablepaste_check.mjs.

const SPECIAL = { "\\": "\\textbackslash{}", "&": "\\&", "%": "\\%", "$": "\\$", "#": "\\#", "_": "\\_", "{": "\\{", "}": "\\}", "~": "\\textasciitilde{}", "^": "\\textasciicircum{}" };

/** Cell text as LaTeX: special characters escaped, line breaks and runs of white space folded to one space. */
export const escapeLatex = (s) => String(s).replace(/\s+/g, " ").trim().replace(/[\\&%$#_{}~^]/g, (c) => SPECIAL[c]);

/** Tab-separated text (spreadsheets quote cells holding tabs, line breaks or quotes) as rows of cells. */
export function rowsFromTsv(text) {
  const rows = [];
  let row = [], cell = "", i = 0;
  text = text.replace(/\r\n?/g, "\n").replace(/\n$/, "");
  while (i <= text.length) {
    if (cell === "" && text[i] === '"') {   // a quoted cell: "" is a quote, the cell ends at the closing quote
      let j = i + 1;
      while (j < text.length && !(text[j] === '"' && text[j + 1] !== '"')) j += text[j] === '"' ? 2 : 1;
      if (j < text.length && [undefined, "\t", "\n"].includes(text[j + 1])) { cell = text.slice(i + 1, j).replace(/""/g, '"'); i = j + 1; continue; }
    }
    const c = text[i];
    if (c === "\t") { row.push(cell); cell = ""; }
    else if (c === "\n" || c === undefined) { row.push(cell); rows.push(row); row = []; cell = ""; }
    else cell += c;
    i++;
  }
  return rows;
}

/** The first <table> of an HTML clipboard as rows of cell text (colspan becomes empty cells), or null. */
export function rowsFromHtml(html, parser = new DOMParser()) {
  const table = parser.parseFromString(html, "text/html").querySelector("table");
  if (!table) return null;
  return [...table.rows].map((tr) => [...tr.cells].flatMap((td) => [td.textContent, ...Array(Math.max(0, (td.colSpan || 1) - 1)).fill("")]));
}

/** Rows worth offering as a table: at least 2 rows and 2 columns, else null. */
export function tableRows(rows) {
  rows = (rows || []).filter((r) => r.some((c) => c.trim()));
  const cols = Math.max(0, ...rows.map((r) => r.length));
  return rows.length >= 2 && cols >= 2 ? rows.map((r) => [...r, ...Array(cols - r.length).fill("")]) : null;
}

/** The clipboard ({html, text}) as table rows, or null. */
export function clipboardTable({ html, text }, parse = rowsFromHtml) {
  return (html && /<table[\s>]/i.test(html) && tableRows(parse(html))) || (text && text.includes("\t") && tableRows(rowsFromTsv(text))) || null;
}

/** A table environment; booktabs rules when the document loads booktabs, else \hline. The first row is the header. */
export function toLatex(rows, booktabs) {
  const [top, mid, bottom] = booktabs ? ["\\toprule", "\\midrule", "\\bottomrule"] : ["\\hline", "\\hline", "\\hline"];
  const line = (r) => "    " + r.map(escapeLatex).join(" & ") + " \\\\";
  return ["\\begin{table}[htbp]", "  \\centering", `  \\begin{tabular}{${"l".repeat(rows[0].length)}}`, "    " + top, line(rows[0]), "    " + mid,
    ...rows.slice(1).map(line), "    " + bottom, "  \\end{tabular}", "  \\caption{}", "\\end{table}", ""].join("\n");
}

export const usesBooktabs = (text) => /\\usepackage\s*(?:\[[^\]]*\])?\s*\{[^}]*\bbooktabs\b/.test(text || "");

/**
 * The editor extension. ctx = {toast(...nodes), el, preamble() -> Promise<string>}: preamble is the text where
 * \usepackage{booktabs} would be (main.tex and the open file).
 */
export function tablePaste(V, ctx) {
  return V.EditorView.domEventHandlers({
    paste(e, view) {
      const data = e.clipboardData;
      if (!data || data.files?.length) return false;
      const rows = clipboardTable({ html: data.getData("text/html"), text: data.getData("text/plain") });
      if (!rows) return false;
      const from = view.state.selection.main.from, plain = data.getData("text/plain").replace(/\r\n?/g, "\n");
      setTimeout(() => {
        const go = ctx.el("button", { type: "button", className: "link tp-go", textContent: "Paste as table" });
        go.onclick = async () => {
          const latex = toLatex(rows, usesBooktabs(await ctx.preamble().catch(() => "")));
          // shortcut: the pasted text must still be where it went; an edit before it in between makes this refuse.
          if (!plain || view.state.sliceDoc(from, from + plain.length) !== plain) { ctx.toast(ctx.el("span", { textContent: "The pasted text moved or changed; paste again." })); return; }
          view.dispatch({ changes: { from, to: from + plain.length, insert: latex }, selection: { anchor: from + latex.length }, userEvent: "input.paste" });
          ctx.toast(ctx.el("span", { textContent: `Pasted a ${rows.length} × ${rows[0].length} table.` }));
          view.focus();
        };
        ctx.toast(ctx.el("span", { textContent: `Pasted ${rows.length} rows of a table as text.` }), go);
      }, 0);
      return false;   // the plain paste goes ahead; the toast offers the table
    },
  });
}
