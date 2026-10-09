#!/usr/bin/env python3
"""
Sync out/ to the GitHub release tagged `pdfs`, which always holds the latest
PDF of every document.

- Uploads every PDF and <name>.log in out/ (replacing older ones).
  out/ holds only what this run built: a failed document keeps its previous
  PDF on the release, next to the log of the failed build.
- Deletes assets whose main.tex no longer exists.
- Release notes: a table of every document (links, pages, last build). The
  state behind it is kept in the notes themselves, see release_notes().
- If BUILD_OK=true, moves the `pdfs` tag to the built commit. CI rebuilds
  everything changed since that tag, so skipped or failed runs are caught up.

Meant for CI after `build.py`; needs the `gh` CLI and GH_TOKEN.
"""

from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

from build import (
    OUT_DIR,
    SOURCE_DIR,
    escape_name,
    find_documents,
    log_path_for,
    output_path_for,
)
from ci_report import cell, load_documents

TAG = "pdfs"
STATE_RE = re.compile(r"<!-- latex-pipeline-state: (.*?) -->", re.DOTALL)


def gh(*args: str) -> str:
    return subprocess.run(
        ["gh", *args], check=True, stdout=subprocess.PIPE, text=True
    ).stdout


def asset_name(pdf: Path) -> str:
    """
    out/reports/final_v2 report.pdf -> reports_2Ffinal_5Fv2_20report.pdf

    GitHub mangles spaces and cannot hold "/" in asset names, so the path is
    escaped by build.escape_name. The original path is kept as the label.
    """
    return escape_name(pdf.relative_to(OUT_DIR).as_posix())


def read_state(body: str) -> dict:
    """Per-document state stored by the previous run's release notes."""
    match = STATE_RE.search(body)
    try:
        return json.loads(match.group(1)) if match else {}
    except ValueError:
        return {}


def release_notes(previous: dict, built: list[dict], commit: str) -> str:
    """
    Notes with one row per document on disk.

    A document built in this run gets its new status. A document that was not
    built keeps its previous row. A failed build keeps the PDF of its last
    success (`pdf_commit`). The state is stored in an HTML comment at the end,
    so the next run can read it back.
    """
    built_by_name = {doc["name"]: doc for doc in built}
    repo = os.environ.get("GITHUB_REPOSITORY", "")
    download = f"https://github.com/{repo}/releases/download/{TAG}/"
    state = {}
    rows = []
    for main_tex in find_documents():
        name = main_tex.parent.relative_to(SOURCE_DIR).as_posix()
        old = previous.get(name, {})
        doc = built_by_name.get(name)
        if doc is None:
            new = old
        elif doc["ok"]:
            new = {"pdf_commit": commit, "pages": doc.get("pages"),
                   "status": "ok", "commit": commit}
        else:
            new = {"pdf_commit": old.get("pdf_commit"), "pages": old.get("pages"),
                   "status": "failed", "commit": commit}
        # Logs are uploaded for every document with one in out/, even a failed one.
        # A log not in out/ this run stays on the release from an earlier run.
        new["log"] = log_path_for(main_tex).exists() or old.get("log", old.get("pdf_commit") is not None)
        state[name] = new

        pdf = (f"[PDF]({download}{asset_name(output_path_for(main_tex))})"
               if new.get("pdf_commit") else None)
        log = f"[log]({download}{asset_name(log_path_for(main_tex))})" if new["log"] else None
        last_build = f"{new.get('status')} ({new['commit'][:7]})" if new.get("commit") else None
        cells = [name, pdf, new.get("pages"), (new.get("pdf_commit") or "")[:7] or None,
                 last_build, log]
        rows.append("| " + " | ".join(cell(value) for value in cells) + " |")

    # "--" is not allowed inside an HTML comment, so escape it in the JSON.
    encoded = json.dumps(state, sort_keys=True).replace("--", "\\u002d\\u002d")
    return "\n".join([
        (f"Latest PDF of every document, built from {commit[:7]}. "
         "A document whose latest build failed keeps the PDF named in \"PDF from\"."),
        "",
        "| Document | PDF | Pages | PDF from | Last build | Log |",
        "|---|---|---|---|---|---|",
        *rows,
        "",
        f"<!-- latex-pipeline-state: {encoded} -->",
        "",
    ])


def main() -> int:
    commit = os.environ.get("GITHUB_SHA", "unknown commit")
    build_ok = os.environ.get("BUILD_OK") == "true"

    try:
        previous = gh("release", "view", TAG, "--json", "body", "--jq", ".body")
    except subprocess.CalledProcessError:
        previous = None

    if previous is None:
        # After a failed build, start the tag at the root commit so the next
        # run rebuilds everything. The notes are written after the uploads.
        target = commit if build_ok else subprocess.run(
            ["git", "rev-list", "--max-parents=0", "HEAD"],
            check=True, stdout=subprocess.PIPE, text=True,
        ).stdout.split()[0]
        gh("release", "create", TAG, "--title", "Latest PDFs", "--notes", "",
           "--target", target)

    with tempfile.TemporaryDirectory() as staging:
        for path in sorted([*OUT_DIR.rglob("*.pdf"), *OUT_DIR.rglob("*.log")]):
            label = path.relative_to(OUT_DIR).as_posix()
            staged = Path(staging) / asset_name(path)
            shutil.copy(path, staged)
            print(f"Uploading: {label}", flush=True)
            gh("release", "upload", TAG, f"{staged}#{label}", "--clobber")

    documents = find_documents()
    expected = {asset_name(output_path_for(document)) for document in documents}
    expected |= {asset_name(log_path_for(document)) for document in documents}

    existing = gh("release", "view", TAG, "--json", "assets", "--jq", ".assets[].name").split()

    for name in existing:
        if name not in expected:
            print(f"Deleting: {name}", flush=True)
            gh("release", "delete-asset", TAG, name, "--yes")

    notes = release_notes(read_state(previous or ""), load_documents(), commit)
    gh("release", "edit", TAG, "--notes", notes)

    if build_ok:
        print(f"Moving tag {TAG} to {commit}", flush=True)
        gh("api", "--method", "PATCH", f"repos/{{owner}}/{{repo}}/git/refs/tags/{TAG}",
           "-f", f"sha={commit}", "-F", "force=true")

    return 0


if __name__ == "__main__":
    sys.exit(main())
