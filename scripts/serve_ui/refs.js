// References panel: every .bib entry of the document, where it is cited, and what is wrong with it.
// The server parses (POST api/bib, bibfix.py); edits come back as splices that go into the open editor document,
// like bib.js's Apply, so co-editing stays consistent and the normal save path writes the file.
import { addBibLookup } from "./bib.js";
import { zoteroUi } from "./zotero.js";

// Fields shown first for each type (the server's lint list wins where it has one), then the usual optional ones.
const TYPES = {
  article: [["author", "title", "journal", "year"], ["volume", "number", "pages", "month", "doi", "url", "note"]],
  book: [["author", "title", "publisher", "year"], ["editor", "edition", "address", "isbn", "doi", "url", "note"]],
  inproceedings: [["author", "title", "booktitle", "year"], ["editor", "pages", "publisher", "address", "doi", "url", "note"]],
  incollection: [["author", "title", "booktitle", "publisher", "year"], ["editor", "pages", "address", "doi", "url", "note"]],
  misc: [["author", "title", "year"], ["howpublished", "url", "urldate", "note"]],
  online: [["title", "url", "year"], ["author", "urldate", "organization", "note"]],
  phdthesis: [["author", "title", "school", "year"], ["address", "month", "url", "note"]],
  mastersthesis: [["author", "title", "school", "year"], ["address", "month", "url", "note"]],
  thesis: [["author", "title", "institution", "type", "year"], ["address", "url", "note"]],
  techreport: [["author", "title", "institution", "year"], ["number", "address", "url", "note"]],
};
const STOP = new Set(["the", "and", "for", "with", "from", "into", "über", "towards", "toward", "about", "using", "via", "what", "when", "how", "why", "are", "its", "this", "that", "some"]);
const SORTS = [["author", "Author"], ["year-desc", "Newest"], ["year", "Oldest"], ["title", "Title"], ["key", "Key"], ["cites", "Most cited"], ["file", "File"]];
const FILTERS = [["all", "All"], ["cited", "Cited"], ["unused", "Unused"], ["missing", "Missing"], ["problems", "Problems"]];

const fold = (s) => (s || "").normalize("NFD").replace(/[̀-ͯ]/g, "").replace(/\\[a-zA-Z]+\s*|[{}\\]/g, "");
const lastName = (author) => {
  const first = fold(author).split(/\s+and\s+/i)[0].trim();
  return (first.includes(",") ? first.split(",")[0] : first.split(/\s+/).pop() || "").replace(/[^A-Za-z0-9-]/g, "");
};
const authorLabel = (f) => {
  const names = fold(f.author || f.editor).split(/\s+and\s+/i).filter(Boolean);
  if (!names.length) return "";
  return lastName(names[0]) + (names.length > 2 ? " et al." : names.length === 2 ? ` & ${lastName(names[1])}` : "");
};
const yearOf = (f) => (f.year || f.date || "").match(/\d{4}/)?.[0] || "";

/** AuthorYearWord, unique among `taken` (a, b, c... appended). */
export function suggestKey(fields, taken) {
  const word = fold(fields.title).split(/[^A-Za-z0-9]+/).find((w) => w.length > 2 && !STOP.has(w.toLowerCase())) || "";
  const base = (lastName(fields.author || fields.editor) || "Ref") + yearOf(fields) + (word ? word[0].toUpperCase() + word.slice(1).toLowerCase() : "");
  let key = base;
  for (let i = 0; taken.has(key); i++) key = base + (i < 26 ? String.fromCharCode(97 + i) : i);
  return key;
}

/**
 * ctx = {api, el, icon, doc(), readOnly, text(path), openTexts(), splice(path, from, to, insert, expect), insertCite(keys),
 *        jump(path, line, col), toast(msg), newFile(path), bib (bib.js context), changed()}
 */
