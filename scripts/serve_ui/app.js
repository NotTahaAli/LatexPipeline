import { state as S, view as V, language as L, commands as C, search as SR, autocomplete as AC, highlight as HL, stex, loadVim, loadEmacs, collabLibs } from "./libs.js";
import { Collab, PALETTE } from "./collab.js";
import { api, Channel } from "./api.js";
import { PdfView } from "./pdf.js";
import { visualField, visualTheme, visualEnv, refresh } from "./visual.js";
import * as prose from "./prose.js";

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
const settings = Object.assign({ autosave: 1000, theme: "system", keys: "default", font: 14, zoom: 0, visual: false, inverse: "app" }, store.json("settings", {}));
if (settings.fit === undefined) settings.fit = !settings.zoom;   // Fit the pane width until the person picks a zoom.
const saveSettings = () => store.set("settings", settings);

// ---- state ------------------------------------------------------------------------------
let docs = {}, cur = decodeURIComponent(location.hash.slice(1));
const tabs = new Map();      // path -> tab (per-file editor state)
let active = null, files = [], refs = { labels: {}, bib: {} }, outlineData = null, lastErrKey = null;
const firstVisit = store.get("ui") === null;
const ui = Object.assign({ side: firstVisit && window.innerWidth >= 1200, sideTab: "files", drawer: false, drawerTab: "problems", split: 50, prose: false }, store.json("ui", {}));
const saveUi = () => store.set("ui", ui);
let view;

// ---- who am I, what may I do ---------------------------------------------------------------------
const config = await api.config().catch(() => ({ role: "owner" }));
const role = config.role, readOnly = role === "view";
const me = store.json("user", null) || (() => { const u = { name: "Guest " + (100 + Math.floor(Math.random() * 900)), color: PALETTE[Math.floor(Math.random() * PALETTE.length)] }; store.set("user", u); return u; })();

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
const keysC = new Compartment(), visualC = new Compartment();

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
    errField,
    L.bracketMatching(), AC.closeBrackets(),
    AC.autocompletion({ override: [latexComplete], icons: false }),
    EditorState.allowMultipleSelections.of(true),
    L.StreamLanguage.define(stex),
    L.syntaxHighlighting(HL.classHighlighter),
    EditorView.lineWrapping,
    EditorView.contentAttributes.of({ "aria-label": `Editor: ${tab.path}`, spellcheck: "false", tabindex: "0" }),
    keymap.of([
      { key: "Mod-s", run: () => { saveTab(active); return true; }, preventDefault: true },
      ...AC.closeBracketsKeymap, ...C.defaultKeymap, ...SR.searchKeymap, ...(room ? collabLibs.yUndoManagerKeymap : C.historyKeymap), ...AC.completionKeymap,
    ]),
    SR.search({ top: true }),
    visualC.of([]),
    visualTheme,
    EditorView.updateListener.of(onUpdate),
  ];
}

function onUpdate(u) {
  if (!active) return;
  if (u.docChanged && !active.collab) {   // A collaborative tab's room tracks what still has to reach the disk.
    active.dirty = !u.state.doc.eq(active.savedDoc);
    if (!active.fromDisk) { renderTabs(); showSaveState(); scheduleAutosave(); }
  }
  if (u.docChanged || u.selectionSet) {
    const head = u.state.selection.main.head, line = u.state.doc.lineAt(head);
    $("cursorPos").textContent = `Ln ${line.number}, Col ${head - line.from + 1}`;
    if (ui.prose) syncProse(u.docChanged && u.transactions.some((t) => t.annotation(proseEdit)));
  }
}

const proseEdit = S.Annotation.define();

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
  ] });
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
  if (active && !same && active.kind === "text") active.state = view.state;
  active = tab;
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
  renderTabs(); renderTree(); showSaveState(); showBanner(); markErrors();
  collab?.hello(tab.path);
  if (ui.prose) syncProse();
}

function gotoLine(line) {
  const doc = view.state.doc, ln = doc.line(Math.max(1, Math.min(line, doc.lines)));
  view.dispatch({ selection: { anchor: ln.from }, effects: EditorView.scrollIntoView(ln.from, { y: "center" }) });
}

