// LaTeX Pipeline for VS Code: edit a document served by scripts/serve.py (locally or through a share link) or a
// hosted project (scripts/host.py) as a folder in VS Code. Files go through the editor's HTTP API, so the server's
// roles apply: a view link cannot save, build-config files stay owner-only. A save is an ordinary save with the
// version the file was read at; if the file moved on (someone else saved, or a co-editing room), the save is refused
// and VS Code says so: reopen the file to get the new text. Build errors and warnings land in the Problems panel;
// the PDF opens in a webview (pdf.js from the same pinned CDN URL the browser editor uses).
"use strict";

const vscode = require("vscode");
const crypto = require("crypto");
const lib = require("./lib");

const SCHEME = "latexpipeline";
const PDFJS = "https://cdnjs.cloudflare.com/ajax/libs/pdf.js/4.4.168/pdf.min.mjs";
const PDFJS_WORKER = "https://cdnjs.cloudflare.com/ajax/libs/pdf.js/4.4.168/pdf.worker.min.mjs";
const STATE_KEY = "latexPipeline.target";

class ApiError extends Error {
  constructor(status, message) { super(message); this.status = status; }
}

/** One editor server and one document on it. */
class Connection {
  constructor(target, secret, doc) {
    Object.assign(this, { target, secret, doc });
    this.cid = "vscode-" + crypto.randomBytes(8).toString("hex");
  }

  async request(method, path, { query, json, raw } = {}) {
    const headers = { ...lib.authHeaders(this.target, this.secret) };
    let body;
    if (json !== undefined) { headers["Content-Type"] = "application/json"; body = JSON.stringify(json); }
    const res = await fetch(lib.apiUrl(this.target.base, path, { doc: this.doc, ...(query || {}) }),
      { method, headers, body, redirect: "manual" });
    if (raw && res.ok) return Buffer.from(await res.arrayBuffer());
    const text = await res.text();
    let data = null;
    try { data = JSON.parse(text); } catch { /* not JSON */ }
    if (!res.ok) throw new ApiError(res.status, (data && data.error) || text.slice(0, 200) || res.statusText);
    return data;
  }
}

/** Sign in once: a share link's token becomes the server's cookie (it names it), a hosted project takes an API token. */
async function signIn(target) {
  if (target.kind === "hosted") {
    const token = await vscode.window.showInputBox({
      prompt: "Personal API token for this project (Account page > API tokens on the hosted server)", password: true,
      ignoreFocusOut: true, validateInput: (v) => (/^lpat_[A-Za-z0-9_-]{20,}$/.test(v.trim()) ? null : "A token starts with lpat_"),
    });
    if (!token) return null;
    return token.trim();
  }
  if (!target.token) return null;  // the local owner: no sign-in
  const res = await fetch(lib.apiUrl(target.base, "", { token: target.token }), { redirect: "manual" });
  const cookie = lib.cookieFrom(res.headers.get("set-cookie"));
  if (!cookie) throw new Error("The share link was not accepted (revoked, or sharing stopped).");
  return cookie;
}

let conn = null;
let poller = null;
const changes = new vscode.EventEmitter();
const diagnostics = vscode.languages.createDiagnosticCollection("latex-pipeline");
const status = vscode.window.createStatusBarItem(vscode.StatusBarAlignment.Left, 50);
let tree = new Map();
let pdfPanel = null;

const uriFor = (rel) => vscode.Uri.from({ scheme: SCHEME, authority: "project", path: "/" + rel });

async function refreshTree() {
  tree = lib.buildTree(await conn.request("GET", "api/files"));
  return tree;
}

function needConn() {
  if (!conn) throw vscode.FileSystemError.Unavailable("Not connected: run \"LaTeX Pipeline: Connect\".");
  return conn;
}

function fsError(err, uri) {
  if (err instanceof ApiError) {
    if (err.status === 404) return vscode.FileSystemError.FileNotFound(uri);
    if (err.status === 401 || err.status === 403) return vscode.FileSystemError.NoPermissions(err.message);
    if (err.status === 409) return vscode.FileSystemError.Unavailable(`${err.message} Reopen the file to get the current text, then save again.`);
  }
  return vscode.FileSystemError.Unavailable(err.message || String(err));
}

