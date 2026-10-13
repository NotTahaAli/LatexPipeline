# PDF/A and tagged PDF

How to build archival (PDF/A) and accessible (tagged, PDF/UA) PDFs, what is validated, and which TeX Live versions and engines pass; for authors who must meet these standards.

## Build a PDF/A file

1. Remove any `\DocumentMetadata` line from `main.tex`.
2. Set the level in `build.toml`:

   ```toml
   pdfa = "2b"
   ```

   Also accepted: `"a-2b"`, `"3b"`, `"4f"` and the other levels listed in [build.toml](../reference/build-toml.md).
3. Build. The build injects `\DocumentMetadata{pdfstandard=a-2b,lang=...}` before `\documentclass` through latexmk's `-usepretex`.

Requirements:

- LaTeX 2023-06 or newer.
- PDF/A-4 (`"4"`, `"4e"`, `"4f"`, PDF 2.0) needs the 2025-06-01 kernel. Otherwise the log says `WARNING: pdfa = a-4f needs LaTeX 2025-06-01 or newer; not applied.`
- The `a` levels need tagging (`tagged = true`).

The same injected code also:

- asks xcolor for RGB output, because cmyk colours break PDF/A against the RGB OutputIntent
- loads pdfTeX's glyph-to-Unicode maps, including `glyphtounicode-cmr` for math symbols (needed by the `u` level)
- turns off object streams for PDF/A-1

Not converted: images embedded as CMYK, and transparency in included PDFs.

## Build a tagged PDF

```toml
tagged = true
```

This adds a structure tree (headings, lists, links, a language) for screen readers and PDF/UA. It goes into the same `\DocumentMetadata` line and combines with `pdfa`. With `pdfa = "2a"` (the accessible level) the PDF/A-2a rules are met too.

The setting depends on the LaTeX kernel, which is tested at build time, so one setting works everywhere:

| Kernel | What `tagged = true` uses |
| --- | --- |
| 2025-06-01 or newer | `tagging=on` |
| 2023-11-01 up to 2025-06-01 (TeX Live 2023 to 2025) | `testphase={phase-III,firstaid}` |
| older | `WARNING: tagged = true needs LaTeX 2023-11-01 or newer` in the log; the PDF is untagged |

Set the title yourself, because PDF/UA requires it. hyperref is asked for `pdfdisplaydoctitle`.

```latex
\hypersetup{pdftitle={...}}
```

## What is declared and validated

The declared standards are exactly the ones veraPDF validates:

| `build.toml` | kernel 2025-06-01 or newer declares | veraPDF checks | TeX Live 2023 to 2025 declares |
|---|---|---|---|
| `tagged` | `pdfstandard=ua-1` | `ua1` | `pdfstandard=ua-1` |
| `tagged` + `pdfa` 1, 2 or 3 | `pdfstandard={a-2a,ua-1}` (LaTeX News 41) | the PDF/A level and `ua1` | `pdfstandard=a-2a`, no PDF/UA (the note says PDF/UA is not validated) |
| `tagged` + `pdfa` 4 | `pdfstandard={a-4f,ua-2}` (PDF 2.0) | the PDF/A level and `ua2` | not available, warning |

After the build:

