# Building documents

How to build, rebuild and watch documents with `scripts/build.py`, and what a build leaves in `out/`; for everyone who builds from the command line.

Run every command from the repository root. `python scripts/build.py --help` lists every flag; the [command-line reference](../reference/cli.md) has them in one table.

## Build

```bash
python scripts/build.py                  # build documents whose PDF is missing or out of date
python scripts/build.py --force          # rebuild everything (latexmk -g, even when its cache looks current)
python scripts/build.py -j 2             # build two documents at a time (default: CPU count, up to 8)
python scripts/build.py --list           # show discovered documents and whether they are up to date
python scripts/build.py --clean          # delete out/ and .latex-cache/
```

## Pick which documents to build

```bash
python scripts/build.py "FP-123 Report"               # one document by name
python scripts/build.py "reports/*"                   # a glob
python scripts/build.py --changed-since origin/main   # only documents changed since a git ref
python scripts/build.py --source bench sample-report  # build from bench/ instead of files/ (not built by CI)
python scripts/build.py my-report --focus Chapters/chapter5   # preview one part in about 2 s
```

`--focus` is explained in [Large documents](large-documents.md).

## Create a document

```bash
python scripts/build.py --new MyDoc                      # files/MyDoc/main.tex
python scripts/build.py --new Thesis --template report   # also: beamer, letter (default: article)
```

## Watch and rebuild on save

```bash
python scripts/build.py --watch --open   # rebuild on every save, open each PDF after its first build
python scripts/build.py --watch --notify --exec "cp out/my-paper.pdf ~/Dropbox/"
```

- `--watch` waits for half a second without changes before rebuilding, so one save builds once.
- A document that fails is retried only after one of its files changes again.
- `--notify` shows a desktop notification after each build: osascript on macOS, `notify-send` on Linux, a PowerShell tray balloon on Windows. Nothing is shown if none of these is there.
- `--exec "command"` runs the command after each successful build, in the repository root. It is split like a shell would split it, but runs without a shell. The environment holds `LP_DOC` (the document name) and `LP_PDF` (its PDF path).

Only the command line runs `--exec`; the editor and shared links never do.

## Check a build more strictly

| Flag | Effect |
| --- | --- |
| `--strict` | Fails a document whose last LaTeX run warned about an undefined or multiply defined reference, label or citation, or had an overfull box wider than 1 pt. Same as `strict = true` in `build.toml`. |
| `--quiet-log` | Prints only the LaTeX lines that need action, using shortened [texfot](https://ctan.org/pkg/texfot) rules: errors and the line after them, warnings, overfull and underfull boxes, missing characters and the "Output written" line. |
| `--profile` | Also prints the figures and LaTeX time of each document, and each latexmk rule run. |

With `--strict` the PDF of a failing document is not copied to `out/`, like any failed build. A current document is not rebuilt, so add `--force` to check one. With `--quiet-log`, `out/<name>.log` still keeps everything; the editor's Log tab has the same filter as a checkbox.

Other per-document tasks (formatting, tidying `.bib` files, preflight, page extracts, dependency lists) are in [Build tools](build-tools.md).

## When a document is out of date

A document is rebuilt when any of these is newer than its PDF:

- any file in its directory (dotfiles such as `.DS_Store` are ignored);
- any file outside it that its last build read, so a shared `.bib` counts;
- `build.py` itself.

Before the first build, only the document's directory counts.

## What a build does

For each document, the script:

1. Runs `latexmk -pdf` in `.latex-cache/` (git-ignored), with one directory per document. The `.aux`, `.bbl` and other files stay there, so a text edit needs one LaTeX pass instead of a full cold build. If a build fails with cached files, the cache is wiped and the build retried once from scratch. CI keeps the same cache with `actions/cache`.
2. Copies only the final PDF into `out/`.
3. Writes the full build log to `out/<name>.log`, whether the build succeeded or not.
4. Carries on with the remaining documents if one fails (an unexpected exception is reported as `Build crashed: ...`), and prints a summary.
5. Exits non-zero if any document failed.

Before building, it deletes any PDF or log in `out/` whose `main.tex` no longer exists; `--watch` does this too. If `latexmk` is not on `PATH`, the script stops before building and prints installation links.

## Read errors and logs

Errors are printed in the summary as `files/<doc>/<file>:<line>: message`, which most editors can open directly. A plain-language hint follows each error when one is known.

`out/<name>.log` (for example `out/FP-123 Proposal.log`) starts with the result and any errors. Then comes the latexmk console output from every pass, LaTeX's own `.log` and BibTeX's `.blg`, so every info line, warning and error is in it.

## Build report

After a build (not `--watch`) the script writes `out/build-report.json` with every document it built in that run. CI reads this file, so its keys stay stable.

| Key | Meaning |
| --- | --- |
| `ok` | whether the build succeeded |
| `seconds` | build time |
| `engine` | the TeX engine used |
| `pages` | page count, `null` if unknown or failed |
| `errors` | list of `file`, `line`, `message`, plus `hint` when one matched |
| `warnings` | count of LaTeX warnings |
| `error` | message of a crashed build, otherwise `null` |
| `phases` | seconds spent on `figures` and `latex`, `null` for a crash |
| `steps` | optional: one `{"rule", "seconds"}` per latexmk rule run (`pdflatex`, `bibtex main`, ...) |
| `outputs` | optional: the `[[output]]` PDFs (see [build.toml keys](../reference/build-toml.md)) |
| `a11y` | only with `tagged = true`: accessibility findings (`kind`, `file`, `line`, `message`, `hint`; see [PDF/A and tagged PDF](pdfa-tagged.md)) |

## Terminal output

- Output is coloured on a terminal. Set `NO_COLOR=1` to turn colour off.
- With several jobs, each document's output appears as one block when that document finishes.
- Ctrl+C in a parallel build cancels the documents still in the queue and exits with status 130.

## Next

- [Build tools](build-tools.md)
- [Documents and settings](documents.md)
- [Command-line reference](../reference/cli.md)
