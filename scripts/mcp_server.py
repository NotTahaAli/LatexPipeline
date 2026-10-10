#!/usr/bin/env python3
"""
Local MCP server for this repository's LaTeX documents, over stdio (JSON-RPC 2.0, one message per line).

    python scripts/mcp_server.py [DOC ...] [--source DIR] [--read-only] [--sandbox]

Any MCP client that starts local servers (Claude Desktop, Claude Code, Codex CLI, Cursor, VS Code, Windsurf,
Gemini CLI) can use it with your own subscription; see README, "Use with AI clients (MCP)". The tools are those of
mcp_tools.py, run as the owner: like serve.py without sharing, builds use your normal local settings (build.toml,
latexmkrc), so only point it at documents you trust, or pass --sandbox. Changes go through the editor's save path
and into the version history as "<client> (MCP)"; a serve.py editor that has the file open sees the change on disk.

Stdlib only, Python 3.9+, Windows, macOS and Linux.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path


class Local:
    """The backend of mcp_tools.handle for this repository: every served document, as the owner."""

    def __init__(self, serve, read_only: bool) -> None:
        import mcp_tools
        self.serve, self.tools_mod, self.read_only = serve, mcp_tools, read_only
        self.client = "AI client"

    @property
    def author(self) -> str:
        return f"{self.client} (MCP)"

    def tools(self) -> list[dict]:
        return self.tools_mod.tools_for(() if self.read_only else ("write", "review"))

    def challenge(self, scope: str):
        return None

    def note_client(self, message) -> None:
        """Remember the client's self-reported name (initialize, or 2026-07-28 per-request _meta) for authorship."""
        params = message.get("params") if isinstance(message, dict) else None
        if not isinstance(params, dict):
            return
        meta = params.get("_meta") if isinstance(params.get("_meta"), dict) else {}
        info = params.get("clientInfo") if message.get("method") == "initialize" else \
            meta.get("io.modelcontextprotocol/clientInfo")
        if isinstance(info, dict):
            self.client = self.tools_mod.client_label(info)

    def call(self, name: str, args: dict) -> dict:
        t, s = self.tools_mod, self.serve
        access = ["read"] if self.read_only else ["read", "write", "review"]
        if name == "list_projects":
            return t.result({"projects": [
                {"project": doc, "name": doc, "access": access, "pdf": s.build.output_path_for(main).is_file()}
                for doc, main in sorted(s.DOCS.items())]})
        if name == "search":
            budget, found = [t.SEARCH_BYTES], []
            for doc, main in sorted(s.DOCS.items()):
                found += [(score, doc, rel) for score, rel in t.search_tree(main.parent, args["query"], budget)]
            found.sort(key=lambda f: (-f[0], f[1], f[2]))
            return t.result({"results": [{"id": f"{doc}::{rel}", "title": f"{doc}: {rel}",
                                          "url": (s.DOCS[doc].parent / rel).resolve().as_uri()}
                                         for _, doc, rel in found[:20]]})
        if name == "fetch":
            doc, _, rel = args["id"].partition("::")
            if doc not in s.DOCS:
                raise t.ToolError("No such file (ids come from search).")
            text, cut = t.read_for_fetch(s.DOCS[doc].parent, rel)
            return t.result({"id": args["id"], "title": f"{doc}: {rel}", "text": text,
                             "url": (s.DOCS[doc].parent / rel).resolve().as_uri(),
                             "metadata": {"project": doc, "path": rel, "truncated": cut}})
        doc = args.pop("project")
        if doc not in s.DOCS:
            raise t.ToolError(f"No project {doc!r}. list_projects shows the names.")
        return t.Doc(s, doc, "owner", None, self.author).run(name, args)


def serve_stdio(backend, lines, out) -> None:
    import mcp_tools
    for raw in lines:
        line = raw.strip()
        if not line:
            continue
        try:
            message = json.loads(line)
        except ValueError:
            response = {"jsonrpc": "2.0", "id": None, "error": {"code": -32700, "message": "Parse error"}}
        else:
            backend.note_client(message)
            response = mcp_tools.handle(message, backend)[0]
        if response is not None:
            out.write(json.dumps(response, ensure_ascii=False) + "\n")
            out.flush()


def main() -> int:
    # The protocol owns the real stdout. Everything else (build.info, latexmk and its children) goes to stderr.
    protocol = os.fdopen(os.dup(1), "w", encoding="utf-8", newline="\n")
    os.dup2(2, 1)
    sys.stdout = sys.stderr
    parser = argparse.ArgumentParser(description="MCP server (stdio) for the LaTeX documents in this repository.")
    parser.add_argument("docs", nargs="*", metavar="DOC", help="Documents to offer (name or glob). Default: all.")
    parser.add_argument("--source", metavar="DIR", help="Directory holding the documents (default: files/).")
    parser.add_argument("--read-only", action="store_true", help="Offer only the tools that read.")
    parser.add_argument("--sandbox", action="store_true", help="Run LaTeX under bubblewrap (LATEX_SANDBOX=bwrap).")
    args = parser.parse_args()

    import build
    import serve
    if args.source:
        source = Path(args.source) if Path(args.source).is_absolute() else build.ROOT_DIR / args.source
        if not source.is_dir():
            build.error(f"--source {args.source}: not a directory")
            return 2
        build.SOURCE_DIR = source.resolve()
    if args.sandbox:
        os.environ[build.sandbox.VARIABLE] = "bwrap"
        try:
            build.sandbox.check()
        except build.sandbox.SandboxError as exc:
            build.error(str(exc))
            return 2
    documents = build.find_documents()
    unknown = build.unknown_patterns(documents, args.docs)
    if unknown:
        build.error(f"No document matches: {', '.join(unknown)}")
        return 2
    for main_tex in build.select_documents(documents, args.docs):
        name = build.doc_name(main_tex)
        serve.DOCS[name] = main_tex
        serve.STATE[name] = serve.fresh_state(name, main_tex)
    serve.SETTINGS["latexmk"] = build.find_latexmk() or "latexmk"
    print(f"MCP server ready: {len(serve.DOCS)} document(s){', read-only' if args.read_only else ''}.", file=sys.stderr)
    try:
        serve_stdio(Local(serve, args.read_only), sys.stdin.buffer, protocol)
    except KeyboardInterrupt:
        pass
    return 0


if __name__ == "__main__":
    sys.path.insert(0, str(Path(__file__).resolve().parent))
    sys.exit(main())
