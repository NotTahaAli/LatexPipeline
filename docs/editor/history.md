# Version history

How the editor keeps, compares, restores and cleans up versions of a document's files, for anyone editing in the editor.

## What makes a version

The Problems panel's History tab lists the versions of the open file (or of all files). A version is made by:

- every save in the editor (one person's saves of a file merge into one version for 5 minutes);
- changes made outside the editor (kept just before the editor overwrites them);
- deletes and renames in the Files tab;
- restores.

## Name a version

"Name this version" (edit role) saves the whole project under a name, such as "Sent to supervisor".

## View changes

View opens a line diff with the changed words marked: "Changes in this version" or "Compared with now". For a named version, pick the file. Diffs look at the first 2000 changed lines and say when they are cut short.

## Restore

- "Restore *file*" puts one file back.
- On a named version, "Restore all files" puts every file back (files added since are kept).
- A restore is itself a version, so it can be undone.

A restore never reverts newer edits. When the restored part was edited in between, it is skipped with a message and can be restored again from History.

Files open in live co-editing are restored through the shared document, so everyone in the room sees it at once. One editor in that file's room applies it as one edit (once, so two editors never apply it twice); if that browser closes first, another editor in the room takes over. Once no editor has the file open, the server writes it, but only if the file is still as it was when the restore was asked for.

## Download or compare a version

| Action | Who | Notes |
|---|---|---|
| Download zip | View role | Text files as they were in that version, images and other files as they are now, at most 64 MB |
| Compare with now (PDF) | Edit role | A latexdiff PDF of what changed between that version and now; counts against the rebuild rate limit |
| Last comparison (PDF) | Everyone else | `pdf/<doc>?diff=1`, once one exists |

The comparison is built from a copy in `.latex-cache/_diff/`; latexdiff and the build both run through the sandbox when it is on, like every LaTeX run. The PDF and its log are `out/<name>.diff.pdf` and `.diff.log`; they are never published to the release.

## Where history is stored

History lives in `.latex-history/<document>/` beside `.latex-cache/`. It is git-ignored, never a build input, and not removed by `--clean`. It is a content-addressed, zlib-compressed blob store deduplicated by SHA-256, with an sqlite index.

## How old versions thin out

Older versions thin out by themselves, a little on every save (the newest in each period stays):

| Age | Kept |
|---|---|
| Last 24 hours | Everything |
| Up to 7 days | One version per file per hour |
| Up to 90 days | One per day |
| Older | One per week |

- Named versions, each file's newest text and the text before a deletion are never thinned. Text no version uses any more is deleted.
- As a last resort it keeps up to 3000 file versions and 64 MB of compressed text per document, by dropping the oldest superseded versions.
- When nothing else may go, history stops recording rather than forgetting, and the History tab says so.

## Clean up

**Clean up** (the owner; hosted, workspace admins) deletes every version older than a week, a month, 3 months or a year. Named versions are deleted only when ticked; each file's newest version stays. Each named version also has Delete.

Both ask first and cannot be undone. Deleting versions is never possible through a share link.

## Who can do what

- Viewers can browse and compare.
- Naming and restoring need the edit role.
- Over a share link, `build.toml` and `latexmkrc` files are neither shown nor restored.

## Next

- [Working together](collaboration.md)
- [Sharing a document](sharing.md)
- [Security model](../explanation/security.md)
