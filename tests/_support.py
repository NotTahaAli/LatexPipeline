"""
Shared setup for the tests: puts scripts/ on sys.path and provides a
throwaway repository layout so the scripts never touch the real files/ or out/.
"""

from __future__ import annotations

import contextlib
import sys
import tempfile
from pathlib import Path
from unittest import mock

SCRIPTS_DIR = Path(__file__).resolve().parent.parent / "scripts"
if str(SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPTS_DIR))

import build  # noqa: E402
import ci_report  # noqa: E402
import publish_release  # noqa: E402

__all__ = ["build", "ci_report", "fake_repo", "publish_release", "write_doc"]


@contextlib.contextmanager
def fake_repo():
    """Point the scripts' path globals at a temporary repository; yields its root."""
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp).resolve()  # Resolved: the scripts compare resolved paths.
        (root / "files").mkdir()
        values = {
            "ROOT_DIR": root,
            "SOURCE_DIR": root / "files",
            "OUT_DIR": root / "out",
            "CACHE_DIR": root / ".latex-cache",
            "FILES_DIR": root / "files",
            "BENCH_DIR": root / "bench",
        }
        with contextlib.ExitStack() as stack:
            for module in (build, publish_release):
                for name, value in values.items():
                    if hasattr(module, name):
                        stack.enter_context(mock.patch.object(module, name, value))
            yield root


def write_doc(root: Path, name: str, main_tex: str, build_toml: str | None = None) -> Path:
    """Create files/<name>/main.tex (and optionally build.toml). Returns main.tex."""
    directory = root / "files" / name
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / "main.tex"
    path.write_text(main_tex, encoding="utf-8")
    if build_toml is not None:
        (directory / "build.toml").write_text(build_toml, encoding="utf-8")
    return path
