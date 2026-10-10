# LaTeX Document Pipeline

A cross-platform build script, a local editor with live preview, and a GitHub Actions workflow that publishes every document's PDF to a release.

**Latest PDFs:** [Releases, `pdfs`](https://github.com/NotTahaAli/LatexPipeline/releases/tag/pdfs). It always holds the newest PDF of every document, with the log of its latest build (`.log`) next to it.

Each directory under `files/` that contains a `main.tex` is a standalone document, built to `out/<path>.pdf`:

```text
files/abc/main.tex            ->  out/abc.pdf
files/reports/final/main.tex  ->  out/reports/final.pdf
```

## Quick start

```bash
latexmk --version                          # needs Python 3.9+ and a LaTeX distribution, see Requirements
python scripts/build.py                    # build documents that are missing or out of date
python scripts/build.py --watch --open     # rebuild on every save, open each PDF
python scripts/serve.py                    # editor with live preview at http://localhost:8000
python scripts/build.py --new MyDoc        # new document from a template (files/MyDoc/main.tex)
```

With uv, `uv run scripts/build.py ...` works too. `python scripts/build.py --help` and `python scripts/serve.py --help` list every flag.

## Contents

* [Requirements](#requirements)
* [Building](#building)
* [Documents and settings](#documents-and-settings): [build.toml keys](#buildtoml-keys), [grammar](#grammar), [PDF/A](#pdfa)
* [Large documents](#large-documents) and [benchmarks](#benchmarks)
* [VS Code](#vs-code)
* [Live preview and editor](#live-preview-and-editor), [sharing](#sharing)
* [Lint and CI reports](#lint-and-ci-reports)
* [GitHub Actions](#github-actions)
* [Tests and lint](#tests-and-lint)
* [Python project](#python-project-and-requirementstxt)

---

## Requirements

* Python 3.9+ (standard library only; Python 3.9 and 3.10 also need `tomli` for `build.toml`, see [build.toml keys](#buildtoml-keys))
* A LaTeX distribution that includes `latexmk`:
  * Windows: [MiKTeX](https://miktex.org/) or [TeX Live](https://www.tug.org/texlive/)
  * macOS: [MacTeX](https://www.tug.org/mactex/)
  * Debian/Ubuntu: `sudo apt install latexmk texlive-latex-extra texlive-plain-generic texlive-fonts-recommended texlive-science texlive-xetex texlive-luatex texlive-extra-utils chktex latexdiff`
* Optional: [uv](https://docs.astral.sh/uv/); [pandoc](https://pandoc.org/installing.html) for DOCX export

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
python scripts/build.py --new Thesis --template report   # also: beamer, letter (default: article)
python scripts/build.py --watch --open   # keep running, rebuild on every save, open each PDF after its first build
python scripts/build.py --list           # show discovered documents and whether they are up to date
python scripts/build.py --clean          # delete out/ and .latex-cache/
python scripts/build.py --changed-since origin/main   # only documents changed since a git ref
python scripts/build.py --profile        # also print the figures and LaTeX time of each document
python scripts/build.py --source bench sample-report   # build from bench/ instead of files/ (not built by CI)
python scripts/build.py my-report --focus Chapters/chapter5   # preview one part in ~2 s, see "Large documents"
```

With uv you can use `uv run scripts/build.py ...` instead of `python`.

Output is coloured on a terminal. Set `NO_COLOR=1` to turn colour off. With several jobs, each document's output appears as one block when that document finishes. `--watch` waits for half a second without changes before rebuilding, so one save builds once. Ctrl+C in a parallel build cancels the documents still in the queue and exits with status 130.

A document counts as **out of date** when any file in its directory, any file outside it that its last build read (from the `.fls` above), or `build.py` itself, is newer than its PDF. Dotfiles such as `.DS_Store` are ignored. Before the first build, only the directory counts.

For each document, the script:

1. Runs `latexmk -pdf` in `.latex-cache/` (git-ignored), with one directory per document, so nested documents never share one. The `.aux`, `.bbl`, ... files stay there between builds, so a text edit needs one LaTeX pass instead of a full cold build (see the measurements under Large documents). If a build fails with cached files, the cache is wiped and the build retried once from scratch. CI keeps the same cache with `actions/cache`.
2. Copies only the final PDF into `out/`.
3. Writes the full build log to `out/<name>.log` (for example `out/FP-123 Proposal.log`), whether the build succeeded or not. The log starts with the result and any errors. Then comes the latexmk console output from every pass, followed by LaTeX's own `.log` and BibTeX's `.blg`, so every info line, warning and error is in it.
4. Carries on with the remaining documents if one fails (an unexpected exception included, reported as `Build crashed: ...`), and prints a summary.
5. Exits non-zero if any document failed.

Errors are printed in the summary as `files/<doc>/<file>:<line>: message`, which most editors can open directly. The same lines start the document's `.log`. Each error is followed by a plain-language hint when one of the rules in `scripts/hints.py` matches (also in `build-report.json` as `hint`).

After a build (not `--watch`) the script writes `out/build-report.json` with every document it built in that run: `ok`, `seconds`, `engine`, `pages` (`null` if unknown or failed), `errors` (`file`, `line`, `message`), `warnings` (a count of LaTeX warnings), `error` (the message of a crashed build, otherwise `null`) and `phases` (seconds spent on `figures` and `latex`, `null` for a crash). CI reads this file, so keep its keys stable.

Before building, it deletes any PDF or log in `out/` whose `main.tex` no longer exists. `--watch` does this too.

In `--watch` mode, a document that fails is retried only after one of its files changes again.

If `latexmk` isn't on `PATH`, the script stops before building and prints installation links.

---

## Documents and settings

### Adding a document

Create a directory under `files/` with a `main.tex`:

```text
files/My New Document/
├── main.tex
├── references.bib
├── custom.cls
└── Figures/
    └── figure.png
```

`python scripts/build.py --new MyDoc --template report` writes this kind of layout for you (`article` is the default; `report` adds `chapters/`, `refs.bib`, `figures/` and a commented `build.toml`; `beamer` and `letter` are single files).

You don't need to change `build.py`. Keep inputs inside the document's directory where you can. A file outside it (for example `\input{../shared/x}`) is tracked for rebuilds once the document's first build has read it: the build records every file it reads in `.latex-cache/<name>/<name>.fls` (latexmk `-recorder`). TeX installation files and the cache are never tracked.

### DOCX export

`python scripts/build.py --docx [DOC ...]` builds the selected documents, then runs `pandoc main.tex` in each document's directory and writes `out/<name>.docx` (every `.bib` beside `main.tex` becomes a `--citeproc` bibliography). It exports even when the PDF build failed, needs `pandoc` on `PATH` (otherwise it stops with a message), and the files are never published to the release. In the editor, the owner gets "Export DOCX" in the More menu when pandoc is installed. It is owner-only while sharing, because pandoc reads any file a `\input` names.

### Reproducible PDFs

latexmk runs with `SOURCE_DATE_EPOCH` set to the time of the last commit that touched the document's directory (its newest input when git has no history for it) and `FORCE_SOURCE_DATE=1`. Building the same commit twice gives byte-identical PDFs. Side effect: `\today` shows that commit's date, not the build date.

### build.toml keys

The LaTeX engine comes from a magic comment in the first 20 lines of `main.tex` (`% !TEX program = xelatex`; `pdflatex` is the default). A `build.toml` next to `main.tex` can set the rest; every key is optional, and an unknown key, a wrong type or an unsupported value fails that document only (the reason is at the top of its log).

| Key | Default | Meaning |
| --- | --- | --- |
| `engine` | magic comment, else `"pdflatex"` | `"pdflatex"`, `"xelatex"` or `"lualatex"`; overrides the magic comment. |
| `shell_escape` | `false` | Allow `\write18`. Forced off while [sharing](#sharing). |
| `latexmk_args` | `[]` | Extra latexmk arguments (list of strings). While sharing, ones that run code or move output fail the build. |
| `externalize` | `true` | Compile TikZ/pgfplots figures once and cache them, see [Large documents](#large-documents). |
| `pdfa` | off | PDF/A level, `"2b"` or `"a-2b"` (part 1, 2 or 3 with conformance a, b or u, such as `"3b"`), see [PDF/A](#pdfa). |
| `lang` | `"en-US"` | Document language: PDF/A metadata and the grammar checker. |
| `timeout` | `600` | Seconds (10 to 7200) before a latexmk run is killed. `--timeout` changes the default; sharing caps it at 300. |
| `grammar` | `"auto"` | `"auto"`, `"off"`, `"local"` or `"public"`, see [Grammar](#grammar). |
| `grammar_url` | `http://localhost:8081` | LanguageTool server for local mode (`LANGUAGETOOL_URL` is the fallback). |
| `disabled_rules` | `[]` | LanguageTool rule ids to ignore. |

```toml
engine = "lualatex"
pdfa = "2b"
timeout = 900
```

### Grammar

Grammar and style checks come from [LanguageTool](https://languagetool.org). `scripts/grammar.py` reduces the LaTeX to prose (no comments, commands, math, `verbatim`, `lstlisting`, `minted` or `tikzpicture`; headings, captions, footnotes and the text of `\emph{}` and `\textbf{}` stay) and remembers where every character came from, so a finding is `file:line:col`. `\ref` and `\cite` become a number, math becomes `X`. Spacing and quote rules that misfire on extracted text are ignored, as is the Typography category.

* **Local server** (what `auto` picks when one answers `/v2/languages` within 300 ms): `docker run -d -p 8081:8010 erikvl87/languagetool`, or the LanguageTool zip with `java -cp languagetool-server.jar org.languagetool.server.HTTPServer --port 8081`. URL: `grammar_url` in `build.toml`, else `LANGUAGETOOL_URL`, else `http://localhost:8081`. The text never leaves your machine, and local requests bypass any proxy.
* **Public API** (`grammar = "public"` in `build.toml`, or Settings > Grammar in the editor): **your text is sent to languagetool.org** (`https://api.languagetool.org/v2/check`). It is never chosen automatically. Requests are paragraph batches of at most 15 KB, throttled below the free tier (20 requests and 75 KB a minute), and cached by paragraph, so re-checking an edited file only sends the changed paragraphs.
* `grammar = "off"` or nothing found: no check, no network. The language is `lang` from `build.toml` (default `en-US`).

`python scripts/ci_report.py lint --grammar` adds the findings to the lint report, annotations and `.lint-baseline` like the other lint findings (as notices; a server that cannot be reached is skipped with a message). The workflow does not pass `--grammar`. In GitHub Actions `grammar = "public"` is ignored unless `GRAMMAR_PUBLIC_OK=1` is set too.

### PDF/A

`pdfa = "2b"` (also `"a-2b"`, `"3b"`, ...) injects `\DocumentMetadata{pdfstandard=a-2b,lang=...}` before `\documentclass` through latexmk's `-usepretex`, so remove any `\DocumentMetadata` from `main.tex`. It needs LaTeX 2023-06 or newer. The same pretex also asks xcolor for RGB output (cmyk colours break PDF/A against the RGB OutputIntent), loads pdfTeX's glyph-to-Unicode maps (including `glyphtounicode-cmr` for math symbols, needed by the `u` level) and, for PDF/A-1, turns off object streams. After the build the PDF is checked for the XMP `pdfaid` declaration and an OutputIntent; if [veraPDF](https://verapdf.org) (`verapdf`) is on `PATH` the PDF is then validated against the requested level and the log note says passed, or which rules failed. Checked with veraPDF 1.30 on TeX Live 2023: `1b`, `2b`, `2u` and `3b` pass with pdfLaTeX, `2b` with XeLaTeX and LuaLaTeX, `1b` with LuaLaTeX, and `bench/sample-report` (TikZ, pgfplots, xcolor) passes `2b`. Images embedded as CMYK and transparency in included PDFs are not converted, and the `a` levels need tagging, which is not switched on (`tagging=on`), so they fail validation.

`build-report.json` and the CI summary table list each PDF's size. A `qpdf --object-streams=generate` pass was measured and dropped: pdfTeX already writes object streams, and it saved 0.3% (`files/test`, 101 KB) to 0.9% (`bench/sample-report`, 1.6 MB).

---

## VS Code

[`.vscode/tasks.json`](.vscode/tasks.json) has the build commands as tasks (Terminal, Run Task):

* **LaTeX: build changed documents** (the default build task): documents whose PDF is missing or out of date.
* **LaTeX: build one document**: asks for a name or glob, and builds it even when up to date.
* **LaTeX: build all (--force)**, **LaTeX: watch** (rebuilds on save), **LaTeX: list documents**, **LaTeX: clean**.

Errors from the build appear in the Problems panel. [`.vscode/settings.json`](.vscode/settings.json) makes LaTeX Workshop build through `build.py`. Its PDF viewer can't read `out/<doc>.pdf`, so use the live preview below or an external viewer.

---

## Live preview and editor

`python scripts/serve.py [DOC ...] [--port 8000] [--no-open] [--source DIR] [--editor vscode]` starts an editor and live preview at http://localhost:8000, bound to 127.0.0.1 only. It rebuilds on save, keeps scroll and zoom, lists errors with editor links, and double-click in the PDF jumps to the source. It needs the synctex CLI from TeX Live, and internet for PDF.js, CodeMirror and Yjs from pinned CDN versions.

**Finding your way.** The first visit shows a one-line quick start (Outline, Visual, Ctrl+K, double-click the PDF); "Got it" hides it for good and More > "Show getting-started tips" brings it back. The Outline lists sections with word counts; hover a row for the target button to set a per-section word goal (stored in this browser; the row fills as you write). While a build runs the top bar counts the seconds and a bar fills against the previous build's time, the empty PDF pane shows a page skeleton instead of a blank, and after a failed build the old PDF stays with a "last good PDF" note. The failing line is marked in the editor and the first error opens by itself with its hint. Fit width follows the pane until you zoom by hand. Visual mode sets prose in the UI sans font and leftover LaTeX in monospace, in both themes; PDF pages are always white. The page is keyboard operable (dialogs trap focus and return it, the build result is announced to screen readers) and passes axe-core in light and dark. A view link shows a "View only" chip, and Rebuild and Paragraph stay visible but disabled, with the reason as their tooltip.

**Build results.** "Up to date" after a restart still shows the pages, build time and warnings of the last build (read back from `out/`). The Problems panel has a Warnings tab parsed from the LaTeX log: undefined references and citations, overfull and underfull boxes, package warnings and BibTeX warnings, filterable by kind, each with a plain-language hint where `scripts/hints.py` explains it and a link to its source line.

**Chapters, files and figures.** "Preview chapter" (status bar, or the command palette) builds only the chapter you are editing with `build.build_focus`, keeping the numbering and references of the full document, and shows it with a "Chapter preview" bar and a "Back to full PDF" button; Settings can build it after each save. Jump-to-PDF is off while it is shown. The first preview of a document builds the whole thing once. The Files tab has new file, new folder, rename/move (F2) and delete (Delete key, with a confirmation); `main.tex`, dotfiles and, over a share link, build-config files cannot be touched, and open co-editing rooms of a renamed or deleted file are closed. Drop or paste a png, jpg or pdf into the editor to save it (into `Figures/` if it exists) and insert `\includegraphics`; up to 8 MB, content checked, svg refused. Spell check (the browser's own, no dictionary shipped) underlines misspelled words in visual mode and the paragraph panel only, never LaTeX commands or keys; Settings turns it off. Shared links: view cannot start previews or change files, edit can for the shared document only.

**Grammar.** The Problems panel has a Grammar tab. The open `.tex` file is checked 2 s after you stop typing (the text of the buffer, saved or not); findings are wavy underlines, and hovering one shows the message and up to three replacements to click (no replacement is offered where the span holds commands or math). Settings > Grammar (owner only) picks automatic, off, local or public, the local server URL, and whether the public API may be used while sharing. See [Grammar](#grammar) for what each mode sends where. Public mode asks for confirmation when chosen.

**Editing together.** Everyone who opens the same document edits it live (Yjs): you see each other's cursors and selections with names and colours, and a stack of avatars in the top bar opens the list of people. The server only relays the changes. One editor per file, the "leader", saves to disk through the normal atomic save; if the leader leaves, another takes over. If you change a file outside the editor (git, vim) while it is open, the change is merged into the shared text instead of overwriting anyone's typing. If the connection drops, you can keep typing; the edits merge when it returns, over WebSocket or the long-poll fallback.

### Sharing

`python scripts/serve.py --share [auto|local|cloudflared|ngrok|localtunnel|pinggy|localhost.run]` (or Share in the top bar) starts a tunnel and prints two links for the first document: a view link (read-only source, PDF and outline) and an edit link (edit the document's files and rebuild it). `local` gives token links without a tunnel (same network or your own tunnel). `auto` picks the first installed of cloudflared, ngrok, npx localtunnel, then ssh (pinggy, localhost.run); with none installed it tells you what to install. No link works after you stop sharing or quit.

* Install: [cloudflared](https://developers.cloudflare.com/cloudflare-one/networks/connectors/cloudflare-tunnel/downloads/) (no account, the default), [ngrok](https://ngrok.com/download) (free account and `ngrok config add-authtoken`), Node.js for localtunnel, or ssh for pinggy and localhost.run. The ssh and ngrok parsers follow each tool's documented output but are untested here; if one fails, please report its output.
* ngrok's free plan shows a warning page on the first visit; each visitor clicks Visit Site once. localtunnel may ask for a tunnel password (your public IP).
* Cloudflare quick tunnels do not support Server-Sent Events; the editor uses WebSocket with a long-poll fallback, so it works. `python scripts/serve.py --share-selftest [--share PROVIDER]` starts the tunnel and checks `/api/health`, a WebSocket echo and a 30 s long-poll through the public URL, then prints a table. Run it on your own machine before a session; it needs outbound access that restricted networks do not give.

**Security while sharing:**

* Every request needs a token, loopback included. A tunnel connects to the server from 127.0.0.1, so "local" proves nothing; your own browser gets a private third token as a cookie when sharing starts. Tokens are random 32-byte URL-safe strings, compared in constant time. A link sets an HttpOnly, SameSite=Lax cookie on the first visit and redirects to a URL without the token. New links ("New links" in the Share dialog) disconnect everyone on the old ones.
* The view role cannot save, rebuild, or see other documents (their messages, cursors and names included), in the UI or the API; it can open existing files of the shared document. The edit role can create and change any file of the shared document (text, `.tex`, `.bib`, `.cls`, ...) and rebuild it, but never `build.toml`, `latexmkrc` or `.latexmkrc`, not even through live co-editing. Rebuilds are rate limited (6 a minute). Each client and room is capped in count and size.
* Grammar checks need the edit role (the view role cannot start one; 30 a minute) and only for the shared document. Settings are owner-only. While sharing, public mode is refused for everyone, build.toml's `grammar = "public"` included, unless the owner ticked "Allow the public API while sharing"; the Share dialog says which applies. The server never sends the local URL to non-owners.
* Builds while sharing are restricted, because LaTeX source is code:
  * Shell escape is off (`shell_escape=f`) for the whole server, and `latexmk` runs with `-norc`, so no `latexmkrc` is read. A `build.toml` whose `latexmk_args` enable shell escape, name programs or code to run (`-e`, `-r`, `-pdflatex=...`, `-latexoption`, `-pretex`, `-usepretex`, `-cnf-line`), or move the output (`-outdir`, `-auxdir`, `-jobname`) fails the build.
  * LuaLaTeX stays allowed, but Lua can write files even with shell escape off, so **an edit link to a document that uses (or is switched to) LuaLaTeX can run code on your computer**. The Share dialog says so. Give edit links only to people you trust; view links are safe.
  * TeX may only read and write inside the document's directory (`openin_any=p`, `openout_any=p`): `\input{/etc/passwd}` fails, and so do inputs that leave the directory with `..` (for example `\input{../shared/x}`) and dotfiles. Such a document builds again when you stop sharing. Your own `latexmkrc` is also ignored until then.
* Responses carry a Content-Security-Policy that allows only this origin and the pinned CDNs, `X-Content-Type-Options: nosniff`, `Referrer-Policy: no-referrer` and `frame-ancestors 'none'`. The Host header must be loopback or the tunnel's own host.
* Anyone with the edit link can change every file of that document and make this machine compile it, so share links only with people you trust, and treat the edit link like a password. These restrictions make that much safer, but compiling someone else's LaTeX is never risk-free, and they do not cover `--host 0.0.0.0`, which is not what sharing is for.

---

## Large documents

Nothing here needs a change to the document.

**TikZ/pgfplots figures are compiled once and cached.** If a document uses `tikz` or `pgfplots` (in `main.tex`, a class, a package or a chapter), `build.py` switches on TikZ's `external` library through `latexmk -usepretex`. A first run lists the figures, they are compiled in parallel (about one process per CPU, shared between the documents built at once; no `make` and no shell escape needed), and the text passes then include the finished PDFs. Figures are cached in `.latex-cache/_figcache/` by the hash of their source, so an edit re-typesets only the text, and renumbering or moving a figure costs nothing. The cache is also invalidated when the preamble, a file the preamble `\input`s, a local `.cls`/`.sty`/`.csv`/`.dat`/`.tikz` file, the TeX engine or the TikZ/pgfplots packages change, and figures nobody uses any more are deleted. Works with `pdflatex`, `xelatex` and `lualatex`. Documents without TikZ are built as before, and so are documents with `remember picture`, `overlay` or `tikzmark` pictures, which cannot be cut out as figures. If the figures cannot be made, the document is built again without externalization and its log says so (a LaTeX error in the text does not trigger that second build).

Limits (use `externalize = false` in `build.toml` if they bite, or `--force` to recompile every figure):

* A figure is recompiled when its source text changes. A macro or `\tikzset` defined in a chapter, a changed `\ref` value inside a figure, or an `\input` file read inside the picture is not noticed. `\addplot table` files are only noticed when they have a `.csv`, `.dat`, `.tsv` or `.table` extension.
* Several pictures with identical source (a `\foreach` around a `tikzpicture`) are told apart by their order among the equal ones. Inserting an identical picture before them can redraw one.

**`--focus` previews one part of a document.** `python scripts/build.py my-report --focus Chapters/chapter5` typesets only the files under that path (a file or a directory, relative to the document) into `out/my-report.focus.pdf` with one LaTeX run, about 2 s for a 300 page report. The first full build records the `\input`/`\include` tree and all counters. The preview reads the full build's `.aux`, `.bbl` and `.toc` files, so chapter, figure and page numbers, references and citations match the full document. Every other `\input` does nothing (`\include` uses `\includeonly`), and pages outside the focused part are discarded. With `--watch`, `--focus auto` previews the top-level file that holds the file you saved last and builds the whole document when the file saved last is `main.tex` or anything but a `.tex` file (a class, a bibliography, a figure). Limits:

* It shows the state of the last *full* build for everything outside the focus. New labels or citations show as `??` until the next full build.
* Macros defined in a skipped file are missing. The title page, table of contents and lists are skipped. Material typeset by `main.tex` itself between chapters is not in the preview.
* `out/<name>.focus.pdf` and `.focus.log` are local previews and are never part of CI.

**Measured on `bench/sample-report`** (303 pages, 25 TikZ figures, 4 CPUs):

| Step | before | after |
| --- | --- | --- |
| cold build (`--force`) | 93 s | 65 s |
| one-line text edit | 47 s | 20 s |
| edit one figure | 24 s | 23 s |
| `--focus Chapters/chapter5` | n/a | 1.7 s |

Not adopted, because they did not pay off: a precompiled preamble format (saves about 1.5 s of a 23 s pass), parallel BibTeX (the ten units take 0.2 s together), and splicing separately built chapters into the full PDF (needs a PDF library; `pdfunite` drops the outline and breaks cross-chapter links).

---

## Benchmarks

`bench/` holds local benchmark documents, such as `bench/sample-report` (303 pages, 25 TikZ figures). CI never builds them. Build one with:

```bash
python scripts/build.py --source bench sample-report
```

`--source DIR` (relative to the repository root) selects the documents to build instead of `files/`. The output goes to `out/` as usual, named after the document: `out/sample-report.pdf`. So a document in `bench/` and one in `files/` with the same name share that output file; give them different names. Building one tree never deletes the other tree's outputs.

---

## Lint and CI reports

`scripts/ci_report.py` (standard library only; CI-only apart from local use) reads `out/build-report.json`:

```bash
python scripts/ci_report.py summary                  # markdown table of the last build
python scripts/ci_report.py lint [DOC ...]           # labels, refs, figures, bib checks, overfull boxes, chktex, word counts -> out/lint-report.json
python scripts/ci_report.py lint --strict            # exit 1 on findings
python scripts/ci_report.py lint --update-baseline   # accept current findings in files/<doc>/.lint-baseline
python scripts/ci_report.py lint --grammar           # add LanguageTool findings, see Grammar
python scripts/ci_report.py diff --base REF --out DIR  # latexdiff of changed documents
python scripts/ci_report.py pr-comment [--print]     # sticky PR comment (needs gh and GH_TOKEN)
```

A missing `chktex`, `texcount` or `latexdiff` leaves its column blank. `--overfull-pt` (default 10) sets the overfull-box threshold.

---

## Tests and lint

```bash
python3 -m unittest discover -s tests   # unit tests, standard library only
uvx ruff check scripts tests            # lint: rules in pyproject.toml
```

The [Lint and tests](.github/workflows/lint.yml) workflow runs the unit tests on Ubuntu, Windows and macOS under Python 3.9 and 3.13, ruff (pinned version) and the editor's co-editing check (`node tests/collab_check.mjs scripts/serve_ui/collab.js`) on Ubuntu, and the `requirements.txt` check. It runs when `scripts/`, `tests/` or the Python project files change.

---

## GitHub Actions

[`.github/workflows/build-pdf.yml`](.github/workflows/build-pdf.yml) runs on pull requests and on pushes to the default branch when `files/`, one of the build scripts (`build.py`, `accel.py`, `hints.py`, `publish_release.py`, `ci_report.py`), the workflow, `.github/texlive-packages.txt` or the Python project files change. Editing only `serve.py`, its UI or the tests does not trigger it. A push to a feature branch does not run it: its pull request does. It can also be started manually (`workflow_dispatch`).

* **build** gets TeX Live from a cache (about 12 s) or, on a cache miss, from trimmed apt (about 70 s) while a parallel `texlive-cache` job installs it from tug.org via `zauguin/install-texlive` and saves the cache for the next run (packages: `.github/texlive-packages.txt`), compiles only the documents that changed, and uploads the resulting PDFs and logs as the `pdfs` run artifact. What counts as changed depends on the trigger:
  * Push to the default branch: everything changed since the last fully successful publish. Runs that were skipped or failed get caught up.
  * Pull requests: changed since the base branch.
  * A document is also changed when a changed file is in its recorded `.fls` inputs, restored from `.latex-cache`.
  * Changes to `scripts/` or the workflow, manual runs, and the first publish rebuild every document.
* **Job summary** (every run): a table per document with its status, pages, words, build time, LaTeX warnings, chktex warnings and first error. The same table is a comment on the pull request, updated in place on each push, with a link to the run's artifacts. Fork pull requests get no comment, because their token is read-only.
* **latexdiff** (pull requests): each changed document is compared with the base branch, and the diff PDFs are uploaded as the `diff-pdfs` artifact. A failure is noted in the comment and never fails the run.
* **Publish** runs only on the default branch. [`scripts/publish_release.py`](scripts/publish_release.py) syncs the [`pdfs` release](https://github.com/NotTahaAli/LatexPipeline/releases/tag/pdfs):
  * Each rebuilt document's PDF and `<name>.log` replace their older versions.
  * A document that fails keeps its previous PDF, and its `.log` shows why the latest build failed.
  * The PDF and log of a deleted document are removed.
  * The release notes list every document with links to its PDF and log, its pages, and the status of its last build.
  * A file whose bytes the release already holds (same sha256, as GitHub reports it) is not uploaded again.
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
