# Just compiling LaTeX

The shortest path for people who only want their LaTeX documents built locally and published as PDFs by GitHub Actions.

You only need `files/` and `scripts/build.py`. Everything else (editor, MCP, hosting, tests, `bench/`) is optional and stays out of your way.

## Set up

1. Click **Use this template** on GitHub, or clone the repository.
2. Put each document in its own folder: `files/<name>/main.tex`, with its `.bib`, `.cls` and figures beside it.
3. Build locally with `python scripts/build.py`, or push to `main`.

To start a document from a template instead of writing `main.tex` yourself:

```bash
python scripts/build.py --new MyDoc      # creates files/MyDoc/main.tex
```

## How documents map to PDFs

Each directory under `files/` that contains a `main.tex` is a standalone document, built to `out/<path>.pdf`:

```text
files/abc/main.tex            ->  out/abc.pdf
files/reports/final/main.tex  ->  out/reports/final.pdf
```

Every build also writes `out/<name>.log` with the result and the full LaTeX and BibTeX logs.

## Build locally

```bash
python scripts/build.py                    # build documents that are missing or out of date
python scripts/build.py --watch --open     # rebuild on every save, open each PDF
```

## Publish with CI

When you push to `main`, CI builds every changed document and publishes the PDFs to your own GitHub release tagged `pdfs`. That release always holds the newest PDF of every document, with the log of its latest build (`.log`) next to it.

In a copy made from the template, CI runs only the build. The unit tests, ruff and the PDF/A fixture check run only in the original repository.

## Next

- [Install](install.md)
- [Building documents](../guides/building.md)
- [GitHub Actions](../guides/ci.md)
