import { state as S, view as V, language as L, commands as C, search as SR, autocomplete as AC, highlight as HL, stex, loadVim, loadEmacs, collabLibs } from "./libs.js";
import { Collab, PALETTE, hunk } from "./collab.js";
import { api, Channel } from "./api.js";
import { PdfView } from "./pdf.js";
import { visualField, visualTheme, visualEnv, refresh } from "./visual.js";
import * as prose from "./prose.js";
import { grammarSupport } from "./grammar.js";
import { addBibLookup } from "./bib.js";
import { refsPanel } from "./refs.js";
import { aiPanel } from "./ai.js";
import { reviewSupport } from "./review.js";
import { historyPanel } from "./history.js";
import { LivePreview } from "./preview.js";

const $ = (id) => document.getElementById(id);
const el = (tag, props = {}, ...kids) => { const n = Object.assign(document.createElement(tag), props); n.append(...kids); return n; };
const icon = (name) => { const s = document.createElementNS("http://www.w3.org/2000/svg", "svg"); s.setAttribute("class", "ic"); s.innerHTML = `<use href="#i-${name}"/>`; return s; };
const t0 = performance.now();

// ---- persistence (per-viewer conveniences only) ------------------------------------------
const store = {
  get(k) { try { return localStorage.getItem("lp." + k); } catch { return null; } },
  set(k, v) { try { localStorage.setItem("lp." + k, typeof v === "string" ? v : JSON.stringify(v)); } catch { /* private mode */ } },
  json(k, d) { try { return JSON.parse(localStorage.getItem("lp." + k)) ?? d; } catch { return d; } },
};
const settings = Object.assign({ autosave: 1000, theme: "system", keys: "default", font: 14, zoom: 0, visual: false, inverse: "app", focusAuto: false, live: true, spell: true, grammar: "auto", grammarUrl: "", grammarShare: false }, store.json("settings", {}));
if (settings.fit === undefined) settings.fit = !settings.zoom;   // Fit the pane width until the person picks a zoom.
const saveSettings = () => store.set("settings", settings);

// ---- state ------------------------------------------------------------------------------
let docs = {}, cur = decodeURIComponent(location.hash.slice(1));
const tabs = new Map();      // path -> tab (per-file editor state)
let active = null, lastTex = null, files = [], emptyDirs = [], treeSel = null, refs = { labels: {}, bib: {} }, outlineData = null, lastErrKey = null;
const firstVisit = store.get("ui") === null;
const ui = Object.assign({ side: firstVisit && window.innerWidth >= 1200, sideTab: "files", drawer: false, drawerTab: "problems", split: 50, prose: false }, store.json("ui", {}));
const saveUi = () => store.set("ui", ui);
// Phone widths show one pane, or both stacked (CSS reads data-view on #work).
function setView(v) {
  ui.view = v; saveUi(); $("work").dataset.view = v;
  for (const b of $("viewTabs").children) b.setAttribute("aria-pressed", String(b.dataset.view === v));
}
for (const b of $("viewTabs").children) b.onclick = () => setView(b.dataset.view);
setView(ui.view || "split");
let view;

// ---- who am I, what may I do ---------------------------------------------------------------------
const config = await api.config().catch(() => ({ role: "owner" }));
const role = config.role, readOnly = role === "view";
const me = store.json("user", null) || (() => { const u = { name: "Guest " + (100 + Math.floor(Math.random() * 900)), color: PALETTE[Math.floor(Math.random() * PALETTE.length)] }; store.set("user", u); return u; })();
if (!me.key) { me.key = Array.from(crypto.getRandomValues(new Uint8Array(18)), (b) => b.toString(16).padStart(2, "0")).join(""); store.set("user", me); }   // marks your own review comments on link shares

// ---- toast ------------------------------------------------------------------------------
function toast(...kids) { const t = $("toast"); t.replaceChildren(...kids); t.hidden = false; clearTimeout(t.t); t.t = setTimeout(() => t.hidden = true, 5000); }

// ---- theme / sizes -----------------------------------------------------------------------
function applyAppearance() {
  const root = document.documentElement;
  if (settings.theme === "system") delete root.dataset.theme; else root.dataset.theme = settings.theme;
  root.style.setProperty("--font-size", settings.font + "px");
  root.style.setProperty("--split", ui.split + "%");
  $("splitter").setAttribute("aria-valuenow", Math.round(ui.split));
}

// ---- CodeMirror ---------------------------------------------------------------------------
const { EditorState, Compartment, Prec } = S;
const { EditorView, keymap, lineNumbers, highlightActiveLine, highlightActiveLineGutter, drawSelection, dropCursor } = V;
const GR = grammarSupport(S, V);
// Review comments and suggest mode (review.js); the panel lives in the drawer's Review tab.
const REV = reviewSupport(S, V, {
  api, el, icon, canEdit: !readOnly, doc: () => cur, me: () => me, view: () => view,
  activePath: () => (active?.kind === "text" ? active.path : null), openFile: (p, line, opts) => openFile(p, line, opts),
  showPanel: () => setDrawer(true, "review"), toast: (m) => toast(el("span", { textContent: m })), live: (m) => { $("live").textContent = m; },
  onCount: (n) => { $("reviewCount").textContent = n || ""; }, toggleSuggest: () => toggleSuggest(), shared: () => !!active?.collab,
});
function toggleSuggest() {
  if (readOnly) return;
  REV.setSuggest(!REV.suggesting);
  $("suggestBtn").setAttribute("aria-checked", String(REV.suggesting));
  $("cm").classList.toggle("suggesting", REV.suggesting);
}
const keysC = new Compartment(), visualC = new Compartment(), spellC = new Compartment();

const latexComplete = (ctx) => {
  const ref = ctx.matchBefore(/\\(?:ref|eqref|cref|Cref|autoref|pageref)\{[^}]*/);
  if (ref) return { from: ref.from + ref.text.indexOf("{") + 1, options: Object.entries(refs.labels).map(([label, v]) => ({ label, detail: v.context.slice(0, 50), type: "variable" })) };
  const cite = ctx.matchBefore(/\\cite[a-z]*\*?(?:\[[^\]]*\])*\{[^}]*/);
  if (cite) {
    const from = Math.max(cite.text.lastIndexOf("{"), cite.text.lastIndexOf(",")) + cite.from + 1;
    return { from, options: Object.entries(refs.bib).map(([label, v]) => ({ label, detail: [v.author, v.year].filter(Boolean).join(" ").slice(0, 50), type: "text" })) };
  }
  const env = ctx.matchBefore(/\\begin\{[a-z*]*/);
  if (env) return { from: env.from + 7, options: ["itemize", "enumerate", "equation", "align", "figure", "table", "tabular", "abstract", "document", "center", "verbatim", "theorem", "proof"].map((label) => ({ label, type: "keyword" })) };
  const cmd = ctx.matchBefore(/\\[A-Za-z]*/);
  if (cmd && (cmd.to - cmd.from > 1 || ctx.explicit)) return { from: cmd.from, options: ["section", "subsection", "subsubsection", "textbf", "textit", "emph", "cite", "ref", "label", "begin", "end", "includegraphics", "caption", "footnote", "item", "frac", "sum", "int", "input", "usepackage"].map((c) => ({ label: "\\" + c, type: "function" })) };
  return null;
};

function extensionsFor(tab) {
  const room = tab.collab;
  return [
    keysC.of([]),
    lineNumbers(), highlightActiveLineGutter(), highlightActiveLine(), drawSelection(), dropCursor(),
    room ? collabLibs.yCollab(room.ytext, room.awareness, { undoManager: room.undo }) : C.history(),
    readOnly ? [EditorState.readOnly.of(true), EditorView.domEventHandlers({ keydown: (e) => { if (e.key.length === 1 && !e.ctrlKey && !e.metaKey) nudgeReadOnly(); }, paste: nudgeReadOnly, drop: nudgeReadOnly })] : [],
    readOnly ? [] : EditorView.domEventHandlers({
      paste: (e, v) => dropImages(e.clipboardData?.files, e, v.state.selection.main.head),
      drop: (e, v) => dropImages(e.dataTransfer?.files, e, v.posAtCoords({ x: e.clientX, y: e.clientY }) ?? v.state.selection.main.head),
    }),
    errField,
    readOnly ? [] : GR.extension,
    REV.extension,
    L.bracketMatching(), AC.closeBrackets(),
    AC.autocompletion({ override: [latexComplete], icons: false }),
    EditorState.allowMultipleSelections.of(true),
    L.StreamLanguage.define(stex),
    L.syntaxHighlighting(HL.classHighlighter),
    EditorView.lineWrapping,
    EditorView.contentAttributes.of({ "aria-label": `Editor: ${tab.path}`, tabindex: "0" }),
    spellC.of(spellAttr()),
    keymap.of([
      { key: "Mod-s", run: () => { saveTab(active); return true; }, preventDefault: true },
      // Shared undo changes the Yjs text directly, past suggest mode's filter: not while suggesting.
      ...(room ? ["Mod-z", "Mod-y", "Mod-Shift-z"].map((key) => ({ key, run: () => REV.suggesting && (toast(el("span", { textContent: "Undo is off in suggest mode: withdraw the suggestion in the Review tab instead." })), true) })) : []),
      ...AC.closeBracketsKeymap, ...C.defaultKeymap, ...SR.searchKeymap, ...(room ? collabLibs.yUndoManagerKeymap : C.historyKeymap), ...AC.completionKeymap,
    ]),
    SR.search({ top: true }),
    visualC.of([]),
    visualTheme,
    EditorView.updateListener.of(onUpdate),
  ];
}

// Spell check (the browser's own, no dictionary shipped) only where the text is prose: visual mode, and the paragraph panel.
// In source mode every line is LaTeX, so it stays off; in visual mode commands and keys are marked spellcheck=false.
const spellAttr = () => EditorView.contentAttributes.of({ spellcheck: settings.spell && settings.visual ? "true" : "false" });
function applySpell() {
  view?.dispatch({ effects: spellC.reconfigure(spellAttr()) });
  $("proseBody").spellcheck = !!settings.spell;
}

function onUpdate(u) {
  if (!active) return;
  if (u.docChanged && !active.collab) {   // A collaborative tab's room tracks what still has to reach the disk.
    active.dirty = !u.state.doc.eq(active.savedDoc);
    if (!active.fromDisk) { renderTabs(); showSaveState(); scheduleAutosave(); }
  }
  if (u.docChanged) scheduleGrammar();
  if (u.docChanged && u.transactions.some((t) => t.annotation(S.Transaction.userEvent))) live.edited();   // Own typing, not a co-editor's.
  if (u.docChanged || u.selectionSet) {
    const head = u.state.selection.main.head, line = u.state.doc.lineAt(head);
    $("cursorPos").textContent = `Ln ${line.number}, Col ${head - line.from + 1}`;
    if (ui.prose) syncProse(u.docChanged && u.transactions.some((t) => t.annotation(proseEdit)));
  }
}

const proseEdit = S.Annotation.define();

// ---- figures: drop or paste an image into the editor; it is saved into the document and \includegraphics is inserted ----------
const UPLOAD_OK = /^(image\/(png|jpeg)|application\/pdf)$/;
function dropImages(list, event, pos) {
  const all = [...(list || [])];
  if (!all.length) return false;   // Plain text paste or a text drop: CodeMirror handles it.
  event.preventDefault();
  const good = all.filter((f) => UPLOAD_OK.test(f.type));
  if (good.length < all.length) toast(el("span", { textContent: "Only png, jpg and pdf images can be added (svg and gif cannot be used by LaTeX here)." }));
  uploadImages(good, pos);
  return true;
}
async function uploadImages(fs, pos) {
  const tab = active;
  for (const f of fs) {
    try {
      const name = f.name && f.name !== "image.png" ? f.name : `pasted-${Date.now()}.${f.type === "image/jpeg" ? "jpg" : f.type === "application/pdf" ? "pdf" : "png"}`;
      const { path } = await api.upload(cur, f, name);
      if (active !== tab) { toast(el("span", { textContent: `Saved ${path}.` })); continue; }
      const text = `\\includegraphics[width=0.8\\linewidth]{${path}}\n`;
      pos = Math.min(pos, view.state.doc.length);
      view.dispatch({ changes: { from: pos, insert: text }, selection: { anchor: pos + text.length }, scrollIntoView: true, userEvent: "input.drop" });
      pos += text.length;
      toast(el("span", { textContent: `Saved ${path} and inserted it.` }));
      loadFiles(); if (settings.visual) loadRefs();
    } catch (e) { toast(el("span", { textContent: `${f.name || "Image"}: ${e.message}` })); }
  }
  view.focus();
}

let nudged = 0;
function nudgeReadOnly() {   // Typing into a locked editor must not look like a bug.
  if (Date.now() - nudged < 15000) return;
  nudged = Date.now();
  toast(el("span", { textContent: "This is a view-only link, so the text can't be changed. Ask the host for the edit link." }));
}

