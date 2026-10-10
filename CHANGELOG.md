# Changelog

Format follows [Keep a Changelog](https://keepachangelog.com/en/1.1.0/). Changes on this branch relative to `origin/main`.

## [Unreleased]

### Added

- Optional DOCX export with pandoc: `build.py --docx` writes `out/<name>.docx`; "Export DOCX" in the editor's More menu (owner only, hidden without pandoc). Never published to the release.
- `build.py --new NAME --template {article,report,beamer,letter}`; `report` creates chapters, `refs.bib`, `figures/` and a commented `build.toml`.
- **Offline editor**: `python scripts/vendor_ui.py` copies the pinned PDF.js, CodeMirror, KaTeX and Yjs files into `scripts/serve_ui/vendor/` (git-ignored); `serve.py` then serves them itself and the CSP drops the CDNs.
- **Local editor** (`scripts/serve.py`): live PDF preview, SyncTeX jump in both directions, CodeMirror source editor with visual mode, outline with per-section word goals, file tree (new, rename, delete), figure drop or paste, spell check, build progress, error and warning panels with plain-language hints, restore of the last build after a restart.
- **Chapter preview** in the editor and `build.py --focus PATH`: typeset one part of a large document with the full build's numbering and references (about 1.7 s for a 303-page report). `--focus auto` follows the file saved last.
- **Co-editing and sharing** (`serve.py --share`): token-gated view and edit links over cloudflared, ngrok, localtunnel, pinggy, localhost.run or `local`; live shared editing (Yjs) with cursors and presence; `--share-selftest`.
- **TikZ/pgfplots externalization**: figures compile once, in parallel, and are cached by source hash, so a text edit no longer re-typesets figures. Falls back to a plain build when it cannot apply.
- **Grammar checking** with LanguageTool (`scripts/grammar.py`): local server, or the public API only by explicit opt-in; `ci_report.py lint --grammar`; Grammar tab with underlines and quick fixes in the editor; `grammar`, `grammar_url`, `disabled_rules` in `build.toml`.
- **`build.toml` keys**: `externalize`, `pdfa`, `lang`, `timeout` (and the grammar keys). Opt-in PDF/A (`pdfa = "2b"`) with a presence check of the PDF/A markers, and validation by veraPDF when `verapdf` is on `PATH`. The pretex fixes what veraPDF flagged (xcolor cmyk, missing ToUnicode for math symbols, object streams in PDF/A-1): `1b`, `2b`, `2u`, `3b` pass.
- **Bib field suggestions** (`scripts/bibfix.py`): for entries lacking required fields, Crossref (by DOI, or by a close title match, never a guess) supplies the missing fields only. `ci_report.py lint --bib-lookup` reports them as notices; the editor's Lint tab has a "Look up" button that shows the fields and, on Apply, inserts them into the open `.bib` without touching the rest of the file. Network only on that opt-in or click.
- `build.py` flags: `--source DIR`, `--focus`, `--profile`, `--timeout`, `--new`, `--changed-since`, `--watch --open`, `-j`; per-document `phases` (figures, LaTeX) in `out/build-report.json`; PDF size in the report and CI table.
- Build errors come with hints (`scripts/hints.py`); `-synctex=1` on every build; log lines no longer wrap at 79 columns.
- Files outside a document's directory count for rebuild detection once a build has recorded them (latexmk `.fls`).
- **Lint**: structural checks (labels, references, figures), bib checks (required fields, suspicious year, duplicate DOIs, unprotected title capitals, unused `.bib`), overfull boxes, chktex, word counts, `.lint-baseline`, `--strict`.
- CI report (`ci_report.py`): job summary table, sticky pull request comment, latexdiff PDFs as the `diff-pdfs` artifact, release notes table of every document.
- VS Code tasks and LaTeX Workshop settings; ruff config and a lint workflow; a unit test suite and the co-editing check (`tests/collab_check.mjs`) in CI.

### Changed

- Editor UX pass: usable on phones (editor and PDF stack, top bar fits, More menu spans the screen), themed find/replace panel (dark mode), "Build failed" note no longer covers the zoom buttons, scrollable drawer tabs, DOCX export feedback, Home/End in the More menu, and contrast fixes (axe: 0 violations).
- **Faster builds** on `bench/sample-report` (303 pages, 25 TikZ figures, 4 CPUs): cold build 93 s to 65 s, one-line text edit 47 s to 20 s, `--focus` preview 1.7 s.
- **Faster CI**: TeX Live comes from a cache (restore about 12 s) filled by a parallel `texlive-cache` job; on a miss the build uses trimmed apt (about 70 s). The build job takes about 19 s warm and about 70 s cold.
- `sample-report` moved from `files/` to `bench/` and is built with `--source bench`. CI never builds it, and the next publish deletes it from the `pdfs` release.
- `\today` follows the date of the last commit that touched the document, so PDFs are reproducible (`SOURCE_DATE_EPOCH`); unchanged release files are not uploaded again.
- Pushes build in CI only on `main` (feature branches build through their pull request); path filters no longer include the editor files.
- Runaway builds are killed after `timeout` seconds (default 600).
- The cache of a failed build is wiped and the build retried once; injected pretex and `\include` directories survive that retry.
- Parallel builds print each document's output as one block; `--clean` also removes `.latex-cache/`.
- `build-report.json` is written atomically.
- LuaLaTeX is allowed while sharing, with a warning in the Share dialog.

### Fixed

- Figure cache bugs found in review (stale figures, private output directories, recorder path on the document's drive).
- Page count is read from wrapped LaTeX log lines.
- Plain saves no longer desync co-editing rooms; shared-session builds can read their injected pretex.
- Windows and Python 3.9 test compatibility (`write_text(newline=)`).
- Editor: nested-interactive accessibility finding in the warnings list; grammar review findings.

### Security

- While sharing, treat every non-owner as hostile: tokens on every request (loopback included), builds run with `-norc`, `shell_escape=f`, `openin_any=p`, `openout_any=p`, unsafe `latexmk_args` rejected, builds time out after at most 300 s and are rate limited (6 a minute).
- View and edit roles enforced in the API and message bus: no cross-document messages, `build.toml` and `latexmkrc` owner-only (also in live rooms), uploads magic-byte checked, per-client and per-room caps, Content-Security-Policy with pinned CDNs, Host header check.
- Grammar: the public API is never chosen automatically, needs confirmation, `GRAMMAR_PUBLIC_OK=1` in CI and the owner's permission while sharing; requests are chunked and throttled.
- Hardened file operations in the editor server.

### Removed

- `sample-report` from `files/` (now `bench/sample-report`).
- The apt-only TeX Live install in CI as the primary path (kept as the cache-miss fallback).
