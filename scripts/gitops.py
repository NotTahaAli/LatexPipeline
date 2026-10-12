"""
Git for the local editor's Git panel (serve.py, owner only) and the helpers host_git.py shares.

Runs the user's `git` CLI with argument lists, never a shell. Nothing a request carries becomes an option: paths
come after `--` and must be ones `git status` reported, branch and remote names must be listed ones, a commit
message goes in on stdin, and pathspecs are literal (GIT_LITERAL_PATHSPECS). Every call has a timeout and never
waits for a terminal prompt. Stdlib only, Python 3.9.
"""

from __future__ import annotations

import contextlib
import os
import shutil
import subprocess
from pathlib import Path

TIMEOUT = 30.0  # a local command (status, diff, commit)
NET_TIMEOUT = 180.0  # pull and push talk to a remote
MAX_OUTPUT = 512 * 1024  # characters of a diff or command output sent back
MAX_MESSAGE = 10_000
LOG_LIMIT = 100
# Ours, not the request's: no fsmonitor hook, never the ext:: transport (it runs a command).
SAFE_CONFIG = ("-c", "core.fsmonitor=false", "-c", "protocol.ext.allow=never", "-c", "core.quotePath=false")


class GitError(Exception):
    def __init__(self, message: str, status: int = 400, **extra) -> None:
        super().__init__(message)
        self.status, self.extra = status, extra


def git_path() -> str:
    found = shutil.which("git")
    if not found:
        raise GitError("Git is not installed (git is not on PATH).", 501)
    return found


def env(extra: dict | None = None) -> dict:
    out = dict(os.environ)
    out.update({"GIT_TERMINAL_PROMPT": "0", "GIT_LITERAL_PATHSPECS": "1", "LC_ALL": "C", "LANGUAGE": "C"})
    out.pop("GIT_DIR", None)
    out.pop("GIT_WORK_TREE", None)
    out.update(extra or {})
    return out


def run(cwd: Path, args: list, timeout: float = TIMEOUT, stdin: str | None = None, check: bool = True,
        prefix: tuple = (), environ: dict | None = None) -> subprocess.CompletedProcess:
    """git <prefix> <args> in cwd. check: a non-zero exit raises GitError with git's own message."""
    try:
        # start_new_session: no controlling terminal, so ssh cannot ask for a passphrase either (POSIX; ignored on
        # Windows). shortcut: only git itself is killed on a timeout, not a credential helper it started.
        done = subprocess.run([git_path(), *SAFE_CONFIG, *prefix, *args], cwd=str(cwd), input=stdin,
                              stdin=None if stdin is not None else subprocess.DEVNULL, capture_output=True,
                              text=True, encoding="utf-8", errors="replace", timeout=timeout,
                              env=environ or env(), start_new_session=os.name == "posix")
    except subprocess.TimeoutExpired:
        raise GitError(f"git {args[0]} took longer than {int(timeout)} s and was stopped.", 504)
    except OSError as exc:
        raise GitError(f"Could not run git: {exc}", 500)
    if check and done.returncode:
        raise GitError(output(done) or f"git {args[0]} failed.", 409)
    return done


def output(done: subprocess.CompletedProcess) -> str:
    text = ((done.stdout or "") + (done.stderr or "")).strip()
    return text if len(text) <= MAX_OUTPUT else text[:MAX_OUTPUT] + "\n[cut short]"


def repo_root(folder: Path) -> Path | None:
    done = run(folder, ["rev-parse", "--show-toplevel"], check=False)
    return Path(done.stdout.strip()) if done.returncode == 0 and done.stdout.strip() else None


def need_root(folder: Path) -> Path:
    root = repo_root(folder)
    if root is None:
        raise GitError("This document's folder is not in a Git repository (run `git init` there first).", 404)
    return root


# --- reading ---------------------------------------------------------------------------------------------------

def parse_status(raw: str) -> dict:
    """`git status --porcelain=v2 --branch -z` -> {branch, head, upstream, ahead, behind, files}."""
    info: dict = {"branch": None, "head": None, "upstream": None, "ahead": 0, "behind": 0, "files": []}
    parts = raw.split("\0")
    i = 0
    while i < len(parts):
        row = parts[i]
        i += 1
        if not row:
            continue
        if row.startswith("# "):
            key, _, value = row[2:].partition(" ")
            if key == "branch.head":
                info["branch"] = None if value == "(detached)" else value
            elif key == "branch.oid":
                info["head"] = None if value == "(initial)" else value
            elif key == "branch.upstream":
                info["upstream"] = value
            elif key == "branch.ab":
                ahead, behind = value.split()
                info["ahead"], info["behind"] = int(ahead), -int(behind)
        elif row[0] == "1":
            fields = row.split(" ", 8)
            info["files"].append({"path": fields[8], "index": fields[1][0], "worktree": fields[1][1]})
        elif row[0] == "2":
            fields = row.split(" ", 9)
            info["files"].append({"path": fields[9], "index": fields[1][0], "worktree": fields[1][1],
                                  "from": parts[i]})
            i += 1  # the original path follows as its own field
        elif row[0] == "u":
            fields = row.split(" ", 10)
            info["files"].append({"path": fields[10], "index": "U", "worktree": "U", "conflict": True})
        elif row[0] == "?":
            info["files"].append({"path": row[2:], "index": "?", "worktree": "?"})
    return info


