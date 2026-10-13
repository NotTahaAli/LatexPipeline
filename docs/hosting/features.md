# Hosted features

How an operator turns on the AI assistant on a hosted server, and how members use Zotero, Git sync and API tokens there.

## AI assistant

Off unless the operator enables it in `config.toml`:

```toml
[ai]
enabled = true
model = "claude-opus-5-5"            # or claude-sonnet-5-5, claude-haiku-5-5
api_key_env = "ANTHROPIC_API_KEY"    # environment variable with the key (or api_key = "..." in the file)
daily_per_user = 50                  # requests per person per day (UTC); 0 turns them off
daily_per_workspace = 500            # requests per workspace per day
daily_total = 2000                   # requests for the whole server per day: the spending cap
daily_tokens_per_user = 1000000      # input + output tokens per person per day
# provider = "openai"                # an OpenAI-compatible Chat Completions endpoint instead of Anthropic:
# base_url = "https://api.openai.com/v1"   # http:// only for a server on this machine or its network
# model = "your-model-name"          # api_key / api_key_env then hold that endpoint's key (optional for local)
```

**Usage is billed to the operator's key, and members' text is sent to Anthropic (or the configured endpoint), so tell your users.**

- Editors may use it; reviewers and viewers may not.
- With `provider = "openai"` the gateway calls that endpoint the same way. Members never choose an endpoint or send a key; a local model server is possible only because the operator configured it. Usage the endpoint does not report counts as a whole reply against the token budget.
- The gateway makes the call itself. The project's worker never sees the key or the request, and the key is taken out of the gateway's own environment at start.
- With open sign-up every account gets its own workspace, so `daily_total` is what caps spending.
- The Site admin page shows requests and tokens per person and workspace for the last 30 days.

How requests are counted:

- Every request counts against the request limits before it is sent.
- While it runs it reserves a whole reply (16,000 output tokens) against the token budget. Its tokens are recorded afterwards, for all models if a refused request fell back to another.
- Failed requests are recorded too: a refusal or a cut-off answer is billed.
- A request Anthropic did not bill (unreachable, an HTTP error before an answer, a bad request) is given back and does not use up the daily count.
- When the browser stops a streamed answer before Anthropic reported the final count, the whole reservation stays recorded, because output is billed as it is generated.

Members who have their own Claude, ChatGPT or other AI subscription can use it through [AI clients (MCP)](ai-clients.md) instead, at no cost to the operator.

## Zotero

Each person brings their own Zotero key.

1. Open Account > Zotero and enter the library type and ID, an optional collection key, BibTeX or BibLaTeX, and a read-only API key.
2. In a project, use References > More > "Sync from Zotero...".
3. Choose the entries to apply to the `.bib` files.

The key is stored in `host.db` (plain text like the rest of the database, which is mode 600). It is never shown again and never given to a worker. The gateway fetches with that person's key; comparing and applying run in the project's worker without network access.

| Limit | Value |
| --- | --- |
| Who can sync | editors who saved a key (not reviewers or viewers) |
| Library size per sync | 20 pages, 2000 entries; a bigger library syncs by collection |
| Syncs per account | 10 per 10 minutes; each counts against `max_streams_per_user` |
| Syncs at once on the server | 4 (`503` with `Retry-After` otherwise) |
| Requests to Zotero | 120 a minute for the whole server |

Better BibTeX (Zotero on your own computer) is local only.

## Git sync

Editors can connect a project to a Git repository over https in the editor's Git tab.

1. Enter the repository URL (`https://host/path`, with no name, password or query in it) and a branch (default `main`).
2. Use the tab like the local Git panel, on that branch only: changed files with diffs, Stage, Unstage, Stage all, a commit message and Commit, and the last 50 commits (click one for its changes).
3. **Pull** to bring the repository's files in; **Push** to send the project's commits.

Rules:

- There is no branch switching, because the project's files are everyone's. There is one staging area per project, shared by its editors. The commit's author is the person committing.
- Everyone in the project can read the tab; only editors change anything. Viewers and reviewers see whether a project is connected, nothing more.
- The first pull never overwrites a project file that differs. It lists those; rename or delete them and pull again.
- Later pulls fast-forward or merge and leave uncommitted changes as they are. If the merge would overwrite one, nothing changes and the files are listed: commit them first. If both sides changed the same lines, nothing is merged and the files are listed.
- A pull writes files the way an edit outside the editor does: open editors merge them into the shared text.
- Push leaves uncommitted changes here and says how many. If the repository has commits the project lacks, it says to pull first. It never force-pushes.
- The hosted editor never commits virtual environments, caches, editor or OS files or LaTeX build files (`.venv/`, `venv/`, `__pycache__/`, `node_modules/`, `.latex-cache/`, `.vscode/`, `.idea/`, `.DS_Store`, `*.aux`, `*.synctex.gz` and the like), on top of the project's own `.gitignore`. Files a pull brought in stay tracked.
- Disconnect forgets the URL and the repository copy; the files stay.

### Git access token

Each person brings their own token: Account > Git takes the user name on the Git host and an access token (a personal access token with access to the repository's contents). Pulling a public repository needs none. The token is stored in `host.db`, never shown again, and used only by the gateway for that person's pulls and pushes.

### Git safety and limits

- Git runs in the gateway, never in a worker, on a repository kept in `<data>/git/<project>` outside the project area. Nothing a project contains can set its configuration or hooks.
- The token goes in git's environment, never on a command line or to a worker.
- No system or global configuration, no hooks, no `fsmonitor`, no submodules, no redirects, only the `https` protocol. Symbolic links are checked out as plain files.
- The host is resolved first and refused if any address is loopback, private, link-local, shared or otherwise not public (SSRF). Git is then pinned to the checked address.
- A transfer stops after 2 minutes or once the repository copy is larger than twice the project quota. The pulled branch's files must fit the project quota before any is written.
- Each account has 20 syncs per 10 minutes, one at a time per project, at most 4 on the server (`503` with `Retry-After` otherwise).
- Connect, disconnect, pull, push and token changes are in the audit log.

## API tokens

Scripts, CI and the [VS Code extension](../guides/vscode.md) cannot sign in with a browser. Account > API tokens makes personal tokens, one kind for every use: a name, its scopes, the projects it may open and an expiry (30 days, 90 days, a year or never).

- The token (`lpat_...`) is shown once; the server keeps only its SHA-256. At most 20 per person.
- It works only as `Authorization: Bearer <token>`, never as a session for the rest of the site, and only where its scope allows (anything else is `403`).
- Revoke a token on the same page. Changing or resetting your password and a disabled account end all of them.
- An unknown, expired or revoked token is `401`. 30 of them from one address within 15 minutes block it for 15 minutes (`429`).
- Making and revoking tokens are in the audit log.

| Scope | Allows |
| --- | --- |
| `projects` | `/p/<project>/...` of the projects picked for it (the project's editor API, with your current role there, so removing or demoting you takes effect at once) and `GET /api/token` (who it belongs to and its projects). The editor's long-poll works. WebSockets, the AI assistant, Zotero and `/api/mcp` stay with the browser or an AI client's connection. |
| `compile` | only the [compile API](../reference/compile-api.md) (`/api/v1/*`), offered when the operator has turned it on |

A token can carry both scopes.

## Next

- [AI clients (MCP)](ai-clients.md)
- [Compile API](../reference/compile-api.md)
- [Operations: TLS, systemd, backups, updates](operations.md)
