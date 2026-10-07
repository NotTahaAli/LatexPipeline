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

* Python 3.9+ (standard library only, no packages needed)
* A LaTeX distribution that includes `latexmk`:
  * Windows: [MiKTeX](https://miktex.org/) or [TeX Live](https://www.tug.org/texlive/)
  * macOS: [MacTeX](https://www.tug.org/mactex/)
  * Debian/Ubuntu: `sudo apt install latexmk texlive-latex-extra texlive-plain-generic texlive-fonts-recommended texlive-science`
* Optional: [uv](https://docs.astral.sh/uv/)

Check that it works with `latexmk --version`. If the command isn't found, add your LaTeX installation to `PATH` and restart the terminal.

---

## Building

From the repository root:

```bash
python scripts/build.py            # build documents whose PDF is missing or out of date
python scripts/build.py --force    # rebuild everything
python scripts/build.py --watch    # keep running and rebuild on every save (Ctrl+C to stop)
python scripts/build.py --list     # show discovered documents and whether they are up to date
python scripts/build.py --clean    # delete out/
python scripts/build.py --changed-since origin/main   # only documents changed since a git ref
```

With uv you can use `uv run scripts/build.py ...` instead of `python`.

A document counts as **out of date** when any file in its directory, or `build.py` itself, is newer than its PDF. Dotfiles such as `.DS_Store` are ignored.

For each document, the script:

1. Runs `latexmk -pdf` in a temporary directory. No `.aux`, `.log`, `.bbl`, ... files end up in the repository.
2. Copies only the final PDF into `out/`.
3. Writes the full build log to `out/<name>.log` (for example `out/FP-123 Proposal.log`), whether the build succeeded or not. The log starts with the result and any errors. Then comes the latexmk console output from every pass, followed by LaTeX's own `.log` and BibTeX's `.blg`, so every info line, warning and error is in it.
4. Carries on with the remaining documents if one fails, and prints a summary.
5. Exits non-zero if any document failed.

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

---

## GitHub Actions

[`.github/workflows/build-pdf.yml`](.github/workflows/build-pdf.yml) runs when anything under `files/` or `scripts/`, the workflow itself, or the Python project files change. It can also be started manually (`workflow_dispatch`).

* **build** installs TeX Live, compiles only the documents that changed, and uploads the resulting PDFs and logs as the `pdfs` run artifact. What counts as changed depends on the trigger:
  * Push to the default branch: everything changed since the last fully successful publish. Runs that were skipped or failed get caught up.
  * Other branches: changed in the push.
  * Pull requests: changed since the base branch.
  * Changes to `scripts/` or the workflow, manual runs, and the first publish rebuild every document.
* **Publish** runs only on the default branch. [`scripts/publish_release.py`](scripts/publish_release.py) syncs the [`pdfs` release](https://github.com/NotTahaAli/LatexPipeline/releases/tag/pdfs):
  * Each rebuilt document's PDF and `<name>.log` replace their older versions.
  * A document that fails keeps its previous PDF, and its `.log` shows why the latest build failed.
  * The PDF and log of a deleted document are removed.
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

Add dependencies with `uv add <package>`. Both scripts use only the standard library, so keep it that way where possible. `publish_release.py` also needs the `gh` CLI, which GitHub runners already include; it's only used in CI.
