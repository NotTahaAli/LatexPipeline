// Test runner that tests/vscode_e2e.mjs loads into a real VS Code (extensionTestsPath). It is copied into the
// extension folder it tests, so `require("vscode")` here is the extension's own API object and the input boxes and
// quick picks it opens can be answered by replacing them. It is also the copy's main: activate() keeps the context
// (secret storage, saved state) and calls the real one. Settings arrive in VSCODE_E2E (JSON): phase, base, doc,
// dir (the document folder on disk), seed (state carried between the two windows), pdf (out/<doc>.pdf).
"use strict";

const assert = require("assert/strict");
const fs = require("fs");
const path = require("path");
const vscode = require("vscode");

const cfg = JSON.parse(process.env.VSCODE_E2E);
const EXT = "latex-pipeline.latex-pipeline";
const uri = (rel) => vscode.Uri.from({ scheme: "latexpipeline", authority: "project", path: "/" + rel });
const disk = (rel) => fs.readFileSync(path.join(cfg.dir, rel), "utf8");
const sleep = (ms) => new Promise((r) => setTimeout(r, ms));

async function until(what, fn, ms = 60000) {
  const end = Date.now() + ms;
  let last;
  while (Date.now() < end) {
    try { const v = await fn(); if (v) return v; } catch (err) { last = err; }
    await sleep(200);
  }
  throw new Error(`timed out: ${what}${last ? ` (${last.message})` : ""}`);
}

// What the extension shows and asks: answers come from `answers`, messages are recorded.
const shown = { info: [], warning: [], error: [] };
const answers = { input: [], pick: [] };
const picksOffered = [];
vscode.window.showInputBox = async () => answers.input.shift();
vscode.window.showQuickPick = async (items) => { picksOffered.push(await items); return answers.pick.shift(); };
for (const kind of ["info", "warning", "error"]) {
  const name = { info: "showInformationMessage", warning: "showWarningMessage", error: "showErrorMessage" }[kind];
  vscode.window[name] = async (msg) => { shown[kind].push(String(msg)); return undefined; };
}

// Every request the extension makes (it uses the extension host's global fetch).
const requests = [];
const realFetch = globalThis.fetch;
globalThis.fetch = async (url, init = {}) => {
  const res = await realFetch(url, init);
  requests.push({ url: String(url), method: init.method || "GET", headers: { ...(init.headers || {}) }, status: res.status });
  return res;
};

// VS Code runs extension tests with in-memory storage, so the second window gets the first one's saved state here.
const real = require("./extension");
let context = null;
exports.activate = async (ctx) => {
  context = ctx;
  if (cfg.phase === "reconnect") {
    const seed = JSON.parse(fs.readFileSync(cfg.seed, "utf8"));
    await ctx.globalState.update("latexPipeline.target", seed.target);
    await ctx.secrets.store(seed.key, seed.secret);
  }
  return real.activate(ctx);
};
exports.deactivate = real.deactivate;

const run = (cmd) => vscode.commands.executeCommand(cmd);
const step = (name) => console.log(`[vscode_e2e] ${cfg.phase}: ${name}`);

async function commandsExist() {
  const pkg = JSON.parse(fs.readFileSync(path.join(__dirname, "package.json"), "utf8"));
  const have = await vscode.commands.getCommands(true);
  for (const c of pkg.contributes.commands) assert.ok(have.includes(c.command), `command ${c.command} registered`);
  return pkg.contributes.commands.map((c) => c.command);
}

async function connectWith(address) {
  shown.info.length = 0; shown.error.length = 0;
  answers.input.push(address);
  await run("latexPipeline.connect");
  assert.deepEqual(shown.error, [], "connect shows no error");
  assert.equal(answers.input.length, 0, "the address was asked for");
}

