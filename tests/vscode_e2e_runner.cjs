// Test runner that tests/vscode_e2e.mjs loads into a real VS Code (extensionTestsPath). It is copied into the
// extension folder it tests, so `require("vscode")` here is the extension's own API object and the input boxes and
// quick picks it opens can be answered by replacing them. It is also the copy's main: activate() keeps the context
// (secret storage, saved state) and calls the real one. Settings arrive in VSCODE_E2E (JSON): phase, base, doc,
// dir (the document folder on disk), seed (state carried between the two windows), pdf (out/<doc>.pdf), tokens
// (hosted: good, compile, expired API tokens). Phase "installed" runs from a separate probe extension and checks the
// extension VS Code installed from the .vsix (`code --install-extension`), so it never loads ./extension itself.
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
const inputBoxes = [];  // the options of every input box, and what validateInput said about the answer
// An answer goes through the box's own validateInput, as typing it would: VS Code does not accept a value it rejects,
// so the person can only cancel (undefined).
vscode.window.showInputBox = async (options = {}) => {
  const value = answers.input.shift();
  const box = { prompt: options.prompt, password: !!options.password, invalid: null };
  inputBoxes.push(box);
  if (value === undefined || !options.validateInput) return value;
  const problem = await options.validateInput(value);
  if (!problem) return value;
  box.invalid = typeof problem === "string" ? problem : problem.message;
  return undefined;
};
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
const real = cfg.phase === "installed" ? null : require("./extension");
let context = null;
exports.activate = async (ctx) => {
  context = ctx;
  if (cfg.phase === "reconnect" || cfg.phase === "hosted-reconnect") {
    const seed = JSON.parse(fs.readFileSync(cfg.seed, "utf8"));
    await ctx.globalState.update("latexPipeline.target", seed.target);
    await ctx.secrets.store(seed.key, seed.secret);
  }
  return real.activate(ctx);
};
exports.deactivate = () => real && real.deactivate();

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

  step("save conflict: another writer saved while this editor has unsaved edits");
  const intro = "ch/intro.tex", mine = "Mine, not saved yet.\n";
  const introPuts = () => requests.filter((r) => r.method === "PUT" && new URL(r.url).searchParams.get("path") === intro);
  await editor.edit((e) => e.insert(new vscode.Position(editor.document.lineCount - 1, 0), mine));
  const theirs = disk(intro) + "Theirs, saved first.\n";
  const current = await (await realFetch(`${cfg.base}api/file?doc=${cfg.doc}&path=${intro}`)).json();
  const put = await realFetch(`${cfg.base}api/file?doc=${cfg.doc}&path=${intro}&cid=other-writer`, { method: "PUT",
    headers: { "Content-Type": "application/json" }, body: JSON.stringify({ text: theirs, base: current.version, eol: "\n" }) });
  assert.equal(put.status, 200, "the other writer saved");
  await until("the bus told VS Code", async () => (await vscode.workspace.fs.stat(uri(intro))).size === Buffer.byteLength(theirs));
  // files.saveConflictResolution "askUser" (the default): VS Code compares the stat with the version it read and
  // shows its conflict message (Compare / Overwrite) without writing anything.
  const puts = introPuts().length;
  assert.equal(await editor.document.save(), false, "the save is refused");
  assert.equal(introPuts().length, puts, "VS Code saw the newer file itself: nothing was sent");
  assert.ok(editor.document.isDirty && editor.document.getText().includes(mine), "the unsaved edit stays in the editor");
  assert.equal(disk(intro), theirs, "the other writer's text stays on the server");

  step("Overwrite (the dialog's button, or files.saveConflictResolution = overwriteFileOnDisk) is refused by the server");
  // Both save with ignoreModifiedSince; the extension still sends the version the text was read at, so serve.py says 409.
  const files = vscode.workspace.getConfiguration("files");
  await files.update("saveConflictResolution", "overwriteFileOnDisk", vscode.ConfigurationTarget.Global);
  try {
    assert.equal(await editor.document.save(), false, "the overwrite is refused");
    assert.equal(introPuts().length, puts + 1, "this time the save was sent");
    assert.equal(introPuts().at(-1).status, 409);
    assert.ok(editor.document.isDirty && editor.document.getText().includes(mine), "the unsaved edit stays in the editor");
    assert.equal(disk(intro), theirs, "the other writer's text is not overwritten");
  } finally {
    await files.update("saveConflictResolution", undefined, vscode.ConfigurationTarget.Global);
  }

  step("as the message says: reopen for the current text, redo the edit, save");
  await vscode.window.showTextDocument(editor.document);
  await run("workbench.action.files.revert");
  await until("reverted to the server's text", () => !editor.document.isDirty && editor.document.getText() === theirs);
  const again = vscode.window.activeTextEditor;
  await again.edit((e) => e.insert(new vscode.Position(again.document.lineCount - 1, 0), mine));
  assert.ok(await again.document.save(), "the save on top of the current text goes through");
  assert.equal(disk(intro), theirs + mine, "both changes are kept");

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

