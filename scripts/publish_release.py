#!/usr/bin/env python3
"""
Sync out/ to the GitHub release tagged `pdfs`, which always holds the latest
PDF of every document.

- Uploads every PDF and <name>.log in out/ (replacing older ones).
  out/ holds only what this run built: a failed document keeps its previous
  PDF on the release, next to the log of the failed build.
- Deletes assets whose main.tex no longer exists.
- If BUILD_OK=true, moves the `pdfs` tag to the built commit. CI rebuilds
  everything changed since that tag, so skipped or failed runs are caught up.

Meant for CI after `build.py`; needs the `gh` CLI and GH_TOKEN.
"""

from __future__ import annotations

import os
import re
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

from build import OUT_DIR, find_documents, log_path_for, output_path_for

TAG = "pdfs"


def gh(*args: str) -> str:
    return subprocess.run(
        ["gh", *args], check=True, stdout=subprocess.PIPE, text=True
    ).stdout


def asset_name(pdf: Path) -> str:
    """
    out/reports/final_v2 report.pdf -> reports_2Ffinal_5Fv2_20report.pdf

    GitHub mangles spaces and cannot hold "/" in asset names, so escape every
    character outside [A-Za-z0-9.-] as "_" + its UTF-8 bytes in hex:
    " " -> "_20", "_" -> "_5F", "/" -> "_2F". Reversible, unlike plain "-".
    The original path is kept as the display label.
    """
    return re.sub(
        r"[^A-Za-z0-9.-]",
        lambda match: "".join(f"_{byte:02X}" for byte in match.group().encode()),
        pdf.relative_to(OUT_DIR).as_posix(),
    )


def main() -> int:
    commit = os.environ.get("GITHUB_SHA", "unknown commit")
    build_ok = os.environ.get("BUILD_OK") == "true"
    notes = f"Latest PDF of every document, built from {commit}."

    try:
        gh("release", "view", TAG)
        gh("release", "edit", TAG, "--notes", notes)
    except subprocess.CalledProcessError:
        # After a failed build, start the tag at the root commit so the next
        # run rebuilds everything.
        target = commit if build_ok else subprocess.run(
            ["git", "rev-list", "--max-parents=0", "HEAD"],
            check=True, stdout=subprocess.PIPE, text=True,
        ).stdout.split()[0]
        gh("release", "create", TAG, "--title", "Latest PDFs", "--notes", notes,
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

    if build_ok:
        print(f"Moving tag {TAG} to {commit}", flush=True)
        gh("api", "--method", "PATCH", f"repos/{{owner}}/{{repo}}/git/refs/tags/{TAG}",
           "-f", f"sha={commit}", "-F", "force=true")

    return 0


if __name__ == "__main__":
    sys.exit(main())
