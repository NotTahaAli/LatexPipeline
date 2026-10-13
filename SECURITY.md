# Security policy

## Reporting a vulnerability

Please report security problems privately, not in a public issue.

1. Open the repository's **Security** tab.
2. Click **Report a vulnerability** and fill in the form.

Include what you did, what happened, and the commit or release you used. You will get a reply in the advisory thread.

## Supported versions

Fixes go into `main` and the next `v*` release. Older releases are not patched.

## What counts

LaTeX source is code, so these matter most:

* Anything that lets a share link, a hosted account (workspace admins included) or an MCP client run code outside the build sandbox.
* Reading or writing files outside a document's own folder through the editor, the hosted gateway or the compile API.
* Leaking an API key, token or another person's data to a client.

[docs/explanation/security.md](docs/explanation/security.md) describes the security model these rely on.