async function hostedPhase() {
  const ext = vscode.extensions.getExtension(EXT);
  await ext.activate();
  const { tokens } = cfg;
  const key = `latexPipeline:${cfg.base}`;
  const folderAdded = () => (vscode.workspace.workspaceFolders || []).some((f) => f.uri.scheme === "latexpipeline");
  const connectHosted = async (token) => {
    shown.info.length = 0; shown.error.length = 0; inputBoxes.length = 0;
    answers.input.push(cfg.base, token);
    await run("latexPipeline.connect");
    assert.equal(answers.input.length, 0, "the address and the token were asked for");
  };
  const nothingKept = async () => {
    assert.equal(await context.secrets.get(key), undefined, "no secret stored");
    assert.equal(context.globalState.get("latexPipeline.target"), undefined, "no saved address");
    assert.ok(!folderAdded(), "no folder added");
  };

  for (const [what, token] of [["a value that is no token", "not-a-token"], ["a token cut short", "lpat_short"],
    ["a pasted header", `Bearer ${tokens.good}`]]) {
    step(`malformed input (${what}) is rejected by the token box itself, before any request`);
    const before = requests.length;
    await connectHosted(token);
    const tokenBox = inputBoxes[1];
    assert.ok(tokenBox && tokenBox.password, "the token box hides what is typed");
    assert.equal(tokenBox.invalid, "A token starts with lpat_");
    assert.equal(requests.length, before, "no request went out");
    assert.deepEqual(shown.error, []);
    assert.deepEqual(shown.info, []);
    await nothingKept();
  }

  for (const [what, token, why] of [["an expired token", tokens.expired, /Unknown, expired or revoked API token/],
    ["a token without the projects scope", tokens.compile, /lacks the projects scope/]]) {
    step(`${what} is refused and not kept`);
    await connectHosted(token);
    assert.equal(inputBoxes[1].invalid, null, "well-formed: the box accepts it");
    assert.match(shown.error.pop() || "", why);
    assert.deepEqual(shown.info, []);
    await nothingKept();
  }

  step("a projects token (pasted with spaces around it) connects");
  const sent = requests.length;
  await connectHosted(`  ${tokens.good}\n`);
  assert.equal(inputBoxes[1].invalid, null);
  assert.deepEqual(shown.error, [], "connect shows no error");
  assert.deepEqual(shown.info, [`Connected to ${cfg.doc} as edit.`]);
  await until("workspace folder added", folderAdded);

  step("the token goes only in the Authorization header and in secret storage");
  const mine = () => requests.slice(sent);
  assert.ok(mine().every((r) => r.url.startsWith(cfg.base)), "requests stay on the project");
  assert.ok(mine().every((r) => r.headers.Authorization === `Bearer ${tokens.good}` && !r.headers.Cookie), "bearer token, no cookie");
  assert.ok(mine().every((r) => !r.url.includes(tokens.good)), "never in a URL");
  assert.equal(await context.secrets.get(key), tokens.good, "the token is in secret storage, trimmed");
  const target = context.globalState.get("latexPipeline.target");
  assert.deepEqual(target, { kind: "hosted", base: cfg.base, project: new URL(cfg.base).pathname.split("/")[2], doc: cfg.doc });
  assert.ok(!JSON.stringify(target).includes(tokens.good), "the saved state holds no token");

  step("list, open and save through the gateway");
  const top = new Map(await vscode.workspace.fs.readDirectory(uri("")));
  assert.equal(top.get("main.tex"), vscode.FileType.File);
  assert.equal((await vscode.workspace.fs.stat(uri("main.tex"))).permissions, undefined, "an editor may write");
  answers.pick.push("main.tex");
  await run("latexPipeline.openFile");
  const editor = await until("editor open", () => vscode.window.activeTextEditor?.document.uri.path === "/main.tex" && vscode.window.activeTextEditor);
  assert.equal(editor.document.getText(), disk("main.tex"));
  const end = editor.document.getText().lastIndexOf("\\end{document}");
  await editor.edit((e) => e.insert(editor.document.positionAt(end), "Saved from VS Code through the gateway.\n\n"));
  assert.ok(await editor.document.save(), "save succeeded");
  assert.match(disk("main.tex"), /Saved from VS Code through the gateway\./);

  step("save conflict through the gateway: another writer saved while this editor has unsaved edits");
  const auth = { Authorization: `Bearer ${tokens.good}` };
  const file = "main.tex", ours = "Ours, not saved yet.\n\n";
  const filePuts = () => requests.filter((r) => r.method === "PUT" && new URL(r.url).searchParams.get("path") === file);
  await editor.edit((e) => e.insert(editor.document.positionAt(editor.document.getText().lastIndexOf("\\end{document}")), ours));
  const current = await (await realFetch(`${cfg.base}api/file?doc=${cfg.doc}&path=${file}`, { headers: auth })).json();
  const theirs = current.text.replace("\\end{document}", "Theirs, saved first.\n\n\\end{document}");
  const put = await realFetch(`${cfg.base}api/file?doc=${cfg.doc}&path=${file}&cid=other-writer`, { method: "PUT",
    headers: { ...auth, "Content-Type": "application/json" }, body: JSON.stringify({ text: theirs, base: current.version, eol: "\n" }) });
  assert.equal(put.status, 200, "the other writer saved through the gateway");
  await until("the bus (through the gateway) told VS Code", async () => (await vscode.workspace.fs.stat(uri(file))).size === Buffer.byteLength(theirs));
  const puts = filePuts().length;
  assert.equal(await editor.document.save(), false, "VS Code refuses the save itself");
  assert.equal(filePuts().length, puts, "nothing was sent");
  const files = vscode.workspace.getConfiguration("files");
  await files.update("saveConflictResolution", "overwriteFileOnDisk", vscode.ConfigurationTarget.Global);
  try {
    assert.equal(await editor.document.save(), false, "the overwrite is refused");
    assert.equal(filePuts().length, puts + 1, "this time the save was sent");
    assert.equal(filePuts().at(-1).status, 409, "the worker's 409 comes back through the gateway");
    assert.ok(editor.document.isDirty && editor.document.getText().includes(ours), "the unsaved edit stays in the editor");
    assert.equal(disk(file), theirs, "the other writer's text is not overwritten");
  } finally {
    await files.update("saveConflictResolution", undefined, vscode.ConfigurationTarget.Global);
  }
  await vscode.window.showTextDocument(editor.document);
  await run("workbench.action.files.revert");
  await until("reverted to the server's text", () => !editor.document.isDirty && editor.document.getText() === theirs);
  const again = vscode.window.activeTextEditor;
  await again.edit((e) => e.insert(again.document.positionAt(again.document.getText().lastIndexOf("\\end{document}")), ours));
  assert.ok(await again.document.save(), "the save on top of the current text goes through");
  assert.ok(disk(file).includes("Theirs, saved first.") && disk(file).includes(ours), "both changes are kept");

  step("hosted build errors land in the Problems panel");
  const good = again.document.getText();
  await again.edit((e) => e.replace(new vscode.Range(0, 0, again.document.lineCount, 0), good.replace("\\end{document}", "\\undefinedmacro\n\\end{document}")));
  await run("latexPipeline.build");  // saves the dirty file first
  assert.ok(!again.document.isDirty, "build saved the open file");
  const errs = await until("error diagnostic", () => {
    const found = vscode.languages.getDiagnostics(uri("main.tex")).filter((d) => d.severity === vscode.DiagnosticSeverity.Error);
    return found.length && found;
  }, 180000);
  assert.ok(errs.some((d) => /Undefined control sequence/.test(d.message)), JSON.stringify(errs.map((d) => d.message)));
  const line = good.split("\n").length - 2;  // \undefinedmacro sits where \end{document} was (0-based)
  assert.ok(errs.some((d) => d.range.start.line === line), `error on line ${line + 1}: ${JSON.stringify(errs.map((d) => d.range.start.line))}`);

  step("a good hosted build clears them, then the PDF");
  await again.edit((e) => e.replace(new vscode.Range(0, 0, again.document.lineCount, 0), good));
  await run("latexPipeline.build");
  await until("errors cleared", () => !vscode.languages.getDiagnostics(uri("main.tex")).some((d) => d.severity === vscode.DiagnosticSeverity.Error), 180000);
  const pdfUrl = `${cfg.base}pdf/${encodeURIComponent(cfg.doc)}`;
  await until("PDF built", async () => (await realFetch(pdfUrl, { headers: auth })).ok, 180000);
  const pdfRequests = () => requests.filter((r) => r.url.startsWith(pdfUrl) && r.status === 200);
  await run("latexPipeline.showPdf");
  await until("PDF fetched for the webview", () => pdfRequests().length > 0, 60000);
  assert.deepEqual(shown.error, [], "no errors");
  // Leave connected: the next window must reconnect from the saved address and the token in secret storage.
  fs.writeFileSync(cfg.seed, JSON.stringify({ target, key, secret: tokens.good }), { mode: 0o600 });
}