// ---- build errors in the source: the failing line is marked where you will look for it -----------------
const setErrors = S.StateEffect.define();
const errField = S.StateField.define({
  create: () => V.Decoration.none,
  update(deco, tr) {
    deco = deco.map(tr.changes);
    for (const e of tr.effects) if (e.is(setErrors)) deco = e.value;
    return deco;
  },
  provide: (f) => EditorView.decorations.from(f),
});
function markErrors() {
  if (!view || !active || active.kind !== "text") return;
  const doc = view.state.doc, seen = new Set(), marks = [];
  for (const e of (docs[cur]?.status === "failed" ? docs[cur].errors : [])) {
    if (e.file !== active.path || seen.has(e.line) || e.line < 1) continue;
    seen.add(e.line);
    marks.push(V.Decoration.line({ class: "cm-errLine", attributes: { title: e.message } }).range(doc.line(Math.min(e.line, doc.lines)).from));
  }
  view.dispatch({ effects: setErrors.of(V.Decoration.set(marks.sort((a, b) => a.from - b.from), true)) });
}

async function applyConfig() {
  if (!view) return;
  let keys = [];
  try {
    if (settings.keys === "vim") keys = (await loadVim()).vim();
    else if (settings.keys === "emacs") keys = (await loadEmacs()).emacs();
  } catch (e) { console.warn("keybindings", e); toast(el("span", { textContent: "Could not load the " + settings.keys + " keybindings (offline?)." })); }
  const env = visualEnv.of({
    refs: () => refs,
    imageUrl: (name) => cur ? api.imageUrl(cur, name, active?.path || "") : null,
  });
  view.dispatch({ effects: [
    keysC.reconfigure(Prec.high(keys)),
    visualC.reconfigure(settings.visual && active?.kind === "text" ? [visualField, env] : []),
    spellC.reconfigure(spellAttr()),
  ] });
  $("proseBody").spellcheck = !!settings.spell;
  $("cm").classList.toggle("visual", !!settings.visual);
  $("visualBtn").setAttribute("aria-checked", settings.visual ? "true" : "false");
}

// ---- files and tabs -------------------------------------------------------------------------
async function openFile(path, line, opts = {}) {
  let tab = tabs.get(path);
  if (!tab) {
    const info = files.find((f) => f.path === path);
    const kind = info?.kind || (/\.(png|jpe?g|gif|svg|webp)$/i.test(path) ? "image" : "text");
    tab = { path, kind, dirty: false, version: null, eol: "\n", savedDoc: null, state: null, conflict: null };
    if (kind === "text") {
      try {
        const f = await api.read(cur, path);
        tab.version = f.version; tab.eol = f.eol;
        const doc = S.Text.of(f.text.split("\n"));
        tab.savedDoc = doc;
        if (collab) {
          try { attachRoom(tab, await collab.open(cur, path)); }
          catch (e) { console.warn("co-editing unavailable for", path, e); toast(el("span", { textContent: "Live co-editing is unavailable; editing a local copy." })); }
        }
        tab.state = EditorState.create({ doc: tab.collab ? S.Text.of(tab.collab.text().split("\n")) : doc, extensions: extensionsFor(tab) });
      } catch (e) { toast(el("span", { textContent: `${path}: ${e.message}` })); return; }
    }
    tabs.set(path, tab);
  }
  await activate(tab, line, opts);
}

// A collaborative tab reads its dirty/saving/error state from the room (only the leader writes to disk).
function attachRoom(tab, room) {
  tab.collab = room;
  for (const key of ["dirty", "saving", "error"]) Object.defineProperty(tab, key, { get: () => room[key], set() { /* the room owns it */ }, configurable: true });
}
function collabState(tab) {
  const len = tab.collab.text().length, head = Math.min(tab.state?.selection.main.head ?? 0, len);
  return EditorState.create({ doc: S.Text.of(tab.collab.text().split("\n")), selection: { anchor: head }, extensions: extensionsFor(tab) });
}

async function activate(tab, line, opts = {}) {
  const same = active === tab;
  if (active && !same && active.kind === "text") { REV.flush(view); active.state = view.state; }
  active = tab;
  if (/\.tex$/i.test(tab.path)) lastTex = tab.path;   // Where the References panel cites into while a .bib is open.
  ui.active = tab.path; persistTabs();
  $("cm").hidden = tab.kind !== "text"; $("imgPreview").hidden = tab.kind !== "image";
  if (tab.kind === "image") {
    $("imgPreview").replaceChildren(el("img", { src: api.rawUrl(cur, tab.path), alt: tab.path }));
  } else {
    if (!same) {   // The live view already holds the active tab's newest state.
      if (tab.collab) tab.state = collabState(tab);   // The shared text moved on while this tab was in the background.
      tab.fromDisk = true;
      view.setState(tab.state);
      tab.fromDisk = false;
      await applyConfig();
    }
    if (line) gotoLine(line);
    if (!opts.noFocus) view.focus();
  }
  renderTabs(); renderTree(); showSaveState(); showBanner(); markErrors(); renderFocus();
  if (tab.kind === "text") REV.refresh(view);
  if (ui.drawer && ui.drawerTab === "history" && !same) historyUi.load();
  collab?.hello(tab.path);
  if (ui.prose) syncProse();
  renderGrammar(); scheduleGrammar(tab.kind === "text" ? 400 : -1);
}

function gotoLine(line) {
  const doc = view.state.doc, ln = doc.line(Math.max(1, Math.min(line, doc.lines)));
  view.dispatch({ selection: { anchor: ln.from }, effects: EditorView.scrollIntoView(ln.from, { y: "center" }) });
}

async function closeTab(tab, force, quiet) {   // force: no unsaved-changes question; quiet: do not open another file afterwards
  if (!force && !tab.collab && tab.dirty && !confirm(`${tab.path} has unsaved changes. Close anyway?`)) return;
  tabs.delete(tab.path);
  const left = tab.collab?.leave();
  if (active === tab) {
    active = null;
    const next = [...tabs.values()].pop();
    if (next) activate(next); else if (!quiet) openFile("main.tex");
  }
  persistTabs(); renderTabs();
  await left;
}

function persistTabs() { store.set("tabs:" + cur, { open: [...tabs.keys()], active: active?.path }); }

function renderTabs() {
  const list = $("tabs");
  list.replaceChildren(...[...tabs.values()].map((tab) => {
    const name = tab.path.split("/").pop();
    // The x is a pointer-only extra (aria-hidden, no role): keyboard users close with Delete or the palette.
    const close = el("span", { className: "close", title: "Close tab", onclick: (e) => { e.stopPropagation(); closeTab(tab); } }, icon("x"));
    close.setAttribute("aria-hidden", "true");
    const node = el("div", { className: "tab" + (tab.dirty ? " dirty" : ""), role: "tab", tabIndex: tab === active ? 0 : -1, title: tab.path + (tab.dirty ? " (unsaved)" : "") + ". Delete closes it.", onclick: () => activate(tab) },
      el("span", { className: "name", textContent: name }), el("span", { className: "u", title: "Unsaved changes" }), close);
    node.setAttribute("aria-selected", tab === active ? "true" : "false");
    node.setAttribute("aria-label", name + (tab.dirty ? ", unsaved changes" : ""));
    node.dataset.path = tab.path;
    node.addEventListener("auxclick", (e) => { if (e.button === 1) { e.preventDefault(); closeTab(tab); } });
    node.addEventListener("keydown", (e) => {
      const all = [...tabs.values()], i = all.indexOf(tab);
      if (e.key === "ArrowRight") activate(all[(i + 1) % all.length], 0, { noFocus: true }).then(() => $("tabs").children[(i + 1) % all.length]?.focus());
      else if (e.key === "ArrowLeft") activate(all[(i - 1 + all.length) % all.length], 0, { noFocus: true }).then(() => $("tabs").children[(i - 1 + all.length) % all.length]?.focus());
      else if (e.key === "Enter" || e.key === " ") { e.preventDefault(); activate(tab); }
      else if (e.key === "Delete") { e.preventDefault(); closeTab(tab); }
    });
    return node;
  }));
}

function showSaveState() {
  const s = $("saveState");
  const tab = active;
  s.className = "";
  if (!tab || tab.kind !== "text") { s.textContent = ""; return; }
  if (readOnly) s.textContent = "View only: you can't edit this document";
  else if (tab.collab && !tab.collab.isLeader) s.textContent = "Live";
  else if (tab.error) { s.textContent = "Save failed: " + tab.error; s.className = "err"; }
  else if (tab.saving) s.textContent = "Saving...";
  else if (tab.dirty) { s.textContent = settings.autosave ? "Unsaved changes" : "Unsaved changes (Ctrl+S)"; s.className = "unsaved"; }
  else s.textContent = "Saved";
}

function showBanner() {
  const b = $("banner"), tab = active;
  if (!tab?.conflict) { b.hidden = true; return; }
  b.hidden = false;
  const reload = el("button", { textContent: "Reload from disk", onclick: () => reloadTab(tab, true) });
  const keep = el("button", { textContent: tab.conflict === "deleted" ? "Keep and recreate on save" : "Keep mine (overwrite on save)", onclick: () => { tab.version = null; tab.conflict = null; showBanner(); saveTab(tab); } });
  b.replaceChildren(el("span", { textContent: tab.conflict === "deleted" ? `${tab.path} was deleted on disk.` : `${tab.path} changed on disk and you have unsaved edits.` }), el("span", { className: "spacer" }), ...(tab.conflict === "deleted" ? [keep] : [reload, keep]));
}

async function reloadTab(tab, announce) {
  try {
    const f = await api.read(cur, tab.path);
    const doc = S.Text.of(f.text.split("\n"));
    tab.version = f.version; tab.eol = f.eol; tab.savedDoc = doc; tab.dirty = false; tab.conflict = null; tab.error = null;
    if (tab === active) {
      const head = Math.min(view.state.selection.main.head, doc.length);
      tab.fromDisk = true;
      view.dispatch({ changes: { from: 0, to: view.state.doc.length, insert: f.text }, selection: { anchor: head }, annotations: REV.bypass.of(true) });
      tab.fromDisk = false;
    } else tab.state = tab.state.update({ changes: { from: 0, to: tab.state.doc.length, insert: f.text } }).state;
    renderTabs(); showSaveState(); showBanner();
    if (announce !== false) toast(el("span", { textContent: `Reloaded ${tab.path} from disk.` }));
  } catch (e) { toast(el("span", { textContent: e.message })); }
}

// ---- saving ----------------------------------------------------------------------------------
let autosaveTimer;
function scheduleAutosave() {
  clearTimeout(autosaveTimer);
  if (settings.autosave > 0) autosaveTimer = setTimeout(() => active?.dirty && saveTab(active), settings.autosave);
}

async function saveTab(tab, opts) {
  if (!tab || tab.kind !== "text" || tab.saving) return;
  if (tab.collab) return tab.collab.save(opts);
  clearTimeout(autosaveTimer);
  const doc = tab === active ? view.state.doc : tab.state.doc;
  if (!tab.dirty && !tab.conflict) return;
  tab.saving = true; tab.error = null; showSaveState();
  try {
    const res = await api.write(cur, tab.path, doc.toString(), tab.version, tab.eol);
    tab.version = res.version; tab.savedDoc = doc; tab.conflict = null;
    tab.dirty = tab === active ? !view.state.doc.eq(doc) : false;   // Typing during the request keeps it dirty.
    if (tab.dirty) scheduleAutosave();
    loadFiles(); loadOutline(); if (settings.visual) loadRefs();
  } catch (e) {
    if (e.status === 409) { tab.conflict = e.data?.deleted ? "deleted" : "changed"; showBanner(); } else tab.error = e.message;
  } finally { tab.saving = false; renderTabs(); showSaveState(); }
}

// External changes arrive on the bus.
let autoFocusTimer;
async function onFsEvent(msg) {
  if (msg.doc !== cur) return;
  loadFiles();
  if (live.last?.meta.overlay === false && msg.changed.includes(active?.path)) live.edited();   // Previewed from disk: again once saved.
  if (settings.focusAuto && !readOnly && isChapter(active) && msg.changed.includes(active.path)) {   // "Build it after each save"
    clearTimeout(autoFocusTimer); autoFocusTimer = setTimeout(() => isChapter(active) && previewChapter(true), 600);
  }
  for (const path of [...msg.changed, ...msg.removed]) {
    const tab = tabs.get(path);
    if (tab?.collab) { tab.collab.onFs(msg.removed.includes(path)); continue; }   // The room's leader folds it into the shared text.
    if (!tab || tab.kind !== "text" || tab.saving) continue;
    if (msg.removed.includes(path)) { tab.conflict = "deleted"; if (tab === active) showBanner(); continue; }
    const info = files.find((f) => f.path === path);
    try {
      const f = await api.read(cur, path);
      if (f.version === tab.version) continue;
      const mine = (tab === active ? view.state.doc : tab.state.doc).toString();
      if (f.text === mine) { tab.version = f.version; tab.savedDoc = S.Text.of(mine.split("\n")); tab.dirty = false; renderTabs(); showSaveState(); }
      else if (!tab.dirty) await reloadTab(tab);
      else { tab.conflict = "changed"; if (tab === active) showBanner(); }
    } catch { /* gone again */ }
    void info;
  }
  if (outlineVisible()) loadOutline();
}

// ---- sidebar: file tree ---------------------------------------------------------------------
const openDirs = new Set(store.json("dirs", [""]));
async function loadFiles() {
  if (!cur) return;
  try { const r = await api.files(cur); files = r.files; emptyDirs = r.dirs || []; } catch { return; }
  if (ui.side) renderTree();
}

