// Team features of the editor, beside review.js: locking a document, asking for more access (and, for the owner,
// granting it), linked files, copying files and uploading folders, comments started on the PDF, comparing a version
// with now as a latexdiff PDF, downloading a version as a zip, and crash recovery of unsaved text. The server checks
// everything again (serve.py check_permission, check_writable; team.py; host.py for hosted access requests).
// Every bit of user text is rendered with textContent.

const DB = "latex-studio-recovery", STORE = "buffers";
const KEEP_DELAY = 800;          // ms after the last keystroke before unsaved text is kept in IndexedDB
const ACCESS_POLL = 10000;       // ms between "was my request granted?" checks
const KEEP_DAYS = 30;            // unsaved text older than this is dropped, never offered

/**
 * ctx = {el, icon, api, S, doc(), role, config, me(), toast(...nodes), live(msg), view(), activePath(), openFile(path, line, opts),
 *        startComment(), fsDialog(opts), files(), loadFiles(), treeTarget(), pdfPoint(event) -> {page, x, y} | null,
 *        shareOpen() -> bool, refreshShare(), openShare()}
 */
export function teamSupport(ctx) {
  const { el, S } = ctx;
  const say = (text) => ctx.toast(el("span", { textContent: text }));
  const canEdit = ctx.role === "owner" || ctx.role === "edit";
  let locked = null, linked = {};

  // ---- read-only while locked, and for linked files ------------------------------------------------------------
  const roC = new S.Compartment();
  const readOnly = (path) => !!locked || !!(path && linked[path]);
  const extension = (tab) => roC.of(S.EditorState.readOnly.of(readOnly(tab.path)));
  function refresh() { ctx.view()?.dispatch({ effects: roC.reconfigure(S.EditorState.readOnly.of(readOnly(ctx.activePath()))) }); }

  const chip = el("button", { type: "button", className: "role locked", id: "lockChip", hidden: true });
  function setLocked(info) {
    if (JSON.stringify(info || null) === JSON.stringify(locked)) return;
    locked = info || null;
    chip.hidden = !locked;
    chip.replaceChildren(ctx.icon("lock"), el("span", { textContent: "Locked" }));
    chip.title = locked ? `Locked by ${locked.by}${locked.time ? " on " + new Date(locked.time * 1000).toLocaleString() : ""}: read-only for everyone.` + (ctx.config.lock ? " Click to unlock." : "") : "";
    chip.disabled = !ctx.config.lock;
    refresh();
  }
  async function toggleLock() {
    if (!ctx.config.lock) return;
    const on = !locked;
    if (!confirm(on ? "Lock this document? Everyone, you included, can only read it until it is unlocked." : "Unlock this document so it can be edited again?")) return;
    try { setLocked((await ctx.api.lock(ctx.doc(), on)).locked); ctx.live(on ? "Document locked" : "Document unlocked"); } catch (e) { say(e.message); }
  }
  chip.onclick = toggleLock;

  // ---- a small dialog in the style of the others ------------------------------------------------------------------
  function dialog(title, body, okLabel, run) {
    const id = "tdlg-" + Math.random().toString(36).slice(2, 8), err = el("p", { className: "err", role: "alert", hidden: true });
    const ok = el("button", { type: "submit", className: "btn primary", textContent: okLabel });
    const close = el("button", { type: "button", className: "icon", onclick: () => dlg.close() }, ctx.icon("x"));
    close.setAttribute("aria-label", "Close");
    const form = el("form", { method: "dialog" }, el("div", { className: "dlg-head" }, el("h2", { id, textContent: title }), close),
      el("div", { className: "share-body" }, ...body, err, el("div", { className: "actions" }, el("button", { type: "button", className: "btn", textContent: "Cancel", onclick: () => dlg.close() }), ok)));
    const dlg = el("dialog", {}, form);
    dlg.setAttribute("aria-labelledby", id);
    form.onsubmit = async (e) => {
      e.preventDefault(); ok.disabled = true;
      try { await run(); dlg.close(); } catch (x) { err.textContent = x.message; err.hidden = false; } finally { ok.disabled = false; }
    };
    dlg.addEventListener("close", () => dlg.remove());
    document.body.append(dlg);
    dlg.showModal();
    dlg.querySelector("select, input, textarea")?.focus();
  }

  // ---- asking for more access (view and review links; hosted, viewers and reviewers) -----------------------------
  const accessBtn = el("button", { type: "button", className: "btn quiet", id: "accessBtn", hidden: ctx.role !== "view" && ctx.role !== "review" });
  accessBtn.title = "Ask the owner to let you edit or review";
  const accessLabel = (text) => { accessBtn.replaceChildren(ctx.icon("edit"), el("span", { className: "lbl", textContent: text })); accessBtn.setAttribute("aria-label", text); };
  accessLabel("Request access");
  const ticketKey = () => "access:" + ctx.doc();
  let pollTimer;
  async function poll() {
    clearTimeout(pollTimer);
    const ticket = ctx.config.hosted ? null : localStorage.getItem(ticketKey());
    if (!ctx.config.hosted && !ticket) return;
    let r;
    try { r = await ctx.api.accessStatus(ctx.doc(), ticket); } catch (e) { if (e.status === 404) localStorage.removeItem(ticketKey()); return; }
    accessLabel(r.state === "pending" ? "Access requested" : "Request access");
    if (r.state === "granted" && r.url) {
      localStorage.removeItem(ticketKey());
      ctx.toast(el("span", { textContent: "The owner granted your request. " }), el("a", { href: r.url, textContent: "Open your new link" }));
      return;
    }
    if (r.state === "denied") { localStorage.removeItem(ticketKey()); say("The owner did not grant your request."); return; }
    if (r.state === "pending") pollTimer = setTimeout(poll, ACCESS_POLL);
  }
  accessBtn.onclick = () => {
    const role = el("select", { id: "accRole" }, ...(ctx.role === "view" ? [new Option("Edit", "edit"), new Option("Review (comment and suggest)", "review")] : [new Option("Edit", "edit")]));
    const note = el("textarea", { id: "accNote", rows: 3, maxLength: 300 });
    dialog("Request access", [el("p", { textContent: ctx.config.hosted ? "The workspace admins see your request on the members page and can change your role." : "The owner sees your request in the Share dialog and can send you a link with that role." }),
      el("label", { htmlFor: "accRole", textContent: "Role" }), role, el("label", { htmlFor: "accNote", textContent: "Note (optional)" }), note], "Send request", async () => {
      const r = await ctx.api.accessRequest(ctx.doc(), { role: role.value, note: note.value, name: ctx.me().name });
      if (r.ticket) { try { localStorage.setItem(ticketKey(), r.ticket); } catch { /* this tab still polls once */ } }
      accessLabel("Access requested");
      ctx.live("Request sent");
      pollTimer = setTimeout(poll, ACCESS_POLL);
    });
  };

  /** The owner's part of the Share dialog: open requests, each granted with a named link or denied. */
  function shareSection() {
    const list = el("ul", { className: "plist named-list" });
    const box = el("section", { className: "named-links", hidden: true }, el("h3", { textContent: "Access requests" }), list);
    const draw = (requests) => {
      box.hidden = !requests.length;
      list.replaceChildren(...requests.map((r) => el("li", {}, el("div", { className: "link-row" },
        el("span", { textContent: `${r.name} asks to ${r.want === "edit" ? "edit" : "review"} (now ${r.have === "view" ? "view only" : "reviewer"})${r.note ? ": " + r.note : ""}` }),
        el("button", { type: "button", className: "btn", textContent: "Grant", title: "Make a named link with this role; their browser opens it", onclick: () => decide(r, true) }),
        el("button", { type: "button", className: "btn ghost", textContent: "Deny", onclick: () => decide(r, false) })))));
    };
    async function decide(r, grant) {
      try { draw((await ctx.api.accessDecide(r.id, grant)).requests); if (grant) ctx.refreshShare(); } catch (e) { say(e.message); }
    }
    ctx.api.accessList().then((r) => draw(r.requests), () => {});
    return box;
  }
  function onAccess() {   // the "access" bus message: only the owner hears it (visible() drops it for everyone else)
    if (ctx.shareOpen()) ctx.refreshShare();
    else ctx.toast(el("span", { textContent: "Someone asks for more access. " }), el("button", { type: "button", className: "link", textContent: "Open Share", onclick: () => ctx.openShare() }));
  }

  // ---- files: copy, upload a folder, linked files -------------------------------------------------------------------
  const copyName = (path) => path.replace(/(\.[^./]+)?$/, (ext) => "-copy" + ext);
  function copyTarget(path = ctx.treeTarget()) {
    if (!path) return;
    ctx.fsDialog({ title: "Copy", label: "Copy to", value: copyName(path), ok: "Copy", run: async (to) => { await ctx.api.fs(ctx.doc(), "copy", path, to); await ctx.loadFiles(); ctx.live(`Copied to ${to}`); } });
  }
  /** Files of a dropped folder (DataTransferItems) or a folder picker (FileList with webkitRelativePath): [{path, file}]. */
  async function walk(entry, prefix, out) {
    if (out.length > 500) return;
    if (entry.isFile) out.push({ path: prefix + entry.name, file: await new Promise((ok, no) => entry.file(ok, no)) });
    else if (entry.isDirectory) {
      const reader = entry.createReader();
      for (let batch; (batch = await new Promise((ok, no) => reader.readEntries(ok, no))).length;) for (const e of batch) await walk(e, prefix + entry.name + "/", out);
    }
  }
  async function uploadAll(list, base) {
    let done = 0;
    const failed = [];
    for (const { path, file } of list) {
      if (path.split("/").some((p) => p.startsWith("."))) continue;   // .git, .DS_Store: never sent
      try { await ctx.api.uploadTo(ctx.doc(), file, (base ? base + "/" : "") + path); done++; } catch (e) { failed.push(`${path}: ${e.message}`); }
    }
    await ctx.loadFiles();
    say(`Uploaded ${done} ${done === 1 ? "file" : "files"}.` + (failed.length ? ` ${failed.length} not: ${failed.slice(0, 3).join("; ")}` : ""));
  }
  const baseFolder = () => { const t = ctx.treeTarget(); return t && !ctx.files().some((f) => f.path === t) ? t : ""; };
  const picker = el("input", { type: "file", hidden: true, multiple: true, onchange: () => { uploadAll([...picker.files].map((file) => ({ path: file.webkitRelativePath || file.name, file })), baseFolder()); picker.value = ""; } });
  picker.webkitdirectory = true;
  function dropFolder(panel) {
    panel.addEventListener("dragover", (e) => { if (canEdit && !locked && [...(e.dataTransfer?.items || [])].some((i) => i.kind === "file")) { e.preventDefault(); panel.classList.add("drop"); } });
    panel.addEventListener("dragleave", () => panel.classList.remove("drop"));
    panel.addEventListener("drop", async (e) => {
      panel.classList.remove("drop");
      const entries = [...(e.dataTransfer?.items || [])].map((i) => i.webkitGetAsEntry?.()).filter(Boolean);
      if (!entries.length || !canEdit) return;
      e.preventDefault();
      const out = [];
      for (const entry of entries) await walk(entry, "", out);
      uploadAll(out, baseFolder());
    });
  }
  function linkFile() {
    if (ctx.role !== "owner") return;
    const src = el("input", { id: "lnkSrc", type: "text", placeholder: "https://example.org/refs.bib  or  other-doc:refs.bib", autocomplete: "off", spellcheck: false });
    const to = el("input", { id: "lnkTo", type: "text", autocomplete: "off", spellcheck: false, value: baseFolder() ? baseFolder() + "/" : "" });
    src.addEventListener("change", () => { if (!to.value || to.value.endsWith("/")) to.value += src.value.split(/[/:]/).pop(); });
    dialog("Link a file", [el("p", { className: "mute", textContent: "A linked file is a read-only copy of a file from an https:// address or another document of this editor. Refresh it to fetch it again. Text files and png, jpg or pdf only." }),
      el("label", { htmlFor: "lnkSrc", textContent: "Source" }), src, el("label", { htmlFor: "lnkTo", textContent: "Save as" }), to], "Link", async () => {
      const s = src.value.trim(), body = /^https:\/\//i.test(s) ? { url: s } : { doc: s.slice(0, s.lastIndexOf(":")), from: s.slice(s.lastIndexOf(":") + 1) };
      await ctx.api.linked(ctx.doc(), { op: "add", path: to.value.trim(), ...body });
      await ctx.loadFiles();
    });
  }
  async function refreshLinked(path = ctx.treeTarget()) {
    if (!linked[path]) { say("Select a linked file first."); return; }
    try { await ctx.api.linked(ctx.doc(), { op: "refresh", path }); ctx.live(`${path} refreshed`); await ctx.loadFiles(); } catch (e) { say(e.message); }
  }
  async function unlink(path = ctx.treeTarget()) {
    if (!linked[path] || !confirm(`Make ${path} an ordinary file? It is no longer refreshed from its source.`)) return;
    try { await ctx.api.linked(ctx.doc(), { op: "remove", path }); await ctx.loadFiles(); } catch (e) { say(e.message); }
  }
  /** Tree tools: copy, upload a folder, link (owner), refresh a linked file. */
  function treeTools(bar) {
    const tool = (name, label, onclick) => { const b = el("button", { type: "button", className: "icon", title: label, onclick }, ctx.icon(name)); b.setAttribute("aria-label", label); return b; };
    const tools = [tool("files", "Copy file or folder", () => copyTarget()), tool("folder", "Upload a folder (or drop one on the file list)", () => picker.click())];
    if (ctx.role === "owner") tools.push(tool("jump", "Link a file from a URL or another document", linkFile));
    tools.push(tool("refresh", "Refresh the selected linked file", () => refreshLinked()));
    for (const t of tools) t.disabled = !canEdit;
    bar.append(...tools, picker);
  }
  /** Mark linked files in the tree (after each render). */
  function decorateTree(tree) {
    for (const row of tree.querySelectorAll(".row[data-path]")) {
      const src = linked[row.dataset.path];
      if (!src) continue;
      const tag = el("span", { className: "tag linked", textContent: "linked" });
      row.append(tag);
      row.title = `${row.dataset.path}: linked from ${src.url || src.doc + ":" + src.path}, read-only` + (src.fetched ? `, fetched ${new Date(src.fetched * 1000).toLocaleString()}` : "");
    }
  }

  // ---- comment on the PDF: click a point or drag over a region; inverse SyncTeX finds the lines --------------------
  let pdfMode = false;
  function pdfComment(viewer) {
    if (!canEdit && ctx.role !== "review") { say("Commenting needs the edit or review role."); return; }
    pdfMode = !pdfMode;
    viewer.classList.toggle("pick", pdfMode);
    ctx.live(pdfMode ? "Click a point or drag over a region of the PDF to comment on it. Escape cancels." : "Commenting on the PDF cancelled");
  }
  function pdfPicker(viewer) {
    let down = null;
    viewer.addEventListener("pointerdown", (e) => { if (pdfMode && e.button === 0) { down = ctx.pdfPoint(e); e.preventDefault(); } });
    viewer.addEventListener("pointerup", async (e) => {
      if (!pdfMode || !down) return;
      const start = down, end = ctx.pdfPoint(e) || start;
      down = null; pdfMode = false; viewer.classList.remove("pick");
      try {
        const a = await ctx.api.inverse(ctx.doc(), start.page, start.x, start.y);
        const b = end.page === start.page && (Math.abs(end.y - start.y) > 4 || Math.abs(end.x - start.x) > 4) ? await ctx.api.inverse(ctx.doc(), end.page, end.x, end.y) : a;
        if (!a.rel) { say("That part of the PDF comes from a file outside the document."); return; }
        await ctx.openFile(a.rel, a.line);
        const view = ctx.view(), doc = view.state.doc;
        const first = Math.min(a.line, b.rel === a.rel ? b.line : a.line), last = Math.max(a.line, b.rel === a.rel ? b.line : a.line);
        view.dispatch({ selection: { anchor: doc.line(Math.min(first, doc.lines)).from, head: doc.line(Math.min(last, doc.lines)).to } });
        ctx.startComment();
      } catch (x) { say(x.message); }
    });
    document.addEventListener("keydown", (e) => { if (e.key === "Escape" && pdfMode) pdfComment(viewer); });
  }

  // ---- History: download a version, compare it with now (latexdiff) -------------------------------------------------
  function versionActions(v) {
    const zip = el("a", { className: "btn", href: ctx.api.historyZipUrl(ctx.doc(), v.id), download: "", textContent: "Download zip", title: "This version as a zip (text files as they were; images as they are now)" });
    const acts = [zip];
    if (canEdit) {
      const cmp = el("button", { type: "button", className: "btn", textContent: "Compare with now (PDF)", title: "A latexdiff PDF: what changed between this version and now" });
      cmp.onclick = async () => {
        cmp.disabled = true; say("Building the comparison PDF (latexdiff)...");
        try {
          const r = await ctx.api.latexdiff(ctx.doc(), v.id);
          const url = ctx.api.diffPdfUrl(ctx.doc(), r.version);
          ctx.toast(el("span", { textContent: r.ok ? "Comparison ready. " : `The comparison failed: ${r.error} ` }), ...(r.ok ? [el("a", { href: url, target: "_blank", rel: "noopener", textContent: "Open" })] : []));
        } catch (e) { say(e.message); } finally { cmp.disabled = false; }
      };
      acts.push(cmp);
    } else acts.push(el("a", { className: "btn", href: ctx.api.diffPdfUrl(ctx.doc(), Date.now()), target: "_blank", rel: "noopener", textContent: "Last comparison (PDF)" }));
    return acts;
  }

  // ---- crash recovery: unsaved text in IndexedDB, offered back after a crash or reload ------------------------------
  let dbp = null;
  const db = () => dbp || (dbp = new Promise((ok, no) => {
    const req = indexedDB.open(DB, 1);
    req.onupgradeneeded = () => req.result.createObjectStore(STORE);
    req.onsuccess = () => ok(req.result); req.onerror = () => no(req.error);
  }));
  const tx = async (mode, fn) => { const d = await db(); return new Promise((ok, no) => { const t = d.transaction(STORE, mode), r = fn(t.objectStore(STORE)); t.oncomplete = () => ok(r?.result); t.onerror = () => no(t.error); }); };
  const key = (path) => ctx.doc() + "\n" + path;
  const keepTimers = new Map();
  /** After an edit: keep the tab's text while it is unsaved, drop it once saved. */
  function keep(tab, text) {
    clearTimeout(keepTimers.get(tab.path));
    keepTimers.set(tab.path, setTimeout(() => {
      (tab.dirty ? tx("readwrite", (s) => s.put({ text: text(), time: Date.now() }, key(tab.path))) : tx("readwrite", (s) => s.delete(key(tab.path)))).catch(() => {});
    }, KEEP_DELAY));
  }
  /** After a file opens: offer text kept from an earlier session when it differs from what is there now. */
  async function offer(tab, now, apply) {
    let kept;
    try { kept = await tx("readonly", (s) => s.get(key(tab.path))); } catch { return; }
    if (!kept) return;
    if (kept.text === now || Date.now() - kept.time > KEEP_DAYS * 86400000) { tx("readwrite", (s) => s.delete(key(tab.path))).catch(() => {}); return; }
    const drop = () => tx("readwrite", (s) => s.delete(key(tab.path))).catch(() => {});
    ctx.toast(el("span", { textContent: `${tab.path} has unsaved text from ${new Date(kept.time).toLocaleString()} (the tab closed before it was saved). ` }),
      el("button", { type: "button", className: "link", textContent: "Recover it", onclick: () => { apply(kept.text); drop(); ctx.live("Unsaved text recovered"); } }),
      el("span", { textContent: " · " }),
      el("button", { type: "button", className: "link", textContent: "Discard", onclick: drop }));
  }

  return {
    chip, accessBtn, extension, refresh, setLocked, toggleLock, shareSection, treeTools, decorateTree, dropFolder, copyTarget, linkFile,
    refreshLinked, unlink, pdfComment, pdfPicker, versionActions, keep, offer, poll, onAccess,
    get locked() { return locked; },
    set linked(v) { linked = v || {}; refresh(); },
    get linked() { return linked; },
  };
}
