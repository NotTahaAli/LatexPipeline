# Docker and Coolify

How to run the hosted server in one container with Docker Compose or Coolify, including the settings the build sandbox needs.

## What the image contains

`Dockerfile` and `deploy/compose.yaml` run `host.py` in one container: Ubuntu 24.04, the TeX Live packages of the [plain install](install.md), and the editor's libraries served from the image instead of a CDN. Data (`config.toml`, the database, projects) lives in the `latex-data` volume at `/data`.

## Deploy on Coolify

1. Create a new resource of type Docker Compose, with compose file `deploy/compose.yaml`.
2. Give the `latex` service a domain. Coolify's proxy does TLS and fills `SERVICE_URL_LATEX_8080`, which becomes `public_url`.
3. Set `LP_ADMIN_EMAIL` and `LP_ADMIN_PASSWORD` for the first site admin. They are used on the first start only.
4. Turn on "Preserve repository during deployment". Coolify reads `deploy/bwrap-seccomp.json` from the repository, so the repository must stay in the deployment.

## Deploy elsewhere

```bash
PUBLIC_URL=https://latex.example.org LP_ADMIN_EMAIL=... LP_ADMIN_PASSWORD=... docker compose -f deploy/compose.yaml up -d
```

Put a TLS proxy in front of port 8080 of the container. The port is not published on the host.

## Change the configuration

The first start runs `host.py init` and sets `listen = "0.0.0.0"` and `trust_proxy = true` in `config.toml`. Later starts only update `public_url`.

To change anything else, edit `/data/config.toml` (copy it out and back with `docker cp`) and restart the container. The keys are listed in [Install a hosted server](install.md).

## Sandbox settings

Builds run under bubblewrap inside the container, which Docker's defaults forbid. The compose file therefore sets:

- `deploy/bwrap-seccomp.json`: Docker's default seccomp profile from [moby/profiles](https://github.com/moby/profiles) (Apache-2.0), plus `clone`, `clone3`, `unshare`, `setns`, `mount`, `umount2` and `pivot_root`.
- `systempaths=unconfined`, so the sandbox can mount its own `/proc`.
- `apparmor=unconfined`.
- No capabilities, `no-new-privileges`, and a non-root user.

This was tested with Docker 29 on a host without AppArmor: a sandboxed hosted build works. The container refuses to start, with a message, when the sandbox does not work.

## Hosts with AppArmor user-namespace restrictions

Ubuntu 23.10 and later restrict unprivileged user namespaces through AppArmor; `sysctl kernel.apparmor_restrict_unprivileged_userns` then prints `1`. Such hosts need one more step, which has not been tested:

1. Load `deploy/apparmor-latex-host` on the host. The commands are in the file.
2. Set `LP_APPARMOR=latex-host` in the environment of the compose project.

The alternative, `sysctl kernel.apparmor_restrict_unprivileged_userns=0`, lifts the restriction for the whole host.

## Next

- [Install a hosted server](install.md)
- [Operations: TLS, systemd, backups, updates](operations.md)
- [Sign-in providers](sign-in.md)
