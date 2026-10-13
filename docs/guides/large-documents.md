# Large documents

How the build speeds up long documents with TikZ figure caching, `--focus` previews and draft mode, for authors of theses and reports; nothing here needs a change to the document.

## TikZ figures are compiled once and cached

`build.py` switches on TikZ's `external` library (through `latexmk -usepretex`) when the document uses TikZ. That means any of these, in `main.tex`, a class, a package or a chapter (comments and words in the text do not count):

- loads `tikz`, `pgf` or `pgfplots`, a package named after them (such as `tikz-cd`), or one built on TikZ (`forest`, `pgfgantt`, `chemfig`, `smartdiagram`, `tkz-*`);
- uses `\documentclass[tikz]{standalone}`, a TikZ library, `\tikz` or a `tikzpicture`.

What happens:

1. A first run lists the figures.
2. The figures are compiled in parallel, about one process per CPU, shared between the documents built at once. No `make` and no shell escape are needed.
3. The text passes include the finished PDFs.

Figures are cached in `.latex-cache/_figcache/` by the hash of their source. An edit re-typesets only the text, and renumbering or moving a figure costs nothing. Figures are listed again only when the figure text of a file changed (its `tikzpicture` environments and lines that mention tikz, pgf or axis), so a text edit next to a figure costs one LaTeX pass.

A figure job skips the `\input` files before and after its figure that define nothing (no `\newcommand`, `\def`, `\setlength`, `\tikzset`, ... outside their pictures). It sets the counters to what the last full run recorded after them. It does so only when no file of the document changed since that run started; a figure that fails this way is compiled again reading every file.

The whole cache is invalidated when any of these change: the preamble, a file the preamble `\input`s, a local `.cls`/`.sty`/`.csv`/`.dat`/`.tikz` file, the TeX engine, or the TikZ/pgfplots packages. Figures nobody uses any more are deleted.

- Works with `pdflatex`, `xelatex` and `lualatex`.
- Documents without TikZ are built as before.
- Documents with `remember picture`, `overlay` or `tikzmark` pictures are built as before, since those cannot be cut out as figures.
- If the figures cannot be made, the document is built again without externalization and its log says so. A LaTeX error in the text does not trigger that second build.

### Limits

If these bite, set `externalize = false` in `build.toml`, or pass `--force` to recompile every figure.

- A figure is recompiled when its source text changes.
- Every figure is recompiled when a chapter changes a `\tikzset`, `\pgfplotsset`, `\tikzstyle`, `\pgfkeys` or TikZ library line, the definition of a macro or colour that a picture uses (`\newcommand`, `\def`, `\definecolor`, ...), or a file a picture `\input`s.
- Not noticed: a changed `\ref` value inside a figure, and macros a picture reaches only through another macro.
- `\addplot table` files are noticed only with a `.csv`, `.dat`, `.tsv` or `.table` extension.
- Several pictures with identical source (a `\foreach` around a `tikzpicture`) are told apart by their order among the equal ones. Inserting an identical picture before them can redraw one.

## Preview one part with `--focus`

```bash
python scripts/build.py my-report --focus Chapters/chapter5
```

This typesets only the files under that path (a file or a directory, relative to the document) into `out/my-report.focus.pdf` with one LaTeX run: about 2 s for a 300 page report.

- The first full build records the `\input`/`\include` tree and all counters.
- The preview reads the full build's `.aux`, `.bbl` and `.toc` files, so chapter, figure and page numbers, references and citations match the full document.
- Every other `\input` does nothing (`\include` uses `\includeonly`), and pages outside the focused part are discarded.
- With `--watch`, `--focus auto` previews the top-level file that holds the file you saved last. It builds the whole document when that file is `main.tex` or anything but a `.tex` file (a class, a bibliography, a figure).

Limits:

- Everything outside the focus shows the state of the last *full* build. New labels or citations show as `??` until the next full build.
- Macros defined in a skipped file are missing. The title page, table of contents and lists are skipped. Material typeset by `main.tex` itself between chapters is not in the preview.
- `out/<name>.focus.pdf` and `.focus.log` are local previews and are never part of CI.

## Draft mode

Draft mode is an opt-in, not a default. `draft = true` in `build.toml` (or `--draft`) passes `draft` to graphicx, which draws a box of the right size instead of each image, so the layout and page numbers stay. It changes the PDF, so a document builds once more when it is switched on or off.

Measured on `bench/sample-report` (11 small PNG and PDF images) on a shared 4-CPU machine, two runs each:

| Step | without | with draft |
| --- | --- | --- |
| `--force` | 129 s, 138 s | 119 s, 103 s |
| one-line text edit | 22 s, 27 s | 28 s, 23 s |

Text edits gain nothing, and the cold-build difference is within this machine's noise. It may help a document with many large images; measure yours before relying on it.

## How much faster

On `bench/sample-report` (303 pages, 25 TikZ figures, 4 CPUs):

| Step | before | after |
| --- | --- | --- |
| cold build (`--force`) | 93 s | 55 s |
| one-line text edit | 47 s | 13 s |
| one-line text edit in a file that holds a figure | n/a | 13 s |
| edit one figure | 24 s | 18 s |
| `--focus Chapters/chapter5` | n/a | 1.7 s |
| live preview of `Chapters/chapter5` in the editor | n/a | 0.7 s |

A no-op rebuild takes 0.12 s, the same as plain latexmk. Plain latexmk needs 27 s for the figure edit. How this compares with plain latexmk, an Overleaf-style build, a naive pdflatex/bibtex script, arara and Tectonic (including where plain latexmk wins) is in [Benchmarks](../explanation/benchmarks.md).

Not adopted, because they did not pay off:

- a precompiled preamble format (saves about 1.2 s of a 12 s pass, changed the page count out of the box and hides the class from `-recorder`);
- `-draftmode` or `\pdfcompresslevel=0` for intermediate passes, and dropping SyncTeX (no gain above the 1 s noise);
- parallel BibTeX (the ten units take 0.2 s together);
- splicing separately built chapters into the full PDF (needs a PDF library; `pdfunite` drops the outline and breaks cross-chapter links).

## Build the benchmark documents

`bench/` holds local benchmark documents, such as `bench/sample-report`. CI never builds them.

```bash
python scripts/build.py --source bench sample-report
```

`--source DIR` selects the documents to build instead of `files/`. `DIR` is relative to the repository root or an absolute path, also outside the repository.

- The build cache of a directory outside the repository is `.latex-cache/_x<hash of its path>...` (the path casefolded on a case-insensitive file system), so two such directories with the same document names never share it.
- `--changed-since` is ignored for a directory outside the repository (with a note), since it is not in the repository's history.
- Output goes to `out/` as usual, named after the document: `out/sample-report.pdf`. A document in `bench/` and one in `files/` with the same name share that output file, so give them different names.
- Building one tree never deletes the other tree's outputs.

## Next

- [Building documents](building.md)
- [build.toml reference](../reference/build-toml.md)
- [Live preview](../editor/live-preview.md)
