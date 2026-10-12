# Hosted editor (scripts/host.py) in one container. See README "Docker and Coolify" for the security options
# the container needs (deploy/compose.yaml): builds run under bubblewrap inside it.
FROM ubuntu:24.04

RUN apt-get update \
 && DEBIAN_FRONTEND=noninteractive apt-get install -y --no-install-recommends \
      python3 ca-certificates bubblewrap git latexmk texlive-latex-extra texlive-plain-generic \
      texlive-fonts-recommended texlive-science texlive-xetex texlive-luatex texlive-bibtex-extra biber latexdiff \
 && rm -rf /var/lib/apt/lists/*

RUN useradd --system --uid 10001 --create-home --home-dir /home/latexhost latexhost \
 && mkdir -p /data && chown latexhost /data

COPY --chown=root:root scripts/ /app/scripts/
COPY --chown=root:root LICENSE /app/
COPY --chown=root:root deploy/entrypoint.sh /app/entrypoint.sh
# The editor's libraries served from the container instead of a CDN.
RUN python3 /app/scripts/vendor_ui.py && chmod -R a+rX /app

USER latexhost
VOLUME /data
EXPOSE 8080
# The gateway answers only its public host name, so the check sends that name.
HEALTHCHECK --interval=30s --timeout=5s --start-period=60s \
  CMD python3 -c "import tomllib, urllib.parse as p, urllib.request as r; \
host = p.urlsplit(tomllib.load(open('/data/config.toml', 'rb'))['public_url']).netloc; \
r.urlopen(r.Request('http://127.0.0.1:8080/', headers={'Host': host}), timeout=4)"
ENTRYPOINT ["/app/entrypoint.sh"]
