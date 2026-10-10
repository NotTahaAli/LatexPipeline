// Review: comment threads anchored to text, and suggest mode (track changes). The server keeps both per document
// (serve.py, review.py) and only says "review changed" on the bus; this module refetches. Anchors are offsets plus
// quote and context (anchors.js): CodeMirror maps them through edits while a file is open, and an editor whose
// mapped anchor no longer matches its quote sends the new one back. Accepting a suggestion claims it on the server
// first (only one person can), then applies the edit through the open document, so co-editing rooms stay in sync.
// One multi-cursor edit is one suggestion with several ranges (anchors.js partsOf), accepted or rejected whole.
// Every bit of user text is rendered with textContent.
import { makeAnchor, locate, partsOf, placeGroups } from "./anchors.js";
import { ago } from "./history.js";

const FILTERS = [["comments", "Comments"], ["suggestions", "Suggestions"], ["resolved", "Resolved"]];
const IDLE_COMMIT = 1500;   // ms of no typing before a suggestion in progress is sent
const UNDO_GROUP = 600;     // ms: keystrokes closer together than this are one undo step in suggest mode
const UNDO_DEPTH = 100;
const clip = (s, n = 280) => (s.length > n ? s.slice(0, n) + "…" : s);

/**
 * ctx = {api, el, icon, doc(), canEdit, me() -> {name, key}, view(), activePath(), openFile(path, line, opts),
 *        showPanel(), toast(msg), live(msg), onCount(n),
 *        fromYjs() -> true while the co-editing binding applies a change that already happened to the shared text,
 *        hold(on) -> keep the editor's text from co-editors and the disk while an IME composition is in progress}
 */
