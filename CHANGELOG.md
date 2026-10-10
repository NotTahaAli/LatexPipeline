# Changelog

Format follows [Keep a Changelog](https://keepachangelog.com/en/1.1.0/). Changes on this branch relative to `origin/main`.

## [Unreleased]

### Added

- **Live preview** in the editor (`scripts/preview.py`, `scripts/serve_ui/preview.js`, `POST /api/preview`): 0.7 s after typing stops, the chapter around the cursor is typeset from the editor's unsaved text by a TeX process that already loaded the preamble (restarted when the preamble, local classes and packages, `build.toml`, the engine or the sharing and sandbox settings change), and its pages replace that chapter's pages in the PDF view in place, keeping the scroll position. Cached TikZ figures are reused when the chapter's pictures did not change. 0.69 s (chapter 5) and 0.81 s (chapter 3, two pgfplots figures) request-to-PDF on `bench/sample-report`, against 2.0 and 3.1 s for `--focus`. Keystrokes cancel the preview in flight; edit role only, 120 a minute, sandboxed like every build; pdfLaTeX, XeLaTeX and LuaLaTeX. Settings > Live preview. `docs/realtime.md` records the measurements and the evaluation of TeXpresso (built and measured, not integrated), Real-Time LuaTeX and SwiftLaTeX; `experiments/live/texpresso_probe.py` drives TeXpresso headless; `tests/preview_e2e.py` checks the browser side.
- Licensed under AGPL-3.0-or-later (`LICENSE`, `pyproject.toml`).
- Hosted quota charges at least one 4 KiB block per file or folder (empty files count); the sandbox's `/tmp` (also `HOME`) is capped at 256 MB where bubblewrap supports `--size` (0.7+).
- **Hosted mode** (`scripts/host.py`, Linux, stdlib only): `host.py init` / `host.py serve` run a multi-user gateway for a VPS. sqlite database with versioned migrations; accounts with scrypt passwords, optional TOTP two-step sign-in with recovery codes, login throttling per IP and per account; sessions in HttpOnly SameSite cookies (hashed in the database, absolute and idle expiry) with same-origin and CSRF-token checks; workspaces (tenants) with admin / editor / viewer members, one-time invite links and admin password-reset links; sign-up modes (invite only, open, open to email domains); projects from the `build.py` templates or a zip upload (size and entry limits, no path traversal or symlinks), zip download and delete; quotas; an audit log; one `serve.py --gateway` worker per open project (idle stop, global and per-workspace caps), reached through a reverse proxy for HTTP, long-poll and WebSocket that strips client `X-Host-*` headers; strict CSP, HSTS on https. Builds run with `LATEX_SANDBOX=bwrap`; `serve` refuses to start without a working bubblewrap unless `--insecure-no-sandbox` (development only) is given.
- Hosted sign-in with OIDC providers (discovery, authorization code + PKCE, state bound to the browser, nonce; `iss`/`aud`/`azp`/`exp`/`iat` checked on the ID token fetched over TLS; verified email required) and GitHub (verified primary email). Provider accounts link to an existing account only from its account page, or automatically when the site allows it and that account's email is verified; two-step sign-in still applies.
- Hosted pages (`scripts/host_ui/`): sign-in with two-step codes, sign-up and invites, password reset, projects per workspace (new from a template, zip upload, download, delete), members and invite links, account (password, two-step sign-in with recovery codes, connected providers), site admin (settings, quotas, workspaces, users, audit log). Light and dark, phone-sized screens, axe-clean; `tests/host_e2e.py` checks the whole flow in Chromium. README: "Hosting on a VPS" (Caddy, systemd, backups, `config.toml`).
- Hosted mode hardening: the project quota is enforced by the worker on every write and build (whole project area, fresh size under the write lock, `507`); zip uploads spool to disk (`max_upload_mb`, two at once) and leave out build configuration files; `max_connections`, `max_streams_per_user` and `signups_per_ip_hour` in `config.toml`; workers start outside the global lock and a full server evicts fairly between workspaces; single-use two-step and recovery codes also under parallel requests; IPv6 throttling per `/64` and a signed known-device cookie so failed guesses cannot lock an owner out; open sign-up no longer reveals whether an address has an account; proxied responses get `X-Frame-Options`, `nosniff`, `Referrer-Policy` and `frame-ancestors 'none'`; cross-site sub-resource GETs under `/p/` are refused; `config.toml` and `host.db` are mode 600.
- `build.template_files(title, template)`: the template text `--new` writes, for reuse.
- `serve.py --gateway`: a worker mode for the hosted gateway (`scripts/host.py`). It serves one document folder on loopback, accepts only requests carrying the per-worker `X-Host-Secret` (role and user from `X-Host-Role` / `X-Host-User`; no cookies, tokens or share links), treats everyone as a shared editor or viewer (sharing build restrictions always on, owner features off), keeps its `out/` and cache beside the document folder, and the editor works under a path prefix.
- biblatex with biber in CI (apt `texlive-bibtex-extra biber`, TeX Live `biblatex biber`); tested plain, sandboxed, with paranoid reads and with LuaLaTeX.
- **Sandboxed builds** (Linux): `build.py --sandbox`, `serve.py --sandbox` or `LATEX_SANDBOX=bwrap` run every LaTeX process (latexmk, figure jobs, `--focus`) under bubblewrap: no network, read-only system, TeX tree and document directory, writable build directory only, minimal environment, CPU/memory/file-size limits. A missing or broken bwrap fails the build instead of running unsandboxed. About 10 ms per run.
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
- **References panel** in the editor: every `.bib` entry of the document with cited / unused / duplicate / missing-field badges, missing cited keys, jump to each `\cite`, filter and sort, add / edit / delete through a per-type form, import from pasted BibTeX or a DOI (Crossref), and insert `\cite{a,b}` at the cursor. Edits go into the open `.bib` as byte-preserving splices (`bibfix.replace_entry`, `delete_entry`, `append_entry`). New endpoints `POST /api/bib` (view role may read), `/api/bib/edit` and `/api/bib/import` (edit role).
- `build.py` flags: `--source DIR`, `--focus`, `--profile`, `--timeout`, `--new`, `--changed-since`, `--watch --open`, `-j`; per-document `phases` (figures, LaTeX) in `out/build-report.json`; PDF size in the report and CI table.
- Build errors come with hints (`scripts/hints.py`); `-synctex=1` on every build; log lines no longer wrap at 79 columns.
- Files outside a document's directory count for rebuild detection once a build has recorded them (latexmk `.fls`).
- **Lint**: structural checks (labels, references, figures), bib checks (required fields, suspicious year, duplicate DOIs, unprotected title capitals, unused `.bib`), overfull boxes, chktex, word counts, `.lint-baseline`, `--strict`.
- CI report (`ci_report.py`): job summary table, sticky pull request comment, latexdiff PDFs as the `diff-pdfs` artifact, release notes table of every document.
- VS Code tasks and LaTeX Workshop settings; ruff config and a lint workflow; a unit test suite and the co-editing check (`tests/collab_check.mjs`) in CI.

### Changed

- Editor: academic redesign ("classic journal"): paper and ink palette with navy, oxblood, ochre and moss in light and dark, Newsreader for titles, Source Sans 3 for the interface, IBM Plex Mono for source (latin subsets in `scripts/fonts/`, served at `/fonts/`, also to the 401 page). Labelled Rebuild button, docked PDF toolbar, Error/Warning/Note tags in the drawer, two-column Settings, key hints in the palette and menu, a caution box for edit links in Share. On phones a Source / Split / PDF bar picks the pane; touch targets are 44 px.
- Hosted pages restyled ("classic journal": paper and ink, Newsreader / Source Sans 3 / IBM Plex Mono served by `host.py` from `scripts/fonts/` under CSP `font-src 'self'`): sign-in beside a title page, project cards with a "Start a project" card and new-project / zip-upload dialogs, members beside the invite form, account sections, and a site admin section menu (sign-up, workspaces, users, quotas and limits, sign-in providers, audit log). Light and dark, phone layouts.
- Editor: the More menu is grouped (Panels, Build, Document, Session, Help); the chapter preview opens at its first page and the full PDF returns to where you were reading; the build pill's tooltip says "Building... N s (last build took M s)" during a build; browsers that open a share link without a token get a small themed 401 page (API clients still get plain text); on phones the error and warning badges collapse into one count chip and the top bar stays on one row.
- `serve.py`: `GET /forward` no longer moves every open PDF viewer (it only answers with the position); `POST /forward` (owner only) does. A GET with side effects could be triggered by any web page.
- Editor UX pass: usable on phones (editor and PDF stack, top bar fits, More menu spans the screen), themed find/replace panel (dark mode), "Build failed" note no longer covers the zoom buttons, scrollable drawer tabs, DOCX export feedback, Home/End in the More menu, and contrast fixes (axe: 0 violations).
- **Faster builds** on `bench/sample-report` (303 pages, 25 TikZ figures, 4 CPUs): cold build 93 s to 65 s, one-line text edit 47 s to 20 s, `--focus` preview 1.7 s.
- A text edit in a file that also holds a TikZ figure no longer re-lists the figures: one LaTeX pass instead of two (23 s to 13 s on `bench/sample-report`).
- Figure jobs skip the `\input` files that define nothing and restore the recorded counters: compiling the 25 figures of `bench/sample-report` takes 14 s instead of 35 s (`--force` 87 s to 55 s, cold 100 s to 76 s).
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

- DOCX export is owner-only (`GET /docx/<doc>` too) and refused while sharing: pandoc follows `\input{../x}` and `\lstinputlisting{/abs/path}` into the .docx, so a shared source could leak files.
- `bibfix.parse` is linear on unterminated braces (a 64 KB input took ~30 s); `POST /api/bib/lookup` refuses text over 1 MB.
- Shared (non-owner) Crossref lookups use their own limit (10 per minute), so they cannot use up the owner's quota.
- While sharing, treat every non-owner as hostile: tokens on every request (loopback included), builds run with `-norc`, `shell_escape=f`, `openin_any=p`, `openout_any=p`, unsafe `latexmk_args` rejected, builds time out after at most 300 s and are rate limited (6 a minute).
- View and edit roles enforced in the API and message bus: no cross-document messages, `build.toml` and `latexmkrc` owner-only (also in live rooms), uploads magic-byte checked, per-client and per-room caps, Content-Security-Policy with pinned CDNs, Host header check.
- Grammar: the public API is never chosen automatically, needs confirmation, `GRAMMAR_PUBLIC_OK=1` in CI and the owner's permission while sharing; requests are chunked and throttled.
- Hardened file operations in the editor server.

### Removed

- `sample-report` from `files/` (now `bench/sample-report`).
- The apt-only TeX Live install in CI as the primary path (kept as the cache-miss fallback).
