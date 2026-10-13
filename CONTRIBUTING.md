# Contributing

How to set up a development clone, run the tests and lint, and keep the Python project files in sync; for people changing the code in this repository.

Repository rules (which files are build inputs, sandboxing, what each script may import) are in [AGENTS.md](AGENTS.md). Read it before changing `scripts/`.

Report security issues as described in [SECURITY.md](SECURITY.md).

## Set up a clone

The repository is a uv project (`pyproject.toml` + `uv.lock`). `requirements.txt` is generated from it; never edit it by hand.

1. Enable the git hook once per clone:

   ```bash
   git config core.hooksPath .githooks
   ```

2. On every commit, [`.githooks/pre-commit`](.githooks/pre-commit) re-exports `requirements.txt` (and refreshes `uv.lock`) and stages both.

Without the hook, run the export by hand, with exactly these flags (CI diffs against this output):

```bash
uv export --no-hashes --no-emit-project -o requirements.txt
```

Add dependencies with `uv add <package>`. The scripts use only the standard library, so keep it that way where possible. `publish_release.py` also needs the `gh` CLI, which GitHub runners already include; it is only used in CI.

## Run the tests and lint

```bash
python3 -m unittest discover -s tests   # unit tests, standard library only
uvx ruff check scripts tests            # lint: rules in pyproject.toml
```

### Integration tests

| Test file | What it runs |
| --- | --- |
| `tests/test_integration_tools.py` | The external programs for real on one small document; each test is skipped when its program is missing. |
| `tests/test_integration_net.py` | The network code against loopback stub servers; it never leaves the machine. |
| `tests/test_cli_flags.py` | Every flag of `build.py`, `serve.py`, `host.py`, `mcp_server.py`, `ci_report.py` and `publish_release.py` in a real subprocess. |
| `tests/test_buildtools.py` | The real notification command of `--watch --notify`. |

**`test_integration_tools.py`** covers make4ht with dvisvgm (TikZ as SVG in `--html`), qpdf (`--pages`), pdftoppm and mutool (MCP page images, also under bubblewrap), latexdiff, the arXiv zip compiled again on its own, the cited-only `.bib`, `--deps` with BibTeX and biber, `--query` with texcount, `--preflight`, chktex, tex-fmt and veraPDF (`pdfa = "2b"`). On Ubuntu install the extras with `apt-get install dvisvgm qpdf mupdf-tools`; install veraPDF as in the `tagged-pdf` job.

**`test_integration_net.py`** runs `ai.post_json`/`post_stream`, `zotero.get`, `bibfix.get_json` and `grammar.post_form` against servers in `tests/netstubs.py`. They answer like the Anthropic Messages API and an OpenAI-compatible Chat Completions endpoint (plain and streamed), the Zotero web API and Better BibTeX, Crossref and LanguageTool. It also goes through `serve.py` and `host.py` (quotas, billed and unbilled failures, streams).

Two opt-in extras never run in CI:

| Variable | Effect |
| --- | --- |
| `LANGUAGETOOL_URL=http://127.0.0.1:8081` | Checks `/api/grammar` against a real LanguageTool server. |
| `LP_LIVE_NET=1` | Makes one real Crossref DOI lookup and one public LanguageTool request (the explicit `public` mode). |

**`test_cli_flags.py`** runs on a copy of `scripts/` in a temporary repository, with a fake `gh` on `PATH`, and fails when a `--help` lists a flag no test runs. Most of it needs LaTeX (and bubblewrap for `--sandbox`), so it skips on the CI runners, which have none; run it locally.

**`test_buildtools.py`** runs the notifier on the macOS and Windows runners (osascript, PowerShell). On Linux, `LP_NOTIFY_LIVE=1` in a desktop session with `notify-send` and `dbus-monitor` watches a notification reach the daemon.

### Browser end-to-end checks

These run by hand and need Chromium for Playwright. Each one also runs axe-core.

| Area | Command | Needs |
| --- | --- | --- |
| Hosted mode | `uv run --no-project --with playwright==1.56.0 python tests/host_e2e.py` | LaTeX, bubblewrap; the editor loads its libraries from CDNs |
| MCP clients | `uv run --no-project --with playwright==1.56.0 python tests/mcp_e2e.py [--insecure-no-sandbox] [--chromium PATH]` | |
| Live preview | `uv run --no-project --with playwright==1.56.0 python tests/preview_e2e.py [--chromium PATH] [--axe PATH]` | LaTeX, a full build of `bench/sample-report` |
| Navigation | `uv run --no-project --with playwright==1.56.0 python tests/nav_e2e.py [--chromium PATH] [--axe PATH]` | LaTeX, git |
| Suggest mode and named links | `uv run --no-project --with playwright==1.56.0 python tests/review_e2e.py [--chromium PATH] [--axe PATH]` | |
| Git | `uv run --no-project --with playwright==1.56.0 python tests/git_e2e.py [--chromium PATH] [--axe PATH] [--insecure-no-sandbox] [--only hosted\|local]` | LaTeX, git, openssl, bubblewrap unless `--insecure-no-sandbox` |
| Every other UI feature | `uv run --no-project --with playwright==1.56.0 python tests/ui_e2e.py [--part local\|hosted\|all] [--chromium PATH] [--axe PATH] [--insecure-no-sandbox]` | LaTeX, latexdiff, bubblewrap for the hosted part unless `--insecure-no-sandbox` |

What each one checks:

