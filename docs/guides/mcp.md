# Use with AI clients (MCP)

This page is for people who want to work on their documents from an AI app they already pay for, through the local MCP server, and covers the server, its tools and its safety rules.

## What MCP gives you

Claude (claude.ai, Desktop, mobile, Claude Code), ChatGPT, OpenAI Codex, Cursor, VS Code with GitHub Copilot, Windsurf and Gemini CLI can talk to this project over the Model Context Protocol (MCP). The model, its usage and its billing are your plan's. There are two servers:

- **Local**, `python scripts/mcp_server.py` (stdio): the documents in this repository, on your computer. This page covers it.
- **Hosted**, `https://<your server>/mcp` (Streamable HTTP with OAuth sign-in): projects on a `host.py` server. See [Hosted features](../hosting/features.md).

Per-client setup for both is in [MCP client setup](mcp-clients.md).

### Subscription or API key

The editor's own AI assistant calls the Anthropic API (or an OpenAI-compatible endpoint the owner configured) and needs an API key, billed per token; a local model server may need none. A Claude subscription (Pro, Max, Team, Enterprise) cannot pay for those calls, and this project never uses a subscription's sign-in to call the API. To use a subscription, connect Claude (or another app) to the MCP server: the app runs the model under your plan and calls the tools here.

## Run the local server

```bash
python scripts/mcp_server.py [DOC ...] [--source DIR] [--read-only] [--sandbox] [--allow-build-config]
```

| Option | Effect |
|---|---|
| `DOC ...`, `--source DIR` | Select documents, as with `serve.py`. |
| `--read-only` | Offer only the tools that read. |
| `--sandbox` | Run builds in bubblewrap (Linux only), as in [sandboxed builds](../explanation/sandbox.md). |
| `--allow-build-config` | Let tools change, create or rename onto `build.toml` and `latexmkrc`. Off by default. |

The server runs as you, like `serve.py` without sharing: builds use the documents' `build.toml` and `latexmkrc`. stdout carries only the protocol; progress and errors go to stderr. In client configs use absolute paths; on Windows use `python` and `C:\\path\\to\\LatexPipeline\\scripts\\mcp_server.py` in JSON.

> **Warning.** `build.toml` and `latexmkrc` can turn on shell escape or run programs, and the model reads text others may have written: a chapter from a co-author or a share link can carry instructions. Point the server only at documents you trust, or add `--sandbox`. Pass `--allow-build-config` only when you accept that risk.

### Running beside the editor

If `serve.py` is running, its editor picks up changes on disk. Files it has open in a co-editing room are not written: `serve.py` lists them in `.latex-history/<document>/rooms.json`, refreshed every 10 s while a room is open. The tool says so, and the model can use `add_suggestion` instead.

## Tools

All tools take a `project` (from `list_projects`) except the first three. Paths are relative to the document folder; nothing outside it is reachable.

| Tool | Does | Needs |
|---|---|---|
| `list_projects`, `search`, `fetch` | Projects you can use; word search over all of them (ChatGPT's deep research and company knowledge need exactly `search` and `fetch`); one file by search id | read |
| `list_files`, `read_file`, `grep` | Files; a file or a line range; lines containing text (regular expressions locally only; hosted searches literal text) | read |
| `outline`, `word_counts`, `references`, `lint` | Headings with file, line and word counts; words per part, chapter and section (texcount when installed); labels, `.bib` entries, missing and unused citations, `\ref` to unknown labels; lint findings | read |
| `list_versions`, `version_diff` | Version history of a file or the project; a line diff of a version against now or against the version before | read |
| `build_status`, `list_reviews` | Last build: ok, pages, errors with file, line, log excerpt and a plain-language hint, the end of the log; open comments (with ids) and suggestions | read |
| `list_members` | People on the project by name (hosted: workspace members, with roles for its admins; locally: commenters, people with it open, named share links) | read |
| `render_page`, `source_for_pdf_position` | A PDF page as PNG (by number, or the page showing a source line through SyncTeX; the chapter preview with `chapter`); the file and line behind a point of a page (inverse SyncTeX) | read |
| `edit_file`, `write_file`, `rename_file`, `delete_file` | Exact text replacement (must match once, or `replace_all`); create or replace a file; rename; delete | write |
| `build`, `preview_chapter` | Build now and return the report; typeset one chapter (`--focus`) | write |
| `restore_version`, `label_version` | Put a file or a named version back (the current text is kept as a version first); name the project as it is now | write |
| `bib_add_entry`, `bib_edit_entry`, `bib_delete_entry`, `bib_import` | `.bib` entries given as BibTeX; import pasted BibTeX, RIS or a DOI (Crossref; keys and DOIs already in the project are skipped); only the touched bytes change | write |
| `bib_lookup`, `insert_citation` | Missing fields from Crossref (shown, added only with `apply`); `\cite{key}` (or `citep`, `parencite`, ...) after a quoted passage, for keys that exist | write |
| `zotero_sync_preview`, `zotero_apply` | Compare a `.bib` file with Zotero (locally the owner's library, hosted your own from the Account page); add new entries and take changed fields by key, never deleting | write |
| `new_document_from_template` | A new document from the article, report, beamer or letter template (hosted: a project in a workspace you connected as a whole and edit) | write |
| `export_docx` | Word export with pandoc; local server only and never while sharing (like the editor) | write |
| `add_comment`, `add_suggestion`, `reply_comment`, `resolve_thread` | A comment thread on quoted text; a suggested replacement (up to 4 places as one suggestion) that people accept or reject in the editor; reply; resolve or reopen | review (editor or reviewer) |
| `delete_comment`, `withdraw_suggestion` | Delete a comment or withdraw a suggestion this client made (not other people's) | review (editor or reviewer) |
| `grammar_check` | LanguageTool findings with line, column and replacements (as in the editor: a local server, the public service only if the owner allowed it, never while sharing unless allowed) | review (editor only) |

The server offers up to 41 tools: fewer with `--read-only` or a read-only connection, and `export_docx` only locally. Pages render with `pdftoppm` (poppler-utils) or else `mutool`; with neither, `render_page` says so and the other tools work.

## Safety rules

- **People decide.** Accepting or rejecting suggestions is not a tool, and neither is deleting versions (history clean-up).
- **Open files become suggestions.** A change to a `.bib` or `.tex` file someone has open in the editor becomes a suggestion with the same change (the tool says so) instead of a write.
- **Network only on request.** `bib_import` with a DOI, `bib_lookup`, the Zotero tools and `grammar_check` send only what the editor would, only when called, under the editor's rate limits.
- **Same save path as the editor.** Writes are atomic, the version history records them (author "*client* (MCP)" locally, "*you* via *client*" hosted) and open editors reload.
- **Capped results.** At most 100,000 characters of text, PNGs up to 2 MB and 500 list items.
- **Build config is protected.** `build.toml` and `latexmkrc` are never changed without `--allow-build-config` (see the warning above).

There is no latexdiff tool. The editor's History tab builds a latexdiff PDF, and `version_diff` shows what changed.

## Protocol

The server speaks MCP revision 2026-07-28 (each request carries its version, `server/discover`, no sessions). For the clients most apps ship today it also accepts the `initialize` handshake of 2025-11-25, 2025-06-18 and 2025-03-26. Only tools are offered (no resources, prompts or sampling), no session ids are issued, and schemas are flat (strings, integers, numbers, booleans), so every client accepts them.

## Next

- [MCP client setup](mcp-clients.md)
- [Hosted features](../hosting/features.md)
- [AI assistant in the editor](../editor/ai-assistant.md)