function renderTree() {
  const root = {}, out = $("tree");
  for (const f of files) {
    let node = root;
    const parts = f.path.split("/");
    parts.slice(0, -1).forEach((d) => node = (node[d + "/"] ||= {}));
    node[parts.at(-1)] = f;
  }
  for (const d of emptyDirs) d.split("/").reduce((node, part) => (node[part + "/"] ||= {}), root);
  const rows = [];
  const walk = (node, prefix, depth) => {
    const entries = Object.entries(node).sort(([a, x], [b, y]) => (a.endsWith("/") ? 0 : 1) - (b.endsWith("/") ? 0 : 1) || a.localeCompare(b));
    for (const [name, v] of entries) {
      const pad = `${6 + depth * 14}px`;
      if (name.endsWith("/")) {
        const path = prefix + name, open = openDirs.has(path);
        const row = el("button", { className: "row", role: "treeitem", onclick: () => { treeSel = path.slice(0, -1); open ? openDirs.delete(path) : openDirs.add(path); store.set("dirs", [...openDirs]); renderTree(); } }, icon("chev"), icon("folder"), el("span", { className: "name", textContent: name.slice(0, -1) }));
        row.querySelector(".ic").classList.add("chev");
        row.dataset.path = path.slice(0, -1); row.dataset.dir = "1";
        row.style.paddingLeft = pad; row.setAttribute("aria-expanded", String(open)); row.setAttribute("aria-selected", String(treeSel === path.slice(0, -1)));
        rows.push(row);
        if (open) walk(v, path, depth + 1);
      } else {
        const text = v.kind === "text", img = v.kind === "image";
        const row = el("button", { className: "row" + (text || img ? "" : " dim"), role: "treeitem", title: v.path + (text || img ? "" : " (not editable)"), disabled: !(text || img), onclick: () => { treeSel = v.path; if (narrow()) setSide(false); openFile(v.path); } }, el("span", { style: "width:16px" }), icon(img ? "image" : "file"), el("span", { className: "name", textContent: name }));
        row.style.paddingLeft = pad; row.dataset.path = v.path;
        row.setAttribute("aria-selected", String(treeSel ? treeSel === v.path : active?.path === v.path));
        rows.push(row);
      }
    }
  };
  walk(root, "", 0);
  out.replaceChildren(...rows);
  showTreeTools();
}

// ---- file tree actions: new file/folder, rename, delete (owner and edit link; the server checks every path again) ----
const treeTarget = () => treeSel || active?.path || null;
const baseDir = () => { const t = treeTarget(); if (!t) return ""; return files.some((f) => f.path === t) ? t.split("/").slice(0, -1).join("/") : t; };
function showTreeTools() {
  const t = treeTarget(), protectedPath = t === "main.tex";
  const why = readOnly ? "File changes need the edit link" : null;
  for (const [id, label, off] of [["fNewFile", "New file", why], ["fNewDir", "New folder", why],
    ["fRename", "Rename or move (F2)", why || (!t ? "Select a file or folder first" : protectedPath ? "main.tex cannot be renamed" : null)],
    ["fDelete", "Delete (Delete key)", why || (!t ? "Select a file or folder first" : protectedPath ? "main.tex cannot be deleted" : null)]]) {
    $(id).disabled = !!off; $(id).title = off ? `${label}: ${off}` : label;
  }
}
const joinPath = (dir, name) => (dir ? dir + "/" : "") + name;

/** One dialog for naming and confirming. run(value) throws to show its message inside the dialog and keep it open. */
function fsDialog({ title, message = "", label = "", value = "", ok, danger = false, run, after }) {
  const dlg = $("fsDlg"), input = $("fsName");
  $("fsTitle").textContent = title; $("fsMsg").textContent = message; $("fsMsg").hidden = !message;
  $("fsLabel").textContent = label; $("fsLabel").hidden = input.hidden = !label;
  input.value = value;
  $("fsErr").hidden = true;
  $("fsOk").textContent = ok; $("fsOk").classList.toggle("danger", danger); $("fsOk").classList.toggle("primary", !danger);
  dlg.onclose = () => { dlg.onclose = null; };
  $("fsForm").onsubmit = async (e) => {
    e.preventDefault();
    $("fsOk").disabled = true;
    try { await run(input.value.trim()); dlg.close(); after?.(); }
    catch (err) { $("fsErr").textContent = err.message; $("fsErr").hidden = false; input.hidden ? $("fsCancel").focus() : input.focus(); }
    finally { $("fsOk").disabled = false; }
  };
  dlg.showModal();
  if (label) {   // Select the file name, not the folder, so typing renames in place.
    input.focus();
    const at = value.lastIndexOf("/") + 1, dot = value.lastIndexOf(".");
    input.setSelectionRange(at, dot > at ? dot : value.length);
  } else $("fsCancel").focus();   // A destructive confirmation starts on Cancel.
}
$("fsCancel").onclick = $("fsClose").onclick = () => $("fsDlg").close();
$("fsDlg").addEventListener("click", (e) => { if (e.target === $("fsDlg")) $("fsDlg").close(); });

const affected = (path) => [...tabs.values()].filter((t) => t.path === path || t.path.startsWith(path + "/"));
function newFile() {
  if (readOnly) return;
  const dir = baseDir();
  fsDialog({ title: "New file", label: "Path (folders are created as needed)", value: joinPath(dir, "untitled.tex"), ok: "Create", run: async (path) => {
    await api.fs(cur, "newfile", path);
    path.split("/").slice(0, -1).forEach((_, i, parts) => openDirs.add(parts.slice(0, i + 1).join("/") + "/"));
    store.set("dirs", [...openDirs]); treeSel = path;
    await loadFiles(); await openFile(path);
  }, after: () => view.focus() });
}
function newFolder() {
  if (readOnly) return;
  fsDialog({ title: "New folder", label: "Path", value: joinPath(baseDir(), "new-folder"), ok: "Create", run: async (path) => {
    await api.fs(cur, "mkdir", path);
    path.split("/").forEach((_, i, parts) => openDirs.add(parts.slice(0, i + 1).join("/") + "/"));
    store.set("dirs", [...openDirs]); treeSel = path; await loadFiles();
  } });
}
/** Close the tabs under path (their files are about to move or vanish), run fn, and on failure reopen them. */
async function withTabsClosed(path, fn) {
  const open = affected(path), was = open.map((t) => t.path), activePath = active?.path;
  for (const t of open) { await saveTab(t); await closeTab(t, true, true); }   // Rooms are left first, so no one is told off for our own change.
  try { await fn(); return { was, activePath }; }
  catch (e) { for (const p of was) await openFile(p, 0, { noFocus: true }); throw e; }
}
function renameTarget(path = treeTarget()) {
  if (readOnly || !path || path === "main.tex") return;
  fsDialog({ title: "Rename or move", message: path, label: "New path (change the folder to move it)", value: path, ok: "Rename", run: async (to) => {
    if (!to || to === path) return;
    const { was, activePath } = await withTabsClosed(path, () => api.fs(cur, "rename", path, to));
    treeSel = to;
    await loadFiles();
    const moved = (p) => to + p.slice(path.length);
    for (const p of was) await openFile(moved(p), 0, { noFocus: true });   // The same tabs, at the new path.
    if (activePath && was.includes(activePath)) await activate(tabs.get(moved(activePath)), 0, { noFocus: true });
    else if (!active) await openFile("main.tex");
    renderTree(); loadOutline();
  } });
}
function deleteTarget(path = treeTarget()) {
  if (readOnly || !path || path === "main.tex") return;
  const inside = files.filter((f) => f.path.startsWith(path + "/")).length, isDir = inside > 0 || emptyDirs.includes(path);
  fsDialog({ title: isDir ? "Delete folder" : "Delete file", message: isDir ? `Delete ${path} and the ${inside} ${inside === 1 ? "file" : "files"} in it? This cannot be undone.` : `Delete ${path}? This cannot be undone.`,
    ok: "Delete", danger: true, run: async () => {
      await withTabsClosed(path, () => api.fs(cur, "delete", path));
      if (treeSel === path || treeSel?.startsWith(path + "/")) treeSel = null;
      await loadFiles(); loadOutline();
      if (!active) await openFile("main.tex");
      renderTree();
      toast(el("span", { textContent: `Deleted ${path}.` }));
    } });
}
$("fNewFile").onclick = newFile; $("fNewDir").onclick = newFolder;
$("fRename").onclick = () => renameTarget(); $("fDelete").onclick = () => deleteTarget();
$("tree").addEventListener("keydown", (e) => {
  const row = e.target.closest?.(".row"); if (!row?.dataset.path) return;
  if (e.key === "F2") { e.preventDefault(); renameTarget(row.dataset.path); }
  else if (e.key === "Delete") { e.preventDefault(); deleteTarget(row.dataset.path); }
});

// ---- sidebar: outline -------------------------------------------------------------------------
const outlineVisible = () => ui.side && ui.sideTab === "outline";
async function loadOutline() {
  if (!cur || !outlineVisible()) return;
  try { outlineData = await api.outline(cur); } catch { return; }
  const d = outlineData, pages = d.pages ?? docs[cur]?.pages;
  $("outlineHead").replaceChildren(
    el("span", {}, el("b", { textContent: d.total_words.toLocaleString() }), " words"),
    ...(pages ? [el("span", {}, el("b", { textContent: pages }), pages === 1 ? " page" : " pages")] : []),
    el("span", { textContent: d.counter === "texcount" ? "via texcount" : "approximate" }));
  const goals = store.json("goals:" + cur, {});
  const setGoal = (it, value) => { if (value > 0) goals[`${it.file}:${it.title}`] = value; else delete goals[`${it.file}:${it.title}`]; store.set("goals:" + cur, goals); loadOutline(); };
  $("outlineList").replaceChildren(...d.items.map((it) => {
    const goal = goals[`${it.file}:${it.title}`];
    const count = el("span", { className: "w", textContent: goal ? `${it.words.toLocaleString()} / ${goal.toLocaleString()}` : it.words.toLocaleString(), title: `${it.own} in this section, ${it.words} with subsections${goal ? `; goal ${goal}` : ""}` });
    const jump = el("button", { className: `orow l${it.level}`, title: `${it.file}:${it.line}`, onclick: () => jumpTo(it.file, it.line) }, el("span", { className: "t", textContent: it.title }), count);
    jump.style.paddingLeft = `${8 + Math.max(0, it.level - 1) * 14}px`;
    // The goal control stays out of sight until the row is hovered or focused (progressive disclosure).
    const edit = el("button", { type: "button", className: "icon goal", title: goal ? "Change or clear the word goal" : "Set a word goal for this section", onclick: () => {
      const input = el("input", { type: "number", min: 0, step: 50, className: "goal-in", value: goal || "", placeholder: "goal" });
      input.setAttribute("aria-label", `Word goal for ${it.title}`);
      let done = false;
      const finish = (save) => { if (done) return; done = true; if (save) setGoal(it, Math.round(+input.value) || 0); else loadOutline(); };
      input.addEventListener("keydown", (e) => { if (e.key === "Enter") finish(true); else if (e.key === "Escape") { e.stopPropagation(); finish(false); } });
      input.addEventListener("blur", () => finish(true));
      count.replaceWith(input); edit.hidden = true; input.focus(); input.select();
    } }, icon("target"));
    edit.setAttribute("aria-label", `${goal ? "Change" : "Set"} word goal for ${it.title}`);
    const wrap = el("div", { className: "owrap" + (goal ? " has" : "") + (goal && it.words >= goal ? " met" : "") }, jump, ...(readOnly ? [] : [edit]));
    if (goal) wrap.style.setProperty("--p", Math.min(100, (it.words / goal) * 100) + "%");
    return wrap;
  }));
}

const narrow = () => window.matchMedia("(max-width: 1000px)").matches;
async function jumpTo(path, line) {
  if (narrow()) setSide(false);   // The sidebar overlays the editor at this width: get out of the way.
  await openFile(path, line);
  forwardSearch(path, line);
}

// ---- PDF -----------------------------------------------------------------------------------------
const pdfView = new PdfView($("viewer"), $("pages"), {
  zoom: (settings.zoom || 100) / 100,
  onZoom: (z, fit) => { settings.zoom = Math.round(z * 100); settings.fit = !!fit; $("zLevel").textContent = settings.zoom + "%"; $("zFit").setAttribute("aria-pressed", String(!!fit)); saveSettings(); },
  onFirstPage: () => { window.__firstPage = Math.round(performance.now() - (window.__loadStart ?? t0)); },
});
$("zLevel").textContent = (settings.zoom || 100) + "%";
pdfView.fitMode = settings.fit; $("zFit").setAttribute("aria-pressed", String(settings.fit));
{   // Follow the pane while in fit mode: opening the sidebar or dragging the splitter must not crop the page.
  let t; new ResizeObserver(() => { clearTimeout(t); t = setTimeout(() => pdfView.fitMode && pdfView.sizes.length && pdfView.fit(), 120); }).observe($("viewer"));
}

