# AGENTS.md

`README.md` covers usage; `python scripts/build.py --help` lists flags.

## Layout

- `files/<name>/main.tex` is one document, built to `out/<name>.pdf`. Only directories under `files/` are searched.
- A document's inputs are everything in its own directory. Keep `.cls`, `.bib`, and figures beside `main.tex`. Files outside it (`\input{../shared/x}`) count for rebuild detection (local mtime and CI `--changed-since`) once a build has recorded them in the document's `.fls` (latexmk `-recorder`, in `.latex-cache/`). A document's first build sees only its own directory.
- `out/` is generated and git-ignored. Every build writes `out/<name>.log`, which holds the result plus the latexmk, LaTeX and BibTeX logs. `build.py` also deletes PDFs and logs whose `main.tex` is gone.
- CI publishes to the GitHub release tagged `pdfs` through `scripts/publish_release.py`. That tag is also the bookmark CI diffs against, so treat it as CI-owned. Moving or deleting it by hand changes what the next run rebuilds.

## Build script

- `scripts/build.py` uses only the Python standard library and must keep working on Windows, macOS, and Linux. Reach for stdlib before adding a dependency.
- `scripts/accel.py` holds the TikZ externalization and `--focus` helpers `build.py` imports (pretex files injected through `latexmk -usepretex`). Keep it stdlib-only and cross-platform too; every accelerated path must fall back to a plain build.
- `scripts/publish_release.py` runs only in CI. It imports from `build.py` and calls the `gh` CLI. It uploads the PDFs and logs in `out/`, so in CI `out/` must hold only this run's output. A failed document then keeps its previous PDF next to the new `.log`. Release file names use reversible `_XX` hex escaping (`asset_name`), and deletion relies on that mapping. Change the naming rule and every existing file on the release gets deleted and re-uploaded under its new name on the next run. The release notes keep their per-document state in a hidden `<!-- latex-pipeline-state: ... -->` comment, so changing that format loses the "PDF from" column for documents not rebuilt in the next run.
- `scripts/ci_report.py` is CI-only and stdlib-only. It reads `out/build-report.json` (written by `build.py`) and writes `out/lint-report.json`. Its `diff` output goes outside `out/`, so the latexdiff PDFs are never published to the release.
- If you rename the workflow file or add shared inputs outside `files/`, update `GLOBAL_INPUTS` in `build.py` and the `paths:` filters in `.github/workflows/build-pdf.yml` together.
- Building needs only Python and a LaTeX distribution; Docker is never required. If this machine has no LaTeX, optionally verify in a throwaway `ubuntu:24.04` container with `apt-get install --no-install-recommends latexmk texlive-latex-extra texlive-plain-generic texlive-fonts-recommended texlive-science texlive-xetex texlive-luatex texlive-extra-utils chktex latexdiff python3 git`. The LaTeX packages are the same ones CI installs (`python3` and `git` are for the container). If a document adds a LaTeX package, update the package list in the workflow and `README.md`.

## Python project

- `requirements.txt` is generated from `uv.lock`, so change dependencies with `uv add` / `uv remove`. The pre-commit hook in `.githooks/` re-exports it. Outside the hook, run `uv export --no-hashes --no-emit-project -o requirements.txt`, with exactly these flags, because CI diffs against that output.