function closeTab(tab) {
  if (!tab.collab && tab.dirty && !confirm(`${tab.path} has unsaved changes. Close anyway?`)) return;
  tabs.delete(tab.path);
  tab.collab?.leave();
  if (active === tab) {
    active = null;
    const next = [...tabs.values()].pop();
    if (next) activate(next); else openFile("main.tex");
  }
  persistTabs(); renderTabs();
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
      view.dispatch({ changes: { from: 0, to: view.state.doc.length, insert: f.text }, selection: { anchor: head } });
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
    if (e.status === 409) { tab.conflict = "changed"; showBanner(); } else tab.error = e.message;
  } finally { tab.saving = false; renderTabs(); showSaveState(); }
}

// External changes arrive on the bus.
async function onFsEvent(msg) {
  if (msg.doc !== cur) return;
  loadFiles();
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
  try { files = (await api.files(cur)).files; } catch { return; }
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
  const rows = [];
  const walk = (node, prefix, depth) => {
    const entries = Object.entries(node).sort(([a, x], [b, y]) => (a.endsWith("/") ? 0 : 1) - (b.endsWith("/") ? 0 : 1) || a.localeCompare(b));
    for (const [name, v] of entries) {
      const pad = `${6 + depth * 14}px`;
      if (name.endsWith("/")) {
        const path = prefix + name, open = openDirs.has(path);
        const row = el("button", { className: "row", role: "treeitem", onclick: () => { open ? openDirs.delete(path) : openDirs.add(path); store.set("dirs", [...openDirs]); renderTree(); } }, icon("chev"), icon("folder"), el("span", { className: "name", textContent: name.slice(0, -1) }));
        row.querySelector(".ic").classList.add("chev");
        row.style.paddingLeft = pad; row.setAttribute("aria-expanded", String(open));
        rows.push(row);
        if (open) walk(v, path, depth + 1);
      } else {
        const text = v.kind === "text", img = v.kind === "image";
        const row = el("button", { className: "row" + (text || img ? "" : " dim"), role: "treeitem", title: v.path + (text || img ? "" : " (not editable)"), disabled: !(text || img), onclick: () => { if (narrow()) setSide(false); openFile(v.path); } }, el("span", { style: "width:16px" }), icon(img ? "image" : "file"), el("span", { className: "name", textContent: name }));
        row.style.paddingLeft = pad;
        row.setAttribute("aria-selected", String(active?.path === v.path));
        rows.push(row);
      }
    }
  };
  walk(root, "", 0);
  out.replaceChildren(...rows);
}

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

