// Navigation in the source: go to definition (F12, Ctrl+click), find references (Shift+F12), rename a label (F2),
// label numbers from the last build next to \ref and \label, bookmarks (Ctrl+Alt+K, next Ctrl+Alt+L, previous
// Ctrl+Alt+J) and the cursor of every file kept per browser, package documentation on \usepackage, a KaTeX preview
// under display math, typeset selection, and the PDF following the cursor. The index (definitions, label numbers,
// floats, TODOs, the file graph) comes from POST api/symbols (scripts/navigate.py) with the unsaved text of open files.
import { symbolAt, numberSpots, docUrl, goodLabel, cropBox } from "./navcore.js";
import { scan } from "./latex.js";
import { replaceThroughRooms } from "./search.js";

const MISSING = {
  label: (n) => `No \\label{${n}} in this document.`,
  cite: (n) => `${n} is in none of the document's .bib files.`,
  env: (n) => `The environment ${n} is not defined in this document (LaTeX or a package defines it).`,
  command: (n) => `\\${n} is not defined in this document (LaTeX or a package defines it).`,
};

/**
 * deps = {S, V, loadKatex}. ctx = {api, el, doc(), view(), activePath(), canEdit, store, settings, texts() -> Promise<{path: text}>,
 * jump(path, line, col, len), forward(path, line) (quiet), saveAll(), room(path), pdf() -> Promise<pdf.js>, toast(msg), live(msg)}
 */
