// My library in the References panel: one personal store of references, the same in every document (library.py;
// locally the owner's file in the user's config folder, hosted each account's own, kept by the gateway). Add by DOI
// (resolved through this document's api/bib/import, Crossref), pasted BibTeX or RIS, or a form; duplicates and
// missing fields are flagged. Inserting goes into a .bib file of this document as one splice from api/bib/edit,
// applied in the open editor like the rest of the panel; exporting downloads the library as a .bib file.

/**
 * ctx = {api, el, doc(), box (the panel's form area), data() (the References data), selected() (keys), readOnly,
 *        text(path), splice(path, from, to, insert, expect), toast(msg), changed(), reload(), uid(prefix),
 *        suggestKey(fields, taken)}
 */
export function libraryUi(ctx) {
  const { el, box } = ctx;
  let lib = null;
  const call = async (body) => {
    const r = await fetch("api/library", body ? { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(body) } : {});
    const d = await r.json().catch(() => null);
    if (!r.ok) throw new Error(d?.error || r.statusText);
    return d;
  };
  const close = () => { box.hidden = true; box.replaceChildren(); };
  const note = (cls, text) => el("p", { className: cls, textContent: text });
  const field = (label, input) => { input.id = ctx.uid("l"); return el("div", { className: "rf" }, el("label", { htmlFor: input.id, textContent: label }), input); };

  /** Whether this viewer has a library here (the local owner, or anyone signed in to a hosted server). */
  async function probe() { try { lib = await call(); return true; } catch { return false; } }

  function missing(e) {
    const need = ctx.data()?.required?.[e.type] || [];
    return need.filter((n) => !e.fields[n] && !(n === "year" && e.fields.date) && !(n === "author" && e.fields.editor) && !(n === "journal" && e.fields.journaltitle));
  }

  async function open() {
    try { lib = await call(); } catch (e) { ctx.toast(e.message); return; }
    render();
  }

  function render(message) {
    const form = el("form", { className: "refs-form" });
    const h = el("h2", { id: ctx.uid("lh"), textContent: `My library (${lib.entries.length})` });
    form.setAttribute("aria-labelledby", h.id);
    form.onsubmit = (e) => e.preventDefault();
    form.addEventListener("keydown", (e) => { if (e.key === "Escape") { e.stopPropagation(); close(); } });
    const out = el("p", { className: "mute", role: "status", textContent: message || "" });
    const inDoc = new Set((ctx.data()?.entries || []).map((e) => e.key));
    const chosen = new Set();

    // add: DOI, BibTeX or RIS
    const paste = el("textarea", { rows: 3, placeholder: "A DOI (10.xxxx/...), BibTeX or RIS", spellcheck: false });
    const addPaste = async () => {
      const raw = paste.value.trim();
      if (!raw) return;
      try {
        let body;
        if (/^(https?:\/\/(dx\.)?doi\.org\/|doi:)?10\.\S+\/\S+$/i.test(raw)) {
          const r = await ctx.api.bibImport(ctx.doc(), { doi: raw });
          if (!r.entries?.length) throw new Error(r.error || "Crossref does not know this DOI.");
          const taken = new Set(lib.entries.map((e) => e.key));
          body = { op: "add", entries: r.entries.map((e) => ({ ...e, key: e.key || ctx.suggestKey(e.fields, taken) })) };
        } else body = { op: "add", text: raw };
        lib = await call(body);
        paste.value = "";
        render(summary(lib));
      } catch (e) { out.textContent = e.message; }
    };

    // add: form
    const type = el("select", {}, ...["article", "book", "inproceedings", "incollection", "misc", "online", "phdthesis", "techreport"].map((t) => new Option(t, t)));
    const inputs = Object.fromEntries(["key", "author", "title", "journal", "booktitle", "publisher", "year", "doi", "url"].map((n) => [n, el("input", { type: "text", autocomplete: "off", spellcheck: false })]));
    const addForm = async () => {
      const fields = Object.fromEntries(Object.entries(inputs).filter(([n, i]) => n !== "key" && i.value.trim()).map(([n, i]) => [n, i.value.trim()]));
      const key = inputs.key.value.trim() || ctx.suggestKey(fields, new Set(lib.entries.map((e) => e.key)));
      try { lib = await call({ op: "add", entries: [{ type: type.value, key, fields }] }); Object.values(inputs).forEach((i) => { i.value = ""; }); render(summary(lib)); }
      catch (e) { out.textContent = e.message; }
    };

    // from this document's selection in the References list
    const sel = (ctx.selected?.() || []);
    const fromDoc = sel.length ? [el("button", { type: "button", className: "btn", textContent: `Save ${sel.length} selected reference${sel.length === 1 ? "" : "s"} here`, onclick: async () => {
      const entries = (ctx.data()?.entries || []).filter((e) => sel.includes(e.key)).map((e) => ({ type: e.type, key: e.key, fields: e.fields }));
      try { lib = await call({ op: "add", entries }); render(summary(lib)); } catch (e) { out.textContent = e.message; }
    } })] : [];

    // list
    const list = el("ul", { className: "plist lib-list" });
    list.setAttribute("aria-label", "My library");
    const byId = new Map(lib.entries.map((e) => [e.id, e]));
    for (const e of lib.entries) {
      const check = el("input", { type: "checkbox", id: ctx.uid("lc") });
      check.onchange = () => { if (check.checked) chosen.add(e.id); else chosen.delete(e.id); };
      const gaps = missing(e), twins = e.duplicates.map((id) => byId.get(id)?.key).filter(Boolean);
      list.append(el("li", { className: "lib-row" },
        el("label", { htmlFor: check.id, className: "check" }, check, el("code", { textContent: e.key })),
        el("span", { className: "lib-title", textContent: ` ${e.fields.title || ""} ${[e.fields.author, e.fields.year].filter(Boolean).join(", ")}` }),
        ...(twins.length ? [el("span", { className: "rbadge warn", textContent: "Duplicate", title: `Same key, DOI or title as ${twins.join(", ")}` })] : []),
        ...(gaps.length ? [el("span", { className: "rbadge warn", textContent: `Missing ${gaps.join(", ")}` })] : []),
        ...(inDoc.has(e.key) ? [el("span", { className: "rbadge ok", textContent: "In this document" })] : [])));
    }

    const bibs = (ctx.data()?.files || []).map((f) => f.path);
    const target = el("select", {}, ...bibs.map((p) => new Option(p, p)));
    const insert = async () => {
      if (!chosen.size) { out.textContent = "Tick the entries to insert first."; return; }
      if (!target.value) { out.textContent = "Create a .bib file first."; return; }
      let added = 0;
      const skipped = [];
      for (const id of chosen) {
        const e = byId.get(id);
        try {
          const text = await ctx.text(target.value);
          if (new RegExp(`@\\w+\\s*[({]\\s*${e.key.replace(/[.*+?^${}()|[\]\\]/g, "\\$&")}\\s*,`).test(text)) { skipped.push(`${e.key} (already there)`); continue; }
          const r = await ctx.api.bibEdit(ctx.doc(), { text, op: "add", entry: { type: e.type, key: e.key, fields: e.fields } });
          if (!(await ctx.splice(target.value, r.from, r.to, r.insert, text))) { skipped.push(`${e.key} (the file changed meanwhile)`); continue; }
          added++;
        } catch (err) { skipped.push(`${e.key} (${err.message})`); }
      }
      ctx.changed();
      out.textContent = `Inserted ${added} into ${target.value}.` + (skipped.length ? ` Skipped: ${skipped.join("; ")}.` : "");
      ctx.reload();
    };
    const remove = async () => {
      if (!chosen.size || !confirm(`Delete ${chosen.size} entr${chosen.size === 1 ? "y" : "ies"} from your library? Your documents keep their copies.`)) return;
      try { lib = await call({ op: "delete", ids: [...chosen] }); render(summary(lib)); } catch (err) { out.textContent = err.message; }
    };
    const exportBib = () => {
      const picked = chosen.size ? lib.entries.filter((e) => chosen.has(e.id)) : lib.entries;
      const blob = new Blob([picked.map((e) => e.bibtex).join("\n\n") + "\n"], { type: "application/x-bibtex" });
      const a = el("a", { href: URL.createObjectURL(blob), download: "library.bib" });
      document.body.append(a); a.click(); a.remove();
      setTimeout(() => URL.revokeObjectURL(a.href), 10000);
    };

    form.append(h,
      note("mute", "Your own references, the same in every document. Tick entries to insert them into a .bib file here, export them, or delete them."),
      el("div", { className: "ai-actions" },
        ...(ctx.readOnly ? [] : [field("Insert into", target), el("button", { type: "button", className: "btn primary", textContent: "Insert ticked", onclick: insert })]),
        el("button", { type: "button", className: "btn", textContent: "Export .bib", title: "The ticked entries, or all of them", onclick: exportBib }),
        el("button", { type: "button", className: "btn ghost", textContent: "Delete ticked", onclick: remove }),
        el("button", { type: "button", className: "btn ghost", textContent: "Close", onclick: close })),
      out, list,
      el("details", {}, el("summary", { textContent: "Add by DOI, BibTeX or RIS" }), field("DOI, BibTeX or RIS", paste),
        el("div", { className: "ai-actions" }, el("button", { type: "button", className: "btn", textContent: "Add", onclick: addPaste }), ...fromDoc)),
      el("details", {}, el("summary", { textContent: "Add with a form" }),
        el("div", { className: "rgrid" }, field("Type", type), ...Object.entries(inputs).map(([n, i]) => field(n === "key" ? "Key (optional)" : n === "doi" ? "DOI" : n === "url" ? "URL" : n[0].toUpperCase() + n.slice(1), i))),
        el("button", { type: "button", className: "btn", textContent: "Add", onclick: addForm })));
    box.replaceChildren(form); box.hidden = false; box.scrollIntoView({ block: "nearest" });
  }

  function summary(r) {
    const parts = [];
    if (r.added) parts.push(`Added ${r.added.length}.`);
    if (r.deleted) parts.push(`Deleted ${r.deleted}.`);
    if (r.skipped?.length) parts.push(`Skipped: ${r.skipped.slice(0, 5).join("; ")}${r.skipped.length > 5 ? "..." : ""}`);
    const dupes = r.entries.filter((e) => e.duplicates.length).length;
    if (dupes) parts.push(`${dupes} entries look like duplicates.`);
    return parts.join(" ");
  }

  return { probe, open };
}