/** The chapter preview the PDF pane should show, if the person asked for one and it is built; else null (the full PDF). */
const mineFocus = () => { const f = docs[cur]?.focus; return focusView && focusView.started != null && f && f.path === focusView.path && f.started >= focusView.started ? f : null; };   // Not an older preview's state.
const shownFocus = () => { const f = mineFocus(); return f && f.status === "ok" ? f : null; };
const wantedVersion = () => shownFocus() ? "focus:" + shownFocus().version : docs[cur]?.version;
async function loadPdf() {
  const d = docs[cur], f = shownFocus();
  if (!d || !d.version) { pdfView.clear(); showEmpty(d); return; }
  $("empty").hidden = true;
  window.__loadStart = performance.now();
  const version = wantedVersion();
  const switching = !!f !== !!pdfView.focused, top = pdfView.viewer.scrollTop;
  await pdfView.load(api.pdfUrl(cur, f ? f.version : d.version, !!f), version);
  if (switching) {   // Chapter preview opens at its first page; the full PDF comes back where you were reading.
    if (f) pdfView.fullTop = top;
    pdfView.viewer.scrollTop = f ? 0 : pdfView.fullTop || 0;
  }
  pdfView.focused = !!f;
  if (pdfView.fitMode) pdfView.fit();
  liveOff.clear();
  if (!f) await live.afterLoad(d.started);
}

// ---- live preview: the chapter being typed, spliced over its pages a moment after typing stops (preview.js) ----------
const liveOff = new Set();   // files the server said cannot be previewed live, until the next full PDF
const live = new LivePreview(pdfView, {
  doc: () => cur,
  tab: () => isChapter(active) ? { path: active.path, text: view.state.doc.toString() } : null,
  enabled: () => settings.live && !readOnly && !shownFocus() && isChapter(active) && !liveOff.has(cur + ":" + active.path),
  onState: (st) => {
    const node = $("liveState");
    if (st.off) { if (active) liveOff.add(cur + ":" + active.path); node.hidden = true; return; }   // Not a chapter, no full build yet, ...
    node.hidden = false;
    node.classList.toggle("busy", !!st.busy); node.classList.toggle("bad", !!st.error);
    node.textContent = st.busy ? "Live: typesetting" : st.error ? "Live: error" : `Live: ${st.seconds.toFixed(1)} s`;
    node.title = st.error || (st.busy ? "Typesetting the chapter you are editing" : `Chapter typeset from your text in ${st.seconds.toFixed(2)} s${st.warm ? "" : " (cold start)"}; the full PDF follows on save`);
  },
});

// ---- chapter preview: build only the chapter being edited; the chip says so and leads back to the full PDF ----------
let focusView = null;   // {path} while the person asked for a chapter preview of that file
const isChapter = (tab) => tab?.kind === "text" && tab.path.endsWith(".tex") && tab.path !== "main.tex";
async function exportDocx() {
  toast(el("span", { textContent: "Converting to DOCX with pandoc..." }));
  try { await api.docx(cur); const a = el("a", { href: api.docxUrl(cur), download: "" }); a.click(); toast(el("span", { textContent: "DOCX ready; your browser is downloading it." })); }
  catch (e) { toast(el("span", { textContent: e.message })); }
}
async function previewChapter(quiet) {
  if (readOnly || !isChapter(active)) { if (!quiet) toast(el("span", { textContent: "Open a chapter file (one that main.tex includes) to preview it." })); return; }
  const path = active.path;
  await saveTab(active);   // The preview is built from the files on disk.
  focusView = { path, started: null };
  renderFocus();
  try { focusView.started = (await api.focus(cur, path)).started; renderFocus(); if (wantedVersion() !== pdfView.version) loadPdf(); }
  catch (e) { if (e.status === 409 && /already building/.test(e.message)) { focusView.started = docs[cur]?.focus?.started ?? 0; return; } focusView = null; renderFocus(); if (!quiet || e.status !== 409) toast(el("span", { textContent: e.message })); }
}
function backToFull() { focusView = null; renderFocus(); loadPdf(); $("live").textContent = "Showing the full PDF"; }
function renderFocus() {
  const f = mineFocus(), note = $("focusNote");
  if (f && f.status === "failed") {   // Say it once; the full PDF stays.
    focusView = null; toast(el("span", { textContent: `Chapter preview failed: ${f.error}` }));
  }
  const building = focusView && (!f || f.status === "building");
  const on = !!shownFocus();
  note.hidden = !(building || on);
  $("pdfPane").classList.toggle("focusing", !note.hidden);
  note.classList.toggle("busy", !!building);
  if (building) note.replaceChildren(el("b", { textContent: "Chapter preview" }), el("span", { textContent: "building..." }));
  else if (on) {
    const again = el("button", { type: "button", textContent: "Refresh", title: "Build the chapter again", onclick: () => previewChapter() });
    again.disabled = readOnly;
    note.replaceChildren(el("b", { textContent: "Chapter preview" }), el("span", { className: "path", textContent: f.target || f.path, title: f.path }), again,
      el("button", { type: "button", textContent: "Back to full PDF", onclick: backToFull }));
  }
  const button = $("focusBtn");
  button.hidden = !isChapter(active);
  button.disabled = readOnly || !!building;
  button.title = readOnly ? "Chapter preview needs the edit link" : "Build only the chapter you are editing, with the numbering and references of the full document";
}
/** The PDF pane before there is a PDF: a page-shaped skeleton and what is going on, never a blank pane. */
function showEmpty(d) {
  const box = $("empty");
  const page = el("div", { className: "skeleton" }, ...[60, 100, 92, 100, 70, 0, 100, 96, 84, 100, 40].map((w) => el("i", { style: `width:${w}%` })));
  page.setAttribute("aria-hidden", "true");
  let title, text, extra = [];
  if (!d) { title = "No such document"; text = "Pick a document from the list at the top left."; }
  else if (d.status === "failed") {
    title = "The first build failed";
    text = "Fix the errors listed below. The preview appears as soon as the document builds.";
    extra = [el("button", { className: "btn", textContent: "Show problems", onclick: () => setDrawer(true, "problems") })];
  } else if (d.status === "building") {
    title = "Building your PDF";
    text = "The first build is the slowest: LaTeX makes several passes and caches what it can. Later saves are much quicker.";
    extra = [el("span", { className: "mute", textContent: "Elapsed " }, el("b", { id: "emptyTime", textContent: "0 s" }))];
  } else { title = "Waiting for the first build"; text = "It starts by itself when you save."; }
  box.replaceChildren(page, el("h2", { textContent: title }), el("p", { textContent: text }), ...extra);
  box.hidden = false;
  tickBuild();
}

async function forwardSearch(path, line) {
  if (!path) return;
  try { pdfView.reveal(await api.forward(cur, path, line)); } catch (e) { toast(el("span", { textContent: e.message })); }
}
const onFocusPdf = () => shownFocus() && (toast(el("span", { textContent: "This is the chapter preview. Go back to the full PDF to jump between source and PDF." })), true);
const toCursor = () => !onFocusPdf() && active?.kind === "text" && forwardSearch(active.path, view.state.doc.lineAt(view.state.selection.main.head).number);

async function inverse(e) {
  const page = e.target.closest(".page"); if (!page || onFocusPdf()) return;
  const r = page.getBoundingClientRect(), z = pdfView.zoom, n = pdfView.fullPage(+page.dataset.i);
  if (n == null) { toast(el("span", { textContent: "This page is a live preview. The jump works once the full PDF is back (after the build)." })); return; }
  try {
    const res = await api.inverse(cur, n, (e.clientX - r.left) / z, (e.clientY - r.top) / z);
    const link = el("a", { href: res.link, textContent: "Open in VS Code" });
    toast(el("span", { textContent: `${res.rel || res.file}:${res.line}  ` }), link);
    if (settings.inverse === "vscode" || !res.rel) location.href = res.link; else await openFile(res.rel, res.line);
  } catch (err) { toast(el("span", { textContent: err.message })); }
}
$("viewer").addEventListener("dblclick", inverse);
$("viewer").addEventListener("click", (e) => (e.ctrlKey || e.metaKey) && inverse(e));
$("zIn").onclick = () => pdfView.setZoom(pdfView.zoom * 1.2);
$("zOut").onclick = () => pdfView.setZoom(pdfView.zoom / 1.2);
$("zFit").onclick = () => pdfView.fit();
$("toCursor").onclick = toCursor;

// ---- build status and problems ----------------------------------------------------------------------
let announced = null;
function announce(d) {   // One short sentence per state change for screen readers (the pill itself is not a live region).
  const errs = d.errors.length || (d.status === "failed" ? 1 : 0);
  const key = d.status + (d.finished || "");
  if (key === announced) return;
  announced = key;
  $("live").textContent = d.status === "building" ? "Build started"
    : d.status === "failed" ? `Build failed, ${errs} ${errs === 1 ? "error" : "errors"}`
    : `Build finished${d.pages != null ? `, ${d.pages} ${d.pages === 1 ? "page" : "pages"}` : ""}${d.warnings ? `, ${d.warnings} ${d.warnings === 1 ? "warning" : "warnings"}` : ""}`;
}

let buildTimer;
const fmtSecs = (n) => n >= 90 ? `${Math.floor(n / 60)} min ${String(Math.round(n % 60)).padStart(2, "0")} s` : `${Math.round(n)} s`;
/** While a build runs: seconds so far in the pill, a bar that fills against the previous build's time, and the same in the empty PDF pane. */
function tickBuild() {
  const d = docs[cur];
  const bar = $("progress");
  if (!d || d.status !== "building") { clearInterval(buildTimer); buildTimer = null; bar.hidden = true; return; }
  const elapsed = Math.max(0, Date.now() / 1000 - (d.started || buildSeen));
  $("label").textContent = `Building ${fmtSecs(elapsed)}`;
  $("info").textContent = d.seconds ? `· last took ${fmtSecs(d.seconds)}` : "";
  $("status").title = statusTitle(d, elapsed);
  bar.hidden = false;
  bar.firstElementChild.style.width = d.seconds ? Math.min(95, (elapsed / d.seconds) * 100) + "%" : "";
  bar.classList.toggle("indeterminate", !d.seconds);
  const e = $("emptyTime"); if (e) e.textContent = fmtSecs(elapsed);
}
let buildSeen = Date.now() / 1000;
const statusTitle = (d, elapsed) => d.status === "building"
  ? `Building... ${fmtSecs(elapsed)}${d.seconds ? ` (last build took ${fmtSecs(d.seconds)})` : ""}. Click for problems and log`
  : [d.finished && "Finished " + new Date(d.finished * 1000).toLocaleTimeString(), d.status === "failed" && d.version && "Showing last good PDF", "Click for problems and log"].filter(Boolean).join(". ");

// The badge counts what the Warnings tab lists (boxes included) once that is loaded, else the build's own count.
function showWarnBadge(d = docs[cur]) {
  if (!d) return;
  const n = warnLoaded === cur + ":" + (d.finished || "") ? warnings.filter((w) => w.level !== "info").length : d.warnings;
  $("warnBadge").hidden = !n; $("warnBadge").textContent = `${n} warning${n === 1 ? "" : "s"}`;
  const errs = d.errors.length || (d.status === "failed" ? 1 : 0), chip = $("probChip");   // Phones show this one chip instead of both badges.
  chip.hidden = !errs && !n; chip.className = "badge " + (errs ? "bad" : "warn"); chip.textContent = errs + n;
  chip.setAttribute("aria-label", [errs && `${errs} error${errs === 1 ? "" : "s"}`, n && `${n} warning${n === 1 ? "" : "s"}`].filter(Boolean).join(", ") + ". Show problems");
}

function renderStatus() {
  const d = docs[cur]; if (!d) return;
  announce(d);
  $("status").className = "pill " + d.status;
  $("label").textContent = { idle: "Up to date", building: "Building...", ok: "Built", failed: "Build failed" }[d.status];
  const bits = [];
  if (d.pages != null) bits.push(d.pages + (d.pages === 1 ? " page" : " pages"));
  if (d.seconds != null) bits.push(d.seconds + "s");
  $("info").textContent = bits.length ? "· " + bits.join(" · ") : "";
  if (d.status === "building") { if (!buildTimer) { buildSeen = Date.now() / 1000; buildTimer = setInterval(tickBuild, 1000); } tickBuild(); } else tickBuild();
  $("status").title = statusTitle(d, Math.max(0, Date.now() / 1000 - (d.started || buildSeen)));
  const errs = d.errors.length || (d.status === "failed" ? 1 : 0);
  $("errBadge").hidden = !errs; $("errBadge").textContent = `${errs} error${errs === 1 ? "" : "s"}`;
  showWarnBadge(d);
  $("warnBadge").title = "Show the warnings from the build";
  $("problemCount").textContent = errs || "";
  const key = d.status === "failed" ? d.finished : null;
  if (key && key !== lastErrKey) { lastErrKey = key; setDrawer(true, "problems"); }   // New failure: show it once.
  if (!key) lastErrKey = null;
  const note = $("pdfNote");   // The PDF on screen is older than the source: say so.
  note.hidden = !(d.status === "failed" && d.version);
  if (!note.hidden) note.replaceChildren(el("span", { textContent: "Build failed. This is the last good PDF." }), el("button", { className: "link", textContent: "Show errors", onclick: () => setDrawer(true, "problems") }));
  if (!d.version) showEmpty(d);
  renderProblems(); markErrors();
}

