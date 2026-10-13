# Command-line reference

Every `build.py` flag used in these docs, and the main `serve.py` options, in one place; for readers who know what they want and need the exact flag.

`python scripts/build.py --help` and `python scripts/serve.py --help` list every flag, including ones not covered here. With uv, `uv run scripts/build.py ...` works too.

## build.py

```bash
python scripts/build.py [flags] [DOC ...]
```

`DOC` is a document name relative to `files/` (for example `"FP-123 Report"` or `reports/final`) or a glob such as `"reports/*"`. Without it, every document is considered.

### Building

| Flag | What it does |
| --- | --- |
| (none) | Build documents whose PDF is missing or out of date. |
| `--force` | Rebuild everything (`latexmk -g`, even when its cache looks current). |
| `-j N` | Build N documents at a time (default: CPU count, up to 8). |
| `--list` | Show discovered documents and whether they are up to date. |
| `--clean` | Delete `out/` and `.latex-cache/`. |
| `--changed-since REF` | Only documents changed since a git ref, for example `origin/main`. |
| `--source DIR` | Build from `DIR` (for example `bench`) instead of `files/`. CI does not build `bench/`. |
| `--focus PATH` | Preview one part (for example `Chapters/chapter5`) in about 2 s. See [Large documents](../guides/large-documents.md). |
| `--new NAME` | Create `files/NAME/main.tex` from a template. |
| `--template NAME` | Template for `--new`: `article` (default), `report`, `beamer`, `letter`. |
| `--profile` | Also print the figures and LaTeX time of each document. |

### Watching

| Flag | What it does |
| --- | --- |
| `--watch` | Keep running and rebuild on every save. |
| `--open` | With `--watch`: open each PDF after its first build. |
| `--notify` | With `--watch`: show a desktop notification after each build. |
| `--exec "COMMAND"` | With `--watch`: run the command after each successful build, without a shell, with `LP_DOC` and `LP_PDF` set. |

### Checks and output

| Flag | What it does |
| --- | --- |
| `--strict` | Also fail on undefined references or citations and overfull boxes over 1 pt. |
| `--quiet-log` | Print only the LaTeX lines that need action (the log keeps everything). |
| `--preflight` | Camera-ready checks of the PDF: fonts, Type 3, page size, PDF version. |
| `--pages RANGES` | Also write `out/<name>.pages.pdf` with those pages, for example `1-3,7` (qpdf or pdftk). |
| `--arxiv` | Also write `out/<name>.arxiv.zip` and check it for arXiv. See [Export](../guides/export.md). |
| `--cited-bib` | Also write `out/<name>.cited.bib` with only the entries the build cited. |

### Source and bibliography tools (no build)

| Flag | What it does |
| --- | --- |
| `--format` | Rewrite the document's `.tex` files with tex-fmt. |
| `--tidy-bib` | Sort and align its `.bib` files, report duplicates. |
| `--bib-sort ORDER` | Sort order for `--tidy-bib`: `key` (default), `author`, `year`, `none`. |
| `--check` | With `--format` or `--tidy-bib`: change nothing, exit 1 when a file would change. |
| `--deps make` / `--deps json` | Print the files the last build read, as a Make rule or JSON. |
| `--query` | Print title, author, pages, words, labels and sections as JSON. |

Details and examples: [Building documents](../guides/building.md) and [Build tools](../guides/build-tools.md).

### Environment

| Variable | Effect |
| --- | --- |
| `NO_COLOR=1` | Turn coloured output off. |

## serve.py

```bash
python scripts/serve.py [DOC ...] [--port 8000] [--no-open] [--source DIR] [--editor vscode]
```

Starts the editor and live preview at http://localhost:8000, bound to 127.0.0.1 only. See [The editor](../getting-started/editor.md).

| Option | What it does |
| --- | --- |
| `DOC ...` | Documents to serve (name or glob). Default: all. |
| `--port PORT` | Port to listen on (default 8000). |
| `--no-open` | Do not open a browser. |
| `--source DIR` | Directory holding the documents (default: `files/`). |
| `--editor vscode` | URL scheme for source links (`vscode`, `cursor`, ...). |

## Next

- [Building documents](../guides/building.md)
- [build.toml keys](build-toml.md)
- [Compile API](compile-api.md)
