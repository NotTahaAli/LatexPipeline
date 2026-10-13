# MCP client setup

This page gives the exact configuration for each AI client, for the local server and for a hosted server, and is for people connecting an app to this project over MCP.

## Before you start

- **Local**: in each snippet `/path/to/LatexPipeline` is your clone. Use absolute paths; on Windows use `python` and `C:\\path\\to\\LatexPipeline\\scripts\\mcp_server.py` in JSON. Server options are in [Use with AI clients (MCP)](mcp.md).
- **Hosted**: replace `https://latex.example.org` with your server's `public_url`. Any client that speaks Streamable HTTP and OAuth 2.1 with dynamic client registration works; your browser opens the server's sign-in and consent pages. Server-side setup is in [Hosted features](../hosting/features.md).

The local snippet for Claude Desktop is the shared `mcpServers` object used by several clients:

```json
{"mcpServers": {"latex": {"command": "python3", "args": ["/path/to/LatexPipeline/scripts/mcp_server.py"]}}}
```

## Claude

**Claude Code, local:**

```bash
claude mcp add latex -- python3 /path/to/LatexPipeline/scripts/mcp_server.py
```

Add `-s project` to share it in `.mcp.json`.

**Claude Code, hosted:**

```bash
claude mcp add --transport http latex https://latex.example.org/mcp
```

Then run `/mcp` to sign in (loopback redirect).

**Claude Desktop, local:** Settings > Developer > Edit Config opens `claude_desktop_config.json` (macOS `~/Library/Application Support/Claude/`, Windows `%APPDATA%\Claude\`). Add the `mcpServers` object above.

**claude.ai, Claude Desktop, Claude mobile, hosted:** Settings > Connectors > Add custom connector, URL `https://latex.example.org/mcp`. On Team and Enterprise an owner adds it under organization settings first. Redirect: `https://claude.ai/api/mcp/auth_callback`.

## ChatGPT

ChatGPT connects only to remote servers, so use a hosted server.

1. Open Settings > Apps & Connectors > Advanced and turn on Developer mode.
2. Choose Create, enter the URL `https://latex.example.org/mcp` and set authentication to OAuth.

Redirect: `https://chatgpt.com/connector_platform_oauth_redirect` (needs the `iss` parameter, which the server sends). Deep research and company knowledge use only `search` and `fetch`; developer mode sees every tool the connection allows.

## OpenAI Codex CLI

Local, in `~/.codex/config.toml`:

```toml
[mcp_servers.latex]
command = "python3"
args = ["/path/to/LatexPipeline/scripts/mcp_server.py"]
```

Hosted, in `~/.codex/config.toml`:

```toml
[mcp_servers.latex]
url = "https://latex.example.org/mcp"
```

Then run `codex mcp login latex`.

## Cursor

Local: the `mcpServers` object above in `~/.cursor/mcp.json`, or `.cursor/mcp.json` in a project.

Hosted, in `mcp.json`:

```json
{"mcpServers": {"latex": {"url": "https://latex.example.org/mcp"}}}
```

Cursor signs in when you enable it (redirect `cursor://anysphere.cursor-mcp/oauth/callback` or loopback, depending on the version). Cursor has warned above about 40 tools across all servers; the server offers up to 41, so turn single tools off in Cursor's MCP settings if needed.

## VS Code

Copilot agent mode. Put the config in `.vscode/mcp.json` in the workspace, or use "MCP: Add Server".

Local:

```json
{"servers": {"latex": {"type": "stdio", "command": "python3", "args": ["${workspaceFolder}/scripts/mcp_server.py"]}}}
```

Hosted:

```json
{"servers": {"latex": {"type": "http", "url": "https://latex.example.org/mcp"}}}
```

Sign-in goes through loopback (`http://127.0.0.1:33418/`) or `https://vscode.dev/redirect`.

## Windsurf

Both go in `~/.codeium/windsurf/mcp_config.json`. Local: the `mcpServers` object above. Hosted:

```json
{"mcpServers": {"latex": {"serverUrl": "https://latex.example.org/mcp"}}}
```

## Gemini CLI

Both go in `~/.gemini/settings.json` (or `.gemini/settings.json` in a project). Local: the `mcpServers` object above. Hosted:

```json
{"mcpServers": {"latex": {"httpUrl": "https://latex.example.org/mcp"}}}
```

Then run `/mcp auth latex` (loopback redirect).

## Redirect URIs a hosted server accepts

At client registration the server accepts:

- any `https` URL;
- `http` to `127.0.0.1`, `[::1]` or `localhost` on any port (RFC 8252; the port is ignored when matching);
- private-use app schemes: reverse-DNS names (`com.example.app:/callback`, RFC 8252 7.1) plus `cursor`, `vscode`, `vscode-insiders` and `windsurf`.

Schemes such as `javascript:`, `data:` and `ms-msdt:` are refused. Host names are shown in punycode on the consent page. Nothing here is specific to one vendor.

## Tool limits

The server offers up to 41 tools (fewer with `--read-only` or a read-only connection; `export_docx` only locally). Clients that cap the number of tools across all servers can turn single tools off in their MCP settings.

## What was tested

The MCP Inspector CLI (`npx @modelcontextprotocol/inspector --cli`) was run against the local server (`tools/list`, `tools/call`) and against the hosted endpoint with a bearer token. The hosted sign-in flow (discovery, registration, sign-in with two-step code, consent, PKCE, refresh, MCP calls, disconnect) was run in a browser. The per-app setups above follow each vendor's current documentation but were not run against the real apps; `claude mcp add` was checked to write the configuration shown.

## Next

- [Use with AI clients (MCP)](mcp.md)
- [Hosted features](../hosting/features.md)
- [VS Code extension](vscode.md)
