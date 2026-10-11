// "Edit visually" for the tikzpicture under the cursor: the parsed subset of tikz.js drawn as SVG. Drag nodes,
// coordinates and path points (snapped to the grid), add nodes and edges, delete, and edit names, labels and
// options; Apply writes the picture back as one edit, with everything the parser does not understand kept verbatim
// and in order. The canvas only approximates TikZ (no TeX in the browser): the build shows the real picture.
// Every label is drawn with textContent.
import { findEnv, groupEnd, maskComments } from "./tableedit.js";
import * as T from "./tikz.js";

const SVGNS = "http://www.w3.org/2000/svg";
const U = 48, PAD = 40;   // pixels per cm, margin

/** The picture around pos: {from, to, bodyFrom, bodyTo} or null. */
export function findPicture(text, pos) {
  const env = findEnv(text, pos, ["tikzpicture"]);
  if (!env) return null;
  const src = maskComments(text);
  let i = env.headFrom;
  while (/\s/.test(src[i]) && src[i] !== "\n") i++;
  const bodyFrom = src[i] === "[" ? groupEnd(src, i, "[", "]") : env.headFrom;
  return bodyFrom < 0 ? null : { ...env, bodyFrom, bodyTo: env.endFrom };
}

/** ctx = {el, toast(msg), view()} */
export function openTikzEditor(ctx) {
  const view = ctx.view(), { el } = ctx;
  const text = view.state.doc.toString(), pic = findPicture(text, view.state.selection.main.head);
  if (!pic) { ctx.toast("Put the cursor inside a tikzpicture first."); return; }
  const original = text.slice(pic.bodyFrom, pic.bodyTo), model = T.parseTikz(original);
  const lab = (n, l) => { n.setAttribute("aria-label", l); return n; };
  const svgEl = (tag, attrs = {}) => { const n = document.createElementNS(SVGNS, tag); for (const [k, v] of Object.entries(attrs)) n.setAttribute(k, v); return n; };
  let mode = "select", sel = null, from = null, grid = 0.5, snap = true, frame = null, drag = null, changed = false;
  const svg = lab(svgEl("svg", { class: "tz-canvas", tabindex: "0", role: "application" }), "TikZ picture canvas. Tab to a node, move it with the arrow keys, Delete removes it.");
  const props = el("div", { className: "tz-props" }), status = el("p", { className: "mute", role: "status" });
  const understood = model.items.filter((it) => it.kind !== "raw").length, kept = model.items.filter((it) => it.kind === "raw" && /\S/.test(it.src)).length;
  const say = (m) => { status.textContent = m; };
  const snapTo = (v) => (snap ? Math.round(v / grid) * grid : Math.round(v * 100) / 100);

  // World (cm, y up) to screen and back; the frame stays put while dragging so the picture does not jump.
  function fit() {
    const { byItem, byName } = T.layout(model), xs = [0], ys = [0];
    for (const p of byItem.values()) { xs.push(p.x); ys.push(p.y); }
    for (const it of model.items) if (it.kind === "path") for (const run of T.pathPoints(it, byName)) for (const [x, y] of run) { xs.push(x); ys.push(y); }
    frame = { minX: Math.min(...xs) - 1, maxX: Math.max(...xs) + 1, minY: Math.min(...ys) - 1, maxY: Math.max(...ys) + 1 };
  }
  const sx = (x) => (x - frame.minX) * U + PAD, sy = (y) => (frame.maxY - y) * U + PAD;
  const toWorld = (e) => { const r = svg.getBoundingClientRect(), k = svg.viewBox.baseVal.width / r.width || 1; return { x: ((e.clientX - r.left) * k - PAD) / U + frame.minX, y: frame.maxY - ((e.clientY - r.top) * k - PAD) / U }; };

  function render() {
    if (!drag) fit();
    const { byItem, byName } = T.layout(model);
    const w = (frame.maxX - frame.minX) * U + 2 * PAD, h = (frame.maxY - frame.minY) * U + 2 * PAD;
    svg.setAttribute("viewBox", `0 0 ${w} ${h}`);
    svg.setAttribute("width", w); svg.setAttribute("height", h);
    const defs = svgEl("defs");
    for (const [id, d] of [["tzEnd", "M0,0 L8,4 L0,8 z"], ["tzStart", "M8,0 L0,4 L8,8 z"]]) {
      const m = svgEl("marker", { id, viewBox: "0 0 8 8", refX: id === "tzEnd" ? 8 : 0, refY: 4, markerWidth: 8, markerHeight: 8, orient: "auto" });
      m.append(svgEl("path", { d, class: "tz-tip" }));
      defs.append(m);
    }
    const layer = svgEl("g", { class: "tz-grid" });
    for (let x = Math.ceil(frame.minX / grid) * grid; x <= frame.maxX; x += grid) layer.append(svgEl("line", { x1: sx(x), x2: sx(x), y1: sy(frame.minY), y2: sy(frame.maxY), class: Math.abs(x) < 1e-9 ? "axis" : "" }));
    for (let y = Math.ceil(frame.minY / grid) * grid; y <= frame.maxY; y += grid) layer.append(svgEl("line", { y1: sy(y), y2: sy(y), x1: sx(frame.minX), x2: sx(frame.maxX), class: Math.abs(y) < 1e-9 ? "axis" : "" }));
    svg.replaceChildren(defs, layer);
    for (const it of model.items.filter((x) => x.kind === "path")) {
      const arrow = T.arrowOf(it.opts), g = svgEl("g", { class: "tz-edge" + (sel === it ? " sel" : ""), tabindex: "0", role: "button" });
      lab(g, `Path ${T.statement(it)}`);
      for (const run of T.pathPoints(it, byName)) {
        const d = run.map(([x, y], i) => `${i ? "L" : "M"}${sx(x)},${sy(y)}`).join(" ");
        g.append(svgEl("path", { d, class: "hit" }), svgEl("path", { d, class: "line" + (it.opts.some((o) => /dash|dott/.test(o)) ? " dashed" : ""),
          ...(arrow.endsWith(">") ? { "marker-end": "url(#tzEnd)" } : {}), ...(arrow.startsWith("<") ? { "marker-start": "url(#tzStart)" } : {}) }));
        const label = it.steps.find((s) => s.label)?.label;
        if (label && run.length > 1) { const t = svgEl("text", { x: (sx(run[0][0]) + sx(run[1][0])) / 2, y: (sy(run[0][1]) + sy(run[1][1])) / 2 - 6, class: "tz-elabel" }); t.textContent = T.plainLabel(label.text); g.append(t); }
      }
      // Literal points of a path are handles too.
      it.steps.forEach((s, k) => {
        if (s.pt.x === undefined) return;
        const hnd = svgEl("circle", { cx: sx(s.pt.x), cy: sy(s.pt.y), r: 5, class: "tz-handle" });
        hnd.addEventListener("pointerdown", (e) => startDrag(e, it, k));
        g.append(hnd);
      });
      g.addEventListener("pointerdown", (e) => { if (mode === "select" && e.target.tagName !== "circle") { select(it); e.stopPropagation(); } });
      g.addEventListener("focus", () => { if (sel !== it) select(it, true); });
      svg.append(g);
    }
    for (const it of model.items.filter((x) => x.kind === "node" || x.kind === "coordinate")) {
      const p = byItem.get(it), x = sx(p.x), y = sy(p.y);
      const g = svgEl("g", { class: "tz-node" + (sel === it ? " sel" : "") + (from === it ? " from" : ""), tabindex: "0", role: "button" });
      lab(g, `${it.kind === "node" ? "Node" : "Coordinate"} ${it.name || "(no name)"} ${it.kind === "node" ? T.plainLabel(it.label) : ""} at ${T.fmt(p.x)}, ${T.fmt(p.y)}`);
      if (it.kind === "coordinate") g.append(svgEl("circle", { cx: x, cy: y, r: 4, class: "tz-coord" }));
      else {
        const txt = T.plainLabel(it.label), wpx = Math.max(24, txt.length * 7.5 + 14), round = it.opts.some((o) => /^(circle|ellipse)$/.test(o));
        // Drawn with a border unless every option is only about placement or text (a style name usually draws).
        const bordered = it.opts.some((o) => !T.isPositioning(o) && !/^(above|below|left|right|anchor|font|text|align|inner|outer|minimum|scale)\b/.test(o));
        g.append(round ? svgEl("ellipse", { cx: x, cy: y, rx: wpx / 2, ry: Math.max(14, wpx / 2.4), class: bordered ? "shape" : "shape ghost" })
          : svgEl("rect", { x: x - wpx / 2, y: y - 13, width: wpx, height: 26, rx: 3, class: bordered ? "shape" : "shape ghost" }));
        const t = svgEl("text", { x, y: y + 4, class: "tz-label" });
        t.textContent = txt;
        g.append(t);
      }
      if (it.name) { const n = svgEl("text", { x: x + 6, y: y - 16, class: "tz-name" }); n.textContent = it.name; g.append(n); }
      g.addEventListener("pointerdown", (e) => { e.stopPropagation(); onNode(e, it); });
      g.addEventListener("focus", () => { if (sel !== it) select(it, true); });
      svg.append(g);
    }
  }

  function onNode(e, it) {
    if (mode === "connect") {
      if (!from) { from = it; say("Now click the node to connect to."); render(); return; }
      if (from !== it) {
        for (const n of [from, it]) if (!n.name) T.renameNode(model, n, T.uniqueName(model));
        select(T.addEdge(model, from.name, it.name));
        changed = true;
      }
      from = null; render(); return;
    }
    select(it);
    startDrag(e, it);
  }
  function startDrag(e, it, step) {
    if (mode !== "select") return;
    e.preventDefault(); e.stopPropagation();
    // Moves are measured from where the drag started, so a dialog that changes size meanwhile does not shift them.
    const r = svg.getBoundingClientRect(), k = svg.viewBox.baseVal.width / r.width || 1;
    const p0 = step !== undefined ? it.steps[step].pt : T.layout(model).byItem.get(it);
    drag = { it, step, moved: false, x0: e.clientX, y0: e.clientY, k, p0: { x: p0.x, y: p0.y } };
    svg.setPointerCapture?.(e.pointerId);
  }
  svg.addEventListener("pointermove", (e) => {
    if (!drag) return;
    const x = snapTo(drag.p0.x + (e.clientX - drag.x0) * drag.k / U), y = snapTo(drag.p0.y - (e.clientY - drag.y0) * drag.k / U);
    if (drag.step !== undefined) T.movePoint(drag.it, drag.step, x, y); else T.moveNode(drag.it, x, y);
    drag.moved = changed = true;
    render();
  });
  svg.addEventListener("pointerup", () => { if (drag) { const moved = drag.moved; drag = null; if (moved) { render(); showProps(); } } });
  svg.addEventListener("pointerdown", (e) => {
    if (mode === "add") { const p = toWorld(e); select(T.addNode(model, snapTo(p.x), snapTo(p.y), "Node")); changed = true; render(); return; }
    if (mode === "select") { select(null); }
  });
  svg.addEventListener("keydown", (e) => {
    if (!sel) return;
    if (e.key === "Delete" || e.key === "Backspace") { e.preventDefault(); remove(); return; }
    const d = { ArrowLeft: [-1, 0], ArrowRight: [1, 0], ArrowUp: [0, 1], ArrowDown: [0, -1] }[e.key];
    if (d && sel.kind !== "path") {
      e.preventDefault();
      const p = T.layout(model).byItem.get(sel);
      T.moveNode(sel, snapTo(p.x + d[0] * grid), snapTo(p.y + d[1] * grid));
      changed = true; render(); showProps(); focusSel();
    }
  });
  const focusSel = () => [...svg.querySelectorAll(".sel")][0]?.focus();

  function select(it, quiet) { sel = it; if (!quiet) render(); showProps(); }
  function remove() {
    if (!sel) return;
    if (sel.kind === "path") T.removeItem(model, sel);
    else { const n = T.removeNode(model, sel); if (n) say(`Also removed ${n} path${n > 1 ? "s" : ""} through it.`); }
    sel = null; changed = true; render(); showProps(); svg.focus();
  }

  function showProps() {
    const it = sel;
    if (!it) { props.replaceChildren(el("p", { className: "mute", textContent: "Select a node or a path to edit it." })); return; }
    const row = (label, input) => { const id = "tz" + label.replace(/\W/g, ""); input.id = id; return el("div", { className: "field" }, el("label", { htmlFor: id, textContent: label }), input); };
    const input = (value, on) => { const i = el("input", { type: "text", value, spellcheck: false, autocomplete: "off" }); i.onchange = () => { on(i.value); changed = true; render(); }; return i; };
    const kids = [];
    if (it.kind !== "path") {
      const name = input(it.name || "", (v) => { if (!T.renameNode(model, it, v.trim())) say("Names are letters, digits, - and _, and must be unique."); });
      kids.push(row("Name", name));
      if (it.kind === "node") kids.push(row("Label", input(it.label, (v) => T.setLabel(it, v))));
      const p = T.layout(model).byItem.get(it);
      for (const [axis, v] of [["x", p.x], ["y", p.y]]) {
        const n = el("input", { type: "number", step: String(grid), value: T.fmt(v) });
        n.onchange = () => { const q = T.layout(model).byItem.get(it); T.moveNode(it, axis === "x" ? Number(n.value) : q.x, axis === "y" ? Number(n.value) : q.y); changed = true; render(); };
        kids.push(row(axis + " (cm)", n));
      }
    } else {
      const arrow = el("select", {}, ...[["", "none"], ["->", "→ (->)"], ["<-", "← (<-)"], ["<->", "↔ (<->)"]].map(([v, l]) => el("option", { value: v, textContent: l, selected: T.arrowOf(it.opts) === v })));
      arrow.onchange = () => { T.setArrow(it, arrow.value); changed = true; render(); showProps(); };
      kids.push(row("Arrow", arrow), row("Label", input(it.steps.find((s) => s.label)?.label.text || "", (v) => T.setLabel(it, v))));
    }
    kids.push(row("Options", input(it.opts.join(", "), (v) => T.setOptions(it, v))),
      el("button", { type: "button", className: "btn", textContent: "Delete", onclick: remove }));
    props.replaceChildren(...kids);
  }

  const modeBtn = (m, label, title) => {
    const b = el("button", { type: "button", className: "mini", textContent: label, title, onclick: () => { mode = m; from = null; for (const x of tools.querySelectorAll("[aria-pressed]")) x.setAttribute("aria-pressed", String(x === b)); say({ select: "Drag to move; click to select.", add: "Click the canvas to place a node.", connect: "Click two nodes to connect them." }[m]); render(); } });
    b.setAttribute("aria-pressed", String(m === mode));
    return b;
  };
  const snapBox = el("input", { type: "checkbox", checked: snap, id: "tzSnap", onchange: () => { snap = snapBox.checked; } });
  const gridSel = el("select", { id: "tzGrid" }, ...[0.25, 0.5, 1].map((g) => el("option", { value: g, textContent: `${g} cm`, selected: g === grid })));
  gridSel.onchange = () => { grid = Number(gridSel.value); render(); };
  const tools = lab(el("div", { className: "tbl-tools", role: "toolbar" }, modeBtn("select", "Select", "Select and drag (S)"), modeBtn("add", "Add node", "Click the canvas to add a node"),
    modeBtn("connect", "Connect", "Click two nodes to draw a path between them"),
    el("button", { type: "button", className: "mini", textContent: "Delete", onclick: remove }),
    el("label", { className: "check" }, snapBox, " Snap"), el("label", { htmlFor: "tzGrid", className: "sr", textContent: "Grid" }), gridSel), "Canvas tools");

  const dlg = el("dialog", { className: "tz-dlg" });
  dlg.setAttribute("aria-labelledby", "tzTitle");
  const close = () => { dlg.close(); dlg.remove(); view.focus(); };
  dlg.append(el("form", { method: "dialog", onsubmit: (e) => {
    e.preventDefault();
    if (!changed) { close(); return; }
    if (view.state.sliceDoc(pic.bodyFrom, pic.bodyTo) !== original) { ctx.toast("The picture changed in the editor meanwhile; open the visual editor again."); close(); return; }
    view.dispatch({ changes: { from: pic.bodyFrom, to: pic.bodyTo, insert: T.serializeTikz(model) }, scrollIntoView: true, userEvent: "input.tikz" });
    close();
  } },
  el("div", { className: "dlg-head" }, el("h2", { id: "tzTitle", textContent: "Edit picture" }), lab(el("button", { type: "button", className: "icon", textContent: "×", onclick: close }), "Close")),
  el("div", { className: "share-body" }, tools,
    el("div", { className: "tz-main" }, el("div", { className: "tz-scroll" }, svg), props),
    status,
    el("p", { className: "mute", textContent: `${understood} statement${understood === 1 ? "" : "s"} on the canvas; ${kept} other line${kept === 1 ? "" : "s"} kept as written. The drawing is a sketch: build to see the real picture.` }),
    el("div", { className: "actions" }, el("button", { type: "button", className: "btn", textContent: "Cancel", onclick: close }), el("button", { type: "submit", className: "btn primary", textContent: "Apply" })))));
  dlg.addEventListener("cancel", (e) => { e.preventDefault(); close(); });
  document.body.append(dlg);
  render(); showProps();
  dlg.showModal();
  svg.focus();
}
