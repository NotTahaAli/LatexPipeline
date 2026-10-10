// Transport layer. Everything the UI knows about the server goes through here; the co-editing layer
// (collab.js) rides on the same message bus.

const enc = encodeURIComponent;
const qs = (o) => new URLSearchParams(o).toString();

async function call(path, opts) {
  const res = await fetch(path, opts);
  let data = null;
  try { data = await res.json(); } catch { /* non-JSON error body */ }
  if (!res.ok) throw Object.assign(new Error(data?.error || res.statusText), { status: res.status, data });
  return data;
}

export const api = {
  files: (doc) => call(`api/files?${qs({ doc })}`),
  read: (doc, path) => call(`api/file?${qs({ doc, path })}`),
  write: (doc, path, text, base, eol, keepalive, cid) => call(`api/file?${qs(cid ? { doc, path, cid } : { doc, path })}`, {
    method: "PUT", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ text, base, eol }), keepalive: !!keepalive,
  }),
  config: () => call("api/config"),
  share: () => call("api/share"),
  shareStart: (provider, doc) => call("api/share", { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ provider, doc }) }),
  shareStop: () => call("api/share/stop", { method: "POST" }),
  shareRegenerate: () => call("api/share/regenerate", { method: "POST" }),
  outline: (doc) => call(`api/outline?${qs({ doc })}`),
  refs: (doc) => call(`api/refs?${qs({ doc })}`),
  lint: (doc) => call(`api/lint?${qs({ doc })}`),
  health: () => call("api/health"),
  rebuild: (doc) => call(`rebuild?${qs({ doc })}`, { method: "POST" }),
  forward: (doc, file, line) => call(`forward?${qs({ doc, file, line, quiet: 1 })}`),
  inverse: (doc, page, x, y) => call(`synctex/edit?${qs({ doc, page, x, y })}`),
  rawUrl: (doc, path) => `api/raw?${qs({ doc, path })}`,
  imageUrl: (doc, name, from) => `api/image?${qs({ doc, name, from })}`,
  pdfUrl: (doc, v) => `pdf/${doc.split("/").map(enc).join("/")}?v=${v}`,
  logUrl: (doc) => `log/${doc.split("/").map(enc).join("/")}`,
};

/**
 * Message bus client. Tries a WebSocket; if it is not open within 3 s (tunnels, proxies)
 * it falls back to long-poll GET + POST. Resumes from the last revision it saw.
 * Messages are {rev, topic, type, data}; binary payloads go as base64 inside data.
 */
export class Channel {
  constructor() {
    this.handlers = new Map();
    this.rev = null;
    this.mode = "connecting";
    this.cid = Math.random().toString(36).slice(2, 10);
    this.lastSeen = 0;
    this.stopped = false;
    this.attempt = 0;
    this.denied = 0;
  }

  on(type, fn) { (this.handlers.get(type) || this.handlers.set(type, []).get(type)).push(fn); return this; }

  emit(msg) {
    this.lastSeen = Date.now();
    if (msg.rev != null && !msg.resync) this.rev = Math.max(this.rev ?? 0, msg.rev);
    if (msg.resync) this.rev = msg.rev;
    for (const fn of this.handlers.get(msg.type) || []) fn(msg.data, msg);
    for (const fn of this.handlers.get("*") || []) fn(msg.data, msg);
  }

  setMode(mode) { this.mode = mode; this.emit({ type: "transport", data: mode }); }

  start() {
    this.pingTimer = setInterval(() => {
      this.send({ type: "ping", topic: "sys", data: Date.now() });
      if (this.mode === "ws" && Date.now() - this.lastSeen > 45000) this.ws?.close();
    }, 20000);
    this.connect();
    return this;
  }

  stop() { this.stopped = true; clearInterval(this.pingTimer); this.ws?.close(); }

  connect() {
    if (this.stopped) return;
    if (this.forcePoll || typeof WebSocket === "undefined") return this.poll();
    const proto = location.protocol === "https:" ? "wss:" : "ws:";
    const base = location.pathname.replace(/[^/]*$/, "");
    const url = `${proto}//${location.host}${base}ws?${qs(this.rev == null ? { cid: this.cid } : { cid: this.cid, since: this.rev })}`;
    let opened = false;
    let ws;
    try { ws = this.ws = new WebSocket(url); } catch { this.forcePoll = true; return this.poll(); }   // Blocked by policy or an extension.
    const timer = setTimeout(() => { if (!opened) { ws.onclose = null; ws.close(); this.forcePoll = true; this.poll(); } }, 3000);
    ws.onopen = () => { opened = true; clearTimeout(timer); this.attempt = 0; this.setMode("ws"); };
    ws.onmessage = (e) => { try { this.emit(JSON.parse(e.data)); } catch { /* ignore */ } };
    ws.onclose = () => {
      clearTimeout(timer);
      if (this.stopped || !opened) return opened ? undefined : (this.forcePoll = true, this.poll());
      this.setMode("reconnecting");
      setTimeout(() => this.connect(), Math.min(1000 * 2 ** this.attempt++, 8000));
    };
  }

  async poll() {
    if (this.polling) return;
    this.polling = true;
    this.setMode("poll");
    while (!this.stopped) {
      try {
        const res = await call(`api/poll?${qs(this.rev == null ? { cid: this.cid } : { cid: this.cid, since: this.rev })}`);
        if (this.mode !== "poll") this.setMode("poll");
        for (const m of res.events) this.emit(m);
        this.attempt = 0; this.denied = 0;
        if (res.rev != null && res.rev > (this.rev ?? -1)) this.rev = res.rev;   // The server may have hidden messages from us.
      } catch (e) {
        if (e.status === 401 && ++this.denied >= 3) { this.stopped = true; this.setMode("revoked"); return; }   // 3 tries: a new cookie may still be in flight
        this.setMode("reconnecting");
        await new Promise((r) => setTimeout(r, Math.min(1000 * 2 ** this.attempt++, 8000)));
      }
    }
  }

  async send(message) {
    if (this.mode === "ws" && this.ws?.readyState === 1) { this.ws.send(JSON.stringify(message)); return true; }
    try {
      const res = await call(`api/send?${qs({ cid: this.cid })}`, {
        method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ messages: [message] }),
      });
      for (const r of res.replies) this.emit(r);
      return true;
    } catch { return false; /* the poll loop reports connectivity; co-editing re-sends what the server lacks on reconnect */ }
  }
}