async function hostedReconnectPhase() {
  step("the reopened window reconnects to the hosted project quietly, with the token from secret storage");
  const { key, secret } = JSON.parse(fs.readFileSync(cfg.seed, "utf8"));
  const folderAdded = () => (vscode.workspace.workspaceFolders || []).some((f) => f.uri.scheme === "latexpipeline");
  assert.ok(folderAdded(), "folder kept in the workspace");
  const names = new Map(await vscode.workspace.fs.readDirectory(uri("")));
  assert.ok(names.has("main.tex"));
  assert.match(Buffer.from(await vscode.workspace.fs.readFile(uri("main.tex"))).toString(), /Theirs, saved first\./);
  const sent = requests.filter((r) => r.url.includes("/api/"));
  assert.ok(sent.length, "requests went out");
  assert.ok(sent.every((r) => r.url.startsWith(cfg.base) && r.headers.Authorization === `Bearer ${secret}` && !r.headers.Cookie), "bearer token from secret storage");
  assert.deepEqual(inputBoxes, [], "nothing was asked");
  assert.deepEqual(shown.info, [], "no message on a quiet reconnect");
  assert.deepEqual(shown.error, []);

  step("the reconnected window saves");
  await vscode.workspace.fs.writeFile(uri("main.tex"), Buffer.from(disk("main.tex").replace("\\end{document}", "After the restart.\n\n\\end{document}")));
  assert.match(disk("main.tex"), /After the restart\./);

  step("disconnect forgets the token");
  await run("latexPipeline.disconnect");
  await until("folder removed", () => !folderAdded());
  assert.equal(await context.secrets.get(key), undefined, "disconnect deletes the token");
  assert.equal(context.globalState.get("latexPipeline.target"), undefined, "and the saved address");
}

