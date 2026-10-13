# Build tools

Extra `build.py` tasks for one or more documents: formatting source, tidying `.bib` files, camera-ready checks, page extracts and build metadata; for authors preparing a submission.

None of the files these tasks write to `out/` is published to the release.

## Format the source

```bash
python scripts/build.py --format my-report   # rewrite its .tex files with tex-fmt (no build)
```

`--format` runs [tex-fmt](https://github.com/WGUNDERWOOD/tex-fmt) on every `.tex` file of each selected document, in the document's directory. A `tex-fmt.toml` there sets the options (wrap length, tab size, extra list or verbatim environments; see tex-fmt's README). It prints the files it changed.

- Install tex-fmt with `cargo install tex-fmt`, `brew install tex-fmt` or a binary from its releases page.
- Without tex-fmt on `PATH` it prints a message and exits 0.
- tex-fmt only rewrites text and never runs LaTeX, so it is not sandboxed.

## Tidy bibliography files

```bash
python scripts/build.py --tidy-bib my-paper   # sort and align its .bib files, report duplicates (no build)
```

`--tidy-bib` sorts the entries of every `.bib` file of each selected document and puts the fields in [bibtex-tidy](https://github.com/FlamingTempura/bibtex-tidy)'s default order (title, author, year, journal, booktitle, ...; unknown fields after them in their own order) with their `=` aligned. It lists possible duplicates (the same key, DOI or title) without removing any.

| Option | Effect |
| --- | --- |
| `--bib-sort key` | sort by key in natural order (default) |
| `--bib-sort author` | sort by first author and year |
| `--bib-sort year` | sort by year |
| `--bib-sort none` | keep the order |
| `--check` | change nothing; exit 1 when a file would change (for pre-commit; also works with `--format`) |

Values are copied byte for byte and a comment above an entry moves with it. `@string`, `@preamble`, `@comment` and anything it cannot parse go to the top in their original order. If the result would still differ in content, the file is left alone with a message.

## Write a cited-only bibliography

```bash
python scripts/build.py my-paper --cited-bib   # also write out/my-paper.cited.bib
```

After the build, `out/<name>.cited.bib` holds only the entries the document cites, read from the build's `.aux` files (BibTeX, `\citation`) or its `.bcf` (biblatex). It keeps the `@string` and `@preamble` definitions of its databases and the `crossref` / `xdata` parents of cited entries; `\nocite{*}` keeps every entry.

- Only `.bib` files inside the document directory are read.
- Cited keys found in no database are printed as a warning.
- It runs no TeX of its own.

## Check a camera-ready PDF

```bash
python scripts/build.py my-paper --preflight   # fonts, Type 3, page size, PDF version
```

`--preflight` runs `pdffonts` and `pdfinfo` (poppler-utils) on each PDF after the build:

- fonts that are not embedded are an error (exit 1; usually a figure PDF from another program);
- Type 3 (bitmap) fonts and pages of different sizes are warnings;
- the PDF version, page size and bookmarks are listed for you to compare with what the venue asks.

Without poppler-utils it says so. For arXiv uploads, use `--arxiv`; see [Export](export.md).

## Extract pages

```bash
python scripts/build.py my-paper --pages 1-3,7   # also write out/my-paper.pages.pdf
```

This copies those pages of one document's PDF to `out/<name>.pages.pdf` with qpdf (or pdftk). Without either it prints where to get qpdf and exits 1.

## List inputs and metadata

```bash
python scripts/build.py --deps make my-paper   # the files its last build read, as a Make rule (or json)
python scripts/build.py --query my-paper       # title, author, pages, words, labels, sections as JSON
```

Neither builds; both need one earlier build.

`--deps make` prints a Make rule `out/<name>.pdf: <inputs>` with every file the last build read: the document's own files and files outside it, including the `.bib` files BibTeX or biber read, but not the TeX installation or the cache. `--deps json` prints the same as `{"target", "inputs"}`.

`--query` prints JSON per document:

| Key | Source |
| --- | --- |
| `title`, `author` | `main.tex`, commands dropped |
| `pages` | the last build's log |
| `words` | texcount, `null` without it |
| `labels` | `key`, `number`, `page` from the `.aux` files |
| `sections` | `level`, `number`, `title`, `page` from the `.toc` |

## Next

- [Building documents](building.md)
- [Export](export.md)
- [Command-line reference](../reference/cli.md)
