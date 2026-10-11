// Zotero in the References panel: settings (owner only) and "Sync from Zotero" with a preview you pick from.
// The server fetches and compares (zotero.py): the owner's key locally (shared-link editors only when the owner allowed
// it), behind the hosted gateway each person's own key from the Account page. The key never reaches this page. Applying asks the server for one
// splice and puts it into the open .bib document, like the rest of the panel, so co-editing stays consistent.

/**
 * ctx = {api, el, doc(), box (the panel's form area), data() (the References data), scope(), text(path),
 *        splice(path, from, to, insert, expect), toast(msg), changed(), reload(), uid(prefix), authorLabel, yearOf}
 */
/** The Share dialog's Zotero line for the owner: what an edit link means for a stored key. */
export function zoteroShareNote(info) {
  if (!info?.has_key || info.key_from_env) return "";
  return info.sandboxed ? `Zotero: ${info.share_editors ? "edit links may sync with your API key (fetched here, never sent to them)." : "off for shared links."}`
    : "Zotero: off for shared links. Builds are not sandboxed, so LuaLaTeX from an edit link can read your stored Zotero API key on this computer; share with --sandbox, or remove the key first.";
}

export function zoteroUi(ctx) {
  const { el, box } = ctx;
  const close = () => { box.hidden = true; box.replaceChildren(); };
  const note = (cls, text) => el("p", { className: cls, textContent: text });
  const field = (label, input, hint) => {
    input.id = ctx.uid("z");
    return el("div", { className: "rf" }, el("label", { htmlFor: input.id, textContent: label }), input, ...(hint ? [el("span", { className: "mute rh", textContent: hint })] : []));
  };
  const show = (form) => {
    form.addEventListener("keydown", (e) => { if (e.key === "Escape") { e.stopPropagation(); close(); } });
    box.replaceChildren(form); box.hidden = false; box.scrollIntoView({ block: "nearest" });
  };
  const heading = (form, text) => { const h = el("h2", { id: ctx.uid("zh"), textContent: text }); form.setAttribute("aria-labelledby", h.id); return h; };

  /** What this viewer may do ({can_sync, can_configure, hosted...}), or null: the menu hides what it may not. */
  async function probe() { try { return await ctx.api.zotero(); } catch { return null; } }

  async function settings() {
    let info;
    try { info = await ctx.api.zotero(); } catch (e) { ctx.toast(e.message); return; }
    if (info.hosted) { window.open("/#account", "_blank", "noopener"); ctx.toast("Your Zotero library and key are on your Account page."); return; }
    if (!info.can_configure) { ctx.toast("Only the owner can change the Zotero settings."); return; }
    const form = el("form", { className: "refs-form" });
    const mode = el("select", {}, new Option("Zotero web API (api.zotero.org)", "web"), ...(info.local_ok ? [new Option("Better BibTeX on this computer", "local")] : []));
    mode.value = info.mode === "local" && info.local_ok ? "local" : "web";
    const type = el("select", {}, new Option("Personal library", "users"), new Option("Group library", "groups"));
    type.value = info.library_type;
    const lib = el("input", { type: "text", value: info.library_id, inputMode: "numeric", autocomplete: "off", spellcheck: false });
    const coll = el("input", { type: "text", value: info.collection, autocomplete: "off", spellcheck: false, maxLength: 300 });
    const fmt = el("select", {}, new Option("BibTeX", "bibtex"), new Option("BibLaTeX", "biblatex"));
    fmt.value = info.format;
    const key = el("input", { type: "password", autocomplete: "off", spellcheck: false, placeholder: info.has_key ? "Stored; leave empty to keep it" : "Paste an API key" });
    const share = el("input", { type: "checkbox", checked: !!info.share_editors && info.sandboxed, disabled: !info.sandboxed });
    const out = el("p", { className: "refs-err", role: "alert", hidden: true });
    const libHint = el("span", { className: "mute rh" }), collHint = el("span", { className: "mute rh" });
    const libField = field("Library ID", lib), collField = field("Collection (optional)", coll);
    libField.append(libHint); collField.append(collHint);
    const keyField = field("API key", key, info.key_from_env ? "ZOTERO_API_KEY is set in the environment and is used instead." : "Create a read-only key at zotero.org/settings/keys/new. It is kept on this computer, outside the project, and never shown to collaborators.");
    const web = el("div", { className: "rgrid" }, field("Library type", type), libField, collField, keyField);
    const shareBox = el("div", {}, el("label", { className: "check" }, share, " Let editors of shared links sync from my Zotero library (the server fetches with your key; it is never sent to them. Better BibTeX is never used while sharing)"),
      ...(info.sandboxed ? [] : [el("p", { className: "mute rh", textContent: "Needs sandboxed builds (serve.py --sandbox). Without them, LuaLaTeX from an edit link can read a stored key on this computer." })]));
    const sync = () => {
      const local = mode.value === "local";
      keyField.hidden = local;
      libField.hidden = local && type.value !== "groups";
      libHint.textContent = local ? "The group's number (as in its zotero.org address)." : "Your numeric user ID (zotero.org/settings/keys) or the number in a group's address.";
      collHint.textContent = local ? "Its key or its path, such as Thesis/Chapter 2; empty exports the whole library." : "8 characters at the end of a collection's web address; empty syncs the whole library.";
    };
    mode.onchange = sync; type.onchange = sync; sync();
    const save = el("button", { type: "submit", className: "btn primary", textContent: "Save" });
    form.append(heading(form, "Zotero"), el("div", { className: "rgrid" }, field("Source", mode), field("Format", fmt, "BibLaTeX keeps fields such as date and journaltitle.")), web, shareBox, out,
      el("div", { className: "fixes" }, save,
        ...(info.has_key && !info.key_from_env ? [el("button", { type: "button", className: "btn", textContent: "Remove key", onclick: () => submit({ clear_key: true }) })] : []),
        el("button", { type: "button", className: "btn", textContent: "Cancel", onclick: close })));
    async function submit(extra = {}) {
      out.hidden = true; save.disabled = true;
      try {
        await ctx.api.zoteroSettings({ mode: mode.value, library_type: type.value, library_id: lib.value, collection: coll.value, format: fmt.value, key: key.value.trim(), share_editors: share.checked, ...extra });
        key.value = ""; ctx.toast("Zotero settings saved."); close();
      } catch (e) { out.textContent = e.message; out.hidden = false; } finally { save.disabled = false; }
    }
    form.onsubmit = (ev) => { ev.preventDefault(); submit(); };
    show(form); mode.focus();
  }

  async function sync() {
    const d = ctx.data();
    let info;
    try { info = await ctx.api.zotero(); } catch (e) { ctx.toast(e.message); return; }
    if (!info.can_sync || !info.configured) { ctx.toast(info.reason || "Set up Zotero first."); if (info.can_configure) await settings(); return; }
    if (!d.files.length) { ctx.toast("Create a .bib file first."); return; }
    const form = el("form", { className: "refs-form" });
    const fileSel = el("select", {}, ...d.files.map((f) => new Option(f.path, f.path)));
    fileSel.value = ctx.scope() || d.files.find((f) => f.used)?.path || d.files[0].path;
    const out = el("div", { className: "refs-imp-out", role: "status" });
    const go = el("button", { type: "submit", className: "btn primary", textContent: "Fetch from Zotero" });
    form.append(heading(form, "Sync from Zotero"),
      el("p", { className: "mute rh", textContent: info.guest ? "Reads the owner's Zotero library (the owner's server uses its key; it is never sent to you). Nothing is written until you choose entries."
        : info.mode === "local" ? `Reads the Better BibTeX export${info.collection ? ` of collection ${info.collection}` : ""} from Zotero on this computer.`
        : `Reads ${info.library_type === "groups" ? "group" : "user"} library ${info.library_id}${info.collection ? `, collection ${info.collection}` : ""} from api.zotero.org. Nothing is written until you choose entries.` }),
      field("Add to and update", fileSel, d.files.length > 1 ? "Entries already in the other .bib files are matched too and not added again." : ""), out,
      el("div", { className: "fixes" }, go, ...(info.can_configure || info.hosted ? [el("button", { type: "button", className: "btn", textContent: "Settings...", onclick: settings })] : []), el("button", { type: "button", className: "btn", textContent: "Cancel", onclick: close })));
    form.onsubmit = async (ev) => {
      ev.preventDefault(); go.disabled = true;
      out.replaceChildren(note("mute", "Asking Zotero..."));
      try {
        const path = fileSel.value, text = await ctx.text(path);
        const taken = d.entries.filter((e) => e.file !== path).map((e) => e.key);
        const others = {};
        for (const f of d.files) if (f.path !== path) others[f.path] = await ctx.text(f.path);
        let r;
        if (info.hosted) {   // the gateway fetches with your key; this project's editor process compares
          const f = await ctx.api.zoteroFetch(ctx.doc());
          r = { ...(await ctx.api.zoteroCompare(ctx.doc(), { remote: f.text, text, taken, others })), cached: f.cached };
        } else r = await ctx.api.zoteroPreview(ctx.doc(), { text, taken, others });
        out.replaceChildren(...result(r, path, text));
      } catch (e) { out.replaceChildren(note("refs-err", e.message)); } finally { go.disabled = false; }
    };
    show(form); fileSel.focus();
  }

  const label = (f) => [ctx.authorLabel(f), ctx.yearOf(f)].filter(Boolean).join(" ");

  function result(r, path, text) {
    const picks = [];
    const section = (title, rows, hint) => {
      if (!rows.length) return [];
      const ul = el("ul", { className: "refs-imp-list" }, ...rows.map((x) => el("li", {}, x.node)));
      const all = el("button", { type: "button", className: "mini", textContent: "All / none", onclick: () => { const on = rows.some((x) => !x.box.checked); rows.forEach((x) => { x.box.checked = on; }); count(); } });
      return [el("h3", { className: "refs-sec", textContent: `${title} (${rows.length})` }), ...(hint ? [note("mute rh", hint)] : []), all, ul];
    };
    const newRows = r.new.map((x) => {
      const c = el("input", { type: "checkbox", checked: true });
      picks.push({ c, op: { op: "add", type: x.type, key: x.key, fields: x.fields } });
      return { box: c, node: el("label", { className: "check" }, c, el("code", { textContent: x.key }), ` ${label(x.fields)} `,
        ...(x.collision ? [el("span", { className: "rbadge warn", textContent: `Zotero key ${x.zotero_key} is taken` })] : [])) };
    });
    const changedRows = r.changed.map((x) => {
      const c = el("input", { type: "checkbox", checked: false });
      const fields = Object.fromEntries(x.diff.map((v) => [v.field, v.new]));
      picks.push({ c, op: { op: "update", key: x.key, ...(x.type !== x.old_type ? { type: x.type } : {}), fields } });
      const lines = [...(x.type !== x.old_type ? [el("li", {}, `type: @${x.old_type} to @${x.type}`)] : []),
        ...x.diff.map((v) => el("li", {}, el("b", { textContent: v.field }), ": ", el("del", { textContent: v.old || "(empty)" }), " ", el("ins", { textContent: v.new })))];
      return { box: c, node: el("div", {}, el("label", { className: "check" }, c, el("code", { textContent: x.key }),
        ` ${x.diff.length} ${x.diff.length === 1 ? "field" : "fields"}${x.by !== "key" ? ` (matched to Zotero ${x.zotero_key} by DOI or title)` : ""}`),
        el("details", { className: "refs-diff" }, el("summary", { textContent: "Show changes" }), el("ul", {}, ...lines))) };
    });
    const apply = el("button", { type: "button", className: "btn primary" });
    const count = () => { const n = picks.filter((p) => p.c.checked).length; apply.textContent = `Apply ${n} selected`; apply.disabled = !n; };
    picks.forEach((p) => p.c.addEventListener("change", count));
    count();
    apply.onclick = async () => {
      apply.disabled = true;
      try {
        const now = await ctx.text(path);
        const ops = picks.filter((p) => p.c.checked).map((p) => p.op);
        const s = await ctx.api.zoteroApply(ctx.doc(), { text: now, ops });
        if (!(await ctx.splice(path, s.from, s.to, s.insert, now))) throw new Error("The file changed while applying; fetch again.");
        const added = ops.filter((o) => o.op === "add").length;
        ctx.toast(`Zotero: added ${added}, updated ${ops.length - added} in ${path}.`);
        close(); ctx.changed(); await ctx.reload();
      } catch (e) { res.prepend(note("refs-err", e.message)); count(); }
    };
    const res = el("div", {});
    const away = r.elsewhere || [];
    const summary = `${r.new.length} new, ${r.changed.length} changed, ${r.same} identical, ${away.length} in other files, ${r.local_only.length} only here${r.cached ? " (library unchanged since the last sync)" : ""}.`;
    res.append(note("mute", summary), ...(r.skipped ? [note("refs-warn", `${r.skipped} Zotero ${r.skipped === 1 ? "entry was" : "entries were"} skipped (unusable key or fields).`)] : []),
      ...section("New in Zotero", newRows), ...section("Changed in Zotero", changedRows, "Only the fields that differ are replaced; fields Zotero does not export stay as they are."),
      ...(away.length ? [el("h3", { className: "refs-sec", textContent: `Already in other files (${away.length})` }), note("mute rh", "Not added again. To update one, choose its file above."),
        el("ul", { className: "refs-keys" }, ...away.map((x) => el("li", {}, el("code", { textContent: x.key }), ` in ${x.file}${x.zotero_key !== x.key ? ` (Zotero ${x.zotero_key})` : ""}`)))] : []),
      ...(r.local_only.length ? [el("h3", { className: "refs-sec", textContent: `Only in ${path} (${r.local_only.length})` }), note("mute rh", "Not in Zotero (or under another key); left untouched."), el("p", { className: "refs-keys" }, ...r.local_only.flatMap((k) => [el("code", { textContent: k }), " "]))] : []),
      ...(picks.length ? [el("div", { className: "fixes" }, apply)] : []));
    return [res];
  }

  return { probe, settings, sync };
}
