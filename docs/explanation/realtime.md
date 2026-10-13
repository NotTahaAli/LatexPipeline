# Near-real-time preview: what was tried

Goal: the PDF follows typing in a large document. Document: `bench/sample-report` (303 pages, 25 TikZ/pgfplots figures, hyperref, ten chapter bibliographies). Machine: 4 CPUs, Linux, TeX Live 2023 (pdfTeX 1.40.25), shared with other jobs; the load average is given with each number because it moves them by up to 2x.

## Stage 1: warm compiler + chapter splice (shipped)

`scripts/preview.py`, `scripts/serve_ui/preview.js`, `POST /api/preview`. See [Live preview](../editor/live-preview.md).

**Where the 1.8 s of a chapter preview went.** `build.py --focus Chapters/chapter5` takes 1.8 s ([benchmarks.md](benchmarks.md); 2.0 s wall time in the run below). Loading the preamble alone (class, about 40 packages, fonts) is 1.0 to 1.2 s of it, measured by stopping the run at `\begin{document}`. The rest is skipping the other chapters (their `\input`s do nothing) and typesetting the chapter, including its pgfplots figures.

**Warm process.** The engine is started ahead of time with `\document` redefined to read one line from the terminal before doing anything (`\read-1`, the only part run in `errorstopmode`, since `nonstopmode` refuses terminal reads). It loads the class and packages and blocks. A preview writes the `--focus` selection (`accel.focus_tex`, unchanged) and the editor's unsaved text into the process's own directory, copies the snapshot of the full build's `.aux`/`.bbl`/`.toc` files there (it has not read them yet: `\document` reads the `.aux`), and sends the file name. TeX then typesets the chapter and exits; the next process is started at the same moment, so it has loaded its preamble by the next pause. Each process is used once: TeX cannot return to its state at `\begin{document}`, and `fork()`-based snapshots (TeXpresso) need a patched engine and do not exist on Windows.

* No format dump (`mylatexformat`): it changed the page count of this document before ([Large documents](../guides/large-documents.md)), hides the class from `-recorder`, and does not work for XeLaTeX/LuaLaTeX. The waiting process works with all three engines, unchanged.
* The unsaved text goes in as an overlay: the `\input` of the file being typed reads a copy in the process directory, so co-editors' files on disk are never written by a preview.
* Figures: the full build's externalized TikZ figures are included (`mode=graphics if exists`, with the figure counter the full build recorded before the chapter) when no picture of the chapter's files changed since that build (`figtext.json`); otherwise they are typeset. Chapter 3 (two pgfplots figures), warm, load about 2: 2.3 s with the figures typeset, 0.74 s with the cached ones.
* Fidelity: chapter 3's 22 preview pages against the full PDF: 20 text-identical (`pdftotext`), the two others differ only in the extraction order of two superscripts; 21 of 22 pixel-identical at 40 dpi.
* Restarted when anything its preamble depended on changes (`preamble_key`: `main.tex` up to `\begin{document}`, files it `\input`s, local `.cls/.sty/.def/.cfg/.clo/.fd/.ldf`, `build.toml`, engine, shell escape, the sharing variables `shell_escape/openin_any/openout_any`, `LATEX_SANDBOX`). A process started under other restrictions is never triggered. Idle processes stop after 10 minutes.
* Sandbox: every process starts through `sandbox.spawn(work=...)` (only its own directory and the font caches writable) and its directory is scrubbed afterwards. One thing broke under bubblewrap only: `--die-with-parent` uses `PR_SET_PDEATHSIG`, which fires when the *thread* that started the process ends, and HTTP request threads end right away, so waiting processes died. They are now started from one long-lived thread.

