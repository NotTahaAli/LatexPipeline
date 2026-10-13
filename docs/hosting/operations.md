# Operations

How an operator runs a hosted server in production: the TLS proxy, the systemd service, the sandbox on Ubuntu, backups, upgrades and automatic updates.

## TLS proxy with Caddy

The TLS proxy is required, not optional. `host.py` speaks plain HTTP, listens on 127.0.0.1 by default (it warns when `listen` is anything else) and has only coarse slow-client protection of its own (a socket timeout and `max_connections`). Let the proxy terminate TLS and time out slow clients.

`/etc/caddy/Caddyfile` (Caddy gets and renews the certificate; WebSockets and long-polls pass through):

```caddyfile
{
    servers {
        timeouts {
            read_header 10s   # slow-loris: a client must send its headers quickly
            read_body 5m      # zip uploads (max_upload_mb) on slow links
            idle 2m
        }
    }
}

latex.example.org {
    encode gzip
    reverse_proxy 127.0.0.1:8080 {
        transport http {
            dial_timeout 5s
            response_header_timeout 90s   # longer than the editor's 25 s long-poll
        }
    }
}
```

## Run as a systemd service

`/etc/systemd/system/latex-host.service`:

```ini
[Unit]
Description=LaTeX Studio (host.py)
After=network-online.target

[Service]
User=latexhost
WorkingDirectory=/var/lib/latex-host
ExecStart=/usr/bin/python3 /var/lib/latex-host/app/scripts/host.py serve --data /var/lib/latex-host/data
EnvironmentFile=-/etc/latex-host.env
Restart=on-failure
# The sandbox sets CPU, memory and file-size limits per LaTeX run but no process limit: cap it here.
TasksMax=512
MemoryMax=8G
NoNewPrivileges=yes
ProtectSystem=strict
ReadWritePaths=/var/lib/latex-host/data
ProtectHome=yes
PrivateTmp=yes

[Install]
WantedBy=multi-user.target
```

1. Put provider secrets in `/etc/latex-host.env` (mode 600), for example `GOOGLE_CLIENT_SECRET=...`.
2. Start both services:

   ```bash
   sudo systemctl enable --now latex-host caddy
   ```

Workers get a minimal environment (never these secrets), and LaTeX inside the sandbox an even smaller one.

## Allow the sandbox on Ubuntu

bubblewrap needs unprivileged user namespaces. Debian allows them. Ubuntu 23.10 and later restrict them through AppArmor: `sysctl kernel.apparmor_restrict_unprivileged_userns` prints `1`, and bwrap then fails with `loopback: Failed RTM_NEWADDR: Operation not permitted`.

Give `/usr/bin/bwrap` its own profile and check that the sandbox works:

```bash
sudo tee /etc/apparmor.d/bwrap >/dev/null <<'EOF'
abi <abi/4.0>,
include <tunables/global>

profile bwrap /usr/bin/bwrap flags=(unconfined) {
  userns,

  include if exists <local/bwrap>
}
EOF
sudo apparmor_parser -r /etc/apparmor.d/bwrap
sudo -u latexhost bwrap --unshare-all --ro-bind / / --proc /proc --dev /dev true && echo sandbox works
```

`--insecure-no-sandbox` runs without bubblewrap (and on any OS) for development only. It prints a warning, and the admin page says so. **Never use it on a server people can reach.**

## Data directory

Everything is in the data directory:

| Path | Holds |
| --- | --- |
| `host.db` | the database (sqlite, WAL) |
| `config.toml` | the operator's configuration |
| `projects/<workspace>/<project>/<name>/` | the document folder |
| `.out/`, `.cache/`, `.home/` beside it | build output and caches; regenerated |
| `.latex-history/` beside it | the editor's version history and review comments |
| `git/<project>/` | the copy of a project's Git repository (Git sync); made again by the next pull if lost |
| `api-cache/<user id>/` | cached compile API results |

`.latex-history/` (the blobs, the sqlite index and `review.json`) counts toward the project quota, is capped at a quarter of it, and is not in the zip download. Workspace admins may delete old versions from the History tab.

## Back up and restore

Back up the database (safe while running) plus the `projects/` tree, for example nightly with `restic` or `rsync`:

```bash
sqlite3 host.db ".backup host-backup.db"
```

To restore:

1. Stop the service.
2. Put the database and the `projects/` tree back.
3. Start the service.

## Upgrade by hand

`git pull` in the clone and restart the service. The database schema migrates itself; the version is in its `schema_version` table.

## Automatic updates

A small root script and a nightly timer install each new release on their own. The script fetches the tags, checks out the newest `v*` tag (never `main`, never an older version than the one running) and restarts the service. It lives outside the clone and is owned by root, because the service user can write to the clone and root runs the script.

1. Create `/usr/local/sbin/latex-host-update` and `sudo chmod 755` it:

```sh
#!/bin/sh
# Install the newest LatexPipeline release (v* tag) and restart latex-host. Never moves backwards.
set -eu
app=/var/lib/latex-host/app
g() { runuser -u latexhost -- env HOME=/var/lib/latex-host git -C "$app" "$@"; }
g fetch --quiet --tags origin
tag=$(g tag --list 'v*' --sort=-version:refname | head -n 1)
[ -n "$tag" ] || exit 0
rc=0; g merge-base --is-ancestor "$tag" HEAD || rc=$?
[ "$rc" -eq 1 ] || exit "$rc"   # 0: already contains it; above 1: git failed
g -c advice.detachedHead=false checkout --quiet "$tag"
systemctl restart latex-host
echo "latex-host now runs $tag"
```

2. Create `/etc/systemd/system/latex-host-update.service`:

```ini
[Unit]
Description=Install the newest LaTeX Studio release
Wants=network-online.target
After=network-online.target

[Service]
Type=oneshot
ExecStart=/usr/local/sbin/latex-host-update
```

3. Create `/etc/systemd/system/latex-host-update.timer`:

```ini
[Unit]
Description=Nightly LaTeX Studio update

[Timer]
OnCalendar=*-*-* 04:00
RandomizedDelaySec=30m
Persistent=true

[Install]
WantedBy=timers.target
```

4. Enable the timer:

   ```bash
   sudo systemctl daemon-reload && sudo systemctl enable --now latex-host-update.timer
   ```

5. Run it once by hand and read the result:

   ```bash
   sudo systemctl start latex-host-update
   journalctl -u latex-host-update
   ```

Behaviour:

- A clone that is ahead of the newest tag (a `git pull` of `main`) stays where it is until a newer tag is released.
- A failed fetch or checkout (for example, local changes in the clone) leaves the running version alone and shows in the journal.
- The restart ends builds that are running at that moment.

## Next

- [Install a hosted server](install.md)
- [Docker and Coolify](docker.md)
- [Security](../explanation/security.md)
