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
// workspace: "owner" (every command, files, save, build, Problems, PDF webview, view and edit share links) and
// "reconnect" (the reopened window signs in again from the saved address and secret). VS Code keeps state in memory
// during extension tests, so the runner carries the saved state and secret from the first window to the second.
// Removes its temporary folder, out/vscode-e2e.* and the document's cache and history afterwards.
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
const MAIN = "\\documentclass{article}\n\\begin{document}\n\\section{Hello}\nA synthetic test document.\n\\input{ch/intro}\n\\end{document}\n";

const freePort = () => new Promise((ok) => { const s = createServer().listen(0, "127.0.0.1", () => { const { port } = s.address(); s.close(() => ok(port)); }); });

async function waitHealth(base, server) {
  for (let i = 0; i < 150; i++) {
    if (server.exitCode !== null) throw new Error("serve.py exited");
    try { if ((await fetch(base + "api/health")).ok) return; } catch { /* not up yet */ }
    await new Promise((r) => setTimeout(r, 200));
  }
  throw new Error("serve.py did not start");
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
let failed = false;
try {
  await waitHealth(base, server);
  const vscodeExecutablePath = opts.code || await te.downloadAndUnzipVSCode({ cachePath: opts.cache ? resolve(opts.cache) : undefined });
  const launch = (phase) => te.runTests({
    vscodeExecutablePath, extensionDevelopmentPath: ext, extensionTestsPath: join(ext, "e2e-runner.js"),
    extensionTestsEnv: { VSCODE_E2E: JSON.stringify({ phase, base, doc: DOC, dir, seed: join(tmp, "seed.json"), pdf: join(ROOT, "out", `${DOC}.pdf`) }) },
    launchArgs: [workspace, "--user-data-dir", userData, "--extensions-dir", join(tmp, "exts"), "--disable-extensions",
      "--password-store=basic", "--disable-workspace-trust", "--skip-welcome", "--skip-release-notes", "--disable-gpu",
      ...(process.getuid && process.getuid() === 0 ? ["--no-sandbox"] : [])],
  });
  await launch("owner");
  await launch("reconnect");
  console.log("vscode_e2e: ok");
} catch (err) {
  failed = true;
  console.error("vscode_e2e: FAILED", err && err.message || err);
} finally {
  server.kill();
  rmSync(tmp, { recursive: true, force: true });
  if (/^_x[\w-]+$/.test(cacheName)) for (const top of [".latex-cache", ".latex-history"]) rmSync(join(ROOT, top, cacheName), { recursive: true, force: true });
  for (const suffix of [".pdf", ".log"]) if (existsSync(join(ROOT, "out", DOC + suffix))) rmSync(join(ROOT, "out", DOC + suffix));
}
process.exit(failed ? 1 : 0);