def status(folder: Path) -> dict:
    root = repo_root(folder)
    if root is None:
        return {"repo": False}
    done = run(root, ["status", "--porcelain=v2", "--branch", "-z", "--untracked-files=all"],
               environ=env({"GIT_OPTIONAL_LOCKS": "0"}))  # reading must not take the index lock
    info = parse_status(done.stdout)
    merging = run(root, ["rev-parse", "-q", "--verify", "MERGE_HEAD"], check=False).returncode == 0
    remotes = run(root, ["remote"]).stdout.split()
    return {"repo": True, "root": str(root), "merging": merging, "remotes": remotes, **info}


def known_paths(root: Path) -> set:
    files = parse_status(run(root, ["status", "--porcelain=v2", "-z", "--untracked-files=all"]).stdout)["files"]
    return {f["path"] for f in files} | {f["from"] for f in files if f.get("from")}


def check_paths(root: Path, paths) -> list:
    """Only paths `git status` lists right now: a request cannot name an option, a glob or a file elsewhere."""
    if not isinstance(paths, list) or not paths or len(paths) > 1000:
        raise GitError("Pick one or more files.", 400)
    known = known_paths(root)
    for path in paths:
        if not isinstance(path, str) or path not in known or path.startswith("-"):
            raise GitError(f"Not a changed file: {str(path)[:200]}", 400)
    return paths


def diff(folder: Path, path, staged: bool) -> dict:
    root = need_root(folder)
    check_paths(root, [path])
    entry = next(f for f in parse_status(run(root, ["status", "--porcelain=v2", "-z", "--untracked-files=all"])
                                         .stdout)["files"] if path in (f["path"], f.get("from")))
    if entry["index"] == "?":  # untracked: no diff, the whole file is new
        target = root / path
        try:
            text = target.read_bytes()[:MAX_OUTPUT].decode("utf-8", "replace") if target.is_file() else ""
        except OSError:
            text = ""
        return {"path": path, "untracked": True, "text": text}
    args = ["diff", "--no-color", "--no-ext-diff", "--no-textconv"] + (["--cached"] if staged else []) + ["--", path]
    text = run(root, args).stdout
    return {"path": path, "staged": staged, "diff": text[:MAX_OUTPUT], "cut": len(text) > MAX_OUTPUT}


def log(folder: Path, limit: int = 50) -> dict:
    root = need_root(folder)
    done = run(root, ["log", f"-n{max(1, min(int(limit), LOG_LIMIT))}", "-z",
                      "--format=%H%x1f%an%x1f%ae%x1f%at%x1f%s"], check=False)
    commits = []
    for row in done.stdout.split("\0") if done.returncode == 0 else []:  # an unborn branch has no log
        fields = row.strip("\n").split("\x1f")
        if len(fields) == 5:
            commits.append({"id": fields[0], "author": fields[1], "email": fields[2], "time": int(fields[3]),
                            "subject": fields[4]})
    return {"commits": commits}


def branches(folder: Path) -> dict:
    root = need_root(folder)
    rows = run(root, ["for-each-ref", "--format=%(refname)%00%(HEAD)%00%(upstream:short)",
                      "refs/heads", "refs/remotes"]).stdout.splitlines()
    local, remote = [], []
    for row in rows:
        ref, head, upstream = (row.split("\0") + ["", ""])[:3]
        if ref.startswith("refs/heads/"):
            local.append({"name": ref[11:], "current": head == "*", "upstream": upstream or None})
        elif not ref.endswith("/HEAD"):
            remote.append(ref[13:])
    if not any(b["current"] for b in local):  # an unborn branch (no commit yet) has no ref to list
        head = run(root, ["symbolic-ref", "-q", "--short", "HEAD"], check=False).stdout.strip()
        if head:
            local.insert(0, {"name": head, "current": True, "upstream": None})
    return {"local": local, "remote": remote, "remotes": run(root, ["remote"]).stdout.split()}


# --- changing ---------------------------------------------------------------------------------------------------

