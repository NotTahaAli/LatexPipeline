// Tooltips for every element with a title, shown on mouse hover after a short delay and on keyboard focus. The
// tip is a popover, so it sits in the top layer and also shows over dialogs (the command palette's rows). While a
// mouse hovers, the title moves to data-tip so the browser's own tooltip never doubles it; on keyboard focus the
// title stays (screen readers read it then). Text only through textContent.
export function tooltips(doc = document) {
  const tip = doc.createElement("div");
  if (!tip.showPopover) return;   // no Popover API: the browser's own title tooltips stay
  tip.id = "tip"; tip.setAttribute("popover", "manual"); tip.setAttribute("role", "tooltip");
  (doc.querySelector("main") || doc.body).append(tip);   // inside a landmark (axe "region"); a popover shows in the top layer anyway
  let target = null, timer = 0;
  function hide() {
    clearTimeout(timer);
    if (target && "tip" in target.dataset) { if (!target.hasAttribute("title")) target.title = target.dataset.tip; delete target.dataset.tip; }
    target = null;
    if (tip.matches(":popover-open")) tip.hidePopover();
  }
  function show(n, text, delay) {
    hide(); target = n;
    timer = setTimeout(() => {
      if (!n.isConnected) return;
      tip.textContent = text; tip.showPopover();
      const r = n.getBoundingClientRect(), t = tip.getBoundingClientRect(), W = doc.documentElement.clientWidth, H = doc.documentElement.clientHeight;
      const y = r.bottom + 6 + t.height <= H - 4 ? r.bottom + 6 : Math.max(4, r.top - 6 - t.height);
      tip.style.left = Math.max(4, Math.min(r.left + r.width / 2 - t.width / 2, W - t.width - 4)) + "px";
      tip.style.top = y + "px";
    }, delay);
  }
  doc.addEventListener("pointerover", (e) => {
    if (e.pointerType === "touch" || target?.contains(e.target)) return;
    const n = e.target.closest?.("[title]");
    if (!n?.title) { hide(); return; }
    const text = n.title;
    show(n, text, 500);
    n.dataset.tip = text; n.removeAttribute("title");
  });
  doc.addEventListener("focusin", (e) => { const n = e.target; if (n.title && n.matches?.(":focus-visible")) show(n, n.title, 0); });
  doc.addEventListener("focusout", (e) => { if (e.target === target) hide(); });
  doc.addEventListener("pointerout", (e) => { if (!e.relatedTarget) hide(); });   // left the window
  doc.addEventListener("pointerdown", hide, true);
  doc.addEventListener("keydown", (e) => { if (e.key === "Escape" && target) hide(); }, true);
  doc.addEventListener("scroll", () => { if (tip.matches(":popover-open")) hide(); }, true);   // a pending tip is placed when it shows
}