export function refsPanel(root, ctx) {
  const { el, icon } = ctx;
  const S = { data: null, scope: "", filter: "all", sort: "author", q: "", sel: new Set(), open: new Set(), form: null, busy: false };
  const uid = (() => { let n = 0; return (p) => `refs-${p}-${++n}`; })();

  // ---- skeleton ------------------------------------------------------------------------------------------------
  const search = el("input", { type: "search", className: "refs-q", placeholder: "Filter: key, author, title, year", autocomplete: "off", spellcheck: false });
  search.setAttribute("aria-label", "Filter references");
  const scope = el("select", { className: "refs-scope", title: "Which .bib files to show" });
  scope.setAttribute("aria-label", "Bibliography files");
  const addBtn = el("button", { type: "button", className: "icon", title: "New reference", hidden: ctx.readOnly }, icon("plus"));
  addBtn.setAttribute("aria-label", "New reference");
  const zotActs = ctx.readOnly ? [] : [action("Sync from Zotero...", () => zot.sync()), action("Zotero settings...", () => zot.settings())];
  zotActs.forEach((b) => { b.hidden = true; });   // Shown once the server says this viewer may sync (and configure).
  const more = details("More reference actions", [
    ...(ctx.readOnly ? [] : [action("Import BibTeX, RIS or DOI...", () => openImport()), ...zotActs, action("New .bib file...", () => newBib())]),
    action("Refresh", () => load()),
  ]);
  const chips = el("div", { className: "refs-chips", role: "group" });
  chips.setAttribute("aria-label", "Show");
  const sort = el("select", { className: "refs-sort" }, ...SORTS.map(([v, t]) => new Option(t, v)));
  sort.setAttribute("aria-label", "Sort by");
  const selBar = el("div", { className: "refs-selbar", role: "region", hidden: true });
  selBar.setAttribute("aria-label", "Selected references");
  const formBox = el("div", { className: "refs-form-box", hidden: true });
  const notes = el("div", { className: "refs-notes", role: "status" });
  const list = el("ul", { className: "plist refs-list" });
  list.setAttribute("aria-label", "References");
  root.replaceChildren(
    el("div", { className: "refs-bar", role: "toolbar" }, search, addBtn, more),
    el("div", { className: "refs-row" }, scope, sort),
    chips, selBar, formBox, notes, list);
  root.querySelector(".refs-bar").setAttribute("aria-label", "References");

  const zot = zoteroUi({ ...ctx, box: formBox, data: () => S.data, scope: () => S.scope, uid, authorLabel, yearOf, reload: () => load() });
  if (zotActs.length) zot.probe().then((info) => { zotActs[0].hidden = !info?.can_sync; zotActs[1].hidden = !(info?.can_configure || (info?.hosted && info.can_sync)); });
  search.oninput = () => { S.q = search.value; render(); };
  scope.onchange = () => { S.scope = scope.value; render(); };
  sort.onchange = () => { S.sort = sort.value; render(); };
  addBtn.onclick = () => openForm({});
  list.addEventListener("keydown", (e) => {   // Up/Down move between rows, so a long list is not a long Tab walk.
    if (e.key !== "ArrowDown" && e.key !== "ArrowUp") return;
    const heads = [...list.querySelectorAll(".head")], i = heads.indexOf(document.activeElement);
    if (i < 0) return;
    e.preventDefault();
    heads[Math.max(0, Math.min(heads.length - 1, i + (e.key === "ArrowDown" ? 1 : -1)))].focus();
  });

  function action(label, run) { return el("button", { type: "button", className: "refs-act", textContent: label, onclick: (e) => { e.target.closest("details").open = false; run(); } }); }
  function details(label, items) {   // Overflow menu: a native disclosure, so keyboard and screen readers just work.
    const summary = el("summary", { className: "icon", title: label }, icon("more"));
    summary.setAttribute("aria-label", label);
    const d = el("details", { className: "refs-ovf" }, summary, el("div", { className: "refs-ovf-menu" }, ...items));
    d.addEventListener("keydown", (e) => { if (e.key === "Escape" && d.open) { e.stopPropagation(); d.open = false; summary.focus(); } });
    return d;
  }
  document.addEventListener("click", (e) => { for (const d of root.querySelectorAll("details.refs-ovf[open]")) if (!d.contains(e.target)) d.open = false; });

  // ---- data ----------------------------------------------------------------------------------------------------------
  async function fetchData() { return ctx.api.bib(ctx.doc(), await ctx.openTexts()); }
  async function load() {
    if (!ctx.doc()) return;
    try { S.data = await fetchData(); } catch (e) { notes.replaceChildren(el("p", { className: "mute", textContent: e.message })); return; }
    if (S.scope && !S.data.files.some((f) => f.path === S.scope)) S.scope = "";
    render();
  }

  function derive() {
    const d = S.data, byKey = new Map();
    for (const e of d.entries) (byKey.get(e.key) || byKey.set(e.key, []).get(e.key)).push(e);
    const missing = Object.keys(d.citations).filter((k) => !byKey.has(k)).sort();
    return { byKey, missing };
  }
  const cites = (key) => S.data.citations[key] || [];
  const isUnused = (e) => !S.data.nocite_all && !cites(e.key).length;
  const problemsOf = (e, byKey) => [...(e.missing.length ? [`lacks ${e.missing.join(", ")}`] : []), ...(byKey.get(e.key).length > 1 ? ["duplicate key"] : [])];
  const matches = (e) => {
    if (!S.q.trim()) return true;
    const hay = fold([e.key, e.fields.author, e.fields.editor, e.fields.title, yearOf(e.fields)].join(" ")).toLowerCase();
    return fold(S.q).toLowerCase().split(/\s+/).filter(Boolean).every((w) => hay.includes(w));
  };
  const sorters = {
    author: (a, b) => (authorLabel(a.fields) || "~").localeCompare(authorLabel(b.fields) || "~") || yearOf(a.fields).localeCompare(yearOf(b.fields)),
    "year-desc": (a, b) => yearOf(b.fields).localeCompare(yearOf(a.fields)), year: (a, b) => (yearOf(a.fields) || "9999").localeCompare(yearOf(b.fields) || "9999"),
    title: (a, b) => fold(a.fields.title).localeCompare(fold(b.fields.title)), key: (a, b) => a.key.localeCompare(b.key),
    cites: (a, b) => cites(b.key).length - cites(a.key).length, file: (a, b) => a.file.localeCompare(b.file) || a.line - b.line,
  };

  // ---- render --------------------------------------------------------------------------------------------------------
  function render() {
    const d = S.data; if (!d) return;
    const { byKey, missing } = derive();
    scope.replaceChildren(new Option(`All .bib files (${d.files.length})`, ""), ...d.files.map((f) => new Option(`${f.path} (${f.entries})${f.used ? "" : " - not used by the document"}`, f.path)));
    scope.value = S.scope;
    const inScope = d.entries.filter((e) => !S.scope || e.file === S.scope);
    const shownMissing = S.scope ? [] : missing;
    const counts = { all: inScope.length + shownMissing.length, cited: inScope.filter((e) => cites(e.key).length).length, unused: inScope.filter(isUnused).length, missing: shownMissing.length, problems: inScope.filter((e) => problemsOf(e, byKey).length).length };
    chips.replaceChildren(...FILTERS.map(([id, label]) => {
      const b = el("button", { type: "button", className: "chip", textContent: `${label} ${counts[id]}`, onclick: () => { S.filter = id; render(); } });
      b.setAttribute("aria-pressed", String(S.filter === id));
      return b;
    }));
    const keep = { all: () => true, cited: (e) => cites(e.key).length, unused: isUnused, missing: () => false, problems: (e) => problemsOf(e, byKey).length }[S.filter];
    const rows = inScope.filter((e) => keep(e) && matches(e)).sort(sorters[S.sort]);
    const missRows = (S.filter === "all" || S.filter === "missing") ? shownMissing.filter((k) => !S.q.trim() || fold(k).toLowerCase().includes(fold(S.q).toLowerCase())) : [];
    notes.replaceChildren(
      ...d.problems.map((p) => el("p", { className: "refs-warn", textContent: p })),
      ...(d.nocite_all ? [el("p", { className: "mute", textContent: "\\nocite{*} is on: every entry is printed, so none counts as unused." })] : []),
      ...(!d.files.length ? [el("p", { className: "mute" }, "No .bib file in this document yet. ", ...(ctx.readOnly ? [] : [el("button", { type: "button", className: "link", textContent: "Create references.bib", onclick: () => newBib("references.bib") })]))] : []));
    list.replaceChildren(...missRows.map(missingRow), ...rows.map((e) => entryRow(e, byKey)),
      ...(!rows.length && !missRows.length && d.files.length ? [el("li", { className: "none", textContent: S.q ? "Nothing matches the filter." : "No references here." })] : []));
    renderSel();
  }

  function badge(text, kind, title) { return el("span", { className: `rbadge ${kind}`, textContent: text, title: title || text }); }

  function rowShell(head, body, key, opened) {   // The same shape as the lint rows, so bib.js's Look up fits in.
    const li = el("li", { className: "ref" });
    const btn = el("button", { type: "button", className: "head" }, icon("chev"), ...head);
    btn.firstChild.classList.add("chev");
    const more = el("div", { className: "more", hidden: !opened, id: uid("more") }, ...body);
    btn.setAttribute("aria-expanded", String(opened));
    btn.setAttribute("aria-controls", more.id);
    btn.onclick = () => { const open = btn.getAttribute("aria-expanded") !== "true"; btn.setAttribute("aria-expanded", String(open)); more.hidden = !open; S.open[open ? "add" : "delete"](key); };
    li.append(el("div", { className: "hrow" }, btn), more);
    return li;
  }

  function citeList(key) {
    const list = cites(key);
    if (!list.length) return [el("p", { className: "mute", textContent: S.data.nocite_all ? "Printed through \\nocite{*}; not cited in the text." : "Not cited anywhere." })];
    return [el("p", { className: "refs-sub", textContent: `Cited ${list.length} ${list.length === 1 ? "time" : "times"}:` }),
      el("ul", { className: "refs-cites" }, ...list.map((c) => el("li", {}, el("a", { href: "#", className: "where", textContent: `${c.file}:${c.line}`, onclick: (ev) => { ev.preventDefault(); ctx.jump(c.file, c.line, c.col); } }))))];
  }

  function missingRow(key) {
    const n = cites(key).length;
    const head = [el("span", { className: "msg" }, el("code", { textContent: key }), el("span", { className: "mute", textContent: " not in any .bib file" })), badge("missing", "bad", "Cited, but no .bib entry has this key"), badge(`${n}x`, "ok", `Cited ${n} times`)];
    const body = [...citeList(key), ...(ctx.readOnly || !S.data.files.length ? [] : [el("div", { className: "fixes" }, el("button", { type: "button", className: "btn", textContent: "Create entry", onclick: () => openForm({ key }) }))])];
    return rowShell(head, body, "?" + key, S.open.has("?" + key));
  }

  function entryRow(e, byKey) {
    const f = e.fields, id = e.file + "\n" + e.key, n = cites(e.key).length, dups = byKey.get(e.key).filter((x) => x !== e);
    const title = fold(f.title) || "(no title)";
    const head = [
      el("span", { className: "msg" }, el("b", { textContent: [authorLabel(f), yearOf(f)].filter(Boolean).join(" ") || e.key }), " ", el("span", { className: "rt", textContent: title })),
      ...(n ? [badge(`${n}x`, "ok", `Cited ${n} ${n === 1 ? "time" : "times"}`)] : isUnused(e) ? [badge("unused", "mute", "In the .bib file but never cited")] : []),
      ...(dups.length ? [badge("dup", "warn", `The key ${e.key} is also in ${dups.map((x) => x.file).join(", ")}`)] : []),
      ...(e.missing.length ? [badge("fields", "bad", `Lacks ${e.missing.join(", ")}`)] : []),
    ];
    const fieldList = el("dl", { className: "refs-fields" }, ...Object.entries(f).flatMap(([k, v]) => [el("dt", { textContent: k }), el("dd", { textContent: v })]));
    const body = [
      el("p", { className: "refs-meta" }, el("code", { textContent: e.key }), ` @${e.type} in `, el("a", { href: "#", className: "where", textContent: `${e.file}:${e.line}`, onclick: (ev) => { ev.preventDefault(); ctx.jump(e.file, e.line); } })),
      ...(dups.length ? [el("p", { className: "refs-warn", textContent: `Duplicate key: also defined in ${dups.map((x) => `${x.file}:${x.line}`).join(", ")}. BibTeX uses only one of them.` })] : []),
      ...(e.missing.length ? [el("p", { className: "refs-warn", textContent: `Missing required: ${e.missing.join(", ")}.` })] : []),
      fieldList, ...citeList(e.key),
    ];
    if (!ctx.readOnly) {
      body.push(el("div", { className: "fixes" },
        el("button", { type: "button", className: "btn", textContent: "Edit", onclick: () => openForm({ entry: e }) }),
        el("button", { type: "button", className: "btn", textContent: "Insert \\cite", onclick: () => ctx.insertCite([e.key]) }),
        details(`More actions for ${e.key}`, [
          action("Copy key", () => navigator.clipboard?.writeText(e.key).then(() => ctx.toast(`Copied ${e.key}.`), () => ctx.toast(e.key))),
          action("Open in .bib file", () => ctx.jump(e.file, e.line)),
          action("Delete entry...", () => remove(e)),
        ])));
    }
    const li = rowShell(head, body, id, S.open.has(id));
    if (!ctx.readOnly) {
      const box = el("input", { type: "checkbox", className: "refs-pick", checked: S.sel.has(e.key) });
      box.setAttribute("aria-label", `Select ${e.key} for \\cite`);
      box.onchange = () => { S.sel[box.checked ? "add" : "delete"](e.key); renderSel(); };
      li.querySelector(".hrow").prepend(box);
      const cite = el("button", { type: "button", className: "mini refs-cite", textContent: "cite", title: `Insert \\cite{${e.key}} at the cursor` });
      cite.setAttribute("aria-label", `Insert cite ${e.key}`);
      cite.onclick = () => ctx.insertCite([e.key]);
      li.querySelector(".hrow").append(cite);
      if (e.missing.length) addBibLookup(li, { path: e.file, subject: e.key }, { ...ctx.bib, apply: async (...a) => { const ok = await ctx.bib.apply(...a); if (ok) setTimeout(load, 300); return ok; } });
    }
    return li;
  }

  function renderSel() {
    const keys = [...S.sel];
    selBar.hidden = !keys.length;
    if (!keys.length) return;
    selBar.replaceChildren(el("span", { textContent: `${keys.length} selected` }),
      el("button", { type: "button", className: "btn primary", textContent: `Insert \\cite{${keys.length > 2 ? keys.slice(0, 2).join(",") + ",..." : keys.join(",")}}`, onclick: () => ctx.insertCite(keys) }),
      el("button", { type: "button", className: "btn", textContent: "Clear", onclick: () => { S.sel.clear(); render(); } }));
  }

  // ---- writes ----------------------------------------------------------------------------------------------------------
  /** Ask the server for the splice against the editor's text, and apply it only if that text has not moved meanwhile. */
  async function write(path, op, key, entry) {
    const text = await ctx.text(path);
    const r = await ctx.api.bibEdit(ctx.doc(), { text, op, key, entry });
    if (!(await ctx.splice(path, r.from, r.to, r.insert, text))) throw new Error("The file changed while saving; try again.");
  }
  /** The entry as the editor holds it now; null when it moved or changed since the list was drawn (then the list reloads). */
  async function fresh(e) {
    const now = await fetchData().catch(() => null);
    const same = now?.entries.find((x) => x.file === e.file && x.key === e.key);
    if (same && same.type === e.type && JSON.stringify(same.fields) === JSON.stringify(e.fields)) return same;
    S.data = now || S.data; render();
    ctx.toast(`${e.key} changed meanwhile (another editor?). The list is up to date now; check it and try again.`);
    return null;
  }

  async function remove(e) {
    const n = cites(e.key).length;
    if (!confirm(`Delete ${e.key} from ${e.file}?${n ? `\n\nIt is cited ${n} ${n === 1 ? "time" : "times"}; those citations will print as [?].` : ""}`)) return;
    if (!(await fresh(e))) return;
    try { await write(e.file, "delete", e.key); ctx.toast(`Deleted ${e.key}.`); S.sel.delete(e.key); ctx.changed(); await load(); }
    catch (err) { ctx.toast(err.message); }
  }

  async function newBib(name) {
    const path = name || prompt("New .bib file (path inside the document):", "references.bib");
    if (!path) return;
    try { await ctx.newFile(path.endsWith(".bib") ? path : path + ".bib"); await load(); }
    catch (err) { ctx.toast(err.message); }
  }

  // ---- add / edit form ------------------------------------------------------------------------------------------------------
  function openForm({ entry, key, prefill, file }) {
    const d = S.data, taken = new Set(d.entries.map((x) => x.key));
    const start = entry ? { type: entry.type, key: entry.key, fields: { ...entry.fields } } : { type: prefill?.type || "article", key: key || "", fields: { ...(prefill?.fields || {}) } };
    let keyTouched = !!(entry || key);
    const form = el("form", { className: "refs-form", noValidate: true });
    const heading = el("h2", { id: uid("form"), textContent: entry ? `Edit ${entry.key}` : "New reference" });
    form.setAttribute("aria-labelledby", heading.id);
    const field = (label, input, hint) => { input.id = uid("f"); return el("div", { className: "rf" }, el("label", { htmlFor: input.id, textContent: label }), input, ...(hint ? [el("span", { className: "mute rh", textContent: hint })] : [])); };
    const typeSel = el("select", {}, ...[...new Set([...Object.keys(TYPES), start.type])].map((t) => new Option(t, t)));
    typeSel.value = start.type;
    const fileSel = el("select", {}, ...d.files.map((f) => new Option(f.path, f.path)));
    fileSel.value = entry?.file || file || (S.scope || d.files.find((f) => f.used)?.path || d.files[0]?.path || "");
    fileSel.disabled = !!entry;
    const keyIn = el("input", { type: "text", value: start.key, autocomplete: "off", spellcheck: false, required: true });
    keyIn.oninput = () => { keyTouched = true; };
    const values = { ...start.fields };   // Survives switching type; empty means "remove".
    const reqBox = el("div", { className: "rgrid" }), optBox = el("div", { className: "rgrid" });
    const moreBox = el("details", { className: "refs-morefields" }, el("summary", { textContent: "More fields" }), optBox);
    const newName = el("input", { type: "text", placeholder: "field name", autocomplete: "off", spellcheck: false });
    newName.setAttribute("aria-label", "New field name");
    const addField = el("button", { type: "button", className: "btn", textContent: "Add field" });
    const err = el("p", { className: "refs-err", role: "alert", hidden: true });
    const input = (name) => {
      const long = /^(title|abstract|note|booktitle|author|editor)$/.test(name);
      const box = el(long ? "textarea" : "input", { value: values[name] || "", spellcheck: false, rows: 1 });
      if (!long) box.type = "text";
      box.dataset.field = name;
      box.oninput = () => { values[name] = box.value; if (!keyTouched && !entry) keyIn.value = suggestKey(values, taken); };
      return box;
    };
    function layout() {
      const kind = typeSel.value, [req0, opt] = TYPES[kind] || [[], []];
      const req = d.required[kind] || req0;
      const others = Object.keys(values).filter((n) => !req.includes(n) && !opt.includes(n));
      reqBox.replaceChildren(...req.map((n) => field(n + " *", input(n), n === "author" ? "Last, First and Last, First" : "")));
      optBox.replaceChildren(...[...opt, ...others].map((n) => {
        const row = field(n, input(n));
        const x = el("button", { type: "button", className: "icon", title: `Remove ${n}`, onclick: () => { delete values[n]; layout(); } }, icon("x"));
        x.setAttribute("aria-label", `Remove field ${n}`);
        if (!opt.includes(n)) row.append(x);
        return row;
      }), el("div", { className: "rf radd" }, newName, addField));
      if (others.some((n) => values[n]) || opt.some((n) => values[n])) moreBox.open = true;
    }
    addField.onclick = () => {
      const n = newName.value.trim().toLowerCase();
      if (!/^[a-z][\w:.+-]*$/.test(n)) { showErr("Field names start with a letter: url, urldate, isbn..."); return; }
      if (!(n in values)) values[n] = "";
      newName.value = ""; layout(); moreBox.open = true;
      form.querySelector(`[data-field="${CSS.escape(n)}"]`)?.focus();
    };
    newName.addEventListener("keydown", (e) => { if (e.key === "Enter") { e.preventDefault(); addField.click(); } });
    typeSel.onchange = layout;
    const showErr = (m) => { err.textContent = m; err.hidden = !m; };
    const close = () => { formBox.hidden = true; formBox.replaceChildren(); S.form = null; };
    form.onsubmit = async (ev) => {
      ev.preventDefault();
      const fields = Object.fromEntries(Object.entries(values).map(([k, v]) => [k, v.trim()]).filter(([, v]) => v));
      const newKey = keyIn.value.trim() || suggestKey(fields, taken);
      if (!/^[^\s,{}()"#%'=~\\]+$/.test(newKey)) { showErr("The key cannot hold spaces, commas, braces, quotes or # % ' = ~ \\."); keyIn.focus(); return; }
      if (newKey !== entry?.key && taken.has(newKey) && !confirm(`${newKey} already exists in ${d.entries.filter((x) => x.key === newKey).map((x) => x.file).join(", ")}. Use it anyway (a duplicate key)?`)) return;
      const lack = (d.required[typeSel.value] || TYPES[typeSel.value]?.[0] || []).filter((n) => !fields[n] && !(n === "year" && fields.date) && !(n === "author" && fields.editor));
      if (lack.length && !err.dataset.warned) { err.dataset.warned = "1"; showErr(`Still missing: ${lack.join(", ")}. Save again to keep it like this.`); return; }
      save.disabled = true;
      try {
        if (entry) { if (!(await fresh(entry))) return; await write(entry.file, "edit", entry.key, { type: typeSel.value, key: newKey, fields }); }
        else await write(fileSel.value, "add", null, { type: typeSel.value, key: newKey, fields });
        ctx.toast(`${entry ? "Saved" : "Added"} ${newKey}.`);
        close(); ctx.changed(); S.open.add((entry?.file || fileSel.value) + "\n" + newKey); await load();
      } catch (e) { showErr(e.message); } finally { save.disabled = false; }
    };
    const save = el("button", { type: "submit", className: "btn primary", textContent: entry ? "Save" : "Add" });
    form.append(heading,
      el("div", { className: "rgrid" }, field("Type", typeSel), ...(entry ? [] : [field("File", fileSel)]), field("Key", keyIn, entry ? "" : "Filled in as Author, year and a title word until you type")),
      reqBox, moreBox, err,
      el("div", { className: "fixes" }, save, el("button", { type: "button", className: "btn", textContent: "Cancel", onclick: close })));
    form.addEventListener("keydown", (e) => { if (e.key === "Escape") { e.stopPropagation(); close(); } });
    layout();
    if (!keyTouched || (key && !start.key)) keyIn.value = suggestKey(values, taken);
    formBox.replaceChildren(form); formBox.hidden = false; S.form = form;
    (entry ? reqBox.querySelector("input,textarea") : typeSel).focus();
    formBox.scrollIntoView({ block: "nearest" });
  }

  // ---- import ---------------------------------------------------------------------------------------------------------
  function openImport() {
    const d = S.data;
    if (!d.files.length) { ctx.toast("Create a .bib file first."); return; }
    const box = el("textarea", { rows: 5, placeholder: "@article{...}, TY  - JOUR ... or 10.1000/xyz123", spellcheck: false, id: uid("imp") });
    const fileSel = el("select", { id: uid("impf") }, ...d.files.map((f) => new Option(f.path, f.path)));
    fileSel.value = S.scope || d.files.find((f) => f.used)?.path || d.files[0].path;
    const picker = el("input", { type: "file", accept: ".bib,.bibtex,.ris,.txt,text/plain", id: uid("impx") });
    picker.onchange = async () => { const f = picker.files[0]; if (f) { if (f.size > 1_000_000) ctx.toast("That file is over 1 MB."); else box.value = await f.text(); } };
    const out = el("div", { className: "refs-imp-out", role: "status" });
    const read = el("button", { type: "submit", className: "btn primary", textContent: "Read" });
    const form = el("form", { className: "refs-form" }, el("h2", { id: uid("imph"), textContent: "Import" }),
      el("div", { className: "rf" }, el("label", { htmlFor: box.id, textContent: "BibTeX, RIS or a DOI" }), box,
        el("span", { className: "mute rh", textContent: "A DOI is looked up at Crossref (only the DOI is sent). From Mendeley (its API is closed to new apps), Zotero, EndNote or a publisher: export as BibTeX or RIS and choose the file." })),
      el("div", { className: "rf" }, el("label", { htmlFor: picker.id, textContent: "Or choose an export file (.bib, .ris)" }), picker),
      el("div", { className: "rf" }, el("label", { htmlFor: fileSel.id, textContent: "Add to" }), fileSel),
      out, el("div", { className: "fixes" }, read, el("button", { type: "button", className: "btn", textContent: "Cancel", onclick: () => { formBox.hidden = true; formBox.replaceChildren(); } })));
    form.setAttribute("aria-labelledby", form.firstChild.id);
    form.addEventListener("keydown", (e) => { if (e.key === "Escape") { e.stopPropagation(); formBox.hidden = true; formBox.replaceChildren(); } });
    form.onsubmit = async (ev) => {
      ev.preventDefault();
      const raw = box.value.trim();
      const doi = /^(?:https?:\/\/(?:dx\.)?doi\.org\/|doi:)?10\.\S+\/\S+$/i.test(raw);
      if (!raw || (!doi && !raw.includes("@") && !/^\s*TY  ?-/m.test(raw))) { out.replaceChildren(el("p", { className: "refs-err", textContent: "Paste BibTeX or RIS entries, choose an export file, or enter a DOI such as 10.1000/xyz123." })); return; }
      read.disabled = true; out.replaceChildren(el("p", { className: "mute", textContent: doi ? "Asking Crossref..." : "Reading..." }));
      try {
        const r = await ctx.api.bibImport(ctx.doc(), doi ? { doi: raw } : { bibtex: raw });
        if (!r.entries.length) { out.replaceChildren(el("p", { className: "refs-err", textContent: r.error || "Nothing found." })); return; }
        const taken = new Set(d.entries.map((x) => x.key));
        if (r.entries.length === 1) {   // One entry: review it in the normal form first.
          const one = r.entries[0], key = one.key && !taken.has(one.key) ? one.key : "";
          openForm({ key, prefill: one, file: fileSel.value });
          if (r.error) ctx.toast(r.error);
          return;
        }
        const picks = r.entries.map((x) => { const c = el("input", { type: "checkbox", checked: !taken.has(x.key) }); return { x, c }; });
        const add = el("button", { type: "button", className: "btn primary", textContent: "Add selected" });
        add.onclick = async () => {
          add.disabled = true;
          let n = 0;
          try {
            for (const { x, c } of picks) if (c.checked) { await write(fileSel.value, "add", null, { type: x.type, key: x.key, fields: x.fields }); taken.add(x.key); n++; }
            ctx.toast(`Added ${n} ${n === 1 ? "entry" : "entries"} to ${fileSel.value}.`); formBox.hidden = true; formBox.replaceChildren(); ctx.changed(); await load();
          } catch (e) { out.prepend(el("p", { className: "refs-err", textContent: `${e.message} (${n} added before this)` })); add.disabled = false; }
        };
        out.replaceChildren(...(r.error ? [el("p", { className: "refs-warn", textContent: r.error })] : []),
          el("ul", { className: "refs-imp-list" }, ...picks.map(({ x, c }) => el("li", {}, el("label", { className: "check" }, c, el("code", { textContent: x.key }), ` ${[authorLabel(x.fields), yearOf(x.fields)].filter(Boolean).join(" ")} `,
            ...(taken.has(x.key) ? [el("span", { className: "rbadge warn", textContent: "key exists" })] : []))))),
          el("div", { className: "fixes" }, add));
      } catch (e) { out.replaceChildren(el("p", { className: "refs-err", textContent: e.message })); }
      finally { read.disabled = false; }
    };
    formBox.replaceChildren(form); formBox.hidden = false; box.focus();
  }

  return { load, render };
}
