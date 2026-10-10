"""
Named share links for the editor (serve.py). Stdlib only, Python 3.9.

The owner mints a link per person ("Alice", edit; "Reviewer 2", view). Each link has its own token, and the name
comes from the link, not from the browser: comments, suggestions, history authors and presence show it. Records
live in the owner's config folder (0600 file in a 0700 folder), never in a document directory, and stay valid for
every later share of the same document until the owner revokes them. Tokens are compared in constant time.

File format: {"version": 1, "links": [{"id", "token", "name", "role", "doc", "created"}]}.
"""

from __future__ import annotations

import hmac
import json
import os
import re
import secrets
import time
from pathlib import Path

FILE = Path(os.environ.get("XDG_CONFIG_HOME") or Path.home() / ".config") / "latex-pipeline" / "share-links.json"
VERSION = 1
MAX_LINKS = 200
MAX_NAME = 40
ROLES = ("edit", "view")


class LinkError(Exception):
    pass


def clean_name(value) -> str:
    """A display name: 1-40 characters, no control characters, inner whitespace collapsed."""
    name = re.sub(r"\s+", " ", value).strip() if isinstance(value, str) else ""
    if not name or len(name) > MAX_NAME or re.search(r"[\x00-\x1f\x7f]", name):
        raise LinkError(f"Give the link a name (up to {MAX_NAME} characters).")
    return name


def _valid(link) -> bool:
    if not isinstance(link, dict) or link.get("role") not in ROLES or not isinstance(link.get("created"), (int, float)):
        return False
    return all(isinstance(link.get(k), str) and link[k] for k in ("id", "token", "name", "doc"))


def load(path: Path | None = None) -> list:
    """Every link record. A missing file is no links; a damaged one too (it is rewritten on the next change)."""
    try:
        data = json.loads((path or FILE).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return []
    if isinstance(data, list):  # never written by a release, but cheap to accept: a bare list of records
        data = {"version": VERSION, "links": data}
    links = data.get("links") if isinstance(data, dict) else None
    return [link for link in links if _valid(link)] if isinstance(links, list) else []


def save(links: list, path: Path | None = None) -> None:
    """Write the records readable by this user only (0600 in a 0700 folder), atomically."""
    path = path or FILE
    path.parent.mkdir(parents=True, exist_ok=True)
    if os.name != "nt":
        os.chmod(path.parent, 0o700)
    tmp = path.with_name(path.name + ".tmp")
    fd = os.open(str(tmp), os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as out:
        json.dump({"version": VERSION, "links": links}, out)
    if os.name != "nt":
        os.chmod(tmp, 0o600)
    os.replace(tmp, path)


def create(links: list, name, role, doc: str, now: float | None = None) -> dict:
    """A new link record (appended to links; the caller saves)."""
    if role not in ROLES:
        raise LinkError("A link is for editing or for viewing.")
    if len(links) >= MAX_LINKS:
        raise LinkError(f"There are {MAX_LINKS} named links; revoke some first.")
    link = {"id": secrets.token_hex(6), "token": secrets.token_urlsafe(32), "name": clean_name(name), "role": role,
            "doc": doc, "created": time.time() if now is None else now}
    links.append(link)
    return link


def find(links: list, token: str | None, doc: str | None) -> dict | None:
    """The link of this document whose token matches. Compares every token, so timing says nothing."""
    probe = (token or "").encode("utf-8", "replace")
    found = None
    for link in links:
        if hmac.compare_digest(probe, link["token"].encode()) and token and link["doc"] == doc:
            found = link
    return found


def user_of(link: dict) -> str:
    """The "id;name" identity serve.py uses for accounts and named links alike."""
    return f"link-{link['id']};{link['name']}"
