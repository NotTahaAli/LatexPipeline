# References

How to manage `.bib` entries in the editor's References panel: browse, add, cite, look up and verify with Crossref, and keep a personal library.

## Who can do what

| Role | References |
| --- | --- |
| view | Sees the list read-only. |
| edit | Adds, edits, deletes, imports and cites entries; Look up, Verify and inserting from My library. |

Only the DOI, title or first author's surname is ever sent to Crossref (`api.crossref.org`), and only on a click. Look up and Verify never change anything until you apply it.

## Browse the references

Open the sidebar's References tab, or "Toggle references" in Ctrl+K. It lists every entry of every `.bib` in the document folder, one line each (author, year, title). Click a row for all its fields and the places it is cited.

- Switch between all files and one file. Files no source names are marked "not used by the document".
- Filter by key, author, title or year.
- Sort by author, year, title, key, citation count or file.

| Badge | Meaning |
| --- | --- |
| cited (count) | Cited that many times. |
| unused | Not cited. |
| duplicate | The key is defined in more than one file. |
| missing required fields | The lint rule; also gets the Look up button. |

Keys cited in the text but defined in no `.bib` are listed as missing, each with its `\cite` locations and "Create entry".

## Add, edit and cite (edit role)

- **New reference** (+) opens a form with the type's required fields first and the rest under "More fields". Add or remove any field. The key fills in as AuthorYearWord, unique, until you type one.
- **Edit** and **Delete** sit in each entry; Delete is under its "..." menu and warns when the entry is cited.
- **More (...) > "Import BibTeX, RIS or DOI..."** reads pasted entries or an export file (validated by the same parser; RIS is converted to BibTeX with generated keys), or asks Crossref for a DOI (only the DOI is sent; same limits as Look up).
- **cite** on a row, or ticking several rows and "Insert \cite{a,b}", inserts at the cursor of the `.tex` you were last in, into the `\cite{...}` the cursor is in, if any.

Changes go into the open `.bib` in the editor as one splice that keeps every other byte, so co-editing and autosave work as for typing. If the file changed since the list was read, nothing is applied and the list reloads.

## Fill in missing fields with Look up

A Lint finding "@article 'k' lacks ..." shows a Look up button (on hover or focus; always on touch screens) for the edit role.

1. Click Look up. It asks Crossref using the entry's DOI. With no DOI, it uses the title, and only when one record matches closely (title similarity >= 0.9 and a year within one); otherwise nothing is suggested.
2. Check the fields it would add.
3. Click Apply. Just those fields go into the open `.bib`, which then autosaves like any edit.

Existing fields are never replaced, the entry's indentation and style are kept, and other entries stay byte-for-byte. Check the values against the paper.

## Verify references with Crossref

References > More (...) > "Verify with Crossref" (edit role) checks the entries shown (all files or the one selected) one by one. It changes nothing.

| Badge | Meaning |
| --- | --- |
| verified | The record matches. |
| mismatch | The DOI's record has another title, the year is off by more than one, or another first author. Hover for which. |
| not found | The DOI does not resolve, or no record has a close title. |

- It uses the rule and limits of `ci_report.py lint --verify-citations` and Look up's rate limits. On a shared link that is 10 lookups a minute for all guests together.
- When it hits a limit it stops and says so; running it again continues with the entries not yet checked.
- Results last until the page reloads. An edited entry is checked again.

## Mendeley and other managers

Mendeley's API is closed to new apps, so there is no live sync. Export from Mendeley (or EndNote, a publisher, anything) as BibTeX or RIS and use More > "Import BibTeX, RIS or DOI...". For a live sync from Zotero, see [Zotero](zotero.md).

## My library

References > More > "My library..." is your own store of references, the same in every document.

| Where | Stored |
| --- | --- |
| Local editor | The owner's `library.json` in the same config folder as `zotero.json` (mode 600, never in a project). Shared links do not get it. |
| Hosted | One per account in `host.db`, answered by the gateway in every project you can open. Workers never see it. |

- Add by DOI (looked up on Crossref through the document's import, edit role), by pasting BibTeX or RIS, with a form, or by saving the references ticked in the panel.
- Entries with the same key, DOI, or title and year are marked Duplicate. Entries missing a field their type requires are marked too.
- Tick entries to insert them into one of the document's `.bib` files (one splice per entry into the open editor, edit role; keys already there are skipped), export them (or all) as a `.bib` download, or delete them. Documents keep their copies.
- At most 2000 entries and 3 MB.

## Next

- [Zotero](zotero.md)
- [Grammar and lint](grammar.md)
- [Writing in the editor](../editor/writing.md)
