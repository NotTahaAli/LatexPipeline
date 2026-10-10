"""
Builds the tagged-PDF fixtures in tests/fixtures/tagged and checks that veraPDF passes the standards each
one declares. Needs a current TeX Live (kernel 2025-06-01 or newer, for the PDF/UA-2 and PDF/A-4 rows) and
`verapdf` on PATH; CI runs it in the build-pdf workflow on the tug.org TeX Live. Not part of unittest discover.

    python3 tests/tagged_check.py
"""
import shutil
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SOURCE = "tests/fixtures/tagged"
PASSED = "veraPDF passed."
EXPECTED = {
    "ua1-pdflatex": ["PDF/UA-1: " + PASSED],
    "a2a-xelatex": ["PDF/A a-2a: " + PASSED, "PDF/UA-1: " + PASSED],
    "a4f-lualatex": ["PDF/A a-4f: " + PASSED, "PDF/UA-2: " + PASSED],
    "a4f-pdflatex": ["PDF/A a-4f: " + PASSED, "PDF/UA-2: " + PASSED],
}


def main() -> int:
    if not shutil.which("verapdf"):
        print("verapdf is not on PATH", file=sys.stderr)
        return 2
    result = subprocess.run([sys.executable, str(ROOT / "scripts" / "build.py"), "--source", SOURCE, "--force"],
                            cwd=ROOT)
    failed = result.returncode != 0
    for name, notes in EXPECTED.items():
        log = ROOT / "out" / f"{name}.log"
        text = log.read_text(encoding="utf-8", errors="replace") if log.exists() else ""
        missing = [note for note in notes if note not in text]
        print(f"{'ok  ' if not missing else 'FAIL'} {name}: " + ("; ".join(notes) if not missing else
              "missing " + "; ".join(missing)))
        if missing:
            failed = True
            for line in text.splitlines():
                if line.startswith(("NOTE:", "ERROR:", "WARNING:")):
                    print("     " + line)
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
