// Assistant panel: explain a build error, rewrite / shorten / fix / translate the selection, write LaTeX from a
// description, ask about the open file. The server (serve.py, or host.py when hosted) holds the key and sends the
// request to Anthropic; nothing comes back into the document until the person clicks Apply, and then it goes in
// through the editor like typing, so co-editing and autosave see a normal edit. Model output is text only (textContent).

const TASKS = { rewrite: "Rewrite", shorten: "Shorten", grammar: "Fix grammar", translate: "Translate", write: "Write LaTeX", explain: "Explain error", ask: "Question" };
const CONTEXT = 2000;   // characters of context before and after the selection or cursor

/**
 * ctx = {api, el, icon, doc(), role, readOnly, toast(msg), show() (open this panel), text(path) (open-tab or saved text),
 *        view(path) -> Promise<EditorView> (opens the file), current() -> {path, view} | null, errors() -> build errors, logUrl()}
 */
export function aiPanel(root, ctx) {
  const { el, icon } = ctx;
  let info = { enabled: false, reason: "Loading..." }, history = [], busy = false;
  const uid = (() => { let n = 0; return (p) => `ai-${p}-${++n}`; })();

  // ---- skeleton ------------------------------------------------------------------------------------------------
  const status = el("p", { className: "ai-note", role: "status" });
  const settingsBox = el("details", { className: "ai-settings", hidden: true });
  const hint = el("p", { className: "mute ai-hint", textContent: "Select text in the editor, then pick an action. Nothing changes until you click Apply." });
  const lang = el("input", { type: "text", id: uid("lang"), value: "English", maxLength: 40, autocomplete: "off" });
  const sel = el("div", { className: "ai-actions", role: "group" });
  sel.setAttribute("aria-label", "Selection actions");
  for (const t of ["rewrite", "shorten", "grammar"]) sel.append(el("button", { type: "button", className: "btn", textContent: TASKS[t], onclick: () => onSelection(t) }));
  const tr = el("div", { className: "ai-row" }, el("label", { htmlFor: lang.id, textContent: "Into" }), lang,
    el("button", { type: "button", className: "btn", textContent: "Translate", onclick: () => onSelection("translate") }));
  const prompt = el("textarea", { id: uid("prompt"), rows: 3, maxLength: 2000, placeholder: "A question about this file, or what to write (a 3-column table of results, the Gaussian integral, a TikZ flow chart...)" });
  const promptLabel = el("label", { htmlFor: prompt.id, className: "eyebrow", textContent: "Ask or describe" });
  const askRow = el("div", { className: "ai-actions" },
    el("button", { type: "button", className: "btn primary", textContent: "Ask", onclick: () => onAsk() }),
    el("button", { type: "button", className: "btn", textContent: "Write LaTeX at cursor", onclick: () => onWrite() }),
    el("button", { type: "button", className: "btn", textContent: "Explain first error", onclick: () => explain(ctx.errors()[0]) }));
  prompt.addEventListener("keydown", (e) => { if (e.key === "Enter" && (e.ctrlKey || e.metaKey)) { e.preventDefault(); onAsk(); } });
  const tools = el("div", { className: "ai-tools" }, hint, sel, tr, promptLabel, prompt, askRow);
  const log = el("ol", { className: "ai-log" });
  log.setAttribute("aria-label", "Assistant answers");
  log.setAttribute("aria-live", "polite");
  root.replaceChildren(el("div", { className: "ai-head" }, el("span", { className: "eyebrow", textContent: "Assistant" }), el("span", { className: "spacer" }),
    el("button", { type: "button", className: "mini", textContent: "Clear", onclick: () => { history = []; log.replaceChildren(); } })),
  status, settingsBox, tools, log);

  // ---- state from the server -------------------------------------------------------------------------------------
  async function load() {
    try { info = await ctx.api.aiInfo(); } catch (e) { info = { enabled: false, reason: e.message }; }
    render();
  }

  function render() {
    status.textContent = info.enabled ? `${info.notice || ""} Model: ${info.model}.` + (info.left != null ? ` ${info.left} requests left today.` : "") : (info.reason || "The assistant is off.");
    tools.hidden = !info.enabled || ctx.readOnly;
    for (const b of tools.querySelectorAll("button")) b.disabled = busy;
    settingsBox.hidden = ctx.role !== "owner" || info.hosted;
    if (!settingsBox.hidden) renderSettings();
  }

  function renderSettings() {
    const open = settingsBox.open;
    const on = el("input", { type: "checkbox", id: uid("on"), checked: !!info.on });
    const share = el("input", { type: "checkbox", id: uid("share"), checked: !!info.share });
    const model = el("select", { id: uid("model") }, ...[...new Set([...(info.models || []), info.model])].map((m) => new Option(m, m, false, m === info.model)));
    const key = el("input", { type: "password", id: uid("key"), autocomplete: "off", spellcheck: false, placeholder: info.key ? "(saved; type to replace)" : "sk-ant-..." });
    const msg = el("p", { className: "mute", role: "status" });
    const save = async (body) => {
      try { info = await ctx.api.aiSettings(body); msg.textContent = "Saved."; render(); } catch (e) { msg.textContent = e.message; }
    };
    on.onchange = () => {
      if (on.checked && !confirm("The assistant sends the text you ask about (selections, the open file, build logs) to Anthropic (api.anthropic.com), billed to your API key. Turn it on?")) { on.checked = false; return; }
      save({ enabled: on.checked });
    };
    share.onchange = () => {
      if (share.checked && !confirm("People with an edit link could then send text to Anthropic with your API key (up to 100 requests while this server runs). Allow it?")) { share.checked = false; return; }
      save({ share: share.checked });
    };
    model.onchange = () => save({ model: model.value });
    const keyNote = info.key === "environment" ? "The key comes from ANTHROPIC_API_KEY." : info.key ? "A key is saved in your user settings folder (readable only by you)." : "No key yet. Set ANTHROPIC_API_KEY, or paste one here.";
    settingsBox.replaceChildren(el("summary", { textContent: "Assistant settings" }),
      el("div", { className: "ai-form" },
        el("label", { className: "check" }, on, " Use the AI assistant (Anthropic)"),
        el("label", { htmlFor: model.id, textContent: "Model" }), model,
        el("label", { htmlFor: key.id, textContent: "API key" }),
        el("div", { className: "ai-row" }, key,
          el("button", { type: "button", className: "btn", textContent: "Save key", onclick: () => { if (key.value.trim()) save({ key: key.value.trim() }); key.value = ""; } }),
          ...(info.key === "settings" ? [el("button", { type: "button", className: "btn ghost", textContent: "Forget", onclick: () => save({ forget_key: true }) })] : [])),
        el("p", { className: "mute", textContent: keyNote + " It is never sent to the browser or to people you share with." }),
        el("label", { className: "check" }, share, " Let people with an edit link use it while sharing"),
        msg));
    settingsBox.open = open || !info.on;
  }

  // ---- requests ----------------------------------------------------------------------------------------------------
  async function run(label, body, show) {
    if (busy) return;
    busy = true; render();
    const item = el("li", { className: "ai-item" }, el("div", { className: "ai-q" }, el("b", { textContent: TASKS[body.task] }), el("span", { className: "mute", textContent: label ? " " + label : "" })),
      el("p", { className: "mute", textContent: "Thinking..." }));
    log.prepend(item);
    try {
      const r = await ctx.api.ai(ctx.doc(), body);
      item.lastChild.remove();
      if (r.answer) item.append(el("p", { className: "ai-a", textContent: r.answer }));
      show?.(item, r);
      for (const n of r.notes || []) item.append(el("p", { className: "refs-warn", textContent: n }));
      if (info.left != null) { info.left = Math.max(0, info.left - 1); }
      return r;
    } catch (e) {
      item.lastChild.replaceWith(el("p", { className: "refs-err", role: "alert", textContent: e.message }));
    } finally { busy = false; render(); }
  }

  function diff(oldText, newText) {
    const lines = (s, sign, cls) => (s ? s.split("\n") : []).map((t) => el("div", { className: cls, textContent: `${sign} ${t}` }));
    return el("pre", { className: "ai-diff", tabIndex: 0 }, ...lines(oldText, "-", "del"), ...lines(newText, "+", "add"));
  }

  /** Where `old` is now: still at from..to, or else its only occurrence. */
  function place(doc, from, to, old) {
    if (to <= doc.length && doc.sliceString(from, to) === old) return { from, to };
    const text = doc.toString(), at = text.indexOf(old);
    return old && at >= 0 && text.indexOf(old, at + 1) < 0 ? { from: at, to: at + old.length } : null;
  }

  function applyButton(label, item, go) {
    const b = el("button", { type: "button", className: "btn primary", textContent: label });
    const discard = el("button", { type: "button", className: "btn ghost", textContent: "Discard", onclick: () => row.remove() });
    const row = el("div", { className: "ai-actions" }, b, discard);
    b.onclick = async () => {
      b.disabled = true;
      const ok = await go();
      row.replaceChildren(el("span", { className: ok ? "ok-mark" : "refs-warn", textContent: ok ? "Applied" : "The text changed since; ask again." }));
    };
    item.append(row);
  }

  async function replaceIn(path, from, to, old, insert) {
    const view = await ctx.view(path);
    const at = place(view.state.doc, from, to, old);
    if (!at) return false;
    view.dispatch({ changes: { ...at, insert }, selection: { anchor: at.from, head: at.from + insert.length }, scrollIntoView: true, userEvent: "input.complete" });
    return true;
  }

  function selection() {
    const cur = ctx.current();
    if (!cur) { ctx.toast("Open a text file first."); return null; }
    const { state } = cur.view, r = state.selection.main;
    return { path: cur.path, from: r.from, to: r.to, text: state.sliceDoc(r.from, r.to),
      before: state.sliceDoc(Math.max(0, r.from - CONTEXT), r.from), after: state.sliceDoc(r.to, Math.min(state.doc.length, r.to + CONTEXT)) };
  }

  async function onSelection(task) {
    const s = selection();
    if (!s) return;
    if (!s.text.trim()) { ctx.toast("Select some text in the editor first."); return; }
    ctx.show();
    const body = { task, selection: { text: s.text, before: s.before, after: s.after }, ...(task === "translate" ? { language: lang.value.trim() } : {}) };
    await run(`${s.path}, ${s.text.length} characters`, body, (item, r) => {
      if (!r.text) return;
      item.append(diff(s.text, r.text));
      applyButton("Replace selection", item, () => replaceIn(s.path, s.from, s.to, s.text, r.text));
    });
  }

  async function onWrite() {
    const s = selection(), what = prompt.value.trim();
    if (!s) return;
    if (!what) { prompt.focus(); ctx.toast("Describe what to write first."); return; }
    await run(what.slice(0, 80), { task: "write", prompt: what, selection: { before: s.before, after: s.after } }, (item, r) => {
      if (!r.text) return;
      item.append(diff("", r.text));
      applyButton("Insert at cursor", item, async () => {
        const view = await ctx.view(s.path), at = view.state.selection.main.head;
        view.dispatch({ changes: { from: at, insert: r.text }, selection: { anchor: at, head: at + r.text.length }, scrollIntoView: true, userEvent: "input.complete" });
        return true;
      });
    });
  }

  async function onAsk() {
    const cur = ctx.current(), q = prompt.value.trim();
    if (!q) { prompt.focus(); return; }
    if (!cur) { ctx.toast("Open a text file first; the assistant reads it."); return; }
    let outline = [];
    try { outline = (await ctx.api.outline(ctx.doc())).items.map((it) => `${"  ".repeat(Math.max(0, it.level - 1))}${it.title} (${it.file}:${it.line})`); } catch { /* without outline */ }
    const r = await run(q.slice(0, 80), { task: "ask", prompt: q, outline, history, files: [{ path: cur.path, text: cur.view.state.doc.toString() }] });
    if (r) { history = [...history, { q, a: r.answer }].slice(-6); prompt.value = ""; }
  }

  /** Error {file, line, message, excerpt, hint} from the build: the log lines around it and the source around the line. */
  async function explain(e) {
    if (!e) { ctx.toast("The last build has no error with a file and line."); return; }
    ctx.show();
    const files = [];
    for (const path of [...new Set([e.file, "main.tex"])]) {
      try { files.push({ path, text: await ctx.text(path), ...(path === e.file ? { line: e.line } : {}) }); } catch { /* not readable: leave it out */ }
    }
    if (!files.length) { ctx.toast(`Cannot read ${e.file}.`); return; }
    let log = "";
    try {
      const rows = (await (await fetch(ctx.logUrl())).text()).split("\n");
      const i = rows.findIndex((r) => r.startsWith("!"));
      log = i >= 0 ? rows.slice(Math.max(0, i - 5), i + 30).join("\n").slice(0, 8000) : "";
    } catch { /* without the log */ }
    await run(`${e.file}:${e.line}`, { task: "explain", error: { message: e.message || "", excerpt: [e.excerpt, e.hint].filter(Boolean).join("\n").slice(0, 8000) }, log, files }, (item, r) => {
      for (const edit of r.edits || []) {
        item.append(el("p", { className: "mute", textContent: `Proposed change in ${edit.file}:` }), diff(edit.old, edit.new));
        applyButton("Apply", item, () => replaceIn(edit.file, edit.from, edit.to, edit.old, edit.new));
      }
    });
  }

  /** "Explain" button for a row of the Problems list, or nothing while the assistant is off. */
  function explainButton(e) {
    if (!info.enabled || ctx.readOnly || !e.file) return [];
    const b = el("button", { type: "button", className: "mini", title: "Ask the AI assistant what this error means and how to fix it" }, icon("search"), "Explain with AI");
    b.onclick = () => explain(e);
    return [el("div", { className: "fixes" }, b)];
  }

  const commands = [
    { id: "ai-explain", edit: true, title: "AI: explain the first build error", run: () => explain(ctx.errors()[0]) },
    { id: "ai-rewrite", edit: true, title: "AI: rewrite the selection", run: () => onSelection("rewrite") },
    { id: "ai-shorten", edit: true, title: "AI: shorten the selection", run: () => onSelection("shorten") },
    { id: "ai-grammar", edit: true, title: "AI: fix grammar in the selection", run: () => onSelection("grammar") },
    { id: "ai-translate", edit: true, title: "AI: translate the selection", run: () => onSelection("translate") },
    { id: "ai-write", edit: true, title: "AI: write LaTeX at the cursor...", run: () => { ctx.show(); prompt.focus(); } },
  ];

  return { load, explainButton, commands, get enabled() { return info.enabled; } };
}
