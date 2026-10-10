"""
Stage-2 experiment (not used by the pipeline): drive TeXpresso headless over its editor protocol and time how
long it takes to settle after an edit. Findings and numbers: docs/realtime.md.

    git clone https://github.com/let-def/texpresso && make -C texpresso all   # see its INSTALL.md
    python3 experiments/live/texpresso_probe.py texpresso/build/texpresso bench/sample-report \
        Chapters/chapter5/subsection1.tex --page 105 --edits 5

It copies the document to a temporary directory, starts `texpresso -texlive -json -lines` with SDL's dummy video
driver (no window), pages forward to --page (TeXpresso only typesets up to the page it shows), then replaces the
file's text through the protocol (`open`, the editor's unsaved buffer) and reports when the last message after
each edit arrived. TeXpresso draws into its own SDL window and sends no PDF or page images over the protocol.
"""

from __future__ import annotations

import argparse
import json
import os
import shutil
import statistics
import subprocess
import tempfile
import threading
import time
from pathlib import Path


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("binary")
    parser.add_argument("document", type=Path)
    parser.add_argument("file")
    parser.add_argument("--page", type=int, default=0)
    parser.add_argument("--edits", type=int, default=5)
    args = parser.parse_args()

    work = Path(tempfile.mkdtemp()) / "doc"
    shutil.copytree(args.document, work)
    proc = subprocess.Popen(
        [args.binary, "-texlive", "-json", "-lines", str(work / "main.tex")], cwd=work,
        env={**os.environ, "SDL_VIDEODRIVER": "dummy"}, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
        stderr=subprocess.DEVNULL, text=True, bufsize=1,
    )
    events: list[float] = []
    lock = threading.Lock()

    def reader() -> None:
        for _ in proc.stdout:
            with lock:
                events.append(time.monotonic())

    def settle(start: float, quiet: float) -> float:
        """Time of the last message once none arrived for `quiet` seconds."""
        while True:
            time.sleep(0.1)
            with lock:
                last = events[-1] if events else start
            if time.monotonic() - max(last, start) > quiet:
                return last

    threading.Thread(target=reader, daemon=True).start()
    try:
        t0 = time.monotonic()
        print(f"first page: {settle(t0, 5) - t0:.1f} s")
        for _ in range(args.page):
            proc.stdin.write('["next-page"]\n')
        proc.stdin.flush()
        t1 = time.monotonic()
        print(f"page forward {args.page} pages: {settle(t1, 5) - t1:.1f} s")
        text = (work / args.file).read_text(encoding="utf-8")
        times = []
        for i in range(args.edits):
            start = time.monotonic()
            proc.stdin.write(json.dumps(["open", args.file, text.replace("the", f"the{i}x", 1)]) + "\n")
            proc.stdin.flush()
            last = settle(start, 2)
            if last < start:
                print(f"edit {i}: no output (the page shown comes before the edit)")
                continue
            times.append(last - start)
            print(f"edit {i}: settled after {times[-1]:.2f} s")
        if times:
            print(f"median {statistics.median(times):.2f} s")
    finally:
        proc.kill()
        shutil.rmtree(work.parent, ignore_errors=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
