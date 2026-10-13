# Editor quick start

This tutorial takes you from a document folder to editing it in the browser with a live PDF beside the source.

You need Python, a LaTeX distribution with the `synctex` command-line tool (TeX Live ships it), and internet access for the editor's libraries, unless you set up [offline mode](../editor/overview.md).

## Start the editor

1. Start the server from the repository root:

   ```bash
   python scripts/serve.py
   ```

   It opens http://localhost:8000 in your browser and listens on 127.0.0.1 only.
2. To serve only some documents, use another port or skip the browser, add options:

   ```bash
   python scripts/serve.py [DOC ...] [--port 8000] [--no-open] [--source DIR] [--editor vscode]
   ```

| Option | What it does |
|---|---|
| `DOC ...` | Documents to serve, by name or glob (default: all) |
| `--port 8000` | Port to listen on (default 8000) |
| `--no-open` | Do not open a browser |
| `--source DIR` | Directory holding the documents (default `files/`) |
| `--editor vscode` | URL scheme for source links (`vscode`, `cursor`, ...; default `vscode`) |

## Edit and see the PDF

1. Open a chapter file in the editor pane. The first visit shows a one-line quick start (Outline, Visual, Ctrl+K, double-click the PDF); "Got it" hides it.
2. Type. About 0.7 s after you pause, the chapter around the cursor is typeset from your unsaved text and its pages replace that chapter's pages in the PDF pane. The PDF toolbar shows "Live: 0.8 s" or the error.
3. Save (Ctrl+S, or wait for autosave). Saving runs the full build; its PDF replaces the preview. Scroll position and zoom are kept.
4. If the build fails, the old PDF stays with a "last good PDF" note, the failing line is marked in the editor, and the first error opens with a hint.
5. Double-click in the PDF to jump to the source line.

## Find commands

Press Ctrl+K (Cmd+K on a Mac), or click "...", to open the command palette. Type to filter. Hover a row or any icon button for a one-line explanation.

## Next

- [Editor overview](../editor/overview.md)
- [Live preview](../editor/live-preview.md)
- [Writing LaTeX in the editor](../editor/writing.md)
