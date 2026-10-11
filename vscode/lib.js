// Pure helpers of the extension: no `vscode` import, so tests/vscode_check.mjs runs them in plain Node.
"use strict";

/**
 * What the person pasted -> where the editor is and how to sign in.
 *   http://localhost:8000/               a local serve.py (owner, no token)
 *   https://x.trycloudflare.com/?token=  a share link of a local serve.py (the token becomes a cookie)
 *   https://latex.example.org/p/<id>/    a hosted project (needs a personal API token, made on the Account page)
 */
function parseTarget(text) {
  let url;
  try { url = new URL(String(text || "").trim()); } catch { throw new Error("Paste the editor's address, such as http://localhost:8000/."); }
  if (url.protocol !== "http:" && url.protocol !== "https:") throw new Error("The address must start with http:// or https://.");
  if (url.username || url.password) throw new Error("Leave the name and password out of the address.");
  if (url.protocol === "http:" && !["localhost", "127.0.0.1", "[::1]"].includes(url.hostname)) {
    throw new Error("Use https for anything but this computer: tokens would cross the network in clear text.");
  }
  const hosted = url.pathname.match(/^\/p\/([0-9a-f]{16})(\/|$)/);
  const token = url.searchParams.get("token") || null;
  if (hosted) return { kind: "hosted", base: `${url.origin}/p/${hosted[1]}/`, project: hosted[1], token: null };
  return { kind: "local", base: `${url.origin}/`, project: null, token };
}

/** base + path + query; path segments are taken as they are, the query is encoded. */
function apiUrl(base, path, query) {
  const qs = query ? new URLSearchParams(Object.entries(query).filter(([, v]) => v !== undefined && v !== null)).toString() : "";
  return base + path.replace(/^\//, "") + (qs ? "?" + qs : "");
}

/** The cookie `name=value` from a Set-Cookie header (serve.py names it lp_<port>), or null. */
function cookieFrom(setCookie) {
  const first = String(setCookie || "").split(";")[0].trim();
  return /^lp_\d+=[A-Za-z0-9_-]+$/.test(first) ? first : null;
}

/** Request headers for a connection: hosted -> the API token, local share link -> its cookie. */
function authHeaders(target, secret) {
  if (!secret) return {};
  return target.kind === "hosted" ? { Authorization: `Bearer ${secret}` } : { Cookie: secret };
}

/** serve.py's file version "<mtime ns hex>-<size hex>" -> {mtime ms, size}. */
function parseVersion(version) {
  const m = /^([0-9a-f]+)-([0-9a-f]+)$/.exec(String(version || ""));
  return m ? { mtime: Math.floor(parseInt(m[1], 16) / 1e6), size: parseInt(m[2], 16) } : { mtime: 0, size: 0 };
}

/** The file list (GET api/files) as a tree: path -> {type: "file"|"dir", ...}; "" is the root folder. */
function buildTree(listing) {
  const tree = new Map([["", { type: "dir", children: new Set() }]]);
  const addDir = (dir) => {
    if (tree.has(dir)) return;
    tree.set(dir, { type: "dir", children: new Set() });
    const parent = dir.includes("/") ? dir.slice(0, dir.lastIndexOf("/")) : "";
    addDir(parent);
    tree.get(parent).children.add(dir.slice(dir.lastIndexOf("/") + 1));
  };
  for (const d of listing.dirs || []) addDir(d);
  for (const f of listing.files || []) {
    const parent = f.path.includes("/") ? f.path.slice(0, f.path.lastIndexOf("/")) : "";
    addDir(parent);
    tree.get(parent).children.add(f.path.slice(f.path.lastIndexOf("/") + 1));
    tree.set(f.path, { type: "file", kind: f.kind, version: f.version, ...parseVersion(f.version), size: f.size });
  }
  return tree;
}

/** A URI path (/a/b.tex) -> the document-relative path the API takes (a/b.tex); rejects dot segments. */
function relPath(uriPath) {
  const rel = String(uriPath || "").replace(/^\/+/, "").replace(/\/+$/, "");
  if (rel.split("/").some((p) => p === "." || p === "..")) throw new Error("Bad path.");
  return rel;
}

/** Text the server sent (always \n) back with the file's own line endings, and the reverse for a save. */
function withEol(text, eol) { return eol === "\r\n" ? text.replace(/\n/g, "\r\n") : text; }
function fromEditor(text) { return { text: text.replace(/\r\n/g, "\n"), eol: text.includes("\r\n") ? "\r\n" : "\n" }; }

/**
 * Problems for the Problems panel: the build's errors (state.docs[].errors) and the log's warnings
 * (GET api/warnings) -> Map(path -> [{line (0-based), message, severity: "error"|"warning"|"info"}]).
 */
function problems(errors, warnings) {
  const out = new Map();
  const add = (file, line, message, severity) => {
    if (!file) return;
    if (!out.has(file)) out.set(file, []);
    out.get(file).push({ line: Math.max(0, (Number(line) || 1) - 1), message: String(message || ""), severity });
  };
  for (const e of errors || []) add(e.file, e.line, e.message, "error");
  for (const w of warnings || []) add(w.file, w.line, w.message, w.level === "info" ? "info" : "warning");
  return out;
}

/** The latest "state" of one document from a batch of bus events (api/poll), or null. */
function latestState(events, doc) {
  let found = null;
  for (const e of events || []) {
    if (e.type === "state") found = (e.data?.docs || []).find((d) => d.name === doc) || found;
  }
  return found;
}

/** Paths changed or removed in a batch of bus events, for one document. */
function fsChanges(events, doc) {
  const changed = new Set(), removed = new Set();
  for (const e of events || []) {
    if (e.type !== "fs" || e.data?.doc !== doc) continue;
    for (const p of e.data.changed || []) { changed.add(p.replace(/\/$/, "")); removed.delete(p); }
    for (const p of e.data.removed || []) { removed.add(p); changed.delete(p); }
  }
  return { changed: [...changed], removed: [...removed] };
}

module.exports = { parseTarget, apiUrl, cookieFrom, authHeaders, parseVersion, buildTree, relPath, withEol, fromEditor, problems, latestState, fsChanges };