function disclosure(head, more, open = false) {
  const li = el("li");
  const links = head.filter((n) => n.classList?.contains("where") && n.tagName === "A");   // A link cannot sit inside the toggle button.
  const btn = el("button", { className: "head", onclick: () => { const open = btn.getAttribute("aria-expanded") !== "true"; btn.setAttribute("aria-expanded", String(open)); body.hidden = !open; } }, icon("chev"), ...head.filter((n) => !links.includes(n)));
  btn.firstChild.classList.add("chev");
  btn.setAttribute("aria-expanded", String(open));
  const body = el("div", { className: "more", hidden: !open }, ...more);
  li.append(el("div", { className: "hrow" }, btn, ...links), body);
  return li;
}

function whereLink(file, line) {
  return el("a", { className: "where", href: "#", textContent: `${file}:${line}`, onclick: (e) => { e.preventDefault(); e.stopPropagation(); openFile(file, line); } });
}

let excerptKey = null, excerpt = null;
/** A failed build with no parsed file:line (missing package, bad class...): TeX's own first complaint from the log. */
async function loadExcerpt(d) {
  if (excerptKey === d.finished) return;
  excerptKey = d.finished; excerpt = null;
  try {
    const rows = (await (await fetch(api.logUrl(cur))).text()).split("\n");
    const i = rows.findIndex((r) => r.startsWith("!"));
    excerpt = i >= 0 ? rows.slice(i, i + 8).join("\n").trim() : "";
  } catch { excerpt = ""; }
  renderProblems();
}

function renderProblems() {
  const d = docs[cur]; if (!d) return;
  const list = [];
  const logLink = el("button", { className: "link", textContent: "Open the full log", onclick: () => setDrawer(true, "log") });
  if (d.error && !d.errors.length) list.push(disclosure([el("span", { className: "sev" }), el("span", { className: "msg", textContent: d.error })], [...(d.error_hint ? [el("div", { className: "hint", textContent: d.error_hint })] : []), logLink], true));
  d.errors.forEach((e, i) => {
    const more = [];
    if (e.hint) more.push(el("div", { className: "hint", textContent: e.hint }));
    if (e.excerpt) more.push(el("pre", { textContent: e.excerpt, tabIndex: 0 }));
    if (!more.length) more.push(el("span", { className: "mute", textContent: "No further details." }), logLink.cloneNode(true));
    more.push(...aiUi.explainButton(e));
    list.push(disclosure([el("span", { className: "sev" }), el("span", { className: "msg", textContent: e.message }), whereLink(e.file, e.line)], more, i === 0));   // The first error opens by itself; the rest stay one line each.
  });
  if (!list.length && d.status === "failed") {
    loadExcerpt(d);
    list.push(disclosure([el("span", { className: "sev" }), el("span", { className: "msg", textContent: "The build failed, but LaTeX did not name a file and line." })],
      [...(excerpt ? [el("pre", { textContent: excerpt, tabIndex: 0 })] : []), logLink.cloneNode(true)], true));
  }
  if (!list.length) list.push(el("li", { className: "none" }, el("span", { className: "ok-mark", textContent: "No errors" }), el("span", { className: "mute", textContent: d.status === "building" ? " Building..." : " in the last build." }),
    ...(d.warnings ? [" ", el("button", { className: "link", textContent: `See ${d.warnings} ${d.warnings === 1 ? "warning" : "warnings"}`, onclick: () => setDrawer(true, "warnings") })] : [])));
  $("problems").replaceChildren(...list);
}

// Bib "Look up" (bib.js): the text comes from the open tab if there is one, and the edit goes into the editor, not to disk.
const bibCtx = {
  api, get doc() { return cur; },
  async text(path) {
    const t = tabs.get(path);
    if (!t) return (await api.read(cur, path)).text;
    return t === active ? view.state.doc.toString() : t.collab ? t.collab.text() : t.state.doc.toString();
  },
  async apply(path, at, insert, text) {
    await openFile(path);
    if (view.state.doc.toString() !== text) return false;  // Edited while the file opened: the offsets no longer fit.
    view.dispatch({ changes: { from: at, insert }, userEvent: "input.complete" });
    return true;
  },
};

// References panel (refs.js): reads through the server, writes as splices into the open editor document.
const refsUi = refsPanel($("panel-refs"), {
  api, el, icon, readOnly, bib: bibCtx, doc: () => cur, toast: (m) => toast(el("span", { textContent: m })),
  text: (path) => bibCtx.text(path),
  async openTexts() { return Object.fromEntries(await Promise.all([...tabs.values()].filter((t) => t.kind === "text" && /\.bib$/i.test(t.path)).map(async (t) => [t.path, await bibCtx.text(t.path)]))); },
  async splice(path, from, to, insert, expect) {
    await openFile(path, 0, { noFocus: true });
    if (view.state.doc.toString() !== expect) return false;   // Edited meanwhile: the offsets no longer fit.
    view.dispatch({ changes: { from, to, insert }, selection: { anchor: from }, scrollIntoView: true, userEvent: "input.complete" });
    return true;
  },
  async insertCite(keys) {
    const path = active?.kind === "text" && /\.tex$/i.test(active.path) ? active.path : lastTex;
    if (!path) { toast(el("span", { textContent: "Open a .tex file first; \\cite goes in at its cursor." })); return; }
    if (narrow()) setSide(false);
    if (active?.path !== path) await openFile(path, 0, { noFocus: true });
    const sel = view.state.selection.main, before = view.state.doc.sliceString(Math.max(0, sel.head - 300), sel.head), after = view.state.doc.sliceString(sel.head, sel.head + 300);
    const inside = sel.empty && /\\\w*cite\w*\*?(?:\[[^\]]*\])*\{[^{}]*$/.test(before) && /^[^{}]*\}/.test(after);   // Cursor in \cite{...}: add the keys to it.
    const insert = inside ? (/[{,]\s*$/.test(before) ? "" : ",") + keys.join(",") + (/^\s*[,}]/.test(after) ? "" : ",") : `\\cite{${keys.join(",")}}`;
    view.dispatch({ changes: { from: sel.from, to: sel.to, insert }, selection: { anchor: sel.from + insert.length }, scrollIntoView: true, userEvent: "input" });
    view.focus();
  },
  async jump(path, line, col) {
    if (/\.tex$/i.test(path)) await jumpTo(path, line); else { if (narrow()) setSide(false); await openFile(path, line); }
    if (!col || active?.path !== path) return;
    const ln = view.state.doc.line(Math.max(1, Math.min(line, view.state.doc.lines))), pos = Math.min(ln.from + col - 1, ln.to);
    view.dispatch({ selection: { anchor: pos }, effects: EditorView.scrollIntoView(pos, { y: "center" }) });
  },
  async newFile(path) { await api.fs(cur, "newfile", path); await loadFiles(); },
  changed: () => loadRefs(),
});

// Assistant panel (ai.js): the server asks the model; answers go into the open editor document only on Apply.
const aiUi = aiPanel($("panel-ai"), {
  api, el, icon, role, readOnly, doc: () => cur, toast: (m) => toast(el("span", { textContent: m })),
  show: () => setSide(true, "ai"), text: (path) => bibCtx.text(path), errors: () => (docs[cur]?.errors || []).filter((e) => e.file),
  logUrl: () => api.logUrl(cur), current: () => active?.kind === "text" ? { path: active.path, view } : null,
  async view(path) { if (narrow()) setSide(false); if (active?.path !== path) await openFile(path, 0, { noFocus: true }); return view; },
});
aiUi.load().then(() => renderProblems());
// Version history (history.js): the drawer's History tab and the diff dialog. Restored text for a file open in a
// co-editing room goes in here, through the editor, like every other edit.
const historyUi = historyPanel($("dpanel-history"), {
  api, el, doc: () => cur, activePath: () => (active?.kind === "text" ? active.path : null), canEdit: !readOnly, dialog: $("diffDlg"),
  toast: (m) => toast(el("span", { textContent: m })), live: (m) => { $("live").textContent = m; },
  async saveAll() { await Promise.all([...tabs.values()].filter((t) => t.kind === "text" && t.dirty).map((t) => saveTab(t))); },
  async apply(path, text) {
    await openFile(path, 0, { noFocus: true });
    if (active?.path !== path) return false;
    const h = hunk(view.state.doc.toString(), text);
    view.dispatch({ changes: { from: h.from, to: h.to, insert: h.insert }, annotations: REV.bypass.of(true), userEvent: "input.restore" });
    return true;
  },
});
$("diffClose").onclick = () => $("diffDlg").close();
REV.mount($("dpanel-review"));
$("commentBtn").onclick = () => REV.startComment();
$("suggestBtn").onclick = () => toggleSuggest();

async function loadLint() {
  $("lintList").replaceChildren(el("li", { className: "none", textContent: "Checking..." }));
  try {
    const { findings } = await api.lint(cur);
    $("lintCount").textContent = findings.length || "";
    $("lintList").replaceChildren(...(findings.length ? findings.map((f) => {
      const li = disclosure([el("span", { className: "sev " + f.level }), el("span", { className: "msg", textContent: f.message }), ...(f.line ? [whereLink(f.path, f.line)] : [el("span", { className: "where mute", textContent: f.path })])],
        [el("span", { className: "mute", textContent: `${f.kind} - ${f.path}${f.line ? ":" + f.line : ""}` })]);
      if (f.kind === "missing-bib-field" && !readOnly) addBibLookup(li, f, bibCtx);
      return li;
    }) : [el("li", { className: "none", textContent: "No lint findings." })]));
  } catch (e) { $("lintList").replaceChildren(el("li", { className: "none", textContent: e.message })); }
}

// ---- grammar: LanguageTool through the server, checked 2 s after typing stops --------------------------------------
// The mode lives on the server (owner setting, build.toml, or a local server found on its own); view-only links cannot run checks.
let grammarTimer, grammarRun = 0, grammarInfo = { mode: null, notice: null, error: null, busy: false };
const grammarMode = () => role === "owner" ? settings.grammar : (config.grammar?.mode || "auto");
const checkable = (tab) => tab && tab.kind === "text" && /\.tex$/i.test(tab.path);
function scheduleGrammar(delay = 2000) {
  clearTimeout(grammarTimer);
  if (delay >= 0 && !readOnly && checkable(active) && grammarMode() !== "off") grammarTimer = setTimeout(runGrammar, delay);
}
async function runGrammar() {
  const tab = active;
  if (readOnly || !checkable(tab) || grammarMode() === "off") { renderGrammar(); return; }
  const doc = view.state.doc, run = ++grammarRun;
  grammarInfo = { ...grammarInfo, busy: true, error: null }; renderGrammar();
  try {
    const res = await api.grammar(cur, doc.toString());
    if (run !== grammarRun) return;
    grammarInfo = { mode: res.mode, notice: res.notice, error: null, busy: false };
    if (active === tab && view.state.doc.eq(doc)) GR.set(view, res.findings);   // Typed meanwhile: the next check is already scheduled.
  } catch (e) { if (run === grammarRun) grammarInfo = { ...grammarInfo, error: e.message, busy: false }; }
  renderGrammar();
}
function renderGrammar() {
  const ranges = !readOnly && checkable(active) ? GR.list(view) : [];
  $("grammarCount").textContent = ranges.length || "";
  const note = readOnly ? "View-only links cannot run grammar checks."
    : !checkable(active) ? "Open a .tex file to check its grammar."
    : grammarMode() === "off" ? "Grammar check is off (Settings, Grammar)."
    : grammarInfo.busy ? "Checking..." : grammarInfo.error ? grammarInfo.error
    : grammarInfo.mode === "off" ? (grammarInfo.notice || "Grammar check is off.")
    : grammarInfo.mode === "public" ? grammarInfo.notice
    : grammarInfo.mode ? "Checked with a LanguageTool server on this machine; the text does not leave it." : "";
  $("grammarNote").textContent = note;
  $("grammarList").replaceChildren(...(ranges.length ? ranges.map((r) => {
    const f = r.finding;
    const jump = el("a", { className: "where", href: "#", textContent: `${f.line}:${f.col}`, onclick: (e) => { e.preventDefault(); view.dispatch({ selection: { anchor: r.from }, effects: EditorView.scrollIntoView(r.from, { y: "center" }) }); view.focus(); } });
    const fixes = f.replacements.map((t) => el("button", { type: "button", className: "btn", textContent: t || "(delete)", onclick: () => { const now = GR.list(view).find((x) => x.finding === f); if (now) GR.apply(view, now, t); renderGrammar(); } }));
    return el("li", {}, el("div", { className: "row" }, el("span", { className: "sev info" }), el("span", { className: "msg", textContent: f.message }), jump),
      ...(fixes.length ? [el("div", { className: "fixes" }, ...fixes)] : []), el("div", { className: "fixes mute", textContent: f.rule }));
  }) : grammarInfo.mode && grammarInfo.mode !== "off" && !grammarInfo.busy && !grammarInfo.error ? [el("li", { className: "none", textContent: "No grammar findings." })] : []));
}
async function pushGrammarSettings() {
  if (role !== "owner") return;
  try { await api.grammarSettings({ mode: settings.grammar, url: settings.grammarUrl.trim() || null, share_public: settings.grammarShare }); }
  catch (e) { toast(el("span", { textContent: "Grammar settings: " + e.message })); }
  scheduleGrammar(0);
}
function grammarShareNote() {   // also the AI assistant's line (ai.js), when the owner has a key
  return [grammarOnlyNote(), aiUi.shareNote()].filter(Boolean).join(" ");
}
function grammarOnlyNote() {
  return settings.grammar === "public" || settings.grammarShare
    ? (settings.grammarShare ? "Grammar: the public LanguageTool API is allowed while sharing, so text from everyone with a link is sent to languagetool.org."
      : "Grammar: public mode is switched off while sharing; allow it in Settings, Grammar.")
    : "Grammar checks use a local LanguageTool server only; public mode stays off while sharing.";
}

