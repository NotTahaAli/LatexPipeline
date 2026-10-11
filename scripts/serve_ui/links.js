// Named share links in the Share dialog (owner only, serve.py share_link_create/revoke): one link per person, with
// the name and role the owner gives it. Comments, suggestions, saves and presence of that link carry this name,
// set by the server; the person cannot change it. Every bit of user text is rendered with textContent.

const ROLE_TEXT = { edit: "can edit", view: "view only" };

/** The "Named links" part of the Share dialog. ctx = {el, api, linkRow(label, url, note, id), refresh(), toast(msg)} */
export function namedLinks(info, ctx) {
  const { el } = ctx;
  const msg = el("p", { className: "err", role: "alert", hidden: true });
  const fail = (e) => { msg.textContent = e.message; msg.hidden = false; };
  const rows = (info.named || []).map((link) => {
    const revoke = el("button", { type: "button", className: "btn danger", textContent: "Revoke", onclick: async () => {
      if (!confirm(`Revoke the link for ${link.name}? It stops working at once, open tabs included.`)) return;
      revoke.disabled = true;
      try { await ctx.api.shareLinkRevoke(link.id); await ctx.refresh(); } catch (e) { revoke.disabled = false; fail(e); }
    } });
    revoke.setAttribute("aria-label", `Revoke the link for ${link.name}`);
    const made = new Date(link.created * 1000).toLocaleString();
    const row = ctx.linkRow(`${link.name} (${ROLE_TEXT[link.role]})`, link.url, `Made ${made}.`, "link-n-" + link.id);
    row.append(revoke);
    return el("li", {}, row);
  });
  const name = el("input", { type: "text", id: "shareLinkName", maxLength: 40, placeholder: "Alice", autocomplete: "off" });
  const role = el("select", { id: "shareLinkRole" }, new Option("Can edit", "edit"), new Option("View only", "view"));
  const create = el("button", { type: "button", className: "btn", id: "shareLinkCreate", textContent: "Make link", onclick: async () => {
    if (!name.value.trim()) { name.focus(); return; }
    create.disabled = true;
    try { await ctx.api.shareLinkCreate(name.value.trim(), role.value); await ctx.refresh(); document.getElementById("shareLinkName")?.focus(); }
    catch (e) { create.disabled = false; fail(e); }
  } });
  name.addEventListener("keydown", (e) => { if (e.key === "Enter") { e.preventDefault(); create.click(); } });
  return el("section", { className: "named-links" },
    el("h3", { textContent: "Links for named people" }),
    el("p", { className: "mute", textContent: "One link per person. Their comments, suggestions, saves and presence show the name you give here, and they cannot change it. These links keep working every time you share this document, until you revoke them." }),
    ...(rows.length ? [el("ul", { className: "plist named-list" }, ...rows)] : []),
    el("div", { className: "named-new" },
      el("label", { htmlFor: "shareLinkName", textContent: "Name" }), name,
      el("label", { htmlFor: "shareLinkRole", textContent: "Role" }), role, create),
    msg);
}