async function loadPdf() {
  const d = docs[cur];
  if (!d || !d.version) { pdfView.clear(); showEmpty(d); return; }
  $("empty").hidden = true;
  window.__loadStart = performance.now();
  await pdfView.load(api.pdfUrl(cur, d.version), d.version);
  if (pdfView.fitMode) pdfView.fit();
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
const toCursor = () => active?.kind === "text" && forwardSearch(active.path, view.state.doc.lineAt(view.state.selection.main.head).number);

async function inverse(e) {
  const page = e.target.closest(".page"); if (!page) return;
  const r = page.getBoundingClientRect(), z = pdfView.zoom;
  try {
    const res = await api.inverse(cur, +page.dataset.i + 1, (e.clientX - r.left) / z, (e.clientY - r.top) / z);
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
  bar.hidden = false;
  bar.firstElementChild.style.width = d.seconds ? Math.min(95, (elapsed / d.seconds) * 100) + "%" : "";
  bar.classList.toggle("indeterminate", !d.seconds);
  const e = $("emptyTime"); if (e) e.textContent = fmtSecs(elapsed);
}
let buildSeen = Date.now() / 1000;

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
  $("status").title = [d.finished && "Finished " + new Date(d.finished * 1000).toLocaleTimeString(), d.status === "failed" && d.version && "Showing last good PDF", "Click for problems and log"].filter(Boolean).join(". ");
  const errs = d.errors.length || (d.status === "failed" ? 1 : 0);
  $("errBadge").hidden = !errs; $("errBadge").textContent = `${errs} error${errs === 1 ? "" : "s"}`;
  $("warnBadge").hidden = !d.warnings; $("warnBadge").textContent = `${d.warnings} warning${d.warnings === 1 ? "" : "s"}`;
  $("warnBadge").title = "Show the build log, where LaTeX lists its warnings";
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
  const btn = el("button", { className: "head", onclick: () => { const open = btn.getAttribute("aria-expanded") !== "true"; btn.setAttribute("aria-expanded", String(open)); body.hidden = !open; } }, icon("chev"), ...head);
  btn.firstChild.classList.add("chev");
  btn.setAttribute("aria-expanded", String(open));
  const body = el("div", { className: "more", hidden: !open }, ...more);
  li.append(btn, body);
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
    if (e.excerpt) more.push(el("pre", { textContent: e.excerpt }));
    if (!more.length) more.push(el("span", { className: "mute", textContent: "No further details." }), logLink.cloneNode(true));
    list.push(disclosure([el("span", { className: "sev" }), el("span", { className: "msg", textContent: e.message }), whereLink(e.file, e.line)], more, i === 0));   // The first error opens by itself; the rest stay one line each.
  });
  if (!list.length && d.status === "failed") {
    loadExcerpt(d);
    list.push(disclosure([el("span", { className: "sev" }), el("span", { className: "msg", textContent: "The build failed, but LaTeX did not name a file and line." })],
      [...(excerpt ? [el("pre", { textContent: excerpt })] : []), logLink.cloneNode(true)], true));
  }
  if (!list.length) list.push(el("li", { className: "none" }, el("span", { className: "ok-mark", textContent: "No errors" }), el("span", { className: "mute", textContent: d.status === "building" ? " Building..." : " in the last build." }),
    ...(d.warnings ? [" ", el("button", { className: "link", textContent: `See ${d.warnings} ${d.warnings === 1 ? "warning" : "warnings"} in the log`, onclick: () => setDrawer(true, "log") })] : [])));
  $("problems").replaceChildren(...list);
}

async function loadLint() {
  $("lintList").replaceChildren(el("li", { className: "none", textContent: "Checking..." }));
  try {
    const { findings } = await api.lint(cur);
    $("lintCount").textContent = findings.length || "";
    $("lintList").replaceChildren(...(findings.length ? findings.map((f) =>
      disclosure([el("span", { className: "sev " + f.level }), el("span", { className: "msg", textContent: f.message }), ...(f.line ? [whereLink(f.path, f.line)] : [el("span", { className: "where mute", textContent: f.path })])],
        [el("span", { className: "mute", textContent: `${f.kind} - ${f.path}${f.line ? ":" + f.line : ""}` })])) : [el("li", { className: "none", textContent: "No lint findings." })]));
  } catch (e) { $("lintList").replaceChildren(el("li", { className: "none", textContent: e.message })); }
}

async function loadLog() {
  try { $("logText").textContent = await (await fetch(api.logUrl(cur))).text(); } catch { $("logText").textContent = "No log yet."; }
}

function setDrawer(open, tabName) {
  ui.drawer = open; if (tabName) ui.drawerTab = tabName; saveUi();
  $("drawer").hidden = !open; $("drawer").dataset.tab = ui.drawerTab;
  for (const b of ["status", "errBadge"]) $(b).setAttribute("aria-expanded", String(open));
  for (const n of ["problems", "lint", "log"]) {
    $("dtab-" + n).setAttribute("aria-selected", String(ui.drawerTab === n));
    $("dpanel-" + n).hidden = ui.drawerTab !== n;
  }
  if (open && ui.drawerTab === "lint") loadLint();
  if (open && ui.drawerTab === "log") loadLog();
}
for (const n of ["problems", "lint", "log"]) $("dtab-" + n).onclick = () => setDrawer(true, n);
const toggleDrawer = () => setDrawer(!ui.drawer);
$("status").onclick = toggleDrawer;
$("errBadge").onclick = () => setDrawer(true, "problems");
$("warnBadge").onclick = () => setDrawer(true, "log");
$("drawerClose").onclick = () => setDrawer(false);

