# Working together

How several people edit, comment on and suggest changes to one document in the editor, for anyone writing with others.

## Edit live with others

Everyone who opens the same document edits it live (Yjs). You see each other's cursors and selections with names and colours. A stack of avatars in the top bar opens the list of people.

- The server only relays the changes. One editor per file, the "leader", saves to disk through the normal atomic save; if the leader leaves, another takes over.
- If you change a file outside the editor (git, vim) while it is open, the change is merged into the shared text instead of overwriting anyone's typing.
- If the connection drops, keep typing. The edits merge when it returns, over WebSocket or the long-poll fallback.

## Comment on text

1. Select text and press **Comment** (Ctrl+Alt+M) to start a thread.
2. The thread shows as a highlight with a mark in the gutter; click the mark to open it. It is also listed in the Review tab.
3. In the Review tab, reply, resolve or reopen threads, and delete your own comments (the local owner may delete any).

Comment text is shown as plain text, never as HTML.

## Suggest changes

**Suggest** (Ctrl+Alt+S) turns on track changes for you. What you type and delete no longer changes the text but becomes a suggestion, shown struck through (deleted) and underlined (inserted). A run of typing is one suggestion, sent after 1.5 s idle or when you move away.

| Action | Who |
|---|---|
| Accept (the change goes into the text through the normal editing path, live for everyone) or Reject | Edit role |
| Accept all / Reject all, optionally for this file only | Edit role |
| Withdraw | The author of the suggestion |

- Suggest mode handles typing, deleting and pasting.
- Typing with several cursors (Ctrl+click, Ctrl+D) makes one suggestion with several ranges, shown as "One edit in N places", accepted or rejected as a whole. If one of its ranges can no longer be found, none of it is applied.
- In suggest mode Ctrl+Z and Ctrl+Y (Ctrl+Shift+Z) work on your own suggestions only, never on the text. Undo shortens or drops the suggestion you are typing, then withdraws your last sent one; redo brings it back.
- Edits from other people keep arriving while you suggest; they are never mistaken for your typing.

**Input methods that compose text** (Chinese, Japanese, Korean, dead keys, phone keyboards): the composed text is kept out of the shared document and the file until the composition ends, then becomes a suggestion. A composition is ended after 5 s, when the editor loses focus or the tab is hidden, because others' edits wait and nothing is saved meanwhile. If the shared text changes underneath it, the composed text is dropped and you type it again.

## Open a suggestion card

Hover a suggestion, click its gutter mark, or put the cursor in it and press Alt+Enter (palette: "Open the suggestion at the cursor"). The card shows who suggested it, when (relative; hover for the exact time), the old and new text, and the replies.

- Suggestions take replies like comment threads (same limits; edit and review roles).
- The card has Accept and Reject for the edit role, Withdraw on your own suggestion, and nothing to decide for reviewers.
- Escape closes it.

## How comments follow edits

Comments and suggestions are anchored by position plus the quoted text and some context, so they follow edits and are found again after the text moves. If the quoted text is gone, the item stays in the list. An accept whose text changed is not applied and stays open.

## Who can comment and decide

| Role | Comments and suggestions |
|---|---|
| View | Reads both; can export |
| Review (a "can review" named link, or the hosted reviewer role) | Comments, replies, resolves, deletes its own comments; Suggest is always on, so whatever it types becomes a suggestion; withdraws its own suggestions |
| Edit | All of the above, plus Accept and Reject |

A reviewer cannot accept or reject, save, change the file tree, rebuild, preview, name or restore versions, or use grammar checks, the assistant or Zotero. The server refuses its co-editing updates, so it sees the live text and who is there but never changes it.

## Review limits

| Item | Limit |
|---|---|
| Threads per document | 1000 |
| Comments per thread | 200 |
| Characters per comment | 4000 |
| Open suggestions | 2000 |
| Characters per suggestion | 20000 |

Review data is stored per document in `.latex-history/<document>/review.json`.

## Mention people

Type `@` in a comment or reply to pick someone from the people of the document (named links locally, workspace members hosted). `@Name` is plain text; nothing else is sent.

The bell in the top bar lists mentions of you and replies on your threads and suggestions, newest first. What you have seen is remembered in this browser. There are no e-mail notifications (the hosted gateway has no mail sender).

## Comment on the PDF

1. Click the comment button above the PDF (palette: "Comment on the PDF...").
2. Click a point or drag over a region.
3. SyncTeX finds the source lines, selects them, and an ordinary comment thread starts there.

Needs the edit or review role and a build with SyncTeX.

## Export comments or build with suggestions

- **Export...** in the Review tab downloads every thread (open and resolved, with replies, authors, times, file:line and the quoted text) and every open suggestion (old and new text, author) as Markdown or JSON. Viewers can export too; over a share link it holds only what the link can read.
- **More > Build with suggestions** (edit role) typesets a copy of the document with the open suggestions marked (green background, red strike-through) and the unresolved comments as notes, and offers it to open or download. Viewers can open `pdf/<doc>?review=1` once it exists. What it skips and why is in its message and log; see [Review markup](../guides/review-markup.md).

## Lock a document

The owner (hosted: a workspace admin) can lock a document from the palette ("Lock the document"). Until it is unlocked nobody, the owner included, can save, change the file tree, upload, restore, suggest or accept, and co-editing updates are refused.

A "Locked" chip shows it. Comments still work. The server enforces the lock and stores it in `.latex-history/<document>/lock.json`.

## Copy files and upload folders

- The Files tab can copy a file or folder (palette: "Copy file or folder..."). The copy gets its own history.
- To upload a whole folder, drop a directory on the file list or use the folder button.
- Each file of the folder passes the same checks as an upload (text, or PNG, JPEG and PDF whose contents match). An existing file is never overwritten, and over a link build-config files are refused.

## Link a file

The owner can link a file (palette: "Link a file from a URL or another document..."). Its content is fetched from an `https://` address or copied from another document of this editor. Local editor only: hosted workers cannot fetch.

- Fetching: no redirect to anything but https, no user name or password in the address, the usual size limits, 100 linked files per document.
- Linked files are read-only and marked "linked" in the tree.
- Refresh (edit role, 10 a minute) fetches them again.
- Unlink (owner) makes one an ordinary file again, and so does renaming it.

## Recover unsaved text

Text you typed but that was not saved yet is kept in this browser (IndexedDB, per file, 30 days). When a file opens and that text differs from the file, the editor offers to recover it.

## Next

- [Version history](history.md)
- [Sharing a document](sharing.md)
- [How real-time editing works](../explanation/realtime.md)
