# Git

How to use the editor's Git tab to commit, pull, push and switch branches, for the owner of a local editor.

## Who can use it

The sidebar's Git tab is for the local owner only. Share links never see it, and the server refuses its API to them. It works on the repository that holds the document's folder, with your own `git` and its configuration (author, credential helper, SSH keys).

## What the tab shows and does

- The branch and how far it is ahead of or behind its upstream.
- The changed files. Click one for its diff; new files show their text.
- Stage, Unstage and Stage all.
- A commit message box and Commit.
- Pull: fetch, then fast-forward or merge, never rebase.
- Push: the first push of a branch sets its upstream. With several remotes it asks which.
- A branch menu to switch branches (remote branches included) and New branch.
- The last 50 commits.

Unsaved edits are saved first.

## Resolve a conflicting pull

A pull that conflicts stops with the conflicts in the files (`<<<<<<<` markers) and says so. Nothing is resolved for you.

1. Fix the conflicting files.
2. Stage them.
3. Commit, or choose Abort merge instead.

## Files open in live co-editing

Pull, switch and abort rewrite files while no save can land (the server's write lock). The open rooms then fold the change into the shared text like any edit made outside the editor; the room's leader merges it (its next save would otherwise be refused as out of date). If someone typed in the same lines meanwhile, the merge can fail and the editor says the room's version will replace the file's. Git then shows that as a change.

## How Git is run

- Git runs as a program with an argument list, never through a shell.
- The browser never chooses an option or path: paths must be ones `git status` listed and go after `--`; branch and remote names must be listed ones; pathspecs are literal.
- The commit message goes in on stdin.
- Every call has a timeout: 30 s, or 3 minutes for pull and push.
- Git never waits for a password prompt, and runs without `fsmonitor` or the `ext::` transport.

## Next

- [Collaboration](collaboration.md)
- [Editor overview](overview.md)
- [Hosted features](../hosting/features.md)
