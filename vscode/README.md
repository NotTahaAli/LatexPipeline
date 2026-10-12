# LaTeX Pipeline for VS Code

Edit a LaTeX Pipeline document in VS Code: one served by `scripts/serve.py` on this computer, one shared with you through a share link, or a project on a hosted server (`scripts/host.py`). Files open and save through the editor's API, so your role applies (a view link cannot save). Builds run on the server; their errors and warnings show in the Problems panel, and the PDF opens in a tab.

Install: `npx @vscode/vsce package` in this folder makes `latex-pipeline-0.1.0.vsix`; in VS Code run "Extensions: Install from VSIX...". It is not on the Marketplace.

Use: run "LaTeX Pipeline: Connect to an editor or hosted project" from the command palette and paste the address you would open in a browser (`http://localhost:8000/`, a share link with `?token=`, or `https://<server>/p/<project>/`). A hosted project asks for a personal API token (Account page, API tokens). The document appears as a folder in the Explorer. "Build", "Show the PDF", "Open a file" and "Disconnect" are in the command palette; the status bar shows the build state and opens the PDF (or the Problems panel after a failed build).

See the main README, section "VS Code", for details.
