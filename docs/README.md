# Documentation

Every page of the LaTeX Document Pipeline docs, grouped by what you want to do. New here? Start with [Install](getting-started/install.md), then pick one path.

## Getting started

| Page | For |
| --- | --- |
| [Install](getting-started/install.md) | Python, a TeX distribution, or the single-file binaries |
| [Compile only](getting-started/compile-only.md) | Build documents locally and publish PDFs from GitHub Actions |
| [Editor](getting-started/editor.md) | Edit in the browser with a live PDF preview |
| [Self-host](getting-started/self-host.md) | Run a multi-user server for a team |

## Guides

| Page | For |
| --- | --- |
| [Building](guides/building.md) | Build, rebuild and watch documents; what lands in `out/` |
| [Build tools](guides/build-tools.md) | Format source, tidy `.bib` files, camera-ready checks, page and dependency queries |
| [Documents](guides/documents.md) | Add a document, start from a template, reproducible PDFs |
| [Export](guides/export.md) | DOCX, HTML, arXiv upload, cited-only `.bib` |
| [PDF/A and tagged PDF](guides/pdfa-tagged.md) | Archival and accessible PDFs, and what is validated |
| [Review markup](guides/review-markup.md) | Write proposed changes in the source and show them in the PDF |
| [Grammar](guides/grammar.md) | Grammar and style checks with LanguageTool |
| [References](guides/references.md) | Manage, look up and verify `.bib` entries |
| [Zotero](guides/zotero.md) | Sync references from Zotero or Better BibTeX |
| [Large documents](guides/large-documents.md) | TikZ caching, single-chapter builds, draft mode |
| [VS Code](guides/vscode.md) | Build from VS Code; the editor extension |
| [MCP](guides/mcp.md) | Work on documents from Claude, ChatGPT, Codex and other AI apps |
| [MCP clients](guides/mcp-clients.md) | Exact configuration for each AI app |
| [CI](guides/ci.md) | GitHub Actions, the `pdfs` release, lint and CI reports |

## Editor

| Page | For |
| --- | --- |
| [Overview](editor/overview.md) | Layout, command palette, build results, offline mode |
| [Writing](editor/writing.md) | LaTeX writing aids, completion, snippets, math shortcuts |
| [Tables, pictures and formatting](editor/tools.md) | Table and TikZ editors, Markdown, format file, tidy `.bib` |
| [Navigation](editor/navigation.md) | Search, go to definition, rename labels, outline |
| [Live preview](editor/live-preview.md) | The PDF following your typing |
| [Collaboration](editor/collaboration.md) | Co-editing, comments, suggestions, locks, linked files |
| [History](editor/history.md) | Versions: compare, restore, download, clean up |
| [Sharing](editor/sharing.md) | Share links and what each role may do |
| [AI assistant](editor/ai-assistant.md) | The assistant, its keys, limits and providers |
| [Git](editor/git.md) | Commit, pull, push and switch branches |

## Hosting

| Page | For |
| --- | --- |
| [Install](hosting/install.md) | Accounts, workspaces, roles; install and first start |
| [Docker](hosting/docker.md) | Docker Compose and Coolify |
| [Sign-in](hosting/sign-in.md) | Google, OpenID Connect and GitHub sign-in |
| [Features](hosting/features.md) | AI assistant, Zotero, Git sync, API tokens on the server |
| [AI clients](hosting/ai-clients.md) | Members connecting their own AI apps over MCP |
| [Operations](hosting/operations.md) | TLS, systemd, sandbox, backups, updates |

## Reference

| Page | For |
| --- | --- |
| [CLI](reference/cli.md) | `build.py` and `serve.py` flags |
| [build.toml](reference/build-toml.md) | Every per-document setting |
| [Compile API](reference/compile-api.md) | `POST /api/v1/compile`, local and hosted |
| [Shortcuts](reference/shortcuts.md) | Keyboard and math shortcuts |

## How it works

| Page | For |
| --- | --- |
| [Security](explanation/security.md) | What sharing changes, roles, rate limits |
| [Sandbox](explanation/sandbox.md) | Running LaTeX under bubblewrap |
| [Real-time preview](explanation/realtime.md) | How the live preview got fast |
| [Benchmarks](explanation/benchmarks.md) | Build and preview timings |

Contributing: [CONTRIBUTING.md](../CONTRIBUTING.md). Security reports: [SECURITY.md](../SECURITY.md).