async function ownerPhase() {
  const ext = vscode.extensions.getExtension(EXT);
  assert.ok(ext, "extension installed for development");
  await ext.activate();
  assert.ok(context, "the runner is the main module VS Code activated");
  step("commands " + (await commandsExist()).join(", "));

  step("commands before connecting say so");
  for (const cmd of ["latexPipeline.build", "latexPipeline.openFile", "latexPipeline.showPdf"]) await run(cmd);
  assert.equal(shown.warning.filter((m) => m.includes("Connect first")).length, 3);
  await assert.rejects(Promise.resolve(vscode.workspace.fs.stat(uri("main.tex"))), /Not connected/);

  step("plain http to another computer is refused before any request");
  const before = requests.length;
  answers.input.push("http://example.org:8000/");
  await run("latexPipeline.connect");
  assert.match(shown.error.pop() || "", /Use https for anything but this computer/);
  assert.equal(requests.length, before, "no request went out");

  step("connect as the local owner");
  await connectWith(cfg.base);
  assert.deepEqual(shown.info, [`Connected to ${cfg.doc} as owner.`]);
  await until("workspace folder added", () => (vscode.workspace.workspaceFolders || []).some((f) => f.uri.scheme === "latexpipeline"));
  assert.ok(requests.every((r) => !r.headers.Cookie && !r.headers.Authorization), "the owner sends no credentials");

  step("list files and folders");
  const top = new Map(await vscode.workspace.fs.readDirectory(uri("")));
  assert.equal(top.get("main.tex"), vscode.FileType.File);
  assert.equal(top.get("fig.png"), vscode.FileType.File);
  assert.equal(top.get("ch"), vscode.FileType.Directory);
  assert.deepEqual(await vscode.workspace.fs.readDirectory(uri("ch")), [["intro.tex", vscode.FileType.File]]);
  const st = await vscode.workspace.fs.stat(uri("main.tex"));
  assert.equal(st.size, Buffer.byteLength(disk("main.tex")));
  assert.equal(st.permissions, undefined, "the owner may write");
  assert.deepEqual(Buffer.from(await vscode.workspace.fs.readFile(uri("fig.png"))), fs.readFileSync(path.join(cfg.dir, "fig.png")));
  await assert.rejects(Promise.resolve(vscode.workspace.fs.stat(uri("nope.tex"))), (e) => e.code === "FileNotFound");

  step("open a file through the quick pick");
  answers.pick.push("ch/intro.tex");
  await run("latexPipeline.openFile");
  assert.deepEqual(picksOffered.pop(), ["ch/intro.tex", "fig.png", "main.tex"]);
  const editor = await until("editor open", () => vscode.window.activeTextEditor?.document.uri.path === "/ch/intro.tex" && vscode.window.activeTextEditor);
  assert.equal(editor.document.getText(), disk("ch/intro.tex"));

  step("edit and save");
  await editor.edit((e) => e.insert(new vscode.Position(editor.document.lineCount - 1, 0), "Saved from VS Code.\n"));
  assert.ok(await editor.document.save(), "save succeeded");
  assert.match(disk("ch/intro.tex"), /Saved from VS Code\./);

  step("a change on disk reaches the open editor (bus poll)");
  fs.writeFileSync(path.join(cfg.dir, "ch/intro.tex"), disk("ch/intro.tex") + "Changed on disk.\n");
  await until("editor reloaded", () => editor.document.getText().includes("Changed on disk."));

  step("create, rename and delete files and folders");
  await vscode.workspace.fs.writeFile(uri("extra.tex"), Buffer.from("% extra\r\nline\r\n"));
  assert.equal(disk("extra.tex"), "% extra\r\nline\r\n", "CRLF kept");
  assert.equal(Buffer.from(await vscode.workspace.fs.readFile(uri("extra.tex"))).toString(), "% extra\r\nline\r\n");
  await vscode.workspace.fs.createDirectory(uri("more"));
  assert.ok(fs.statSync(path.join(cfg.dir, "more")).isDirectory());
  await vscode.workspace.fs.rename(uri("extra.tex"), uri("more/extra.tex"));
  assert.ok(fs.existsSync(path.join(cfg.dir, "more/extra.tex")) && !fs.existsSync(path.join(cfg.dir, "extra.tex")));
  await vscode.workspace.fs.delete(uri("more/extra.tex"));
  await vscode.workspace.fs.delete(uri("more"));
  assert.ok(!fs.existsSync(path.join(cfg.dir, "more")));

  step("build errors land in the Problems panel");
  const main = await vscode.workspace.openTextDocument(uri("main.tex"));
  const good = main.getText();
  const shownMain = await vscode.window.showTextDocument(main);
  await shownMain.edit((e) => e.replace(new vscode.Range(0, 0, main.lineCount, 0), good.replace("\\end{document}", "\\undefinedmacro\n\\end{document}")));
  await run("latexPipeline.build");  // saves the dirty file first
  assert.ok(!main.isDirty, "build saved the open file");
  const errs = await until("error diagnostic", () => {
    const found = vscode.languages.getDiagnostics(uri("main.tex")).filter((d) => d.severity === vscode.DiagnosticSeverity.Error);
    return found.length && found;
  }, 180000);
  assert.ok(errs.some((d) => /Undefined control sequence/.test(d.message)), JSON.stringify(errs.map((d) => d.message)));

  step("a good build clears them and makes the PDF");
  const pdfBefore = fs.existsSync(cfg.pdf) ? fs.statSync(cfg.pdf).mtimeMs : 0;
  await shownMain.edit((e) => e.replace(new vscode.Range(0, 0, main.lineCount, 0), good));
  await run("latexPipeline.build");
  await until("PDF rebuilt", () => fs.existsSync(cfg.pdf) && fs.statSync(cfg.pdf).mtimeMs > pdfBefore, 180000);
  await until("errors cleared", () => !vscode.languages.getDiagnostics(uri("main.tex")).some((d) => d.severity === vscode.DiagnosticSeverity.Error), 60000);

  step("PDF webview loads pdf.js and asks for the PDF");
  const pdfRequests = () => requests.filter((r) => new URL(r.url).pathname === `/pdf/${encodeURIComponent(cfg.doc)}` && r.status === 200);
  const had = pdfRequests().length;
  await run("latexPipeline.showPdf");
  const tab = await until("webview tab", () => vscode.window.tabGroups.all.flatMap((g) => g.tabs)
    .find((t) => t.input instanceof vscode.TabInputWebview && t.input.viewType.endsWith("latexPipelinePdf")));
  assert.equal(tab.label, `${cfg.doc}.pdf`);
  // The page posts "ready" only after its module imported pdf.js from the pinned CDN URL; then the PDF is fetched.
  await until("PDF fetched for the webview", () => pdfRequests().length > had, 60000);

  step("disconnect");
  await run("latexPipeline.disconnect");
  await until("folder removed", () => !(vscode.workspace.workspaceFolders || []).some((f) => f.uri.scheme === "latexpipeline"));
  await until("webview closed", () => !vscode.window.tabGroups.all.flatMap((g) => g.tabs).some((t) => t.input instanceof vscode.TabInputWebview));
  await assert.rejects(Promise.resolve(vscode.workspace.fs.stat(uri("main.tex"))), /Not connected/);

  step("start sharing (local token links)");
  const { links } = await (await realFetch(cfg.base + "api/share", { method: "POST", headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ provider: "local", doc: cfg.doc }) })).json();
  assert.ok(links.view && links.edit, "share links");
  const local = (link) => link.replace(/^http:\/\/localhost:/, "http://127.0.0.1:");

  step("a view link: files are read-only");
  await connectWith(local(links.view));
  assert.deepEqual(shown.info, [`Connected to ${cfg.doc} as view.`]);
  assert.equal((await vscode.workspace.fs.stat(uri("main.tex"))).permissions, vscode.FilePermission.Readonly);
  await assert.rejects(Promise.resolve(vscode.workspace.fs.writeFile(uri("main.tex"), Buffer.from("x"))), (e) => e.code === "NoPermissions");
  assert.equal(disk("main.tex"), good);

  step("an edit link: its token becomes a cookie, kept in secret storage");
  const sent = requests.length;
  await connectWith(local(links.edit));
  assert.deepEqual(shown.info, [`Connected to ${cfg.doc} as edit.`]);
  const later = requests.slice(sent).filter((r) => r.url.includes("/api/"));
  assert.ok(later.length && later.every((r) => /^lp_\d+=[A-Za-z0-9_-]+$/.test(r.headers.Cookie || "")), "API requests carry the cookie");
  assert.ok(later.every((r) => !r.url.includes("token=")), "the token itself is not sent again");
  const key = `latexPipeline:${new URL(links.edit).origin.replace("localhost", "127.0.0.1")}/`;
  const secret = await context.secrets.get(key);
  assert.equal(secret, later[0].headers.Cookie, "the cookie is in secret storage");
  const target = context.globalState.get("latexPipeline.target");
  assert.deepEqual(target, { kind: "local", base: new URL(key.slice("latexPipeline:".length)).href, project: null, doc: cfg.doc });
  assert.ok(!JSON.stringify(target).includes(secret.split("=")[1]), "the saved state holds no credential");
  await vscode.workspace.fs.writeFile(uri("ch/intro.tex"), Buffer.from(disk("ch/intro.tex") + "Edit link.\n"));
  assert.match(disk("ch/intro.tex"), /Edit link\./);
  // Leave connected: the next window must reconnect from the saved address and the secret.
  fs.writeFileSync(cfg.seed, JSON.stringify({ target, key, secret }), { mode: 0o600 });
}