// ---- sidebar toggles ----------------------------------------------------------------------------------
function setSide(open, tabName) {
  ui.side = open; if (tabName) ui.sideTab = tabName; saveUi();
  $("side").hidden = !open; $("sideBtn").setAttribute("aria-expanded", String(open));
  for (const n of ["files", "outline"]) {
    $("tab-" + n).setAttribute("aria-selected", String(ui.sideTab === n));
    $("panel-" + n).hidden = ui.sideTab !== n;
  }
  if (open) { loadFiles(); renderTree(); loadOutline(); }
}
$("sideBtn").onclick = () => setSide(!ui.side);
$("sideClose").onclick = () => setSide(false);
$("tab-files").onclick = () => setSide(true, "files");
$("tab-outline").onclick = () => setSide(true, "outline");

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
      view.dispatch({ changes: { from: span.from, to: span.to, insert: next }, annotations: proseEdit.of(true) });
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
  { id: "problems", title: "Toggle problems panel", keys: `${mod}+J`, run: () => toggleDrawer() },
  { id: "log", title: "Show build log", run: () => setDrawer(true, "log") },
  { id: "lint", title: "Show lint findings", run: () => setDrawer(true, "lint") },
  { id: "visual", title: "Toggle visual mode", keys: `${mod}+Alt+V`, run: () => setVisual(!settings.visual) },
  { id: "prose", edit: true, title: "Paragraph editor (rich text)", keys: `${mod}+Alt+P`, run: () => setProse(!ui.prose) },
  { id: "rebuild", edit: true, title: "Rebuild from scratch", run: () => !readOnly && api.rebuild(cur) },
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
  paletteItems = COMMANDS.filter((c) => !(readOnly && c.edit)).filter((c) => words.every((w) => c.title.toLowerCase().includes(w)));
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
for (const d of ["settings", "cheat"]) $(d).addEventListener("click", (e) => { if (e.target === $(d)) $(d).close(); });

$("cheatList").replaceChildren(...[...COMMANDS.filter((c) => c.keys && !(readOnly && c.edit)), { title: "Close dialogs and panels", keys: "Esc" }].flatMap((c) => [el("dt", { textContent: c.title }), el("dd", {}, el("kbd", { textContent: c.keys }))]));

