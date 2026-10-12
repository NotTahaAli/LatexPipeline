// Notifications and @mentions. The bell lists, for the open document, replies after yours in a thread you wrote in,
// replies on your suggestions, and comments that mention you (@Name). They are worked out here from the review data
// review.js already loads (no extra server state); what you have seen is remembered per document in this browser.
// Nothing is e-mailed: neither serve.py nor host.py has a mail sender. @-completion offers the people of the
// document (GET api/people: commenters, who is here, the owner's named links; hosted, the workspace members).
// Every bit of user text is rendered with textContent.
import { ago } from "./history.js";

const MAX_SHOWN = 50;
const escape = (s) => s.replace(/[.*+?^${}()|[\]\\]/g, "\\$&");

/** What concerns me in review data: [{id (comment), item (thread or suggestion id), kind, name, text, time}], newest first. */
export function notificationsFor(data, myName) {
  const name = (myName || "").replace(" (unverified name)", "").trim();
  const mention = name ? new RegExp(`@${escape(name)}(?![\\p{L}\\p{N}_])`, "iu") : null;
  const out = [];
  for (const item of [...(data.threads || []), ...(data.suggestions || [])]) {
    const comments = item.comments || [];
    comments.forEach((c, i) => {
      if (c.mine) return;
      const kind = mention?.test(c.text) ? "mention"
        : comments.slice(0, i).some((x) => x.mine) || (item.mine && "insert" in item) ? "reply" : null;   // a thread you wrote in, your suggestion
      if (kind) out.push({ id: c.id, item: item.id, kind, name: c.name, text: c.text, time: c.time });
    });
  }
  return out.sort((a, b) => b.time - a.time).slice(0, MAX_SHOWN);
}

/** ctx = {el, icon, api, doc(), myName(), data() -> review data, open(itemId), store: {get, set}} */
export function notifySupport(ctx) {
  const { el } = ctx;
  const seenKey = () => "seen:" + ctx.doc();
  const bell = el("button", { type: "button", className: "btn quiet icon bell", id: "bellBtn" }, ctx.icon("bell"), el("span", { className: "count" }));
  bell.setAttribute("aria-haspopup", "true"); bell.setAttribute("aria-expanded", "false"); bell.setAttribute("aria-controls", "bellList");
  const list = el("div", { className: "menu bell-list", id: "bellList", hidden: true });
  list.setAttribute("role", "region"); list.setAttribute("aria-label", "Notifications");
  const wrap = el("div", { className: "menu-wrap" }, bell, list);
  let items = [];

  function update() {
    items = notificationsFor(ctx.data(), ctx.myName());
    const seen = Number(ctx.store.get(seenKey()) || 0), fresh = items.filter((n) => n.time > seen).length;
    bell.querySelector(".count").textContent = fresh ? String(fresh) : "";
    bell.title = fresh ? `${fresh} new ${fresh === 1 ? "notification" : "notifications"}: mentions and replies` : "Notifications: mentions and replies";
    bell.setAttribute("aria-label", bell.title);
    if (!list.hidden) render();
  }
  function render() {
    const rows = items.map((n) => {
      const b = el("button", { type: "button", className: "row", onclick: () => { close(); ctx.open(n.item); } },
        el("b", { textContent: n.name }), el("span", { textContent: n.kind === "mention" ? " mentioned you" : " replied" }),
        el("span", { className: "mute", textContent: " · " + ago(n.time), title: new Date(n.time * 1000).toLocaleString() }),
        el("span", { className: "bell-text", textContent: n.text.length > 140 ? n.text.slice(0, 140) + "…" : n.text }));
      return b;
    });
    list.replaceChildren(el("p", { className: "eyebrow", textContent: "Mentions and replies" }),
      ...(rows.length ? rows : [el("p", { className: "mute", textContent: "Nothing yet. When someone replies to you or writes @your name, it shows up here." })]));
  }
  function close() { list.hidden = true; bell.setAttribute("aria-expanded", "false"); }
  bell.onclick = () => {
    if (!list.hidden) { close(); return; }
    render(); list.hidden = false; bell.setAttribute("aria-expanded", "true");
    ctx.store.set(seenKey(), String(Math.max(Number(ctx.store.get(seenKey()) || 0), ...items.map((n) => n.time))));
    update();
    list.querySelector("button")?.focus();
  };
  list.addEventListener("keydown", (e) => { if (e.key === "Escape") { close(); bell.focus(); } });
  document.addEventListener("click", (e) => { if (!wrap.contains(e.target)) close(); });

  // ---- @mentions: completion in every comment box --------------------------------------------------------------
  let people = null, peopleDoc = null;
  const loadPeople = async () => {
    if (peopleDoc !== ctx.doc()) { peopleDoc = ctx.doc(); people = ctx.api.people(peopleDoc).then((r) => r.people, () => []); }
    return people;
  };
  let boxes = 0;
  function mention(area) {
    const id = "mention-" + ++boxes, menu = el("ul", { className: "menu mention-list", id, hidden: true });
    menu.setAttribute("role", "listbox"); menu.setAttribute("aria-label", "People");
    area.setAttribute("aria-autocomplete", "list");
    let shown = [], at = 0, start = -1;
    const hide = () => { menu.hidden = true; shown = []; area.removeAttribute("aria-activedescendant"); };
    const pick = (i) => {
      const name = shown[i];
      if (name == null) return;
      const end = area.selectionStart, text = `@${name} `;
      area.setRangeText(text, start, end, "end");
      hide(); area.focus();
    };
    const draw = () => {
      menu.replaceChildren(...shown.map((name, i) => {
        const li = el("li", { id: `${id}-${i}`, className: i === at ? "on" : "", textContent: name, onmousedown: (e) => { e.preventDefault(); pick(i); } });
        li.setAttribute("role", "option"); li.setAttribute("aria-selected", String(i === at));
        return li;
      }));
      // aria-controls only once the list is in the page: an id nothing has is an invalid value (axe).
      if (!menu.isConnected) { area.after(menu); area.setAttribute("aria-controls", id); }
      menu.hidden = !shown.length;
      if (shown.length) area.setAttribute("aria-activedescendant", `${id}-${at}`); else area.removeAttribute("aria-activedescendant");
    };
    area.addEventListener("input", async () => {
      const before = area.value.slice(0, area.selectionStart), m = /(^|\s)@([^\s@]{0,30})$/.exec(before);
      if (!m) { hide(); return; }
      start = before.length - m[2].length - 1;
      const typed = m[2].toLowerCase(), all = await loadPeople();
      shown = all.filter((p) => p.toLowerCase().includes(typed)).slice(0, 8); at = 0;
      draw();
    });
    area.addEventListener("keydown", (e) => {
      if (menu.hidden) return;
      if (e.key === "ArrowDown" || e.key === "ArrowUp") { e.preventDefault(); at = (at + (e.key === "ArrowDown" ? 1 : shown.length - 1)) % shown.length; draw(); }
      else if (e.key === "Enter" || e.key === "Tab") { e.preventDefault(); e.stopImmediatePropagation(); pick(at); }
      else if (e.key === "Escape") { e.preventDefault(); e.stopImmediatePropagation(); hide(); }
    });
    area.addEventListener("blur", () => setTimeout(hide, 150));
  }

  return { node: wrap, update, mention };
}
