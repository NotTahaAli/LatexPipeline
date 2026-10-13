# AI clients (MCP) on a hosted server

How an operator lets members connect their own AI apps (Claude, ChatGPT and others) to a hosted server over MCP, and what those apps may do.

## Turn it on

Off unless the operator enables it in `config.toml`:

```toml
[mcp]
enabled = true
calls_per_minute = 60              # tool calls per connected client
registrations_per_ip_hour = 20     # new client registrations per address
max_clients = 5000                 # registered clients kept at most (unused ones go after a day)
```

Members use their own AI subscription, so this costs the operator nothing.

## Connect an app

1. Add `https://latex.example.org/mcp` to the AI app. Per-app snippets are in [MCP](../guides/mcp.md).
2. The app finds the sign-in through `/.well-known/oauth-protected-resource` (RFC 9728) and `/.well-known/oauth-authorization-server` (RFC 8414), registers itself at `/oauth/register` (RFC 7591), and opens `/oauth/authorize` in the browser.
3. Sign in as usual (password and two-step code, or a sign-in provider).
4. On the consent page, check the app and choose what it may do and where.

The consent page shows:

- The app's name. The app chooses it, so the page also says where it returns: `claude.ai`, or "an app on your computer" with a warning for loopback and app-scheme redirects.
- What it may do: read; change files and run builds; add comments and suggestions.
- Which workspaces (all their projects, also later ones) or single projects. Only workspaces the person is a member of are offered.

## What a connected app can do

- Every tool call runs with the person's current role in that workspace, narrowed by the grant.
- A viewer never writes, even with the write permission. A reviewer gets only the review tools (comments and suggestions, not `grammar_check`).
- Leaving a workspace ends access to it at once.
- Files, history, review, quotas and the sandbox (builds and `render_page`) work exactly as in the editor.
- Changes appear in the history as "*name* via *app*".
- Changes, builds, comments, connections, refusals, revocations and token reuse go to the audit log (`mcp_tool`, `mcp_authorized`, ...).

## Disconnect an app

The account page lists the connected apps (where they return, permissions, projects, last use). Disconnect ends a connection at once. An app's own "disconnect" usually calls `/oauth/revoke`, which does the same.

Changing or resetting the password and disabling the account disconnect every app; re-enabling does not bring them back.

## Security details

- OAuth 2.1 authorization code with PKCE (S256 only), public clients only (`token_endpoint_auth_method` `none`; a Basic header's client id is accepted, its secret ignored).
- Codes last 2 minutes and work once; a second use revokes the connection. Every redirect carries `state` and `iss` (RFC 9207).
- A bad authorization request is shown on this server's own page, never redirected to the app, until the person has decided (RFC 9700 4.11.2: no open redirect).
- Access tokens last an hour and are bound to `<public_url>/mcp` (RFC 8707 `resource`; another resource is `invalid_target`, a token for another one is refused).
- Refresh tokens last 30 days and rotate on every use; presenting an old one revokes the connection. Tokens are random and only their SHA-256 is stored.
- `/mcp` takes only `Authorization: Bearer` (cookies are ignored). It answers `401` with `WWW-Authenticate: Bearer resource_metadata=...`, and `403 insufficient_scope` for a tool outside the grant. It refuses a request whose `Origin` is another site.
- `/mcp` is stateless: no `Mcp-Session-Id`, no SSE stream (`GET` is `405`). Workers never see tokens.
- Registration is open by design (that is how the apps connect) but rate limited per address and capped in number. Unused registrations are deleted after a day.
- Client ID Metadata Documents (the newer alternative to registration) are not supported, because the gateway would have to fetch URLs that anyone names. Every app listed falls back to registration.
- `search` reads files in the gateway, so it is limited to 10 a minute per person and 16 MB over at most 20 projects. Regular expressions in `grep` are local-only.
- The model and the app are untrusted. They get nothing a person with the same role could not do in the editor, never change `build.toml` or `latexmkrc` (only the local owner can, with `mcp_server.py --allow-build-config`), never accept suggestions, and their tool results are capped.

## Next

- [MCP](../guides/mcp.md)
- [Hosted features](features.md)
- [Security](../explanation/security.md)