export function navSupport({ S, V, loadKatex }, ctx) {
  const { el } = ctx;
  let index = { defs: [], floats: [], todos: [], edges: [], numbers: {} }, timer = null;
  const listeners = [];
  const refreshFx = S.StateEffect.define();

  // ---- the index ----------------------------------------------------------------------------------------------
  async function load(uses) {
    const doc = ctx.doc();
    const data = await ctx.api.symbols(doc, { texts: await ctx.texts(), ...(uses ? { uses } : {}) });
    if (doc !== ctx.doc()) return null;
    index = data;
    for (const fn of listeners) fn(index);
    ctx.view()?.dispatch({ effects: refreshFx.of(null) });
    return data;
  }
  const refresh = (delay = 1500) => { clearTimeout(timer); timer = setTimeout(() => load().catch(() => {}), delay); };
  const under = (state, pos) => {
    const line = state.doc.lineAt(pos), s = symbolAt(line.text, pos - line.from);
    return s && { ...s, from: line.from + s.from, to: line.from + s.to };
  };
  const defOf = (sym) => index.defs.find((d) => d.kind === sym.kind && d.name === sym.name);
  const shown = (sym) => (sym.kind === "command" ? "\\" + sym.name : sym.name);

  // ---- one dialog for references, the file graph and typeset selections -----------------------------------------
  const dlg = document.getElementById("navDlg");
  document.getElementById("navClose").onclick = () => dlg.close();
  dlg.addEventListener("click", (e) => { if (e.target === dlg) dlg.close(); });
  function show(title, ...kids) {
    document.getElementById("navTitle").textContent = title;
    document.getElementById("navBody").replaceChildren(...kids);
    if (!dlg.open) dlg.showModal();
  }

  // ---- definition, references, rename ---------------------------------------------------------------------------
  async function definition(view, pos = view.state.selection.main.head) {
    const sym = under(view.state, pos);
    if (!sym) { ctx.toast("Put the cursor on a \\ref, \\cite, \\label, environment name or command of your own."); return; }
    if (sym.kind === "package") { window.open(docUrl(sym.name), "_blank", "noopener"); return; }
    try { await load(); } catch (e) { ctx.toast(e.message); return; }
    const def = defOf(sym);
    if (def) ctx.jump(def.file, def.line, def.col, def.len); else ctx.toast(MISSING[sym.kind](sym.name));
  }

  async function references(view, pos = view.state.selection.main.head) {
    const sym = under(view.state, pos);
    if (!sym || sym.kind === "package") { ctx.toast("Put the cursor on a \\ref, \\cite, \\label, environment name or command of your own."); return; }
    let data;
    try { data = await load(`${sym.kind}:${sym.name}`); } catch (e) { ctx.toast(e.message); return; }
    if (!data) return;
    const defs = data.defs.filter((d) => d.kind === sym.kind && d.name === sym.name);
    const row = (r, what) => el("li", {}, el("button", { type: "button", className: "link nav-hit", title: `${r.file}:${r.line}`, onclick: () => { dlg.close(); ctx.jump(r.file, r.line, r.col, r.len); } },
      el("span", { className: "mute", textContent: `${r.file}:${r.line}  ` }), el("span", { textContent: what || r.text || "" })));
    const n = data.uses.length;
    show(`References to ${shown(sym)}`,
      el("p", { className: "mute", textContent: `${defs.length ? "Defined here" : "Not defined in this document"}; ${n}${data.uses_truncated ? "+" : ""} ${n === 1 ? "use" : "uses"} (in the files as last saved, and the open files as typed).` }),
      el("ul", { className: "plist nav-list" }, ...defs.map((d) => row(d, "Definition")), ...data.uses.map((u) => row(u))));
    ctx.live(`${n} references to ${shown(sym)}`);
  }

  async function rename(view, pos = view.state.selection.main.head) {
    if (!ctx.canEdit) { ctx.toast("Renaming a label needs the edit role."); return; }
    const sym = under(view.state, pos);
    if (sym?.kind !== "label") { ctx.toast("Put the cursor on a \\label or \\ref to rename that label."); return; }
    const next = window.prompt(`Rename the label ${sym.name}: its \\label and every \\ref, \\cref, \\eqref, \\autoref and \\pageref to it become`, sym.name)?.trim();
    if (!next || next === sym.name) return;
    if (!goodLabel(next)) { ctx.toast("A label cannot hold spaces, braces, commas, %, # or backslashes."); return; }
    try { await load(); } catch { /* the check below is a courtesy; the rename itself is safe */ }
    if (index.defs.some((d) => d.kind === "label" && d.name === next) && !window.confirm(`${next} is already a label in this document. Rename anyway?`)) return;
    try {
      await ctx.saveAll();   // the server renames in the saved files
      const { count, missed } = await replaceThroughRooms((body) => ctx.api.rename(ctx.doc(), body), { old: sym.name, new: next }, ctx.room);
      const msg = `Renamed ${sym.name} to ${next} in ${count} ${count === 1 ? "place" : "places"}` + (missed.length ? `; not changed (open and busy): ${missed.join(", ")}` : ".");
      ctx.live(msg); ctx.toast(msg);
      refresh(300);
    } catch (e) { ctx.toast(e.message); }
  }

  // ---- label numbers (the last good build's .aux) next to \label and \ref --------------------------------------------
  class Num extends V.WidgetType {
    constructor(text) { super(); this.text = text; }
    eq(o) { return o.text === this.text; }
    toDOM() { const s = document.createElement("span"); s.className = "cm-navNum"; s.textContent = this.text; s.title = "Number in the last build"; return s; }
  }
  function numberDeco(view) {
    const nums = index.numbers || {};
    if (!Object.keys(nums).length) return V.Decoration.none;
    const b = new S.RangeSetBuilder();
    let last = 0;
    for (const { from, to } of view.visibleRanges) {
      for (let pos = from; pos <= to;) {
        const line = view.state.doc.lineAt(pos);
        if (line.number > last) {
          last = line.number;
          for (const spot of numberSpots(line.text)) b.add(line.from + spot.at, line.from + spot.at, V.Decoration.widget({ widget: new Num(spot.keys.map((k) => nums[k]?.number || "?").join(", ")), side: 1 }));
        }
        pos = line.to + 1;
      }
    }
    return b.finish();
  }
  const numbers = V.ViewPlugin.fromClass(class {
    constructor(view) { this.decorations = numberDeco(view); }
    update(u) { if (u.docChanged || u.viewportChanged || u.transactions.some((t) => t.effects.some((e) => e.is(refreshFx)))) this.decorations = numberDeco(u.view); }
  }, { decorations: (p) => p.decorations });

  // ---- typeset preview under display math (source mode; visual mode renders math in place) --------------------------
  class MathW extends V.WidgetType {
    constructor(tex) { super(); this.tex = tex; }
    eq(o) { return o.tex === this.tex; }
    get estimatedHeight() { return 48; }
    toDOM() {
      const d = document.createElement("div");
      d.className = "cm-navMath"; d.setAttribute("aria-hidden", "true"); d.textContent = this.tex;
      loadKatex().then((k) => { d.textContent = ""; k.render(this.tex, d, { displayMode: true, throwOnError: false }); }).catch(() => {});
      return d;
    }
  }
  const mathDeco = (doc) => doc.length > 500_000 ? V.Decoration.none : V.Decoration.set(scan(doc.toString()).math.filter((m) => m.display && m.tex.trim())
    .map((m) => V.Decoration.widget({ widget: new MathW(m.tex), block: true, side: 1 }).range(doc.lineAt(m.to).to)), true);
  const mathField = S.StateField.define({
    create: (state) => mathDeco(state.doc),
    update: (deco, tr) => (tr.docChanged ? mathDeco(tr.state.doc) : deco),
    provide: (f) => V.EditorView.decorations.from(f),
  });

  // ---- bookmarks and the cursor of each file, per document, in this browser ---------------------------------------
  const marksKey = () => "bookmarks:" + ctx.doc(), cursorKey = () => "cursor:" + ctx.doc();
  class Mark extends V.GutterMarker { toDOM() { const s = document.createElement("span"); s.className = "cm-navMark"; s.textContent = "◆"; return s; } }
  const MARK = new Mark();
  const markFx = S.StateEffect.define();   // value: a position; toggles the bookmark on its line
  const marksField = S.StateField.define({
    create: () => S.RangeSet.empty,
    update(set, tr) {
      set = set.map(tr.changes);
      for (const e of tr.effects) {
        if (!e.is(markFx)) continue;
        const line = tr.state.doc.lineAt(e.value);
        let had = false;
        set.between(line.from, line.to, () => { had = true; });
        set = had ? set.update({ filter: () => false, filterFrom: line.from, filterTo: line.to }) : set.update({ add: [MARK.range(line.from)] });
      }
      return set;
    },
  });
  const lines = (state) => { const out = new Set(); state.field(marksField, false)?.between(0, state.doc.length, (from) => { out.add(state.doc.lineAt(from).number); }); return [...out].sort((a, b) => a - b); };
  const pending = new Map();   // key -> timer: writes wait for a pause
  const later = (key, fn) => { clearTimeout(pending.get(key)); pending.set(key, setTimeout(fn, 400)); };
  function saveMarks(path, state) {
    const doc = ctx.doc(), ls = lines(state);
    later("m" + path, () => { const all = ctx.store.json("bookmarks:" + doc, {}); if (ls.length) all[path] = ls; else delete all[path]; ctx.store.set("bookmarks:" + doc, all); for (const fn of listeners) fn(index); });
  }
  function saveCursor(path, head) {
    const doc = ctx.doc();
    later("c" + path, () => { const all = ctx.store.json("cursor:" + doc, {}); all[path] = head; ctx.store.set("cursor:" + doc, all); });
  }
  function toggleMark(view) { view.dispatch({ effects: markFx.of(view.state.selection.main.head) }); return true; }
  function jumpMark(view, dir) {
    const at = [];
    view.state.field(marksField, false)?.between(0, view.state.doc.length, (from) => { at.push(from); });
    if (!at.length) { ctx.toast("No bookmarks in this file. Ctrl+Alt+K sets one on the cursor's line."); return true; }
    const here = view.state.doc.lineAt(view.state.selection.main.head).from;
    const to = dir > 0 ? at.find((p) => p > here) ?? at[0] : [...at].reverse().find((p) => p < here) ?? at[at.length - 1];
    view.dispatch({ selection: { anchor: to }, effects: V.EditorView.scrollIntoView(to, { y: "center" }) });
    return true;
  }

  // ---- the PDF follows the cursor (optional): forward search on a pause, and after each build -------------------------
  let followTimer, followed = null;
  function follow(path, state) {
    if (!ctx.settings.follow || !/\.tex$/i.test(path)) return;
    clearTimeout(followTimer);
    followTimer = setTimeout(() => {
      const line = state.doc.lineAt(state.selection.main.head).number;
      if (followed === `${path}:${line}`) return;
      followed = `${path}:${line}`;
      ctx.forward(path, line);
    }, 800);
  }

  // Per tab: keep bookmarks and the cursor, and follow it in the PDF.
  const tracker = (path) => V.ViewPlugin.define(() => ({
    update(u) {
      if (u.docChanged || u.transactions.some((t) => t.effects.some((e) => e.is(markFx)))) saveMarks(path, u.state);
      if (u.selectionSet || u.docChanged) saveCursor(path, u.state.selection.main.head);
      if (u.selectionSet && !u.docChanged) follow(path, u.state);
    },
  }));

  // ---- package documentation on \usepackage{...} (texdoc.org; the server runs no texdoc) -------------------------------
  const docsHover = V.hoverTooltip((view, pos) => {
    const sym = under(view.state, pos);
    if (sym?.kind !== "package") return null;
    return { pos: sym.from, end: sym.to, above: true, create: () => {
      const d = el("div", { className: "cm-texHover" }, el("a", { href: docUrl(sym.name), target: "_blank", rel: "noopener noreferrer", textContent: `Open the documentation of ${sym.name}` }),
        el("div", { className: "cm-texHover-mute", textContent: "texdoc.org, in a new tab (F12)" }));
      return { dom: d };
    } };
  });

  const clicks = V.EditorView.domEventHandlers({
    mousedown(e, view) {   // Ctrl/Cmd+click: definition. Where there is none to go to, it still adds a cursor.
      if (!(e.ctrlKey || e.metaKey) || e.button !== 0 || e.altKey || e.shiftKey) return false;
      const pos = view.posAtCoords({ x: e.clientX, y: e.clientY });
      const sym = pos == null ? null : under(view.state, pos);
      if (!sym || ((sym.kind === "command" || sym.kind === "env") && !defOf(sym))) return false;
      e.preventDefault();
      definition(view, pos);
      return true;
    },
  });

  // ---- typeset selection: the server sets it with the document's preamble; shown cropped ------------------------------
  async function typesetSelection(view) {
    if (!ctx.canEdit) { ctx.toast("Typesetting a selection needs the edit role."); return; }
    const sel = view.state.selection.main, text = view.state.sliceDoc(sel.from, sel.to);
    if (!text.trim()) { ctx.toast("Select the LaTeX to typeset first."); return; }
    const box = el("div", { className: "nav-typeset", textContent: "Typesetting..." });
    box.setAttribute("aria-live", "polite");
    show("Typeset selection", box, el("p", { className: "mute", textContent: "Set with the document's preamble on a page of its own: \\ref and \\cite show as ?? here." }));
    try {
      const res = await ctx.api.typeset(ctx.doc(), text);
      if (!(res.headers.get("Content-Type") || "").startsWith("application/pdf")) throw new Error((await res.json().catch(() => ({}))).error || res.statusText);
      const pdf = await (await ctx.pdf()).getDocument({ data: await res.arrayBuffer() }).promise;
      const page = await pdf.getPage(1), vp = page.getViewport({ scale: 2 });
      const full = Object.assign(document.createElement("canvas"), { width: vp.width, height: vp.height });
      const g = full.getContext("2d", { willReadFrequently: true });
      await page.render({ canvasContext: g, viewport: vp }).promise;
      const c = cropBox(g.getImageData(0, 0, full.width, full.height).data, full.width, full.height, 12) || { x: 0, y: 0, w: full.width, h: full.height };
      const out = Object.assign(document.createElement("canvas"), { width: c.w, height: c.h });
      out.getContext("2d").drawImage(full, c.x, c.y, c.w, c.h, 0, 0, c.w, c.h);
      out.style.width = c.w / 2 + "px";
      out.setAttribute("role", "img"); out.setAttribute("aria-label", "The selection, typeset");
      box.replaceChildren(out, ...(pdf.numPages > 1 ? [el("p", { className: "mute", textContent: `Page 1 of ${pdf.numPages}.` })] : []));
    } catch (e) { box.textContent = e.message; }
  }

  const numC = new S.Compartment(), mathC = new S.Compartment();
  const numExt = () => (ctx.settings.labelNumbers ? numbers : []);
  const mathExt = () => (ctx.settings.mathPreview && !ctx.settings.visual ? mathField : []);

  return {
    /** Extensions of one tab's editor state. */
    extension(tab) {
      const saved = ctx.store.json(marksKey(), {})[tab.path] || [];
      const tex = /\.(tex|ltx|sty|cls)$/i.test(tab.path);
      return [
        marksField.init((state) => S.RangeSet.of(saved.filter((n) => n <= state.doc.lines).map((n) => MARK.range(state.doc.line(n).from)), true)),
        V.gutter({ class: "cm-navMarks", markers: (v) => v.state.field(marksField), initialSpacer: () => MARK,
          domEventHandlers: { mousedown: (v, line) => { v.dispatch({ effects: markFx.of(line.from) }); return true; } } }),
        tracker(tab.path),
        V.keymap.of([
          { key: "F12", run: (v) => { definition(v); return true; } },
          { key: "Shift-F12", run: (v) => { references(v); return true; } },
          { key: "F2", run: (v) => { rename(v); return true; } },
          { key: "Mod-Alt-k", run: toggleMark, preventDefault: true },
          { key: "Mod-Alt-l", run: (v) => jumpMark(v, 1), preventDefault: true },
          { key: "Mod-Alt-j", run: (v) => jumpMark(v, -1), preventDefault: true },
        ]),
        ...(tex ? [clicks, docsHover, numC.of(numExt()), mathC.of(mathExt())] : []),
      ];
    },
    /** Effects that apply the settings (labelNumbers, mathPreview, visual) to the open editor. */
    reconfigure: () => [numC.reconfigure(numExt()), mathC.reconfigure(mathExt())],
    /** A tab opened for the first time: put the cursor where it was in this browser. */
    restoreCursor(path, view) {
      const head = ctx.store.json(cursorKey(), {})[path];
      if (head == null || head > view.state.doc.length) return;
      view.dispatch({ selection: { anchor: head }, effects: V.EditorView.scrollIntoView(head, { y: "center" }) });
    },
    afterBuild() { followed = null; const v = ctx.view(), p = ctx.activePath(); if (v && p) follow(p, v.state); },
    get index() { return index; },
    onIndex(fn) { listeners.push(fn); },
    load, refresh, show,
    /** Bookmarks of every file of the document: {path: [line numbers]}. */
    bookmarks: () => ctx.store.json(marksKey(), {}),
    run: { definition, references, rename, typesetSelection, toggleMark, next: (v) => jumpMark(v, 1), prev: (v) => jumpMark(v, -1) },
    docUrl, under,
  };
}
