# Benchmarks: this pipeline against other ways to build LaTeX

Document: `bench/sample-report` (303 pages, 25 TikZ/pgfplots figures, ten bibunit-style chapter bibliographies, custom class). Times are wall-clock seconds, median / min of 3 runs. Measured numbers are marked **measured**; everything under "Published" comes from the linked sources and was not re-measured.

## Machine and method

* 4 CPUs, Linux, TeX Live 2023 (Debian), pdfTeX 1.40.25, latexmk 4.83, arara 7.1.2, Tectonic 0.15.0. The machine was shared with other jobs while the other tools were measured (load average 1.5 to 4, sometimes higher); the final re-measurement of this pipeline against plain latexmk ran at load 1.1 to 1.6.
* Same sources for every tool (a fresh copy of `bench/sample-report`). Runs alternate between tools within each scenario, so load changes affect all tools.
* Cold: fresh copy (this pipeline: also `build.py --clean`). No-op: rebuild with nothing changed. Text edit: one word changed in `Chapters/chapter5/subsection1.tex` (a file that holds a figure) or `Chapters/chapter3/subsection3.tex` (no figure). Figure edit: one pgfplots `coordinates` value in `Chapters/chapter6/subsection2.tex`. Edits are toggled back and forth so each run really changes the file.
* This pipeline: `python3 scripts/build.py --source bench sample-report` (commit ca12fc6), and `--focus Chapters/chapter5`.
* Plain latexmk: `latexmk -pdf main.tex`.
* Overleaf-equivalent: the command from overleaf/overleaf `services/clsi/app/js/LatexRunner.js`, `latexmk -cd -jobname=output -auxdir=D -outdir=D -synctex=1 -interaction=batchmode -time -f -pdf D/main.tex`, plus `$go_mode = 3;` from `server-ce/config/latexmkrc`, with a persistent output directory. Not replicated: Docker sandbox, file sync, queueing, PDF post-processing. Real Overleaf CE was not run.
* Naive script: `pdflatex; bibtex` for every `.aux` with `\bibdata` (ten); `pdflatex; pdflatex`. arara: the same sequence as directives (a three-line custom rule runs bibtex per unit; the stock rule takes one aux file). Tectonic: `tectonic main.tex` after one unmeasured run to fill its bundle cache.

## Measured results

| tool | cold | no-op | text edit (file with figure) | text edit (no figure) | pgfplots edit | `--focus` |
|---|---|---|---|---|---|---|
| **this pipeline** (ca12fc6) | 60.7 / 59.7 | 0.37 / 0.35 | 12.9 / 12.6 | 13.6 / 12.2 | 28.3 / 28.1 | 1.8 / 1.8 |
| plain `latexmk -pdf` (same session) | 99.3 / 98.9 | 0.12 / 0.12 | 25.1 / 24.8 | 25.4 / 25.2 | 25.4 / 24.8 | n/a |
| **this pipeline** (no-op and figure-edit fixes, later session) | 70.3 / 67.3 | 0.12 / 0.10 | 12.3 / 12.0 | 13.6 / 12.6 | 16.0 / 15.8 | 2.6 / 2.1 |
| plain `latexmk -pdf` (later session, alternating with the row above) | 113.4 / 108.8 | 0.12 / 0.11 | 27.5 / 25.3 | 25.5 / 24.9 | 27.5 / 26.7 | n/a |

The last two rows were measured in a later session on a slower machine state (plain latexmk cold took 113 s instead of 99 s; every ratio to plain latexmk is what to compare, not the absolute seconds). Two changes since ca12fc6: the no-op check reads the `.fls` with plain strings and one `kpsewhich` call (the pathlib version cost 0.25 s for the 3,700 recorded paths), and a figure edit lists only the changed files before compiling the figure (see below). Cold, text-edit and `--focus` rows are unchanged code paths; their differences from the first row are machine state (`--focus` 2.6 s median against 1.8 s was the noisiest).

Other tools, measured earlier the same day (this pipeline at 092eac7 measured 74.0 cold, 24.0 text, 27.5 figure in that run; the other tools do not depend on our code). These ran under more load, so treat differences below about 10% as noise. They do not distinguish the two text-edit cases:

| tool | cold | no-op | text edit | pgfplots edit |
|---|---|---|---|---|
| Overleaf-equivalent latexmk | 103.8 / 102.7 | 25.9 / 25.8 | 26.6 / 25.3 | 26.2 / 25.3 |
| naive script | 74.0 / 72.7 | 77.0 / 76.7 | 74.4 / 73.2 | 74.0 / 73.5 |
| arara | 76.1 / 74.1 | 77.9 / 76.8 | 81.1 / 71.6 | 75.8 / 75.3 |
| Tectonic | 178.9 / 172.3 | 186.6 (1 run) | 182.8 / 172.9 | 167.3 (1 run) |

Tiny document (`files/test`, 2 pages), measured: cold 0.77 s here against 0.62 s for plain latexmk, no-op 0.24 against 0.14, text edit 0.85 against 0.71. The wrapper costs 0.1 to 0.25 s on small documents.

