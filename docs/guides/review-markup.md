# Review markup

How to write proposed changes into the LaTeX source, show them in the PDF, and build a PDF with the editor's suggestions inlined; for authors and reviewers.

## Turn the markup on

Set `review` in the document's `build.toml`:

```toml
review = "show"
```

Or pass `--review` on the command line. It overrides `build.toml` for the selected documents and rebuilds them:

```bash
python scripts/build.py --review show
```

| Value | Effect |
| --- | --- |
| `"off"` (default) | Injects nothing, so a document without the key builds exactly as before. A document that uses the macros then fails with `Undefined control sequence`. |
| `"show"` | Marks changes in colour. Loads `xcolor` and `ulem`. |
| `"clean"` | "All accepted": prints the document as if every change were accepted. Loads nothing. |

Switch between `show` and `clean` to get a marked-up copy for reviewers and the final text from the same source.

## Markup

| Markup | `show` | `clean` |
| --- | --- | --- |
| `\lpadd{new text}` | green background | the text, plain |
| `\lpdel{old text}` | red, struck through | nothing |
| `\lpedit{old}{new}` | both of the above | `new` |
| `\lpnote{text}` | `[text]` in small blue sans serif | nothing |
| `\begin{lpaddblock}...\end{lpaddblock}` | green text (whole paragraphs, lists) | the content, plain |
| `\begin{lpdelblock}...\end{lpdelblock}` | red text | nothing |
| `\begin{lpnoteblock}...\end{lpnoteblock}` | small blue sans serif | nothing |

## How the definitions apply

- They are injected with `latexmk -usepretex` and take effect at `\begin{document}`.
- They work in all three engines, in chapter previews and in the live preview.
- If the document or a package already defines one of these names, its own definition wins. Define them in the preamble to restyle them.
- `show` loads `ulem` with `normalem`, so `\emph` stays italic. A document that loads `ulem` itself keeps its options. Both packages are in the CI package lists.

## Limits

- The inline macros cannot span a paragraph break. Use the block environments.
- Like any `ulem` mark, the inline macros can break a moving argument or a tagged PDF.
- In `clean`, the block environments `lpdelblock` and `lpnoteblock` need LaTeX 2020-10 or newer.

## Build a PDF with the editor's suggestions

```bash
python scripts/build.py --review-pdf DOC
python scripts/build.py --review-pdf DOC --no-comments
```

In the editor: More > Build with suggestions.

This writes `out/<name>.review.pdf` and `.review.log`:

- Open suggestions are inlined as `\lpdel{old}\lpadd{new}`.
- Unresolved comments are inlined as `\lpnote{Name: text}`. `--no-comments` leaves them out.

The document is copied to `.latex-cache/_review/`, the copy is edited and built like any document with `review = "show"`. The document's own files are never written, and the review PDF is never published to the release.

### What gets inlined

A change is inlined only where the anchored text is still found (as the editor finds it) and is plain prose:

- in the body of `main.tex`, or of a `.tex` file without `\documentclass` or `\usepackage`
- outside math, verbatim, comments and optional arguments
- outside the arguments of commands, except `\emph`, `\textbf`, `\textit`, `\textsl`, `\textsc`, `\textup`, `\textmd`, `\textrm`, `\textsf` and `\footnote`
- with old and new text free of TeX specials and blank lines (`\%`, `\&`, `\$`, `\#`, `\_` are fine)

Everything else is skipped and listed at the top of the log and in the editor's message. So are suggestions that overlap one taken already. A suggestion made with several cursors goes in whole or not at all.

Comment text is escaped, and reduced to ASCII under pdfLaTeX. Inputs outside the document's directory (`\input{../shared/x}`) are not copied, so a document that needs them builds no review PDF.

## Next

- [build.toml reference](../reference/build-toml.md)
- [Collaboration in the editor](../editor/collaboration.md)
- [Documents](documents.md)
