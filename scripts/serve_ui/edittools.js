// Editing tools for the source editor (the text logic is in edits.js): figure thumbnails and path completion,
// glossary completion, math prefix shortcuts, bold/italic toggles and wrapping, structural edits, user snippets,
// Unicode to LaTeX on paste, relative line numbers, soft wrap and rewrap, zen mode and .editorconfig.
// Every change goes through view.dispatch without the review bypass, so in suggest mode it becomes a suggestion like
// typing; a reviewer (always suggesting) gets nothing else, and a view-only editor gets nothing at all.
import * as E from "./edits.js";
import { matchEntries } from "./latex.js";
import { clipboardTable } from "./tablepaste.js";

const DEFAULTS = { relLines: false, wrap: true, wrapCol: 80, mathPrefix: false, unicodePaste: true };
const MOD = /Mac/.test(globalThis.navigator?.platform || "") ? "Cmd" : "Ctrl";
const GLS = /\\(?:[Gg]ls|GLS|[Aa]cr(?:short|long|full)|glsxtr)[A-Za-z]*\*?(?:\[[^\]]*\])*\{[^}]*/;

/**
 * libs = {S, V, L, AC}; ctx = {el, toast(...nodes), settings, saveSettings(), files(), refs(), path(), imageUrl(name),
 * engine(), mayChange() (edit role, or a reviewer while suggesting), suggesting()}.
 */
