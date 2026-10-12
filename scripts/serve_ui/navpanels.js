// Panels around the navigation index (nav.js): the outline's filter with figures and tables, labels and TODOs; the
// drawer's TODO and Bookmarks tabs; the Symbols sidebar tab; the file graph (an SVG drawn here, no library); and the
// PDF's presentation mode and "open in a new tab". User text only through textContent.
import { layoutGraph, filterSymbols } from "./navcore.js";
import { SYMBOLS, GROUPS } from "./symbols.js";

const SVG = "http://www.w3.org/2000/svg";

/**
 * ctx = {el, nav, open(path, line, col, len), insert(text, cursor) | null (cannot type), lineText(path, n) -> string|null,
 * view(), pdfView, pdfUrl() -> string|null, toast(msg), live(msg)}
 */
export function navPanels(ctx) {
  const { el, nav } = ctx;
  const $ = (id) => document.getElementById(id);
  const number = (label) => (label && nav.index.numbers?.[label]?.number) || "";
  const row = (it, tag, text) => el("button", { type: "button", className: "orow nav-row", title: `${it.file}:${it.line}`, onclick: () => ctx.open(it.file, it.line, it.col, it.len) },
    el("span", { className: "nav-tag", textContent: tag }), el("span", { className: "t", textContent: text }));

  // ---- Outline: a filter over the headings, and figures and tables, labels and TODOs under them --------------------------
  const filter = el("input", { type: "search", className: "nav-filter", placeholder: "Filter: sections, captions, labels, TODOs", autocomplete: "off", spellcheck: false });
  filter.setAttribute("aria-label", "Filter the outline");
  $("outlineList").before(filter);
  const words = () => filter.value.toLowerCase().split(/\s+/).filter(Boolean);
  const hit = (text) => words().every((w) => text.toLowerCase().includes(w));
  function outline() {
    for (const node of $("outlineList").querySelectorAll(".owrap")) node.hidden = !hit(node.textContent);
    const ix = nav.index;
    const group = (title, items, show) => items.length ? el("details", { className: "nav-group", open: true }, el("summary", { textContent: `${title} (${items.length})` }), ...items.map((it) => row(it, ...show(it)))) : "";
    $("outlineExtra").replaceChildren(
      group("Figures and tables", ix.floats.filter((f) => hit(`${f.kind} ${f.caption} ${f.label || ""} ${number(f.label)}`)),
        (f) => [`${f.kind[0].toUpperCase()}${f.kind.slice(1)} ${number(f.label)}`.trim(), f.caption || "(no caption)"]),
      group("Labels", ix.defs.filter((d) => d.kind === "label" && hit(`${d.name} ${number(d.name)}`)), (d) => [number(d.name) || "–", d.name]),
      group("TODOs", ix.todos.filter((t) => hit(`${t.kind} ${t.text}`)), (t) => [t.kind, t.text || "(empty)"]));
  }
  filter.addEventListener("input", outline);

  // ---- drawer: TODOs and bookmarks --------------------------------------------------------------------------------------------
  function todos() {
    const items = nav.index.todos;
    $("todoCount").textContent = items.length || "";
    $("todoList").replaceChildren(...(items.length ? items.map((t) => el("li", {}, row(t, t.kind, t.text || "(empty)")))
      : [el("li", { className: "none", textContent: "No % TODO, % FIXME or \\todo{} in the document." })]));
  }
  function bookmarks() {
    const all = Object.entries(nav.bookmarks()).flatMap(([path, lines]) => lines.map((line) => ({ file: path, line, col: 0, len: 0 })));
    $("bookmarkList").replaceChildren(...(all.length ? all.map((b) => el("li", {}, row(b, `${b.file}:${b.line}`, ctx.lineText(b.file, b.line) ?? "")))
      : [el("li", { className: "none", textContent: "No bookmarks yet. Ctrl+Alt+K (or a click left of the line numbers) marks the cursor's line; Ctrl+Alt+L and Ctrl+Alt+J go to the next and previous one in the file." })]));
  }
  for (const [id, dir] of [["bmPrev", -1], ["bmNext", 1]]) $(id).onclick = () => { const v = ctx.view(); if (v) { (dir > 0 ? nav.run.next : nav.run.prev)(v); v.focus(); } };

  // ---- Symbols tab: search, then click to insert at the cursor ----------------------------------------------------------------
  const search = el("input", { type: "search", className: "nav-filter", placeholder: "Search symbols (alpha, arrow, subset...)", autocomplete: "off", spellcheck: false });
  search.setAttribute("aria-label", "Search symbols");
  const grid = el("div", { className: "sym-groups" });
  $("panel-symbols").replaceChildren(search, grid);
  function symbols() {
    const found = filterSymbols(SYMBOLS, search.value);
    grid.replaceChildren(...GROUPS.map((g) => {
      const mine = found.filter((s) => s.group === g);
      if (!mine.length) return "";
      return el("section", { className: "sym-group" }, el("h2", { className: "eyebrow", textContent: g }), el("div", { className: "sym-grid" }, ...mine.map((s) => {
        const b = el("button", { type: "button", className: "sym", title: `${s.cmd.replace("{}", "{...}")}`, disabled: !ctx.insert, onclick: () => ctx.insert?.(s.cmd, s.cmd.includes("{}") ? s.cmd.indexOf("{}") + 1 : undefined) }, el("span", { textContent: s.ch }));
        b.setAttribute("aria-label", `${s.cmd} (${s.name})`);
        return b;
      })));
    }), ...(found.length ? [] : [el("p", { className: "mute", textContent: "No symbol matches." })]));
  }
  search.addEventListener("input", symbols);
  symbols();

  // ---- the file graph: \input, \include, \bibliography, \includegraphics and local packages, left to right ---------------------
  function graph() {
    const edges = nav.index.edges;
    if (!edges.length) { nav.show("File graph", el("p", { textContent: "main.tex reads no other file of the document (no \\input, \\include, \\bibliography or \\includegraphics)." })); return; }
    const { nodes, cols, rows } = layoutGraph(edges);
    const W = 190, H = 30, GX = 70, GY = 12, at = new Map(nodes.map((n) => [n.id, n]));
    const x = (n) => 10 + n.col * (W + GX), y = (n) => 10 + n.row * (H + GY);
    const svg = document.createElementNS(SVG, "svg");
    const width = 20 + cols * (W + GX) - GX, height = 20 + rows * (H + GY) - GY;
    svg.setAttribute("viewBox", `0 0 ${width} ${height}`); svg.setAttribute("width", width); svg.setAttribute("height", height);
    svg.setAttribute("role", "group"); svg.setAttribute("aria-label", `File graph: ${nodes.length} files`);
    const make = (tag, attrs, parent = svg) => { const n = document.createElementNS(SVG, tag); for (const [k, v] of Object.entries(attrs)) n.setAttribute(k, v); parent.append(n); return n; };
    for (const e of edges) {
      const a = at.get(e.from), b = at.get(e.to), x1 = x(a) + W, y1 = y(a) + H / 2, x2 = x(b), y2 = y(b) + H / 2, mid = (x1 + x2) / 2;
      make("path", { d: `M${x1},${y1} C${mid},${y1} ${mid},${y2} ${x2},${y2}`, class: `nav-edge e-${e.kind}${e.missing ? " missing" : ""}` });
    }
    for (const n of nodes) {
      const g = make("g", { class: `nav-node${n.missing ? " missing" : ""}`, transform: `translate(${x(n)},${y(n)})`, tabindex: n.missing ? "-1" : "0", role: n.missing ? "img" : "button" });
      g.setAttribute("aria-label", n.missing ? `${n.id}, not found` : `Open ${n.id}`);
      make("rect", { width: W, height: H, rx: 6 }, g);
      const t = make("text", { x: 10, y: H / 2 + 4 }, g);
      t.textContent = n.id.length > 26 ? "…" + n.id.slice(-25) : n.id;
      make("title", {}, g).textContent = n.missing ? `${n.id}: not found` : n.id;
      if (!n.missing) {
        const go = () => { if (/\.(tex|ltx|sty|cls|bib)$/i.test(n.id) || /\.(png|jpe?g|svg|gif|webp)$/i.test(n.id)) { $("navDlg").close(); ctx.open(n.id, 1, 0, 0); } else ctx.toast(`${n.id} cannot be opened in the editor.`); };
        g.addEventListener("click", go);
        g.addEventListener("keydown", (e) => { if (e.key === "Enter" || e.key === " ") { e.preventDefault(); go(); } });
      }
    }
    const key = (cls, text) => el("span", { className: `nav-key ${cls}`, textContent: text });
    nav.show("File graph", el("p", { className: "nav-legend" }, key("e-input", "\\input / \\include"), key("e-bibliography", "bibliography"), key("e-graphics", "figure"), key("e-package", "local package or class"), key("missing", "not found")),
      el("div", { className: "nav-graph", tabIndex: 0 }, svg));
  }

  // ---- PDF: presentation mode (full screen, one page, arrow keys) and the PDF in a new tab ---------------------------------------
  function present() {
    const pv = ctx.pdfView, pdf = pv.pdf;
    if (!pdf) { ctx.toast("There is no PDF yet."); return; }
    const top = pv.viewer.scrollTop, i = pv.els.findIndex((e) => e.offsetTop + e.offsetHeight / 2 > top);
    let n = Math.max(1, (i >= 0 ? pv.fullPage(i) : 1) || 1), token = 0;
    const canvas = document.createElement("canvas"), count = el("div", { className: "nav-present-n" });
    const box = el("div", { className: "nav-present", tabIndex: -1 }, canvas, count);
    box.setAttribute("role", "dialog"); box.setAttribute("aria-label", "Presentation. Arrow keys change the page, Escape ends it.");
    canvas.setAttribute("role", "img");
    async function draw() {
      const mine = ++token, page = await pdf.getPage(n);
      if (mine !== token) return;
      const one = page.getViewport({ scale: 1 }), dpr = window.devicePixelRatio || 1;
      const vp = page.getViewport({ scale: Math.min(window.innerWidth / one.width, window.innerHeight / one.height) * dpr });
      const next = Object.assign(document.createElement("canvas"), { width: vp.width, height: vp.height });
      await page.render({ canvasContext: next.getContext("2d"), viewport: vp }).promise;
      if (mine !== token) return;
      canvas.width = vp.width; canvas.height = vp.height; canvas.style.width = vp.width / dpr + "px";
      canvas.getContext("2d").drawImage(next, 0, 0);
      count.textContent = `${n} / ${pdf.numPages}`;
      canvas.setAttribute("aria-label", `Page ${n} of ${pdf.numPages}`);
    }
    const go = (to) => { to = Math.max(1, Math.min(pdf.numPages, to)); if (to !== n) { n = to; draw(); } };
    const resize = () => draw();
    const end = () => { if (!box.isConnected) return; box.remove(); window.removeEventListener("resize", resize); if (document.fullscreenElement) document.exitFullscreen().catch(() => {}); pv.viewer.focus(); };
    box.addEventListener("keydown", (e) => {
      const k = e.key;
      if (["ArrowRight", "ArrowDown", "PageDown", " ", "Enter", "n"].includes(k)) go(n + 1);
      else if (["ArrowLeft", "ArrowUp", "PageUp", "Backspace", "p"].includes(k)) go(n - 1);
      else if (k === "Home") go(1);
      else if (k === "End") go(pdf.numPages);
      else if (k === "Escape") end();
      else return;
      e.preventDefault(); e.stopPropagation();
    });
    box.addEventListener("click", (e) => go(e.clientX < window.innerWidth / 3 ? n - 1 : n + 1));
    document.addEventListener("fullscreenchange", function off() { if (!document.fullscreenElement) { document.removeEventListener("fullscreenchange", off); end(); } });
    document.body.append(box);
    box.focus();
    box.requestFullscreen?.().catch(() => { /* still a window-sized overlay */ });
    window.addEventListener("resize", resize);
    draw();
  }
  function newTab() {
    const url = ctx.pdfUrl();
    if (!url) ctx.toast("There is no PDF yet."); else window.open(url, "_blank", "noopener");
  }

  nav.onIndex(() => { outline(); todos(); bookmarks(); });
  return {
    outline, graph, present, newTab, symbols,
    /** The drawer showed one of its tabs. */
    drawer(tab) { if (tab === "todos") { todos(); nav.load().catch(() => {}); } else if (tab === "bookmarks") bookmarks(); },
  };
}
