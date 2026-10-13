# Compile API

How scripts and CI send a document to `POST /api/v1/compile` and get the PDF or the build log back, on your own computer (`serve.py`) or on a hosted server with an API token.

## Request

`POST /api/v1/compile` with the document as the body. One folder around everything is taken off.

| Body | `Content-Type` example |
| --- | --- |
| a zip | `application/zip` |
| a tar archive, plain, gzip, bzip2 or xz | `application/gzip` for `.tar.gz` |
| JSON `{"files": {"main.tex": "...", "fig.png": {"base64": "..."}}}` | `application/json` |

Query parameters:

| Parameter | Default | Meaning |
| --- | --- | --- |
| `main` | `main.tex` | the main file, a path inside the upload |
| `output` | `pdf` | `pdf`: the PDF with `200`, or the build log as text with `422` when the build fails. `log`: always the log. `json`: `ok`, `seconds`, `pages`, `errors`, `log` and the PDF in base64. |

The `X-Compile` response header carries the summary in every case: `ok`, `seconds`, `pages`, and the first errors with file and line.

## Use it locally

`serve.py` offers the endpoint for scripts on your own computer:

```bash
curl --fail-with-body --data-binary @paper.zip -H "Content-Type: application/zip" -o paper.pdf "http://127.0.0.1:8000/api/v1/compile"
tar czf - -C paper . | curl --data-binary @- -H "Content-Type: application/gzip" -o talk.pdf "http://127.0.0.1:8000/api/v1/compile?main=talk.tex"
```

- **Who may call it:** only the owner. With no token, that is loopback without sharing, or the owner's cookie while sharing. Share links of every role get `403`. A request from another web page (`Origin`) is refused.
- **How it builds:** the upload is written to `.latex-cache/api-compile/` and built like any document: its `build.toml` applies, the sandbox applies with `--sandbox`, and it takes turns with the editor's builds (one build at a time). It is removed afterwards; nothing goes to `out/`.
- **Limits:** paths outside the upload (`..`, absolute, links) are refused. At most 2000 files and 200 MB unpacked.

## Use it on a hosted server

1. The operator turns it on in `config.toml`:

```toml
[api]
enabled = true
compiles_per_hour = 60             # requests per person per hour (cache hits included)
compiles_per_day = 300             # builds per person per day (UTC; cache hits not counted)
max_upload_mb = 20                 # largest request body
concurrent = 2                     # builds at once on the whole server (one per person)
cache_per_user = 20                # results kept per person
```

2. Make an API token with the `compile` scope in Account > API tokens (see [Hosted features](../hosting/features.md)).
3. Send the same bodies and parameters as locally, with `Authorization: Bearer lpat_...`. The answers are the same too.

```bash
curl --fail-with-body --data-binary @paper.zip -H "Content-Type: application/zip" -H "Authorization: Bearer $LATEX_TOKEN" -o paper.pdf https://latex.example.org/api/v1/compile
```

### How hosted builds run

- The gateway only checks and forwards. The build runs in a worker of the person's own, counted against `max_workers_per_tenant` and `max_workers` like a workspace.
- It runs in the bubblewrap sandbox with the sharing restrictions and the project size quota (`max_project_mb`).
- The upload's `build.toml` and `latexmkrc` are left out; `X-Compile`'s `skipped` names them.
- `/p/<id>/api/v1/...` is `404`: the endpoint is not reachable through the editor proxy.

### Authentication

- Only `Authorization: Bearer` counts. Cookies are ignored and no CSRF token is needed.
- A request whose `Origin` is another site is `403`.
- A wrong, expired or revoked token, or a disabled account, is `401` with `WWW-Authenticate: Bearer`.
- A token without the `compile` scope (one made only for projects) is `403`.
- 30 wrong tokens from one address within 15 minutes block it for 15 minutes (`429`).
- Changing or resetting the password and disabling the account revoke every token.

### Limits

| Limit | Answer |
| --- | --- |
| body larger than `max_upload_mb` | `413` |
| more than `compiles_per_hour` requests or `compiles_per_day` builds per person | `429` with `Retry-After` |
| a second build of the same person while one runs | `429` |
| more than `concurrent` builds on the server | `503` |

### Cache

The cache key is the SHA-256 of the person, `main`, the content type and the body. The same request again is answered from that person's last `cache_per_user` results without a build (`X-Compile-Cache: hit`), so CI that sends an unchanged document costs nothing. Cached results live in `api-cache/<user id>/` in the data directory.

### Audit

Every compile, cache hit, token made and token revoked is in the audit log (`api_compile`, `api_token_created`, `api_token_revoked`). `api_usage` in `host.db` counts builds and cache hits per person and day.

## Next

- [Hosted features](../hosting/features.md)
- [Continuous integration](../guides/ci.md)
- [Command-line reference](cli.md)
