# Install a hosted server

How `host.py` turns the editor into a multi-user site, and how an operator installs and first starts it on a Linux VPS.

## What a hosted server offers

`scripts/host.py` adds accounts, workspaces (organisations) with admin, editor, reviewer and viewer members, projects, and sign-in. People sign in with a password (plus optional two-step codes), Google or any OIDC provider, or GitHub. It uses the standard library only and needs Python 3.9+.

**Serving runs on Linux only.** Every build runs under bubblewrap (see [Security](../explanation/security.md)), and `host.py serve` refuses to start when this command fails:

```bash
bwrap --unshare-all --ro-bind / / --proc /proc --dev /dev true
```

## How it works

- `host.py` is the only thing that faces the network, behind a TLS proxy. It owns logins, sessions, workspaces and projects, and serves the account and admin pages.
- Each open project gets its own worker, `serve.py --gateway`, on 127.0.0.1 with a random secret. The gateway proxies `/p/<project>/...` to it (HTTP, long-poll and WebSocket).
- The gateway tells the worker the user's role in `X-Host-*` headers and strips those headers from client requests. Workers stop after an idle timeout.

## Roles

| Role | Can |
| --- | --- |
| Site admin (`host.py init`) | everything below in every workspace; sign-up mode, quotas, workspaces, users (disable, reset links, remove two-step), audit log |
| Workspace admin | members and roles, invite links, plus what editors can |
| Editor | create projects (from the `build.py` templates or a zip), edit and build them live with others, download, delete |
| Reviewer | what viewers can, plus comment, reply, resolve and suggest changes (Suggest mode locked on) that editors accept or reject; withdraw their own suggestions; no direct edits, file changes or builds |
| Viewer | open projects read-only, download them as a zip |

A user of another workspace gets `404` for every URL of a project.

## What the gateway enforces

- Every proxied response gets `X-Frame-Options: DENY`, `nosniff`, `Referrer-Policy: no-referrer` and `frame-ancestors 'none'`, on top of the editor's own CSP.
- Cross-site sub-resource requests under `/p/` are refused; only a top-level link from another site is let through.
- Everyone except the operator is untrusted. LaTeX source is code, so workers always build inside the sandbox with the [sharing restrictions](../editor/sharing.md): `-norc`, no shell escape, paranoid reads and writes, no build-config edits from the browser.

## Install and first start

1. Install the packages, create a service user and clone the repository:

   ```bash
   sudo apt-get install bubblewrap git latexmk texlive-latex-extra texlive-plain-generic texlive-fonts-recommended texlive-science texlive-xetex texlive-luatex texlive-bibtex-extra biber latexdiff
   sudo useradd --system --create-home --home-dir /var/lib/latex-host latexhost
   sudo -u latexhost git clone https://github.com/NotTahaAli/LatexPipeline /var/lib/latex-host/app
   ```

2. Create the data directory and the first site admin:

   ```bash
   sudo -u latexhost LP_ADMIN_EMAIL=you@example.org LP_ADMIN_PASSWORD='a long password' \
        python3 /var/lib/latex-host/app/scripts/host.py init --data /var/lib/latex-host/data
   ```

3. Edit the configuration:

   ```bash
   sudoedit /var/lib/latex-host/data/config.toml        # public_url = "https://latex.example.org", trust_proxy = true
   ```

4. Put a TLS proxy in front and start the service, see [Operations](operations.md).

`init` creates the data directory (mode 700), `config.toml`, the database and the first site admin. The admin comes from `LP_ADMIN_EMAIL`, `LP_ADMIN_PASSWORD` and `LP_ADMIN_NAME`, or `init` asks. Python 3.9 and 3.10 need `pip install tomli` for `config.toml`.

To get back in when the last admin is locked out, run `init` again with the email of an existing account. It makes that account a site admin and prints a one-time password reset link.

## config.toml

Only the operator edits it; restart after changes.

| Key | Default | Meaning |
| --- | --- | --- |
| `public_url` | `http://localhost:8080` | The address people open. `https://` turns on `Secure` `__Host-` cookies and HSTS. Requests for any other host name are refused. |
| `listen`, `port` | `127.0.0.1`, `8080` | Where `host.py` listens. Keep loopback and put the TLS proxy in front. |
| `trust_proxy` | `false` | `true` behind a proxy on this machine: client addresses (for login throttling and the audit log) come from `X-Forwarded-For`. |
| `site_name` | `LaTeX Studio` | Page titles and the authenticator app label. |
| `session_days`, `session_idle_hours` | `14`, `12` | A login lasts at most this long, and ends after this long without a request. |
| `max_connections` | `256` | Client connections the gateway serves at once; more get `503` straight away. |
| `max_upload_mb` | `50` | Largest zip upload. At most two uploads are spooled to disk and unpacked at once. |
| `max_streams_per_user` | `8` | Open editor connections (WebSocket, long-poll) per account; more get `429`. |
| `signups_per_ip_hour` | `10` | Sign-ups from one address per hour (IPv6: per `/64`). |
| `[providers.<name>]` | none | Sign-in providers, see [Sign-in providers](sign-in.md). |

The `[ai]`, `[mcp]` and `[api]` sections are described in [Hosted features](features.md) and the [Compile API](../reference/compile-api.md).

## Site settings

Site settings live in the database and are changed on the Site admin page:

- Sign-up: `invite_only` (default); `open`, where each new person gets their own workspace; or `open_domains`, which needs a provider that confirms the email.
- Linking providers to existing accounts by email.
- Projects per workspace and megabytes per project.
- Open projects on the server and per workspace. Each worker builds one document at a time, so this also caps concurrent builds.
- Idle timeout and build time limit.

A changed quota applies to workers started after the change.

### Project size quota

The megabytes per project count the document plus its build output and caches (`.out/`, `.cache/`, `.home/`). The worker measures them afresh on every save, co-editing save, upload and new file, and answers `507` when full; shrinking and deleting always work. A build that goes over loses what it wrote and reports a quota error.

### Open-project limit

When the server's limit is reached and none is idle, the workspace with the most open projects gives up its least recently active one (fair share). A workspace that already has one open never takes another workspace's last one.

### Sign-up and sign-in behaviour

- The sign-up form answers the same whether or not the address already has an account, then signs in like the login form. A taken address just gets "wrong email or password".
- Failed passwords back off per client address (IPv6 per `/64`) and per account.
- A browser that has signed in to the account before carries a signed known-device cookie with its own backoff, so someone guessing the password elsewhere cannot lock the owner out.
- Each two-step code and recovery code works once, also when sent twice at the same moment.

## Next

- [Operations: TLS, systemd, backups, updates](operations.md)
- [Docker and Coolify](docker.md)
- [Sign-in providers](sign-in.md)
