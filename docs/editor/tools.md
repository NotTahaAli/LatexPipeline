# Tables, pictures and formatting

This page covers the editor's tools for tables, TikZ pictures, Markdown, pasted tables, and formatting `.tex` and `.bib` files.

All of these are edits like typing: they need the edit role, a reviewer gets them as suggestions, and undo, co-editing and autosave treat them as one edit.

## Paste a table

1. Copy cells from a spreadsheet, Word or a web page.
2. Paste into the editor. The text goes in as usual.
3. If the clipboard held a table (an HTML `<table>`, or tab-separated text of at least 2 rows and 2 columns), a message offers **Paste as table**. Click it.

The pasted text becomes a `table` with a `tabular`:

- the first row is the header;
- rules are `\toprule`/`\midrule`/`\bottomrule` when the document loads `booktabs`, else `\hline`;
- `& % $ # _ { } ~ ^ \` are escaped.

The HTML is only read for cell text, never shown.

## Edit a table as a grid

With the cursor in a `tabular`, `tabularx` or `longtable`, the status bar offers **Edit table** and **Align &** (also in the command palette).

**Edit table** opens the table as a grid. You can:

- add, remove and move rows and columns;
- edit cells (they stay LaTeX source, so `\multicolumn` or a comment is kept as typed);
- set each column's alignment or edit the column spec;
- switch the rules to booktabs (`\toprule`, `\midrule`, `\bottomrule`) or `\hline`.

Apply writes the table back as one edit. It is refused if the table changed in the editor meanwhile.

**Align &** pads the columns in the source so the `&` line up.

## Edit a TikZ picture

With the cursor in a `tikzpicture`, **Edit picture** draws it on a canvas. It understands:

- `\node`s with coordinates or relative placement (`right=of a`, `below of=b`);
- `\coordinate`s;
- `\draw`/`\path` lines and arrows (`--`, `to`, `-|`, `|-`, a `node` label, `cycle`) between nodes and points.

On the canvas you can:

- drag nodes and path points, snapped to a 0.25, 0.5 or 1 cm grid, or free;
- add nodes and connect two nodes with an arrow;
- delete (paths through a deleted node go too);
- edit names (references follow), labels, options and arrows;
- move the selected node with the arrow keys and remove it with Delete.

Apply rewrites only the statements you changed. Everything else (styles, `\foreach`, scopes, comments, polar or computed coordinates) stays as written and in order. The canvas is a sketch, not TeX: the build shows the real picture.

## Markdown files

`.md` files in the document folder open with Markdown highlighting and a rendered preview beside the editor. The preview is built from DOM nodes and text, never as HTML, and links go only to http, https, mailto or relative paths. "Markdown preview on/off" in the palette toggles it.

**Insert Markdown selection as LaTeX** turns selected Markdown (headings, bold, italics, code, lists, links, quotes, code blocks) into LaTeX in place, with special characters escaped. Links need `hyperref`.

## Format a .tex file

Ctrl+K > "Format file (tex-fmt)" (also More > Document; edit role) formats the open `.tex` file, saved or not.

1. The editor sends the file's text to the server.
2. The server runs `tex-fmt --stdin` in the document folder, so its `tex-fmt.toml` applies, and returns the result.
3. The editor applies it as one edit covering only the changed part.

The server writes nothing. If the text changed while tex-fmt ran, nothing is applied.

| Limit | Value |
|---|---|
| Text size | 1 MB |
| Requests on a shared link | 20 a minute |

Without `tex-fmt` on the server's `PATH` you get a message.

## Tidy a .bib file

Ctrl+K > "Tidy .bib file (sort entries, align fields)" (edit role) does what `build.py --tidy-bib` does, to the open `.bib` file's text, saved or not. It applies the result as one edit like Format file and lists possible duplicates in the message. The server writes nothing.

## Next

- [Writing LaTeX](writing.md)
- [Editor overview](overview.md)
- [Keyboard shortcuts](../reference/shortcuts.md)