// Warnings parsed from the LaTeX log on the server; kinds are filterable, each row jumps to its source line.
const WARN_KINDS = [["all", "All"], ["undefined", "References"], ["box", "Boxes"], ["package", "Packages"]];
const warnGroup = (w) => w.kind === "overfull" || w.kind === "underfull" ? "box" : w.kind === "undefined" ? "undefined" : ["package", "latex", "bibtex"].includes(w.kind) ? "package" : "other";
let warnings = [], warnFilter = "all", warnKey = null, warnLoaded = null;
function renderWarnings() {
  const real = warnings.filter((w) => w.level !== "info").length;
  $("warnCount").textContent = real || "";
  showWarnBadge();
  $("warnFilter").replaceChildren(...WARN_KINDS.map(([id, label]) => {
    const n = id === "all" ? warnings.length : warnings.filter((w) => warnGroup(w) === id).length;
    const b = el("button", { type: "button", className: "chip", textContent: `${label} ${n}`, disabled: id !== "all" && !n, onclick: () => { warnFilter = id; renderWarnings(); } });
    b.setAttribute("aria-pressed", String(warnFilter === id));
    return b;
  }));
  const shown = warnings.filter((w) => warnFilter === "all" || warnGroup(w) === warnFilter);
  $("warnList").replaceChildren(...(shown.length ? shown.map((w) => {
    const where = w.file ? whereLink(w.file, w.line) : el("span", { className: "where mute", textContent: w.source });
    const more = [...(w.hint ? [el("div", { className: "hint", textContent: w.hint })] : []), ...(w.excerpt && w.excerpt !== w.message ? [el("pre", { textContent: w.excerpt, tabIndex: 0 })] : [])];
    if (!more.length) more.push(el("span", { className: "mute", textContent: `${w.source}${w.file ? ` - ${w.file}:${w.line}` : ""}` }));
    return disclosure([el("span", { className: "sev " + w.level }), el("span", { className: "msg", textContent: w.message }), where], more);
  }) : [el("li", { className: "none" }, el("span", { className: "ok-mark", textContent: "No warnings" }), el("span", { className: "mute", textContent: " in the last build." }))]));
}
async function loadWarnings() {
  const key = cur + ":" + (docs[cur]?.finished || "");
  if (key === warnKey) return;
  warnKey = key;
  try { warnings = (await api.warnings(cur)).warnings; } catch { warnings = []; }
  if (warnKey !== key) return;   // Another build or document came in meanwhile.
  warnLoaded = key;
  renderWarnings();
}

async function loadLog() {
  try { $("logText").textContent = await (await fetch(api.logUrl(cur))).text(); } catch { $("logText").textContent = "No log yet."; }
}

const DRAWER_TABS = ["problems", "warnings", "lint", "grammar", "review", "history", "log"];
function setDrawer(open, tabName) {
  ui.drawer = open; if (tabName) ui.drawerTab = tabName; saveUi();
  $("drawer").hidden = !open; $("drawer").dataset.tab = ui.drawerTab;
  for (const b of ["status", "errBadge"]) $(b).setAttribute("aria-expanded", String(open));
  for (const n of DRAWER_TABS) {
    $("dtab-" + n).setAttribute("aria-selected", String(ui.drawerTab === n));
    $("dpanel-" + n).hidden = ui.drawerTab !== n;
  }
  if (open && ui.drawerTab === "warnings") loadWarnings();
  if (open && ui.drawerTab === "lint") loadLint();
  if (open && ui.drawerTab === "grammar") { renderGrammar(); runGrammar(); }
  if (open && ui.drawerTab === "log") loadLog();
  if (open && ui.drawerTab === "review") REV.load();
  if (open && ui.drawerTab === "history") historyUi.load();
}
for (const n of DRAWER_TABS) $("dtab-" + n).onclick = () => setDrawer(true, n);
const toggleDrawer = () => setDrawer(!ui.drawer);
$("status").onclick = toggleDrawer;
$("errBadge").onclick = () => setDrawer(true, "problems");
$("warnBadge").onclick = () => setDrawer(true, "warnings");
$("probChip").onclick = () => setDrawer(true, $("errBadge").hidden ? "warnings" : "problems");
$("drawerClose").onclick = () => setDrawer(false);

// ---- sidebar toggles ----------------------------------------------------------------------------------
function setSide(open, tabName) {
  ui.side = open; if (tabName) ui.sideTab = tabName; saveUi();
  $("side").hidden = !open; $("sideBtn").setAttribute("aria-expanded", String(open)); $("side").dataset.tab = ui.sideTab;
  for (const n of ["files", "outline", "refs", "ai"]) {
    $("tab-" + n).setAttribute("aria-selected", String(ui.sideTab === n));
    $("panel-" + n).hidden = ui.sideTab !== n;
  }
  if (open) { loadFiles(); renderTree(); loadOutline(); if (ui.sideTab === "refs") refsUi.load(); if (ui.sideTab === "ai") aiUi.load(); }
}
$("sideBtn").onclick = () => setSide(!ui.side);
$("sideClose").onclick = () => setSide(false);
$("tab-files").onclick = () => setSide(true, "files");
$("tab-outline").onclick = () => setSide(true, "outline");
$("tab-refs").onclick = () => setSide(true, "refs");
$("tab-ai").onclick = () => setSide(true, "ai");

// ---- visual mode and prose panel --------------------------------------------------------------------------
async function setVisual(on) {
  settings.visual = on; saveSettings();
  if (on) await loadRefs();
  await applyConfig();
}
async function loadRefs() { try { refs = await api.refs(cur); view?.dispatch({ effects: refresh.of(null) }); } catch { /* keep old */ } }
$("visualBtn").onclick = () => setVisual(!settings.visual);

let proseSpan = null, proseReadOnly = false, proseTimer;
function setProse(open) {
  ui.prose = open; saveUi();
  $("prose").hidden = !open; $("proseBtn").setAttribute("aria-expanded", String(open));
  if (open) syncProse();
}
function syncProse(own) {
  if (!ui.prose || active?.kind !== "text") return;
  const doc = view.state.doc.toString(), pos = view.state.selection.main.head;
  const p = prose.paragraphAt(doc, pos);
  if (own && proseSpan && p.from === proseSpan.from) { proseSpan = p; return; }   // Our own edit: leave the panel alone.
  proseSpan = p;
  const body = $("proseBody");
  if (!p.text.trim()) { body.contentEditable = "false"; body.textContent = ""; $("proseNote").textContent = "Empty line - place the cursor in a paragraph."; proseReadOnly = true; return; }
  const r = prose.toEditor(p.text);
  if (r.error) { proseReadOnly = true; body.contentEditable = "false"; body.textContent = p.text; $("proseNote").textContent = "Read-only: " + r.error; }
  else { proseReadOnly = false; body.contentEditable = "true"; body.innerHTML = r.html; $("proseNote").textContent = "Plain prose: bold, italic and emphasis only"; }
  for (const b of ["pBold", "pItalic", "pEmph"]) $(b).disabled = proseReadOnly;
}
function onProseInput() {
  if (proseReadOnly || !proseSpan) return;
  clearTimeout(proseTimer);
  proseTimer = setTimeout(() => {
    const next = prose.htmlToLatex($("proseBody").innerHTML);
    const span = proseSpan;
    if (view.state.doc.sliceString(span.from, span.to) === span.text) {
      view.dispatch({ changes: { from: span.from, to: span.to, insert: next }, annotations: proseEdit.of(true), userEvent: "input.prose" });
      proseSpan = { from: span.from, to: span.from + next.length, text: next };
    }
  }, 120);
}
$("proseBody").addEventListener("input", onProseInput);
$("proseBody").addEventListener("keydown", (e) => { if (e.key === "Enter") { e.preventDefault(); document.execCommand("insertText", false, "\n"); } });
$("proseBody").addEventListener("paste", (e) => { e.preventDefault(); document.execCommand("insertText", false, e.clipboardData.getData("text/plain")); });
$("pBold").onclick = () => { $("proseBody").focus(); document.execCommand("bold"); onProseInput(); };
$("pItalic").onclick = () => { $("proseBody").focus(); document.execCommand("italic"); onProseInput(); };
$("pEmph").onclick = () => {
  $("proseBody").focus();
  const sel = getSelection().toString();
  if (sel) { document.execCommand("insertHTML", false, "<em>" + sel.replace(/&/g, "&amp;").replace(/</g, "&lt;") + "</em>"); onProseInput(); }
};
$("proseBtn").onclick = () => setProse(!ui.prose);
$("proseClose").onclick = () => setProse(false);

// A modal <dialog> keeps clicks inside but Tab can still walk out to the browser chrome: wrap it.
for (const dlg of document.querySelectorAll("dialog")) dlg.addEventListener("keydown", (e) => {
  if (e.key !== "Tab") return;
  const f = [...dlg.querySelectorAll("button,input,select,textarea,a[href],summary,[tabindex]:not([tabindex='-1'])")].filter((x) => !x.disabled && x.getClientRects().length && (x.tagName === "SUMMARY" || !x.closest("details:not([open])")));
  if (!f.length) return;
  const first = f[0], last = f[f.length - 1];
  if (e.shiftKey && document.activeElement === first) { e.preventDefault(); last.focus(); }
  else if (!e.shiftKey && document.activeElement === last) { e.preventDefault(); first.focus(); }
});

// ---- first-run tips: one dismissible line that teaches the four things worth knowing ----------------------------
function showTips(force) {
  const box = $("tips");
  if (!force && (readOnly || store.get("tips") === "done")) { box.hidden = true; return; }
  const tip = (label, text, run) => el("button", { className: "tip", onclick: run }, el("b", { textContent: label }), el("span", { textContent: text }));
  box.replaceChildren(
    el("span", { className: "tips-title", textContent: "Quick start" }),
    tip("Outline", "jump to any section, with word counts", () => setSide(true, "outline")),
    tip("Visual", "see formatting and math instead of raw LaTeX", () => setVisual(true)),
    tip(`${mod}+K`, "search every command", () => openPalette()),
    el("span", { className: "tip plain", textContent: "Double-click the PDF to jump to its source" }),
    el("span", { className: "spacer" }),
    el("button", { className: "btn", textContent: "Got it", onclick: () => { store.set("tips", "done"); box.hidden = true; } }));
  box.hidden = false;
}

