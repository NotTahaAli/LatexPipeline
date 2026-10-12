// The VS Code extension (vscode/) in a real VS Code against a live scripts/serve.py. Not part of CI or `unittest
// discover`; needs LaTeX, a display (xvfb-run on a headless machine) and network for VS Code and pdf.js:
//
//   npm install --prefix /tmp/vt @vscode/test-electron@3.1.0
//   xvfb-run -a node tests/vscode_e2e.mjs --modules /tmp/vt/node_modules [--cache DIR] [--code PATH/TO/code]
//
// --modules: the node_modules holding @vscode/test-electron (kept out of the extension, which has no dependencies).
// --cache: where test-electron downloads VS Code (default .vscode-test). --code: an already unpacked VS Code.
// It writes a tiny document to a temporary folder, serves it and copies vscode/ with tests/vscode_e2e_runner.cjs as
// the copy's main (the runner must sit inside the extension to get the extension's own `vscode` API object, so it
// can answer the input boxes, and it keeps the extension's context to check secret storage). Two windows run on one
// workspace: "owner" (every command, files, save, a save conflict (409), build, Problems, PDF webview, view and edit
// share links) and "reconnect" (the reopened window signs in again from the saved address and secret). VS Code keeps
// state in memory during extension tests, so the runner carries the saved state and secret from the first window to
// the second. A third window, "hosted", connects to a project of a local scripts/host.py (temporary data folder,
// workers in bubblewrap, so `bwrap` must work) with a personal API token: an expired token and one without the
// projects scope are refused, the good one lists, opens, saves and builds and lives only in secret storage.
// Removes its temporary folders, out/vscode-e2e.* and the document's cache and history afterwards.
import { spawn, execFileSync } from "node:child_process";
import { cpSync, existsSync, mkdirSync, mkdtempSync, readFileSync, rmSync, writeFileSync } from "node:fs";
import { createRequire } from "node:module";
import { createServer } from "node:net";
import { tmpdir } from "node:os";
import { join, resolve } from "node:path";
import { parseArgs } from "node:util";

const ROOT = resolve(import.meta.dirname, "..");
const DOC = "vscode-e2e";
const { values: opts } = parseArgs({ options: { modules: { type: "string" }, cache: { type: "string" }, code: { type: "string" } } });
if (!opts.modules) { console.error("usage: node tests/vscode_e2e.mjs --modules DIR/node_modules [--cache DIR] [--code PATH]"); process.exit(2); }
const te = createRequire(join(resolve(opts.modules), "noop.js"))("@vscode/test-electron");

// A 1x1 PNG, served through api/raw.
const PNG = Buffer.from("iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mP8z8DwHwAFBQIAX8jx0gAAAABJRU5ErkJggg==", "base64");
const PASSWORD = "password-123";
const MAIN = "\\documentclass{article}\n\\begin{document}\n\\section{Hello}\nA synthetic test document.\n\\input{ch/intro}\n\\end{document}\n";

const freePort = () => new Promise((ok) => { const s = createServer().listen(0, "127.0.0.1", () => { const { port } = s.address(); s.close(() => ok(port)); }); });

// host.py's JSON API as one signed-in browser would use it: the session cookie, the CSRF token and our own Origin.
async function hostApi(origin, jar, method, path, body) {
  const headers = { Origin: origin, ...(body === undefined ? {} : { "Content-Type": "application/json" }) };
  if (jar.cookies) headers.Cookie = Object.entries(jar.cookies).map(([k, v]) => `${k}=${v}`).join("; ");
  if (jar.csrf) headers["X-CSRF-Token"] = jar.csrf;
  const res = await fetch(origin + path, { method, headers, body: body === undefined ? undefined : JSON.stringify(body), redirect: "manual" });
  for (const c of res.headers.getSetCookie()) { const [kv] = c.split(";"); const at = kv.indexOf("="); (jar.cookies ||= {})[kv.slice(0, at)] = kv.slice(at + 1); }
  const data = await res.json();
  if (!res.ok) throw new Error(`${method} ${path}: ${res.status} ${data.error}`);
  if (data.csrf) jar.csrf = data.csrf;
  return data;
}

// A site admin (host.py init), a workspace, an editor who signs up through an invite, a project and three tokens.
async function startHost(tmp) {
  const data = join(tmp, "host"), port = await freePort(), origin = `http://127.0.0.1:${port}`;
  execFileSync("python3", [join(ROOT, "scripts", "host.py"), "init", "--data", data], { stdio: "ignore",
    env: { ...process.env, LP_ADMIN_EMAIL: "root@example.org", LP_ADMIN_PASSWORD: PASSWORD, LP_ADMIN_NAME: "Root" } });
  writeFileSync(join(data, "config.toml"), `public_url = "${origin}"\nport = ${port}\n[api]\nenabled = true\n`);
  const proc = spawn("python3", [join(ROOT, "scripts", "host.py"), "serve", "--data", data], { cwd: ROOT, stdio: ["ignore", "inherit", "inherit"] });
  await waitUp(origin + "/api/me", proc);
  const admin = {}, editor = {};
  await hostApi(origin, admin, "POST", "/api/login", { email: "root@example.org", password: PASSWORD });
  const { id: tid } = await hostApi(origin, admin, "POST", "/api/admin/tenants", { name: "E2E workspace" });
  const { link } = await hostApi(origin, admin, "POST", `/api/tenants/${tid}/invites`, { role: "editor" });
  await hostApi(origin, editor, "POST", "/api/signup", { email: "editor@example.org", name: "Editor", password: PASSWORD, invite: link.split("#invite=")[1] });
  const { id: pid } = await hostApi(origin, editor, "POST", `/api/tenants/${tid}/projects`, { name: DOC });
  const mint = async (name, scopes) => (await hostApi(origin, editor, "POST", "/api/account/tokens",
    { name, scopes, days: 1, ...(scopes.includes("projects") ? { projects: [pid] } : {}) })).token;
  const tokens = { good: await mint("vscode", ["projects"]), compile: await mint("compile only", ["compile"]), expired: await mint("old", ["projects"]) };
  // Tokens last at least a day, so the expired one is aged in the database.
  execFileSync("python3", ["-I", "-c", "import sqlite3, sys, time; db = sqlite3.connect(sys.argv[1]); "
    + "db.execute('UPDATE api_tokens SET expires = ? WHERE name = ?', (time.time() - 60, 'old')); db.commit()", join(data, "host.db")]);
  return { proc, base: `${origin}/p/${pid}/`, dir: join(data, "projects", tid, pid, DOC), tokens };
}

