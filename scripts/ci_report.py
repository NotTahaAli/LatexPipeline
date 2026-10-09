#!/usr/bin/env python3
"""
CI reports for build-pdf.yml, built from out/build-report.json (written by build.py).

Subcommands:

- summary     Markdown table of this run's documents, for $GITHUB_STEP_SUMMARY.
- lint        chktex warnings and word counts into out/lint-report.json.
- diff        latexdiff of every document changed since --base, PDFs into --out
              (keep --out outside out/, so the PDFs are not published to the release).
- pr-comment  Create or update the sticky pull request comment (needs gh and GH_TOKEN).

Stdlib only. A missing chktex, texcount or latexdiff leaves its values blank
instead of failing the run.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path, PurePosixPath

from build import OUT_DIR, ROOT_DIR, SOURCE_DIR

REPORT_PATH = OUT_DIR / "build-report.json"
LINT_PATH = OUT_DIR / "lint-report.json"
DIFF_REPORT = "diff-report.json"
MARKER = "<!-- latex-pipeline -->"
TOP_N = 5  # chktex warnings listed per document.
ENGINE_FLAGS = {"pdflatex": "-pdf", "xelatex": "-pdfxe", "lualatex": "-pdflua"}
CHKTEX_WARNING = re.compile(r"^Warning \d+ in (.+) line (\d+): (.*)$", re.MULTILINE)
MAGIC_ENGINE = re.compile(
    r"^\s*%\s*!TEX\s+(?:TS-)?program\s*=\s*(\w+)", re.IGNORECASE | re.MULTILINE
)


# ---------------------------------------------------------------------------
# Shared helpers
# ---------------------------------------------------------------------------

def load_json(path: Path) -> dict | None:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None


def load_documents() -> list[dict]:
    """Documents built in this run. Empty if build-report.json is missing."""
    report = load_json(REPORT_PATH)
    return report.get("documents", []) if report else []


def cell(value) -> str:
    """Markdown table cell: blank for None, pipes and newlines escaped."""
    if value is None:
        return "-"
    return str(value).replace("|", "\\|").replace("\n", " ")


def gh(*args: str) -> str:
    return subprocess.run(
        ["gh", *args], check=True, capture_output=True, text=True
    ).stdout


# ---------------------------------------------------------------------------
# summary / pr-comment
# ---------------------------------------------------------------------------

def first_error(doc: dict) -> str | None:
    errors = doc.get("errors") or []
    if not errors:
        return None
    error = errors[0]
    where = error.get("file", "")
    if error.get("line") is not None:
        where = f"{where}:{error['line']}"
    return f"{where}: {error.get('message', '')}"


def table(documents: list[dict], lint: dict) -> list[str]:
    columns = ["Document", "Status", "Pages", "Words", "Time", "Warnings", "chktex", "First error"]
    rows = ["| " + " | ".join(columns) + " |", "|" + "---|" * len(columns)]
    for doc in documents:
        info = lint.get(doc["name"], {})
        seconds = doc.get("seconds")
        cells = [
            doc["name"],
            "ok" if doc["ok"] else "**failed**",
            doc.get("pages"),
            info.get("words"),
            None if seconds is None else f"{seconds:.1f} s",
            doc.get("warnings"),
            info.get("chktex"),
            first_error(doc),
        ]
        rows.append("| " + " | ".join(cell(value) for value in cells) + " |")
    return rows


def report_markdown(heading: str) -> str:
    """The build table with chktex details. `heading` is "##" or "###"."""
    documents = load_documents()
    if not documents:
        return f"{heading} LaTeX build\n\nNo documents were built (see the run log).\n"

    lint = (load_json(LINT_PATH) or {}).get("documents", {})
    failed = sum(not doc["ok"] for doc in documents)
    lines = [
        f"{heading} LaTeX build: {len(documents) - failed} ok, {failed} failed",
        "",
        *table(documents, lint),
        "",
    ]

    details = []
    for doc in documents:
        info = lint.get(doc["name"]) or {}
        if info.get("chktex"):
            details.append(f"**{doc['name']}** ({info['chktex']} warnings)")
            details += [f"- {warning}" for warning in info.get("chktex_top", [])]
    if details:
        lines += [
            f"<details><summary>chktex warnings (first {TOP_N} per document)</summary>",
            "",
            *details,
            "",
            "</details>",
            "",
        ]
    return "\n".join(lines) + "\n"


def cmd_summary(_args: argparse.Namespace) -> None:
    print(report_markdown("##"), end="")


def diff_section(diff_dir: Path) -> list[str]:
    report = load_json(diff_dir / DIFF_REPORT)
    if report is None:
        return []
    lines = ["", "### latexdiff", ""]
    documents = report.get("documents", {})
    if not documents:
        lines.append("No changed document to compare.")
    for name, result in documents.items():
        if result["ok"]:
            lines.append(f"- `{name}`: compiled, see the `diff-pdfs` artifact.")
        else:
            error = result.get("error", "unknown error")
            lines.append(f"- `{name}`: failed ({error}).")
    return lines


def comment_body(diff_dir: Path | None) -> str:
    server = os.environ.get("GITHUB_SERVER_URL", "https://github.com")
    run_url = (
        f"{server}/{os.environ.get('GITHUB_REPOSITORY', '')}"
        f"/actions/runs/{os.environ.get('GITHUB_RUN_ID', '')}"
    )
    lines = [MARKER, report_markdown("###").rstrip(), ""]
    if sha := os.environ.get("GITHUB_SHA"):
        lines.append(f"Built commit `{sha[:7]}`.")
    lines.append(f"Artifacts for this run: [`pdfs` and `diff-pdfs`]({run_url}).")
    if diff_dir is not None:
        lines += diff_section(diff_dir)
    return "\n".join(lines) + "\n"


def cmd_pr_comment(args: argparse.Namespace) -> None:
    body = comment_body(args.diff_dir)
    if args.print_only:
        print(body, end="")
        return

    event_path = os.environ.get("GITHUB_EVENT_PATH")
    event = load_json(Path(event_path)) if event_path else None
    pull = (event or {}).get("pull_request")
    if pull is None:
        print("Not a pull request event: no comment.")
        return
    repo = os.environ["GITHUB_REPOSITORY"]
    if pull["head"]["repo"]["full_name"] != repo:
        # A fork's token is read-only, so it cannot comment.
        print("Pull request from a fork: no comment.")
        return

    number = pull["number"]
    ids = gh(
        "api", "--paginate", f"repos/{repo}/issues/{number}/comments",
        "--jq", f'.[] | select(.body | contains("{MARKER}")) | .id',
    ).split()
    with tempfile.TemporaryDirectory() as tmp:
        payload = Path(tmp) / "comment.json"
        payload.write_text(json.dumps({"body": body}), encoding="utf-8")
        if ids:
            gh("api", "--method", "PATCH", f"repos/{repo}/issues/comments/{ids[0]}",
               "--input", str(payload))
            print(f"Updated comment {ids[0]} on PR #{number}.")
        else:
            gh("api", "--method", "POST", f"repos/{repo}/issues/{number}/comments",
               "--input", str(payload))
            print(f"Posted comment on PR #{number}.")


# ---------------------------------------------------------------------------
# lint
# ---------------------------------------------------------------------------

def count_words(doc_dir: Path) -> int | None:
    """Words in main.tex and everything it inputs."""
    if shutil.which("texcount") is None:
        return None
    result = subprocess.run(
        ["texcount", "-1", "-sum", "-inc", "-q", "main.tex"],
        cwd=doc_dir, check=False, capture_output=True, text=True,
    )
    text = result.stdout.strip()
    return int(text) if result.returncode == 0 and text.isdigit() else None


def run_chktex(doc_dir: Path) -> list[str]:
    """chktex warnings for every .tex file in the document, as "file:line: message"."""
    warnings = []
    for tex in sorted(doc_dir.rglob("*.tex")):
        rel = tex.relative_to(doc_dir).as_posix()
        result = subprocess.run(
            ["chktex", "-q", rel], cwd=doc_dir, check=False, capture_output=True, text=True,
        )
        # chktex exits non-zero when it finds warnings, so only the output counts.
        warnings += [f"{file}:{line}: {msg}"
                     for file, line, msg in CHKTEX_WARNING.findall(result.stdout)]
    return warnings


def cmd_lint(_args: argparse.Namespace) -> None:
    lint = {}
    for doc in load_documents():
        doc_dir = SOURCE_DIR / doc["name"]
        warnings = run_chktex(doc_dir) if shutil.which("chktex") else None
        lint[doc["name"]] = {
            "words": count_words(doc_dir),
            "chktex": None if warnings is None else len(warnings),
            "chktex_top": (warnings or [])[:TOP_N],
        }
        print(f"{doc['name']}: words={lint[doc['name']]['words']} "
              f"chktex={lint[doc['name']]['chktex']}")
    OUT_DIR.mkdir(exist_ok=True)
    LINT_PATH.write_text(json.dumps({"documents": lint}, indent=2), encoding="utf-8")


# ---------------------------------------------------------------------------
# diff (latexdiff)
# ---------------------------------------------------------------------------

def git_paths(*args: str) -> list[str]:
    """Output of a git command run with -z, split on NUL (paths are not quoted)."""
    output = subprocess.run(
        ["git", *args], cwd=ROOT_DIR, check=True, capture_output=True,
    ).stdout
    return [path for path in output.decode("utf-8").split("\0") if path]


def engine_of(doc_dir: Path) -> str:
    """build.toml's `engine`, else the `% !TEX program` magic comment, else pdflatex."""
    config = doc_dir / "build.toml"
    # ponytail: build.toml needs tomllib (3.11+); CI runs 3.12, so no tomli fallback.
    if config.exists() and sys.version_info >= (3, 11):
        import tomllib
        engine = tomllib.loads(config.read_text(encoding="utf-8")).get("engine")
        if engine:
            return engine
    head = (doc_dir / "main.tex").read_text(encoding="utf-8", errors="replace")
    match = MAGIC_ENGINE.search("\n".join(head.splitlines()[:20]))
    return match.group(1).lower() if match else "pdflatex"