## Where we win, tie and lose

* **Win, cold build:** about 39% faster than plain latexmk (60.7 vs 99.3 s) because figures compile in parallel and are cached, and latexmk's extra passes are avoided.
* **Win, text edits:** about 2x faster than plain latexmk (12.9 vs 25.1 s) for edits in files with or without figures, because the text pass includes finished figure PDFs instead of re-typesetting them.
* **Win, `--focus`:** 1.8 s for a chapter preview; none of the other tools has an equivalent. It is a preview, not the full PDF.
* **Win, no-op on Overleaf-style or always-full builds:** 0.4 s against 26 s (Overleaf's rc forces a pass) or 77 s (naive, arara).
* **Win, figure edit:** 16.0 vs 27.5 s for plain latexmk (was a loss, 28.3 vs 25.4 s). The old flow typeset the whole document once in draft mode to list the figures (11 s of 25), compiled the figure (1.3 s), then ran the text pass (12 s). Now only the files whose figure text changed are listed (the files that define nothing are skipped, as in a figure job; 1.5 s), the figure is compiled (1.3 s) and the text pass runs. The listing is a guess that the text pass checks: it compares every figure with its `.md5` and sets a mismatching one inline, and the build loop then syncs and runs again. It falls back to the full listing when a figure was added or removed in the changed file, a non-`.tex` input changed, or the `\input` tree is not known. Floor: one text pass (12 s) plus the figure job; there is none lower without changing what a pass does.
* **Tie, no-op:** 0.12 s against plain latexmk's 0.12 s.
* **Lose, tiny documents:** plain latexmk is 0.1 to 0.25 s quicker on a two-page document (Python start-up and cache checks); the no-op fix does not change that.
* **Tie/context:** the naive and arara times look equal to our cold build, but their output is wrong: three passes do not converge on this document, so table-of-contents page numbers are stale (section 3 listed on p.47 instead of p.48; 284 of 143246 word tokens differ). A correct naive script needs a fourth pass (about 98 s).
* Tectonic builds the document (303 pages, no unresolved references) but was 1.7x to 2.4x slower than pdfTeX here, has no incremental mode, and downloads its bundle on first use.

## Output check

All runs produced 303 pages and no `??` in the text. Plain latexmk and Overleaf-equivalent differ from this pipeline's PDF text by 48 word tokens (math glyph extraction order); Tectonic by 764 (XeTeX fonts and ligatures).

## Published (not measured here)

* Overleaf compile timeout: 10 s on the free plan, 240 s on premium plans: https://docs.overleaf.com/getting-started/free-and-premium-plans/plan-limits.md (these have changed over time). A 25 s pass of this document would not fit the free limit. Overleaf's advice for slow projects (draft mode, PDF instead of PNG/EPS/SVG, "externalize TikZ/pgfplots pictures"): https://docs.overleaf.com/troubleshooting-and-support/fixing-and-preventing-compile-timeouts.md
* Overleaf's compile command and forced pass: https://github.com/overleaf/overleaf/blob/main/services/clsi/app/js/LatexRunner.js and https://github.com/overleaf/overleaf/blob/main/server-ce/config/latexmkrc
* TeXpresso, live preview on a modified Tectonic engine that re-renders only changed parts; its README gives no numbers and calls the project early stage (Linux and macOS): https://github.com/let-def/texpresso. Not run here.
* Typst, a different language: Zerodha reports Typst "2 to 3 times faster" on small files and about 1 minute against about 18 minutes with lualatex for a 2,000-page table-heavy document (uncontrolled, engine for the small-file case not stated): https://zerodha.tech/blog/1-5-million-pdfs-in-25-minutes/. Typst's incremental compilation is built on memoization (`comemo`): https://github.com/typst/typst/blob/main/docs/dev/architecture.md
* Tectonic, "powered by XeTeX and TeXLive", no speed claims found: https://github.com/tectonic-typesetting/tectonic
* mylatexformat (precompiled preamble) is documented as speeding up heavy preambles, without timings: https://www.ctan.org/pkg/mylatexformat. latexmk's example rc notes xelatex/lualatex cannot use it: https://ctan.net/support/latexmk/example_rcfiles/precompile-preamble_latexmkrc
* Academic work on incremental TeX compilation: none found. `\include`/`\includeonly` as the built-in partial compile: https://www.tug.org/pipermail/texhax/2008-October/011254.html

## Caveats

* Shared machine; medians of 3 carry a few seconds of noise, more for the earlier runs of the other tools.
* The per-pass cost of this document (about 12 to 25 s) comes mostly from text, hyperref and cleveref over 303 pages; documents with heavier figures gain more from figure caching.
* The cold advantage needs free CPUs for the parallel figure pass.
* Overleaf-equivalent is the command only; real Overleaf adds network, sandboxing and queue time.
* Edits are one word or one coordinate inside a chapter; edits that move pagination or labels cost extra passes in every latexmk-based tool.
* Raw per-run data and the harness scripts are not part of the repository.