async function waitUp(url, server) {
  for (let i = 0; i < 150; i++) {
    if (server.exitCode !== null) throw new Error("the server exited");
    try { if ((await fetch(url)).ok) return; } catch { /* not up yet */ }
    await new Promise((r) => setTimeout(r, 200));
  }
  throw new Error("the server did not start");
}

const tmp = mkdtempSync(join(tmpdir(), "vscode-e2e-"));
const src = join(tmp, "src"), dir = join(src, DOC), ext = join(tmp, "ext"), userData = join(tmp, "user"), wsDir = join(tmp, "ws");
mkdirSync(join(dir, "ch"), { recursive: true });
mkdirSync(wsDir);
writeFileSync(join(dir, "main.tex"), MAIN);
writeFileSync(join(dir, "ch", "intro.tex"), "An included chapter.\n");
writeFileSync(join(dir, "fig.png"), PNG);
cpSync(join(ROOT, "vscode"), ext, { recursive: true });
cpSync(join(ROOT, "tests", "vscode_e2e_runner.cjs"), join(ext, "e2e-runner.js"));
// The runner is also the copy's main: it hands the real activate() the context and keeps it for its checks.
const manifest = JSON.parse(readFileSync(join(ext, "package.json"), "utf8"));
writeFileSync(join(ext, "package.json"), JSON.stringify({ ...manifest, main: "./e2e-runner.js" }));
const workspace = join(tmp, "e2e.code-workspace");
writeFileSync(workspace, JSON.stringify({ folders: [{ path: wsDir }] }));
const cacheName = execFileSync("python3", ["-I", "-c", "import sys; from pathlib import Path; sys.path.insert(0, sys.argv[1]); "
  + "import build; print(build.cache_dir_for(Path(sys.argv[2])).name)", join(ROOT, "scripts"), join(dir, "main.tex")], { encoding: "utf8" }).trim();

const port = await freePort();
const base = `http://127.0.0.1:${port}/`;
const server = spawn("python3", [join(ROOT, "scripts", "serve.py"), DOC, "--source", src, "--no-open", "--port", String(port)],
  { cwd: ROOT, stdio: ["ignore", "inherit", "inherit"] });
let failed = false, host = null;
try {
  await waitUp(base + "api/health", server);
  const vscodeExecutablePath = opts.code || await te.downloadAndUnzipVSCode({ cachePath: opts.cache ? resolve(opts.cache) : undefined });
  const launch = (phase, extra = {}, ws = workspace) => te.runTests({
    vscodeExecutablePath, extensionDevelopmentPath: ext, extensionTestsPath: join(ext, "e2e-runner.js"),
    extensionTestsEnv: { VSCODE_E2E: JSON.stringify({ phase, base, doc: DOC, dir, seed: join(tmp, "seed.json"), pdf: join(ROOT, "out", `${DOC}.pdf`), ...extra }) },
    launchArgs: [ws, "--user-data-dir", userData, "--extensions-dir", join(tmp, "exts"), "--disable-extensions",
      "--password-store=basic", "--disable-workspace-trust", "--skip-welcome", "--skip-release-notes", "--disable-gpu",
      ...(process.getuid && process.getuid() === 0 ? ["--no-sandbox"] : [])],
  });
  await launch("owner");
  await launch("reconnect");
  host = await startHost(tmp);
  const hostedWs = join(tmp, "hosted.code-workspace");  // a fresh workspace: no folder left from the windows above
  writeFileSync(hostedWs, JSON.stringify({ folders: [{ path: wsDir }] }));
  await launch("hosted", { base: host.base, dir: host.dir, pdf: null, tokens: host.tokens }, hostedWs);
  console.log("vscode_e2e: ok");
} catch (err) {
  failed = true;
  console.error("vscode_e2e: FAILED", err && err.message || err);
} finally {
  server.kill();
  host?.proc.kill();
  rmSync(tmp, { recursive: true, force: true });
  if (/^_x[\w-]+$/.test(cacheName)) for (const top of [".latex-cache", ".latex-history"]) rmSync(join(ROOT, top, cacheName), { recursive: true, force: true });
  for (const suffix of [".pdf", ".log"]) if (existsSync(join(ROOT, "out", DOC + suffix))) rmSync(join(ROOT, "out", DOC + suffix));
}
process.exit(failed ? 1 : 0);