export function reviewSupport(S, V, ctx) {
  const { el } = ctx;
  let data = { threads: [], suggestions: [], moderator: false };
  let filter = "comments", fileOnly = false, activeId = null, composing = null, suggesting = false, temp = 0;
  const replyOpen = new Set();

  // ---- editor: decorations for the active file -----------------------------------------------------------------
  const setItems = S.StateEffect.define();
  const itemsField = S.StateField.define({
    create: () => V.Decoration.none,
    update(deco, tr) {
      deco = deco.map(tr.changes);
      for (const e of tr.effects) if (e.is(setItems)) deco = e.value;
      return deco;
    },
    provide: (f) => V.EditorView.decorations.from(f),
  });

  class Inserted extends V.WidgetType {
    constructor(text, cls) { super(); this.text = text; this.cls = cls; }
    eq(o) { return o.text === this.text && o.cls === this.cls; }
    toDOM() { const s = document.createElement("span"); s.className = this.cls; s.textContent = this.text; return s; }
    ignoreEvent() { return false; }
  }
  const suggestionDeco = (from, to, insert, spec, extra = "") => [
    ...(to > from ? [V.Decoration.mark({ class: "cm-sug-del" + extra, ...spec }).range(from, to)] : []),
    ...(insert ? [V.Decoration.widget({ widget: new Inserted(insert, "cm-sug-ins" + extra), side: 1, ...spec }).range(to)] : []),
  ];

  /** Rebuild the active file's decorations from the anchors (after a load or a tab switch). */
  function refresh(view = ctx.view()) {
    if (!view) return;
    const path = ctx.activePath(), text = view.state.doc.toString(), marks = [];
    for (const t of data.threads) {
      if (t.path !== path || t.resolved) continue;
      const at = locate(text, t.anchor);
      if (at && at.to > at.from) marks.push(V.Decoration.mark({ class: "cm-cmt" + (t.id === activeId ? " active" : ""), id: t.id, kind: "c" }).range(at.from, at.to));
    }
    for (const s of data.suggestions) {
      if (s.path !== path) continue;
      partsOf(s).forEach((p, part) => {
        const at = locate(text, p.anchor);
        if (at) marks.push(...suggestionDeco(at.from, at.to, p.insert, { id: s.id, part, kind: "s" }, s.id === activeId ? " active" : ""));
      });
    }
    view.dispatch({ effects: setItems.of(V.Decoration.set(marks, true)) });
  }

  /**
   * id -> {from, to} as the open file has them now (mapped through every edit since the last refresh); a grouped
   * suggestion spans all of its parts. With byPart, the key is "id:part" and each range is its own.
   */
  function places(state, byPart = false) {
    const out = new Map();
    state.field(itemsField, false)?.between(0, state.doc.length, (from, to, value) => {
      const id = byPart ? `${value.spec.id}:${value.spec.part || 0}` : value.spec.id, old = out.get(id);
      const range = value.spec.widget ? { from: to, to } : { from, to };
      out.set(id, old ? { from: Math.min(old.from, range.from), to: Math.max(old.to, range.to) } : range);
    });
    return out;
  }

  // An anchor whose quote no longer finds the place the editor mapped it to is sent back, moved.
  let anchorTimer;
  function reanchorSoon() { clearTimeout(anchorTimer); anchorTimer = setTimeout(reanchor, 1500); }
  function reanchor() {
    const view = ctx.view();
    if (!ctx.canEdit || !view) return;
    const text = view.state.doc.toString(), path = ctx.activePath(), now = places(view.state, true), items = [];
    for (const item of [...data.threads, ...data.suggestions]) {
      if (item.path !== path || String(item.id).startsWith("tmp")) continue;
      const parts = item.comments ? [item] : [item, ...(item.more || [])];   // the objects that hold each anchor
      parts.forEach((p, part) => {
        const at = now.get(`${item.id}:${part}`);
        if (!at) return;
        const found = locate(text, p.anchor);
        if (found && found.from === at.from && found.to === at.to) return;
        p.anchor = makeAnchor(text, at.from, at.to);
        items.push({ id: item.id, ...(part ? { part } : {}), anchor: p.anchor });
      });
    }
    if (items.length) send({ op: "reanchor", items }).catch(() => {});
  }

  // Gutter: a mark on each line where a comment or a suggestion starts; clicking it opens the thread.
  class Marker extends V.GutterMarker {
    constructor(kind, ids) { super(); this.kind = kind; this.ids = ids; }
    eq(o) { return o.kind === this.kind && o.ids.join() === this.ids.join(); }
    toDOM() {
      const s = document.createElement("span");
      s.className = "cm-rv-mark " + this.kind;
      s.title = this.kind === "c" ? "Comment: click to open" : "Suggestion: click to open";
      s.setAttribute("aria-hidden", "true");
      return s;
    }
  }
  const gutter = V.gutter({
    class: "cm-rv-gutter",
    markers(view) {
      const lines = new Map();
      for (const [id, at] of places(view.state)) {
        const line = view.state.doc.lineAt(at.from).from, kind = data.threads.some((t) => t.id === id) ? "c" : "s";
        const entry = lines.get(line) || { kinds: new Set(), ids: [] };
        entry.kinds.add(kind); entry.ids.push(id); lines.set(line, entry);
      }
      return S.RangeSet.of([...lines].sort((a, b) => a[0] - b[0]).map(([pos, e]) => new Marker(e.kinds.has("c") ? "c" : "s", e.ids).range(pos)));
    },
    domEventHandlers: {
      mousedown(view, line) {
        const ids = [...places(view.state)].filter(([, at]) => at.from >= line.from && at.from <= line.to).map(([id]) => id);
        if (!ids.length) return false;
        focusItem(ids[0]);
        return true;
      },
    },
  });

  // ---- suggest mode: typing makes a suggestion instead of changing the text ---------------------------------------
  // The state of the open file: the draft being typed ({id, path, parts: [{from, to, insert}]}, one part per
  // cursor), and suggest mode's own undo stack. past/future hold {draft} (an earlier draft, mapped through every
  // change like the draft itself) and {sent: box} (a suggestion sent to the server; box.item once it is saved).
  const bypass = S.Annotation.define();   // edits that must go through even in suggest mode (accept, restore)
  const setSug = S.StateEffect.define();
  const finish = S.StateEffect.define();  // a draft is done: send it (box = {d, doc})
  let draftIds = 0;

  const mapDraft = (d, changes) => d && {
    ...d, parts: d.parts.map((p) => { const from = changes.mapPos(p.from, -1); return { ...p, from, to: Math.max(from, changes.mapPos(p.to, 1)) }; }),
  };
  const mapStep = (s, changes) => ("draft" in s ? { ...s, draft: mapDraft(s.draft, changes) } : s);
  const sugField = S.StateField.define({
    create: () => ({ draft: null, past: [], future: [], at: 0 }),
    update(v, tr) {
      for (const e of tr.effects) if (e.is(setSug)) return e.value;
      if (!tr.docChanged) return v;
      return { ...v, draft: mapDraft(v.draft, tr.changes), past: v.past.map((s) => mapStep(s, tr.changes)), future: v.future.map((s) => mapStep(s, tr.changes)) };
    },
    provide: (f) => V.EditorView.decorations.compute([f], (state) => {
      const d = state.field(f).draft;
      return d ? V.Decoration.set(d.parts.flatMap((p) => suggestionDeco(p.from, p.to, p.insert, { id: "draft", kind: "d" }, " draft")), true) : V.Decoration.none;
    }),
  });
  const draftOf = (state) => state.field(sugField, false)?.draft ?? null;
  const live = (d) => { const parts = d.parts.filter((p) => p.to > p.from || p.insert); return parts.length ? { ...d, parts } : null; };
  const cursors = (parts) => S.EditorSelection.create(parts.map((p) => S.EditorSelection.cursor(p.to)));
  const pushStep = (past, step) => [...past, step].slice(-UNDO_DEPTH);
  // A sent draft is one undo step (withdraw it): its typing steps (tagged with its id) are folded into that.
  const sentStep = (past, box) => pushStep(past.filter((s) => s.of !== box.d.id), { sent: box });

  /** The draft d grown by this edit (typing on, deleting on, with one cursor per part), or null if it does not continue d. */
  function extend(d, changes, st, tr) {
    const ranges = st.selection.ranges;
    if (!d || d.parts.length !== changes.length || ranges.length !== changes.length) return null;
    const parts = [];
    for (let i = 0; i < changes.length; i++) {
      const c = changes[i], p = d.parts[i], n = c.to - c.from;
      if (!ranges[i].empty || ranges[i].head !== p.to) return null;
      if (tr.isUserEvent("delete.backward") && !c.insert && c.to === p.to) parts.push(p.insert ? { ...p, insert: Array.from(p.insert).slice(0, -1).join("") } : { ...p, from: Math.max(0, p.from - n) });
      else if (tr.isUserEvent("delete.forward") && !c.insert && c.from === p.to) parts.push({ ...p, to: Math.min(st.doc.length, p.to + n) });
      else if (tr.isUserEvent("input") && c.from === c.to && c.from === p.to) parts.push({ ...p, insert: p.insert + c.insert });
      else return null;
    }
    return { ...d, parts };
  }

  // IME composition (CJK input, dead keys, phone keyboards): the browser owns the text being composed, so it goes
  // into the editor as usual, but held back from co-editors and the disk (ctx.hold); when the composition ends the
  // text is taken out again and typed once more, which suggest mode turns into a suggestion like any other typing.
  let ime = null;   // {base: the text before, changes: what the composition did to it}
  function endIme(view) {
    const p = ime;
    if (!p || !view || view.composing) return;
    ime = null;
    try {
      view.dispatch({ changes: p.changes.invert(p.base), annotations: [bypass.of(true), S.Transaction.addToHistory.of(false)] });
      if (view.state.doc.eq(p.base) && !p.changes.empty) view.dispatch({ changes: p.changes, userEvent: "input.type", scrollIntoView: true });
    } finally {
      ctx.hold?.(false);
    }
  }

  const filterTr = S.EditorState.transactionFilter.of((tr) => {
    // Every change while suggesting becomes a suggestion: typing, commands, panels. What goes through: edits that
    // must (accept, restore, reloads: the bypass annotation), and changes the co-editing binding applies because
    // the shared text already changed (another person's edit, a merge from disk: ctx.fromYjs, from the Yjs
    // transaction in progress, so no guessing from missing user events).
    if (!suggesting || !tr.docChanged || tr.annotation(bypass) || ctx.fromYjs?.()) return tr;
    if (ime || ctx.view()?.composing || tr.isUserEvent("input.type.compose")) {
      if (!ime) { ime = { base: tr.startState.doc, changes: tr.changes }; ctx.hold?.(true); }
      else ime.changes = ime.changes.compose(tr.changes);
      return tr;
    }
    const st = tr.startState, v = st.field(sugField, false), d = v?.draft ?? null, changes = [], now = Date.now();
    if (!v) return tr;
    tr.changes.iterChanges((from, to, _f, _t, ins) => changes.push({ from, to, insert: ins.toString() }));
    let past = v.past, next = extend(d, changes, st, tr), sel;
    const effects = [];
    if (next) {   // One undo step per burst of typing.
      if (now - v.at > UNDO_GROUP || past[past.length - 1]?.of !== d.id) past = pushStep(past, { draft: d, of: d.id });
      sel = cursors(next.parts);
    } else {
      if (d) { const box = { d, doc: st.doc }; effects.push(finish.of(box)); past = sentStep(past, box); }
      next = { id: ++draftIds, path: ctx.activePath(), parts: changes };
      past = pushStep(past, { draft: null, of: next.id });
      sel = cursors(changes);
    }
    next = live(next);
    effects.push(setSug.of({ draft: next, past, future: [], at: now }));
    return { effects, selection: sel, scrollIntoView: true };
  });

  let idle;
  const listener = V.EditorView.updateListener.of((u) => {
    if (u.docChanged) reanchorSoon();
    for (const tr of u.transactions) for (const e of tr.effects) if (e.is(finish)) commit(e.value);
    if (ime && !u.view.composing) setTimeout(() => endIme(ctx.view()), 0);
    const d = draftOf(u.state);
    if (!d || ime) return;
    const head = u.state.selection.main.head;
    if (u.selectionSet && !d.parts.some((p) => head >= p.from && head <= p.to)) { setTimeout(() => flush(ctx.view()), 0); return; }   // Moved away: send it now.
    if (draftOf(u.startState) !== d) { clearTimeout(idle); idle = setTimeout(() => flush(ctx.view()), IDLE_COMMIT); }
  });
  const imeEnd = V.EditorView.domEventHandlers({ compositionend: (_e, view) => { setTimeout(() => endIme(view), 0); return false; } });

  /** Send the suggestion being typed, if any (before a tab switch, on idle, when suggest mode goes off). */
  function flush(view = ctx.view()) {
    clearTimeout(idle);
    if (!view) return;
    if (ime) endIme(view);
    const v = view.state.field(sugField, false), d = v?.draft;
    if (!d) return;
    const box = { d, doc: view.state.doc };
    view.dispatch({ effects: [setSug.of({ ...v, draft: null, past: sentStep(v.past, box) }), finish.of(box)] });
  }

  /** Send a finished draft; box.item is the saved suggestion afterwards (undo withdraws it), box.dead if nothing came of it. */
  async function commit(box) {
    const text = box.doc.toString();
    const parts = box.d.parts.map((p) => ({ anchor: makeAnchor(text, p.from, p.to), insert: p.insert }));
    if (parts.every((p) => p.anchor.quote === p.insert)) { box.dead = true; return; }
    const [first, ...more] = parts, path = box.d.path;
    const body = { path, anchor: first.anchor, insert: first.insert, ...(more.length ? { more } : {}) };
    const item = { id: "tmp" + ++temp, ...body, name: ctx.me().name, mine: true, time: Date.now() / 1000 };
    data.suggestions.push(item);
    redraw();
    try {
      const r = await send({ op: "suggest", ...body });
      box.item = r.item;
      data.suggestions = data.suggestions.map((s) => (s === item ? r.item : s));
    } catch (e) {
      box.dead = true;
      data.suggestions = data.suggestions.filter((s) => s !== item);
      ctx.toast(`Suggestion not saved: ${e.message}`);
    }
    redraw();
  }

  // ---- suggest mode's undo and redo: your own drafts and suggestions only, never the shared text ------------------
  function undo(view) {
    if (!suggesting || !view) return false;
    if (ime) return true;
    let v = view.state.field(sugField, false);
    while (v?.past.length && v.past[v.past.length - 1].sent?.dead) v = { ...v, past: v.past.slice(0, -1) };   // never saved
    const step = v?.past[v.past.length - 1];
    if (!step) { ctx.live("Nothing to undo in suggest mode"); return true; }
    const past = v.past.slice(0, -1);
    if ("draft" in step) {
      view.dispatch({ effects: setSug.of({ ...v, draft: step.draft, past, future: [...v.future, { draft: v.draft, of: step.of }], at: 0 }), ...(step.draft ? { selection: cursors(step.draft.parts) } : {}) });
      ctx.live(step.draft ? "Suggestion shortened" : "Suggestion removed");
      return true;
    }
    const box = step.sent;
    if (!box.item) { ctx.toast("That suggestion is still being saved; undo again in a moment."); return true; }
    view.dispatch({ effects: setSug.of({ ...v, past, future: [...v.future, { sent: box }] }) });
    send({ op: "reject", ids: [box.item.id] }).then(() => { ctx.live("Suggestion withdrawn"); load(); },
      (e) => { box.dead = true; ctx.toast(`Not withdrawn: ${e.message}`); });
    return true;
  }

  function redo(view) {
    if (!suggesting || !view) return false;
    if (ime) return true;
    const v = view.state.field(sugField, false), step = v?.future[v.future.length - 1];
    if (!step || step.sent?.dead) { ctx.live("Nothing to redo in suggest mode"); return true; }
    const future = v.future.slice(0, -1);
    if ("draft" in step) {
      view.dispatch({ effects: setSug.of({ ...v, draft: step.draft, past: pushStep(v.past, { draft: v.draft, of: step.of }), future, at: 0 }), ...(step.draft ? { selection: cursors(step.draft.parts) } : {}) });
      return true;
    }
    // A withdrawn suggestion comes back with the same text, found again by its anchors.
    const old = step.sent.item, box = { d: null, item: null };
    view.dispatch({ effects: setSug.of({ ...v, past: pushStep(v.past, { sent: box }), future }) });
    const body = { path: old.path, anchor: old.anchor, insert: old.insert, ...(old.more ? { more: old.more } : {}) };
    send({ op: "suggest", ...body }).then((r) => { box.item = r.item; ctx.live("Suggestion restored"); load(); },
      (e) => { box.dead = true; ctx.toast(`Suggestion not restored: ${e.message}`); });
    return true;
  }
  const undoKeys = S.Prec.high(V.keymap.of([
    { key: "Mod-z", run: undo, preventDefault: true },
    { key: "Mod-y", run: redo, preventDefault: true },
    { key: "Mod-Shift-z", run: redo, preventDefault: true },
  ]));

  function setSuggest(on) {
    if (!ctx.canEdit) return;
    if (!on) flush();
    suggesting = !!on;
    render();
    ctx.live(suggesting ? "Suggest mode on: your edits become suggestions" : "Suggest mode off: edits change the text");
  }

  // ---- server -------------------------------------------------------------------------------------------------------
  const send = (body) => ctx.api.reviewOp(ctx.doc(), { ...body, name: ctx.me().name, key: ctx.me().key });

  let loadTimer, loading = 0;
  async function load() {
    const doc = ctx.doc(), run = ++loading;
    if (!doc) return;
    try {
      const fresh = await ctx.api.review(doc, ctx.me().key);
      if (run !== loading || doc !== ctx.doc()) return;
      const pending = data.suggestions.filter((s) => String(s.id).startsWith("tmp"));   // Not confirmed yet: keep showing.
      data = { ...fresh, suggestions: [...fresh.suggestions, ...pending] };
    } catch { return; }
    redraw();
  }
  const changed = () => { clearTimeout(loadTimer); loadTimer = setTimeout(load, 150); };

  function redraw() { refresh(); render(); }

  // ---- actions ---------------------------------------------------------------------------------------------------------
  async function run(body, ok) {
    try { const r = await send(body); if (ok) ctx.live(ok); await load(); return r; }
    catch (e) { ctx.toast(e.message); return null; }
  }

  /** Start a comment on the selection (or the cursor's line). */
  function startComment() {
    const view = ctx.view(), path = ctx.activePath();
    if (!ctx.canEdit || !view || !path) { ctx.toast(ctx.canEdit ? "Open a text file first." : "Commenting needs the edit role."); return; }
    let { from, to } = view.state.selection.main;
    if (from === to) { const line = view.state.doc.lineAt(from); from = line.from; to = line.to; }
    if (from === to) { ctx.toast("Select some text to comment on."); return; }
    flush(view);
    composing = { path, anchor: makeAnchor(view.state.doc.toString(), from, to) };
    filter = "comments";
    ctx.showPanel();
    render();
    root.querySelector(".rv-compose textarea")?.focus();
  }

  async function decide(ids, accept) {
    ids = ids.filter((id) => !String(id).startsWith("tmp"));
    if (!ids.length) return;
    const r = await run({ op: accept ? "accept" : "reject", ids });
    if (!r || !accept) { if (r) ctx.live(`${ids.length === 1 ? "Suggestion" : ids.length + " suggestions"} rejected`); return; }
    const byPath = new Map(), failed = [];
    for (const s of r.removed) (byPath.get(s.path) || byPath.set(s.path, []).get(s.path)).push(s);
    for (const [path, items] of byPath) {
      await ctx.openFile(path, 0, { noFocus: true });
      const view = ctx.view();
      if (ctx.activePath() !== path) { failed.push(...items); continue; }
      const { ok, failed: missed } = placeGroups(view.state.doc.toString(), items);   // a group applies whole or not at all
      failed.push(...missed);
      if (ok.length) view.dispatch({ changes: ok.map(({ from, to, insert }) => ({ from, to, insert })), annotations: bypass.of(true), userEvent: "input.review" });
    }
    if (failed.length) await send({ op: "release", ids: failed.map((s) => s.id) }).catch(() => {});   // Back as they were: same id and author.
    const applied = r.removed.length - failed.length;
    ctx.live(`${applied} ${applied === 1 ? "suggestion" : "suggestions"} accepted`);
    if (failed.length) ctx.toast(`${failed.length} ${failed.length === 1 ? "suggestion was" : "suggestions were"} not applied: the text changed or they overlap. They stay open.`);
    await load();
  }

  async function jump(item) {
    activeId = item.id;
    await ctx.openFile(item.path, 0, { noFocus: true });
    const view = ctx.view();
    if (ctx.activePath() !== item.path) return;
    const at = places(view.state).get(item.id) || locate(view.state.doc.toString(), item.anchor);
    refresh(view); render();
    if (!at) { ctx.toast("The text this refers to was changed or removed."); return; }
    view.dispatch({ selection: { anchor: at.from, head: at.to }, effects: V.EditorView.scrollIntoView(at.from, { y: "center" }) });
    view.focus();
  }

  function focusItem(id) {
    const thread = data.threads.find((t) => t.id === id);
    activeId = id;
    filter = thread ? (thread.resolved ? "resolved" : "comments") : "suggestions";
    ctx.showPanel();
    refresh(); render();
    const node = root.querySelector(`[data-id="${CSS.escape(id)}"]`);
    node?.scrollIntoView({ block: "nearest" });
    node?.querySelector("button")?.focus();
  }

  // ---- panel ---------------------------------------------------------------------------------------------------------
  let root = null;
  function mount(node) { root = node; render(); }

  const btn = (text, onclick, cls = "btn", title) => el("button", { type: "button", className: cls, textContent: text, onclick, ...(title ? { title } : {}) });
  function where(item) {
    const view = ctx.view(), at = item.path === ctx.activePath() && view ? places(view.state).get(item.id) : null;
    const label = at ? `${item.path}:${view.state.doc.lineAt(at.from).number}` : item.path;
    const a = el("button", { type: "button", className: "link where", textContent: label, title: "Show in the editor", onclick: () => jump(item) });
    return a;
  }
  const stamp = (t) => el("span", { className: "mute rv-time", textContent: ago(t), title: new Date(t * 1000).toLocaleString() });

  function textBox(label, onSend, onCancel, sendLabel) {
    const id = "rv-t" + Math.random().toString(36).slice(2, 8);
    const area = el("textarea", { id, rows: 3, maxLength: 4000, className: "rv-text" });
    const lab = el("label", { htmlFor: id, className: "sr", textContent: label });
    const go = btn(sendLabel, async () => { const text = area.value.trim(); if (!text) { area.focus(); return; } go.disabled = true; if (await onSend(text)) return; go.disabled = false; }, "btn primary");
    area.addEventListener("keydown", (e) => { if (e.key === "Enter" && (e.ctrlKey || e.metaKey)) { e.preventDefault(); go.click(); } else if (e.key === "Escape") { e.stopPropagation(); onCancel(); } });
    return el("div", { className: "rv-form" }, lab, area, el("div", { className: "rv-actions" }, go, btn("Cancel", onCancel, "btn ghost"), el("span", { className: "mute", textContent: "Ctrl+Enter sends" })));
  }

  function threadNode(t) {
    const li = el("li", { className: "rv-item" + (t.id === activeId ? " active" : "") });
    li.dataset.id = t.id;
    const quote = el("blockquote", { className: "rv-quote", textContent: clip(t.anchor.quote, 200) });
    const comments = t.comments.map((c) => el("div", { className: "rv-c" },
      el("div", { className: "rv-who" }, el("b", { textContent: c.name }), stamp(c.time),
        ...(ctx.canEdit && (c.mine || data.moderator) ? [btn("Delete", () => { if (confirm("Delete this comment?")) run({ op: "delete", thread: t.id, comment: c.id }, "Comment deleted"); }, "link rv-del", "Delete this comment")] : [])),
      el("p", { className: "rv-body", textContent: c.text })));
    const actions = [];
    if (ctx.canEdit) {
      if (!t.resolved) actions.push(btn("Reply", () => { replyOpen.add(t.id); render(); root.querySelector(`[data-id="${CSS.escape(t.id)}"] textarea`)?.focus(); }));
      actions.push(btn(t.resolved ? "Reopen" : "Resolve", () => run({ op: "resolve", thread: t.id, resolved: !t.resolved }, t.resolved ? "Thread reopened" : "Thread resolved")));
    }
    li.append(el("div", { className: "rv-head" }, where(t), ...(t.resolved ? [el("span", { className: "mute", textContent: `Resolved${t.resolved_by ? " by " + t.resolved_by : ""}` })] : [])), quote, ...comments);
    if (replyOpen.has(t.id) && ctx.canEdit && !t.resolved) {
      li.append(textBox("Reply", async (text) => { const ok = await run({ op: "reply", thread: t.id, text }, "Reply sent"); if (ok) { replyOpen.delete(t.id); render(); } return !!ok; },
        () => { replyOpen.delete(t.id); render(); }, "Reply"));
    } else if (actions.length) li.append(el("div", { className: "rv-actions" }, ...actions));
    return li;
  }

  function suggestionNode(s) {
    const li = el("li", { className: "rv-item" + (s.id === activeId ? " active" : "") });
    li.dataset.id = s.id;
    const parts = partsOf(s);
    const changes = parts.map(({ anchor: { quote }, insert }) => {
      const shown = el("span", {}, ...(quote ? [el("del", { textContent: clip(quote) })] : []), ...(quote && insert ? [" "] : []),
        ...(insert ? [el("ins", { textContent: clip(insert) })] : []));
      shown.setAttribute("aria-hidden", "true");
      const say = quote && insert ? `Replace "${clip(quote, 80)}" with "${clip(insert, 80)}"` : insert ? `Insert "${clip(insert, 80)}"` : `Delete "${clip(quote, 80)}"`;
      return el("p", { className: "rv-change" }, el("span", { className: "sr", textContent: say }), shown);
    });
    const pending = String(s.id).startsWith("tmp");
    li.append(el("div", { className: "rv-head" }, where(s), el("span", { className: "rv-who" }, el("b", { textContent: s.name }), stamp(s.time))),
      ...(parts.length > 1 ? [el("p", { className: "mute rv-group", textContent: `One edit in ${parts.length} places, accepted or rejected together:` })] : []), ...changes);
    if (ctx.canEdit) {
      const yes = btn("Accept", () => decide([s.id], true)), no = btn(s.mine ? "Withdraw" : "Reject", () => decide([s.id], false));
      yes.disabled = no.disabled = pending;
      li.append(el("div", { className: "rv-actions" }, yes, no, ...(pending ? [el("span", { className: "mute", textContent: "Saving..." })] : [])));
    }
    return li;
  }

  function render() {
    if (!root) return;
    const path = ctx.activePath(), here = (i) => !fileOnly || i.path === path;
    const open = data.threads.filter((t) => !t.resolved && here(t)), resolved = data.threads.filter((t) => t.resolved && here(t));
    const sugg = data.suggestions.filter(here);
    ctx.onCount?.(data.threads.filter((t) => !t.resolved).length + data.suggestions.length);
    const counts = { comments: open.length, suggestions: sugg.length, resolved: resolved.length };
    const chips = FILTERS.map(([id, label]) => {
      const b = el("button", { type: "button", className: "chip", textContent: `${label} ${counts[id]}`, onclick: () => { filter = id; render(); } });
      b.setAttribute("aria-pressed", String(filter === id));
      return b;
    });
    const only = el("input", { type: "checkbox", checked: fileOnly, id: "rvOnly", onchange: () => { fileOnly = only.checked; render(); } });
    const tools = [];
    if (ctx.canEdit) {
      const sw = btn("Suggest", () => ctx.toggleSuggest(), "chip rv-switch", "Suggest mode: your edits become suggestions that others accept or reject");
      sw.setAttribute("role", "switch"); sw.setAttribute("aria-checked", String(suggesting));
      tools.push(btn("Comment", startComment, "btn", "Comment on the selected text (Ctrl+Alt+M)"), sw);
      if (filter === "suggestions" && sugg.length) {
        tools.push(btn("Accept all", () => { if (confirm(`Accept ${sugg.length} ${sugg.length === 1 ? "suggestion" : "suggestions"}${fileOnly ? " in this file" : ""}?`)) decide(sugg.map((s) => s.id), true); }),
          btn("Reject all", () => { if (confirm(`Reject ${sugg.length} ${sugg.length === 1 ? "suggestion" : "suggestions"}${fileOnly ? " in this file" : ""}?`)) decide(sugg.map((s) => s.id), false); }));
      }
    }
    const bar = el("div", { className: "rv-bar" }, el("div", { className: "rv-chips", role: "group" }, ...chips),
      el("label", { className: "check rv-only", htmlFor: "rvOnly" }, only, "This file only"), el("span", { className: "spacer" }), ...tools);
    bar.firstChild.setAttribute("aria-label", "Show");
    const parts = [bar];
    if (composing) {
      parts.push(el("div", { className: "rv-compose" }, el("div", { className: "rv-head" }, el("b", { textContent: "New comment" }), el("span", { className: "mute", textContent: composing.path })),
        el("blockquote", { className: "rv-quote", textContent: clip(composing.anchor.quote, 200) }),
        textBox("Comment", async (text) => {
          const r = await run({ op: "comment", path: composing.path, anchor: composing.anchor, text }, "Comment added");
          if (r) { composing = null; activeId = r.item?.id; render(); }
          return !!r;
        }, () => { composing = null; render(); ctx.view()?.focus(); }, "Comment")));
    }
    const list = filter === "suggestions" ? sugg.map(suggestionNode) : (filter === "resolved" ? resolved : open).map(threadNode);
    const empty = { comments: ctx.canEdit ? "No open comments. Select text and press Comment (Ctrl+Alt+M)." : "No open comments.",
      suggestions: ctx.canEdit ? "No suggestions. Turn on Suggest and type: your edits become suggestions." : "No suggestions.", resolved: "No resolved comments." }[filter];
    parts.push(el("ul", { className: "plist rv-list" }, ...(list.length ? list : [el("li", { className: "none", textContent: empty })])));
    const keep = root.contains(document.activeElement) && document.activeElement.tagName === "TEXTAREA" ? document.activeElement : null;
    const draft = keep?.value, keepId = keep?.closest("[data-id]")?.dataset.id;
    root.replaceChildren(...parts);
    if (keep && draft) {   // A reload while typing a reply must not lose it.
      const again = keepId ? root.querySelector(`[data-id="${CSS.escape(keepId)}"] textarea`) : root.querySelector(".rv-compose textarea");
      if (again) { again.value = draft; again.focus(); }
    }
  }

  return {
    extension: [itemsField, sugField, filterTr, listener, imeEnd, undoKeys, gutter],
    bypass, mount, load, changed, refresh, flush, startComment, setSuggest, focusItem,
    get suggesting() { return suggesting; },
    reset() { data = { threads: [], suggestions: [], moderator: false }; activeId = null; composing = null; replyOpen.clear(); render(); },
  };
}
