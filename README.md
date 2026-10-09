# Latex Document Pipeline

LaTeX document pipeline, including a cross-platform build script and github releases document upload.

**Latest PDFs:** [Releases, `pdfs`](https://github.com/NotTahaAli/LatexPipeline/releases/tag/pdfs). This release always holds the newest PDF of every document, with the full log of its latest build (`.log`) next to it.

Every directory under `files/` that contains a `main.tex` is a standalone document. It compiles to a PDF under `out/`, mirroring its path:

```text
files/abc/main.tex   ->  out/abc.pdf
files/reports/final/main.tex     ->  out/reports/final.pdf
```

Everything inside a document's directory (`.tex`, `.bib`, `.cls`, figures, ...) counts as that document's input.

---

## Requirements

* Python 3.9+ (standard library only; Python 3.9 and 3.10 also need `tomli` for `build.toml`, see [Engine and settings](#engine-and-settings))
* A LaTeX distribution that includes `latexmk`:
  * Windows: [MiKTeX](https://miktex.org/) or [TeX Live](https://www.tug.org/texlive/)
  * macOS: [MacTeX](https://www.tug.org/mactex/)
  * Debian/Ubuntu: `sudo apt install latexmk texlive-latex-extra texlive-plain-generic texlive-fonts-recommended texlive-science texlive-xetex texlive-luatex texlive-extra-utils chktex latexdiff`
* Optional: [uv](https://docs.astral.sh/uv/)

Check that it works with `latexmk --version`. If the command isn't found, add your LaTeX installation to `PATH` and restart the terminal.

---

## Building

From the repository root:

```bash
python scripts/build.py                  # build documents whose PDF is missing or out of date
python scripts/build.py --force          # rebuild everything (latexmk -g, even when its cache looks current)
python scripts/build.py -j 2             # build two documents at a time (default: CPU count, up to 8)
python scripts/build.py "FP-123 Report"  # build only that document (or a glob such as "reports/*")
python scripts/build.py --new MyDoc      # create files/MyDoc/main.tex from a template
python scripts/build.py --watch --open   # keep running, rebuild on every save, open each PDF after its first build
python scripts/build.py --list           # show discovered documents and whether they are up to date
python scripts/build.py --clean          # delete out/
python scripts/build.py --changed-since origin/main   # only documents changed since a git ref
```

With uv you can use `uv run scripts/build.py ...` instead of `python`.

Output is coloured on a terminal. Set `NO_COLOR=1` to turn colour off. With several jobs, each document's output appears as one block when that document finishes. `--watch` waits for half a second without changes before rebuilding, so one save builds once. Ctrl+C in a parallel build cancels the documents still in the queue and exits with status 130.

A document counts as **out of date** when any file in its directory, or `build.py` itself, is newer than its PDF. Dotfiles such as `.DS_Store` are ignored.

For each document, the script:

1. Runs `latexmk -pdf` in `.latex-cache/` (git-ignored), with one directory per document, so nested documents never share one. The `.aux`, `.bbl`, ... files stay there between builds, so a text edit needs one LaTeX pass instead of a full cold build (about 7 s instead of 19 s for the report). If a build fails with cached files, the cache is wiped and the build retried once from scratch. CI keeps the same cache with `actions/cache`.
2. Copies only the final PDF into `out/`.
3. Writes the full build log to `out/<name>.log` (for example `out/FP-123 Proposal.log`), whether the build succeeded or not. The log starts with the result and any errors. Then comes the latexmk console output from every pass, followed by LaTeX's own `.log` and BibTeX's `.blg`, so every info line, warning and error is in it.
4. Carries on with the remaining documents if one fails (an unexpected exception included, reported as `Build crashed: ...`), and prints a summary.
5. Exits non-zero if any document failed.

Errors are printed in the summary as `files/<doc>/<file>:<line>: message`, which most editors can open directly. The same lines start the document's `.log`.

After a build (not `--watch`) the script writes `out/build-report.json` with every document it built in that run: `ok`, `seconds`, `engine`, `pages` (`null` if unknown or failed), `errors` (`file`, `line`, `message`), `warnings` (a count of LaTeX warnings) and `error` (the message of a crashed build, otherwise `null`). CI reads this file, so keep its keys stable.

Before building, it deletes any PDF or log in `out/` whose `main.tex` no longer exists. `--watch` does this too.

In `--watch` mode, a document that fails is retried only after one of its files changes again.

If `latexmk` isn't on `PATH`, the script stops before building and prints installation links.

---

## Adding a document

Create a directory under `files/` with a `main.tex`:

```text
files/My New Document/
├── main.tex
├── references.bib
├── custom.cls
└── Figures/
    └── figure.png
```

You don't need to change `build.py`. Keep every input inside the document's directory. Files outside it (for example `\input{../shared/x}`) aren't tracked for rebuilds.

### Engine and settings

The LaTeX engine comes from a magic comment in the first 20 lines of `main.tex`:

```latex
% !TEX program = xelatex
```

Supported engines are `pdflatex` (the default), `xelatex` and `lualatex`.

A document can also have a `build.toml` next to its `main.tex`. Every key is optional:

```toml
engine = "lualatex"      # overrides the magic comment
shell_escape = true      # default: false
latexmk_args = ["-g"]    # extra arguments passed to latexmk
```

An invalid `build.toml` or unknown engine fails that document only, and the reason is at the top of its log.

---

## GitHub Actions

[`.github/workflows/build-pdf.yml`](.github/workflows/build-pdf.yml) runs when anything under `files/` or `scripts/`, the workflow itself, or the Python project files change. It can also be started manually (`workflow_dispatch`).

* **build** installs TeX Live, compiles only the documents that changed, and uploads the resulting PDFs and logs as the `pdfs` run artifact. What counts as changed depends on the trigger:
  * Push to the default branch: everything changed since the last fully successful publish. Runs that were skipped or failed get caught up.
  * Other branches: changed in the push.
  * Pull requests: changed since the base branch.
  * Changes to `scripts/` or the workflow, manual runs, and the first publish rebuild every document.
* **Job summary** (every run): a table per document with its status, pages, words, build time, LaTeX warnings, chktex warnings and first error. The same table is a comment on the pull request, updated in place on each push, with a link to the run's artifacts. Fork pull requests get no comment, because their token is read-only.
* **latexdiff** (pull requests): each changed document is compared with the base branch, and the diff PDFs are uploaded as the `diff-pdfs` artifact. A failure is noted in the comment and never fails the run.
* **Publish** runs only on the default branch. [`scripts/publish_release.py`](scripts/publish_release.py) syncs the [`pdfs` release](https://github.com/NotTahaAli/LatexPipeline/releases/tag/pdfs):
  * Each rebuilt document's PDF and `<name>.log` replace their older versions.
  * A document that fails keeps its previous PDF, and its `.log` shows why the latest build failed.
  * The PDF and log of a deleted document are removed.
  * The release notes list every document with links to its PDF and log, its pages, and the status of its last build.
  * File names on the release are escaped. Every character other than letters, digits, `.` and `-` becomes `_` followed by its UTF-8 bytes in hex: space becomes `_20`, `_` becomes `_5F`, and `/` becomes `_2F`. For example, `FP-123 Proposal.pdf` becomes `FP-123_20Proposal.pdf`. To recover the original name, replace each `_` with `%` and URL-decode. The release page shows the original path as the file's label.
  * After a fully successful build, the `pdfs` tag moves to the commit that was built. That tag marks where the next run starts from.
* **requirements** fails if `uv.lock` or `requirements.txt` has fallen behind `pyproject.toml`.

`out/` is git-ignored, so never commit it.

---

## Python project and `requirements.txt`

The repository is a uv project (`pyproject.toml` + `uv.lock`). `requirements.txt` is generated from it. Never edit it by hand.

Enable the git hook once per clone:

```bash
git config core.hooksPath .githooks
```

On every commit, [`.githooks/pre-commit`](.githooks/pre-commit) re-exports `requirements.txt` (and refreshes `uv.lock`) and stages both. Without the hook, run the export manually:

```bash
uv export --no-hashes --no-emit-project -o requirements.txt
```

Add dependencies with `uv add <package>`. The scripts use only the standard library, so keep it that way where possible. `publish_release.py` also needs the `gh` CLI, which GitHub runners already include; it's only used in CI.
