# Editor overview

This page describes the layout of the local editor (`serve.py`), its command palette, build results, the chapter preview, files and figures, and offline mode.

`python scripts/serve.py` serves the editor and live preview at http://localhost:8000, bound to 127.0.0.1 only. It rebuilds on save, keeps scroll and zoom, lists errors with editor links, and double-click in the PDF jumps to the source. It needs the `synctex` command-line tool from TeX Live. To start it, see the [quick start](../getting-started/editor.md).

## Requirements and fonts

- The editor loads PDF.js, CodeMirror, KaTeX and Yjs from pinned CDN versions, so it needs internet unless you set up [offline mode](#work-offline).
- Its fonts (Newsreader, Source Sans 3, IBM Plex Mono; SIL OFL) ship in `scripts/fonts/` and are served from `/fonts/`.

## Work offline

1. Download the libraries (about 3.4 MB, from the URLs pinned in the import map of `scripts/serve_ui/index.html`) into the git-ignored `scripts/serve_ui/vendor/`:

   ```bash
   python scripts/vendor_ui.py
   ```

2. Restart `serve.py`. While that directory exists, it serves only the files listed in `vendor/manifest.json`, and its Content-Security-Policy allows no CDN.

Delete the directory to go back to the CDNs. Re-run the script after bumping a pinned version.

## Layout

- **First visit:** a one-line quick start (Outline, Visual, Ctrl+K, double-click the PDF). "Got it" hides it for good; More > "Show getting-started tips" brings it back.
- **Outline:** lists sections with word counts. Hover a row for the target button to set a per-section word goal; the row fills as you write. Goals are stored in this browser.
- **Sidebar:** drag its right edge (or focus it and use the arrow keys) to resize it. Each tab (Files, Search, References, ...) keeps its own width in this browser; a double-click resets it.
- **Visual mode:** prose and headings in the serif (Newsreader), leftover LaTeX in monospace, in both themes.
- **PDF pane:** Fit width follows the pane until you zoom by hand. Pages stay white unless you turn on dark pages: the moon button in the PDF toolbar, Settings > PDF preview, or "PDF: dark pages" in Ctrl+K. Dark pages are off by default and remembered in this browser; they invert the pages and turn the hues back, so figures keep roughly their colours.
- **Phone:** the error and warning badges become one count chip, and a Source / Split / PDF bar at the bottom picks which pane shows.
- **Accessibility:** the page is keyboard operable (dialogs trap focus and return it, the build result is announced to screen readers) and passes axe-core in light and dark.

### Shared links

| Link | What you see |
|---|---|
| View | A "View only" chip. Rebuild and Paragraph stay visible but disabled, with the reason as their tooltip. View links cannot start previews or change files. |
| Review | A "Reviewer" chip. The editor opens in Suggest mode, locked on. |
| Edit | Can start previews and change files, for the shared document only. |

## Command palette

The "..." (More) button opens the command palette: Ctrl+K, or Cmd+K on a Mac. Commands are grouped as Panels, Build, Review, Document, Session, Help, then Other; typing filters to a flat list. Hover a row, or any button with an icon, for a one-line explanation.

The Document group holds:

- "Download PDF" (also the download icon in the PDF toolbar).
- "Download source (zip)": any role that can read the document may use it; build-config files are included only for the owner.

## Build results

- **While a build runs:** the top bar counts the seconds, a bar fills against the previous build's time, and an empty PDF pane shows a page skeleton.
- **After a failed build:** the old PDF stays with a "last good PDF" note. The failing line is marked in the editor and the first error opens by itself with its hint.
- **After a restart:** "Up to date" still shows the pages, build time and warnings of the last build, read back from `out/`.
- **Warnings tab** (Problems panel): parsed from the LaTeX log. It lists undefined references and citations, overfull and underfull boxes, package warnings and BibTeX warnings, filterable by kind. Each has a plain-language hint where one exists, and a link to its source line.
- **Log tab:** "Only lines that need action" filters the log like `build.py --quiet-log`. The choice is remembered in this browser.
- **Stop** (next to Rebuild, edit role): kills the build's latexmk, and the build fails with "Build cancelled.". A TikZ figure job already running finishes first. The next save builds again.
- **Notifications:** Ctrl+K > "Desktop notification when a build ends" asks the browser for permission. It then notifies you when a build finishes or fails while the window is in the background.

## Preview one chapter

"Preview chapter" (status bar or command palette) builds only the chapter you are editing, keeping the numbering and references of the full document. It shows with a "Chapter preview" bar and a "Back to full PDF" button.

- Settings can build it after each save.
- Jump-to-PDF is off while it is shown.
- The first preview of a document builds the whole thing once.

For preview while you type, see [Live preview](live-preview.md).

## Manage files

The Files tab has new file, new folder, rename/move (F2) and delete (Delete key, with a confirmation).

- `main.tex`, dotfiles and, over a share link, build-config files cannot be touched.
- Open co-editing rooms of a renamed or deleted file are closed.

## Add a figure

1. Drop or paste a png, jpg or pdf into the editor.
2. In the figure dialog, set the file name, folder, caption, label, width and placement.
   - Folder: `figures/` or `Figures/` when one exists; a new folder is created.
   - Label: `fig:<name>`.
3. Confirm. The file is saved and a whole `figure` environment is inserted, or only `\includegraphics` if you untick that. Skip leaves the image out.

Images up to 8 MB are accepted; the content is checked and svg is refused.

## Spell check

The browser's own spell check (no dictionary shipped) underlines misspelled words in visual mode and the paragraph panel only, never LaTeX commands or keys. Settings turns it off.

## Next

- [Writing LaTeX](writing.md)
- [Live preview](live-preview.md)
- [Keyboard shortcuts](../reference/shortcuts.md)
