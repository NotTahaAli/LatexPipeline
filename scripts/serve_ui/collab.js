// Real-time co-editing. Every open file is a Yjs document that this client keeps in sync with the others through
// the server, which only relays and stores the binary updates (base64 on the message bus). One member per file,
// the leader, writes the text to disk through the ordinary PUT API; the leader also folds edits made to the file
// outside the editor into the shared document.
import { Y, awarenessProtocol as AP } from "./libs.js";

const REMOTE = "remote";
const enc = (u8) => { let s = ""; for (let i = 0; i < u8.length; i += 0x8000) s += String.fromCharCode.apply(null, u8.subarray(i, i + 0x8000)); return btoa(s); };
const dec = (str) => { const s = atob(str), u = new Uint8Array(s.length); for (let i = 0; i < s.length; i++) u[i] = s.charCodeAt(i); return u; };

export const PALETTE = ["#d73a49", "#e36209", "#b08800", "#22863a", "#0e8a8a", "#0969da", "#6f42c1", "#c4157f"];

/** The one contiguous span in which a and b differ: replace a[from, to) with insert to get b. */
export function hunk(a, b) {
  const room = Math.min(a.length, b.length);
  let i = 0;
  while (i < room && a[i] === b[i]) i++;
  let j = 0;
  while (j < room - i && a[a.length - 1 - j] === b[b.length - 1 - j]) j++;
  return { from: i, to: a.length - j, insert: b.slice(i, b.length - j) };
}

/**
 * Apply the change base -> target to a Y.Text that may have moved on since base. The span is found again by
 * its surrounding text; if it is gone or ambiguous nothing is changed and false is returned.
 */
export function mergeInto(ytext, base, target) {
  const h = hunk(base, target), cur = ytext.toString();
  let at = h.from;
  if (cur !== base) {
    const before = base.slice(Math.max(0, h.from - 24), h.from), after = base.slice(h.to, h.to + 24);
    const needle = before + base.slice(h.from, h.to) + after, k = cur.indexOf(needle);
    if (k < 0 || cur.indexOf(needle, k + 1) >= 0) return false;
    at = k + before.length;
  }
  ytext.doc.transact(() => { if (h.to > h.from) ytext.delete(at, h.to - h.from); if (h.insert) ytext.insert(at, h.insert); }, "external");
  return true;
}

export class Collab {
  /** user() -> {name, color}, doc() -> the document being viewed; callbacks: onChange(room), onRebind(room), onWarn(text) */
  constructor(channel, api, { user, doc, role, saveDelay, onChange, onRebind, onWarn, onPresence }) {
    Object.assign(this, { channel, api, user, doc, role, saveDelay, onChange, onRebind, onWarn, onPresence });
    this.rooms = new Map();
    this.down = false;
    channel.on("y-state", (d) => this.rooms.get(d.room)?.onState(d));
    channel.on("y-update", (d) => { if (d.cid !== channel.cid) this.rooms.get(d.room)?.onRemote(d.u); });
    channel.on("y-aware", (d) => { if (d.cid !== channel.cid) this.rooms.get(d.room)?.onAware(d.u); });
    channel.on("y-gone", (d) => this.rooms.get(d.room)?.onGone(d.aid));
    channel.on("y-closed", (d) => this.rooms.get(d.room)?.onClosed());
    channel.on("y-leader", (d) => this.rooms.get(d.room)?.setLeader(d.leader, d));
    channel.on("presence", (d) => this.onPresence?.(d.users));
    channel.on("error", (d) => { if (d?.error) this.onWarn?.(d.error); });
    channel.on("transport", (mode) => {
      if (mode === "reconnecting") this.down = true;
      else if (this.down && (mode === "ws" || mode === "poll")) { this.down = false; this.rejoin(); }
      if (mode === "ws" || mode === "poll") this.hello();
    });
    // A gap in the bus (long offline, server restarted): the log can no longer be replayed, so ask again.
    channel.on("state", (_d, msg) => { if (msg.resync && this.rooms.size) this.rejoin(); });
  }

  hello(path) {
    if (path !== undefined) this.path = path;
    const u = this.user();
    this.channel.send({ type: "hello", topic: "y", data: { name: u.name, color: u.color, path: this.path || null, doc: this.doc?.() ?? null } });
  }

  /** Tab closing: tell the server so the others see us leave at once (a beacon survives pagehide). */
  bye() {
    try { navigator.sendBeacon(`api/send?cid=${this.channel.cid}`, new Blob([JSON.stringify({ messages: [{ type: "bye" }] })], { type: "application/json" })); } catch { /* the timeout covers it */ }
  }

  rejoin() { for (const room of this.rooms.values()) if (!room.gone) room.sendJoin(); }

  async open(doc, path) {
    const old = this.rooms.get(`${doc}\n${path}`);
    if (old) return old;
    const room = new Room(this, doc, path);
    this.rooms.set(room.rid, room);
    try { await room.join(); } catch (e) { this.rooms.delete(room.rid); room.destroy(); throw e; }
    return room;
  }
}

