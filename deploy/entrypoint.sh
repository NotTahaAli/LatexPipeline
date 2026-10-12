#!/bin/sh
# First start: create /data (config.toml, database, the site admin from LP_ADMIN_EMAIL and LP_ADMIN_PASSWORD).
# Every start: public_url follows PUBLIC_URL, else Coolify's SERVICE_URL_LATEX_8080, when one is set. Then serve.
set -eu
DATA=/data
if [ ! -f "$DATA/host.db" ]; then
  python3 /app/scripts/host.py init --data "$DATA"
  # Coolify's proxy (or yours) reaches the container over its network, and sends X-Forwarded-For.
  sed -i -e 's/^listen = "127.0.0.1" .*/listen = "0.0.0.0"                     # inside the container; the proxy is in front/' \
         -e 's/^trust_proxy = false .*/trust_proxy = true                    # behind the proxy: client IPs from X-Forwarded-For/' \
      "$DATA/config.toml"
fi
PUBLIC_URL=${PUBLIC_URL:-${SERVICE_URL_LATEX_8080:-}}
if [ -n "$PUBLIC_URL" ]; then
  url=$(printf '%s' "$PUBLIC_URL" | sed 's|/*$||; s|[\\&|"]||g')
  sed -i "s|^public_url = \"[^\"]*\"|public_url = \"$url\"|" "$DATA/config.toml"
fi
if ! bwrap --unshare-all --ro-bind / / --proc /proc --dev /dev true 2>/dev/null; then
  echo "bubblewrap cannot start a sandbox in this container, so every build would fail. Run it with the security" >&2
  echo "options in deploy/compose.yaml (seccomp profile, systempaths=unconfined, apparmor); on Ubuntu 23.10 and" >&2
  echo "later also see README 'Docker and Coolify' for the AppArmor user-namespace setting." >&2
  bwrap --unshare-all --ro-bind / / --proc /proc --dev /dev true || true
  exit 2
fi
exec python3 /app/scripts/host.py serve --data "$DATA"