/** The document folder as a VS Code file system. */
const provider = {
  onDidChangeFile: changes.event,
  versions: new Map(),  // path -> {version, eol} the open text was read at: the base of the next save

  watch() { return new vscode.Disposable(() => {}); },  // the bus poll reports every change (startPoll)

  async stat(uri) {
    needConn();
    const rel = lib.relPath(uri.path);
    if (!tree.has(rel)) await refreshTree().catch(() => {});
    const node = tree.get(rel);
    if (!node) throw vscode.FileSystemError.FileNotFound(uri);
    if (node.type === "dir") return { type: vscode.FileType.Directory, ctime: 0, mtime: 0, size: 0 };
    return { type: vscode.FileType.File, ctime: node.mtime, mtime: node.mtime, size: node.size,
      ...(conn.readOnly ? { permissions: vscode.FilePermission.Readonly } : {}) };
  },

  async readDirectory(uri) {
    needConn();
    const rel = lib.relPath(uri.path);
    await refreshTree().catch((e) => { throw fsError(e, uri); });
    const node = tree.get(rel);
    if (!node || node.type !== "dir") throw vscode.FileSystemError.FileNotDirectory(uri);
    return [...node.children].sort().map((name) => {
      const child = tree.get(rel ? `${rel}/${name}` : name);
      return [name, child && child.type === "dir" ? vscode.FileType.Directory : vscode.FileType.File];
    });
  },

  async readFile(uri) {
    needConn();
    const rel = lib.relPath(uri.path);
    const node = tree.get(rel) || (await refreshTree(), tree.get(rel));
    if (!node || node.type !== "file") throw vscode.FileSystemError.FileNotFound(uri);
    try {
      if (node.kind === "image") return await conn.request("GET", "api/raw", { query: { path: rel }, raw: true });
      if (node.kind !== "text") throw vscode.FileSystemError.Unavailable("The editor serves only text files and images.");
      const f = await conn.request("GET", "api/file", { query: { path: rel } });
      this.versions.set(rel, { version: f.version, eol: f.eol });
      return Buffer.from(lib.withEol(f.text, f.eol), "utf8");
    } catch (err) { throw err instanceof vscode.FileSystemError ? err : fsError(err, uri); }
  },

  async writeFile(uri, content, options) {
    needConn();
    const rel = lib.relPath(uri.path);
    const exists = tree.get(rel);
    if (!exists && !options.create) throw vscode.FileSystemError.FileNotFound(uri);
    if (exists && !options.overwrite) throw vscode.FileSystemError.FileExists(uri);
    try {
      if (!exists) await conn.request("POST", "api/fs", { query: { op: "newfile", path: rel, cid: conn.cid } });
      const { text, eol } = lib.fromEditor(Buffer.from(content).toString("utf8"));
      const base = exists ? this.versions.get(rel)?.version ?? exists.version : null;
      const res = await conn.request("PUT", "api/file", { query: { path: rel, cid: conn.cid }, json: { text, base, eol } });
      this.versions.set(rel, { version: res.version, eol });
      await refreshTree().catch(() => {});
    } catch (err) { throw fsError(err, uri); }
  },

  async createDirectory(uri) {
    try { await needConn().request("POST", "api/fs", { query: { op: "mkdir", path: lib.relPath(uri.path), cid: conn.cid } }); await refreshTree(); }
    catch (err) { throw fsError(err, uri); }
  },

  async delete(uri) {
    try { await needConn().request("POST", "api/fs", { query: { op: "delete", path: lib.relPath(uri.path), cid: conn.cid } }); await refreshTree(); }
    catch (err) { throw fsError(err, uri); }
  },

  async rename(from, to) {
    try {
      await needConn().request("POST", "api/fs", { query: { op: "rename", path: lib.relPath(from.path), to: lib.relPath(to.path), cid: conn.cid } });
      await refreshTree();
    } catch (err) { throw fsError(err, from); }
  },
};

// ---- the server's message bus: file changes, build state ----------------------------------------------------------
function showState(state) {
  if (!state) return;
  const label = { building: "$(sync~spin) Building", ok: "$(check) PDF ready", failed: "$(error) Build failed", idle: "$(file-pdf) LaTeX" }[state.status] || "$(file-pdf) LaTeX";
  status.text = label;
  status.tooltip = state.error || (state.pages ? `${state.pages} pages` : "Build or show the PDF");
  status.command = state.status === "failed" ? "workbench.actions.view.problems" : "latexPipeline.showPdf";
  status.show();
}

async function refreshProblems(state) {
  let warnings = [];
  try { warnings = (await conn.request("GET", "api/warnings")).warnings || []; } catch { /* a viewer may not have them */ }
  diagnostics.clear();
  const sev = { error: vscode.DiagnosticSeverity.Error, warning: vscode.DiagnosticSeverity.Warning, info: vscode.DiagnosticSeverity.Information };
  for (const [file, list] of lib.problems(state ? state.errors : [], warnings)) {
    diagnostics.set(uriFor(file), list.map((p) => {
      const d = new vscode.Diagnostic(new vscode.Range(p.line, 0, p.line, 1000), p.message, sev[p.severity]);
      d.source = "LaTeX";
      return d;
    }));
  }
}

