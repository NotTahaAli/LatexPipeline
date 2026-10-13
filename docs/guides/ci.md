# CI and lint reports

What the GitHub Actions workflows build and publish, how release files are named, and how to run the lint and CI reports yourself; for anyone using the repository (or a copy made from the template) on GitHub.

## When the build workflow runs

[`.github/workflows/build-pdf.yml`](../../.github/workflows/build-pdf.yml) runs on pull requests and on pushes to the default branch when any of these change:

- `files/`
- a build script: `build.py`, `accel.py`, `sandbox.py`, `hints.py`, `a11y.py`, `publish_release.py`, `ci_report.py`
- the workflow itself, `.github/texlive-packages.txt`, `.github/fonts/`, `.github/hooks/`
- the Python project files

Editing only `serve.py`, its UI or the tests does not trigger it. A push to a feature branch does not run it: its pull request does. You can also start it by hand (`workflow_dispatch`).

## What the jobs do

| Job or step | What it does |
| --- | --- |
| **build** | Gets TeX Live, compiles only the documents that changed, and uploads the PDFs and logs as the `pdfs` run artifact. |
| **Job summary** (every run) | A table per document: status, pages, words, build time, LaTeX warnings, chktex warnings and first error. The same table is a pull request comment, updated in place on each push, with a link to the run's artifacts. Fork pull requests get no comment, because their token is read-only. |
| **latexdiff** (pull requests) | Compares each changed document with the base branch and uploads the diff PDFs as the `diff-pdfs` artifact. A failure is noted in the comment and never fails the run. |
| **Publish** (default branch only) | Syncs the `pdfs` release, see [below](#the-pdfs-release). |
| **requirements** | Fails if `uv.lock` or `requirements.txt` has fallen behind `pyproject.toml`. |

TeX Live comes from a cache (about 12 s). On a cache miss, build uses trimmed apt (about 70 s) while a parallel `texlive-cache` job installs TeX Live from tug.org via `zauguin/install-texlive` and saves the cache for the next run. Its package list is `.github/texlive-packages.txt`.

### What counts as changed

- Push to the default branch: everything changed since the last fully successful publish. Runs that were skipped or failed get caught up.
- Pull requests: changed since the base branch.
- A document also counts as changed when a changed file is in its recorded `.fls` inputs, restored from `.latex-cache`.
- Changes to `scripts/` or the workflow, manual runs, and the first publish rebuild every document.

## The `pdfs` release

[`scripts/publish_release.py`](../../scripts/publish_release.py) syncs the [`pdfs` release](https://github.com/NotTahaAli/LatexPipeline/releases/tag/pdfs):

- Each rebuilt document's PDF and `<name>.log` replace their older versions.
- So do its `[[output]]` PDFs and logs, and `<name>.html.zip` when its `build.toml` sets `html = true`.
- Nothing else in `out/` is published: review, latexdiff, chapter preview and `--pages` PDFs, DOCX and arXiv zips stay local.
- A document that fails keeps its previous PDF, and its `.log` shows why the latest build failed.
- The PDF and log of a deleted document are removed.
- The release notes list every document with links to its PDF and log, its pages, and the status of its last build.
- A file whose bytes the release already holds (same sha256, as GitHub reports it) is not uploaded again.
- After a fully successful build, the `pdfs` tag moves to the commit that was built. That tag marks where the next run starts from.

`publish_release.py` needs the `gh` CLI, which GitHub runners already include.

### Release file names

File names on the release are escaped. Every character other than letters, digits, `.` and `-` becomes `_` followed by its UTF-8 bytes in hex.

| Character | Escaped |
| --- | --- |
| space | `_20` |
| `_` | `_5F` |
| `/` | `_2F` |

For example, `FP-123 Proposal.pdf` becomes `FP-123_20Proposal.pdf`. To recover the original name:

1. Replace each `_` with `%`.
2. URL-decode the result.

The release page shows the original path as the file's label.

## Pin the TeX Live year

`TEXLIVE_VERSION` at the top of the workflow (empty: the latest) is passed to `zauguin/install-texlive` as `texlive_version` and is part of the cache key. Setting it to, say, `"2025"` installs that year once and keeps it. Historic years come from tug.org's archive; the apt fallback on a cache miss is always Ubuntu's own TeX Live, whatever the setting.

## Extra fonts and hooks

- Font files in `.github/fonts/` are copied to `~/.local/share/fonts` before building (fontconfig for XeLaTeX, `OSFONTDIR` for LuaLaTeX).
- `.github/hooks/pre-build` runs before the build and `.github/hooks/post-build` after it (with `BUILD_OUTCOME` set), both with `bash`.

These are repository files that run in CI only: neither the editor nor a shared link ever runs them. Changing them rebuilds every document.

## Binaries workflow

[`.github/workflows/binaries.yml`](../../.github/workflows/binaries.yml) runs on pushed `v*` tags (and manually). It builds the single-file binaries on Linux, macOS and Windows, runs each with `--help` (and `build --list`), and attaches them to that tag's release, creating the release if needed. A manual run on a branch keeps them as run artifacts only.

`out/` is git-ignored, so never commit it.

## Run the lint and CI reports

`scripts/ci_report.py` (standard library only) reads `out/build-report.json` from the last build:

```bash
python scripts/ci_report.py summary                  # markdown table of the last build
python scripts/ci_report.py lint [DOC ...]           # labels, refs, figures, bib checks, overfull boxes, accessibility notices, chktex, word counts -> out/lint-report.json
python scripts/ci_report.py lint --strict            # exit 1 on findings
python scripts/ci_report.py lint --update-baseline   # accept current findings in files/<doc>/.lint-baseline
python scripts/ci_report.py lint --grammar           # add LanguageTool findings
python scripts/ci_report.py lint --bib-lookup        # suggest missing bib fields from Crossref (sends DOIs/titles)
python scripts/ci_report.py lint --verify-citations  # flag bib entries Crossref does not confirm (sends DOIs/titles)
python scripts/ci_report.py lint --format            # list .tex files tex-fmt would change (notices)
python scripts/ci_report.py diff --base REF --out DIR  # latexdiff of changed documents
python scripts/ci_report.py pr-comment [--print]     # sticky PR comment (needs gh and GH_TOKEN)
```

A missing `chktex`, `texcount` or `latexdiff` leaves its column blank. `--overfull-pt` (default 10) sets the overfull-box threshold. `--grammar`, `--bib-lookup` and `--verify-citations` are opt-in; the workflow passes none of them.

### What lint reports

Besides undefined references and unused labels, `lint` reports a label defined twice (`duplicate-label`). These are notices, which `--strict` ignores (after [pre-commit-latex-hooks](https://github.com/jonasbb/pre-commit-latex-hooks)):

| Notice | Finds |
| --- | --- |
| `cite-space` | A breakable space or line break before `\cite`, `\citep`, `\parencite` or `\autocite`. Write `word~\cite{key}`. |
| `cref-case` | `\cref` starting a sentence, or `\Cref` inside one. |
| `spelling` | A word spelt both the US and the UK way in one document: color/colour, behavior, modeling, analyze, optimize, organize, center, labeled. |

### Verify citations

`--verify-citations` checks every entry of every `.bib` against Crossref. It reports "possibly fabricated or wrong reference" when:

- its DOI does not resolve;
- there is no DOI and no Crossref record has a close title (similarity >= 0.9, five results searched by title and first author's surname);
- the record disagrees on title, year (by more than one) or first author's surname.

Only the DOI, the title and that surname are sent. Lookups are throttled (30 a minute) and cached. An entry that could not be checked (network, rate limit) is skipped with a message, never reported.

Crossref does not list everything (many books, theses, web pages, very new preprints), so read a finding as "check this". Accept real references with `--update-baseline`.

### Formatting check

`--format` runs `tex-fmt --check` on each `.tex` file (with the document's `tex-fmt.toml`) and reports the files it would change as notices, which `--strict` ignores. `build.py --format` fixes them. A missing tex-fmt is a message; the workflow does not pass `--format`, so formatting never fails CI.

## Use the pre-commit hooks

[`.pre-commit-hooks.yaml`](../../.pre-commit-hooks.yaml) offers three hooks:

| Hook | Runs |
| --- | --- |
| `latex-lint` | `ci_report.py lint --strict` |
| `latex-format` | `build.py --format --check` |
| `bib-tidy` | `build.py --tidy-bib --check` |

They run the repository's own `scripts/` (language `unsupported`, pre-commit 4.4 or newer, `python3` on `PATH`), so use them in a repository made from this template:

```yaml
repos:
  - repo: https://github.com/NotTahaAli/LatexPipeline
    rev: <a tag or commit>
    hooks:
      - id: latex-lint
      - id: bib-tidy
```

## Next

- [Building documents](building.md)
- [Grammar checks](grammar.md)
- [References and bibliographies](references.md)