def check_branch(root: Path, name) -> str:
    if not isinstance(name, str) or not name or name.startswith("-") or len(name) > 200 \
            or run(root, ["check-ref-format", f"refs/heads/{name}"], check=False).returncode:
        raise GitError("Not a valid branch name.", 400)
    return name


def stage(folder: Path, paths) -> dict:
    root = need_root(folder)
    run(root, ["add", "-A", "--", *check_paths(root, paths)])
    return {"ok": True}


def unstage(folder: Path, paths) -> dict:
    root = need_root(folder)
    run(root, ["reset", "-q", "--", *check_paths(root, paths)])  # also fine before the first commit
    return {"ok": True}


def commit(folder: Path, message) -> dict:
    root = need_root(folder)
    if not isinstance(message, str) or not message.strip() or len(message) > MAX_MESSAGE or "\0" in message:
        raise GitError("Write a commit message.", 400)
    done = run(root, ["commit", "-q", "-F", "-"], stdin=message, check=False)  # author: the user's git config
    if done.returncode:
        raise GitError(output(done) or "Nothing to commit.", 409)
    return {"ok": True, "head": run(root, ["rev-parse", "HEAD"]).stdout.strip()}


def conflicts(root: Path) -> list:
    return [p for p in run(root, ["diff", "--name-only", "-z", "--diff-filter=U"], check=False).stdout.split("\0") if p]


def pull(folder: Path, lock=None) -> dict:
    """
    Fetch, then fast-forward or merge the upstream (never rebase). Conflicts are left for the user to resolve;
    nothing is auto-resolved. lock: held only while the merge rewrites files, not during the network part.
    """
    root = need_root(folder)
    if not status(folder)["upstream"]:
        raise GitError("This branch has no upstream branch to pull from (push it once first).", 409)
    run(root, ["fetch", "--no-recurse-submodules"], timeout=NET_TIMEOUT)
    with lock or contextlib.nullcontext():
        done = run(root, ["merge", "--no-edit", "@{upstream}"], check=False)
    found = conflicts(root)
    if done.returncode and not found:
        raise GitError(output(done) or "git merge failed.", 409)
    return {"ok": not found, "conflicts": found, "output": output(done)}


def push(folder: Path, remote=None) -> dict:
    root = need_root(folder)
    info = status(folder)
    if info["upstream"]:
        args = ["push"]
    else:  # first push of this branch: to a listed remote, setting the upstream
        if not info["branch"]:
            raise GitError("Switch to a branch before pushing.", 409)
        remote = remote or (info["remotes"][0] if len(info["remotes"]) == 1 else None)
        if remote not in info["remotes"]:
            raise GitError("Pick a remote to push to." if info["remotes"] else "This repository has no remote.", 400)
        args = ["push", "-u", remote, f"HEAD:refs/heads/{info['branch']}"]
    done = run(root, args, timeout=NET_TIMEOUT, check=False)
    if done.returncode:
        raise GitError(output(done) or "git push failed.", 409)
    return {"ok": True, "output": output(done)}


def switch(folder: Path, name) -> dict:
    """Switch to a listed local branch, or to a remote-tracking one (git makes the local branch)."""
    root = need_root(folder)
    listed = branches(folder)
    locals_ = {b["name"] for b in listed["local"]}
    if name in locals_:
        target = name
    elif name in listed["remote"]:
        target = name.split("/", 1)[1]
    else:
        raise GitError("No such branch.", 404)
    check_branch(root, target)
    done = run(root, ["switch", target], check=False)
    if done.returncode:
        raise GitError(output(done), 409)
    return {"ok": True, "output": output(done)}


def create(folder: Path, name) -> dict:
    root = need_root(folder)
    run(root, ["switch", "-c", check_branch(root, name)])
    return {"ok": True}


def abort_merge(folder: Path) -> dict:
    root = need_root(folder)
    run(root, ["merge", "--abort"])
    return {"ok": True}


def operate(folder: Path, data: dict, lock=None) -> dict:
    """One POST of the Git panel. lock (serve.WRITE_LOCK) is held while git rewrites files in the working tree, so
    no editor save lands half-way; open co-editing rooms then merge the change like any other edit on disk."""
    op, guard = data.get("op"), lock or contextlib.nullcontext()
    if op == "pull":
        return pull(folder, lock)
    if op in ("switch", "abort"):
        with guard:
            return switch(folder, data.get("name")) if op == "switch" else abort_merge(folder)
    ops = {"stage": lambda: stage(folder, data.get("paths")), "unstage": lambda: unstage(folder, data.get("paths")),
           "commit": lambda: commit(folder, data.get("message")), "push": lambda: push(folder, data.get("remote")),
           "create": lambda: create(folder, data.get("name"))}
    if op not in ops:
        raise GitError("Unknown Git operation.", 400)
    return ops[op]()