function startPoll() {
  const mine = { stopped: false };
  (async () => {
    let since = null, last = null;
    while (!mine.stopped && conn) {
      try {
        const r = await conn.request("GET", "api/poll", { query: { cid: conn.cid, ...(since == null ? {} : { since }) } });
        since = r.rev;
        const { changed, removed } = lib.fsChanges(r.events, conn.doc);
        if (changed.length || removed.length) {
          await refreshTree().catch(() => {});
          changes.fire([...changed.map((p) => ({ type: vscode.FileChangeType.Changed, uri: uriFor(p) })),
            ...removed.map((p) => ({ type: vscode.FileChangeType.Deleted, uri: uriFor(p) }))]);
        }
        const state = lib.latestState(r.events, conn.doc);
        if (state) {
          showState(state);
          if (state.status !== "building" && state.finished !== last) {
            last = state.finished;
            await refreshProblems(state);
            if (pdfPanel && state.ok) sendPdf().catch(() => {});
          }
        }
      } catch (err) {
        if (err instanceof ApiError && err.status === 401) { vscode.window.showErrorMessage(`LaTeX Pipeline: ${err.message}`); return; }
        await new Promise((r) => setTimeout(r, 5000));
      }
    }
  })();
  return { dispose() { mine.stopped = true; } };
}

// ---- PDF ---------------------------------------------------------------------------------------------------------
function pdfHtml(nonce) {
  const csp = `default-src 'none'; script-src 'nonce-${nonce}' https://cdnjs.cloudflare.com; worker-src blob:; `
    + `style-src 'nonce-${nonce}'; img-src blob: data:; font-src data: blob:; connect-src https://cdnjs.cloudflare.com`;
  return `<!doctype html><html lang="en"><head><meta charset="utf-8"><meta http-equiv="Content-Security-Policy" content="${csp}">
<style nonce="${nonce}">body{margin:0;padding:12px;background:#525659}canvas{display:block;margin:0 auto 12px;box-shadow:0 1px 4px #0006;max-width:100%}p{color:#fff;font:14px sans-serif}</style></head>
<body><p id="msg">Loading the PDF...</p><div id="pages"></div>
<script type="module" nonce="${nonce}">
import * as pdfjs from "${PDFJS}";
pdfjs.GlobalWorkerOptions.workerSrc = "${PDFJS_WORKER}";
const vscode = acquireVsCodeApi();
let run = 0;
window.addEventListener("message", async (e) => {
  if (e.data?.type === "error") { document.getElementById("msg").textContent = e.data.message; return; }
  if (e.data?.type !== "pdf") return;
  const mine = ++run, keep = window.scrollY;
  const pdf = await pdfjs.getDocument({ data: e.data.bytes }).promise;
  const out = [];
  for (let n = 1; n <= pdf.numPages && mine === run; n++) {
    const page = await pdf.getPage(n), view = page.getViewport({ scale: 1.5 * (window.devicePixelRatio || 1) });
    const canvas = document.createElement("canvas");
    canvas.width = view.width; canvas.height = view.height; canvas.style.width = (view.width / (window.devicePixelRatio || 1)) + "px";
    await page.render({ canvasContext: canvas.getContext("2d"), viewport: view }).promise;
    out.push(canvas);
  }
  if (mine !== run) return;
  document.getElementById("msg").textContent = "";
  document.getElementById("pages").replaceChildren(...out);
  window.scrollTo(0, keep);
});
vscode.postMessage({ type: "ready" });
</script></body></html>`;
}

async function sendPdf() {
  if (!pdfPanel) return;
  try {
    const bytes = await conn.request("GET", "pdf/" + conn.doc.split("/").map(encodeURIComponent).join("/"),
      { query: { v: Date.now() }, raw: true });
    pdfPanel.webview.postMessage({ type: "pdf", bytes: new Uint8Array(bytes) });
  } catch (err) {
    pdfPanel.webview.postMessage({ type: "error", message: err.status === 404 ? "Not built yet: run LaTeX Pipeline: Build." : err.message });
  }
}

function showPdf() {
  if (!conn) return vscode.window.showWarningMessage("Connect first (LaTeX Pipeline: Connect).");
  if (pdfPanel) { pdfPanel.reveal(vscode.ViewColumn.Beside); return sendPdf(); }
  pdfPanel = vscode.window.createWebviewPanel("latexPipelinePdf", `${conn.doc}.pdf`, vscode.ViewColumn.Beside,
    { enableScripts: true, localResourceRoots: [], retainContextWhenHidden: true });
  pdfPanel.webview.html = pdfHtml(crypto.randomBytes(16).toString("base64"));
  pdfPanel.webview.onDidReceiveMessage((m) => { if (m?.type === "ready") sendPdf(); });
  pdfPanel.onDidDispose(() => { pdfPanel = null; });
}

