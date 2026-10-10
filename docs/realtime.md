# Near-real-time preview: what was tried

Goal: the PDF follows typing in a large document. Document: `bench/sample-report` (303 pages, 25 TikZ/pgfplots figures, hyperref, ten chapter bibliographies). Machine: 4 CPUs, Linux, TeX Live 2023 (pdfTeX 1.40.25), shared with other jobs; the load average is given with each number because it moves them by up to 2x.

## Stage 1: warm compiler + chapter splice (shipped)

`scripts/preview.py`, `scripts/serve_ui/preview.js`, `POST /api/preview`. See README, "Live preview".

**Where the 1.8 s of a chapter preview went.** `build.py --focus Chapters/chapter5` takes 1.8 s (docs/benchmarks.md; 2.0 s wall time in the run below). Loading the preamble alone (class, about 40 packages, fonts) is 1.0 to 1.2 s of it, measured by stopping the run at `\begin{document}`. The rest is skipping the other chapters (their `\input`s do nothing) and typesetting the chapter, including its pgfplots figures.

**Warm process.** The engine is started ahead of time with `\document` redefined to read one line from the terminal before doing anything (`\read-1`, the only part run in `errorstopmode`, since `nonstopmode` refuses terminal reads). It loads the class and packages and blocks. A preview writes the `--focus` selection (`accel.focus_tex`, unchanged) and the editor's unsaved text into the process's own directory, copies the snapshot of the full build's `.aux`/`.bbl`/`.toc` files there (it has not read them yet: `\document` reads the `.aux`), and sends the file name. TeX then typesets the chapter and exits; the next process is started at the same moment, so it has loaded its preamble by the next pause. Each process is used once: TeX cannot return to its state at `\begin{document}`, and `fork()`-based snapshots (TeXpresso) need a patched engine and do not exist on Windows.

* No format dump (`mylatexformat`): it changed the page count of this document before (README, "Large documents"), hides the class from `-recorder`, and does not work for XeLaTeX/LuaLaTeX. The waiting process works with all three engines, unchanged.
* The unsaved text goes in as an overlay: the `\input` of the file being typed reads a copy in the process directory, so co-editors' files on disk are never written by a preview.
* Figures: the full build's externalized TikZ figures are included (`mode=graphics if exists`, with the figure counter the full build recorded before the chapter) when no picture of the chapter's files changed since that build (`figtext.json`); otherwise they are typeset. Chapter 3 (two pgfplots figures), warm, load about 2: 2.3 s with the figures typeset, 0.74 s with the cached ones.
* Fidelity: chapter 3's 22 preview pages against the full PDF: 20 text-identical (`pdftotext`), the two others differ only in the extraction order of two superscripts; 21 of 22 pixel-identical at 40 dpi.
* Restarted when anything its preamble depended on changes (`preamble_key`: `main.tex` up to `\begin{document}`, files it `\input`s, local `.cls/.sty/.def/.cfg/.clo/.fd/.ldf`, `build.toml`, engine, shell escape, the sharing variables `shell_escape/openin_any/openout_any`, `LATEX_SANDBOX`). A process started under other restrictions is never triggered. Idle processes stop after 10 minutes.
* Sandbox: every process starts through `sandbox.spawn(work=...)` (only its own directory and the font caches writable) and its directory is scrubbed afterwards. One thing broke under bubblewrap only: `--die-with-parent` uses `PR_SET_PDEATHSIG`, which fires when the *thread* that started the process ends, and HTTP request threads end right away, so waiting processes died. They are now started from one long-lived thread.

