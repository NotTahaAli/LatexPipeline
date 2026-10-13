# Security model

What sharing changes for builds, how sandboxed builds confine LaTeX, and what each role may do, for owners deciding whom to share with and how.

## The risk

LaTeX source is code. Anyone with the edit link can change every file of that document and make this machine compile it, so share links only with people you trust, and treat the edit link like a password. The restrictions below make that much safer, but compiling someone else's LaTeX is never risk-free. They do not cover `--host 0.0.0.0`, which is not what sharing is for.

## Builds while sharing

While sharing is on, builds are restricted:

- **No shell escape.** Shell escape is off (`shell_escape=f`) for the whole server.
- **No `latexmkrc`.** `latexmk` runs with `-norc`, so no `latexmkrc` is read. Your own `latexmkrc` is ignored until you stop sharing.
- **Unsafe `latexmk_args` fail the build.** A `build.toml` whose `latexmk_args` enable shell escape, name programs or code to run (`-e`, `-r`, `-pdflatex=...`, `-latexoption`, `-pretex`, `-usepretex`, `-cnf-line`), or move the output (`-outdir`, `-auxdir`, `-jobname`) fails the build.
- **Files stay in the document directory.** TeX may only read and write inside the document's directory (`openin_any=p`, `openout_any=p`). `\input{/etc/passwd}` fails, and so do inputs that leave the directory with `..` (for example `\input{../shared/x}`) and dotfiles. Such a document builds again when you stop sharing.
- **Live previews** run like every build: through the sandbox when it is on, with these restrictions. A TeX process started before sharing was turned on (or off) is never used; it is replaced.

**Warning: LuaLaTeX.** LuaLaTeX stays allowed, but Lua can write files even with shell escape off, so **an edit link to a document that uses (or is switched to) LuaLaTeX can run code on your computer**. The Share dialog says so. Give edit links only to people you trust; view links are safe. On Linux, `serve.py --share --sandbox` confines that code ([sandboxed builds](sandbox.md)).

## Tokens and HTTP

- Every request needs a token, loopback included. A tunnel connects to the server from 127.0.0.1, so "local" proves nothing; your own browser gets a private third token as a cookie when sharing starts.
- Tokens are random 32-byte URL-safe strings, compared in constant time.
- A link sets an HttpOnly, SameSite=Lax cookie on the first visit and redirects to a URL without the token. New links (Share dialog) disconnect everyone on the old ones.
- Responses carry a Content-Security-Policy that allows only this origin and the pinned CDNs, `X-Content-Type-Options: nosniff`, `Referrer-Policy: no-referrer` and `frame-ancestors 'none'`.
- The Host header must be loopback or the tunnel's own host.
- Each client and room is capped in count and size.

## Roles

Over a link, everything is for the shared document only; the view role cannot see other documents (their messages, cursors and names included), in the UI or the API.

| Action | View | Review | Edit | Owner |
|---|---|---|---|---|
| Open files, PDF, outline, References list | Yes | Yes | Yes | Yes |
| Browse versions, diffs, comments, suggestions; download a version zip; export review | Yes | Yes | Yes | Yes |
| Comment, reply, resolve, suggest; delete or withdraw own | No | Yes | Yes | Yes |
| Comment on the PDF | No | Yes | Yes | Yes |
| Accept or reject suggestions | No | No | Yes | Yes |
| Save, create and change files, change the file tree | No | No | Yes | Yes |
| Change `build.toml`, `latexmkrc`, `.latexmkrc` (not even through co-editing) | No | No | No | Yes |
| Rebuild, live preview, latexdiff comparison | No | No | Yes | Yes |
| Name and restore versions | No | No | Yes | Yes |
| Add, edit, delete bibliography entries; bib lookups and DOI imports | No | No | Yes | Yes |
| Grammar checks | No | No | Yes | Yes |
| AI assistant | No | No | Only if allowed | Yes |
| Zotero sync | No | No | Only if allowed | Yes |
| Refresh linked files | No | No | Yes | Yes |
| Request access | Yes | Yes | No | Sees requests |
| Lock a document, link files, unlink | No | No | No | Yes |
| Delete versions (Clean up) | No | No | No | Yes |
| Settings (grammar, AI, Zotero) | No | No | No | Yes |
| Git panel | No | No | No | Yes |

- Build-config files never appear, and are never restored, through a link.
- A reviewer cannot save, change the file tree, rebuild, preview, name or restore versions, or use grammar checks, the assistant or Zotero. The server refuses its co-editing updates.
- No link role reaches the Git panel, so a guest can neither run git nor read the repository's status, diffs or log.
- Authors behind a named link are that link's name. Behind the anonymous view and edit links they are the name the browser gives, marked "(unverified name)", and "your own" comments are tied to a random key kept in that browser.

## Rate limits over a link

| What | Limit |
|---|---|
| Rebuilds (a latexdiff comparison counts as one) | 6 a minute |
| Saves | 900 a minute |
| File-tree changes | 60 a minute |
| History reads | 120 a minute |
| Naming and restoring versions | 20 a minute |
| Commenting and suggesting | 120 a minute |
| Live previews | 120 a minute, two at once per document |
| Bib lookups and DOI imports | 30 a minute |
| Grammar checks | 30 a minute |
| Access requests | 5 a minute |
| AI assistant | 10 a minute, 20 an hour per visitor, 100 per server run for all visitors |
| Zotero fetch | 6 a minute for all links together |

## Features that reach the network or the owner's keys

- **Live previews** cover only the shared document's `.tex`, `.cls` and `.sty` files (never build configuration). An edit of the preamble, a class or a package runs no TeX; it only answers "rebuilding preamble".
- **Bibliography.** Bib lookups and DOI imports go to Crossref and send only a DOI or title. Applying happens in the editor, not on the server. `.bib` files over 1 MB (5 MB and 50 files in all) are not listed.
- **Grammar.** While sharing, public mode is refused for everyone, `build.toml`'s `grammar = "public"` included, unless the owner ticked "Allow the public API while sharing". The Share dialog says which applies. The server never sends the local URL to non-owners.
- **AI assistant.** Off for shared links unless the owner ticks "Let people with an edit link use it while sharing" (it uses the owner's key). That is only possible with sandboxed builds (`serve.py --share --sandbox`): otherwise LuaLaTeX from an edit link could read the key. A visitor is a named link, or an anonymous link from one address; behind a tunnel everyone on an anonymous link shares it, so give people named links.
- **Zotero.** Fetching uses the owner's key and network, so it is owner-only unless the owner ticked "Let editors of shared links sync from my Zotero library" (possible only with sandboxed builds). A link is told only whether it may sync. Better BibTeX sync is refused while sharing. The edit role may apply a chosen change, which needs no network.

## Sandboxed builds

On Linux, `serve.py --share --sandbox` (or `build.py --sandbox`) runs every LaTeX process under bubblewrap, with no network and write access only to the document's build output. See [Sandboxed builds](sandbox.md).

## Next

- [Sharing a document](../editor/sharing.md)
- [Sandboxed builds](sandbox.md)
- [Security policy](../../SECURITY.md)