def diff_document(name: str, base: str, work: Path) -> Path | None:
    """
    latexdiff of the document at `base` against the working tree.

    Returns the diff PDF, or None if the document did not exist at `base`.
    """
    old = work / "old"
    prefix = f"files/{name}/"
    tracked = git_paths("ls-tree", "-r", "-z", "--name-only", base, "--", f"files/{name}")
    for path in tracked:
        if not path.startswith(prefix):
            continue
        target = old / PurePosixPath(path).relative_to(prefix)
        target.parent.mkdir(parents=True, exist_ok=True)
        blob = subprocess.run(
            ["git", "show", f"{base}:{path}"],
            cwd=ROOT_DIR, check=True, capture_output=True,
        )
        target.write_bytes(blob.stdout)
    if not (old / "main.tex").exists():
        return None

    # Compile in a copy of the working tree, so latexmk's files stay out of the repo.
    new = work / "new"
    shutil.copytree(SOURCE_DIR / name, new)
    engine = engine_of(new)
    if engine not in ENGINE_FLAGS:
        raise ValueError(f"unknown engine {engine!r}")

    diff = subprocess.run(
        ["latexdiff", "--flatten", str(old / "main.tex"), str(new / "main.tex")],
        check=False, capture_output=True,
    )
    if diff.returncode:
        message = diff.stderr.decode("utf-8", "replace").strip()
        raise RuntimeError(message or "latexdiff failed")
    (new / "diff.tex").write_bytes(diff.stdout)

    run = subprocess.run(
        ["latexmk", ENGINE_FLAGS[engine], "-interaction=nonstopmode",
         "-halt-on-error", "-file-line-error", "diff.tex"],
        cwd=new, check=False, capture_output=True, text=True, errors="replace",
    )
    if run.returncode:
        # TeX may print "./diff.tex:12: ...", so allow a path prefix.
        found = re.search(r"^\S*diff\.tex:\d+: .*$", run.stdout, re.MULTILINE)
        raise RuntimeError(found.group(0) if found else "latexmk failed")
    return new / "diff.pdf"


