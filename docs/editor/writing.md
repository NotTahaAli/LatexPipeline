# Writing LaTeX

This page covers the source editor's aids for writing LaTeX: folding, hover cards, the code check, completion, formatting commands, structural edits, math shortcuts, snippets and paste conversion.

Folding, hover cards, the code check and completion work in `.tex`, `.sty`, `.cls`, `.ltx` and `.tikz` files. Every key below is also listed in [Keyboard shortcuts](../reference/shortcuts.md).

## Fold sections and environments

Click the gutter arrow, or press Ctrl+Shift+[ and Ctrl+Shift+] (Cmd+Alt+[ and ] on a Mac). Ctrl+Alt+[ folds all and Ctrl+Alt+] unfolds all. You can fold:

- a section, to the next heading of the same or a higher level (`\part` to `\subparagraph`);
- an environment, to its `\end`;
- the preamble;
- `% region` ... `% endregion` blocks.

## Hover cards

- **Math** (`$...$`, `\(...\)`, `\[...\]`, `$$...$$`, `equation`, `align` and friends) is shown rendered with KaTeX in source mode.
- **`\cite` key:** the entry's author, year and title.
- **`\ref` label:** the line it was set on.
- **`\includegraphics{...}`:** a thumbnail of a png, jpg, gif, svg or webp figure. PDF and EPS figures only say so.

## Check the structure as you type

0.6 s after you stop typing, the editor underlines:

- unmatched `\begin`/`\end` and unbalanced braces;
- `\left` without `\right`;
- unclosed `$`, `$$`, `\(` and `\[`, and math running across a blank line.

Ctrl+Shift+M lists the findings and F8 jumps to the next. Comments, `verbatim`, `lstlisting`, `minted`, `comment`, `\verb`, URLs and the bodies of `\newcommand`, `\def` and friends are skipped.

| To turn it off | Write or set |
|---|---|
| For one file | A line `%%novalidate` |
| For a region | `%%begin novalidate` ... `%%end novalidate` |
| Everywhere | Settings > Editor |

It is a quick structural check, not a LaTeX run: the build log stays the authority.

## Completion

- **Macros and environments** the document defines (`\newcommand`, `\renewcommand`, `\DeclareMathOperator`, `\def`, `\NewDocumentCommand`, `\newenvironment`, `\NewDocumentEnvironment`) in any `.tex`, `.sty` or `.cls` of its folder.
- **`\cite{` and `\ref{`** match any part of the key, author or title; every word you type must appear.
- **File paths:** inside `\includegraphics{`, `\input{`, `\include{`, `\includeonly{`, `\subfile{`, `\bibliography{` and `\addbibresource{`, the document's matching files (images; `.tex` without the extension; `.bib`).
- **Glossaries:** `\gls{`, `\Gls{`, `\glspl{`, `\glstext{` and the rest of the `\gls...` family, and `\acrshort{`, `\acrlong{`, `\acrfull{` (and capitalised forms) complete the keys of `\newglossaryentry`, `\longnewglossaryentry`, `\newacronym` and `\newabbreviation` in any `.tex`, `.sty` or `.cls` of the folder, matched by key or text.

## Bold, emphasis and wrapping

- Ctrl+B (Cmd+B) toggles `\textbf{...}` and Ctrl+I toggles `\emph{...}` around the selection, or removes it when the cursor is inside one. Outside the editor Ctrl+B still toggles the file list.
- Ctrl+K > "Wrap selection in environment..." or "...in command..." wraps the selection in `\begin{name}`/`\end{name}` or `\name{...}`.

## Structural edits

All in the command palette (Ctrl+K):

| Command | Effect |
|---|---|
| Change surrounding environment... | Renames both `\begin` and `\end` of the innermost environment around the cursor |
| Delete surrounding environment (keep content) | Removes `\begin{x}[...]` and `\end{x}` (and lines left blank) |
| Delete surrounding command (keep content) | Turns `\footnote{text}` into `text` |
| Toggle starred environment | Switches `figure` and `figure*` |
| Toggle \left / \right | Removes them from the pair around the cursor, or adds them to the innermost `( )`, `[ ]` or `\{ \}` pair |

## Math shortcuts

Turn them on in Settings > Editing (off by default). In math mode, `@` and a key types a symbol, after LaTeX Workshop's table: `@a` gives `\alpha`, `@8` gives `\infty`. The full table is in [Keyboard shortcuts](../reference/shortcuts.md).

Math mode is `$...$`, `$$...$$`, `\(...\)`, `\[...\]` and the display environments, but not `\text{...}` inside them.

## Snippets

1. Type a trigger word and press Tab (or Ctrl+Space for the list).
2. Tab and Shift+Tab move between the tab stops; Esc leaves them.

The defaults are `fig` (figure), `eq` (equation), `itm` (itemize) and `sec` (section with label). Edit them in Settings > Editing > "Edit snippets..." (or Ctrl+K): a trigger (a word) and a body where `${1:default}` or `${1}` marks a tab stop. `${` always starts one, and a default cannot hold `}`. Snippets are kept in this browser.

## Convert Unicode on paste

When pasted text has characters LaTeX spells differently, a message offers **Convert to LaTeX**:

- curly quotes become ``` `` ``` and `''`, dashes `--` and `---`, the ellipsis `\dots{}`, a no-break space `~`;
- for pdflatex documents (not xelatex or lualatex, which read UTF-8), also accented letters (`é` to `\'e`, `ç` to `\c{c}`) and `ß`, `æ`, `ø` and friends;
- pasted into math, Greek letters and symbols (`α` to `\alpha`, `≤` to `\leq`, `ℝ` to `\mathbb{R}`).

Settings > Editing turns the offer off.

## Lines, wrapping and zen mode

- Settings > Editing (or Ctrl+K) switches soft wrap and relative line numbers.
- Alt+Q ("Rewrap paragraph") refills the paragraph around the cursor to the width set there (80 columns by default), keeping its first line's indent. It leaves paragraphs with comments, environments, `\item`, headings or `\verb` alone.
- Zen mode (More > Document or Ctrl+K > "Zen mode") hides everything but the editor, in a centred column. Esc (when nothing in the editor uses it) or the "Exit zen mode" button leaves it.

## Use .editorconfig

A `.editorconfig` in the document folder sets the indent (`indent_style`, `indent_size`, `tab_width`) per file pattern (`*`, `**`, `?`, `[...]`, `{a,b}`). On an explicit save (Ctrl+S) it also applies `trim_trailing_whitespace` and `insert_final_newline`; lines with a cursor keep their trailing spaces, and autosave changes nothing. Files outside the folder are not read.

## How edits are applied

Every tool changes the text the way typing does. With the view role it does nothing; in suggest mode (always on for reviewers) the change becomes a suggestion; co-editing and undo see one edit.

## Next

- [Tables, pictures and formatting](tools.md)
- [Keyboard shortcuts](../reference/shortcuts.md)
- [Live preview](live-preview.md)