**Browser.** 700 ms after the last keystroke (own typing only, not a co-editor's) the buffer is posted; the response is the PDF itself (description in an `X-Preview` header, one round trip). The viewer keeps the full PDF and maps the chapter's pages to the preview's: originally through the preview's first page label (hyperref's `/PageLabels`) looked up in the full PDF; now through the pages the full build had shipped when the chapter started and ended (Stage 1b), with the labels as the fallback for an older `.focusmap`. Pages render into new canvases that replace the old ones only when drawn, so nothing blanks and the scroll position stays. A keystroke aborts the request in flight; the next request for the same browser tab kills its TeX process on the server. A full build's PDF replaces the splice unless the preview was typeset after that build started.

### Stage 1c: preamble edits and page numbers after the chapter (shipped)

* **Preamble edits keep the preview going.** An edit of `main.tex`'s preamble, a local class or package, or a file the preamble `\input`s is kept per browser tab as unsaved preamble text (`Warm.unsaved`; for `main.tex` only the part before `\begin{document}`, its body stays the file's). A process is started from it: the text is written into the process directory and `\declare@file@substitution` (one command line, no newline: a newline there is typeset before `\documentclass` and stops the run) makes `\input{main.tex}`, `\usepackage` and `\documentclass` read it in place of the file. `preamble_key` covers the substituted text, so that process serves only this tab's previews until the file on disk says the same, after which the saved preamble's key applies. The chapter the tab previewed last (else the first one) is typeset on it at once, and its pages replace the old splice when they arrive; the browser keeps the old pages until then. The full build's cached TikZ figures are not reused under such a process (they were made under the old preamble). References, page numbers and the input tree still come from the last full build, as for any preview; a preamble that writes `.aux` lines the old build did not know can make the preview show an error until the full build is in.
* **Page numbers after the chapter.** The splice replaces the chapter's pages of the full PDF with the preview's; when their number differs, the pages after it move by that much in the viewer but keep the numbers the full build printed. Renumbering them would need a new full build (cross-references, the table of contents, page styles), so the status line says "later pages +N" and its tooltip says page numbers after the chapter may be off until the full build after the save.
* **Measured** on `bench/sample-report` (`Warm.compile` in-process, 6 runs per row, the first dropped from the median; a 4-CPU machine shared with other jobs, so the load is given per run and only numbers from the same load compare):

| | before | after |
|---|---|---|
| one word in `Chapters/chapter5/subsection1.tex`, back to back (load 3.5 to 4.5) | 1.33 s (min 1.18) | 1.31 s (min 1.22) |
| the same, separate runs | 1.25 s (load 0.4) | 1.51 s (load 1.8), 3.0 s (load 7) |
| a `\newcommand` added to `main.tex`'s preamble, unsaved: first preview (new process loading the edited preamble) | none until the full build after the save (about 60 s here) | **3.0 s** (load 1.8); 4.3 and 5.9 s (load 4 to 9) |
| then one word in chapter 5, preamble still unsaved (warm) | (paused) | 1.36 s (load 1.8); 1.93 and 1.46 s (load 4 to 9) |

The one-word path does the same work as before (one dictionary lookup more when no preamble text is kept), and back to back the medians match; the separate runs differ with the load. The preamble edit costs what a cold preview costs: loading the edited preamble plus the chapter.

### Measured (median of 5 after one warm-up; edit = one word changed)

| Path | chapter 5 (text + 2 figures) | chapter 3 (2 pgfplots figures) | load |
|---|---|---|---|
| `build.py --focus` (cold, from saved files, wall time incl. Python start) | 2.02 s | 3.14 s | 0.9 to 1.8 |
| live preview, server: request to PDF bytes (HTTP) | **0.69 s** (0.64 to 0.69) | **0.81 s** (0.74 to 1.00) | 0.9 to 1.8 |
| live preview, first request (no waiting process yet) | 2.4 s | | 1.5 |
| browser, keystroke to spliced pages, incl. the 700 ms pause, full builds of the autosave running alongside (`tests/preview_e2e.py`) | | **1.76 s** (min 1.63; server part 0.7 to 1.0 s) | 0.8 to 1.6 |
| the same, other jobs on the machine | | 2.27 s (server part 1.1 to 2.0 s) | 3.1 |

On a quiet machine the target of about 1 s from request to PDF is met for both chapters; the warm process saves the 1.0 to 1.2 s preamble load, the cached figures another 1.5 s on chapter 3. In the browser the 700 ms pause comes on top, and the autosave's full build (one more pdfTeX at 100% CPU) competes with the preview; on this 4-CPU machine with other jobs running the server part grew to 1.1 to 2.7 s.

### Stage 1b: what used to stay stale (shipped)

Measured on `bench/sample-report` as in [benchmarks.md](benchmarks.md) ("Live preview"); load 2.7 to 4.1 from other jobs.

* **Labels and citations new in the chapter.** After a pass, the `\newlabel`/`\bibcite` lines the preview wrote are compared with what it read (the snapshot, or an earlier pass), on what a reference prints only (number and page: hyperref's anchor names differ in every focus run, because counters of skipped chapters are not restored, and comparing them made every preview take two passes). If a changed one is referenced in the typed text, the next waiting process typesets again with those lines appended to the main `.aux`. Undefined citations in the log start BibTeX (sandboxed, in the finished process's directory, with copies of the `.bib` files it names) on the snapshot's `\citation` lines plus the new ones, for every `.aux` with `\bibdata` (chapterbib units too); the new `.bbl` is typeset in the next pass and a third prints the numbers. At most 3 passes. New label: 1.54 s instead of 0.74 s with `??`; new citation: 2.20 s instead of 0.70 s with `(?)`. Without the second waiting process (kept after the first multi-pass preview), pass 2 waited about 0.4 s for its preamble. biblatex with `backend=biber` (no `\bibdata`, but the run wrote a `.bcf`, which biblatex writes only for biber): biber runs the same way on the run's `.bcf`, each refsection listing the snapshot's keys first (from the full build's `.bcf`, kept in the snapshot but not copied into the processes) so `sorting=none` numbers stay, with only the document's local data files (copied; remote sources left out); biblatex takes its labels from the `.bbl`, so the second pass prints them. 1.3 s instead of 0.8 s with `[key]` on a two-chapter test document; with `backend=bibtex` biblatex goes through BibTeX as above.
* **main.tex.** The posted text is compared with `main.tex` as the snapshot's build saw it. A change before `\begin{document}` (or of a local `.cls`/`.sty`/file the preamble reads) used to answer "preamble": nothing was typeset, the old splice was removed, and previews paused until the full build after the save was loaded (about a minute on the bench report). Since Stage 1c it restarts the preview instead (see above). A change in the body between two `\input`s is typeset alone: every chapter is skipped, pages are kept from the end of the chapter before (its exit counters restored) to the start of the next; the body is read from an overlay through the primitive `\input` in a `begindocument/end` hook, ending with its own `\end{document}`, padded so line numbers match. 0.2 to 0.4 s. An edit that adds, removes or changes an `\input`/`\include` line answers "rebuild" (the input tree is the full build's to record).
* **Placing the pages without hyperref.** The full build's `.focusmap` now records, when each file starts and ends, how many pages were shipped (`\ReadonlyShipoutCounter`) and whether the page in progress held something or floats waited. A chapter that starts with a page break after a filled page starts one page later (two with waiting floats): the splice uses that, and the focus run now counts those pages too, so chapters 8 to 10 of the bench report (plain `\input` after a chapter whose last page was not cleared) are numbered like the full PDF (they were one or two pages behind; `--focus` had the same error and gets the same fix). All 13 units of the bench report: first page identical to the full PDF's, page count equal to the full build's.
* **SyncTeX.** Preview runs use `-synctex=1`; the PDF and `.synctex.gz` of each browser tab's newest preview are kept (16 at most), and `/synctex/edit?preview=<tab>` maps the overlay file back to the file being typed. Double-click on a preview page jumps (`tests/preview_e2e.py` checks it).
* **`\include` and package-read files.** The typed text replaces the file through `\declare@file@substitution` (LaTeX 2020-10), so `\input`, `\InputIfFileExists` and packages read it; `\include` uses the name for `\includeonly` and its `.aux`, so there `\@input@` is redirected instead. A file not in the input tree but in the full build's `.fls` is previewed in the chapter the same browser tab previewed last.
* **Priority.** For a minute after a preview, every LaTeX run except the preview's own starts at nice 10 (Windows: below normal). On Linux with autogroup scheduling a process in its own session forms its own group, where nice alone changes nothing, so the build's session lowers its group too; bubblewrap's `--new-session` makes another group that cannot be lowered from outside, so figure jobs also wait (5 s at most) while a preview typesets. Effect, with a forced full build alongside: medians unchanged within noise (0.90 / 0.77 s against 0.85 / 0.76 s), worst previews 0.4 to 0.7 s shorter, the full build 5 to 9 s longer.
* Partial figure listing (`Figures.discover_partial`, from the performance review): a skipped file whose text shows fewer pictures than the map recorded draws some with a macro; it is read instead of skipped, so a changed macro definition cannot leave figure numbers wrong.

### Limits

* Everything outside the chapter is the last full build's: page numbers after the chapter do not move, the table of contents shows the old headings.
* A chapter that does not start with a page break but shares the previous chapter's last page is shown from the top of a fresh page.
* Forward search lands on the chapter's first page while a preview is shown.
* One or two waiting TeX processes per document previewed (about 40 MB resident each for this document), plus at most two running.

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