// ---- commands ----------------------------------------------------------------------------------------------------
async function connect(context, saved) {
  const target = saved || lib.parseTarget(await vscode.window.showInputBox({
    prompt: "Address of the LaTeX editor: http://localhost:8000/, a share link, or https://<server>/p/<project>/",
    ignoreFocusOut: true, value: "http://localhost:8000/",
  }) || "");
  const key = `latexPipeline:${target.base}`;
  let secret = saved ? await context.secrets.get(key) : await signIn(target);
  if (target.kind === "hosted" && !secret) return;
  const probe = new Connection(target, secret, null);
  const health = await probe.request("GET", "api/health");
  const config = await probe.request("GET", "api/config");
  let doc = saved?.doc;
  if (!doc || !health.docs.includes(doc)) {
    doc = health.docs.length === 1 ? health.docs[0] : await vscode.window.showQuickPick(health.docs, { placeHolder: "Which document?" });
  }
  if (!doc) return;
  if (secret) await context.secrets.store(key, secret); else await context.secrets.delete(key);
  await context.globalState.update(STATE_KEY, { kind: target.kind, base: target.base, project: target.project, doc });
  poller?.dispose();
  conn = new Connection(target, secret, doc);
  conn.role = config.role;
  conn.readOnly = config.role === "view" || config.role === "review";
  await refreshTree();
  poller = startPoll();
  status.text = "$(file-pdf) LaTeX"; status.command = "latexPipeline.showPdf"; status.show();
  const root = uriFor("");
  changes.fire([{ type: vscode.FileChangeType.Changed, uri: root }]);  // the explorer asked before we were connected
  if (!saved && !(vscode.workspace.workspaceFolders || []).some((f) => f.uri.toString() === root.toString())) {
    vscode.workspace.updateWorkspaceFolders((vscode.workspace.workspaceFolders || []).length, 0, { uri: root, name: `LaTeX: ${doc}` });
  }
  if (!saved) vscode.window.showInformationMessage(`Connected to ${doc} as ${config.role}.`);
}

async function disconnect(context) {
  const saved = context.globalState.get(STATE_KEY);
  if (saved) await context.secrets.delete(`latexPipeline:${saved.base}`);
  await context.globalState.update(STATE_KEY, undefined);
  poller?.dispose(); poller = null; conn = null; diagnostics.clear(); status.hide();
  pdfPanel?.dispose();
  const folders = vscode.workspace.workspaceFolders || [];
  const at = folders.findIndex((f) => f.uri.scheme === SCHEME);
  if (at >= 0) vscode.workspace.updateWorkspaceFolders(at, 1);
}

async function build() {
  if (!conn) return vscode.window.showWarningMessage("Connect first (LaTeX Pipeline: Connect).");
  for (const doc of vscode.workspace.textDocuments) if (doc.uri.scheme === SCHEME && doc.isDirty) await doc.save();
  try { await conn.request("POST", "rebuild"); status.text = "$(sync~spin) Building"; }
  catch (err) { vscode.window.showErrorMessage(`Build: ${err.message}`); }
}

async function openFile() {
  if (!conn) return vscode.window.showWarningMessage("Connect first (LaTeX Pipeline: Connect).");
  await refreshTree();
  const files = [...tree.entries()].filter(([, n]) => n.type === "file").map(([p]) => p).sort();
  const pick = await vscode.window.showQuickPick(files, { placeHolder: "Open a file of the document" });
  if (pick) await vscode.window.showTextDocument(uriFor(pick));
}

function activate(context) {
  const wrap = (fn) => async (...args) => { try { await fn(...args); } catch (err) { vscode.window.showErrorMessage(`LaTeX Pipeline: ${err.message}`); } };
  context.subscriptions.push(
    vscode.workspace.registerFileSystemProvider(SCHEME, provider, { isCaseSensitive: true }),
    vscode.commands.registerCommand("latexPipeline.connect", wrap(() => connect(context))),
    vscode.commands.registerCommand("latexPipeline.disconnect", wrap(() => disconnect(context))),
    vscode.commands.registerCommand("latexPipeline.build", wrap(build)),
    vscode.commands.registerCommand("latexPipeline.showPdf", wrap(showPdf)),
    vscode.commands.registerCommand("latexPipeline.openFile", wrap(openFile)),
    diagnostics, status, changes, { dispose: () => poller?.dispose() },
  );
  const saved = context.globalState.get(STATE_KEY);
  if (saved) wrap(() => connect(context, saved))();  // a window reopened with the folder: reconnect quietly
}

function deactivate() { poller?.dispose(); }

module.exports = { activate, deactivate };
