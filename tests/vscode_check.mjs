// Node check for vscode/lib.js (pure helpers of the VS Code extension; extension.js itself needs VS Code) and the
// Git panel's pure helpers (scripts/serve_ui/git.js). Usage: node tests/vscode_check.mjs
import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import { createRequire } from "node:module";
import { splitChanges, lineKind, since } from "../scripts/serve_ui/git.js";

const require = createRequire(import.meta.url);
const lib = require("../vscode/lib.js");

// Addresses: local owner, share link, hosted project; tokens never in clear text over the network.
assert.deepEqual(lib.parseTarget("http://localhost:8000/"), { kind: "local", base: "http://localhost:8000/", project: null, token: null });
assert.deepEqual(lib.parseTarget(" https://abc.trycloudflare.com/?token=T0k_en "), { kind: "local", base: "https://abc.trycloudflare.com/", project: null, token: "T0k_en" });
assert.deepEqual(lib.parseTarget("https://latex.example.org/p/0123456789abcdef/"),
  { kind: "hosted", base: "https://latex.example.org/p/0123456789abcdef/", project: "0123456789abcdef", token: null });
assert.equal(lib.parseTarget("https://latex.example.org/p/0123456789abcdef").kind, "hosted");
for (const bad of ["", "ftp://x/", "http://example.org/", "https://u:p@example.org/", "not a url"]) {
  assert.throws(() => lib.parseTarget(bad), Error, bad);
}

assert.equal(lib.apiUrl("http://h/p/x/", "api/file", { doc: "a b", path: "ch/1.tex", none: undefined }), "http://h/p/x/api/file?doc=a+b&path=ch%2F1.tex");
assert.equal(lib.apiUrl("http://h/", "/rebuild"), "http://h/rebuild");
assert.equal(lib.cookieFrom("lp_8000=abc_DEF-1; Path=/; HttpOnly; SameSite=Lax"), "lp_8000=abc_DEF-1");
assert.equal(lib.cookieFrom("evil=1; Path=/"), null);
assert.equal(lib.cookieFrom(null), null);
assert.deepEqual(lib.authHeaders({ kind: "hosted" }, "lpat_x"), { Authorization: "Bearer lpat_x" });
assert.deepEqual(lib.authHeaders({ kind: "local" }, "lp_1=x"), { Cookie: "lp_1=x" });
assert.deepEqual(lib.authHeaders({ kind: "local" }, null), {});

assert.deepEqual(lib.parseVersion("17d3a1b2c3d4e5f6-1a"), { mtime: Math.floor(parseInt("17d3a1b2c3d4e5f6", 16) / 1e6), size: 26 });
assert.deepEqual(lib.parseVersion("junk"), { mtime: 0, size: 0 });

const tree = lib.buildTree({ files: [{ path: "main.tex", kind: "text", size: 3, version: "3b9aca00-3" },
  { path: "ch/a.tex", kind: "text", size: 1, version: "1-1" }, { path: "fig/x/y.png", kind: "image", size: 9, version: "1-9" }], dirs: ["empty"] });
assert.deepEqual([...tree.get("").children].sort(), ["ch", "empty", "fig", "main.tex"]);
assert.deepEqual([...tree.get("fig").children], ["x"]);
assert.equal(tree.get("fig/x/y.png").kind, "image");
assert.equal(tree.get("main.tex").mtime, 1000);
assert.equal(tree.get("empty").type, "dir");

assert.equal(lib.relPath("/ch/a.tex"), "ch/a.tex");
assert.equal(lib.relPath("/"), "");
assert.throws(() => lib.relPath("/a/../b"));
assert.equal(lib.withEol("a\nb", "\r\n"), "a\r\nb");
assert.deepEqual(lib.fromEditor("a\r\nb"), { text: "a\nb", eol: "\r\n" });
assert.deepEqual(lib.fromEditor("a\nb"), { text: "a\nb", eol: "\n" });

const found = lib.problems([{ file: "main.tex", line: 3, message: "Undefined control sequence." }, { file: null, line: 1, message: "x" }],
  [{ file: "main.tex", line: 9, message: "Overfull", level: "info" }, { file: "ch/a.tex", line: null, message: "w", level: "warning" }]);
assert.deepEqual(found.get("main.tex"), [{ line: 2, message: "Undefined control sequence.", severity: "error" }, { line: 8, message: "Overfull", severity: "info" }]);
assert.deepEqual(found.get("ch/a.tex"), [{ line: 0, message: "w", severity: "warning" }]);
assert.equal(found.size, 2);

const events = [{ type: "state", data: { docs: [{ name: "d", status: "building" }, { name: "e" }] } },
  { type: "fs", data: { doc: "d", changed: ["a.tex", "new/"], removed: [] } }, { type: "fs", data: { doc: "x", changed: ["z.tex"], removed: [] } },
  { type: "fs", data: { doc: "d", changed: [], removed: ["a.tex"] } }, { type: "state", data: { docs: [{ name: "d", status: "ok" }] } }];
assert.deepEqual(lib.latestState(events, "d"), { name: "d", status: "ok" });
assert.equal(lib.latestState(events, "nope"), null);
assert.deepEqual(lib.fsChanges(events, "d"), { changed: ["new"], removed: ["a.tex"] });

// The extension's manifest: no build step, an engine range, the commands it registers.
const pkg = JSON.parse(readFileSync(new URL("../vscode/package.json", import.meta.url), "utf8"));
assert.match(pkg.engines.vscode, /^\^1\.\d+\.\d+$/);
assert.equal(pkg.main, "./extension.js");
const source = readFileSync(new URL("../vscode/extension.js", import.meta.url), "utf8");
for (const c of pkg.contributes.commands) assert.ok(source.includes(`"${c.command}"`), c.command);

// Git panel helpers (serve_ui/git.js).
assert.deepEqual(splitChanges([{ path: "a", index: "M", worktree: "M" }, { path: "b", index: "?", worktree: "?" },
  { path: "c", index: "U", worktree: "U", conflict: true }, { path: "d", index: "R", worktree: ".", from: "old" }]),
{ staged: [{ path: "a", code: "M", from: undefined }, { path: "d", code: "R", from: "old" }],
  unstaged: [{ path: "a", code: "M" }, { path: "b", code: "?" }, { path: "c", code: "U" }] });
assert.deepEqual(["+++ b/x", "@@ -1 +1 @@", "+a", "-b", " c", "diff --git a b"].map(lineKind), ["meta", "hunk", "add", "del", "ctx", "meta"]);
assert.equal(since(1000, 1030), "just now");
assert.equal(since(0, 7200), "2 h ago");

console.log("vscode_check: ok");
