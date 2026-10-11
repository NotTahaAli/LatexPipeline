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

### Just compiling LaTeX

You only need `files/` and `scripts/build.py`. Everything else (editor, MCP, hosting, tests, `bench/`) is optional and stays out of your way.

1. Click **Use this template** on GitHub, or clone the repository.
2. Put each document in its own folder: `files/<name>/main.tex`, with its `.bib`, `.cls` and figures beside it.
3. Build locally with `python scripts/build.py`, or push to `main`: CI builds every changed document and publishes the PDFs to your own `pdfs` release.

In a copy made from the template, CI runs only the build. The unit tests, ruff and the PDF/A fixture check run only in this repository.

## Contents

* [Requirements](#requirements)
* [Building](#building)
* [Documents and settings](#documents-and-settings): [build.toml keys](#buildtoml-keys), [review markup](#review-markup), [grammar](#grammar), [PDF/A](#pdfa-and-tagged-pdf)
* [Large documents](#large-documents) and [benchmarks](#benchmarks)
* [VS Code](#vs-code)
* [Live preview and editor](#live-preview-and-editor), [sharing](#sharing), [sandboxed builds](#sandboxed-builds)
* [Use with AI clients (MCP)](#use-with-ai-clients-mcp): Claude, ChatGPT, Codex, Cursor, VS Code, Windsurf, Gemini CLI with your own subscription
* [Hosting on a VPS](#hosting-on-a-vps) (accounts, workspaces, sign-in providers)
* [Lint and CI reports](#lint-and-ci-reports)
* [GitHub Actions](#github-actions)
* [Tests and lint](#tests-and-lint)
* [Python project](#python-project-and-requirementstxt)
* [Licence](#licence)

---

## Requirements

* Python 3.9+ (standard library only; Python 3.9 and 3.10 also need `tomli` for `build.toml`, see [build.toml keys](#buildtoml-keys))
* A LaTeX distribution that includes `latexmk`:
  * Windows: [MiKTeX](https://miktex.org/) or [TeX Live](https://www.tug.org/texlive/)
  * macOS: [MacTeX](https://www.tug.org/mactex/)
  * Debian/Ubuntu: `sudo apt install latexmk texlive-latex-extra texlive-plain-generic texlive-fonts-recommended texlive-science texlive-xetex texlive-luatex texlive-extra-utils texlive-bibtex-extra biber chktex latexdiff`
* Optional: [uv](https://docs.astral.sh/uv/); [pandoc](https://pandoc.org/installing.html) for DOCX export

Check that it works with `latexmk --version`. If the command isn't found, add your LaTeX installation to `PATH` and restart the terminal.

### Binaries

No Python? Each version release (`v*` tags, not the `pdfs` release) carries single-file builds of `build.py` and `serve.py` made with PyInstaller by `.github/workflows/binaries.yml`: `build-<platform>` and `serve-<platform>` for `linux-x64`, `macos-arm64` (Apple silicon) and `windows-x64` (`.exe`). They take the same flags as the scripts. LaTeX is still required.

* Run them from the project folder (the one holding `files/`): a binary treats the directory it is started in as the repository, so `out/` and `.latex-cache/` land there.
* On Linux and macOS, make the download executable first: `chmod +x build-linux-x64`. The binaries are not signed, so macOS and Windows may warn on the first start.
* The editor binary loads its libraries from the CDN; `scripts/vendor_ui.py` (offline editor) needs the Python version.

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
python scripts/build.py --format my-report   # rewrite its .tex files with tex-fmt (no build)
python scripts/build.py my-paper --arxiv  # also write out/my-paper.arxiv.zip and check it for arXiv, see "arXiv export"
```

`--format` runs [tex-fmt](https://github.com/WGUNDERWOOD/tex-fmt) (install it with `cargo install tex-fmt`, `brew install tex-fmt` or a binary from its releases page) on every `.tex` file of each selected document, in the document's directory, so a `tex-fmt.toml` there sets the options (wrap length, tab size, extra list or verbatim environments; see tex-fmt's README). It prints the files it changed. Without tex-fmt on `PATH` it prints a message and exits 0. tex-fmt only rewrites text and never runs LaTeX, so it is not sandboxed.

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

After a build (not `--watch`) the script writes `out/build-report.json` with every document it built in that run: `ok`, `seconds`, `engine`, `pages` (`null` if unknown or failed), `errors` (`file`, `line`, `message`), `warnings` (a count of LaTeX warnings), `error` (the message of a crashed build, otherwise `null`) and `phases` (seconds spent on `figures` and `latex`, `null` for a crash). A document built with `tagged = true` also gets `a11y`, a list of accessibility findings (`kind`, `file`, `line`, `message`, `hint`; see "PDF/A and tagged PDF"); other documents have no such key. CI reads this file, so keep its keys stable.

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

`python scripts/build.py --docx [DOC ...]` builds the selected documents, then runs `pandoc main.tex` in each document's directory and writes `out/<name>.docx` (every `.bib` beside `main.tex` becomes a `--citeproc` bibliography). It exports even when the PDF build failed, needs `pandoc` on `PATH` (otherwise it stops with a message), and the files are never published to the release. In the editor, the owner gets "Export DOCX" in the More menu when pandoc is installed. The export is owner-only (the download too) and disabled while sharing, because pandoc reads any file the source names (`\input{../x}`, `\lstinputlisting{/abs/path}`) into the .docx. `--docx` cannot be combined with `--watch` or `--focus`.

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
| `pdfa` | off | PDF/A level, `"2b"` or `"a-2b"` (part 1, 2 or 3 with conformance a, b or u, such as `"3b"`; part 4 as `"4"`, `"4e"` or `"4f"`), see [PDF/A and tagged PDF](#pdfa-and-tagged-pdf). |
| `tagged` | `false` | Tagged PDF (accessibility, PDF/UA-1; UA-2 with PDF/A-4), see [PDF/A and tagged PDF](#pdfa-and-tagged-pdf). |
| `lang` | `"en-US"` | Document language: PDF/A metadata and the grammar checker. |
| `timeout` | `600` | Seconds (10 to 7200) before a latexmk run is killed. `--timeout` changes the default; sharing caps it at 300. |
| `review` | `"off"` | `"show"` or `"clean"`: the [review markup](#review-markup) macros; `"off"` defines none. `--review` overrides it. |
| `grammar` | `"auto"` | `"auto"`, `"off"`, `"local"` or `"public"`, see [Grammar](#grammar). |
| `grammar_url` | `http://localhost:8081` | LanguageTool server for local mode (`LANGUAGETOOL_URL` is the fallback). |
| `disabled_rules` | `[]` | LanguageTool rule ids to ignore. |

```toml
engine = "lualatex"
pdfa = "2b"
timeout = 900
```

### Review markup

Proposed changes can be written into the source and shown in the PDF. With `review = "show"` in `build.toml` (or `build.py --review show`, which overrides `build.toml` for the selected documents and rebuilds them), these are defined:

| Markup | `show` | `clean` |
| --- | --- | --- |
| `\lpadd{new text}` | green background | the text, plain |
| `\lpdel{old text}` | red, struck through | nothing |
| `\lpedit{old}{new}` | both of the above | `new` |
| `\lpnote{text}` | `[text]` in small blue sans serif | nothing |
| `\begin{lpaddblock}...\end{lpaddblock}` | green text (whole paragraphs, lists) | the content, plain |
| `\begin{lpdelblock}...\end{lpdelblock}` | red text | nothing |
| `\begin{lpnoteblock}...\end{lpnoteblock}` | small blue sans serif | nothing |

`clean` ("all accepted") prints the document as if every change were accepted; switch between the two to get a marked-up copy for reviewers and the final text from the same source. `"off"` (the default) injects nothing, so a document without the key builds exactly as before; one that uses the macros then fails with `Undefined control sequence`. The definitions are injected with `latexmk -usepretex` and take effect at `\begin{document}`, in all three engines, in chapter previews and the live preview. If the document (or a package) already defines one of these names, its own definition wins and ours is not used: define them in the preamble to restyle them. `show` loads `xcolor` and `ulem` (with `normalem`, so `\emph` stays italic; a document that loads `ulem` itself keeps its options), both in the CI package lists; `clean` loads nothing. The inline macros cannot span a paragraph break (use the block environments) and, like any `ulem` mark, can break a moving argument or a tagged PDF; in `clean` the block environments `lpdelblock` and `lpnoteblock` need LaTeX 2020-10 or newer.

`build.py --review-pdf DOC` (and More > Build with suggestions in the editor) writes `out/<name>.review.pdf` and `.review.log` with the editor's open suggestions inlined as `\lpdel{old}\lpadd{new}` and its unresolved comments as `\lpnote{Name: text}` (`--no-comments` leaves them out). It copies the document to `.latex-cache/_review/`, edits the copy and builds it like any document with `review = "show"`; the document's own files are never written, and the review PDF is never published to the release. A change is inlined only where the anchored text is still found (as the editor finds it) and is plain prose: in the body of `main.tex` or a `.tex` file without `\documentclass` or `\usepackage`, outside math, verbatim, comments, optional arguments and the arguments of commands other than `\emph`, `\textbf`, `\textit`, `\textsl`, `\textsc`, `\textup`, `\textmd`, `\textrm`, `\textsf` and `\footnote`, with old and new text free of TeX specials (`\%`, `\&`, `\$`, `\#`, `\_` are fine) and blank lines. Everything else is skipped and listed at the top of the log (and in the editor's message), as are suggestions that overlap one taken already; a suggestion made with several cursors goes in whole or not at all. Comment text is escaped (and reduced to ASCII under pdfLaTeX). Inputs outside the document's directory (`\input{../shared/x}`) are not copied, so a document that needs them builds no review PDF.

### Grammar

Grammar and style checks come from [LanguageTool](https://languagetool.org). `scripts/grammar.py` reduces the LaTeX to prose (no comments, commands, math, `verbatim`, `lstlisting`, `minted` or `tikzpicture`; headings, captions, footnotes and the text of `\emph{}` and `\textbf{}` stay) and remembers where every character came from, so a finding is `file:line:col`. `\ref` and `\cite` become a number, math becomes `X`. Spacing and quote rules that misfire on extracted text are ignored, as is the Typography category.

* **Local server** (what `auto` picks when one answers `/v2/languages` within 300 ms): `docker run -d -p 8081:8010 erikvl87/languagetool`, or the LanguageTool zip with `java -cp languagetool-server.jar org.languagetool.server.HTTPServer --port 8081`. URL: `grammar_url` in `build.toml`, else `LANGUAGETOOL_URL`, else `http://localhost:8081`. The text never leaves your machine, and local requests bypass any proxy.
* **Public API** (`grammar = "public"` in `build.toml`, or Settings > Grammar in the editor): **your text is sent to languagetool.org** (`https://api.languagetool.org/v2/check`). It is never chosen automatically. Requests are paragraph batches of at most 15 KB, throttled below the free tier (20 requests and 75 KB a minute), and cached by paragraph, so re-checking an edited file only sends the changed paragraphs.
* `grammar = "off"` or nothing found: no check, no network. The language is `lang` from `build.toml` (default `en-US`).

`python scripts/ci_report.py lint --grammar` adds the findings to the lint report, annotations and `.lint-baseline` like the other lint findings (as notices; a server that cannot be reached is skipped with a message). The workflow does not pass `--grammar`. In GitHub Actions `grammar = "public"` is ignored unless `GRAMMAR_PUBLIC_OK=1` is set too.

### arXiv export

`python scripts/build.py --arxiv [DOC ...]` builds the selected documents (also when only the cache is missing), then writes `out/<name>.arxiv.zip` for upload to arXiv, which takes `.zip` and `.tar.gz` alike. The archive holds exactly the files the last build read from the document directory (from its `.fls`; nothing outside it, no unused figures, no `build.toml`), plus the generated `main.bbl` (and `.ind`, `.gls`, `.nls`, which arXiv does not make itself). `%` comments are stripped from `.tex` files: a comment line goes, a comment after text leaves a bare `%` (so the end of line still makes no space), and `\%`, verbatim-like environments (`verbatim`, `lstlisting`, `minted`, `comment`, ...), `\verb`, `\lstinline`, `\url`, `\href` and `\path` are kept as they are; a file that changes `\catcode` is copied unchanged, and `\iffalse ... \fi` blocks are left in. With `engine = "xelatex"` the archive gets a `00README.json` selecting XeLaTeX ([format](https://info.arxiv.org/help/00README.html)); arXiv has no LuaLaTeX.

A preflight prints each problem with a fix hint and writes it to the end of `out/<name>.log` (`===== arXiv export =====`): files the build read outside the document directory, file names with spaces, non-ASCII or special characters, hidden files (arXiv deletes them at announcement), absolute paths or `..` in `\input`, `\includegraphics` and similar, shell escape (`shell_escape`, `\write18`, minted, pythontex, svg), figures not in the archive, EPS figures converted during the build, PNG or JPEG images over 34 megapixels (arXiv states no byte limit, it warns above that), a biblatex `.bbl` format other than 3.3 (TeX Live 2025, arXiv's default; 3.2 works only if you select TeX Live 2023 at submission), LuaLaTeX, and a note that `pdfa` / `tagged` come from `build.toml` and need a `\DocumentMetadata` line in `main.tex` on arXiv. The command exits non-zero when an export failed or a preflight check is an error; documents whose build failed are not exported. It runs no LaTeX (it reads the cache), is never run in CI, and the archive is never published to the release. In the editor, the owner gets "Export for arXiv" in the More menu, which exports from the last build and downloads the archive; it is owner-only and disabled while sharing, like DOCX export.

### PDF/A and tagged PDF

`pdfa = "2b"` (also `"a-2b"`, `"3b"`, `"4f"`, ...) injects `\DocumentMetadata{pdfstandard=a-2b,lang=...}` before `\documentclass` through latexmk's `-usepretex`, so remove any `\DocumentMetadata` from `main.tex`. It needs LaTeX 2023-06 or newer; PDF/A-4 (`"4"`, `"4e"`, `"4f"`, PDF 2.0) needs the 2025-06-01 kernel and otherwise prints `WARNING: pdfa = a-4f needs LaTeX 2025-06-01 or newer; not applied.` into the log. The same pretex also asks xcolor for RGB output (cmyk colours break PDF/A against the RGB OutputIntent), loads pdfTeX's glyph-to-Unicode maps (including `glyphtounicode-cmr` for math symbols, needed by the `u` level) and, for PDF/A-1, turns off object streams. After the build the PDF is checked for the XMP `pdfaid` declaration and an OutputIntent; if [veraPDF](https://verapdf.org) (`verapdf`) is on `PATH` the PDF is then validated against the requested level and the log note says passed, or which rules failed. The editor's builds skip veraPDF (it takes seconds); CLI builds run it, except `--focus`. Images embedded as CMYK and transparency in included PDFs are not converted, and the `a` levels need tagging (`tagged = true`, below).

`tagged = true` adds a structure tree (headings, lists, links, a language) for screen readers and PDF/UA. It goes into the same `\DocumentMetadata` line, one `-usepretex` argument, and combines with `pdfa`; with `pdfa = "2a"` (the accessible level) the PDF/A-2a rules are met too. The key depends on the LaTeX kernel, which the pretex tests itself with `\IfFormatAtLeastTF`, so one setting works everywhere: kernel 2025-06-01 or newer gets `tagging=on`; 2023-11-01 up to that gets `testphase={phase-III,firstaid}` (TeX Live 2023 to 2025); an older kernel prints `WARNING: tagged = true needs LaTeX 2023-11-01 or newer` into the log and builds an untagged PDF. The declared standards are exactly the ones veraPDF validates:

| `build.toml` | kernel 2025-06-01 or newer declares | veraPDF checks | TeX Live 2023 to 2025 declares |
|---|---|---|---|
| `tagged` | `pdfstandard=ua-1` | `ua1` | `pdfstandard=ua-1` |
| `tagged` + `pdfa` 1, 2 or 3 | `pdfstandard={a-2a,ua-1}` (LaTeX News 41) | the PDF/A level and `ua1` | `pdfstandard=a-2a`, no PDF/UA (the note says PDF/UA is not validated) |
| `tagged` + `pdfa` 4 | `pdfstandard={a-4f,ua-2}` (PDF 2.0) | the PDF/A level and `ua2` | not available, warning |

After the build the PDF is checked for a `/StructTreeRoot` (the note says if it is missing); veraPDF then validates the PDF/UA part that the XMP (`pdfuaid:part`) declares, so a file that carries no identification is not judged against UA. The pretex also asks hyperref for `pdfdisplaydoctitle`; set the title yourself with `\hypersetup{pdftitle={...}}`, which PDF/UA requires. Two workarounds ride in the tagged pretex. PDF/A-1, A-2 and A-4 without `f` forbid embedded files that are not PDF/A, but the tagging kernel attaches the TeX source of formulas and two CSS files, so for those levels `\tagpdfsetup{attach-css=false,math/tex/AF=false}` is set (each key only if tagpdf has it; A-3 and A-4f allow the files and keep them). With pdfTeX, tagging writes a real interword space glyph from the `pdftexspace` font; in TeX Live 2023 its metrics (`pdftexspace.tfm` 0.334249, the font program 333) disagree and veraPDF fails "glyph width information ... shall be consistent". The pretex measures the font and, only if it is too wide, selects the old `dummy-space` font with `\pdfspacefont` (pdfTeX 1.40.25 or newer), which keeps the spaces in text extraction; a TeX Live tree with the corrected metrics (2026 here) keeps the default font.

Checked with veraPDF 1.28.2 on a small document (title with `pdftitle`, bold, link, list, equation; `tests/fixtures/tagged`) and, where it builds, `files/test`, built by `build.py` with each of the three engines:

| | TeX Live 2026 (kernel 2026-06-01; pdfTeX 1.40.29, LuaHBTeX 1.24.0, XeTeX 0.999998) | TeX Live 2023 (Debian; kernel 2023-11-01, pdfTeX 1.40.25) |
|---|---|---|
| `tagged` (UA-1) | pdfLaTeX, LuaLaTeX, XeLaTeX pass | pass (pdfLaTeX only with the space-font workaround; `files/test` needs a `pdftitle`) |
| `tagged` + `2a`, `2b`, `2u`, `3a`, `3b` | PDF/A and UA-1 pass, all engines | PDF/A passes, all engines; no PDF/UA identification |
| `tagged` + `4f`, `4` | PDF/A-4 and UA-2 pass with pdfLaTeX and LuaLaTeX; XeLaTeX fails A-4 (xdvipdfmx writes an Info dictionary) | warning, untagged |
| `pdfa` only: `1b`, `2b`, `2u`, `3b` | pass, all engines | pass, all engines except `1b` with XeLaTeX ("Xref streams shall not be used") |
| `pdfa` only: `4f`, `4` | pdfLaTeX and LuaLaTeX pass, XeLaTeX fails (Info dictionary) | warning, no PDF/A |

`pdfstandard=ua-2` alone (no `build.toml` key; `tagged` stays UA-1) also passes `ua2` with all three engines on TeX Live 2026. Limits: tagging is still maturing; classes and packages that are not tagging-ready can produce an incomplete tree, and tagging slows the LaTeX run. Any `\DocumentMetadata`, so `pdfa` too, makes titlesec stop on the TeX Live 2026 kernel (`Package titlesec Error: No format for this command` at `\begin{thebibliography}`); `bench/sample-report` (class with titlesec) therefore builds with `pdfa = "2b"` (veraPDF passes) only on TeX Live 2023, and with `tagged` on neither (TeX Live 2023 stops in cleveref). Whether formulas get MathML depends on the engine and packages and was not checked. CI builds the four fixtures in `tests/fixtures/tagged` on the tug.org TeX Live and fails the `tagged-pdf` job if veraPDF (pinned 1.28.2 jar from Maven Central, checked by SHA-256) does not pass the standards each declares; `python3 tests/tagged_check.py` does the same locally.

**Accessibility checks.** After a successful build with `tagged = true`, `scripts/a11y.py` checks the PDF and the sources (`main.tex`, the files it `\input`s or `\include`s and local `.sty`/`.cls` files, comments ignored) and reports four things, each with a hint: a missing title (the XMP `dc:title`, else the Info `/Title`, is empty: set `pdftitle`), `\includegraphics` without the kernel's `alt=` key (or `artifact` for decorative images, or `actualtext`), skipped heading levels (`\section` followed by `\subsubsection`), and packages or classes the [LaTeX Tagging Project](https://latex3.github.io/tagging-project/tagging-status/) lists as currently incompatible or unsupported (a curated list of common ones, such as caption, float, listings, multirow, titlesec, wrapfig and beamer, taken from that page on 2026-10-11; nothing is fetched during a build). They run only for tagged documents: an untagged PDF carries no structure for alt text or headings to land in, and warning every document about them would bury the warnings that matter. Findings are lines starting with `ACCESSIBILITY:` at the top of `out/<name>.log`, a count in the build summary, the `a11y` key of `build-report.json`, an Accessibility filter in the editor's Warnings tab, and notices (never failures, also with `--strict`) in `ci_report.py lint`.

`build-report.json` and the CI summary table list each PDF's size. A `qpdf --object-streams=generate` pass was measured and dropped: pdfTeX already writes object streams, and it saved 0.3% (`files/test`, 101 KB) to 0.9% (`bench/sample-report`, 1.6 MB).

---

## VS Code

[`.vscode/tasks.json`](.vscode/tasks.json) has the build commands as tasks (Terminal, Run Task):

* **LaTeX: build changed documents** (the default build task): documents whose PDF is missing or out of date.
* **LaTeX: build one document**: asks for a name or glob, and builds it even when up to date.
* **LaTeX: build all (--force)**, **LaTeX: watch** (rebuilds on save), **LaTeX: list documents**, **LaTeX: clean**.

Errors from the build appear in the Problems panel. [`.vscode/settings.json`](.vscode/settings.json) makes LaTeX Workshop build through `build.py`. Its PDF viewer can't read `out/<doc>.pdf`, so use the live preview below or an external viewer.

### VS Code extension

[`vscode/`](vscode/) is a small extension (plain JavaScript, no dependencies, no build step) that edits a document served by `serve.py` or a hosted project from inside VS Code. It is not on the Marketplace: in `vscode/`, run `npx @vscode/vsce package` (makes `latex-pipeline-0.1.0.vsix`), then in VS Code "Extensions: Install from VSIX...". It needs VS Code 1.82 or newer (its extension host has `fetch`).

Run **LaTeX Pipeline: Connect** and paste the address you would open in a browser:

* `http://localhost:8000/`: your own local editor (the owner, no sign-in).
* A share link (`https://...?token=...`): the extension exchanges the token for the editor's cookie once and keeps that in VS Code's secret storage; you get the link's role.
* `https://<server>/p/<project>/`: a hosted project; it asks for a personal API token (Account > API tokens on the hosted server, see [API tokens](#api-tokens)).

Plain `http://` is accepted only for this computer. The document then appears as a folder in the Explorer (`latexpipeline:` files): opening, saving, creating, renaming and deleting go through the editor's API, so the server's rules apply (a view link cannot save, build-config files stay owner-only, every save is a version in History). A save sends the version the file was read at; if someone saved it since (or the file is open in a co-editing room in the browser and changed there), the save is refused instead of overwriting, and VS Code says so: reopen the file and save again. **Build** asks the server to build (edit role); the status bar shows the build state, build errors and the log's warnings land in the Problems panel, and **Show the PDF** opens it in a tab (pdf.js from the same pinned CDN URL as the browser editor), refreshed after each good build. **Open a file** picks one by name. **Disconnect** forgets the address and the token. Changes made elsewhere (the browser, other people) arrive over the editor's long-poll and refresh the Explorer.

---

## Live preview and editor

`python scripts/serve.py [DOC ...] [--port 8000] [--no-open] [--source DIR] [--editor vscode]` starts an editor and live preview at http://localhost:8000, bound to 127.0.0.1 only. It rebuilds on save, keeps scroll and zoom, lists errors with editor links, and double-click in the PDF jumps to the source. It needs the synctex CLI from TeX Live, and internet for PDF.js, CodeMirror, KaTeX and Yjs from pinned CDN versions, unless you set up offline mode. Its fonts (Newsreader, Source Sans 3, IBM Plex Mono; SIL OFL) ship in `scripts/fonts/` and are served from `/fonts/`.

**Offline mode:** `python scripts/vendor_ui.py` downloads those libraries (about 3.4 MB, the URLs pinned in the import map of `scripts/serve_ui/index.html`) into `scripts/serve_ui/vendor/`, which is git-ignored. While that directory exists, `serve.py` serves only the files listed in its `manifest.json` and its Content-Security-Policy allows no CDN. Delete the directory to go back to the CDNs; re-run the script after bumping a pinned version.

**Finding your way.** The first visit shows a one-line quick start (Outline, Visual, Ctrl+K, double-click the PDF); "Got it" hides it for good and More > "Show getting-started tips" brings it back. The Outline lists sections with word counts; hover a row for the target button to set a per-section word goal (stored in this browser; the row fills as you write). While a build runs the top bar counts the seconds and a bar fills against the previous build's time, the empty PDF pane shows a page skeleton instead of a blank, and after a failed build the old PDF stays with a "last good PDF" note. The failing line is marked in the editor and the first error opens by itself with its hint. Fit width follows the pane until you zoom by hand. Visual mode sets prose and headings in the serif (Newsreader) and leftover LaTeX in monospace, in both themes. PDF pages stay white unless you turn on dark pages (the moon button in the PDF toolbar, Settings > PDF preview, or "PDF: dark pages" in Ctrl+K; off by default, remembered in this browser), which inverts the rendered pages and turns the hues back, so figures keep roughly their colours. The page is keyboard operable (dialogs trap focus and return it, the build result is announced to screen readers) and passes axe-core in light and dark. The More menu is grouped (Panels, Build, Document, Session, Help); on a phone the error and warning badges become one count chip, and a Source / Split / PDF bar at the bottom picks which pane shows. A view link shows a "View only" chip, and Rebuild and Paragraph stay visible but disabled, with the reason as their tooltip; a review link shows a "Reviewer" chip and opens in Suggest mode, locked on.

**Writing LaTeX.** In `.tex`, `.sty`, `.cls`, `.ltx` and `.tikz` files:

* Folding: the gutter arrow (or Ctrl+Shift+[ and Ctrl+Shift+], Cmd+Alt+[ and ] on a Mac; Ctrl+Alt+[ and ] for all) folds a section to the next heading of the same or a higher level (`\part` to `\subparagraph`), an environment to its `\end`, the preamble, and `% region` ... `% endregion` blocks.
* Hover cards: math (`$...$`, `\(...\)`, `\[...\]`, `$$...$$`, `equation`, `align` and friends) is shown rendered with KaTeX in source mode; a `\cite` key shows the entry's author, year and title, a `\ref` label the line it was set on.
* Code check: unmatched `\begin`/`\end`, unbalanced braces, `\left` without `\right` and unclosed `$`, `$$`, `\(` and `\[` (also math running across a blank line) are underlined 0.6 s after you stop typing; Ctrl+Shift+M lists them, F8 jumps to the next. Comments, `verbatim`, `lstlisting`, `minted`, `comment`, `\verb`, URLs and the bodies of `\newcommand`, `\def` and friends are skipped. A line `%%novalidate` turns it off for the file, and `%%begin novalidate` ... `%%end novalidate` for a region; Settings > Editor turns it off everywhere. It is a quick structural check, not a LaTeX run: the build log stays the authority.
* Completion also offers the macros and environments the document defines (`\newcommand`, `\renewcommand`, `\DeclareMathOperator`, `\def`, `\NewDocumentCommand`, `\newenvironment`, `\NewDocumentEnvironment`, in any `.tex`, `.sty` or `.cls` of its folder), and `\cite{` and `\ref{` match any part of the key, author or title (every word you type must appear).

**Build results.** "Up to date" after a restart still shows the pages, build time and warnings of the last build (read back from `out/`). The Problems panel has a Warnings tab parsed from the LaTeX log: undefined references and citations, overfull and underfull boxes, package warnings and BibTeX warnings, filterable by kind, each with a plain-language hint where `scripts/hints.py` explains it and a link to its source line.

**Chapters, files and figures.** "Preview chapter" (status bar, or the command palette) builds only the chapter you are editing with `build.build_focus`, keeping the numbering and references of the full document, and shows it with a "Chapter preview" bar and a "Back to full PDF" button; Settings can build it after each save. Jump-to-PDF is off while it is shown. The first preview of a document builds the whole thing once. The Files tab has new file, new folder, rename/move (F2) and delete (Delete key, with a confirmation); `main.tex`, dotfiles and, over a share link, build-config files cannot be touched, and open co-editing rooms of a renamed or deleted file are closed. Drop or paste a png, jpg or pdf into the editor and a figure dialog asks for the file name, folder (`figures/` or `Figures/` when one exists; a new folder is created), caption, label (`fig:<name>`), width and placement, then saves it and inserts a whole `figure` environment (or only `\includegraphics`, if you untick it); Skip leaves the image out. Up to 8 MB, content checked, svg refused. Spell check (the browser's own, no dictionary shipped) underlines misspelled words in visual mode and the paragraph panel only, never LaTeX commands or keys; Settings turns it off. Shared links: view cannot start previews or change files, edit can for the shared document only.

**Search and replace in all files.** The sidebar's Search tab (Ctrl+Shift+F, which takes the selected text) searches every text file of the document: literal text, with Aa (match case) and Word (whole words); the owner also gets `.*`, a Python regular expression whose replacement may use `\1` or `\g<name>` (guests never, since a pattern can keep the server busy). Results are grouped by file with a line preview; click one to open the file with the match selected. It reads the files on disk, so text typed since the last autosave shows up after the next save. Replace (one match) and Replace all need the edit role: files nobody has open are written by the server with a version in History, and files open in live co-editing are changed through the shared document by your editor (it joins the room if needed), so nobody's typing is overwritten; a file whose room is busy with an input-method composition is skipped and named. At most 500 matches are listed (replacing has no such cap), files over 1 MB are skipped, and over a share link `build.toml` and `latexmkrc` files are neither searched nor changed. Guests may search 60 times and replace 60 times a minute.

**Editing tools.** In the source editor (`scripts/serve_ui/edittools.js`, text logic in `edits.js`, checked by `node tests/edits_check.mjs`):

* Figures and paths: hovering `\includegraphics{...}` shows a thumbnail of a png, jpg, gif, svg or webp figure (read through the view-role image endpoint; PDF and EPS figures only say so). Inside `\includegraphics{`, `\input{`, `\include{`, `\includeonly{`, `\subfile{`, `\bibliography{` and `\addbibresource{` completion lists the document's matching files (images; `.tex` without the extension; `.bib`).
* Glossaries: `\gls{`, `\Gls{`, `\glspl{`, `\glstext{` and the rest of the `\gls...` family, `\acrshort{`, `\acrlong{`, `\acrfull{` (and capitalised forms) complete the keys of `\newglossaryentry`, `\longnewglossaryentry`, `\newacronym` and `\newabbreviation` in any `.tex`, `.sty` or `.cls` of the folder, matched by key or text.
* Bold and emphasis: Ctrl+B (Cmd+B) toggles `\textbf{...}` and Ctrl+I toggles `\emph{...}` around the selection, or removes it when the cursor is inside one (outside the editor Ctrl+B still toggles the file list). Ctrl+K > "Wrap selection in environment..." or "...in command..." wraps it in `\begin{name}`/`\end{name}` or `\name{...}`.
* Structural edits (Ctrl+K): "Change surrounding environment..." renames both `\begin` and `\end` of the innermost environment around the cursor; "Delete surrounding environment (keep content)" removes `\begin{x}[...]` and `\end{x}` (and lines left blank); "Delete surrounding command (keep content)" turns `\footnote{text}` into `text`; "Toggle starred environment" switches `figure` and `figure*`; "Toggle \left / \right" removes them from the pair around the cursor, or adds them to the innermost `( )`, `[ ]` or `\{ \}` pair.
* Math shortcuts (Settings > Editing, off by default): in math mode `@` and a key types a symbol, after LaTeX Workshop's table: `@a` `\alpha`, `@b` `\beta`, `@c` `\chi`, `@d` `\delta`, `@e` `\epsilon`, `@ve` `\varepsilon`, `@f` `\phi`, `@vf` `\varphi`, `@g` `\gamma`, `@h` `\eta`, `@i` `\iota`, `@k` `\kappa`, `@l` `\lambda`, `@m` `\mu`, `@n` `\nu`, `@p` `\pi`, `@vp` `\varpi`, `@q` `\theta`, `@vq` `\vartheta`, `@r` `\rho`, `@vr` `\varrho`, `@s` `\sigma`, `@vs` `\varsigma`, `@t` `\tau`, `@u` `\upsilon`, `@o` `\omega`, `@x` `\xi`, `@y` `\psi`, `@z` `\zeta`; capitals `@D` `@F` `@G` `@Q` `@L` `@P` `@X` `@Y` `@S` `@U` `@W` for `\Delta` `\Phi` `\Gamma` `\Theta` `\Lambda` `\Pi` `\Xi` `\Psi` `\Sigma` `\Upsilon` `\Omega`; and `@.` `\cdot`, `@8` `\infty`, `@6` `\partial`, `@@` `\circ`, `@0` `^\circ`, `@=` `\equiv`, `@*` `\times`, `@<` `\leq`, `@>` `\geq`, `@+` `\bigcup`, `@-` `\bigcap`, `@&` `\wedge`, `@,` `\nonumber`. Math mode is `$...$`, `$$...$$`, `\(...\)`, `\[...\]` and the display environments, but not `\text{...}` inside them.
* Snippets: type a trigger word and press Tab (or Ctrl+Space for the list); Tab and Shift+Tab move between the tab stops, Esc leaves them. Settings > Editing > "Edit snippets..." (or Ctrl+K) edits them: a trigger (a word) and a body where `${1:default}` or `${1}` marks a tab stop (`${` always starts one; a default cannot hold `}`). They are kept in this browser; the defaults are `fig` (figure), `eq` (equation), `itm` (itemize) and `sec` (section with label).
* Unicode on paste: when pasted text has characters LaTeX spells differently, a message offers **Convert to LaTeX**: curly quotes become ``` `` ``` and `''`, dashes `--` and `---`, the ellipsis `\dots{}`, a no-break space `~`; for pdflatex documents (not xelatex or lualatex, which read UTF-8) also accented letters (`é` `\'e`, `ç` `\c{c}`) and `ß`, `æ`, `ø` and friends; pasted into math, Greek letters and symbols (`α` `\alpha`, `≤` `\leq`, `ℝ` `\mathbb{R}`). Settings > Editing turns the offer off.
* Lines: Settings > Editing (or Ctrl+K) switches soft wrap and relative line numbers; Alt+Q ("Rewrap paragraph") refills the paragraph around the cursor to the width set there (80 columns by default), keeping its first line's indent, and leaves paragraphs with comments, environments, `\item`, headings or `\verb` alone.
* Zen mode: More > Document or Ctrl+K > "Zen mode" hides everything but the editor, in a centred column; Esc (when nothing in the editor uses it) or the "Exit zen mode" button leaves it.
* `.editorconfig`: a `.editorconfig` in the document folder sets the indent (`indent_style`, `indent_size`, `tab_width`) per file pattern (`*`, `**`, `?`, `[...]`, `{a,b}`), and on an explicit save (Ctrl+S) `trim_trailing_whitespace` and `insert_final_newline` (lines with a cursor keep their trailing spaces; autosave changes nothing). Files outside the folder are not read.

Every tool changes the text the way typing does: with the view role it does nothing, in suggest mode (always on for reviewers) the change becomes a suggestion, and co-editing and undo see one edit.

**Paste a table.** Copy cells from a spreadsheet, Word or a web page and paste: the text goes in as usual, and when the clipboard held a table (an HTML `<table>`, or tab-separated text of at least 2 rows and 2 columns) a message offers **Paste as table**, which swaps it for a `table` with a `tabular` (first row as header, `\toprule`/`\midrule`/`\bottomrule` when the document loads `booktabs`, else `\hline`), with `& % $ # _ { } ~ ^ \` escaped. The HTML is only read for cell text, never shown.

**Tables, TikZ pictures and Markdown.** With the cursor in a `tabular`, `tabularx` or `longtable`, the status bar offers **Edit table** and **Align &** (also in the command palette). Edit table opens the table as a grid: add, remove and move rows and columns, edit cells (they stay LaTeX source, so `\multicolumn` or a comment is kept as typed), set each column's alignment or edit the column spec, and switch the rules to booktabs (`\toprule`, `\midrule`, `\bottomrule`) or `\hline`; Apply writes the table back as one edit (refused if the table changed in the editor meanwhile). Align & pads the columns in the source so the `&` line up. With the cursor in a `tikzpicture`, **Edit picture** draws it on a canvas: `\node`s with coordinates or relative placement (`right=of a`, `below of=b`), `\coordinate`s and `\draw`/`\path` lines and arrows (`--`, `to`, `-|`, `|-`, a `node` label, `cycle`) between nodes and points. Drag nodes and path points (snapped to a 0.25, 0.5 or 1 cm grid, or free), add nodes, connect two nodes with an arrow, delete (paths through a deleted node go too), and edit names (references follow), labels, options and arrows; the keyboard moves the selected node with the arrow keys and Delete removes it. Apply rewrites only the statements you changed; everything else (styles, `\foreach`, scopes, comments, polar or computed coordinates) stays as written and in order. The canvas is a sketch, not TeX: the build shows the real picture. `.md` files in the document folder open with Markdown highlighting and a rendered preview beside the editor (built from DOM nodes and text, never as HTML; links only to http, https, mailto or relative paths; "Markdown preview on/off" in the palette), and **Insert Markdown selection as LaTeX** turns selected Markdown (headings, bold, italics, code, lists, links, quotes, code blocks) into LaTeX in place, with special characters escaped (links need `hyperref`). These are edits like typing: they need the edit role, and a reviewer gets them as suggestions.

**Live preview.** While you type in a chapter file, the PDF follows: 0.7 s after you pause, the chapter around the cursor is typeset from the editor's text (saved or not) and its pages replace that chapter's pages in the PDF pane, in place, without a blank and without moving your scroll position; the status line in the PDF toolbar says "Live: 0.8 s" (or the error, with the line in its tooltip). It uses `--focus`, so numbering, references and citations come from the last full build, and a TeX process that has already loaded the preamble is kept waiting (`scripts/preview.py`), so only the chapter itself is typeset: about 0.7 to 0.8 s for a chapter of `bench/sample-report` against 2.0 to 3.1 s for "Preview chapter" (numbers in [docs/benchmarks.md](docs/benchmarks.md#live-preview)). TikZ figures come from the full build when no picture of the chapter changed since. A keystroke cancels the preview in flight. Saving still runs the full build, and its PDF replaces the preview unless you typed again after that build started. Settings > "Live preview" turns it off; it needs the edit role and works with pdfLaTeX, XeLaTeX and LuaLaTeX. What else follows the typing: labels the chapter defines and citations of keys the full build never cited resolve in the same preview (a second pass on another waiting process reads the first pass's labels; BibTeX runs in the sandbox on the full build's citations plus the new ones, then a third pass prints the numbers; with biblatex and biber, biber runs the same way on the run's `.bcf` with the full build's keys added, and the second pass prints them; the status tooltip says how many passes it took). `\include`d chapters and `.tex` files a package reads (`\InputIfFileExists`, once you previewed the chapter that reads them) are typeset from the editor's text too. Typing in `main.tex` between two chapters previews that part (between the end of the chapter before and the start of the next); adding, removing or changing an `\input`/`\include` line there, or editing the preamble, a local `.cls`/`.sty` or a file the preamble reads, pauses the live preview with "Live: rebuilding preamble" (or "... changed, rebuilds on save" before the autosave) until the full build that follows the save has finished: the old pages are taken away, never left standing. The preview's pages are placed in the full PDF from the number of pages the full build had shipped when the chapter started and ended (recorded in the `.focusmap`), so the splice works without hyperref; double-click on a preview page jumps to the source like on any other page (the preview runs with SyncTeX, and its overlay of your unsaved text maps back to the file you are typing in). While previews are in use (and for a minute after the last one), full builds and figure jobs run below normal priority (`nice` 10 on Linux and macOS, which Linux also applies to their disk access; below normal priority class on Windows), so the build of the autosave does not slow the preview down. Limits: page numbers after the chapter do not move; forward search lands on the chapter's first page while a preview is shown. The waiting process is restarted when `main.tex`'s preamble, a file it `\input`s, a local `.cls`/`.sty`, `build.toml`, the engine or the sharing and sandbox settings change, and stopped after 10 minutes unused. How this compares with TeXpresso and other live engines: [docs/realtime.md](docs/realtime.md).

**Bib lookup.** A Lint finding "@article 'k' lacks ..." shows a Look up button (on hover or focus; always on touch screens) for editors with the edit role. It asks Crossref (`api.crossref.org`) using the entry's DOI, or its title when there is no DOI and one record matches closely (title similarity >= 0.9 and a year within one); otherwise nothing is suggested. Only the DOI or title is sent. You see the fields it would add, and Apply inserts just those into the open `.bib` (existing fields are never replaced, the entry's indentation and style are kept, other entries stay byte-for-byte), which then autosaves like any edit. Check the values against the paper.

**References.** The sidebar's References tab (or "Toggle references" in Ctrl+K) lists every entry of every `.bib` in the document folder, one line each (author, year, title); click a row for all its fields and the places it is cited. Switch between all files and one file (files no source names are marked "not used by the document"), filter by key, author, title or year, sort by author, year, title, key, citation count or file. Badges: cited (count), unused, duplicate key across files, and missing required fields (the lint rule), which also gets the Look up button above. Keys cited in the text but defined in no `.bib` are listed as missing, each with its `\cite` locations and "Create entry". With the edit role: New reference (+) opens a form with the type's required fields first and the rest under "More fields" (add or remove any field; the key fills in as AuthorYearWord, unique, until you type one); Edit and Delete (with a warning when the entry is cited) sit in each entry, Delete under its "..." menu; More (...) > "Import BibTeX or DOI" reads pasted entries (validated by the same parser) or asks Crossref for a DOI (only the DOI is sent; same limits as Look up). "cite" on a row, or ticking several rows and "Insert \cite{a,b}", inserts at the cursor of the `.tex` you were last in (into the `\cite{...}` the cursor is in, if any). Changes go into the open `.bib` in the editor as one splice that keeps every other byte, so co-editing and autosave work as for typing; if the file changed since the list was read, nothing is applied and the list reloads. The view role sees the list read-only.

**Verify references.** References > More (...) > "Verify with Crossref" (edit role) checks the entries shown (all files or the one selected) one by one and puts a badge on each: verified, mismatch (the DOI's record has another title, or the year is off by more than one, or another first author; hover for which) or not found (the DOI does not resolve, or no record has a close title). It uses the rule and limits of `ci_report.py lint --verify-citations` and Look up's rate limits (a shared link: 10 lookups a minute for all guests together); when it hits one it stops and says so, and running it again continues with the entries not yet checked. Results last until the page reloads; an edited entry is checked again. Only the DOI, title and first author's surname are sent, and nothing is changed.

**Zotero and Mendeley.** In the References panel, More (...) > "Zotero settings..." (the owner of a local editor; hosted, each person's Account page, see "Zotero" under "Hosting on a VPS") takes a personal or group library ID, an optional collection key (8 characters), BibTeX or BibLaTeX, and an API key made at zotero.org/settings/keys/new (read-only is enough). The key is kept outside the project: in the environment variable `ZOTERO_API_KEY` (wins if set) or in `zotero.json` under your config directory (`$XDG_CONFIG_HOME` or `~/.config`, `~/Library/Application Support`, `%APPDATA%`, in a `latex-pipeline` folder; mode 600). It is sent only in the `Zotero-API-Key` header to `api.zotero.org`, never put into the project, a reply, the co-editing bus or a log, and the settings dialog never shows it back. "Sync from Zotero..." fetches `items/top` as BibTeX (100 per request, `If-Modified-Since-Version` so an unchanged library answers 304; at most 120 requests a minute, waiting rather than failing, no redirects followed) and compares it with the `.bib` you pick ("Add to and update"), unsaved edits included, and with every other `.bib` of the document: an entry already in another file (same key, DOI or title) is listed under "Already in other files" and not added again (choose that file to update it). The result: New in Zotero (ticked), Changed in Zotero (unticked, with each field's old and new value; only differing fields are replaced and fields Zotero does not export stay), and the entries only your file has (never deleted). Entries are matched by key, then by DOI or title, so your own keys survive; a Zotero key that your file or another `.bib` uses for a different work is added under a free key (`smith20a`). Zotero's `file` field (attachment paths) is left out. Apply puts one splice into the open editor document like every other References edit, so co-editing stays consistent and the rest of the file stays byte-for-byte. Better BibTeX: choose "Better BibTeX on this computer" to read its pull export from `http://127.0.0.1:23119` (local editor only, never while sharing, no proxy, no key; it keeps your pinned citation keys): My Library, or a group library by its group ID, the whole library (`/better-bibtex/export/library?/<1 or group ID>/library.bibtex`) or one collection by its key or its path such as `Thesis/Chapter 2` (`/better-bibtex/export/collection?/<1 or group ID>/<key or path>.bibtex`), the URLs Better BibTeX's own "Download Better BibTeX export..." gives (checked against its `content/pull-export.ts`; not tested against a running Zotero). While sharing, the owner may tick "Let editors of shared links sync from my Zotero library" (off by default, and only with sandboxed builds, `serve.py --share --sandbox`, like the AI assistant: without the sandbox LuaLaTeX from an edit link could read a stored key from `zotero.json`, and the Share dialog says so): edit links then get "Sync from Zotero..." for the shared document, fetched by this server with the owner's key (never sent to them; they do not see the library settings), 6 previews a minute for all links together; view links never. `ZOTERO_API_KEY` is taken out of the environment when the editor starts, so no build inherits it. Comparing is bounded (titles cut to 300 characters, a work budget per comparison, indexed DOI and title pools): a library of thousands of entries sharing one DOI or title compares in about a second. Mendeley's API is closed to new apps, so there is no live sync: export from Mendeley (or EndNote, a publisher, anything) as BibTeX or RIS and use More > "Import BibTeX, RIS or DOI...", which also takes the export file; RIS is converted to BibTeX with generated keys.

**Grammar.** The Problems panel has a Grammar tab. The open `.tex` file is checked 2 s after you stop typing (the text of the buffer, saved or not); findings are wavy underlines, and hovering one shows the message and up to three replacements to click (no replacement is offered where the span holds commands or math). Settings > Grammar (owner only) picks automatic, off, local or public, the local server URL, and whether the public API may be used while sharing. See [Grammar](#grammar) for what each mode sends where. Public mode asks for confirmation when chosen.

**Format file.** Ctrl+K > "Format file (tex-fmt)" (also More > Document; edit role) sends the open `.tex` file's text, saved or not, to the server, which runs `tex-fmt --stdin` in the document folder (so its `tex-fmt.toml` applies) and returns the result. The editor applies it as one edit covering only the changed part, so undo, co-editing, suggest mode and autosave treat it like typing; the server writes nothing, and if the text changed while tex-fmt ran nothing is applied. Up to 1 MB of text and 20 requests a minute on a shared link. Without tex-fmt on the server's `PATH` you get a message.

**AI assistant.** The sidebar's Assistant tab (or "Toggle AI assistant" and the "AI: ..." commands in Ctrl+K) uses the Anthropic Messages API (`scripts/ai.py`, default model `claude-opus-5-5`; `claude-sonnet-5-5` or `claude-haiku-5-5` in its settings). It is **off until the owner turns it on** in the panel's settings, which asks for confirmation because **the text you ask about is sent to Anthropic** (`api.anthropic.com`): the selection and 2,000 characters around it, the open file and the outline for a question, or the build error, about 40 lines of log and the source around the error line (plus the top of `main.tex`) for Explain. Nothing else is sent, and nothing is sent until you click.

* Selection actions: Rewrite, Shorten, Fix grammar, Translate (into a language you name). "Write LaTeX at cursor" turns a description (a table, an equation, a TikZ figure) into LaTeX. "Ask" answers questions about the open file, with the last six turns as context. "Explain with AI" in a Problems row (or "Explain first error") explains a build error and may propose a fix.
* Every answer is shown as a diff first; **nothing is applied until you click** Replace selection, Insert at cursor or Apply, which goes into the editor like typing (undo, co-editing and autosave as usual). If the text changed in the meantime the change is refused. Answers are plain text, never HTML; proposed fixes must quote text that occurs once in the part of the file the model saw, and may only touch files that were sent. `build.toml`, `latexmkrc` and `.latexmkrc` are never sent and never edited this way, for anyone (a build can print any file name into its log). New LaTeX that looks like it runs programs or writes files (`\write18`, `\directlua`, `luacode`, `\openout`, `\input|`) is dropped, but that filter is best effort: TeX can spell anything indirectly, so read a diff before applying it. The document is treated as data: instructions inside it are not followed.
* The key: `ANTHROPIC_API_KEY` in the environment of `serve.py` (taken out of `os.environ` at start, so builds do not inherit it), or pasted into the settings, which stores it in `~/.config/latex-pipeline/ai.json` (or `$XDG_CONFIG_HOME`), mode 600, outside every project. The server never sends it to a browser or over the co-editing channel. It is still on this computer: without `--sandbox`, code that runs in a build as you (LuaLaTeX from an edit link while sharing) can read that file, or the original environment in `/proc`. The Share dialog says so when a key is set.
* Limits: at most 20 requests a minute for the whole server, replies of at most 16,000 tokens (thinking included), files up to 120,000 characters. Only the three models above are accepted.
* Answers stream: the explanation appears as the model writes it (`POST /api/ai?stream=1`, NDJSON lines; the gateway streams them itself when hosted), and the diff and Apply buttons only once the whole reply has been checked. **Stop** ends the request, and the server closes the connection to Anthropic at once (tokens already generated are still billed). A refused or cut-off answer replaces the partial text with the error. A failed connection is reported, never retried (the first request may already have reached Anthropic); a server without streaming answers with its plain reply, and a tunnel that buffers shows the answer at the end.
* It needs an API key: a Claude subscription cannot be used here. With a subscription, connect Claude to the [MCP server](#use-with-ai-clients-mcp) instead.

**Editing together.** Everyone who opens the same document edits it live (Yjs): you see each other's cursors and selections with names and colours, and a stack of avatars in the top bar opens the list of people. The server only relays the changes. One editor per file, the "leader", saves to disk through the normal atomic save; if the leader leaves, another takes over. If you change a file outside the editor (git, vim) while it is open, the change is merged into the shared text instead of overwriting anyone's typing. If the connection drops, you can keep typing; the edits merge when it returns, over WebSocket or the long-poll fallback.

**Version history.** The Problems panel's History tab lists the versions of the open file (or of all files): every save in the editor makes one (one person's saves of a file merge into one version for 5 minutes), and so do changes made outside the editor (kept just before the editor overwrites them), deletes and renames in the Files tab, and restores. "Name this version" (edit role) saves the whole project under a name, such as "Sent to supervisor". View opens a line diff with the changed words marked: "Changes in this version" or "Compared with now"; for a named version, pick the file. "Restore *file*" puts one file back, and on a named version "Restore all files" puts every file back (files added since are kept). A restore is itself a version, so it can be undone. Files open in live co-editing are restored through the shared document, so everyone in the room sees it at once: the server writes the other files and keeps a pending restore for each open file, which one editor in that file's room claims (once, so two editors never apply it twice) and applies as one edit; if that browser closes first, another editor in the room takes it over, and once no editor has the file open the server writes it, but only if the file is still as it was when the restore was asked for. A restore never reverts newer edits: when the restored part was edited in between, it is skipped with a message and can be restored again from History. History lives in `.latex-history/<document>/` beside `.latex-cache/` (git-ignored, never a build input, not removed by `--clean`): a content-addressed, zlib-compressed blob store deduplicated by SHA-256 and an sqlite index. Older versions thin out by themselves, a little on every save: everything from the last 24 hours stays, then one version per file per hour for 7 days, one per day for 90 days and one per week after that (the newest in each period). Named versions, each file's newest text and the text before a deletion are never thinned, and text no version uses any more is deleted. As a last resort it keeps up to 3000 file versions and 64 MB of compressed text per document by dropping the oldest superseded versions; when nothing else may go, history stops recording rather than forgetting, and the History tab says so. **Clean up** (the owner; hosted, workspace admins) deletes every version older than a week, a month, 3 months or a year (named versions only when ticked; each file's newest version stays), and each named version has Delete. Both ask first and cannot be undone. Diffs look at the first 2000 changed lines and say when they are cut short. Viewers can browse and compare; naming and restoring need the edit role; over a share link `build.toml` and `latexmkrc` files are neither shown nor restored.

**Review: comments and suggestions.** Select text and press Comment (Ctrl+Alt+M) to start a thread; it shows as a highlight with a mark in the gutter (click it to open the thread) and in the Review tab, where you reply, resolve or reopen, and delete your own comments (the local owner may delete any). Suggest (Ctrl+Alt+S) turns on track changes for you: what you type and delete no longer changes the text but becomes a suggestion, shown struck through (deleted) and underlined (inserted); a run of typing is one suggestion, sent after 1.5 s idle or when you move away. Anyone with the edit role can Accept (the change goes into the text through the normal editing path, live for everyone) or Reject; Withdraw for your own; Accept all / Reject all, optionally for this file only. Comments and suggestions are anchored by position plus the quoted text and some context, so they follow edits and are found again after the text moves; if the quoted text is gone the item stays in the list, and an accept whose text changed is not applied and stays open. Viewers read both; commenting and deciding need the edit role. A reviewer (a "can review" named link, or the hosted reviewer role) sits between the two: it comments, replies, resolves, deletes its own comments and suggests (Suggest is always on, so whatever it types becomes a suggestion) and withdraws its own suggestions, but cannot accept or reject, save, change the file tree, rebuild, preview, name or restore versions, or use grammar checks, the assistant or Zotero; the server refuses its co-editing updates, so it sees the live text and who is there but never changes it. Comment text is shown as plain text, never as HTML. Stored per document in `.latex-history/<document>/review.json`. Limits: 1000 threads, 200 comments per thread, 4000 characters per comment, 2000 open suggestions, 20000 characters per suggestion; suggest mode handles typing, deleting and pasting, including input methods that compose text (Chinese, Japanese, Korean, dead keys, phone keyboards: the composed text is kept out of the shared document and the file until the composition ends, then becomes a suggestion; a composition is ended after 5 s, when the editor loses focus or the tab is hidden, because others' edits wait and nothing is saved meanwhile; if the shared text changes underneath it, the composed text is dropped and you type it again). Typing with several cursors (Ctrl+click, Ctrl+D) makes one suggestion with several ranges, shown as "One edit in N places" and accepted or rejected as a whole; if one of its ranges can no longer be found, none of it is applied. In suggest mode Ctrl+Z and Ctrl+Y (Ctrl+Shift+Z) work on your own suggestions only, never on the text: undo shortens or drops the suggestion you are typing, then withdraws your last sent one; redo brings it back. Edits from other people keep arriving while you suggest (the co-editing layer marks them, so they are never mistaken for your typing). **Export...** in the Review tab downloads every thread (open and resolved, with replies, authors, times, file:line and the quoted text) and every open suggestion (old and new text, author) as Markdown or JSON; viewers can export too, and over a share link it holds only what the link can read. More > Build with suggestions (edit role) typesets a copy of the document with the open suggestions marked (green background, red strike-through) and the unresolved comments as notes, and offers it to open or download; viewers can open `pdf/<doc>?review=1` once it exists. What it skips and why is in its message and log; see [Review markup](#review-markup).

**Git.** The sidebar's Git tab (the local owner only; share links never see it, and the server refuses `/api/git*` to them) works on the repository that holds the document's folder, with your own `git` and its configuration (author, credential helper, SSH keys): the branch and how far it is ahead of or behind its upstream, the changed files (click one for its diff; new files show their text), Stage, Unstage and Stage all, a commit message and Commit, Pull (fetch, then fast-forward or merge, never rebase), Push (the first push of a branch sets its upstream; with several remotes it asks which), a branch menu to switch (remote branches included), New branch, and the last 50 commits. Unsaved edits are saved first. A pull that conflicts stops with the conflicts in the files (`<<<<<<<` markers) and says so; nothing is resolved for you: fix the files, stage them and commit, or Abort merge. Git runs as a program with an argument list (no shell), never with an option or path the browser chose: paths must be ones `git status` listed and go after `--`, branch and remote names must be listed ones, the message goes in on stdin, pathspecs are literal; every call has a timeout (30 s, 3 minutes for pull and push), never waits for a password prompt, and runs without `fsmonitor` or the `ext::` transport. Files open in live co-editing: pull, switch and abort rewrite files while no save can land (the server's write lock), and the open rooms then fold the change into the shared text like any edit made outside the editor (the room's leader merges it; its next save would otherwise be refused as out of date). If someone typed in the same lines meanwhile, the merge can fail and the editor says the room's version will replace the file's; Git then shows that as a change.

### Sharing

`python scripts/serve.py --share [auto|local|cloudflared|ngrok|localtunnel|pinggy|localhost.run]` (or Share in the top bar) starts a tunnel and prints two links for the first document: a view link (read-only source, PDF and outline) and an edit link (edit the document's files and rebuild it). `local` gives token links without a tunnel (same network or your own tunnel). `auto` picks the first installed of cloudflared, ngrok, npx localtunnel, then ssh (pinggy, localhost.run); with none installed it tells you what to install. No link works after you stop sharing or quit.

**Links for named people.** The Share dialog can also make a link per person ("Alice", can edit; "Bob", can review; "Reviewer 2", view only): each has its own token, and comments, suggestions, history authors and presence show the name you gave it, set by the server (the person cannot change it, and nothing the browser sends, no header, field or client id, can). People on the anonymous view and edit links pick their own name, which everyone sees marked "(unverified name)" ("Guest (unverified name)" without one). Revoke ends a named link at once: its open tabs are disconnected and its cookie stops working; the other links keep working. "New links" replaces only the anonymous pair. Named links are kept in `share-links.json` in your config folder (`$XDG_CONFIG_HOME` or `~/.config`, in `latex-pipeline`; mode 600, folder 700; never in a document directory) and work again each time you share the same document of the same checkout (a link is bound to the document's name and the full path of its `main.tex`, so a clone elsewhere with a document of the same name does not accept it), until you revoke them; a revoke made from another running editor holds at once. Hosted mode has accounts instead and ignores them.

* Install: [cloudflared](https://developers.cloudflare.com/cloudflare-one/networks/connectors/cloudflare-tunnel/downloads/) (no account, the default), [ngrok](https://ngrok.com/download) (free account and `ngrok config add-authtoken`), Node.js for localtunnel, or ssh for pinggy and localhost.run. The ssh and ngrok parsers follow each tool's documented output but are untested here; if one fails, please report its output.
* ngrok's free plan shows a warning page on the first visit; each visitor clicks Visit Site once. localtunnel may ask for a tunnel password (your public IP).
* Cloudflare quick tunnels do not support Server-Sent Events; the editor uses WebSocket with a long-poll fallback, so it works. `python scripts/serve.py --share-selftest [--share PROVIDER]` starts the tunnel and checks `/api/health`, a WebSocket echo and a 30 s long-poll through the public URL, then prints a table. Run it on your own machine before a session; it needs outbound access that restricted networks do not give.

**Security while sharing:**

* Every request needs a token, loopback included. A tunnel connects to the server from 127.0.0.1, so "local" proves nothing; your own browser gets a private third token as a cookie when sharing starts. Tokens are random 32-byte URL-safe strings, compared in constant time. A link sets an HttpOnly, SameSite=Lax cookie on the first visit and redirects to a URL without the token. New links ("New links" in the Share dialog) disconnect everyone on the old ones.
* The view role cannot save, rebuild, or see other documents (their messages, cursors and names included), in the UI or the API; it can open existing files of the shared document. The edit role can create and change any file of the shared document (text, `.tex`, `.bib`, `.cls`, ...) and rebuild it, but never `build.toml`, `latexmkrc` or `.latexmkrc`, not even through live co-editing. Rebuilds are rate limited (6 a minute). Each client and room is capped in count and size.
* Zotero settings are owner-only; fetching (`/api/zotero/preview`, the owner's key and network) is owner-only too unless the owner ticked "Let editors of shared links sync from my Zotero library" (possible only with sandboxed builds): then the edit role may, for the shared document, 6 a minute for all links together; `GET /api/zotero` tells a link only whether it may sync. Better BibTeX sync is refused while sharing. The edit role may apply a chosen splice (`/api/zotero/apply`, no network).
* Bib lookups and DOI imports (Crossref, sends only a DOI or title) need the edit role, 30 a minute, and only for the shared document. Applying happens in the editor, not on the server. The References list is readable with the view role; adding, editing and deleting entries need the edit role. `.bib` files over 1 MB (5 MB and 50 files in all) are not listed.
* Version history and review comments: the view role can browse versions, diffs, comments and suggestions of the shared document (history reads 120 a minute); file-tree changes (60 a minute) and saves (900 a minute) are rate limited too, since each is a version; naming and restoring versions (20 a minute) and accepting and rejecting suggestions need the edit role; commenting and suggesting (120 a minute) need the edit or review role, and a reviewer may delete or withdraw only its own. Deleting versions (Clean up) is never possible through a link. Build-config files never appear, and are never restored, through a link. Authors behind a named link are that link's name; behind the anonymous view and edit links they are the name the browser gives, marked "(unverified name)", and "your own" comments are tied to a random key kept in that browser.
* Live previews (`POST /api/preview`) need the edit role, 120 a minute, two at once per document, and only for the shared document's `.tex`, `.cls` and `.sty` files (never build configuration); an edit of the preamble, a class or a package runs no TeX, it only answers "rebuilding preamble". They run like every build: through the sandbox when it is on, with the sharing restrictions below. A TeX process started before sharing was turned on (or off) is never used: the restrictions are part of what it was started with, so it is replaced.
* Grammar checks need the edit role (the view role cannot start one; 30 a minute) and only for the shared document. Settings are owner-only. While sharing, public mode is refused for everyone, build.toml's `grammar = "public"` included, unless the owner ticked "Allow the public API while sharing"; the Share dialog says which applies. The server never sends the local URL to non-owners.
* The AI assistant is off for shared links unless the owner ticks "Let people with an edit link use it while sharing" (it uses the owner's key), which is only possible with sandboxed builds (`serve.py --share --sandbox`): otherwise LuaLaTeX from an edit link could read the key. Then the edit role may use it, for the shared document only, 10 requests a minute, 20 an hour per visitor (a named link, or an anonymous link from one address; behind a tunnel everyone on an anonymous link shares it, so give people named links) and 100 per server run for all visitors together; the view role never. Settings are owner-only.
* Builds while sharing are restricted, because LaTeX source is code:
  * Shell escape is off (`shell_escape=f`) for the whole server, and `latexmk` runs with `-norc`, so no `latexmkrc` is read. A `build.toml` whose `latexmk_args` enable shell escape, name programs or code to run (`-e`, `-r`, `-pdflatex=...`, `-latexoption`, `-pretex`, `-usepretex`, `-cnf-line`), or move the output (`-outdir`, `-auxdir`, `-jobname`) fails the build.
  * LuaLaTeX stays allowed, but Lua can write files even with shell escape off, so **an edit link to a document that uses (or is switched to) LuaLaTeX can run code on your computer**. The Share dialog says so. Give edit links only to people you trust; view links are safe. On Linux, `serve.py --share --sandbox` confines that code ([sandboxed builds](#sandboxed-builds)).
  * TeX may only read and write inside the document's directory (`openin_any=p`, `openout_any=p`): `\input{/etc/passwd}` fails, and so do inputs that leave the directory with `..` (for example `\input{../shared/x}`) and dotfiles. Such a document builds again when you stop sharing. Your own `latexmkrc` is also ignored until then.
* Responses carry a Content-Security-Policy that allows only this origin and the pinned CDNs, `X-Content-Type-Options: nosniff`, `Referrer-Policy: no-referrer` and `frame-ancestors 'none'`. The Host header must be loopback or the tunnel's own host.
* The Git panel (`/api/git*`) is the owner's only: no link role reaches it, so a guest can neither run git nor read the repository's status, diffs or log.
* Anyone with the edit link can change every file of that document and make this machine compile it, so share links only with people you trust, and treat the edit link like a password. These restrictions make that much safer, but compiling someone else's LaTeX is never risk-free, and they do not cover `--host 0.0.0.0`, which is not what sharing is for.

### Sandboxed builds

On Linux, `build.py --sandbox` or `serve.py --sandbox` (both set `LATEX_SANDBOX=bwrap`, which you can also set yourself) run every LaTeX process under [bubblewrap](https://github.com/containers/bubblewrap) (`apt-get install bubblewrap`): latexmk with its pdflatex/xelatex/lualatex and bibtex/biber runs, the TikZ figure jobs and `--focus` previews. Hosted workers always run this way. Inside the sandbox:

* No network, no other processes (own PID namespace), a private `/tmp`, and `$HOME=/tmp`. The environment holds only `PATH`, locale, `SOURCE_DATE_EPOCH` and TeX's own variables, so tokens and secrets of the server are not visible.
* Read-only: `/usr`, the libraries, fonts and fontconfig, the TeX installation (from `kpsewhich`: `TEXMFROOT`, `TEXMFDIST`, `TEXMFLOCAL`, `TEXMFSYSVAR`, `TEXMFSYSCONFIG`) and the document's own directory. Not `/etc` (only its fonts, texmf, perl and linker files), not your home, not other documents.
* Writable: only the document's build directory in `.latex-cache/` (a figure job: only its own work directory). luaotfload's and fontconfig's caches live there too (`texmf-var/`), so the first LuaLaTeX build of a document takes a few seconds longer; `luaotfload-tool -u` as root fills the shared read-only cache instead. The PDF and the log are copied to `out/` by `build.py` outside the sandbox, and symlinks a build leaves in its build directory are deleted first, so a document cannot make it read or write another file.
* Limits per run: CPU seconds = the build timeout, 2 GB address space, 512 MB per file, 1024 open files. The number of processes is not capped (that limit counts all of a user's processes); a hosted operator sets systemd `TasksMax=` on the workers.
* So LuaLaTeX, and even shell escape, can only change the document's own build output: `io.open("/etc/passwd")`, writes to `$HOME` and network sockets fail.
* Not available: `~/texmf` (personal packages) and files outside the document's directory (`\input{../shared/x}`); your `~/.latexmkrc` is not read.

If `LATEX_SANDBOX=bwrap` and bubblewrap is missing or cannot create namespaces (some containers disable unprivileged user namespaces), every build fails with a message saying so; nothing ever runs unsandboxed. Any other value than `bwrap` (or empty) is refused too. Each sandboxed run costs about 10 ms to start (bwrap), plus about 0.1 s once per `build.py` process for the checks: `files/test` builds in 0.53 s instead of 0.44 s, and `bench/sample-report` (303 pages, 25 figure jobs) in 52 s either way. CI does not sandbox (it builds only the repository's own documents); `synctex`, `texcount`, veraPDF and pandoc (owner-only) run outside it.

---

## Use with AI clients (MCP)

Work on your documents from the AI app you already pay for: Claude (claude.ai, Desktop, mobile, Claude Code), ChatGPT, OpenAI Codex, Cursor, VS Code with GitHub Copilot, Windsurf or Gemini CLI. They talk to this project over the Model Context Protocol (MCP), and the model, its usage and its billing are your plan's. Two servers:

* **Local**, `python scripts/mcp_server.py` (stdio): the documents in this repository, on your computer.
* **Hosted**, `https://<your server>/mcp` (Streamable HTTP with OAuth sign-in): projects on a `host.py` server, see [AI clients on a hosted server](#ai-clients-mcp).

**Subscription or API key.** The editor's own AI assistant calls the Anthropic API and needs an API key (billed per token). A Claude subscription (Pro, Max, Team, Enterprise) cannot pay for those calls, and this project never uses a subscription's sign-in to call the API. To use a subscription, connect Claude (or another app) to the MCP server: the app runs the model under your plan and calls the tools here.

**Tools.** All take a `project` (from `list_projects`) except the first three. Paths are relative to the document folder; nothing outside it is reachable.

| Tool | Does | Needs |
|---|---|---|
| `list_projects`, `search`, `fetch` | projects you can use; word search over all of them (ChatGPT's deep research and company knowledge need exactly `search` and `fetch`); one file by search id | read |
| `list_files`, `read_file`, `grep` | files; a file or a line range; lines containing text (regular expressions locally only) | read |
| `outline`, `word_counts`, `references`, `lint` | headings with file, line and word counts; words per part, chapter and section (texcount when installed); labels, `.bib` entries, missing and unused citations, `\ref` to unknown labels; lint findings | read |
| `list_versions`, `version_diff` | the version history of a file or the project; a line diff of a version against now or against the version before | read |
| `build_status`, `list_reviews` | last build: ok, pages, errors with file, line, log excerpt and a plain-language hint, the end of the log; open comments and suggestions | read |
| `render_page`, `source_for_pdf_position` | a PDF page as a PNG (by number, or the page showing a source line through SyncTeX; the chapter preview with `chapter`); the file and line behind a point of a page (inverse SyncTeX) | read |
| `edit_file`, `write_file`, `rename_file`, `delete_file` | exact text replacement (must match once, or `replace_all`); create or replace a file; rename; delete | write |
| `build`, `preview_chapter` | build now and return the report; typeset one chapter (`--focus`) | write |
| `restore_version`, `label_version` | put a file or a named version back (the current text is kept as a version first); name the project as it is now | write |
| `bib_add_entry`, `bib_edit_entry`, `bib_delete_entry`, `bib_import` | `.bib` entries given as BibTeX; import pasted BibTeX, RIS or a DOI (Crossref; keys already in the project are skipped); only the touched bytes change | write |
| `bib_lookup`, `insert_citation` | missing fields from Crossref (shown, added only with `apply`); `\cite{key}` (or `citep`, `parencite`, ...) after a quoted passage, for keys that exist | write |
| `zotero_sync_preview`, `zotero_apply` | compare a `.bib` file with Zotero (locally the owner's library, hosted your own from the Account page); add new entries and take changed fields by key, never deleting | write |
| `new_document_from_template` | a new document from the article, report, beamer or letter template (hosted: a project in a workspace you connected as a whole and edit) | write |
| `export_docx` | Word export with pandoc, local server only and never while sharing (like the editor) | write |
| `add_comment`, `add_suggestion`, `reply_comment`, `resolve_thread` | a comment thread on quoted text; a suggested replacement (up to 4 places as one suggestion) that people accept or reject in the editor; reply; resolve or reopen | review (editor or reviewer) |
| `delete_comment`, `withdraw_suggestion` | delete a comment or withdraw a suggestion this client made (not other people's) | review (editor or reviewer) |
| `grammar_check` | LanguageTool findings with line, column and replacements (as in the editor: a local server, the public service only if the owner allowed it, never while sharing unless allowed) | review (editor only) |

Accepting or rejecting suggestions is not a tool: people decide. Deleting versions (history clean-up) is not one either, and there is no latexdiff tool: latexdiff itself is a Perl script, but showing its result needs a second LaTeX build of a generated document, and AGENTS.md lets only CI run that outside `sandbox.spawn()`; `version_diff` shows what changed instead. Changes to a `.bib` or `.tex` file someone has open in the editor become a suggestion with the same change (the tool says so) instead of a write. Network tools (`bib_import` with a DOI, `bib_lookup`, the Zotero tools, `grammar_check`) send only what the editor would, only when called, under the editor's rate limits. Changes take the editor's save path: atomic write, the version history records them (author "*client* (MCP)" locally, "*you* via *client*" hosted), open editors reload. A file someone has open in the hosted editor is not written: the tool says so and the model can use `add_suggestion` instead. Results are capped (100,000 characters of text, PNGs up to 2 MB, at most 500 list items); schemas are flat (strings, integers, numbers, booleans), so every client accepts them. Pages render with `pdftoppm` (poppler-utils) or else `mutool`; with neither, `render_page` says so and the rest works.

The protocol is MCP revision 2026-07-28 (each request carries its version, `server/discover`, no sessions) and, for the clients most apps ship today, the `initialize` handshake of 2025-11-25, 2025-06-18 and 2025-03-26. Only tools are offered (no resources, prompts or sampling); no session ids are issued.

### Local server

```bash
python scripts/mcp_server.py [DOC ...] [--source DIR] [--read-only] [--sandbox] [--allow-build-config]
```

It runs as you, like `serve.py` without sharing: builds use the documents' `build.toml` and `latexmkrc`. The tools never change, create or rename onto those two files unless you pass `--allow-build-config`: they can turn on shell escape or run programs, and the model reads text that others may have written (a chapter from a co-author or a share link can carry instructions). Point it only at documents you trust, or add `--sandbox` (Linux, bubblewrap, as in [Sandboxed builds](#sandboxed-builds)). `grep` takes regular expressions here (the hosted server searches literal text only). `--read-only` offers only the tools that read. `DOC` and `--source` select documents like `serve.py`. stdout carries only the protocol; progress and errors go to stderr. If `serve.py` is running, its editor picks up the change on disk, and files it has open in a co-editing room are not written (it lists them in `.latex-history/<document>/rooms.json`, refreshed every 10 s while a room is open; the model can use `add_suggestion` instead).

Use absolute paths (on Windows `python` and `C:\\path\\to\\LatexPipeline\\scripts\\mcp_server.py` in JSON). In each snippet `/path/to/LatexPipeline` is your clone.

* **Claude Code**: `claude mcp add latex -- python3 /path/to/LatexPipeline/scripts/mcp_server.py` (add `-s project` to share it in `.mcp.json`).
* **Claude Desktop**: Settings > Developer > Edit Config opens `claude_desktop_config.json` (macOS `~/Library/Application Support/Claude/`, Windows `%APPDATA%\Claude\`):
  ```json
  {"mcpServers": {"latex": {"command": "python3", "args": ["/path/to/LatexPipeline/scripts/mcp_server.py"]}}}
  ```
* **Cursor** (`~/.cursor/mcp.json`, or `.cursor/mcp.json` in a project), **Windsurf** (`~/.codeium/windsurf/mcp_config.json`) and **Gemini CLI** (`~/.gemini/settings.json` or `.gemini/settings.json`): the same `mcpServers` object as Claude Desktop.
* **VS Code** (Copilot agent mode; `.vscode/mcp.json` in the workspace, or "MCP: Add Server"):
  ```json
  {"servers": {"latex": {"type": "stdio", "command": "python3", "args": ["${workspaceFolder}/scripts/mcp_server.py"]}}}
  ```
* **OpenAI Codex CLI** (`~/.codex/config.toml`):
  ```toml
  [mcp_servers.latex]
  command = "python3"
  args = ["/path/to/LatexPipeline/scripts/mcp_server.py"]
  ```
* **ChatGPT** connects only to remote servers: use the hosted mode.

### Connect from other AI clients

The hosted endpoint works with any client that speaks Streamable HTTP and OAuth 2.1 with dynamic client registration, which is what each app below does when you give it the URL; your browser then opens this server's sign-in and consent pages. Replace `https://latex.example.org` with your `public_url`.

| Client | Remote setup |
|---|---|
| claude.ai, Claude Desktop, Claude mobile | Settings > Connectors > Add custom connector, URL `https://latex.example.org/mcp` (Team and Enterprise: an owner adds it under organization settings first). Redirect `https://claude.ai/api/mcp/auth_callback`. |
| Claude Code | `claude mcp add --transport http latex https://latex.example.org/mcp`, then `/mcp` to sign in (loopback redirect). |
| ChatGPT | Settings > Apps & Connectors > Advanced > Developer mode, then Create: the URL, authentication OAuth. Redirect `https://chatgpt.com/connector_platform_oauth_redirect` (needs the `iss` parameter, which this server sends). |
| OpenAI Codex CLI | `~/.codex/config.toml`: `[mcp_servers.latex]` with `url = "https://latex.example.org/mcp"`, then `codex mcp login latex`. |
| Cursor | `{"mcpServers": {"latex": {"url": "https://latex.example.org/mcp"}}}` in `mcp.json`; Cursor signs in when you enable it (redirect `cursor://anysphere.cursor-mcp/oauth/callback` or loopback, depending on the version). |
| VS Code | `{"servers": {"latex": {"type": "http", "url": "https://latex.example.org/mcp"}}}` in `.vscode/mcp.json`; sign-in through loopback (`http://127.0.0.1:33418/`) or `https://vscode.dev/redirect`. |
| Windsurf | `{"mcpServers": {"latex": {"serverUrl": "https://latex.example.org/mcp"}}}` in `~/.codeium/windsurf/mcp_config.json`. |
| Gemini CLI | `{"mcpServers": {"latex": {"httpUrl": "https://latex.example.org/mcp"}}}` in `settings.json`, then `/mcp auth latex` (loopback redirect). |

Redirect URIs the server accepts at registration: any `https` URL, `http` to `127.0.0.1`, `[::1]` or `localhost` on any port (RFC 8252; the port is ignored when matching), and private-use app schemes: reverse-DNS names (`com.example.app:/callback`, RFC 8252 7.1) plus `cursor`, `vscode`, `vscode-insiders` and `windsurf` (not `javascript:`, `data:`, `ms-msdt:` and the like). Host names are shown in punycode on the consent page. Nothing above is specific to one vendor.

The server offers up to 41 tools (fewer with `--read-only` or a read-only connection; `export_docx` only locally). Clients that cap the number of tools across all servers (Cursor has warned above about 40) can turn single tools off in their MCP settings. ChatGPT's deep research and company knowledge use only `search` and `fetch`; its developer mode and the other clients see every tool the connection allows.

What was tested: the MCP Inspector CLI (`npx @modelcontextprotocol/inspector --cli`) against the local server (`tools/list`, `tools/call`) and against the hosted endpoint with a bearer token (`tools/list`, `list_projects`, `build`, `read_file`); `tests/mcp_e2e.py` plays a client through the whole hosted flow in Chromium (discovery, registration, sign-in with two-step code, consent, PKCE, refresh, MCP calls, disconnect); `tests/test_mcp.py` and `tests/test_host.py` cover the protocol, tools and OAuth rules. The per-app setups in the two lists above follow each vendor's current documentation and were not run against the real apps here (they need accounts and desktop apps); `claude mcp add` was checked to write the configuration shown.

---

## Large documents

Nothing here needs a change to the document.

**TikZ/pgfplots figures are compiled once and cached.** If a document loads `tikz`, `pgf` or `pgfplots` (or a package named after them, such as `tikz-cd`, or one built on TikZ: `forest`, `pgfgantt`, `chemfig`, `smartdiagram`, `tkz-*`), uses `\documentclass[tikz]{standalone}`, a TikZ library, `\tikz` or a `tikzpicture` (in `main.tex`, a class, a package or a chapter; comments and words in the text do not count), `build.py` switches on TikZ's `external` library through `latexmk -usepretex`. A first run lists the figures, they are compiled in parallel (about one process per CPU, shared between the documents built at once; no `make` and no shell escape needed), and the text passes then include the finished PDFs. Figures are cached in `.latex-cache/_figcache/` by the hash of their source, so an edit re-typesets only the text, and renumbering or moving a figure costs nothing. Figures are listed again only when the figure text of a file changed (its `tikzpicture` environments and lines that mention tikz, pgf or axis), so a text edit next to a figure costs one LaTeX pass. A figure job skips the `\input` files before and after its figure that define nothing (no `\newcommand`, `\def`, `\setlength`, `\tikzset`, ... outside their pictures) and sets the counters to what the last full run recorded after them. It does so only when no file of the document changed since that run started; a figure that fails this way is compiled again reading every file. The cache is also invalidated when the preamble, a file the preamble `\input`s, a local `.cls`/`.sty`/`.csv`/`.dat`/`.tikz` file, the TeX engine or the TikZ/pgfplots packages change, and figures nobody uses any more are deleted. Works with `pdflatex`, `xelatex` and `lualatex`. Documents without TikZ are built as before, and so are documents with `remember picture`, `overlay` or `tikzmark` pictures, which cannot be cut out as figures. If the figures cannot be made, the document is built again without externalization and its log says so (a LaTeX error in the text does not trigger that second build).

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
| cold build (`--force`) | 93 s | 55 s |
| one-line text edit | 47 s | 13 s |
| one-line text edit in a file that holds a figure | n/a | 13 s (was 23 s) |
| edit one figure | 24 s | 18 s (was 23 s) |
| `--focus Chapters/chapter5` | n/a | 1.7 s |
| live preview of `Chapters/chapter5` in the editor | n/a | 0.7 s |

A no-op rebuild takes 0.12 s (plain latexmk: 0.12 s). The figure edit now lists only the changed files instead of typesetting the whole document first; plain latexmk needs 27 s for the same edit. The two optimizations before were measured on a busier machine (median of 3): `--force` 87 s to 55 s (figures 35 s to 14 s), a text edit in a file holding a figure 23 s to 13 s, other text edits unchanged at 13 s (one LaTeX pass).

How this compares with plain latexmk, an Overleaf-style build, a naive pdflatex/bibtex script, arara and Tectonic (including where plain latexmk wins) is in [docs/benchmarks.md](docs/benchmarks.md).

Not adopted, because they did not pay off: a precompiled preamble format (saves about 1.2 s of a 12 s pass, changed the page count out of the box and hides the class from `-recorder`), `-draftmode` or `\pdfcompresslevel=0` for intermediate passes and dropping SyncTeX (no gain above the 1 s noise), parallel BibTeX (the ten units take 0.2 s together), and splicing separately built chapters into the full PDF (needs a PDF library; `pdfunite` drops the outline and breaks cross-chapter links).

---

## Benchmarks

`bench/` holds local benchmark documents, such as `bench/sample-report` (303 pages, 25 TikZ figures). CI never builds them. Build one with:

```bash
python scripts/build.py --source bench sample-report
```

`--source DIR` (relative to the repository root, or an absolute path, also outside the repository) selects the documents to build instead of `files/`. The build cache of a directory outside the repository is `.latex-cache/_x<hash of its path>...` (the path casefolded on a case-insensitive file system), so two such directories with the same document names never share it. `--changed-since` is ignored for such a directory (with a note), since it is not in the repository's history. The output goes to `out/` as usual, named after the document: `out/sample-report.pdf`. So a document in `bench/` and one in `files/` with the same name share that output file; give them different names. Building one tree never deletes the other tree's outputs.

---

## Hosting on a VPS

`scripts/host.py` turns the editor into a multi-user site: accounts, workspaces (organisations) with admin, editor, reviewer and viewer members, projects, and sign-in with a password (plus optional two-step codes), Google or any OIDC provider, or GitHub. Standard library only, Python 3.9+. **Serving runs on Linux only**: every build runs under [bubblewrap](#sandboxed-builds), and `host.py serve` refuses to start when `bwrap --unshare-all --ro-bind / / true` fails.

How it works: `host.py` is the only thing that faces the network (behind a TLS proxy). It owns logins, sessions, workspaces and projects, and serves the account and admin pages. Each open project gets its own worker, `serve.py --gateway`, on 127.0.0.1 with a random secret; the gateway proxies `/p/<project>/...` to it (HTTP, long-poll and WebSocket) and tells it the user's role in `X-Host-*` headers, which it strips from client requests. Workers stop after an idle timeout.

What people can do:

| Role | Can |
| --- | --- |
| Site admin (`host.py init`) | everything below in every workspace; sign-up mode, quotas, workspaces, users (disable, reset links, remove two-step), audit log |
| Workspace admin | members and roles, invite links, plus what editors can |
| Editor | create projects (from the `build.py` templates or a zip), edit and build them live with others, download, delete |
| Reviewer | what viewers can, plus comment, reply, resolve and suggest changes (Suggest mode locked on) that editors accept or reject; withdraw their own suggestions; no direct edits, file changes or builds |
| Viewer | open projects read-only, download them as a zip |

The gateway adds `X-Frame-Options: DENY`, `nosniff`, `Referrer-Policy: no-referrer` and `frame-ancestors 'none'` to every proxied response (on top of the editor's own CSP) and refuses cross-site sub-resource requests under `/p/` (only a top-level link from another site is let through). Everyone except the operator is treated as untrusted: LaTeX source is code, so workers always build with the [sharing restrictions](#sharing) (`-norc`, no shell escape, paranoid reads and writes, no build-config edits from the browser) inside the sandbox. A user of another workspace gets `404` for every URL of a project.

### Install and first start

```bash
sudo apt-get install bubblewrap latexmk texlive-latex-extra texlive-fonts-recommended texlive-science texlive-xetex texlive-luatex
sudo useradd --system --create-home --home-dir /var/lib/latex-host latexhost
sudo -u latexhost git clone https://github.com/NotTahaAli/LatexPipeline /var/lib/latex-host/app
sudo -u latexhost LP_ADMIN_EMAIL=you@example.org LP_ADMIN_PASSWORD='a long password' \
     python3 /var/lib/latex-host/app/scripts/host.py init --data /var/lib/latex-host/data
sudoedit /var/lib/latex-host/data/config.toml        # public_url = "https://latex.example.org", trust_proxy = true
```

`init` creates the data directory (mode 700), `config.toml`, the database and the first site admin (from `LP_ADMIN_EMAIL`, `LP_ADMIN_PASSWORD`, `LP_ADMIN_NAME`, or asks). Run it again with the email of an existing account to make it a site admin and print a one-time password reset link; that is the way back in if the last admin is locked out. Python 3.9 and 3.10 need `pip install tomli` for `config.toml`.

`config.toml` (only the operator edits it; restart after changes):

| Key | Default | |
| --- | --- | --- |
| `public_url` | `http://localhost:8080` | The address people open. `https://` turns on `Secure` `__Host-` cookies and HSTS. Requests for any other host name are refused. |
| `listen`, `port` | `127.0.0.1`, `8080` | Where `host.py` listens. Keep loopback and put the TLS proxy in front. |
| `trust_proxy` | `false` | `true` behind a proxy on this machine: client addresses (for login throttling and the audit log) come from `X-Forwarded-For`. |
| `site_name` | `LaTeX Studio` | Page titles and the authenticator app label. |
| `session_days`, `session_idle_hours` | `14`, `12` | A login lasts at most this long, and ends after this long without a request. |
| `max_connections` | `256` | Client connections the gateway serves at once; more get `503` straight away. |
| `max_upload_mb` | `50` | Largest zip upload. At most two uploads are spooled to disk and unpacked at once. |
| `max_streams_per_user` | `8` | Open editor connections (WebSocket, long-poll) per account; more get `429`. |
| `signups_per_ip_hour` | `10` | Sign-ups from one address per hour (IPv6: per `/64`). |
| `[providers.<name>]` | none | Sign-in providers, see below. |

Site settings live in the database and are changed on the Site admin page: sign-up (`invite_only` by default; `open`, where each new person gets their own workspace; or `open_domains`, which needs a provider that confirms the email), linking providers to existing accounts by email, projects per workspace, megabytes per project, open projects on the server and per workspace (each worker builds one document at a time, so this also caps concurrent builds), idle timeout and build time limit. The megabytes per project count the document plus its build output and caches (`.out/`, `.cache/`, `.home/`); the project's worker measures them afresh on every save, co-editing save, upload and new file (`507` when full; shrinking and deleting always work), and a build that goes over loses what it wrote and reports a quota error. A changed quota applies to workers started after the change. When the server's open-project limit is reached and none is idle, the workspace with the most open projects gives up its least recently active one (fair share); a workspace that already has one open never takes another workspace's last one. Open sign-up gives each new account one workspace. The sign-up form answers the same whether or not the address already has an account, then signs in like the login form (so a taken address just gets "wrong email or password").

Sign-in throttling: failed passwords back off per client address (IPv6 per `/64`) and per account. A browser that has signed in to the account before carries a signed known-device cookie and has its own backoff, so someone guessing the password elsewhere cannot lock the owner out. Each two-step code and recovery code works once, also when sent twice at the same moment.

### Sign-in providers

Register `<public_url>/auth/<name>/callback` as the redirect URI with the provider, then:

```toml
[providers.google]
type = "oidc"                                 # any OpenID Connect issuer: Google, Microsoft, Keycloak, Authentik, ...
label = "Google"
issuer = "https://accounts.google.com"        # exactly as the issuer's discovery document says
client_id = "1234.apps.googleusercontent.com"
client_secret_env = "GOOGLE_CLIENT_SECRET"    # read from the environment (or client_secret = "..." in the file)

[providers.github]
type = "github"
label = "GitHub"
client_id = "Iv1.abc"
client_secret_env = "GITHUB_CLIENT_SECRET"
```

Sign-in uses the authorization code flow with PKCE, a state bound to the browser and (OIDC) a nonce; the ID token comes straight from the token endpoint over verified TLS and its issuer, audience, expiry and nonce are checked. Only verified email addresses are accepted (GitHub: the verified primary one). A provider account is linked to an existing account from that account's page ("Connect"), or automatically when the site admin allows it and the existing account's email is confirmed (it signed up through a provider or an invite sent to that address); a password account is never taken over by someone who merely owns the same address at a provider. Two-step sign-in still applies.

### AI assistant

Off unless the operator enables it in `config.toml`:

```toml
[ai]
enabled = true
model = "claude-opus-5-5"            # or claude-sonnet-5-5, claude-haiku-5-5
api_key_env = "ANTHROPIC_API_KEY"    # environment variable with the key (or api_key = "..." in the file)
daily_per_user = 50                  # requests per person per day (UTC); 0 turns them off
daily_per_workspace = 500            # requests per workspace per day
daily_total = 2000                   # requests for the whole server per day: the spending cap
daily_tokens_per_user = 1000000      # input + output tokens per person per day
```

The gateway answers `/p/<project>/api/ai` itself and makes the call to Anthropic; the project's worker never sees the key or the request (workers get a minimal environment, and the key is taken out of the gateway's own environment at start). Editors may use it, reviewers and viewers may not. Every request counts against the request limits before it is sent, and reserves a whole reply (16,000 output tokens) against the token budget while it runs, and its tokens (all models, if a refused request fell back to another) are recorded afterwards, failed ones included (a refusal or a cut-off answer is billed too). A request Anthropic did not bill (unreachable, an HTTP error before an answer, a bad request) is given back and does not use up the daily count. Streamed answers are relayed by the gateway; when the browser stops one before Anthropic reported the final count, the whole reservation stays recorded (output is billed as it is generated). With open sign-up every account gets its own workspace, so `daily_total` is what caps spending. The site admin page shows requests and tokens per person and workspace for the last 30 days. Usage is billed to the operator's key, and members' text is sent to Anthropic, so tell your users. Members who have their own Claude, ChatGPT or other AI subscription can use it through [MCP](#ai-clients-mcp) instead, at no cost to the operator.

### Zotero

Each person brings their own Zotero key: Account > Zotero takes the library type and ID, an optional collection key, BibTeX or BibLaTeX, and a read-only API key. The key is stored in `host.db` (table `zotero_settings`, plain text like the rest of the database, which is mode 600), never shown again (`has_key` only) and never given to a worker. In a project, editors who saved a key get References > More > "Sync from Zotero...": the gateway answers `/p/<project>/api/zotero` and `/api/zotero/fetch` itself and fetches with that person's key (at most 20 pages, 2000 entries; a bigger library syncs by collection), the browser hands the export to the project's worker (`/api/zotero/compare`, network-free, editors only) to compare with the `.bib` files, and applying the chosen entries is a network-free splice there as usual, so no comparison runs in the shared gateway. Reviewers and viewers cannot sync; each account has 10 syncs per 10 minutes, a sync counts against `max_streams_per_user`, at most 4 run on the server at once (503 with `Retry-After` otherwise), and the server makes at most 120 requests a minute to Zotero in all. Better BibTeX (Zotero on your own computer) is local only.

### Git sync

Editors can connect a project to a Git repository over https in the editor's Git tab: the repository URL (`https://host/path`, no name, password or query in it) and a branch (default `main`). **Pull** brings the repository's files in: the first pull takes them but never overwrites a file of the project that differs (it lists those; rename or delete them and pull again); later pulls first commit the project's current files (as the person pulling) and then fast-forward or merge, and if both sides changed the same lines nothing is merged and the files are listed. **Push** commits the project's files as one commit by the person pushing (their name and e-mail) and pushes it to the branch; if the repository has commits the project lacks, it says to pull first (never a force push). Disconnect forgets the URL and the repository copy, the files stay.

Each person brings their own token: Account > Git takes the user name on the Git host and an access token (a personal access token with access to the repository's contents). It is stored in `host.db` (table `git_tokens`), never shown again (`has_key` only), and used only by the gateway for that person's pulls and pushes; pulling a public repository needs none. Git runs in the gateway, never in a worker, on a repository kept in `<data>/git/<project>` outside the project area (so nothing a project contains can set its configuration or hooks), with the token passed in git's environment (never on a command line or to a worker) and: no system or global configuration, no hooks, no `fsmonitor`, symbolic links checked out as plain files, no submodules, only the `https` protocol, no redirects, and the host resolved first, refused if any of its addresses is loopback, private, link-local, shared or otherwise not public (SSRF), and pinned for git to the checked address (`http.curloptResolve`). A transfer stops after 2 minutes or once the repository copy is larger than twice the project quota, the files of the pulled branch must fit the project quota before any is written, and each account has 20 syncs per 10 minutes, one at a time per project, at most 4 on the server (503 with `Retry-After` otherwise). Connect, disconnect, pull, push and token changes are in the audit log. A pull writes files the way an edit outside the editor does: open editors merge them into the shared text. Viewers and reviewers see whether a project is connected, nothing more.

### API tokens

Scripts and the [VS Code extension](#vs-code-extension) cannot sign in with a browser, so Account > API tokens makes personal tokens: a name and the projects it may open. The token (`lpat_...`) is shown once; the server keeps only its SHA-256. It works only as `Authorization: Bearer <token>` on `/p/<project>/...` (the project's editor API, with your current role there, so removing or demoting you takes effect at once) and on `GET /api/token` (who it belongs to and its projects), never as a session for the rest of the site; WebSockets, the AI assistant and Zotero stay with the browser (the editor's long-poll works). Revoke a token on the same page; changing or resetting your password and a disabled account end all of them. Making and revoking tokens are in the audit log.

### AI clients (MCP)

Off unless the operator enables it in `config.toml`:

```toml
[mcp]
enabled = true
calls_per_minute = 60              # tool calls per connected client
registrations_per_ip_hour = 20     # new client registrations per address
max_clients = 5000                 # registered clients kept at most (unused ones go after a day)
```

Members then add `https://latex.example.org/mcp` to their AI app (snippets in [Connect from other AI clients](#connect-from-other-ai-clients)). The app finds the sign-in through `/.well-known/oauth-protected-resource` (RFC 9728) and `/.well-known/oauth-authorization-server` (RFC 8414), registers itself at `/oauth/register` (RFC 7591), and opens `/oauth/authorize` in the browser. The person signs in as usual (password and two-step code, or a sign-in provider) and sees a consent page: the app's name (chosen by the app, so the page also says where it returns: `claude.ai`, or "an app on your computer" with a warning for loopback and app-scheme redirects), what it may do (read; change files and run builds; add comments and suggestions), and which workspaces (all their projects, also later ones) or single projects. Only workspaces the person is a member of are offered.

Every tool call runs with the person's current role in that workspace, narrowed by the grant: a viewer never writes even with the write permission, a reviewer gets only the review tools (comments and suggestions, not `grammar_check`), and leaving a workspace ends access to it at once. `list_projects`, `search` and `fetch` read the project folders in the gateway; every other tool is forwarded to the project's worker (`POST /api/mcp`, with `X-Host-Role` and `X-Host-User` like the editor's requests), so files, history, review, quotas and the bubblewrap sandbox (builds and `render_page`'s `pdftoppm`) work exactly as in the editor. Changes appear in the history as "*name* via *app*". Changes, builds, comments, connections, refusals, revocations and token reuse go to the audit log (`mcp_tool`, `mcp_authorized`, ...).

The account page lists the connected apps (where they return, permissions, projects, last use) with Disconnect, which ends a connection at once. An app's own "disconnect" usually calls `/oauth/revoke`, which does the same.

Security details:

* OAuth 2.1 authorization code with PKCE (S256 only), public clients only (`token_endpoint_auth_method` `none`; a Basic header's client id is accepted, its secret ignored). Codes last 2 minutes and work once; a second use revokes the connection. Every redirect carries `state` and `iss` (RFC 9207). A bad authorization request is shown on this server's own page, never redirected to the app, until the person has decided (RFC 9700 4.11.2: no open redirect).
* Access tokens last an hour and are bound to `<public_url>/mcp` (RFC 8707 `resource`; another resource is `invalid_target`, a token for another one is refused). Refresh tokens last 30 days and rotate on every use; presenting an old one revokes the connection. Tokens are random and only their SHA-256 is stored.
* `/mcp` takes only `Authorization: Bearer` (cookies are ignored), answers `401` with `WWW-Authenticate: Bearer resource_metadata=...` and `403 insufficient_scope` for a tool outside the grant, refuses a request whose `Origin` is another site, and is stateless: no `Mcp-Session-Id`, no SSE stream (`GET` is `405`). Workers never see tokens.
* Registration is open by design (that is how the apps connect) but rate limited per address and capped in number; unused registrations are deleted after a day. Client ID Metadata Documents (the newer alternative to registration) are not supported, because the gateway would have to fetch URLs that anyone names; every app listed falls back to registration.
* Changing or resetting the password and disabling the account disconnect every app (re-enabling does not bring them back); leaving a workspace ends access to it. `search` reads files in the gateway, so it is limited to 10 a minute per person and 16 MB over at most 20 projects; regular expressions in `grep` are local-only. The worker's `/api/mcp` is not reachable through the editor proxy, only through `/mcp`.
* The model and the app are untrusted: they get nothing a person with the same role could not do in the editor, never change `build.toml` or `latexmkrc` (only the local owner can, with `mcp_server.py --allow-build-config`), never accept suggestions, and their tool results are capped.

### TLS with Caddy, and systemd

The TLS proxy is required, not optional: `host.py` speaks plain HTTP, listens on 127.0.0.1 by default (it warns when `listen` is anything else) and has only coarse slow-client protection of its own (a socket timeout and `max_connections`). Let the proxy terminate TLS and time out slow clients. `/etc/caddy/Caddyfile` (Caddy gets and renews the certificate; WebSockets and long-polls pass through):

```caddyfile
{
    servers {
        timeouts {
            read_header 10s   # slow-loris: a client must send its headers quickly
            read_body 5m      # zip uploads (max_upload_mb) on slow links
            idle 2m
        }
    }
}

latex.example.org {
    encode gzip
    reverse_proxy 127.0.0.1:8080 {
        transport http {
            dial_timeout 5s
            response_header_timeout 90s   # longer than the editor's 25 s long-poll
        }
    }
}
```

`/etc/systemd/system/latex-host.service`:

```ini
[Unit]
Description=LaTeX Studio (host.py)
After=network-online.target

[Service]
User=latexhost
WorkingDirectory=/var/lib/latex-host
ExecStart=/usr/bin/python3 /var/lib/latex-host/app/scripts/host.py serve --data /var/lib/latex-host/data
EnvironmentFile=-/etc/latex-host.env
Restart=on-failure
# The sandbox sets CPU, memory and file-size limits per LaTeX run but no process limit: cap it here.
TasksMax=512
MemoryMax=8G
NoNewPrivileges=yes
ProtectSystem=strict
ReadWritePaths=/var/lib/latex-host/data
ProtectHome=yes
PrivateTmp=yes

[Install]
WantedBy=multi-user.target
```

Put provider secrets in `/etc/latex-host.env` (mode 600), for example `GOOGLE_CLIENT_SECRET=...`. Workers get a minimal environment (never these secrets), and LaTeX inside the sandbox an even smaller one. `sudo systemctl enable --now latex-host caddy`. bubblewrap needs unprivileged user namespaces (on by default on Debian and Ubuntu; Ubuntu 24.04's AppArmor rule allows `bwrap`).

`--insecure-no-sandbox` runs without bubblewrap (and on any OS) for development only: it prints a warning, and the admin page says so. Never use it on a server people can reach.

### Backups and data

Everything is in the data directory: `host.db` (sqlite, WAL), `config.toml` and `projects/<workspace>/<project>/<name>/` (the document folder; `.out/`, `.cache/` and `.home/` beside it are regenerated; `.latex-history/` beside it holds the editor's version history and review comments, counts toward the project quota (the blobs, the sqlite index and `review.json`), is capped at a quarter of it, and is not in the zip download; workspace admins may delete old versions from the History tab, which the gateway tells the worker with an `X-Host-Admin` header). `git/<project>/` holds the copy of a project's Git repository (Git sync; made again by the next pull if lost). Back up with `sqlite3 host.db ".backup host-backup.db"` (safe while running) plus the `projects/` tree, for example nightly with `restic` or `rsync`. To restore, stop the service, put both back, start it. Upgrades: `git pull` and restart; the database schema migrates itself (the version is in its `schema_version` table).

## Lint and CI reports

`scripts/ci_report.py` (standard library only; CI-only apart from local use) reads `out/build-report.json`:

```bash
python scripts/ci_report.py summary                  # markdown table of the last build
python scripts/ci_report.py lint [DOC ...]           # labels, refs, figures, bib checks, overfull boxes, accessibility notices, chktex, word counts -> out/lint-report.json
python scripts/ci_report.py lint --strict            # exit 1 on findings
python scripts/ci_report.py lint --update-baseline   # accept current findings in files/<doc>/.lint-baseline
python scripts/ci_report.py lint --grammar           # add LanguageTool findings, see Grammar
python scripts/ci_report.py lint --bib-lookup        # suggest missing bib fields from Crossref (sends DOIs/titles)
python scripts/ci_report.py lint --verify-citations  # flag bib entries Crossref does not confirm (sends DOIs/titles)
python scripts/ci_report.py lint --format            # list .tex files tex-fmt would change (notices)
python scripts/ci_report.py diff --base REF --out DIR  # latexdiff of changed documents
python scripts/ci_report.py pr-comment [--print]     # sticky PR comment (needs gh and GH_TOKEN)
```

A missing `chktex`, `texcount` or `latexdiff` leaves its column blank. `--overfull-pt` (default 10) sets the overfull-box threshold.

`--verify-citations` checks every entry of every `.bib` against Crossref and reports "possibly fabricated or wrong reference" when its DOI does not resolve, when there is no DOI and no Crossref record has a close title (the Look up rule: similarity >= 0.9, five results searched by title and first author's surname), or when the record disagrees on title, year (by more than one) or first author's surname. Only the DOI, the title and that surname are sent; lookups are throttled (30 a minute) and cached. An entry that could not be checked (network, rate limit) is skipped with a message, never reported. Crossref does not list everything (many books, theses, web pages, very new preprints), so read a finding as "check this", and accept real ones with `--update-baseline`. Like `--bib-lookup` and `--grammar`, it is opt-in and the workflow does not pass it.

`--format` runs `tex-fmt --check` on each `.tex` file (with the document's `tex-fmt.toml`) and reports the files it would change as notices, which `--strict` ignores; `build.py --format` fixes them. A missing tex-fmt is a message. The workflow does not pass `--format`, so formatting never fails CI.

---

## Tests and lint

```bash
python3 -m unittest discover -s tests   # unit tests, standard library only
uvx ruff check scripts tests            # lint: rules in pyproject.toml
```

The [Lint and tests](.github/workflows/lint.yml) workflow runs the unit tests on Ubuntu, Windows and macOS under Python 3.9 and 3.13, ruff (pinned version) and the editor's co-editing check (`node tests/collab_check.mjs scripts/serve_ui/collab.js`) and the table-paste, review-export, editing-tools and visual-tool checks (`node tests/tablepaste_check.mjs`, `node tests/reviewexport_check.mjs`, `node tests/edits_check.mjs`, `tableedit_check.mjs`, `figure_check.mjs`, `tikz_check.mjs`, `markdown_check.mjs`) and the VS Code extension's and Git panel's helpers (`node tests/vscode_check.mjs`) on Ubuntu, and the `requirements.txt` check. It runs when `scripts/`, `tests/`, `vscode/` or the Python project files change.

The hosted mode has a browser end-to-end check, run by hand (needs LaTeX, bubblewrap and Chromium for Playwright; the editor loads its libraries from CDNs): `uv run --no-project --with playwright==1.56.0 python tests/host_e2e.py`. It signs in with two-step codes, invites editors and a viewer, builds and co-edits a project, checks workspace isolation, sign-out and session expiry, and runs axe-core on every host page in both themes and at phone width.

AI clients over MCP have one as well: `uv run --no-project --with playwright==1.56.0 python tests/mcp_e2e.py [--insecure-no-sandbox] [--chromium PATH]` registers a client, signs in with a two-step code, consents, exchanges the code with PKCE, builds, reads, edits and renders a page over MCP, rotates the refresh token, and disconnects on the account page, with axe-core on the consent and account pages (both themes, phone width).

The editor's live preview has one too: `uv run --no-project --with playwright==1.56.0 python tests/preview_e2e.py [--chromium PATH] [--axe PATH]` (needs LaTeX and a full build of `bench/sample-report`). It types into a chapter five times, measures keystroke to spliced pages, checks the new word is in them, that the scroll position stays and that a newer keystroke aborts the preview in flight, runs axe-core in both themes, and restores the file.

---

## GitHub Actions

[`.github/workflows/build-pdf.yml`](.github/workflows/build-pdf.yml) runs on pull requests and on pushes to the default branch when `files/`, one of the build scripts (`build.py`, `accel.py`, `hints.py`, `a11y.py`, `publish_release.py`, `ci_report.py`), the workflow, `.github/texlive-packages.txt` or the Python project files change. Editing only `serve.py`, its UI or the tests does not trigger it. A push to a feature branch does not run it: its pull request does. It can also be started manually (`workflow_dispatch`).

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

[`.github/workflows/binaries.yml`](.github/workflows/binaries.yml) runs on pushed `v*` tags (and manually): it builds the [binaries](#binaries) on Linux, macOS and Windows, runs each with `--help` (and `build --list`), and attaches them to that tag's release, creating the release if needed. A manual run on a branch keeps them as run artifacts only.

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

## Licence

GNU Affero General Public License v3.0 or later (`AGPL-3.0-or-later`), see [`LICENSE`](LICENSE). If you run a modified copy as a network service (for example the hosted mode), you must offer its users the modified source. The bundled fonts in `scripts/fonts/` keep their own SIL Open Font License (`scripts/fonts/OFL.txt`).
