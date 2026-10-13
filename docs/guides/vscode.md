# VS Code

How to build documents from VS Code and how to edit a local or hosted document from VS Code with the bundled extension.

## Build with tasks

[`.vscode/tasks.json`](../../.vscode/tasks.json) has the build commands as tasks (Terminal, Run Task).

| Task | What it does |
| --- | --- |
| **LaTeX: build changed documents** | Default build task. Builds documents whose PDF is missing or out of date. |
| **LaTeX: build one document** | Asks for a name or glob and builds it, even when up to date. |
| **LaTeX: build all (--force)** | Rebuilds every document. |
| **LaTeX: watch** | Rebuilds on save. |
| **LaTeX: list documents** | Lists the documents. |
| **LaTeX: clean** | Cleans the build output. |

Build errors appear in the Problems panel. [`.vscode/settings.json`](../../.vscode/settings.json) makes LaTeX Workshop build through `build.py`. Its PDF viewer cannot read `out/<doc>.pdf`, so use the editor's live preview or an external viewer.

## Install the extension

[`vscode/`](../../vscode/) is a small extension (plain JavaScript, no dependencies, no build step). It edits a document served by `serve.py`, or a hosted project, from inside VS Code. It is not on the Marketplace and needs VS Code 1.82 or newer.

1. In `vscode/`, run:

   ```bash
   npx @vscode/vsce package
   ```

   This makes `latex-pipeline-0.1.0.vsix`.
2. In VS Code, run "Extensions: Install from VSIX..." and pick that file.

## Connect

Run **LaTeX Pipeline: Connect to an editor or hosted project** from the command palette. Paste the address you would open in a browser.

| Address | What you get |
| --- | --- |
| `http://localhost:8000/` | Your own local editor, as the owner, with no sign-in. |
| A share link (`https://...?token=...`) | The link's role. The extension exchanges the token for the editor's cookie once and keeps the cookie in VS Code's secret storage. |
| `https://<server>/p/<project>/` | A hosted project. It asks for a personal API token with the `projects` scope (Account > API tokens on the hosted server; see [hosted features](../hosting/features.md)). |

Plain `http://` is accepted only for this computer.

## Edit files

The document appears as a folder in the Explorer (`latexpipeline:` files). Opening, saving, creating, renaming and deleting go through the editor's API, so the server's rules apply:

- A view link cannot save.
- Build-config files stay owner-only.
- Every save is a version in History.

A save sends the version the file was read at. If someone saved it since, or the file is open in a co-editing room in the browser and changed there, the save is refused instead of overwriting, and VS Code says so. Reopen the file and save again.

Changes made elsewhere (the browser, other people) arrive over the editor's long-poll and refresh the Explorer.

## Commands

| Command | What it does |
| --- | --- |
| **Build** | Asks the server to build (edit role). The status bar shows the build state; build errors and the log's warnings land in the Problems panel. |
| **Show the PDF** | Opens the PDF in a tab (pdf.js from the same pinned CDN URL as the browser editor), refreshed after each good build. |
| **Open a file** | Picks a file by name. |
| **Disconnect** | Forgets the address and the token. |

## Next

- [The browser editor](../editor/overview.md)
- [Sharing a document](../editor/sharing.md)
- [MCP for AI clients](mcp.md)
