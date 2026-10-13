# Sandboxed builds

How to run every LaTeX process under bubblewrap on Linux and what the sandbox allows, for owners who share edit links or host the editor.

## Turn it on

On Linux, `build.py --sandbox` or `serve.py --sandbox` run every LaTeX process under [bubblewrap](https://github.com/containers/bubblewrap). Both set `LATEX_SANDBOX=bwrap`, which you can also set yourself. Hosted workers always run this way.

```
apt-get install bubblewrap
python scripts/serve.py --share --sandbox
```

It covers latexmk with its pdflatex/xelatex/lualatex and bibtex/biber runs, the TikZ figure jobs, `--focus` previews, the live preview and its BibTeX or biber passes, "Typeset selection", make4ht for the [HTML export](../guides/export.md), latexdiff for History's "Compare with now (PDF)" and the page images of the MCP server.

## What the sandbox allows

| | |
|---|---|
| Isolation | No network, no other processes (own PID namespace), a private `/tmp`, `$HOME=/tmp` |
| Environment | Only `PATH`, locale, `SOURCE_DATE_EPOCH` and TeX's own variables, so tokens and secrets of the server are not visible |
| Read-only | `/usr`, the libraries, fonts and fontconfig, the TeX installation (from `kpsewhich`: `TEXMFROOT`, `TEXMFDIST`, `TEXMFLOCAL`, `TEXMFSYSVAR`, `TEXMFSYSCONFIG`) and the document's own directory |
| Not visible | `/etc` (only its fonts, texmf, perl and linker files), your home, other documents |
| Writable | Only the document's build directory in `.latex-cache/` (a figure job: only its own work directory) |
| Limits per run | CPU seconds = the build timeout, 2 GB address space, 512 MB per file, 1024 open files |

- The number of processes is not capped (that limit counts all of a user's processes); a hosted operator sets systemd `TasksMax=` on the workers.
- `build.py` copies the PDF and the log to `out/` outside the sandbox, after deleting symlinks a build leaves in its build directory, so a document cannot make it read or write another file.
- So LuaLaTeX, and even shell escape, can only change the document's own build output: `io.open("/etc/passwd")`, writes to `$HOME` and network sockets fail.
- luaotfload's and fontconfig's caches live in the build directory too (`texmf-var/`), so the first LuaLaTeX build of a document takes a few seconds longer. `luaotfload-tool -u` as root fills the shared read-only cache instead.
- Not available: `~/texmf` (personal packages) and files outside the document's directory (`\input{../shared/x}`); your `~/.latexmkrc` is not read.

## When the sandbox cannot run

If `LATEX_SANDBOX=bwrap` and bubblewrap is missing or cannot create namespaces (some containers disable unprivileged user namespaces), every build fails with a message saying so. Nothing ever runs unsandboxed. Any other value than `bwrap` (or empty) is refused too.

## Cost

Each sandboxed run costs about 10 ms to start, plus about 0.1 s once per `build.py` process for the checks. `files/test` builds in 0.53 s instead of 0.44 s, and `bench/sample-report` (303 pages, 25 figure jobs) in 52 s either way.

## What runs outside the sandbox

CI does not sandbox (it builds only the repository's own documents). These never run LaTeX and run outside it: `synctex`, `texcount`, tex-fmt, veraPDF, `pdffonts`/`pdfinfo` (`--preflight`), qpdf or pdftk (`--pages`), `git` (Git panel and hosted Git sync), pandoc (owner-only), the desktop notifiers, and the command of `--watch --exec`, which only the command line starts.

## Next

- [Security model](security.md)
- [Sharing a document](../editor/sharing.md)
- [Security policy](../../SECURITY.md)
