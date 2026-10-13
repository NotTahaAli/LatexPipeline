# Navigating the source

How to search, jump to definitions, rename labels, preview math and find your way around a document in the browser editor.

## Search and replace in all files

The sidebar's Search tab (Ctrl+Shift+F, which takes the selected text) searches every text file of the document. Results are grouped by file with a line preview; click one to open the file with the match selected.

| Option | Who | Meaning |
| --- | --- | --- |
| Aa | everyone | Match case. |
| Word | everyone | Whole words only. |
| `.*` | owner only | A Python regular expression; the replacement may use `\1` or `\g<name>`. Guests never get it, since a pattern can keep the server busy. |

- Search reads the files on disk, so text typed since the last autosave shows up after the next save.
- Replace (one match) and Replace all need the edit role. Files nobody has open are written by the server with a version in History. Files open in live co-editing are changed through the shared document by your editor (it joins the room if needed), so nobody's typing is overwritten.
- A file whose room is busy with an input-method composition is skipped and named.

| Limit | Value |
| --- | --- |
| Matches listed | 500 (replacing has no cap) |
| File size | files over 1 MB are skipped |
| Guests | 60 searches and 60 replaces a minute |
| Share links | `build.toml` and `latexmkrc` are neither searched nor changed |

## Go to definition and find uses

In `.tex` files:

| Key | Action |
| --- | --- |
| F12, or Ctrl+click (Cmd+click on a Mac) | Go to the definition of what is under the cursor. |
| Shift+F12 | List every use of a label, key, command or environment across the document, definition first. Click one to go there. |
| F2 on a `\label` or `\ref` | Rename the label everywhere (edit role). |

Go to definition follows:

- a `\ref`/`\cref`/`\eqref` label to its `\label`;
- a `\cite` key to its entry in the `.bib` file;
- a command to its `\newcommand`/`\def`/`\DeclareMathOperator`;
- an environment to its `\newenvironment` or `\newtheorem`.

On a `\usepackage` or `\documentclass` name it opens the package's documentation from texdoc.org (`https://texdoc.org/pkg/<name>`) in a new tab; hovering the name offers the same link. The server never runs `texdoc`. Ctrl+click where there is nothing to go to still adds a cursor.

Rename changes the `\label` and every `\ref`, `\cref`, `\Cref`, `\eqref`, `\autoref`, `\pageref`, `\nameref` and `\vref` to it, including one key among several. Files nobody has open are written with a version in History; files open in live co-editing change through the shared document, as with Replace all.

## Label numbers and math preview

| Setting | Default | Effect |
| --- | --- | --- |
| Settings > Editor > Label numbers | on | After a build, each `\label` and `\ref` shows the number it got in that build's `.aux` in grey after it (`?` for a label the build did not number). |
| Settings > Editor > Math preview | off | Shows each displayed equation typeset with KaTeX under its source. Figures get no preview; use the PDF pane or Typeset selection. |

## Typeset selection

"Typeset selection" (Ctrl+K, or More > Document; edit role) sets the selected LaTeX with the document's preamble on a page of its own and shows it cropped in a popup. `\ref` and `\cite` show as `??` there.

- It runs one TeX process through the build sandbox (bubblewrap when builds are sandboxed) in a fresh folder of the document's cache.
- No shell escape, 60 s of CPU, one at a time.
- Guests: 20 a minute.

## Outline, TODOs and file graph

The symbol index (any role; it reads the saved files plus the text of your open files, and over a share link not `build.toml` or `latexmkrc`) feeds:

- **Outline**: figures and tables with their captions, labels with their numbers and TODOs under the sections, with a filter box over all of them.
- **TODOs** drawer tab: lists `% TODO`, `% FIXME` and `\todo{...}` notes.
- **File graph** (Ctrl+K or More > Document): an SVG of what reads what: `\input`, `\include`, `\subfile`, `\bibliography`/`\addbibresource`, `\includegraphics` (with `\graphicspath`) and local `.sty`/`.cls` files. Missing targets are dashed; click a file to open it.

## Bookmarks

| Key | Action |
| --- | --- |
| Ctrl+Alt+K, or a click left of the line numbers | Mark the cursor's line. |
| Ctrl+Alt+L | Next mark in the file. |
| Ctrl+Alt+J | Previous mark in the file. |

The Bookmarks drawer tab lists them all. They move with edits and are kept per document in this browser, like the cursor of each file, which comes back when you reopen the file or the page.

## Symbols

The Symbols sidebar tab has Greek letters, operators, relations, arrows, accents and other symbols, searchable by name. It inserts the command at the cursor (accents with the cursor in their braces).

## PDF pane

- Settings > PDF preview > "Follow the cursor" (off by default) scrolls the PDF to the line you are on 0.8 s after the cursor stops and after each build.
- The PDF toolbar's screen button presents the PDF: full screen, one page at a time. Move with the arrow keys, Page Up/Down, Space, Home/End or a click; Esc ends.
- The button next to it opens the PDF in a new browser tab.

## Next

- [Writing in the editor](writing.md)
- [Live preview](live-preview.md)
- [Keyboard shortcuts](../reference/shortcuts.md)
