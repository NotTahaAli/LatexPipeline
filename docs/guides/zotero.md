# Zotero

How to sync references from a Zotero library (through the Zotero API or Better BibTeX) into a document's `.bib`, and how the API key is kept private.

## Set up the Zotero API

1. Make an API key at zotero.org/settings/keys/new. Read-only is enough.
2. Open References > More (...) > "Zotero settings...". On a local editor only the owner gets it; hosted, each person sets this on their Account page (see [hosted features](../hosting/features.md)).
3. Enter a personal or group library ID, an optional collection key (8 characters), BibTeX or BibLaTeX, and the API key.

### Where the key is kept

The key is kept outside the project:

- in the environment variable `ZOTERO_API_KEY`, which wins if set; or
- in `zotero.json` (mode 600) in a `latex-pipeline` folder under your config directory: `$XDG_CONFIG_HOME` or `~/.config`, `~/Library/Application Support`, or `%APPDATA%`.

It is sent only in the `Zotero-API-Key` header to `api.zotero.org`. It is never put into the project, a reply, the co-editing bus or a log, and the settings dialog never shows it back. `ZOTERO_API_KEY` is taken out of the environment when the editor starts, so no build inherits it.

## Sync from Zotero

1. Choose "Sync from Zotero..." and pick the `.bib` to "Add to and update".
2. Review the result and tick what you want.
3. Click Apply.

"Sync from Zotero..." fetches `items/top` as BibTeX:

| Detail | Value |
| --- | --- |
| Page size | 100 per request |
| Unchanged library | `If-Modified-Since-Version`, so it answers 304 |
| Rate | at most 120 requests a minute, waiting rather than failing |
| Redirects | not followed |

It compares the fetch with the `.bib` you picked (unsaved edits included) and with every other `.bib` of the document. An entry already in another file (same key, DOI or title) is listed under "Already in other files" and not added again; choose that file to update it.

| Group | Default | What Apply does |
| --- | --- | --- |
| New in Zotero | ticked | Adds the entry. |
| Changed in Zotero | unticked | Shows each field's old and new value; only differing fields are replaced, and fields Zotero does not export stay. |
| Only in your file | n/a | Never deleted. |

- Entries are matched by key, then by DOI or title, so your own keys survive.
- A Zotero key that your file or another `.bib` uses for a different work is added under a free key (`smith20a`).
- Zotero's `file` field (attachment paths) is left out.
- Apply puts one splice into the open editor document like every other References edit, so co-editing stays consistent and the rest of the file stays byte-for-byte.
- Comparing is bounded (titles cut to 300 characters, a work budget per comparison, indexed DOI and title pools): a library of thousands of entries sharing one DOI or title compares in about a second.

## Better BibTeX on this computer

Choose "Better BibTeX on this computer" to read Better BibTeX's pull export from `http://127.0.0.1:23119`. It keeps your pinned citation keys.

- Local editor only, never while sharing.
- No proxy and no key.
- My Library, or a group library by its group ID.

| Export | URL |
| --- | --- |
| Whole library | `/better-bibtex/export/library?/<1 or group ID>/library.bibtex` |
| One collection, by key or path such as `Thesis/Chapter 2` | `/better-bibtex/export/collection?/<1 or group ID>/<key or path>.bibtex` |

These are the URLs Better BibTeX's own "Download Better BibTeX export..." gives. They were checked against its source but not tested against a running Zotero.

## Let share-link editors sync

While sharing, the owner may tick "Let editors of shared links sync from my Zotero library". It is off by default.

- It needs sandboxed builds (`serve.py --share --sandbox`), like the AI assistant. Without the sandbox, LuaLaTeX from an edit link could read a stored key from `zotero.json`; the Share dialog says so.
- Edit links then get "Sync from Zotero..." for the shared document. This server fetches with the owner's key, which is never sent to them; they do not see the library settings.
- 6 previews a minute for all links together.
- View links never get it.

## Next

- [References](references.md)
- [Sharing a document](../editor/sharing.md)
- [Hosted features](../hosting/features.md)
