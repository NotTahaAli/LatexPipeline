# Export

How to export a document as DOCX, HTML, an arXiv upload or a cited-only `.bib`, from the command line or the editor.

None of these files is published to the GitHub release, except the HTML zip of a document with `html = true`.

## Export to DOCX

```bash
python scripts/build.py --docx [DOC ...]
```

1. The selected documents are built.
2. `pandoc main.tex` runs in each document's directory and writes `out/<name>.docx`.
3. Every `.bib` beside `main.tex` becomes a `--citeproc` bibliography.

- It exports even when the PDF build failed.
- It needs `pandoc` on `PATH`; otherwise it stops with a message.
- `--docx` cannot be combined with `--watch` or `--focus`.

**In the editor:** the owner gets More > "Export DOCX" when pandoc is installed. The export and its download are owner-only and disabled while sharing, because pandoc reads any file the source names (`\input{../x}`, `\lstinputlisting{/abs/path}`) into the `.docx`.

## Export to HTML

```bash
python scripts/build.py --html [DOC ...]
```

The selected documents are built, then each is converted with [make4ht](https://ctan.org/pkg/make4ht) into `out/<name>.html/index.html` with its stylesheet and images. The same folder is also written as `out/<name>.html.zip`.

The folder is `<name>.html/`, not `<name>/`, because `out/<name>/` holds the PDFs of documents nested below it.

### What you need

| Tool | For | TeX Live | Debian/Ubuntu package |
| --- | --- | --- | --- |
| make4ht (TeX4ht) | the conversion | `collection-binextra`, `collection-plaingeneric` | `texlive-extra-utils`, `texlive-plain-generic` |
| `pdftocairo` | PDF figures to SVG | - | `poppler-utils` |
| `dvisvgm` | TikZ pictures to SVG | `collection-binextra` | `dvisvgm` |

When `pdftocairo` or `dvisvgm` is missing, the export still succeeds and its message names the images that were not converted.

### What the output contains

- Math is MathML (`mathml` option), so the page needs no MathJax and loads nothing from a CDN.
- `alt=` texts of `\includegraphics` carry over.
- Citations come from the last build's `.bbl`, so build first (`--html` does).
- TeX4ht handles common classes and packages. Custom classes and heavy layout packages may come out plainer than the PDF.

TeX4ht runs LaTeX, so make4ht runs through the same sandbox runner as every build (`--sandbox` confines it). It works on a copy of the document in `.latex-cache/`; the document's own folder gets no auxiliary files.

`--html` cannot be combined with `--watch`, `--focus`, `--review-pdf` or `--format`.

### Publish HTML with every build

Set `html = true` in the document's `build.toml`. Every build of it, CI's too, then also exports the HTML, and `<name>.html.zip` is uploaded to the release next to the PDF (named like the PDF, `_XX`-escaped). CI then needs `dvisvgm` and `poppler-utils` in its apt step for figures.

**In the editor:** the owner gets More > "Export HTML" when make4ht is installed; it downloads the zip. Owner-only (the download too) and off while sharing, like DOCX. The editor never serves the pages themselves, since they are the document's own HTML.

## Export for arXiv

```bash
python scripts/build.py --arxiv [DOC ...]
```

This builds the selected documents (also when only the cache is missing), then writes `out/<name>.arxiv.zip`. arXiv takes `.zip` and `.tar.gz` alike.

### What goes in the archive

- Exactly the files the last build read from the document directory (from its `.fls`). Nothing outside it, no unused figures, no `build.toml`.
- The generated `main.bbl`, plus `.ind`, `.gls` and `.nls`, which arXiv does not make itself.
- With `engine = "xelatex"`, a `00README.json` selecting XeLaTeX ([format](https://info.arxiv.org/help/00README.html)). arXiv has no LuaLaTeX.

`%` comments are stripped from `.tex` files:

- A comment line goes. A comment after text leaves a bare `%`, so the end of line still makes no space.
- Kept as they are: `\%`, verbatim-like environments (`verbatim`, `lstlisting`, `minted`, `comment`, ...), `\verb`, `\lstinline`, `\url`, `\href` and `\path`.
- A file that changes `\catcode` is copied unchanged. `\iffalse ... \fi` blocks are left in.

### Preflight checks

Each problem is printed with a fix hint and written to the end of `out/<name>.log` under `===== arXiv export =====`. It checks for:

- files the build read outside the document directory
- file names with spaces, non-ASCII or special characters
- hidden files (arXiv deletes them at announcement)
- absolute paths or `..` in `\input`, `\includegraphics` and similar
- shell escape (`shell_escape`, `\write18`, minted, pythontex, svg)
- figures not in the archive, and EPS figures converted during the build
- PNG or JPEG images over 34 megapixels (arXiv states no byte limit, it warns above that)
- a biblatex `.bbl` format other than 3.3 (TeX Live 2025, arXiv's default; 3.2 works only if you select TeX Live 2023 at submission)
- LuaLaTeX
- `pdfa` / `tagged`: these come from `build.toml` and need a `\DocumentMetadata` line in `main.tex` on arXiv

The command exits non-zero when an export failed or a preflight check is an error. Documents whose build failed are not exported. It runs no LaTeX (it reads the cache) and is never run in CI.

**In the editor:** the owner gets More > "Export for arXiv", which exports from the last build and downloads the archive. Owner-only and disabled while sharing, like DOCX export.

## Download a cited-only .bib

```bash
python scripts/build.py my-paper --cited-bib
```

After the build this writes `out/<name>.cited.bib` with only the entries the document cites:

- Cited keys are read from the build's `.aux` files (BibTeX, `\citation`) or its `.bcf` (biblatex).
- The `@string` and `@preamble` definitions of its databases and the `crossref` / `xdata` parents of cited entries are kept.
- `\nocite{*}` keeps every entry.
- Only `.bib` files inside the document directory are read.
- Cited keys found in no database are printed as a warning.

It runs no TeX of its own.

**In the editor:** References > More > "Download cited-only .bib" downloads the entries the last successful build cited. Any role that can read the document can use it. Build first; it says which cited keys are in no `.bib` file.

## Next

- [build.toml reference](../reference/build-toml.md)
- [References](references.md)
- [PDF/A and tagged PDF](pdfa-tagged.md)