export function editTools({ S, V, L, AC }, ctx) {
  const { settings, el } = ctx;
  for (const [k, v] of Object.entries(DEFAULTS)) if (settings[k] === undefined) settings[k] = v;
  const linesC = new S.Compartment(), wrapC = new S.Compartment(), indentC = new S.Compartment();
  const say = (text) => ctx.toast(el("span", { textContent: text }));
  const can = (view) => view && !view.state.readOnly && ctx.mayChange();
  const dispatch = (view, changes, sel, userEvent = "input") =>
    view.dispatch({ changes, ...(sel ? { selection: sel } : {}), scrollIntoView: true, userEvent });

  // ---- snippets (per browser) ----
  const loadSnippets = () => { try { const s = localStorage.getItem("lp.snippets"); return s === null ? E.DEFAULT_SNIPPETS : E.cleanSnippets(JSON.parse(s)); } catch { return E.DEFAULT_SNIPPETS; } };
  let snippets = loadSnippets();
  const saveSnippets = () => { try { localStorage.setItem("lp.snippets", JSON.stringify(snippets)); } catch { /* private mode */ } };
  const expand = (view, s, from, to) => AC.snippet(E.snippetTemplate(s.body))(view, { label: s.trigger }, from, to);

  // ---- completion sources (beside latexComplete) ----
  const pathSource = (c) => {
    const m = c.matchBefore(/\\[A-Za-z]+\*?(?:\[[^\]]*\])*\{[^}]*/), p = m && E.PATH_CMD.exec(m.text);
    if (!p) return null;
    const typed = p[2].slice(p[2].lastIndexOf(",") + 1);
    return { from: c.pos - typed.length, validFor: /^[^},]*$/, options: E.pathOptions(p[1], ctx.files()).map((label) => ({ label, type: "file" })) };
  };
  const glossarySource = (c) => {
    const m = c.matchBefore(GLS);
    if (!m) return null;
    const from = m.from + m.text.lastIndexOf("{") + 1;
    return { from, filter: false, options: matchEntries(ctx.refs().glossary || {}, m.text.slice(from - m.from), (v) => v.text).slice(0, 200)
      .map(({ key, v }) => ({ label: key, detail: v.text.slice(0, 50), type: v.kind === "acronym" ? "constant" : "variable" })) };
  };
  const snippetSource = (c) => {
    if (!c.explicit || !ctx.mayChange()) return null;
    const w = c.matchBefore(/[A-Za-z][A-Za-z0-9]*/), from = w ? w.from : c.pos;
    if (from > 0 && c.state.sliceDoc(from - 1, from) === "\\") return null;
    return { from, options: snippets.map((s) => ({ label: s.trigger, detail: "snippet", type: "text", apply: (view, _c, a, b) => expand(view, s, a, b) })) };
  };

  // ---- commands ----
  const sel = (view) => view.state.selection.main;
  const text = (view) => view.state.doc.toString();
  const run = (fn) => (view) => { if (!can(view)) return false; fn(view); return true; };
  const toggle = (name) => run((view) => {
    const { from, to } = sel(view), r = E.toggleCommand(text(view), from, to, name);
    dispatch(view, r.changes, { anchor: r.anchor, head: r.head }, "input.format");
  });
  const ask = (label, value = "") => { const v = prompt(label, value); return v === null ? null : v.trim(); };
  const wrapEnv = run((view) => {
    const name = ask("Wrap the selection in environment:", "center");
    if (!name) return;
    if (!/^[A-Za-z]+\*?$/.test(name)) return say("An environment name is letters, with an optional *.");
    const { from, to } = sel(view), r = E.wrapEnv(text(view), from, to, name);
    dispatch(view, r.changes, { anchor: r.anchor, head: r.head });
  });
  const wrapCmd = run((view) => {
    const name = ask("Wrap the selection in command (without the backslash):", "textbf");
    if (!name) return;
    if (!/^[A-Za-z]+$/.test(name)) return say("A command name is letters only.");
    const { from, to } = sel(view), r = E.wrapCommand(text(view), from, to, name);
    dispatch(view, r.changes, { anchor: r.anchor, head: r.head });
  });
  const withEnv = (fn) => run((view) => {
    const env = E.surroundingEnv(text(view), sel(view).head);
    if (!env) return say("The cursor is not inside an environment.");
    fn(view, env);
  });
  const renameEnv = withEnv((view, env) => {
    const name = ask(`Change \\begin{${env.name}} ... \\end{${env.name}} to:`, env.name);
    if (!name || name === env.name) return;
    if (!/^[A-Za-z]+\*?$/.test(name)) return say("An environment name is letters, with an optional *.");
    dispatch(view, E.renameEnv(env, name));
  });
  const starEnv = withEnv((view, env) => dispatch(view, E.toggleStar(env)));
  const deleteEnv = withEnv((view, env) => dispatch(view, E.deleteEnv(text(view), env), null, "delete"));
  const deleteCmd = run((view) => {
    const c = E.deleteCommand(text(view), sel(view).head);
    if (!c) return say("The cursor is not inside a command's {argument}.");
    dispatch(view, c, null, "delete");
  });
  const leftRight = run((view) => {
    const c = E.toggleLeftRight(text(view), sel(view).head);
    if (!c) return say("No brackets around the cursor.");
    dispatch(view, c);
  });
  const rewrap = run((view) => {
    const doc = text(view), head = sel(view).head;
    let from = 0, to = doc.length;
    for (const m of doc.slice(0, head).matchAll(/\n[ \t]*\n/g)) from = m.index + m[0].length;
    const after = /\n[ \t]*\n/.exec(doc.slice(head));
    if (after) to = head + after.index;
    const par = doc.slice(from, to), out = E.rewrap(par, Math.max(20, +settings.wrapCol || 80));
    if (out === null) return say("This paragraph holds comments, environments or verbatim text; it was left as it is.");
    if (out !== par) dispatch(view, [{ from, to, insert: out }], null, "input.rewrap");
  });

  // ---- zen mode ----
  let zen = false;
  const exitBtn = el("button", { type: "button", className: "btn", id: "zenExit", textContent: "Exit zen mode (Esc)", hidden: true, onclick: () => setZen(false) });
  document.body.append(exitBtn);
  function setZen(on) {
    zen = !!on;
    document.documentElement.classList.toggle("zen", zen);
    exitBtn.hidden = !zen;
    if (zen) say("Zen mode: only the editor. Esc exits.");
    ctx.view()?.focus();
  }
  // After CodeMirror: Esc that closed a completion, a search or a snippet (defaultPrevented) stays there.
  document.addEventListener("keydown", (e) => { if (zen && e.key === "Escape" && !e.defaultPrevented && !document.querySelector("dialog[open]")) setZen(false); });

  // ---- view settings ----
  const lineNumbersExt = () => V.lineNumbers(settings.relLines ? {
    // Redrawn when the cursor changes line because highlightActiveLineGutter changes the gutter's line classes.
    formatNumber: (n, state) => { const cur = state.doc.lineAt(state.selection.main.head).number; return String(n === cur ? n : Math.abs(n - cur)); },
  } : {});
  const indentExt = (path) => {
    const c = E.editorconfigFor(ctx.refs().editorconfig, path || "");
    return [c.indentUnit ? L.indentUnit.of(c.indentUnit) : [], c.tabSize ? S.EditorState.tabSize.of(c.tabSize) : []];
  };

  // ---- extensions ----
  const thumbnail = V.hoverTooltip((view, pos) => {
    const line = view.state.doc.lineAt(pos);
    for (const m of line.text.matchAll(/\\includegraphics\*?(?:\[[^\]]*\])*\{([^}]+)\}/g)) {
      const from = line.from + m.index, to = from + m[0].length;
      if (pos < from || pos > to) continue;
      return { pos: from, end: to, above: true, create: () => {
        const box = el("div", { className: "cm-texHover et-thumb" }), note = el("div", { className: "cm-texHover-mute", textContent: m[1] });
        const img = el("img", { src: ctx.imageUrl(m[1].trim()), alt: `Preview of ${m[1]}`, onerror: () => { img.remove(); note.textContent = `${m[1]}: no preview (PDF and EPS figures show in the PDF only)`; } });
        box.append(img, note);
        return { dom: box };
      } };
    }
    return null;
  });
  const mathPrefix = V.EditorView.inputHandler.of((view, from, to, ins) => {
    if (!settings.mathPrefix || ins.length !== 1 || from !== to || !can(view)) return false;
    const x = E.prefixExpansion(view.state.sliceDoc(Math.max(0, from - 3), from), ins);
    if (!x) return false;
    const before = view.state.sliceDoc(Math.max(0, from - 20000), from);
    if (!E.inMath(before, before.length)) return false;
    view.dispatch({ changes: { from: from - x.len, to, insert: x.insert }, selection: { anchor: from - x.len + x.insert.length }, userEvent: "input.type" });
    return true;
  });
  const unicodePaste = V.EditorView.domEventHandlers({
    paste(e, view) {
      const data = e.clipboardData, plain = (data?.getData("text/plain") || "").replace(/\r\n?/g, "\n");
      if (!settings.unicodePaste || !plain || !/[^\x00-\x7f]/.test(plain) || data.files?.length || !can(view) || !/\.(tex|ltx)$/i.test(ctx.path() || "")) return false;
      if (clipboardTable({ html: data.getData("text/html"), text: plain })) return false;   // the table toast offers its own conversion
      const from = view.state.selection.main.from, before = view.state.sliceDoc(Math.max(0, from - 20000), from);
      const latex = E.unicodeToLatex(plain, { math: E.inMath(before, before.length), pdflatex: !["xelatex", "lualatex"].includes(ctx.engine()) });
      if (latex === plain) return false;
      setTimeout(() => {
        const go = el("button", { type: "button", className: "link tp-go", textContent: "Convert to LaTeX" });
        go.onclick = () => {
          // shortcut: the pasted text must still be where it went; an edit before it in between makes this refuse.
          if (view.state.sliceDoc(from, from + plain.length) !== plain) return say("The pasted text moved or changed; paste again.");
          view.dispatch({ changes: { from, to: from + plain.length, insert: latex }, selection: { anchor: from + latex.length }, userEvent: "input.paste" });
          say("Converted the pasted characters to LaTeX.");
          view.focus();
        };
        ctx.toast(el("span", { textContent: "The pasted text has Unicode characters." }), go);
      }, 0);
      return false;   // the plain paste goes ahead; the toast offers the conversion
    },
  });
  const keys = S.Prec.high(V.keymap.of([
    { key: "Mod-b", run: toggle("textbf") },
    { key: "Mod-i", run: toggle("emph") },
    { key: "Alt-q", run: rewrap },
    { key: "Tab", run: (view) => {
      const { head, empty } = sel(view);
      if (!empty || AC.hasNextSnippetField(view.state) || AC.completionStatus(view.state) === "active" || !can(view)) return false;
      const line = view.state.doc.lineAt(head), s = E.snippetAt(line.text.slice(0, head - line.from), snippets);
      if (!s) return false;
      expand(view, s, head - s.trigger.length, head);
      return true;
    } },
  ]));

  const ED = !ctx.reviewer;   // reviewers see the changing commands too: their changes become suggestions
  const commands = [
    { id: "bold", edit: ED, title: "Bold: \\textbf on/off", keys: `${MOD}+B (editor)`, run: () => toggle("textbf")(ctx.view()) },
    { id: "emph", edit: ED, title: "Emphasis: \\emph on/off", keys: `${MOD}+I`, run: () => toggle("emph")(ctx.view()) },
    { id: "wrapenv", edit: ED, title: "Wrap selection in environment...", run: () => wrapEnv(ctx.view()) },
    { id: "wrapcmd", edit: ED, title: "Wrap selection in command...", run: () => wrapCmd(ctx.view()) },
    { id: "envrename", edit: ED, title: "Change surrounding environment...", run: () => renameEnv(ctx.view()) },
    { id: "envdelete", edit: ED, title: "Delete surrounding environment (keep content)", run: () => deleteEnv(ctx.view()) },
    { id: "cmddelete", edit: ED, title: "Delete surrounding command (keep content)", run: () => deleteCmd(ctx.view()) },
    { id: "envstar", edit: ED, title: "Toggle starred environment", run: () => starEnv(ctx.view()) },
    { id: "leftright", edit: ED, title: "Toggle \\left / \\right", run: () => leftRight(ctx.view()) },
    { id: "rewrap", edit: ED, title: "Rewrap paragraph", keys: "Alt+Q", run: () => rewrap(ctx.view()) },
    { id: "snippets", edit: ED, title: "Edit snippets...", run: () => openSnippets() },
    { id: "rellines", get title() { return `Relative line numbers: ${settings.relLines ? "on" : "off"} (change)`; }, run: () => { settings.relLines = !settings.relLines; ctx.saveSettings(); ctx.applyConfig(); } },
    { id: "softwrap", get title() { return `Soft wrap: ${settings.wrap ? "on" : "off"} (change)`; }, run: () => { settings.wrap = !settings.wrap; ctx.saveSettings(); ctx.applyConfig(); } },
    { id: "zen", title: "Zen mode (only the editor)", run: () => setZen(!zen) },
  ];

  // ---- settings: a section of the Settings dialog, and the snippet editor ----
  function settingsSection(box) {
    const check = (key, label) => {
      const input = el("input", { type: "checkbox", checked: !!settings[key], onchange: () => { settings[key] = input.checked; ctx.saveSettings(); ctx.applyConfig(); } });
      return el("label", { className: "check wide" }, input, " " + label);
    };
    const col = el("input", { id: "sWrapCol", type: "number", min: 20, max: 200, step: 1, value: settings.wrapCol,
      onchange: () => { settings.wrapCol = Math.max(20, Math.min(200, +col.value || 80)); ctx.saveSettings(); } });
    box.replaceChildren(el("summary", { textContent: "Editing" }), el("div", { className: "fields" },
      el("div", { className: "field" }, el("label", { htmlFor: "sWrapCol", textContent: "Rewrap width (Alt+Q)" }), col),
      check("wrap", "Soft wrap long lines"),
      check("relLines", "Relative line numbers (distance from the cursor's line)"),
      check("mathPrefix", "Math shortcuts: in math, @ and a key types a symbol (@a \\alpha, @b \\beta, @8 \\infty; the README has the table)"),
      check("unicodePaste", "Offer to turn pasted Unicode into LaTeX (quotes, dashes; accents for pdflatex; Greek in math)"),
      el("div", { className: "field" }, el("button", { type: "button", className: "btn ghost", textContent: "Edit snippets...", onclick: () => openSnippets() }))));
  }
  function openSnippets() {
    const dlg = document.getElementById("snippetsDlg");
    const list = el("div", { className: "et-snips" });
    const row = (s, i) => {
      const t = el("input", { type: "text", id: `snipT${i}`, value: s.trigger, maxLength: 30, autocomplete: "off", spellcheck: false });
      const b = el("textarea", { id: `snipB${i}`, value: s.body, rows: Math.min(8, s.body.split("\n").length + 1), spellcheck: false });
      t.onchange = () => { if (/^[A-Za-z][A-Za-z0-9]*$/.test(t.value)) { s.trigger = t.value; saveSnippets(); } else { t.value = s.trigger; say("A trigger is a word: letters, then letters or digits."); } };
      b.onchange = () => { if (b.value) { s.body = b.value; saveSnippets(); } };
      const del = el("button", { type: "button", className: "btn ghost", textContent: "Delete", onclick: () => { snippets.splice(i, 1); saveSnippets(); draw(); } });
      del.setAttribute("aria-label", `Delete snippet ${s.trigger}`);
      return el("div", { className: "et-snip" }, el("div", { className: "field" }, el("label", { htmlFor: t.id, textContent: "Trigger" }), t),
        el("div", { className: "field wide" }, el("label", { htmlFor: b.id, textContent: "Body (${1:default} marks a tab stop)" }), b), del);
    };
    const draw = () => list.replaceChildren(...snippets.map(row));
    snippets = loadSnippets();
    draw();
    const close = el("button", { type: "button", className: "icon", onclick: () => dlg.close() });
    close.setAttribute("aria-label", "Close snippets");
    close.innerHTML = '<svg class="ic"><use href="#i-x"/></svg>';   // a fixed string, no user text
    dlg.replaceChildren(el("form", { method: "dialog" },
      el("div", { className: "dlg-head" }, el("h2", { id: "snippetsTitle", textContent: "Snippets" }), close),
      el("p", { className: "mute et-snip-note", textContent: "Type a trigger word and press Tab, or press Ctrl+Space for the list. Tab and Shift+Tab move between the tab stops. Snippets are kept in this browser." }),
      list,
      el("div", { className: "actions" },
        el("button", { type: "button", className: "btn ghost", textContent: "Reset to defaults", onclick: () => { if (confirm("Replace your snippets with the defaults?")) { snippets = E.DEFAULT_SNIPPETS.map((s) => ({ ...s })); saveSnippets(); draw(); } } }),
        el("button", { type: "button", className: "btn", textContent: "Add snippet", onclick: () => { snippets.push({ trigger: "new" + (snippets.length + 1), body: "${1}" }); saveSnippets(); draw(); list.lastChild?.querySelector("input")?.focus(); } }),
        el("button", { className: "btn primary", value: "close", textContent: "Done" }))));
    dlg.onclick = (e) => { if (e.target === dlg) dlg.close(); };
    if (!dlg.open) dlg.showModal();
  }

  return {
    sources: [pathSource, glossarySource, snippetSource],
    extension: [keys, linesC.of(lineNumbersExt()), wrapC.of(settings.wrap ? V.EditorView.lineWrapping : []), indentC.of([]), thumbnail, mathPrefix, unicodePaste],
    /** Effects for applyConfig: line numbers, wrap and the .editorconfig of the active file. */
    reconfigure: (path) => [linesC.reconfigure(lineNumbersExt()), wrapC.reconfigure(settings.wrap ? V.EditorView.lineWrapping : []), indentC.reconfigure(indentExt(path))],
    /** An explicit save: .editorconfig trailing white space (not on lines with a cursor) and final newline. */
    beforeSave(view, path) {
      const c = E.editorconfigFor(ctx.refs().editorconfig, path || "");
      if (!(c.trim || c.finalNewline) || !can(view) || ctx.suggesting()) return;
      const keep = new Set(view.state.selection.ranges.map((r) => view.state.doc.lineAt(r.head).number));
      const changes = E.saveFixes(text(view), c, keep);
      if (changes.length) view.dispatch({ changes, userEvent: "input.editorconfig" });
    },
    commands, settingsSection, setZen, get zen() { return zen; },
  };
}
