// Node check for pure helpers of scripts/serve_ui that no other check loads: the API's URLs, the message bus's
// revocation and resync rules, replace through co-editing rooms, citation keys and the Share dialog's Zotero note.
// Usage: node tests/cov_check.mjs
import assert from "node:assert/strict";
import path from "node:path";
import { fileURLToPath, pathToFileURL } from "node:url";

const UI = path.join(path.dirname(fileURLToPath(import.meta.url)), "..", "scripts", "serve_ui");
const load = (name) => import(pathToFileURL(path.join(UI, name)).href);

// --- api.js: every request is relative (the gateway serves the editor under /p/<id>/) and parameters are encoded.
{
  const seen = [];
  globalThis.fetch = async (url, opts) => {
    seen.push({ url, opts });
    if (url.startsWith("api/fail")) return { ok: false, status: 403, statusText: "Forbidden", json: async () => ({ error: "nope" }) };
    return { ok: true, status: 200, json: async () => ({ ok: true }) };
  };
  const { api } = await load("api.js");
  const hostile = "a&doc=other#x/../";
  let called = 0;
  for (const [name, fn] of Object.entries(api)) {
    if (typeof fn !== "function") continue;
    seen.length = 0;
    try { await fn(hostile, hostile, hostile, hostile, hostile, hostile, hostile); } catch { /* argument shapes differ */ }
    for (const { url } of seen) {
      called++;
      assert.ok(!url.startsWith("/") && !/^[a-z]+:/i.test(url), `${name}: ${url} is not relative`);
      const query = url.split("?")[1] || "";
      assert.ok(!query.includes("doc=other"), `${name}: parameter not encoded in ${url}`);
    }
  }
  assert.ok(called > 20, `only ${called} API calls checked`);
  // Errors carry the server's message and status.
  globalThis.fetch = async () => ({ ok: false, status: 409, statusText: "Conflict", json: async () => ({ error: "changed" }) });
  await assert.rejects(api.files("d"), (e) => e.status === 409 && e.message === "changed");
  globalThis.fetch = async () => ({ ok: false, status: 502, statusText: "Bad Gateway", json: async () => { throw new Error("html"); } });
  await assert.rejects(api.files("d"), (e) => e.status === 502 && e.message === "Bad Gateway");
}

// --- api.js Channel: revisions only grow except on resync; three 401s in a row stop a revoked link for good.
{
  const { Channel } = await load("api.js");
  const realTimeout = globalThis.setTimeout;
  globalThis.setTimeout = (fn) => realTimeout(fn, 0);
  const ch = new Channel();
  const got = [];
  ch.on("x", (data) => got.push(data)).on("*", (_d, m) => got.push("any:" + m.type));
  ch.emit({ type: "x", rev: 5, data: 1 });
  ch.emit({ type: "x", rev: 3, data: 2 });
  assert.equal(ch.rev, 5);
  ch.emit({ type: "y", rev: 2, resync: true });
  assert.equal(ch.rev, 2);
  assert.deepEqual(got, [1, "any:x", 2, "any:x", "any:y"]);

  let polls = 0;
  globalThis.fetch = async (url) => {
    if (url.startsWith("api/poll")) {
      polls++;
      return { ok: false, status: 401, statusText: "Unauthorized", json: async () => ({ error: "revoked" }) };
    }
    return { ok: true, status: 200, json: async () => ({ replies: [{ type: "pong", data: 7 }] }) };
  };
  const modes = [];
  ch.on("transport", (m) => modes.push(m));
  await ch.poll();
  assert.equal(polls, 3);
  assert.equal(ch.mode, "revoked");
  assert.ok(ch.stopped);
  // send() falls back to POST and hands the replies to the handlers; a failure is false, never a throw.
  const pongs = [];
  ch.on("pong", (d) => pongs.push(d));
  assert.equal(await ch.send({ type: "ping" }), true);
  assert.deepEqual(pongs, [7]);
  globalThis.fetch = async () => { throw new Error("offline"); };
  assert.equal(await ch.send({ type: "ping" }), false);
  globalThis.setTimeout = realTimeout;
}

// --- search.js: files open in a room are replaced through the room, never left half done.
{
  const { replaceThroughRooms } = await load("search.js");
  const calls = [];
  const call = async (body) => {
    calls.push(body);
    if (!body.path) return { count: 2, rooms: ["a.tex", "b.tex", "c.tex"] };
    return { count: 1, text: body.text.replace("old", "new") };
  };
  const saved = [];
  const rooms = {
    "a.tex": { text: () => "old a", applyText: (t, base) => base === "old a" && t === "new a", save: async () => saved.push("a") },
    "b.tex": { text: () => "old b", applyText: () => false, save: async () => saved.push("b") },   // edited meanwhile
  };
  const room = async (p) => { if (!rooms[p]) throw new Error("no room"); return rooms[p]; };
  const out = await replaceThroughRooms(call, { doc: "d", find: "old", replace: "new" }, room);
  assert.deepEqual(out, { count: 3, missed: ["b.tex", "c.tex"] });
  assert.deepEqual(saved, ["a"]);
  assert.deepEqual(calls[1], { doc: "d", find: "old", replace: "new", path: "a.tex", text: "old a" });
  assert.equal(calls.length, 3);   // c.tex: no room, so nothing is sent for it
}

// --- refs.js: citation keys are unique.
{
  const { suggestKey } = await load("refs.js");
  const fields = { author: "Knuth, Donald and Lamport, Leslie", year: "1984", title: "The TeXbook" };
  assert.equal(suggestKey(fields, new Set()), "Knuth1984Texbook");
  assert.equal(suggestKey(fields, new Set(["Knuth1984Texbook"])), "Knuth1984Texbooka");
  const taken = new Set(["Knuth1984Texbook", ...Array.from({ length: 26 }, (_, i) => "Knuth1984Texbook" + String.fromCharCode(97 + i))]);
  assert.equal(suggestKey(fields, taken), "Knuth1984Texbook26");
  assert.equal(suggestKey({}, new Set()), "Ref");
  assert.equal(suggestKey({ editor: "Doe, J.", date: "2020-05-01", title: "On the and of it" }, new Set()), "Doe2020");
}

// --- zotero.js: the owner is told when an edit link could read a stored key.
{
  const { zoteroShareNote } = await load("zotero.js");
  assert.equal(zoteroShareNote(null), "");
  assert.equal(zoteroShareNote({ has_key: true, key_from_env: true }), "");
  assert.match(zoteroShareNote({ has_key: true, sandboxed: false }), /not sandboxed.*read your stored Zotero API key/);
  assert.match(zoteroShareNote({ has_key: true, sandboxed: true, share_editors: true }), /never sent to them/);
  assert.match(zoteroShareNote({ has_key: true, sandboxed: true, share_editors: false }), /off for shared links/);
}

console.log("cov_check: ok");