- **`host_e2e.py`** signs in with two-step codes, invites editors and a viewer, builds and co-edits a project, makes an API token on the Account page (`[api]` on), checks workspace isolation, sign-out and session expiry. axe-core runs on every host page in both themes and at phone width.
- **`mcp_e2e.py`** registers a client, signs in with a two-step code, consents, exchanges the code with PKCE, builds, reads, edits and renders a page over MCP, rotates the refresh token, and disconnects on the account page. axe-core runs on the consent and account pages (both themes, phone width).
- **`preview_e2e.py`** types into a chapter five times and measures keystroke to spliced pages. It checks the new word is in them, that the scroll position stays and that a newer keystroke aborts the preview in flight, runs axe-core in both themes, and restores the file.
- **`nav_e2e.py`** builds a throwaway document and checks label numbers, go to definition, references, the outline extras, TODOs, bookmarks, the symbol palette, the file graph, math preview, typeset selection, presentation mode, following the cursor, a label rename and the restored cursor after a reload. axe-core runs in both themes on the snippets dialog, the figure dialog of a pasted image, the table editor, the TikZ canvas and the Git panel.
- **`review_e2e.py`** starts `serve.py --share local` on a throwaway document with an owner and two named guests. It checks that a guest's typing becomes a suggestion under the link's name, undo and redo of own suggestions, one grouped suggestion from several cursors, IME composition, the suggestion card with an @mention reply and the bell, presence names and revoking a link, with axe-core in both themes and at phone width.
- **`git_e2e.py`** runs against a real https Git server on loopback (git http-backend behind a throwaway self-signed certificate, from `tests/test_git_live.py`, which runs the same server in the unit tests on Linux).
  - Hosted, an editor saves a Git token on the Account page, connects, pulls (a differing file is listed, never overwritten), pushes an edit, sees a push refused after the token was rotated, saves the new token, removes it and disconnects; the audit rows are checked.
  - Locally it clicks every button of the Git panel (diff, stage, unstage, commit, push, pull, a conflicting pull and Abort merge, branch switch and New branch, history).
  - axe-core runs on each state in both themes.
- **`ui_e2e.py`**, local part, prints the inventory (palette commands, More menu, tabs, toolbar buttons, dialogs, shortcuts). It runs every palette command, its shortcut and its More menu entry with a check of its visible effect, then the features one by one: hover cards, folding, snippets, figure, table and TikZ dialogs, bookmarks, zen mode, history (zip, latexdiff PDF, restore), the review markup PDF, lock, linked files, crash recovery, the assistant and grammar (stub servers on loopback), Zotero and the personal library, and sharing with a named link per role in its own browser (co-editing, suggestions and their card by role, @mentions, the bell, comments on the PDF by click and drag, request access, revoke).
  - The hosted part goes through every page of the gateway (sign-up, two-step code, password reset, projects, members and invites, access requests, account, each admin section, error pages).
  - Every step fails on a console error or a failed request, and axe-core must report no violations on each panel and dialog, light and dark.
  - `UI_E2E_ONLY=id,...` limits the command sweeps, `UI_E2E_FEATURES=name,...` the features.

### VS Code extension end-to-end

`tests/vscode_e2e.mjs` runs the extension in a real VS Code against a live `serve.py`. It is not in CI and needs LaTeX, network (for VS Code and pdf.js) and a display (`xvfb-run` on a headless machine). The hosted part starts a local `host.py`, so `bwrap` must work.

It runs every command, file operations, save conflicts (refused with 409, nothing lost), view and edit share links, the refusal of plain `http://` to another computer, quiet reconnects, and a hosted project through a `projects` API token (malformed, expired and wrong-scope tokens refused). With `--vsix` every window runs the packaged files, and a last window checks the extension installed from the package, so a file missing from the package fails the run. `@vscode/test-electron` and `@vscode/vsce` stay outside the extension:

```bash
npm install --prefix /tmp/vt @vscode/test-electron@3.1.0 @vscode/vsce@4.0.0
(cd vscode && /tmp/vt/node_modules/.bin/vsce package --out /tmp/vt/latex-pipeline.vsix)
xvfb-run -a node tests/vscode_e2e.mjs --modules /tmp/vt/node_modules --vsix /tmp/vt/latex-pipeline.vsix   # downloads VS Code to .vscode-test/; --cache DIR or --code PATH to reuse one
```

## What CI checks

The [Lint and tests](.github/workflows/lint.yml) workflow runs when `scripts/`, `tests/`, `vscode/` or the Python project files change:

- the unit tests on Ubuntu, Windows and macOS under Python 3.9 and 3.13;
- ruff (pinned version);
- on Ubuntu, the Node checks of the editor and extension helpers:

  ```bash
  node tests/collab_check.mjs scripts/serve_ui/collab.js   # co-editing
  node tests/latex_check.mjs scripts/serve_ui/latex.js     # folding, code check, completion
  node tests/tablepaste_check.mjs
  node tests/reviewexport_check.mjs
  node tests/edits_check.mjs
  node tests/nav_check.mjs scripts/serve_ui
  node tests/cov_check.mjs                                 # editor API and bus helpers
  node tests/vscode_check.mjs                              # VS Code extension and Git panel helpers
  ```

  plus `tableedit_check.mjs`, `figure_check.mjs`, `tikz_check.mjs` and `markdown_check.mjs` (table editor, figure, TikZ and Markdown tools);
- the `requirements.txt` check.

## Next

- [CI and lint reports](docs/guides/ci.md)
- [Documentation index](docs/README.md)
- [Security model](docs/explanation/security.md)
