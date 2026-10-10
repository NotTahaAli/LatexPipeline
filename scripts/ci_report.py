#!/usr/bin/env python3
"""
CI reports for build-pdf.yml, built from out/build-report.json (written by build.py).

Subcommands:

- summary     Markdown table of this run's documents, for $GITHUB_STEP_SUMMARY.
- lint        Structural checks (labels, references, figures, bib), overfull boxes from
              the cached log, chktex and word counts into out/lint-report.json.
              Findings are GitHub annotations in Actions; --strict fails on them.
- diff        latexdiff of every document changed since --base, PDFs into --out
              (keep --out outside out/, so the PDFs are not published to the release).
- pr-comment  Create or update the sticky pull request comment (needs gh and GH_TOKEN).

Stdlib only. A missing chktex, texcount or latexdiff leaves its values blank
instead of failing the run.
"""

from __future__ import annotations

import argparse
import datetime
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path, PurePosixPath
from typing import NamedTuple

from build import ENGINES, LATEXMK_ARGS, OUT_DIR, ROOT_DIR, SOURCE_DIR, read_settings, size_text
from hints import overfull_boxes

REPORT_PATH = OUT_DIR / "build-report.json"
LINT_PATH = OUT_DIR / "lint-report.json"
DIFF_REPORT = "diff-report.json"
MARKER = "<!-- latex-pipeline -->"
BOT_LOGIN = "github-actions[bot]"
TOP_N = 5  # chktex warnings listed per document.
CHKTEX_WARNING = re.compile(r"^Warning \d+ in (.+) line (\d+): (.*)$", re.MULTILINE)

# lint: findings are compared with files/<doc>/.lint-baseline by fingerprint.
BASELINE_NAME = ".lint-baseline"
DEFAULT_OVERFULL_PT = 10.0
LINT_ANNOTATION_LIMIT = 50  # GitHub annotations per document.
IMAGE_SUFFIXES = {".pdf", ".png", ".jpg", ".jpeg", ".eps", ".svg", ".gif", ".tif", ".tiff", ".bmp"}
COMMENT = re.compile(r"(?<!\\)((?:\\\\)*)%[^\n]*")
INCLUDE = re.compile(r"\\(?:\w*input|include)\*?((?:\{[^{}]*\})+)")
ARGUMENTS = re.compile(
    r"\\(?:\w*input|include|includegraphics|includesvg|includepdf|lstinputlisting|verbatiminput"
    r"|addbibresource|bibliography|usepackage|documentclass)\*?(?:\[[^\]]*\])?((?:\{[^{}]*\})+)"
)
LABEL = re.compile(r"\\label\{([^{}]+)\}|\blabel=\{([^{}]+)\}")  # also listings' label={...} option
REF = re.compile(r"\\(?:ref|eqref|cref|Cref|autoref|Autoref|pageref|nameref|vref|labelcref)\*?\{([^{}]+)\}")
CREF_RANGE = re.compile(r"\\[cC]refrange\*?\{([^{}]+)\}\{([^{}]+)\}")
CITE = re.compile(r"\\\w*cite\w*\*?(?:\[[^\]]*\]){0,2}\{([^{}]+)\}")
NOCITE_ALL = re.compile(r"\\nocite\{\s*\*\s*\}")
BIB_ENTRY = re.compile(r"^[ \t]*@(\w+)[ \t]*[({][ \t]*([^,\s]+)[ \t]*,", re.MULTILINE)
# Fields an entry type needs; "a|b" accepts either (biblatex names included).
BIB_REQUIRED = {
    "article": ("author", "title", "journal|journaltitle", "year|date"),
    "book": ("author|editor", "title", "publisher", "year|date"),
    "inproceedings": ("author", "title", "booktitle", "year|date"),
}
BIB_YEAR_FIRST = 1450
BIB_ACRONYM = re.compile(r"\b(?=\w*[A-Z]\w*[A-Z])\w+\b")  # BERT, LaTeX, iPhone: two capitals in a word
BIB_LINK = re.compile(r"[{,\s](?:doi|url)\s*=", re.IGNORECASE)  # inline or on its own line


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
        return doc.get("error")  # A crashed build has no LaTeX errors.
    error = errors[0]
    where = error.get("file", "")
    if error.get("line") is not None:
        where = f"{where}:{error['line']}"
    return f"{where}: {error.get('message', '')}"


