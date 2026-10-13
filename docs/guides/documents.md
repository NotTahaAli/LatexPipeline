# Documents

How to add a document to the repository, start one from a template, and what makes its PDF reproducible; for anyone writing in this repository.

## Add a document

1. Create a directory under `files/` with a `main.tex` in it.
2. Put everything the document needs beside `main.tex`: `.bib` files, a custom `.cls`, figures.
3. Build it. You don't need to change `build.py`.

```text
files/My New Document/
├── main.tex
├── references.bib
├── custom.cls
└── Figures/
    └── figure.png
```

The PDF is `out/<name>.pdf`. Settings for one document go in a `build.toml` next to `main.tex`, see [build.toml keys](../reference/build-toml.md).

## Start from a template

```bash
python scripts/build.py --new MyDoc --template report
```

| Template | What you get |
| --- | --- |
| `article` (default) | A single `main.tex`. |
| `report` | `main.tex` plus `chapters/`, `refs.bib`, `figures/` and a commented `build.toml`. |
| `beamer` | A single file. |
| `letter` | A single file. |

## Files outside the document directory

Keep inputs inside the document's directory where you can. A file outside it (for example `\input{../shared/x}`) is tracked for rebuilds only once the document's first build has read it.

The build records every file it reads in `.latex-cache/<name>/<name>.fls` (latexmk `-recorder`). The `.bib` files BibTeX or biber read are recorded in `<name>.fdb_latexmk` beside it. TeX installation files and the cache are never tracked.

## Choose the engine

The engine comes from a magic comment in the first 20 lines of `main.tex`:

```latex
% !TEX program = xelatex
```

`pdflatex` is the default. The `engine` key in `build.toml` overrides the comment.

## Reproducible PDFs

latexmk runs with `SOURCE_DATE_EPOCH` set to the time of the last commit that touched the document's directory, and with `FORCE_SOURCE_DATE=1`. When git has no history for the directory, its newest input's time is used instead.

Building the same commit twice gives byte-identical PDFs. Side effect: `\today` shows that commit's date, not the build date.

## Next

- [build.toml reference](../reference/build-toml.md)
- [Building](building.md)
- [Export](export.md)