export class Room {
  constructor(collab, doc, path) {
    this.collab = collab; this.doc = doc; this.path = path; this.rid = `${doc}\n${path}`;
    this.epoch = null; this.leader = null; this.ready = false; this.buffer = [];
    this.version = null; this.eol = "\n"; this.savedText = null; this.saving = false; this.error = null;
    this.canEdit = collab.role !== "view";
    this.fresh();
  }

  get cid() { return this.collab.channel.cid; }
  get isLeader() { return this.leader === this.cid && this.canEdit; }
  get dirty() { return this.isLeader && this.savedText !== null && this.text() !== this.savedText; }
  text() { return this.ytext.toString(); }

  fresh() {
    this.ydoc = new Y.Doc();
    this.ytext = this.ydoc.getText("t");
    this.awareness = new AP.Awareness(this.ydoc);
    this.undo = new Y.UndoManager(this.ytext);
    const u = this.collab.user();
    this.awareness.setLocalStateField("user", { name: u.name, color: u.color, colorLight: u.color + "33" });
    this.ydoc.on("update", (update, origin) => {
      if (origin !== REMOTE && this.canEdit) this.collab.channel.send({ type: "y-update", topic: "y", data: { room: this.rid, u: enc(update) } });
      this.touch();
    });
    this.awareness.on("update", (_c, origin) => {
      if (origin === REMOTE) return;
      clearTimeout(this.awareTimer);
      this.awareTimer = setTimeout(() => this.collab.channel.send({ type: "y-aware", topic: "y", data: { room: this.rid, u: enc(AP.encodeAwarenessUpdate(this.awareness, [this.awareness.clientID])) } }), 60);
    });
  }

  // ---- joining ----------------------------------------------------------------------------------------------
  join() {
    return new Promise((resolve, reject) => {
      const timer = setTimeout(() => reject(new Error("The server did not answer the join request.")), 8000);
      this.joined = () => { clearTimeout(timer); resolve(); };
      this.sendJoin();
    });
  }

  sendJoin() {
    this.ready = false;
    this.collab.channel.send({ type: "y-join", topic: "y", data: { doc: this.doc, path: this.path, aid: this.awareness.clientID, epoch: this.epoch } });
  }

  hasState() { return this.ydoc.store.clients.size > 0; }

  onState(d) {
    if (d.conflict) return this.reset(d);   // The room was rebuilt from disk while we were away.
    this.epoch = d.epoch;
    if (d.eol) this.eol = d.eol;
    const server = new Y.Doc();
    for (const u of d.updates) { const bytes = dec(u); Y.applyUpdate(server, bytes); Y.applyUpdate(this.ydoc, bytes, REMOTE); }
    if (!d.updates.length && d.seed != null && !this.hasState()) {
      // Every first joiner builds the identical seed (fixed client id), so racing joiners cannot duplicate the text.
      const seed = new Y.Doc();
      seed.clientID = 1;
      seed.getText("t").insert(0, d.seed);
      Y.applyUpdate(this.ydoc, Y.encodeStateAsUpdate(seed), REMOTE);
    }
    for (const u of Object.values(d.aware || {})) AP.applyAwarenessUpdate(this.awareness, dec(u), REMOTE);
    // Whatever the server lacks (our seed, edits made offline) goes up as one update.
    const missing = Y.encodeStateAsUpdate(this.ydoc, Y.encodeStateVector(server));
    server.destroy();
    if (this.canEdit && missing.length > 2) this.collab.channel.send({ type: "y-update", topic: "y", data: { room: this.rid, u: enc(missing) } });
    this.collab.channel.send({ type: "y-aware", topic: "y", data: { room: this.rid, u: enc(AP.encodeAwarenessUpdate(this.awareness, [this.awareness.clientID])) } });
    this.ready = true;
    for (const u of this.buffer.splice(0)) Y.applyUpdate(this.ydoc, dec(u), REMOTE);
    this.setLeader(d.leader, d);
    this.joined?.(); this.joined = null;
  }

  /** Another lineage owns the room now: rebuild on top of it and carry our unsaved edits over. */
  reset(d) {
    const mine = this.text(), base = this.savedText ?? mine;
    this.destroy(true);
    this.epoch = null;
    this.fresh();
    this.onState({ ...d, conflict: false });
    if (mine !== base && !mergeInto(this.ytext, base, mine)) this.collab.onWarn?.(`${this.path}: your offline edits could not be merged and were dropped.`);
    this.collab.onRebind?.(this);
  }

  onRemote(u) { if (this.ready) Y.applyUpdate(this.ydoc, dec(u), REMOTE); else this.buffer.push(u); }
  onAware(u) { AP.applyAwarenessUpdate(this.awareness, dec(u), REMOTE); }
  onGone(aid) { if (aid != null) AP.removeAwarenessStates(this.awareness, [aid], REMOTE); }