// ---- command palette, menu, settings, cheat sheet ----------------------------------------------------
const mod = /Mac/.test(navigator.platform) ? "Cmd" : "Ctrl";
const COMMANDS = [
  { id: "save", edit: true, title: "Save file", keys: `${mod}+S`, run: () => saveTab(active) },
  { id: "palette", title: "Command palette", keys: `${mod}+K`, run: () => openPalette() },
  { id: "files", title: "Toggle files", keys: `${mod}+B`, run: () => (ui.side && ui.sideTab === "files") ? setSide(false) : setSide(true, "files") },
  { id: "outline", title: "Toggle outline", keys: `${mod}+Shift+O`, run: () => (ui.side && ui.sideTab === "outline") ? setSide(false) : setSide(true, "outline") },
  { id: "references", title: "Toggle references", run: () => (ui.side && ui.sideTab === "refs") ? setSide(false) : setSide(true, "refs") },
  { id: "assistant", title: "Toggle AI assistant", run: () => (ui.side && ui.sideTab === "ai") ? setSide(false) : setSide(true, "ai") },
  ...aiUi.commands,
  { id: "problems", title: "Toggle problems panel", keys: `${mod}+J`, run: () => toggleDrawer() },
  { id: "log", title: "Show build log", run: () => setDrawer(true, "log") },
  { id: "warnings", title: "Show warnings", run: () => setDrawer(true, "warnings") },
  { id: "lint", title: "Show lint findings", run: () => setDrawer(true, "lint") },
  { id: "grammar", title: "Show grammar findings", run: () => setDrawer(true, "grammar") },
  { id: "comment", edit: true, title: "Comment on the selection", keys: `${mod}+Alt+M`, run: () => REV.startComment() },
  { id: "suggest", edit: true, title: "Toggle suggest mode (track changes)", keys: `${mod}+Alt+S`, run: () => toggleSuggest() },
  { id: "review", title: "Show comments and suggestions", run: () => setDrawer(true, "review") },
  { id: "history", title: "Show version history", run: () => setDrawer(true, "history") },
  { id: "visual", title: "Toggle visual mode", keys: `${mod}+Alt+V`, run: () => setVisual(!settings.visual) },
  { id: "prose", edit: true, title: "Paragraph editor (rich text)", keys: `${mod}+Alt+P`, run: () => setProse(!ui.prose) },
  { id: "newfile", edit: true, title: "New file...", run: () => newFile() },
  { id: "newdir", edit: true, title: "New folder...", run: () => newFolder() },
  { id: "rename", edit: true, title: "Rename or move file...", keys: "F2", run: () => { setSide(true, "files"); renameTarget(); } },
  { id: "delete", edit: true, title: "Delete file or folder...", run: () => { setSide(true, "files"); deleteTarget(); } },
  { id: "focus", edit: true, title: "Preview this chapter", run: () => previewChapter() },
  { id: "unfocus", title: "Back to the full PDF", run: () => backToFull() },
  { id: "rebuild", edit: true, title: "Rebuild from scratch", run: () => !readOnly && api.rebuild(cur) },
  { id: "docx", owner: true, title: "Export DOCX (pandoc)", run: () => exportDocx() },
  { id: "closetab", title: "Close tab", run: () => active && closeTab(active) },
  { id: "cursor", title: "Show cursor position in PDF", keys: `${mod}+Enter`, run: toCursor },
  { id: "zin", title: "PDF zoom in", run: () => pdfView.setZoom(pdfView.zoom * 1.2) },
  { id: "zout", title: "PDF zoom out", run: () => pdfView.setZoom(pdfView.zoom / 1.2) },
  { id: "fit", title: "PDF fit width", run: () => pdfView.fit() },
  ...(role === "owner" ? [{ id: "share", title: "Share...", run: () => openShare() }] : []),
  { id: "settings", title: "Settings", keys: `${mod}+,`, run: () => openSettings() },
  { id: "cheat", title: "Keyboard shortcuts", keys: "?", run: () => $("cheat").showModal() },
  { id: "tips", title: "Show getting-started tips", run: () => showTips(true) },
  { id: "theme", get title() { return `Theme: ${settings.theme} (change)`; }, run: () => { settings.theme = { system: "light", light: "dark", dark: "system" }[settings.theme]; saveSettings(); applyAppearance(); toast(el("span", { textContent: "Theme: " + settings.theme })); } },
  { id: "keys-default", title: "Keybindings: default", run: () => { settings.keys = "default"; saveSettings(); applyConfig(); } },
  { id: "keys-vim", title: "Keybindings: Vim", run: () => { settings.keys = "vim"; saveSettings(); applyConfig(); } },
  { id: "keys-emacs", title: "Keybindings: Emacs", run: () => { settings.keys = "emacs"; saveSettings(); applyConfig(); } },
];

let paletteItems = [], paletteSel = 0;
function openPalette() { $("palette").showModal(); $("paletteInput").value = ""; renderPalette(); $("paletteInput").focus(); }
function renderPalette() {
  const words = $("paletteInput").value.toLowerCase().split(/\s+/).filter(Boolean);
  paletteItems = COMMANDS.filter((c) => !(readOnly && c.edit) && !(c.owner && !(role === "owner" && config.pandoc))).filter((c) => words.every((w) => c.title.toLowerCase().includes(w)));
  paletteSel = Math.min(paletteSel, Math.max(0, paletteItems.length - 1));
  $("paletteList").replaceChildren(...paletteItems.map((c, i) => {
    const li = el("li", { role: "option", id: "pal-" + c.id, onclick: () => runPalette(i) }, el("span", { textContent: c.title }), ...(c.keys ? [el("span", { className: "keys", textContent: c.keys })] : []));
    li.setAttribute("aria-selected", String(i === paletteSel));
    return li;
  }));
  $("paletteInput").setAttribute("aria-activedescendant", paletteItems[paletteSel] ? "pal-" + paletteItems[paletteSel].id : "");
}
function runPalette(i) { const c = paletteItems[i]; $("palette").close(); if (c) setTimeout(c.run, 0); }
$("paletteInput").addEventListener("input", () => { paletteSel = 0; renderPalette(); });
$("paletteInput").addEventListener("keydown", (e) => {
  if (e.key === "ArrowDown") { paletteSel = Math.min(paletteSel + 1, paletteItems.length - 1); renderPalette(); e.preventDefault(); }
  else if (e.key === "ArrowUp") { paletteSel = Math.max(paletteSel - 1, 0); renderPalette(); e.preventDefault(); }
  else if (e.key === "Enter") { runPalette(paletteSel); e.preventDefault(); }
});
$("palette").addEventListener("click", (e) => { if (e.target === $("palette")) $("palette").close(); });

function openSettings() {
  $("sAutosave").value = settings.autosave; $("sTheme").value = settings.theme; $("sKeys").value = settings.keys;
  $("sName").value = me.name;
  $("sFont").value = settings.font; $("sZoom").value = settings.zoom; $("sVisual").checked = settings.visual; $("sInverse").value = settings.inverse;
  $("sSpell").checked = settings.spell; $("sFocusAuto").checked = settings.focusAuto; $("sFocusAuto").disabled = readOnly;
  $("sLive").checked = settings.live; $("sLive").disabled = readOnly;
  $("sGrammarBox").hidden = role !== "owner";
  $("sGrammar").value = settings.grammar; $("sGrammarUrl").value = settings.grammarUrl; $("sGrammarShare").checked = settings.grammarShare; grammarSettingsNote();
  $("settings").showModal();
  $("sName").focus();
}
const bind = (id, apply) => $(id).addEventListener("change", () => { apply($(id)); saveSettings(); applyAppearance(); });
$("sName").addEventListener("change", () => {
  me.name = $("sName").value.trim().slice(0, 40) || me.name; store.set("user", me);
  for (const room of collab?.rooms.values() || []) room.awareness.setLocalStateField("user", { name: me.name, color: me.color, colorLight: me.color + "33" });
  collab?.hello(active?.path);
});
bind("sAutosave", (n) => { settings.autosave = +n.value; showSaveState(); });
bind("sTheme", (n) => settings.theme = n.value);
bind("sKeys", (n) => { settings.keys = n.value; applyConfig(); });
bind("sFont", (n) => settings.font = Math.max(10, Math.min(28, +n.value || 14)));
bind("sZoom", (n) => pdfView.setZoom((+n.value || 133) / 100));
bind("sVisual", (n) => setVisual(n.checked));
bind("sInverse", (n) => settings.inverse = n.value);
bind("sSpell", (n) => { settings.spell = n.checked; applySpell(); });
bind("sFocusAuto", (n) => settings.focusAuto = n.checked);
bind("sLive", (n) => { settings.live = n.checked; if (!n.checked) { live.stop(); $("liveState").hidden = true; } });
function grammarSettingsNote() {
  $("sGrammarNote").textContent = settings.grammar === "public" ? "Public mode sends the text of the file you are editing to languagetool.org (api.languagetool.org), in paragraph batches within its free limits. Nothing is sent in the other modes."
    : "A local LanguageTool server keeps the text on this machine (docker run -p 8081:8010 erikvl87/languagetool). The URL can also come from LANGUAGETOOL_URL or build.toml (grammar_url).";
}
bind("sGrammar", (n) => {
  if (n.value === "public" && !confirm("Public mode sends the text of the file you are editing to languagetool.org. Use it?")) { n.value = settings.grammar; return; }
  settings.grammar = n.value; grammarSettingsNote(); pushGrammarSettings();
});
bind("sGrammarUrl", (n) => { settings.grammarUrl = n.value; pushGrammarSettings(); });
bind("sGrammarShare", (n) => { settings.grammarShare = n.checked; pushGrammarSettings(); });
$("focusBtn").onclick = () => previewChapter();
$("sCheat").onclick = () => { $("settings").close(); $("cheat").showModal(); };
for (const d of ["settings", "cheat"]) $(d).addEventListener("click", (e) => { if (e.target === $(d)) $(d).close(); });

$("cheatList").replaceChildren(...[...COMMANDS.filter((c) => c.keys && !(readOnly && c.edit)), { title: "Close dialogs and panels", keys: "Esc" }].flatMap((c) => [el("dt", { textContent: c.title }), el("dd", {}, el("kbd", { textContent: c.keys }))]));

function buildMenu() {
  const m = $("moreMenu");
  const groups = [["Panels", ["files", "outline", "references", "assistant"]], ["Build", ["problems", "warnings", "lint", "grammar", "log"]], ["Review", [...(readOnly ? [] : ["comment", "suggest"]), "review", "history"]],
    ["Document", [...(readOnly ? [] : ["prose"]), "visual", ...(role === "owner" && config.pandoc ? ["docx"] : [])]],
    ["Session", [...(role === "owner" ? ["share"] : []), "theme", "settings"]], ["Help", ["cheat", "tips", "palette"]]];
  m.replaceChildren(...groups.map(([label, ids], g) => withLabel(el("div", { role: "group", className: "mgroup" },
    el("div", { className: "mlabel", id: "mg" + g, textContent: label }), ...ids.map((id) => {
      const c = COMMANDS.find((x) => x.id === id);
      const b = el("button", { role: "menuitem", onclick: () => { closeMenu(); c.run(); } }, el("span", { textContent: c.title }), ...(c.keys ? [el("span", { className: "keys", textContent: c.keys })] : []));
      if (id === "docx" && sharing) { b.disabled = true; b.title = "Off while sharing: pandoc reads any file the shared source names."; }
      return b;
    })), "mg" + g)));
}
const withLabel = (n, id) => (n.setAttribute("aria-labelledby", id), n);
function closeMenu() { $("moreMenu").hidden = true; $("moreBtn").setAttribute("aria-expanded", "false"); }
$("moreBtn").onclick = (e) => {
  e.stopPropagation();
  const open = $("moreMenu").hidden;
  buildMenu();
  $("moreMenu").hidden = !open; $("moreBtn").setAttribute("aria-expanded", String(open));
  if (open) $("moreMenu").querySelector("button").focus();
};
$("moreMenu").addEventListener("keydown", (e) => {
  const items = [...$("moreMenu").querySelectorAll("button")], i = items.indexOf(document.activeElement);
  if (e.key === "ArrowDown") { items[(i + 1) % items.length].focus(); e.preventDefault(); }
  else if (e.key === "ArrowUp") { items[(i - 1 + items.length) % items.length].focus(); e.preventDefault(); }
  else if (e.key === "Home") { items[0].focus(); e.preventDefault(); }
  else if (e.key === "End") { items[items.length - 1].focus(); e.preventDefault(); }
  else if (e.key === "Escape") { closeMenu(); $("moreBtn").focus(); e.stopPropagation(); }
});
document.addEventListener("click", (e) => { if (!e.target.closest(".menu-wrap")) closeMenu(); });
$("rebuildBtn").onclick = () => api.rebuild(cur);

document.addEventListener("keydown", (e) => {
  const k = e.key.toLowerCase(), m = e.ctrlKey || e.metaKey;
  const typing = e.target.closest?.(".cm-editor, input, textarea, select, [contenteditable=true]");
  if (m && !e.altKey && !e.shiftKey && k === "k") { e.preventDefault(); openPalette(); }
  else if (m && e.shiftKey && k === "p") { e.preventDefault(); openPalette(); }
  else if (m && !e.shiftKey && !e.altKey && k === "b") { e.preventDefault(); COMMANDS.find((c) => c.id === "files").run(); }
  else if (m && e.shiftKey && k === "o") { e.preventDefault(); COMMANDS.find((c) => c.id === "outline").run(); }
  else if (m && !e.shiftKey && !e.altKey && k === "j") { e.preventDefault(); toggleDrawer(); }
  else if (m && e.altKey && k === "v") { e.preventDefault(); setVisual(!settings.visual); }
  else if (m && e.altKey && k === "p") { e.preventDefault(); setProse(!ui.prose); }
  else if (m && e.altKey && k === "m" && !readOnly) { e.preventDefault(); REV.startComment(); }
  else if (m && e.altKey && k === "s" && !readOnly) { e.preventDefault(); toggleSuggest(); }
  else if (m && k === ",") { e.preventDefault(); openSettings(); }
  else if (m && k === "enter" && active?.kind === "text") { e.preventDefault(); toCursor(); }
  else if (m && k === "s") { e.preventDefault(); saveTab(active); }
  else if (e.key === "?" && !typing && !e.target.closest("dialog")) { e.preventDefault(); $("cheat").showModal(); }
  else if (e.key === "Escape" && !document.querySelector("dialog[open]") && !typing) { if (ui.drawer) setDrawer(false); else if (ui.side) setSide(false); }
});

// ---- people and sharing ---------------------------------------------------------------------------------
const avatar = (u) => { const a = el("span", { className: "av", textContent: (u.name || "?").trim().slice(0, 1).toUpperCase(), title: u.name }); a.style.background = u.color; return a; };
const roleLabel = (r) => ({ owner: "host", edit: "can edit", view: "view only" }[r] || r);
function renderUsers(users) {
  const everyone = users || [], others = everyone.filter((u) => u.cid !== channel.cid);
  $("usersWrap").hidden = everyone.length < 2;
  $("usersBtn").replaceChildren(...others.slice(0, 4).map(avatar), ...(others.length > 4 ? [el("span", { className: "av more", textContent: "+" + (others.length - 4) })] : []));
  $("usersBtn").setAttribute("aria-label", `${everyone.length} people here. Show list`);
  $("usersList").replaceChildren(...everyone.map((u) => el("div", { className: "person" }, avatar(u),
    el("span", { className: "pname", textContent: u.name + (u.cid === channel.cid ? " (you)" : "") }),
    el("span", { className: "mute prole", textContent: roleLabel(u.role) + (u.path ? " · " + u.path.split("/").pop() : "") }))));
}
$("usersBtn").onclick = (e) => { e.stopPropagation(); const open = $("usersList").hidden; $("usersList").hidden = !open; $("usersBtn").setAttribute("aria-expanded", String(open)); };
document.addEventListener("click", (e) => { if (!e.target.closest("#usersWrap")) { $("usersList").hidden = true; $("usersBtn").setAttribute("aria-expanded", "false"); } });

