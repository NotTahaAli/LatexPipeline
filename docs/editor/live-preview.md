# Live preview

This page explains how the editor's PDF follows your typing before you save, what it covers, and its limits.

## How it works

While you type in a chapter file, the PDF follows:

1. 0.7 s after you pause, the chapter around the cursor is typeset from the editor's text, saved or not.
2. Its pages replace that chapter's pages in the PDF pane, in place, without a blank and without moving your scroll position.
3. The status line in the PDF toolbar says "Live: 0.8 s", or the error (with the line in its tooltip).

A keystroke cancels the preview in flight. Saving still runs the full build, and its PDF replaces the preview unless you typed again after that build started.

The preview uses `--focus`, so numbering, references and citations come from the last full build. A TeX process that has already loaded the preamble is kept waiting, so only the chapter itself is typeset. On a chapter of `bench/sample-report` that takes about 0.7 to 0.8 s, against 2.0 to 3.1 s for "Preview chapter" (see [Benchmarks](../explanation/benchmarks.md)).

## Turn it on or off

| Setting | Default |
|---|---|
| Settings > "Live preview" | On |

It needs the edit role and works with pdfLaTeX, XeLaTeX and LuaLaTeX.

## What follows the typing

- **New labels and citations:** labels the chapter defines, and citations of keys the full build never cited, resolve in the same preview. A second pass on another waiting process reads the first pass's labels. BibTeX runs in the sandbox on the full build's citations plus the new ones, then a third pass prints the numbers. With biblatex and biber, biber runs the same way on the run's `.bcf` with the full build's keys added, and the second pass prints them. The status tooltip says how many passes it took.
- **Other files:** `\include`d chapters and `.tex` files a package reads (`\InputIfFileExists`, once you previewed the chapter that reads them) are typeset from the editor's text too.
- **TikZ:** figures come from the full build when no picture of the chapter changed since.
- **SyncTeX:** double-click on a preview page jumps to the source like on any other page. The preview runs with SyncTeX, and its overlay of your unsaved text maps back to the file you are typing in.

## Typing in main.tex

Typing in `main.tex` between two chapters previews that part, from the end of the chapter before to the start of the next.

Adding, removing or changing an `\input`/`\include` line there pauses the live preview. The status shows "Live: rebuilding document" (or "... changed, rebuilds on save" before the autosave) until the full build that follows the save has finished. The old pages are taken away, never left standing.

## Editing the preamble

Editing the preamble, a local `.cls`/`.sty` or a file the preamble reads does not pause the preview:

1. A TeX process is started in the background from the edited text, saved or not.
2. The chapter you previewed last is typeset again on it. Its pages replace the old ones when they arrive; the old ones stay until then. This takes about 3 s on `bench/sample-report`, mostly loading the new preamble.
3. Chapters you type in afterwards use that preamble too, until the file on disk says the same. Other tabs keep the saved preamble.

References and the page layout still come from the last full build, and cached TikZ figures are not reused under an edited preamble.

## Page numbers

The preview's pages are placed in the full PDF from the number of pages the full build had shipped when the chapter started and ended (recorded in the `.focusmap`), so the splice works without hyperref.

When the previewed part has more or fewer pages than in the full PDF, the status says so ("Live: 0.8 s · later pages +1"). Its tooltip warns that page numbers after it may be off: the pages after the chapter keep the full build's numbers until the full build after the save.

## Build priority

While previews are in use, and for a minute after the last one, full builds and figure jobs run below normal priority, so the build of the autosave does not slow the preview down.

| Platform | Priority |
|---|---|
| Linux | `nice` 10, also applied to disk access |
| macOS | `nice` 10 |
| Windows | Below normal priority class |

## Limits

- Forward search lands on the chapter's first page while a preview is shown.
- The waiting process restarts when any of these change: `main.tex`'s preamble (saved or typed), a file it `\input`s, a local `.cls`/`.sty`, `build.toml`, the engine, or the sharing and sandbox settings.
- The waiting process stops after 10 minutes unused.

How this compares with TeXpresso and other live engines: [Real-time preview](../explanation/realtime.md).

## Next

- [Editor overview](overview.md)
- [Large documents](../guides/large-documents.md)
- [Real-time preview](../explanation/realtime.md)