def table(documents: list[dict], lint: dict) -> list[str]:
    columns = ["Document", "Status", "Pages", "Size", "Words", "Time", "Warnings", "chktex", "Lint", "First error"]
    rows = ["| " + " | ".join(columns) + " |", "|" + "---|" * len(columns)]
    for doc in documents:
        info = lint.get(doc["name"], {})
        seconds = doc.get("seconds")
        cells = [
            doc["name"],
            "ok" if doc["ok"] else "**failed**",
            doc.get("pages"),
            size_text(doc["size"]) if doc.get("size") else None,
            info.get("words"),
            None if seconds is None else f"{seconds:.1f} s",
            doc.get("warnings"),
            info.get("chktex"),
            info.get("lint"),
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
    # Only our own comment: anyone can paste the marker into a comment.
    ids = gh(
        "api", "--paginate", f"repos/{repo}/issues/{number}/comments",
        "--jq", f'.[] | select(.user.login == "{BOT_LOGIN}" and (.body | contains("{MARKER}"))) | .id',
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


class Finding(NamedTuple):
    kind: str
    path: str  # relative to the document directory
    line: int | None
    message: str
    subject: str  # what the finding is about, stable under edits (keys, file names)
    level: str = "warning"  # or "info", shown as a notice

    @property
    def fingerprint(self) -> str:
        return ":".join(part for part in (self.kind, self.path, self.subject) if part)

    def as_dict(self) -> dict:
        return {"kind": self.kind, "path": self.path, "line": self.line,
                "message": self.message, "level": self.level, "fingerprint": self.fingerprint}


def line_of(text: str, position: int) -> int:
    return text.count("\n", 0, position) + 1


def brace_args(text: str) -> list[str]:
    """The arguments of {a}{b}{c}, each stripped; comma lists are split by the caller."""
    return [arg.strip() for arg in re.findall(r"\{([^{}]*)\}", text)]


def is_macro_key(key: str) -> bool:
    """Labels and keys built with macros (\\label{fig:\\thechapter}) cannot be checked here."""
    return "\\" in key or "#" in key


def strip_comments(text: str) -> str:
    """Drop % comments but keep the newlines, so line numbers match the file. \\% and \\\\% are handled."""
    return COMMENT.sub(r"\1", text)


def reachable_sources(doc_dir: Path) -> dict[str, str]:
    """
    Comment-stripped text of main.tex and every file it pulls in, keyed by path relative to doc_dir.

    Follows \\input, \\include and any macro ending in "input" (\\fypinput{name}{file}), which
    the document uses for its chapters. The doc dir's .sty and .cls files are added as label sources.
    """
    sources: dict[str, str] = {}
    queue = ["main.tex"]
    while queue:
        rel = queue.pop(0)
        path = doc_dir / rel
        if rel in sources or not path.is_file():
            continue
        text = strip_comments(path.read_text(encoding="utf-8", errors="replace"))
        sources[rel] = text
        for group in INCLUDE.finditer(text):
            for arg in brace_args(group.group(1)):
                if arg and not is_macro_key(arg):
                    target = arg if arg.endswith(".tex") else f"{arg}.tex"
                    queue.append(PurePosixPath(target).as_posix())
    for path in visible_files(doc_dir):
        if path.suffix in (".sty", ".cls"):
            rel = path.relative_to(doc_dir).as_posix()
            sources[rel] = strip_comments(path.read_text(encoding="utf-8", errors="replace"))
    return sources


def visible_files(doc_dir: Path) -> list[Path]:
    """Files under doc_dir, skipping hidden ones such as .lint-baseline."""
    return sorted(
        path for path in doc_dir.rglob("*")
        if path.is_file() and not any(part.startswith(".") for part in path.relative_to(doc_dir).parts)
    )


def label_findings(sources: dict[str, str]) -> list[Finding]:
    defined: dict[str, tuple[str, int]] = {}
    refs: list[tuple[str, str, int]] = []
    for path, text in sources.items():
        for match in LABEL.finditer(text):
            key = (match.group(1) or match.group(2)).strip()
            if not is_macro_key(key):
                defined.setdefault(key, (path, line_of(text, match.start())))
        for match in REF.finditer(text):
            refs += [(key.strip(), path, line_of(text, match.start()))
                     for key in match.group(1).split(",")]
        for match in CREF_RANGE.finditer(text):
            refs += [(match.group(1).strip(), path, line_of(text, match.start())),
                     (match.group(2).strip(), path, line_of(text, match.start()))]

    referenced = {key for key, _path, _line in refs}
    findings = [
        Finding("unused-label", path, line, f"label '{key}' is never referenced", key)
        for key, (path, line) in defined.items() if key not in referenced
    ]
    findings += [
        Finding("undefined-ref", path, line, f"reference to undefined label '{key}'", key)
        for key, path, line in refs
        if key and not is_macro_key(key) and key not in defined
    ]
    return findings


def figure_findings(doc_dir: Path, sources: dict[str, str]) -> list[Finding]:
    used = set()
    for text in sources.values():
        for group in ARGUMENTS.finditer(text):
            for arg in brace_args(group.group(1)):
                used.update(part.strip().removeprefix("./") for part in arg.split(","))
    findings = []
    for path in visible_files(doc_dir):
        if path.suffix.lower() not in IMAGE_SUFFIXES:
            continue
        rel = path.relative_to(doc_dir).as_posix()
        stem = PurePosixPath(rel).with_suffix("").as_posix()
        # Matches with and without extension, and by a trailing part of the path (\graphicspath).
        if not any(u in (rel, stem) or rel.endswith(f"/{u}") or stem.endswith(f"/{u}") for u in used):
            findings.append(Finding("unused-figure", rel, None,
                                    f"figure '{rel}' is never included", ""))
    return findings


def bib_fields(body: str) -> dict[str, str]:
    """name -> value of the top-level fields of one entry body (the text after "@type{key,")."""
    fields: dict[str, str] = {}
    flat, depth, quoted = [], 0, False
    for char in body:  # blank everything nested in braces or quotes so "title = {a = b}" is one field
        if char == "{":
            depth += 1
        elif char == "}":
            depth -= 1
            if depth < 0:
                break
        elif char == '"' and depth == 0:
            quoted = not quoted
        flat.append(char if depth == 0 and not quoted and char not in '{}"' else " ")
    flat_text = "".join(flat)
    for match in re.finditer(r"(\w[\w-]*)\s*=", flat_text):
        rest = body[match.end():]
        value = re.match(r"\s*(\{(?:[^{}]|\{[^{}]*\})*\}|\"[^\"]*\"|[^,}\s]+)", rest)
        fields[match.group(1).lower()] = value.group(1).strip("{}\" \n") if value else ""
    return fields


def entry_findings(rel: str, line: int, kind: str, key: str, body: str, dois: dict[str, str]) -> list[Finding]:
    fields = bib_fields(body)
    found: list[Finding] = []
    if "crossref" not in fields:  # inherits its fields
        missing = [need.split("|")[0] for need in BIB_REQUIRED.get(kind, ()) if not any(
            fields.get(name) for name in need.split("|"))]
        if missing:
            found.append(Finding("missing-bib-field", rel, line,
                                 f"@{kind} '{key}' lacks {', '.join(missing)}", key))
    year = fields.get("year", "")
    if year and not (re.fullmatch(r"\d{4}", year) and BIB_YEAR_FIRST <= int(year) <= datetime.date.today().year + 1):
        found.append(Finding("bib-year", rel, line, f"'{key}' has a suspicious year: {year!r}", key))
    doi = re.sub(r"^(?:https?://(?:dx\.)?doi\.org/|doi:)", "", fields.get("doi", ""), flags=re.IGNORECASE).lower()
    if doi:
        if doi in dois:
            found.append(Finding("dup-doi", rel, line, f"'{key}' has the same DOI as '{dois[doi]}'", key))
        else:
            dois[doi] = key
    title = fields.get("title", "")
    unprotected = re.sub(r"\{[^{}]*\}", "", title)  # text inside braces is protected
    if BIB_ACRONYM.search(unprotected):
        found.append(Finding("bib-title-case", rel, line,
                             f"'{key}': wrap capitals in braces so styles keep them, e.g. {{BERT}}",
                             key, level="info"))
    return found


def unused_bib_files(doc_dir: Path, sources: dict[str, str]) -> list[Finding]:
    """A .bib whose path is named nowhere in the sources (custom macros such as \\unitbib{...} count)."""
    text = "\n".join(sources.values())
    return [
        Finding("unused-bib-file", rel, None, f"{rel} is not named in any source file", rel)
        for rel in (path.relative_to(doc_dir).as_posix() for path in visible_files(doc_dir) if path.suffix == ".bib")
        if not re.search(rf"(?<![\w/.-]){re.escape(rel.removesuffix('.bib'))}(?:\.bib)?(?![\w/-])", text)
    ]


def bib_findings(doc_dir: Path, sources: dict[str, str]) -> list[Finding]:
    findings: list[Finding] = []
    entries: dict[str, tuple[str, int]] = {}  # first definition of each key
    dois: dict[str, str] = {}
    for path in visible_files(doc_dir):
        if path.suffix != ".bib":
            continue
        rel = path.relative_to(doc_dir).as_posix()
        text = path.read_text(encoding="utf-8", errors="replace")
        matches = list(BIB_ENTRY.finditer(text))
        for index, match in enumerate(matches):
            kind = match.group(1).lower()
            if kind in ("comment", "string", "preamble"):
                continue
            key = match.group(2)
            line = line_of(text, match.start())
            if key in entries:
                findings.append(Finding("dup-bib-key", rel, line,
                                        f"bib key '{key}' is also defined in {entries[key][0]}", key))
            else:
                entries[key] = (rel, line)
            end = matches[index + 1].start() if index + 1 < len(matches) else len(text)
            findings += entry_findings(rel, line, kind, key, text[match.end():end], dois)
            if kind == "article" and not BIB_LINK.search(text[match.start():end]):
                findings.append(Finding("missing-doi", rel, line,
                                        f"@article '{key}' has no doi or url", key, level="info"))

    findings += unused_bib_files(doc_dir, sources)
    if any(NOCITE_ALL.search(text) for text in sources.values()):
        return findings  # \nocite{*} cites every entry.
    cited = {key.strip() for text in sources.values()
             for group in CITE.finditer(text) for key in group.group(1).split(",")}
    findings += [
        Finding("uncited-bib", rel, line, f"bib entry '{key}' is never cited", key)
        for key, (rel, line) in entries.items() if key not in cited
    ]
    return findings


def overfull_findings(log_text: str, budget: float) -> list[Finding]:
    return [
        Finding("overfull", file or "main.tex", line,
                f"Overfull \\hbox {points:g}pt too wide (budget {budget:g}pt)", str(line))
        for file, line, points in overfull_boxes(log_text) if points > budget
    ]


def lint_document(doc_dir: Path, log_path: Path | None, budget: float) -> list[Finding]:
    sources = reachable_sources(doc_dir)
    findings = label_findings(sources) + figure_findings(doc_dir, sources) + bib_findings(doc_dir, sources)
    if log_path is not None and log_path.is_file():
        findings += overfull_findings(log_path.read_text(encoding="utf-8", errors="replace"), budget)
    return findings


def read_baseline(doc_dir: Path) -> set[str]:
    path = doc_dir / BASELINE_NAME
    if not path.is_file():
        return set()
    return {line.strip() for line in path.read_text(encoding="utf-8").splitlines() if line.strip()}


def write_baseline(doc_dir: Path, findings: list[Finding]) -> None:
    fingerprints = sorted({finding.fingerprint for finding in findings})
    (doc_dir / BASELINE_NAME).write_text("".join(f"{item}\n" for item in fingerprints), encoding="utf-8")


def escape_command(text: str, property_value: bool = False) -> str:
    """Escapes for a GitHub workflow command; property values also escape ':' and ','."""
    text = text.replace("%", "%25").replace("\r", "%0D").replace("\n", "%0A")
    if property_value:
        text = text.replace(":", "%3A").replace(",", "%2C")
    return text


def annotate(name: str, findings: list[Finding]) -> None:
    """Plain lines locally; GitHub annotations (capped per document) in Actions."""
    if os.environ.get("GITHUB_ACTIONS") != "true":
        for finding in findings:
            where = finding.path if finding.line is None else f"{finding.path}:{finding.line}"
            print(f"  {where}: {finding.kind}: {finding.message}")
        return
    for finding in findings[:LINT_ANNOTATION_LIMIT]:
        command = "notice" if finding.level == "info" else "warning"
        props = f"file={escape_command(f'files/{name}/{finding.path}', True)}"
        if finding.line is not None:
            props += f",line={finding.line}"
        print(f"::{command} {props}::{escape_command(finding.message)}")
    if len(findings) > LINT_ANNOTATION_LIMIT:
        print(f"{name}: {len(findings) - LINT_ANNOTATION_LIMIT} more findings, "
              f"not annotated (limit {LINT_ANNOTATION_LIMIT}).")


def lint_targets(names: list[str]) -> list[str]:
    """Documents from build-report.json, or every files/<name>/main.tex when there is no report."""
    names_found = [doc["name"] for doc in load_documents()]
    if not names_found:
        names_found = [main.parent.relative_to(SOURCE_DIR).as_posix()
                       for main in sorted(SOURCE_DIR.rglob("main.tex"))
                       if not any(part.startswith(".") for part in main.relative_to(SOURCE_DIR).parts)]
    if names:
        unknown = sorted(set(names) - set(names_found))
        if unknown:
            print(f"Not a document: {', '.join(unknown)}")
        names_found = [name for name in names_found if name in names]
    return names_found


def cmd_lint(args: argparse.Namespace) -> int:
    lint = {}
    total = 0
    for name in lint_targets(args.documents):
        doc_dir = SOURCE_DIR / name
        findings = lint_document(doc_dir, OUT_DIR / f"{name}.log", args.overfull_pt)
        suppressed = 0
        if args.update_baseline:
            write_baseline(doc_dir, findings)
            findings = []
        else:
            baseline = read_baseline(doc_dir)
            suppressed = sum(finding.fingerprint in baseline for finding in findings)
            findings = [finding for finding in findings if finding.fingerprint not in baseline]
        warnings = run_chktex(doc_dir) if shutil.which("chktex") else None
        lint[name] = {
            "words": count_words(doc_dir),
            "chktex": None if warnings is None else len(warnings),
            "chktex_top": (warnings or [])[:TOP_N],
            "lint": len(findings),
            "suppressed": suppressed,
            "findings": [finding.as_dict() for finding in findings],
        }
        total += len(findings)
        print(f"{name}: words={lint[name]['words']} chktex={lint[name]['chktex']} "
              f"lint={len(findings)} suppressed={lint[name]['suppressed']}")
        annotate(name, findings)
    OUT_DIR.mkdir(exist_ok=True)
    LINT_PATH.write_text(json.dumps({"documents": lint}, indent=2), encoding="utf-8")
    return 1 if args.strict and total else 0


# ---------------------------------------------------------------------------
# diff (latexdiff)
# ---------------------------------------------------------------------------

def git_paths(*args: str) -> list[str]:
    """Output of a git command run with -z, split on NUL (paths are not quoted)."""
    output = subprocess.run(
        ["git", *args], cwd=ROOT_DIR, check=True, capture_output=True,
    ).stdout
    return [path for path in output.decode("utf-8").split("\0") if path]


def owning_document(path: str, names: list[str]) -> str | None:
    """
    The document a changed file belongs to: the deepest one whose directory
    contains it. A nested child's files are the child's, not its parent's.
    """
    owners = [name for name in names if path.startswith(f"files/{name}/")]
    return max(owners, key=len, default=None)


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
    settings = read_settings(new / "main.tex")

    diff = subprocess.run(
        ["latexdiff", "--flatten", str(old / "main.tex"), str(new / "main.tex")],
        check=False, capture_output=True,
    )
    if diff.returncode:
        message = diff.stderr.decode("utf-8", "replace").strip()
        raise RuntimeError(message or "latexdiff failed")
    (new / "diff.tex").write_bytes(diff.stdout)

    run = subprocess.run(
        ["latexmk", *LATEXMK_ARGS, ENGINES[settings["engine"]],
         *(["-shell-escape"] if settings["shell_escape"] else []),
         *settings["latexmk_args"], "diff.tex"],
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
    documents = load_documents()
    names = [doc["name"] for doc in documents]
    changed_names = {owning_document(path, names) for path in changed}
    results = {}
    with tempfile.TemporaryDirectory() as tmp:
        for doc in documents:
            name = doc["name"]
            if name not in changed_names:
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
    lint = commands.add_parser("lint", help="structural lint, chktex and word counts into out/lint-report.json")
    lint.add_argument("documents", nargs="*", help="documents to lint (default: all)")
    lint.add_argument("--strict", action="store_true", help="exit 1 when there are findings")
    lint.add_argument("--update-baseline", action="store_true",
                      help="write files/<doc>/.lint-baseline from the current findings")
    lint.add_argument("--overfull-pt", type=float, default=DEFAULT_OVERFULL_PT,
                      help="report overfull boxes wider than this many points (default 10)")

    diff = commands.add_parser("diff", help="latexdiff of changed documents")
    diff.add_argument("--base", required=True, help="git ref to compare against")
    diff.add_argument("--out", required=True, help="directory for the diff PDFs")

    comment = commands.add_parser("pr-comment", help="create or update the PR comment")
    comment.add_argument("--diff-dir", type=Path, help="directory written by `diff`")
    comment.add_argument("--print", dest="print_only", action="store_true",
                         help="print the comment instead of posting it")

    args = parser.parse_args()
    handlers = {
        "summary": cmd_summary,
        "lint": cmd_lint,
        "diff": cmd_diff,
        "pr-comment": cmd_pr_comment,
    }
    return handlers[args.command](args) or 0


if __name__ == "__main__":
    sys.exit(main())
