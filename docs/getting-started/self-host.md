# Self-host

The shortest path to a running multi-user server on a Linux VPS, for operators; each step links to the hosting page with the details.

## Before you start

- A Linux server. Serving runs on Linux only, because every build runs under bubblewrap.
- Python 3.9 or newer (3.9 and 3.10 also need `pip install tomli`).

Prefer containers? Use [Docker and Coolify](../hosting/docker.md) instead of steps 1 to 3.

## Steps

1. Install the packages, create the service user and clone the repository:

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

3. Set `public_url = "https://latex.example.org"` and `trust_proxy = true` in `/var/lib/latex-host/data/config.toml` (`sudoedit` it). All keys: [Install a hosted server](../hosting/install.md).
4. On Ubuntu 23.10 and later, give bubblewrap an AppArmor profile so the sandbox works. See [Operations](../hosting/operations.md).
5. Put Caddy in front for TLS and install the systemd unit, both from [Operations](../hosting/operations.md). Then start them:

   ```bash
   sudo systemctl enable --now latex-host caddy
   ```

6. Open your `public_url` and sign in with the admin email and password.

## After the first start

- Add Google, OIDC or GitHub sign-in: [Sign-in providers](../hosting/sign-in.md).
- Turn on the AI assistant, or let members use Zotero, Git sync and API tokens: [Hosted features](../hosting/features.md).
- Set up backups and nightly updates: [Operations](../hosting/operations.md).

## Next

- [Install a hosted server](../hosting/install.md)
- [Operations](../hosting/operations.md)
- [Docker and Coolify](../hosting/docker.md)