1. A PDF/A file is checked for the XMP `pdfaid` declaration and an OutputIntent. A tagged file is checked for a `/StructTreeRoot`; the log note says if it is missing.
2. If [veraPDF](https://verapdf.org) (`verapdf`) is on `PATH`, the PDF is validated against the requested PDF/A level. The log note says passed, or which rules failed.
3. veraPDF validates the PDF/UA part that the XMP (`pdfuaid:part`) declares, so a file that carries no identification is not judged against UA.

CLI builds run veraPDF, except `--focus`. The editor's builds skip it, because it takes seconds.

## Workarounds applied for you

- **Embedded files.** PDF/A-1, A-2 and A-4 without `f` forbid embedded files that are not PDF/A. The tagging kernel attaches the TeX source of formulas and two CSS files. For those levels `\tagpdfsetup{attach-css=false,math/tex/AF=false}` is set (each key only if tagpdf has it). A-3 and A-4f allow the files and keep them.
- **pdfTeX space font.** With pdfTeX, tagging writes a real interword space glyph from the `pdftexspace` font. In TeX Live 2023 its metrics disagree (`pdftexspace.tfm` 0.334249, the font program 333) and veraPDF fails "glyph width information ... shall be consistent". The font is measured and, only if it is too wide, the old `dummy-space` font is selected with `\pdfspacefont` (pdfTeX 1.40.25 or newer). This keeps the spaces in text extraction. A TeX Live tree with the corrected metrics (2026 here) keeps the default font.

## Support matrix

Checked with veraPDF 1.28.2 on a small document (title with `pdftitle`, bold, link, list, equation; [`tests/fixtures/tagged`](../../tests/fixtures/tagged)) and, where it builds, `files/test`, built by `build.py` with each of the three engines:

| | TeX Live 2026 (kernel 2026-06-01; pdfTeX 1.40.29, LuaHBTeX 1.24.0, XeTeX 0.999998) | TeX Live 2023 (Debian; kernel 2023-11-01, pdfTeX 1.40.25) |
|---|---|---|
| `tagged` (UA-1) | pdfLaTeX, LuaLaTeX, XeLaTeX pass | pass (pdfLaTeX only with the space-font workaround; `files/test` needs a `pdftitle`) |
| `tagged` + `2a`, `2b`, `2u`, `3a`, `3b` | PDF/A and UA-1 pass, all engines | PDF/A passes, all engines; no PDF/UA identification |
| `tagged` + `4f`, `4` | PDF/A-4 and UA-2 pass with pdfLaTeX and LuaLaTeX; XeLaTeX fails A-4 (xdvipdfmx writes an Info dictionary) | warning, untagged |
| `pdfa` only: `1b`, `2b`, `2u`, `3b` | pass, all engines | pass, all engines except `1b` with XeLaTeX ("Xref streams shall not be used") |
| `pdfa` only: `4f`, `4` | pdfLaTeX and LuaLaTeX pass, XeLaTeX fails (Info dictionary) | warning, no PDF/A |

`pdfstandard=ua-2` alone (no `build.toml` key; `tagged` stays UA-1) also passes `ua2` with all three engines on TeX Live 2026.

CI builds the four fixtures in `tests/fixtures/tagged` on the tug.org TeX Live and fails the `tagged-pdf` job if veraPDF (pinned 1.28.2 jar from Maven Central, checked by SHA-256) does not pass the standards each declares. `python3 tests/tagged_check.py` does the same locally.

## Known limits

- Tagging is still maturing. Classes and packages that are not tagging-ready can produce an incomplete tree, and tagging slows the LaTeX run.
- Any `\DocumentMetadata`, so `pdfa` too, makes titlesec stop on the TeX Live 2026 kernel: `Package titlesec Error: No format for this command` at `\begin{thebibliography}`. So `bench/sample-report` (a class with titlesec) builds with `pdfa = "2b"` (veraPDF passes) only on TeX Live 2023, and with `tagged` on neither (TeX Live 2023 stops in cleveref).
- Whether formulas get MathML depends on the engine and packages, and was not checked.

## Accessibility checks

After a successful build with `tagged = true`, the PDF and the sources are checked. Sources are `main.tex`, the files it `\input`s or `\include`s, and local `.sty`/`.cls` files; comments are ignored. Each finding comes with a hint:

- **Missing title:** the XMP `dc:title`, else the Info `/Title`, is empty. Set `pdftitle`.
- **No alt text:** `\includegraphics` without the kernel's `alt=` key (or `artifact` for decorative images, or `actualtext`).
- **Skipped heading levels:** for example `\section` followed by `\subsubsection`.
- **Incompatible packages:** packages or classes the [LaTeX Tagging Project](https://latex3.github.io/tagging-project/tagging-status/) lists as currently incompatible or unsupported. This is a curated list of common ones, such as caption, float, listings, multirow, titlesec, wrapfig and beamer, taken from that page on 2026-10-11. Nothing is fetched during a build.
- **Slides:** beamer is reported with what that page says about it: no support, because its internals rebox material in a way no tagging can follow. The Project recommends the `ltx-talk` class for tagged presentations.
- **Untitled frames:** every `frame` without a title (`\begin{frame}{Title}` or `\frametitle`; title-page frames excepted), since slide titles are what screen readers navigate by. Images on slides go through the same alt-text check.

The checks run only for tagged documents. An untagged PDF carries no structure for alt text or headings to land in, and warning every document would bury the warnings that matter.

Findings appear as:

- lines starting with `ACCESSIBILITY:` at the top of `out/<name>.log`
- a count in the build summary, and the `a11y` key of `build-report.json`
- an Accessibility filter in the editor's Warnings tab
- notices in `ci_report.py lint`, never failures, also with `--strict`

## Next

- [build.toml reference](../reference/build-toml.md)
- [Export](export.md)
- [CI](ci.md)
