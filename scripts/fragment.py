"""
Typeset a selection (imported by serve.py only, for POST /api/typeset): the document's preamble, then the selected
LaTeX on an otherwise empty page, in one TeX run through sandbox.spawn() in a fresh folder of the document's cache.
The editor shows the PDF it returns and crops the white margin. No shell escape, whatever build.toml says; while
sharing, the environment carries the shared-link restrictions (openin_any=p ...) like every build.
stdlib-only, Python 3.9.
"""
from __future__ import annotations

import os
import re
import secrets
import shutil
import subprocess
import threading
from pathlib import Path

import build
import preview
import sandbox

MAX_CHARS = 20_000
TIMEOUT = 60  # CPU seconds, and twice that of wall time
LOCK = threading.Lock()  # one run at a time: a click that comes in meanwhile is told to wait
BODY = "\\begin{document}\n\\pagestyle{empty}\n%s\n\\end{document}\n"


class FragmentError(Exception):
    def __init__(self, message: str, status: int = 422) -> None:
        super().__init__(message)
        self.status = status


def first_error(log: str) -> str:
    """TeX's first complaint in a log, with the line it names."""
    m = re.search(r"^(?:\S*:\d+: |! )(.*)$", log, re.M)
    return m.group(1).strip()[:300] if m else "LaTeX could not typeset the selection."


def typeset(main_tex: Path, text) -> bytes:
    """The PDF of `text` set with main.tex's preamble; FragmentError says why not."""
    if not isinstance(text, str) or not text.strip() or len(text) > MAX_CHARS or "\0" in text:
        raise FragmentError(f"Select some LaTeX (at most {MAX_CHARS} characters) to typeset.", 400)
    try:
        settings = build.read_settings(main_tex)  # serve.py's guarded version while sharing
    except (build.ConfigError, OSError) as exc:
        raise FragmentError(str(exc))
    if settings["engine"] not in preview.ENGINES:
        raise FragmentError(f"Typesetting a selection does not support {settings['engine']}.")
    if not LOCK.acquire(blocking=False):
        raise FragmentError("Another selection is being typeset; try again in a moment.", 409)
    try:
        root = build.cache_dir_for(main_tex) / "fragment"
        work = root / secrets.token_hex(6)
        work.mkdir(parents=True)
        try:
            source = work / "fragment.tex"
            source.write_text(preview.preamble_of(main_tex.read_text(encoding="utf-8", errors="replace"))
                              + BODY % text, encoding="utf-8")
            command = [
                settings["engine"], "-interaction=nonstopmode", "-halt-on-error", "-file-line-error",
                f"-output-directory={work}", "-jobname=fragment",
                build.review_pretex(settings) + "\\input{" + preview.tex_ref(source, main_tex.parent) + "}",
            ]
            spec = sandbox.spawn(command, main_tex.parent, root, {**os.environ, **build.LATEX_LOG_ENV},
                                 cpu=TIMEOUT, work=work)
            try:
                subprocess.run(**spec, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                               stderr=subprocess.DEVNULL, timeout=TIMEOUT * 2, check=False)
            except subprocess.TimeoutExpired:
                raise FragmentError("Typesetting the selection took too long.")
            finally:
                sandbox.scrub(root)
            pdf = work / "fragment.pdf"
            if not pdf.is_file():
                log = work / "fragment.log"
                raise FragmentError(first_error(log.read_text("utf-8", "replace")) if log.is_file()
                                    else "LaTeX did not start.")
            return pdf.read_bytes()
        finally:
            shutil.rmtree(work, ignore_errors=True)
    finally:
        LOCK.release()