async function reconnectPhase() {
  step("the reopened window reconnects quietly from the saved address and secret");
  assert.ok((vscode.workspace.workspaceFolders || []).some((f) => f.uri.scheme === "latexpipeline"), "folder kept in the workspace");
  // No retry: requests made while the reconnect is still on its way wait for it.
  const names = new Map(await vscode.workspace.fs.readDirectory(uri("")));
  assert.ok(names.has("main.tex"));
  assert.match(Buffer.from(await vscode.workspace.fs.readFile(uri("ch/intro.tex"))).toString(), /Edit link\./);
  assert.ok(requests.filter((r) => r.url.includes("/api/")).every((r) => /^lp_\d+=/.test(r.headers.Cookie || "")), "cookie from secret storage");
  assert.deepEqual(shown.info, [], "no message on a quiet reconnect");
  const { key } = JSON.parse(fs.readFileSync(cfg.seed, "utf8"));
  await run("latexPipeline.disconnect");
  await until("folder removed", () => !(vscode.workspace.workspaceFolders || []).some((f) => f.uri.scheme === "latexpipeline"));
  assert.equal(await context.secrets.get(key), undefined, "disconnect deletes the secret");
  assert.equal(context.globalState.get("latexPipeline.target"), undefined, "and the saved address");
}

exports.run = async function run() {
  try {
    await (cfg.phase === "owner" ? ownerPhase() : reconnectPhase());
    step("ok");
  } catch (err) {
    console.error(`[vscode_e2e] ${cfg.phase} FAILED:`, err && err.stack || err);
    console.error("[vscode_e2e] messages:", JSON.stringify(shown));
    throw err;
  }
};
