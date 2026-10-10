// Node check for scripts/serve_ui/collab.js: runs the real Room class with the Yjs libraries stubbed out
// (they come from a CDN in the browser). Usage: node collab_check.mjs <path to collab.js>
import fs from "node:fs";
import os from "node:os";
import path from "node:path";
import assert from "node:assert/strict";
import { pathToFileURL } from "node:url";

const source = fs.readFileSync(process.argv[2], "utf8").replace(/^import .*libs\.js";$/m, `
class Doc { constructor() { this.t = ""; this.store = { clients: new Map() }; }
  getText() { const d = this; return { toString: () => d.t, doc: d, insert(i, s) { d.t = d.t.slice(0, i) + s + d.t.slice(i); }, delete(i, n) { d.t = d.t.slice(0, i) + d.t.slice(i + n); } }; }
  on() {} destroy() {} transact(fn) { fn(); } }
const Y = { Doc, UndoManager: class { destroy() {} }, applyUpdate() {}, encodeStateAsUpdate: () => new Uint8Array(), encodeStateVector: () => new Uint8Array() };
const awarenessProtocol = { Awareness: class { constructor() { this.clientID = 1; } setLocalStateField() {} on() {} destroy() {} }, encodeAwarenessUpdate: () => new Uint8Array() };
const AP = awarenessProtocol;
`);
const dir = fs.mkdtempSync(path.join(os.tmpdir(), "collab-"));
const file = path.join(dir, "collab.mjs");
fs.writeFileSync(file, source);
const { Room } = await import(pathToFileURL(file).href);

function make(api) {
  const sent = [], warned = [];
  const collab = { channel: { cid: "me", send: (m) => sent.push(m) }, api, role: "edit", user: () => ({ name: "n", color: "#000000" }), saveDelay: () => 5, onWarn: (t) => warned.push(t), onChange() {}, rooms: new Map() };
  const room = new Room(collab, "d", "main.tex");
  room.ytext.doc.t = "room text";
  room.leader = "me";
  return { room, sent, warned };
}
const err = (status) => Object.assign(new Error("boom " + status), { status });
const wait = (ms) => new Promise((r) => setTimeout(r, ms));
let writes;

// 1. A failed read (not 404) must not lead to a save without a base version; it retries and stays read-only.
{
  writes = 0; let reads = 0;
  const { room } = make({ read: async () => { reads++; throw err(500); }, write: async () => { writes++; return { version: "1" }; } });
  await room.becomeLeader();
  assert.equal(room.savedText, null);
  assert.match(room.error, /retrying/);
  room.touch(); await wait(30);
  assert.equal(writes, 0, "saved after a failed read");
  assert.equal(room.save && await room.save(), undefined);
  assert.equal(writes, 0);
  clearTimeout(room.retryTimer);
}
// 2. 404 on a file that never existed: the first save creates it (base null).
{
  let args;
  const { room } = make({ read: async () => { throw err(404); }, write: async (...a) => { args = a; return { version: "1" }; } });
  await room.becomeLeader(); await wait(30);
  assert.equal(args?.[3], null);
}
// 3. 404 on a file that was deleted behind the room: drop the room and warn, write nothing.
{
  writes = 0;
  const { room, warned, sent } = make({ read: async () => { throw err(404); }, write: async () => { writes++; return { version: "1" }; } });
  await room.becomeLeader({ stale: true, base: "old", gone: true }); await wait(30);
  assert.equal(writes, 0); assert.equal(room.isLeader, false); assert.equal(warned.length, 1);
  assert.ok(sent.some((m) => m.type === "y-leave"));
}
// 4. A leader sees the file removed on disk: no recreate.
{
  writes = 0;
  const { room, warned } = make({ read: async () => ({ text: "room text", version: "1", eol: "\n" }), write: async () => { writes++; return { version: "2" }; } });
  await room.becomeLeader(); await room.onFs(true); await wait(30);
  assert.equal(writes, 0); assert.equal(room.gone, true); assert.equal(warned.length, 1);
}
// 5. Taking over a stale room merges the newer disk text into the room instead of overwriting it.
{
  let saved = null;
  const disk = "line one\nline TWO from disk\nline three";
  const { room } = make({ read: async () => ({ text: disk, version: "9", eol: "\n" }), write: async (_d, _p, text) => { saved = text; return { version: "10" }; } });
  room.ytext.doc.t = "line one\nline two\nline three\nline four typed in the room";
  await room.becomeLeader({ stale: true, base: "line one\nline two\nline three", gone: false }); await wait(40);
  assert.ok(room.text().includes("line TWO from disk"), room.text());
  assert.ok(room.text().includes("typed in the room"), room.text());
  assert.equal(saved, room.text());
}
// 6. A stale takeover whose merge cannot be read stays read-only (no blind save).
{
  writes = 0; let n = 0;
  const { room } = make({ read: async () => { if (n++ === 0) return { text: "disk", version: "9", eol: "\n" }; throw err(500); }, write: async () => { writes++; return { version: "1" }; } });
  await room.becomeLeader({ stale: true, base: "other", gone: false }); await wait(30);
  assert.equal(writes, 0); assert.equal(room.savedText, null);
  clearTimeout(room.retryTimer);
}
// 7. The server closed the room (file renamed or deleted in the tree): leader or not, stop writing, leave, forget the room.
for (const leader of [true, false]) {
  writes = 0;
  const { room, warned, sent } = make({ read: async () => ({ text: "room text", version: "1", eol: "\n" }), write: async () => { writes++; return { version: "2" }; } });
  room.collab.rooms.set(room.rid, room);
  if (leader) await room.becomeLeader(); else room.leader = "someone else";
  room.touch(); room.onClosed(); await wait(30);
  assert.equal(writes, 0); assert.equal(room.canEdit, false); assert.equal(warned.length, 1);
  assert.equal(sent.filter((m) => m.type === "y-leave").length, 1);
  assert.equal(room.collab.rooms.has(room.rid), false);
}
// 8. Review anchors (anchors.js, beside collab.js): found again after the text moves, gone when the text is gone.
{
  const { makeAnchor, locate, disjoint } = await import(pathToFileURL(path.join(path.dirname(path.resolve(process.argv[2])), "anchors.js")).href);
  const text = "The fox ran. The fox sat. The dog sat.";
  const at = text.indexOf("fox sat"), a = makeAnchor(text, at, at + 3);
  assert.deepEqual(locate(text, a), { from: at, to: at + 3 });
  const moved = "Intro.\n" + text;
  assert.deepEqual(locate(moved, a), { from: at + 7, to: at + 10 }, "follows an insertion before it");
  const other = text.replace("The fox ran. ", "");   // the first "fox" is gone; the context picks the right one
  assert.deepEqual(locate(other, a), { from: other.indexOf("fox sat"), to: other.indexOf("fox sat") + 3 });
  assert.equal(locate(text.replace("fox sat", "cat sat"), a), null, "quoted text removed");
  assert.equal(locate("completely different", a), null);
  const ins = makeAnchor(text, 12, 12);   // a suggested insertion: placed by its context alone
  assert.deepEqual(locate("Hello. " + text, ins), { from: 19, to: 19 });
  const long = makeAnchor(text, 0, 11);
  assert.deepEqual(locate("x" + text, long), { from: 1, to: 12 }, "a long quote needs no context");
  const { ok, clash } = disjoint([{ from: 5, to: 8 }, { from: 0, to: 3 }, { from: 6, to: 9 }, { from: 3, to: 3 }]);
  assert.deepEqual(ok.map((e) => e.from), [0, 3, 5]);
  assert.deepEqual(clash.map((e) => e.from), [6]);
}
// 9. Grouped suggestions (one multi-cursor edit): placed whole or not at all, never half applied.
{
  const { makeAnchor, partsOf, placeGroups } = await import(pathToFileURL(path.join(path.dirname(path.resolve(process.argv[2])), "anchors.js")).href);
  const text = "alpha beta gamma delta epsilon zeta";
  const part = (word, insert) => { const at = text.indexOf(word); return { anchor: makeAnchor(text, at, at + word.length), insert }; };
  const group = { id: "g", ...part("alpha", "ALPHA"), more: [part("gamma", "GAMMA"), part("epsilon", "EPSILON")] };
  assert.deepEqual(partsOf(group).map((p) => p.insert), ["ALPHA", "GAMMA", "EPSILON"]);
  assert.deepEqual(partsOf({ id: "s", ...part("beta", "B") }).length, 1);
  const moved = "Intro: " + text;
  let r = placeGroups(moved, [group]);
  assert.deepEqual(r.ok.map((e) => moved.slice(e.from, e.to)), ["alpha", "gamma", "epsilon"], "every part found after the text moved");
  assert.deepEqual(r.failed, []);
  const gone = text.replace("gamma", "GAM");   // one part's text is gone: the whole group stays open
  assert.deepEqual(placeGroups(gone, [group]), { ok: [], failed: [group] });
  const single = { id: "s", ...part("gamma", "G") };   // overlaps the group's second part: the later item fails whole
  r = placeGroups(text, [group, single]);
  assert.deepEqual(r.failed, [single]);
  assert.equal(r.ok.length, 3);
  r = placeGroups(text, [single, group]);
  assert.deepEqual(r.failed, [group]);
  assert.deepEqual(r.ok.map((e) => e.item.id), ["s"]);
  const other = { id: "o", ...part("zeta", "Z") };
  r = placeGroups(text, [group, other]);
  assert.deepEqual(r.ok.map((e) => e.from), [...r.ok.map((e) => e.from)].sort((a, b) => a - b), "document order");
  assert.equal(r.ok.length, 4);
}
// 10. hold(): while an IME composition is in suggest mode, nothing goes out or to disk and remote edits wait.
{
  writes = 0;
  const { room } = make({ read: async () => ({ text: "room text", version: "1", eol: "\n" }), write: async () => { writes++; return { version: "2" }; } });
  await room.becomeLeader();
  room.ready = true;
  room.hold(true);
  room.ytext.doc.t = "room text composing";
  room.touch(); await wait(30);
  assert.equal(writes, 0, "saved while composing");
  await room.save();
  assert.equal(writes, 0);
  room.onRemote("AAAA");
  assert.equal(room.held.remote.length, 1, "remote update applied during a composition");
  room.ytext.doc.t = "room text";   // the composition was taken out again
  room.hold(false);
  assert.equal(room.held, null);
  room.ytext.doc.t = "room text, typed later";
  room.touch(); await wait(30);
  assert.equal(writes, 1, "saving resumes after the composition");
}
console.log("collab ok");