**Browser.** 700 ms after the last keystroke (own typing only, not a co-editor's) the buffer is posted; the response is the PDF itself (description in an `X-Preview` header, one round trip). The viewer keeps the full PDF and maps the chapter's pages to the preview's: the preview's first page label (hyperref's `/PageLabels`) is looked up in the full PDF, and the chapter ends at the page counter the full build recorded when it left the chapter. Pages render into new canvases that replace the old ones only when drawn, so nothing blanks and the scroll position stays. A keystroke aborts the request in flight; the next request for the same browser tab kills its TeX process on the server. A full build's PDF replaces the splice unless the preview was typeset after that build started.

### Measured (median of 5 after one warm-up; edit = one word changed)

| Path | chapter 5 (text + 2 figures) | chapter 3 (2 pgfplots figures) | load |
|---|---|---|---|
| `build.py --focus` (cold, from saved files, wall time incl. Python start) | 2.02 s | 3.14 s | 0.9 to 1.8 |
| live preview, server: request to PDF bytes (HTTP) | **0.69 s** (0.64 to 0.69) | **0.81 s** (0.74 to 1.00) | 0.9 to 1.8 |
| live preview, first request (no waiting process yet) | 2.4 s | | 1.5 |
| browser, keystroke to spliced pages, incl. the 700 ms pause, full builds of the autosave running alongside (`tests/preview_e2e.py`) | | **1.76 s** (min 1.63; server part 0.7 to 1.0 s) | 0.8 to 1.6 |
| the same, other jobs on the machine | | 2.27 s (server part 1.1 to 2.0 s) | 3.1 |

On a quiet machine the target of about 1 s from request to PDF is met for both chapters; the warm process saves the 1.0 to 1.2 s preamble load, the cached figures another 1.5 s on chapter 3. In the browser the 700 ms pause comes on top, and the autosave's full build (one more pdfTeX at 100% CPU) competes with the preview; on this 4-CPU machine with other jobs running the server part grew to 1.1 to 2.7 s.

### Limits

* Everything outside the chapter is the last full build's: new labels and citations are `??`, page numbers after the chapter do not move.
* The splice needs hyperref page labels; without them the preview is shown on its own (as "Preview chapter" does).
* `\include`d chapters and files read by packages are previewed from disk (after the autosave); `main.tex` and preamble files are not live-previewed.
* Double-click (SyncTeX) on a preview page is refused; forward search lands on the chapter's first page while a preview is shown.
* One waiting TeX process per document previewed (about 40 MB resident for this document), plus at most two running.

## Stage 2: live engines

### TeXpresso (built and measured)

[let-def/texpresso](https://github.com/let-def/texpresso) at e8df770 (MIT), built from source on Ubuntu 24.04 (`libsdl2-dev libmupdf-dev ...`, its engine is a patched XeTeX in C, about 5 minutes). Driven headless (`SDL_VIDEODRIVER=dummy`) over its editor protocol with `experiments/live/texpresso_probe.py`:

| step on `bench/sample-report` | time |
|---|---|
| first page shown | 1.6 s |
| page forward to page 105 (it typesets only up to the page shown) | 10.4 s |
| edit in chapter 5 while page ~105 is shown, until output settles (5 edits) | median 0.62 s (0.49 to 0.66) |
| edit in chapter 3 (before two pgfplots figures) while page ~90 is shown | 8.9 s for the first edit; the next four produced no output at all |

It runs under our bubblewrap sandbox (`sandbox.wrap`, `files/test`, 5.4 s for `-test-initialize`), so the sandbox rule would not exclude it. It was not integrated because:

* It renders into its own SDL window with MuPDF. The protocol carries the log, file lookups and SyncTeX jumps, but no PDF and no page images, so the browser viewer cannot show its output without patching TeXpresso (a page-image or PDF stream).
* Its engine is XeTeX: a pdfLaTeX document (this one) gets other fonts and line breaks than the real build; pdfTeX-only code fails.
* Edits are fast only when the shown page is just after the edit; reaching a page costs a typeset of everything before it (10 s to page 105), and the chapter 3 edit with figures took 8.9 s and then stalled.
* Linux and macOS only (`fork()` checkpoints), early-stage by its own README.

### Real-Time LuaTeX / texlode (TUG 2026, read, not run)

Clemens Lode, "Real-Time LuaTeX: Recompiling Large Documents in 1ms", TUG 2026 preprint (https://www.tug.org/tug2026/preprints/lode-realtime.pdf). A LuaTeX process with the preamble loaded stays alive and is fed single paragraphs; the display list is read from the node lists after line breaking instead of writing a PDF; about 1 ms per paragraph (0.17 to 2 ms by length, constant over 500 compiles), on unmodified TeX Live 2025. Page breaking, floats, footnotes and `\ref`/`\thepage` fall back to a background full compile. The browser editor (texlode) was announced for October 2026; no code or licence was available to test. The idea that applies here is the same split as Stage 1 (fast local path, full build for global layout) at paragraph instead of chapter granularity; it needs LuaLaTeX and a custom renderer for display lists, so it does not fit a pdfLaTeX pipeline whose output is the real PDF.

### SwiftLaTeX / BusyTeX (read, not run)

TeX Live compiled to WebAssembly, running in the browser. Each compile still processes the whole document (25 s per pass here with pdfTeX natively, slower in WASM), so it is a way to move compiles off the server, not to make them incremental.

## Recommendation

Keep Stage 1 as the live path: it gives the real pdfTeX output of the chapter in about 0.7 to 0.8 s on a quiet machine, works with all three engines and in the sandbox, and needs nothing installed. Do not integrate TeXpresso now (no PDF/image output to stream into the browser viewer, XeTeX-only, unstable on figure edits); revisit if it gains a page-image stream. Watch texlode's release: paragraph-level LuaTeX updates could replace the chapter splice for LuaLaTeX documents. Cheaper next steps for Stage 1: focus on the section instead of the chapter (fewer pages to typeset), and skip `\maketitle`-like front matter more aggressively in the focus run.