// The extension as VS Code installed it from the .vsix: its own files (not a development folder), activated by
// VS Code, every command registered, the file system provider answering. A missing file in the package fails here.
async function installedPhase() {
  const ext = vscode.extensions.getExtension(EXT);
  assert.ok(ext, "the packaged extension is installed");
  assert.ok(ext.extensionPath.startsWith(cfg.extensionsDir), `installed under --extensions-dir: ${ext.extensionPath}`);
  const listed = fs.readdirSync(ext.extensionPath).sort();
  step("installed files " + listed.join(", "));
  for (const f of ["extension.js", "lib.js", "package.json"]) assert.ok(listed.includes(f), `${f} in the package`);
  assert.ok(listed.some((f) => /^readme\.md$/i.test(f)) && listed.some((f) => /^license/i.test(f)), "readme and licence (vsce renames them)");
  await ext.activate();
  assert.ok(ext.isActive, "activated by VS Code");
  await assert.rejects(Promise.resolve(vscode.workspace.fs.stat(uri("main.tex"))), /Not connected/);  // its file system provider
  const have = await vscode.commands.getCommands(true);
  for (const c of ext.packageJSON.contributes.commands) assert.ok(have.includes(c.command), `command ${c.command} registered`);
  // Disconnect needs no input and shows nothing (the others would wait on a real message box this runner cannot
  // answer: the installed extension has its own API object, so the runner's replacements do not reach it).
  await run("latexPipeline.disconnect");
  step("commands " + ext.packageJSON.contributes.commands.map((c) => c.command).join(", "));
}

exports.run = async function run() {
  try {
    await ({ owner: ownerPhase, reconnect: reconnectPhase, hosted: hostedPhase, "hosted-reconnect": hostedReconnectPhase,
      installed: installedPhase }[cfg.phase])();
    step("ok");
  } catch (err) {
    console.error(`[vscode_e2e] ${cfg.phase} FAILED:`, err && err.stack || err);
    console.error("[vscode_e2e] messages:", JSON.stringify(shown));
    throw err;
  }
};