function buildMenu() {
  const m = $("moreMenu");
  const entries = ["files", "outline", "-", "problems", "lint", "log", "-", ...(readOnly ? [] : ["prose"]), "visual", "-", ...(role === "owner" ? ["share"] : []), "theme", "settings", "-", "cheat", "tips", "palette"];
  m.replaceChildren(...entries.map((id) => {
    if (id === "-") return el("hr");
    const c = COMMANDS.find((x) => x.id === id);
    const b = el("button", { role: "menuitem", onclick: () => { closeMenu(); c.run(); } }, el("span", { textContent: c.title }), ...(c.keys ? [el("span", { className: "keys", textContent: c.keys })] : []));
    return b;
  }));
}
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
async function openShare() { $("share").showModal(); await refreshShare(); $("shareBody").querySelector("select, button")?.focus(); }
async function refreshShare() {
  clearTimeout(shareTimer);
  let info;
  try { info = await api.share(); } catch (e) { $("shareBody").textContent = e.message; return; }
  renderShare(info);
  $("shareBtn").classList.toggle("on", info.on);
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
      el("label", { htmlFor: "shareProvider", textContent: "Tunnel" }), select, msg, el("div", { className: "actions" }, start));
  } else if (info.status === "starting") {
    body.replaceChildren(el("p", { role: "status", textContent: `Starting ${info.provider}... this can take up to a minute.` }),
      el("div", { className: "actions" }, el("button", { type: "button", className: "btn", textContent: "Cancel", onclick: async () => { await api.shareStop(); refreshShare(); } })));
  } else {
    const notes = [info.provider === "ngrok" && "ngrok's free plan shows a warning page first; visitors click Visit Site once.", info.provider === "localtunnel" && "localtunnel may ask visitors for a tunnel password (your public IP)."].filter(Boolean);
    body.replaceChildren(
      el("p", { textContent: `Sharing ${info.doc || "the document"} through ${info.provider}.` }),
      linkRow("View link", info.links.view, "Read-only: the source, the PDF and the outline."),
      linkRow("Edit link", info.links.edit, "Can edit the files of this document and rebuild it. Shell escape stays off."),
      ...notes.map((t) => el("p", { className: "mute", textContent: t })),
      el("div", { className: "actions" },
        el("button", { type: "button", className: "btn", id: "shareRegen", textContent: "New links", title: "Revoke both links and make new ones", onclick: async () => { if (confirm("Everyone using the current links loses access. Make new links?")) { await api.shareRegenerate(); refreshShare(); } } }),
        el("button", { type: "button", className: "btn danger", id: "shareStop", textContent: "Stop sharing", onclick: async () => { await api.shareStop(); refreshShare(); } })));
  }
}
$("shareBtn").onclick = openShare;
$("share").addEventListener("click", (e) => { if (e.target === $("share")) $("share").close(); });
$("share").addEventListener("close", () => clearTimeout(shareTimer));
if (readOnly || role !== "owner") $("shareBtn").hidden = true;
else api.share().then((i) => $("shareBtn").classList.toggle("on", i.on)).catch(() => {});
// Roles: say who you are, and keep what you cannot do visible but disabled, with the reason.
if (role !== "owner") {
  const chip = $("roleChip");
  chip.hidden = false; chip.className = "role " + role;
  chip.replaceChildren(icon(readOnly ? "eye" : "share"), el("span", { textContent: readOnly ? "View only" : "Can edit" }));
  chip.title = readOnly ? "You opened a view link: you can read, scroll and jump between source and PDF, but not change anything. Ask the host for the edit link." : "You opened an edit link: changes are shared live. Only the host can share or stop sharing.";
  chip.tabIndex = 0;
}
if (readOnly) {
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
const collab = collabLibs && config.collab !== false ? new Collab(channel, api, {
  user: () => me, role, saveDelay: () => settings.autosave || 1000,
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
  if (docs[cur].version !== pdfView.version) loadPdf();
  if (wasBuilding && lastStatus !== "building") { loadOutline(); if (ui.drawer && ui.drawerTab === "log") loadLog(); if (ui.drawer && ui.drawerTab === "lint") loadLint(); }
});
let lastStatus = null, booted = false;
channel.on("fs", onFsEvent);
channel.on("forward", (b) => { if (b.doc !== cur) pick(b.doc); pdfView.reveal(b); });
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
  $("doc").value = name;
  tabs.clear(); active = null; files = []; outlineData = null; refs = { labels: {}, bib: {} };
  pdfView.version = null;
  renderStatus(); loadPdf(); await loadFiles(); await restoreTabs();
}

async function restoreTabs() {
  const saved = store.json("tabs:" + cur, null);
  const open = (saved?.open || []).filter((p) => files.some((f) => f.path === p));
  if (!open.length) open.push("main.tex");
  for (const p of open) await openFile(p, 0, { noFocus: true });
  const want = tabs.get(saved?.active) || [...tabs.values()][0];
  if (want) await activate(want, 0, { noFocus: true });
  if (settings.visual) loadRefs();
}

$("doc").onchange = (e) => pick(e.target.value);
window.addEventListener("beforeunload", (e) => { if ([...tabs.values()].some((t) => t.dirty && !t.collab)) { e.preventDefault(); e.returnValue = ""; } });
window.addEventListener("pagehide", () => { for (const t of tabs.values()) if (t.kind === "text" && (t.dirty || t.collab)) saveTab(t, { keepalive: true }); collab?.bye(); });

// ---- boot --------------------------------------------------------------------------------------------------------------
view = new EditorView({ state: EditorState.create({ doc: "" }), parent: $("cm") });
applyAppearance(); setSide(ui.side); setDrawer(ui.drawer); setProse(ui.prose); buildMenu(); showTips();
if (window.matchMedia("(max-width: 1000px)").matches) { ui.side = false; setSide(false); }
channel.start();
window.__app = { get view() { return view; }, tabs, settings, ui, channel, pdfView, get active() { return active; }, openFile, saveTab, setVisual, get docs() { return docs; }, get cur() { return cur; } };