  // ---- leader: disk ---------------------------------------------------------------------------------------------
  /** info: {stale, base, gone} from the server when the file changed on disk while the room had no leader. */
  setLeader(cid, info) {
    const was = this.isLeader;
    this.leader = cid;
    if (this.isLeader && !was) this.becomeLeader(info);
    this.collab.onChange?.(this);
  }

  /**
   * Take over writing. Nothing is saved until the file has been read: a failed read (anything but "missing") must not
   * turn into a save without a base version, and a file that changed behind the room's back is merged first.
   */
  async becomeLeader(info) {
    clearTimeout(this.retryTimer);
    const retry = (why) => {
      this.savedText = null; this.error = `Cannot read ${this.path} (${why}); retrying.`;
      this.retryTimer = setTimeout(() => { if (this.isLeader && this.savedText === null) this.becomeLeader(info); }, 3000);
      this.collab.onChange?.(this);
    };
    let f = null;
    try { f = await this.collab.api.read(this.doc, this.path); }
    catch (e) {
      if (e.status !== 404) return retry(e.message);
      if (info?.gone) return this.drop(`${this.path} was deleted on disk; shared editing of it stopped.`);
    }
    this.error = null;
    if (f) {
      this.version = f.version; this.eol = f.eol; this.savedText = f.text;
      if (info?.stale && info.base != null && info.base !== f.text) {
        this.savedText = info.base;
        try { await this.mergeDisk(); } catch (e) { return retry(e.message); }
      }
    } else { this.savedText = ""; this.version = null; }   // Missing file: the first save creates it.
    this.collab.onChange?.(this);
    if (this.dirty) this.scheduleSave();
  }

  /** The file is gone from disk: stop writing (never recreate it behind the user's back) and leave the room. */
  drop(why) {
    clearTimeout(this.saveTimer); clearTimeout(this.retryTimer);
    this.gone = true; this.canEdit = false; this.savedText = null; this.error = why;
    this.collab.channel.send({ type: "y-leave", topic: "y", data: { room: this.rid } });
    this.collab.onWarn?.(why);
    this.collab.onChange?.(this);
  }

  /** The server closed the room because the file was renamed or deleted in the file tree: stop, whoever leads. */
  onClosed() {
    this.drop(`${this.path} was renamed or deleted; shared editing of it stopped.`);
    this.collab.rooms.delete(this.rid);   // A file created under this name later starts a fresh room.
  }

  touch() {
    if (this.isLeader && this.savedText !== null) this.scheduleSave();
    this.collab.onChange?.(this);
  }

  scheduleSave(delay) { clearTimeout(this.saveTimer); this.saveTimer = setTimeout(() => this.save(), delay ?? this.collab.saveDelay()); }

  async save(opts = {}) {
    clearTimeout(this.saveTimer);
    if (!this.isLeader || this.saving || this.savedText === null) return;
    const text = this.text();
    if (text === this.savedText) return;
    this.saving = true; this.error = null; this.collab.onChange?.(this);
    try {
      const res = await this.collab.api.write(this.doc, this.path, text, this.version, this.eol, opts.keepalive, this.cid);
      this.version = res.version; this.savedText = text;
    } catch (e) {
      if (e.status === 409) await this.mergeDisk().catch(() => {});
      else this.error = e.message;
    } finally {
      this.saving = false;
      if (this.dirty && !this.error) this.scheduleSave(this.collab.saveDelay());   // Typed during the request, or merged.
      this.collab.onChange?.(this);
    }
  }

  /** The file changed on disk behind our back: fold the difference into the shared text. */
  async mergeDisk() {
    const f = await this.collab.api.read(this.doc, this.path);
    const base = this.savedText ?? f.text;
    if (f.text !== base && f.text !== this.text() && !mergeInto(this.ytext, base, f.text)) {
      this.collab.onWarn?.(`${this.path} changed on disk and the change could not be merged; your version will replace it.`);
    }
    this.savedText = f.text; this.version = f.version; this.eol = f.eol;
  }

  async onFs(removed) {
    if (!this.isLeader || this.saving || this.savedText === null) return;
    if (removed) return this.drop(`${this.path} was deleted on disk; shared editing of it stopped.`);
    try {
      const f = await this.collab.api.read(this.doc, this.path);
      if (f.version === this.version || f.text === this.savedText) { this.version = f.version; return; }
      await this.mergeDisk();
      if (this.dirty) this.scheduleSave(0);
      this.collab.onChange?.(this);
    } catch { /* gone again */ }
  }

  // ---- leaving -----------------------------------------------------------------------------------------------------
  async leave() {
    if (this.isLeader) await this.save();
    this.collab.channel.send({ type: "y-leave", topic: "y", data: { room: this.rid } });
    this.collab.rooms.delete(this.rid);
    this.destroy();
  }

  destroy(keepRegistered) {
    clearTimeout(this.saveTimer); clearTimeout(this.retryTimer);
    this.awareness.destroy(); this.undo.destroy(); this.ydoc.destroy();
    clearTimeout(this.awareTimer);
    if (!keepRegistered) this.collab.rooms.delete(this.rid);
  }
}