let shareTimer;
let sharing = false;  // DOCX export is off while sharing (pandoc reads any file the source names)
const setSharing = (on) => { if (on !== sharing) { sharing = on; buildMenu(); } };
async function openShare() { $("share").showModal(); await refreshShare(); $("shareBody").querySelector("select, button")?.focus(); }
async function refreshShare() {
  clearTimeout(shareTimer);
  let info;
  try { info = await api.share(); } catch (e) { $("shareBody").textContent = e.message; return; }
  renderShare(info);
  $("shareBtn").classList.toggle("on", info.on);
  setSharing(!!info.on);
  if ($("share").open && info.on && info.status === "starting") shareTimer = setTimeout(refreshShare, 1000);
}
function linkRow(label, url, note) {
  const input = el("input", { type: "text", readOnly: true, value: url, id: "link-" + label.split(" ")[0].toLowerCase() });
  input.setAttribute("aria-label", label);
  const copy = el("button", { type: "button", className: "btn", textContent: "Copy", onclick: async () => {
    try { await navigator.clipboard.writeText(url); copy.textContent = "Copied"; } catch { input.select(); copy.textContent = "Press Ctrl+C"; }
    setTimeout(() => copy.textContent = "Copy", 2000);
  } });
  return el("div", { className: "link-row" }, el("b", { textContent: label }), el("div", { className: "link-line" }, input, copy), el("span", { className: "mute", textContent: note }));
}
function renderShare(info) {
  const body = $("shareBody");
  if (!info.on) {
    const select = el("select", { id: "shareProvider" }, new Option("Automatic (best installed tool)", "auto"),
      ...info.providers.map((p) => new Option(p.name + (p.available ? "" : " (not installed)"), p.name)), new Option("No tunnel (links for this machine only)", "local"));
    const start = el("button", { type: "button", className: "btn primary", id: "shareStart", textContent: "Start sharing", onclick: async () => {
      start.disabled = true;
      try { await api.shareStart(select.value, cur); await refreshShare(); } catch (e) { start.disabled = false; msg.textContent = e.message; msg.hidden = false; }
    } });
    const msg = el("p", { className: "err", role: "alert", hidden: !info.error, textContent: info.error || "" });
    body.replaceChildren(
      el("p", { textContent: "Start a tunnel and get two links to this document: one to read, one to edit together in real time. Each link holds a secret token and works until you stop sharing or quit." }),
      el("label", { htmlFor: "shareProvider", textContent: "Tunnel" }), select, msg, el("p", { className: "mute", textContent: grammarShareNote() }), el("div", { className: "actions" }, start));
  } else if (info.status === "starting") {
    body.replaceChildren(el("p", { role: "status", textContent: `Starting ${info.provider}... this can take up to a minute.` }),
      el("div", { className: "actions" }, el("button", { type: "button", className: "btn", textContent: "Cancel", onclick: async () => { await api.shareStop(); refreshShare(); } })));
  } else {
    const notes = [info.provider === "ngrok" && "ngrok's free plan shows a warning page first; visitors click Visit Site once.", info.provider === "localtunnel" && "localtunnel may ask visitors for a tunnel password (your public IP)."].filter(Boolean);
    body.replaceChildren(
      el("p", { textContent: `Sharing ${info.doc || "the document"} through ${info.provider}.` }),
      linkRow("View link", info.links.view, "Read-only: the source, the PDF and the outline."),
      linkRow("Edit link", info.links.edit, "Can edit the files of this document and rebuild it."),
      el("p", { className: "caution" }, el("b", { textContent: "Share edit links only with people you trust." }), " Shell escape stays off, but LuaLaTeX can still run code on this computer."),
      ...notes.map((t) => el("p", { className: "mute", textContent: t })),
      el("p", { className: "mute", id: "shareGrammar", textContent: grammarShareNote() }),
      el("div", { className: "actions" },
        el("button", { type: "button", className: "btn", id: "shareRegen", textContent: "New links", title: "Revoke both links and make new ones", onclick: async () => { if (confirm("Everyone using the current links loses access. Make new links?")) { await api.shareRegenerate(); refreshShare(); } } }),
        el("button", { type: "button", className: "btn danger", id: "shareStop", textContent: "Stop sharing", onclick: async () => { await api.shareStop(); refreshShare(); } })));
  }
}
$("shareBtn").onclick = openShare;
$("share").addEventListener("click", (e) => { if (e.target === $("share")) $("share").close(); });
$("share").addEventListener("close", () => clearTimeout(shareTimer));
if (readOnly || role !== "owner") $("shareBtn").hidden = true;
else api.share().then((i) => { $("shareBtn").classList.toggle("on", i.on); setSharing(!!i.on); }).catch(() => {});
// Roles: say who you are, and keep what you cannot do visible but disabled, with the reason.
if (role !== "owner") {
  const chip = $("roleChip");
  chip.hidden = false; chip.className = "role " + role;
  chip.replaceChildren(icon(readOnly ? "eye" : "share"), el("span", { textContent: readOnly ? "View only" : "Can edit" }));
  chip.title = config.hosted ? (readOnly ? "Your role in this project is viewer: you can read and download, but not change anything." : "You can edit this project: changes are shared live with everyone in it.")
    : readOnly ? "You opened a view link: you can read, scroll and jump between source and PDF, but not change anything. Ask the host for the edit link." : "You opened an edit link: changes are shared live. Only the host can share or stop sharing.";
  chip.tabIndex = 0;
}
if (config.hosted) {   // Behind scripts/host.py the editor lives at /p/<id>/: the logo goes back to the project list.
  const logo = document.querySelector(".brand"), home = el("a", { className: "brand", href: "../../", title: "All projects" });
  home.setAttribute("aria-label", "All projects");
  home.append(...logo.childNodes); logo.replaceWith(home);
}
if (readOnly) {
  $("commentBtn").hidden = $("suggestBtn").hidden = true;   // Comments and suggestions need the edit role; the Review tab still lists them.
  for (const [id, why] of [["rebuildBtn", "Only people with the edit link can rebuild"], ["proseBtn", "Paragraph editing needs the edit link"]]) {
    $(id).disabled = true; $(id).title = why;
  }
  $("rebuildBtn").setAttribute("aria-label", "Rebuild from scratch (disabled: view only)");
}

// ---- splitter ----------------------------------------------------------------------------------------------
{
  const sp = $("splitter");
  const set = (pct) => { ui.split = Math.max(20, Math.min(80, pct)); applyAppearance(); saveUi(); };
  const move = (e) => { const r = $("work").getBoundingClientRect(), s = $("side").hidden ? 0 : $("side").offsetWidth; set(((e.clientX - r.left - s) / (r.width - s)) * 100); };
  sp.addEventListener("pointerdown", (e) => { sp.setPointerCapture(e.pointerId); sp.classList.add("drag"); document.body.style.userSelect = "none"; });
  sp.addEventListener("pointermove", (e) => sp.hasPointerCapture(e.pointerId) && move(e));
  sp.addEventListener("pointerup", (e) => { sp.releasePointerCapture(e.pointerId); sp.classList.remove("drag"); document.body.style.userSelect = ""; });
  sp.addEventListener("keydown", (e) => { if (e.key === "ArrowLeft") { set(ui.split - 3); e.preventDefault(); } else if (e.key === "ArrowRight") { set(ui.split + 3); e.preventDefault(); } });
  sp.addEventListener("dblclick", () => set(50));
}

// ---- bus, documents ---------------------------------------------------------------------------------------------
const channel = new Channel();
api.cid = channel.cid;
const collab = collabLibs && config.collab !== false ? new Collab(channel, api, {
  user: () => me, doc: () => cur, role, saveDelay: () => settings.autosave || 1000,
  onChange: (room) => {
    const key = [room.dirty, room.saving, room.error, room.isLeader].join();
    if (room.shown === key) return;
    room.shown = key; renderTabs(); showSaveState();
  },
  onRebind: (room) => {
    const tab = tabs.get(room.path);
    if (!tab) return;
    tab.state = collabState(tab);
    if (tab === active) { tab.fromDisk = true; view.setState(tab.state); tab.fromDisk = false; applyConfig(); }
  },
  onWarn: (text) => toast(el("span", { textContent: text })),
  onPresence: (users) => renderUsers(users),
}) : null;
channel.on("state", (data) => {
  const list = data.docs;
  docs = Object.fromEntries(list.map((d) => [d.name, d]));
  const names = list.map((d) => d.name);
  if ([...$("doc").options].map((o) => o.value).join("\n") !== names.join("\n")) $("doc").replaceChildren(...names.map((n) => new Option(n, n)));
  if (!docs[cur] || !booted) { booted = true; pick(docs[cur] ? cur : docs[store.get("doc")] ? store.get("doc") : names[0]); return; }
  $("doc").value = cur;
  const wasBuilding = lastStatus === "building";
  lastStatus = docs[cur].status;
  renderStatus();
  renderFocus();
  if (wantedVersion() !== pdfView.version) loadPdf();
  loadWarnings();   // Cheap: it fetches only when a build finished since the last look.
  if (wasBuilding && lastStatus !== "building") { loadOutline(); if (ui.drawer && ui.drawerTab === "log") loadLog(); if (ui.drawer && ui.drawerTab === "lint") loadLint(); }
});
let lastStatus = null, booted = false;
channel.on("fs", onFsEvent);
channel.on("review", (d) => { if (d.doc === cur) REV.changed(); });
channel.on("history", (d) => { if (d.doc === cur && ui.drawer && ui.drawerTab === "history") { clearTimeout(historyTimer); historyTimer = setTimeout(() => historyUi.load(), 400); } });
let historyTimer;
channel.on("forward", (b) => { if (b.doc !== cur) pick(b.doc); else if (shownFocus()) return; pdfView.reveal(b); });
channel.on("transport", (mode) => {
  document.documentElement.dataset.transport = mode;
  if (mode === "revoked") $("revoked").hidden = false;
  if (mode === "reconnecting") $("label").textContent = "Reconnecting...";
  else if (docs[cur]) renderStatus();
});

async function pick(name) {
  if (!name) return;
  const changing = cur && cur !== name;
  if (changing && [...tabs.values()].some((t) => t.dirty && !t.collab)) await Promise.all([...tabs.values()].filter((t) => t.dirty && !t.collab).map((t) => saveTab(t)));
  if (changing) await Promise.all([...tabs.values()].map((t) => t.collab?.leave()));
  cur = name; location.hash = encodeURIComponent(name); store.set("doc", name);
  $("doc").value = name; $("brandDoc").textContent = $("treeDoc").textContent = name.split("/").pop();
  focusView = null; renderFocus();
  tabs.clear(); active = null; files = []; emptyDirs = []; treeSel = null; outlineData = null; refs = { labels: {}, bib: {} };
  pdfView.version = null;
  warnKey = warnLoaded = null; warnings = []; renderWarnings();
  REV.reset(); historyUi.reset();
  renderStatus(); loadPdf(); loadWarnings(); await loadFiles(); await restoreTabs();
  REV.load(); if (ui.drawer && ui.drawerTab === "history") historyUi.load();
}

async function restoreTabs() {
  const saved = store.json("tabs:" + cur, null);
  const open = (saved?.open || []).filter((p) => files.some((f) => f.path === p));
  if (!open.length) open.push("main.tex");
  for (const p of open) await openFile(p, 0, { noFocus: true });
  const want = tabs.get(saved?.active) || [...tabs.values()][0];
  if (want) await activate(want, 0, { noFocus: true });
  if (settings.visual) loadRefs();
  if (ui.side && ui.sideTab === "refs") refsUi.load();
}

$("doc").onchange = (e) => pick(e.target.value);
window.addEventListener("beforeunload", (e) => { if ([...tabs.values()].some((t) => t.dirty && !t.collab)) { e.preventDefault(); e.returnValue = ""; } });
window.addEventListener("pagehide", () => { for (const t of tabs.values()) if (t.kind === "text" && (t.dirty || t.collab)) saveTab(t, { keepalive: true }); collab?.bye(); });

// ---- boot --------------------------------------------------------------------------------------------------------------
view = new EditorView({ state: EditorState.create({ doc: "" }), parent: $("cm") });
applyAppearance(); setSide(ui.side); setDrawer(ui.drawer); setProse(ui.prose); buildMenu(); showTips();
if (window.matchMedia("(max-width: 1000px)").matches) { ui.side = false; setSide(false); }
channel.start();
pushGrammarSettings();
window.__app = { REV, get view() { return view; }, tabs, settings, ui, channel, pdfView, get active() { return active; }, openFile, saveTab, setVisual, get docs() { return docs; }, get cur() { return cur; } };