def cmd_diff(args: argparse.Namespace) -> None:
    out = Path(args.out).resolve()
    out.mkdir(parents=True, exist_ok=True)
    changed = git_paths("diff", "--name-only", "-z", args.base, "--", "files")
    results = {}
    with tempfile.TemporaryDirectory() as tmp:
        for doc in load_documents():
            name = doc["name"]
            if not any(path.startswith(f"files/{name}/") for path in changed):
                continue
            if not doc["ok"]:
                results[name] = {"ok": False, "error": "the new version does not build"}
                continue
            try:
                pdf = diff_document(name, args.base, Path(tempfile.mkdtemp(dir=tmp)))
                if pdf is None:
                    print(f"{name}: new document, nothing to compare.")
                    continue
                target = out / f"{name}.pdf"
                target.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy(pdf, target)
                results[name] = {"ok": True}
                print(f"{name}: compiled {target.name}.")
            except Exception as error:  # noqa: BLE001 - one document must not stop the others.
                results[name] = {"ok": False, "error": str(error)}
                print(f"{name}: failed: {error}")
    report = json.dumps({"documents": results}, indent=2)
    (out / DIFF_REPORT).write_text(report, encoding="utf-8")


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

def main() -> int:
    parser = argparse.ArgumentParser(description="CI reports from out/build-report.json.")
    commands = parser.add_subparsers(dest="command", required=True)

    commands.add_parser("summary", help="markdown table for $GITHUB_STEP_SUMMARY")
    commands.add_parser("lint", help="chktex and word counts into out/lint-report.json")

    diff = commands.add_parser("diff", help="latexdiff of changed documents")
    diff.add_argument("--base", required=True, help="git ref to compare against")
    diff.add_argument("--out", required=True, help="directory for the diff PDFs")

    comment = commands.add_parser("pr-comment", help="create or update the PR comment")
    comment.add_argument("--diff-dir", type=Path, help="directory written by `diff`")
    comment.add_argument("--print", dest="print_only", action="store_true",
                         help="print the comment instead of posting it")

    args = parser.parse_args()
    {
        "summary": cmd_summary,
        "lint": cmd_lint,
        "diff": cmd_diff,
        "pr-comment": cmd_pr_comment,
    }[args.command](args)
    return 0


if __name__ == "__main__":
    sys.exit(main())
