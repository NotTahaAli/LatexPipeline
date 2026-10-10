// Review: comment threads anchored to text, and suggest mode (track changes). The server keeps both per document
// (serve.py, review.py) and only says "review changed" on the bus; this module refetches. Anchors are offsets plus
// quote and context (anchors.js): CodeMirror maps them through edits while a file is open, and an editor whose
// mapped anchor no longer matches its quote sends the new one back. Accepting a suggestion claims it on the server
// first (only one person can), then applies the edit through the open document, so co-editing rooms stay in sync.
// Every bit of user text is rendered with textContent.
import { makeAnchor, locate, disjoint } from "./anchors.js";
import { ago } from "./history.js";

const FILTERS = [["comments", "Comments"], ["suggestions", "Suggestions"], ["resolved", "Resolved"]];
const IDLE_COMMIT = 1500;   // ms of no typing before a suggestion in progress is sent
const clip = (s, n = 280) => (s.length > n ? s.slice(0, n) + "…" : s);

/**
 * ctx = {api, el, icon, doc(), canEdit, me() -> {name, key}, view(), activePath(), openFile(path, line, opts),
 *        showPanel(), toast(msg), live(msg), onCount(n)}
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
      const at = locate(text, s.anchor);
      if (at) marks.push(...suggestionDeco(at.from, at.to, s.insert, { id: s.id, kind: "s" }, s.id === activeId ? " active" : ""));
    }
    view.dispatch({ effects: setItems.of(V.Decoration.set(marks, true)) });
  }

  /** id -> {from, to} as the open file has them now (mapped through every edit since the last refresh). */
  function places(state) {
    const out = new Map();
    state.field(itemsField, false)?.between(0, state.doc.length, (from, to, value) => {
      const id = value.spec.id, old = out.get(id);
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
    const text = view.state.doc.toString(), path = ctx.activePath(), now = places(view.state), items = [];
    for (const item of [...data.threads, ...data.suggestions]) {
      const at = item.path === path && now.get(item.id);
      if (!at || String(item.id).startsWith("tmp")) continue;
      const found = locate(text, item.anchor);
      if (found && found.from === at.from && found.to === at.to) continue;
      item.anchor = makeAnchor(text, at.from, at.to);
      items.push({ id: item.id, anchor: item.anchor });
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
  const bypass = S.Annotation.define();   // edits that must go through even in suggest mode (accept, restore)
  const setDraft = S.StateEffect.define();
  const draftField = S.StateField.define({
    create: () => null,
    update(d, tr) {
      for (const e of tr.effects) if (e.is(setDraft)) return e.value;
      if (d && tr.docChanged) { const from = tr.changes.mapPos(d.from, -1); return { ...d, from, to: Math.max(from, tr.changes.mapPos(d.to, 1)) }; }
      return d;
    },
    provide: (f) => V.EditorView.decorations.compute([f], (state) => {
      const d = state.field(f);
      return d ? V.Decoration.set(suggestionDeco(d.from, d.to, d.insert, { id: "draft", kind: "d" }, " draft"), true) : V.Decoration.none;
    }),
  });

  const filterTr = S.EditorState.transactionFilter.of((tr) => {
    if (!suggesting || !tr.docChanged || tr.annotation(bypass) || !["input", "delete", "move"].some((e) => tr.isUserEvent(e))) return tr;
    const st = tr.startState, d = st.field(draftField, false), sel = st.selection.main, changes = [];
    tr.changes.iterChanges((from, to, _f, _t, ins) => changes.push({ from, to, insert: ins.toString() }));
    const c = changes[0], n = c.to - c.from;
    let next = null;
    if (changes.length === 1 && d && sel.empty && sel.head === d.to) {   // Keep extending the suggestion being typed.
      if (tr.isUserEvent("delete.backward") && !c.insert) next = d.insert ? { ...d, insert: Array.from(d.insert).slice(0, -1).join("") } : { ...d, from: Math.max(0, d.from - n) };
      else if (tr.isUserEvent("delete.forward") && !c.insert) next = { ...d, to: Math.min(st.doc.length, d.to + n) };
      else if (tr.isUserEvent("input") && c.from === c.to && c.from === d.to) next = { ...d, insert: d.insert + c.insert };
    }
    const done = [];
    if (!next) {
      if (d) done.push(d);
      const path = ctx.activePath();
      if (changes.length === 1) next = { ...c, path };
      else done.push(...changes.map((x) => ({ ...x, path })));
    }
    if (next && next.from === next.to && !next.insert) next = null;
    if (done.length) queueMicrotask(() => done.forEach((x) => commit(x, st.doc)));
    return { effects: setDraft.of(next), selection: { anchor: next ? next.to : sel.head }, scrollIntoView: true };
  });

  let idle;
  const listener = V.EditorView.updateListener.of((u) => {
    if (u.docChanged) reanchorSoon();
    const d = u.state.field(draftField, false);
    if (!d) return;
    const head = u.state.selection.main.head;
    if (u.selectionSet && (head < d.from || head > d.to)) { flush(u.view); return; }   // Moved away: send it now.
    if (u.startState.field(draftField, false) !== d) { clearTimeout(idle); idle = setTimeout(() => flush(ctx.view()), IDLE_COMMIT); }
  });

  /** Send the suggestion being typed, if any (before a tab switch, on idle, when suggest mode goes off). */
  function flush(view = ctx.view()) {
    clearTimeout(idle);
    const d = view?.state.field(draftField, false);
    if (!d) return;
    view.dispatch({ effects: setDraft.of(null) });
    commit(d, view.state.doc);
  }

  async function commit(d, doc) {
    const anchor = makeAnchor(doc.toString(), d.from, d.to);
    if (anchor.quote === d.insert) return;
    const item = { id: "tmp" + ++temp, path: d.path, anchor, insert: d.insert, name: ctx.me().name, mine: true, time: Date.now() / 1000 };
    data.suggestions.push(item);
    redraw();
    try {
      const r = await send({ op: "suggest", path: d.path, anchor, insert: d.insert });
      data.suggestions = data.suggestions.map((s) => (s === item ? r.item : s));
    } catch (e) {
      data.suggestions = data.suggestions.filter((s) => s !== item);
      ctx.toast(`Suggestion not saved: ${e.message}`);
    }
    redraw();
  }

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
      const text = view.state.doc.toString();
      const edits = items.map((s) => { const at = locate(text, s.anchor); if (!at) failed.push(s); return at && { ...at, insert: s.insert, s }; }).filter(Boolean);
      const { ok, clash } = disjoint(edits);
      failed.push(...clash.map((e) => e.s));
      if (ok.length) view.dispatch({ changes: ok.map(({ from, to, insert }) => ({ from, to, insert })), annotations: bypass.of(true), userEvent: "input.review" });
    }
    for (const s of failed) await send({ op: "suggest", path: s.path, anchor: s.anchor, insert: s.insert }).catch(() => {});   // Keep it open.
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
    const change = el("p", { className: "rv-change" },
      ...(s.anchor.quote ? [el("del", { textContent: clip(s.anchor.quote) })] : []),
      ...(s.anchor.quote && s.insert ? [" "] : []),
      ...(s.insert ? [el("ins", { textContent: clip(s.insert) })] : []));
    change.setAttribute("aria-label", s.anchor.quote && s.insert ? `Replace "${clip(s.anchor.quote, 80)}" with "${clip(s.insert, 80)}"` : s.insert ? `Insert "${clip(s.insert, 80)}"` : `Delete "${clip(s.anchor.quote, 80)}"`);
    const pending = String(s.id).startsWith("tmp");
    li.append(el("div", { className: "rv-head" }, where(s), el("span", { className: "rv-who" }, el("b", { textContent: s.name }), stamp(s.time))), change);
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
    extension: [itemsField, draftField, filterTr, listener, gutter],
    bypass, mount, load, changed, refresh, flush, startComment, setSuggest, focusItem,
    get suggesting() { return suggesting; },
    reset() { data = { threads: [], suggestions: [], moderator: false }; activeId = null; composing = null; replyOpen.clear(); render(); },
  };
}
