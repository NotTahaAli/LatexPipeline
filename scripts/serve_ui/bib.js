// "Look up" on a lint finding for a bib entry with missing fields: the server asks Crossref (only a DOI or title leaves),
// we show the fields it would add, and Apply inserts them into the open editor document, so co-editing stays in sync
// and the normal autosave writes the file.

/** Adds a "Look up" button to the finding's row and a result area to its body. `ctx` = {api, doc, text(path), apply(path, at, insert), toast}. */
export function addBibLookup(li, f, ctx) {
  const el = (tag, props = {}, ...kids) => { const n = Object.assign(document.createElement(tag), props); n.append(...kids); return n; };
  const out = el("div", { className: "bib-result", role: "status" });
  li.querySelector(".more").prepend(out);
  const open = () => { li.querySelector(".head").setAttribute("aria-expanded", "true"); li.querySelector(".more").hidden = false; };
  const btn = el("button", { type: "button", className: "mini bib-lookup", textContent: "Look up", title: "Ask Crossref for the missing fields (sends the DOI or title)" });
  li.querySelector(".hrow").append(btn);
  btn.onclick = async () => {
    btn.disabled = true; open();
    out.replaceChildren(el("span", { className: "mute", textContent: "Asking Crossref..." }));
    try {
      const text = await ctx.text(f.path);
      const r = await ctx.api.bibLookup(ctx.doc, text, f.subject);
      const names = Object.keys(r.fields || {});
      if (!names.length) { out.replaceChildren(el("span", { className: "mute", textContent: r.error || "Crossref has nothing to add." })); return; }
      const apply = el("button", { type: "button", className: "btn", textContent: "Apply" });
      apply.onclick = async () => {
        if (await ctx.text(f.path) !== text) { out.replaceChildren(el("span", { className: "mute", textContent: "The file changed; look up again." })); return; }
        await ctx.apply(f.path, r.at, r.insert);
        out.replaceChildren(el("span", { className: "ok-mark", textContent: "Added" }), el("span", { className: "mute", textContent: ` ${names.join(", ")} (from Crossref, ${r.source}).` }));
        btn.hidden = true;
      };
      out.replaceChildren(
        el("pre", { className: "bib-diff" }, ...names.map((n) => el("div", { className: "add", textContent: `+ ${n} = {${r.fields[n]}}` }))),
        el("div", { className: "fixes" }, apply, el("span", { className: "mute", textContent: "Check them against the paper before keeping them." })));
    } catch (e) {
      out.replaceChildren(el("span", { className: "mute", textContent: e.message }));
    } finally { btn.disabled = false; }
  };
}
