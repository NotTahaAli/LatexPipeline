// The figure dialog: an image pasted or dropped into the editor asks for its file name, folder, caption, label,
// width and placement before it is saved, then a whole figure environment goes in (or just \includegraphics).
// The upload checks stay on the server (png, jpg, pdf only, contents must match, 8 MB). The pure functions are
// checked by tests/figure_check.mjs.

const PLACEMENTS = ["htbp", "!htbp", "h", "t", "b", "p", "H"];

/** A label from a file name: fig:<stem>, letters, digits, - and _ only. */
export const defaultLabel = (name) => "fig:" + (String(name).replace(/\.[^.]*$/, "").replace(/[^A-Za-z0-9_-]+/g, "-").replace(/^-+|-+$/g, "").toLowerCase() || "figure");

/** Width as typed, minus what could break out of [width=...]. */
const cleanWidth = (w) => String(w || "").replace(/[[\]{},%]/g, "").trim();

/** The file name the person typed, keeping the image's own extension (the server checks the contents against it). */
export function withExtension(name, original) {
  const ext = (/\.(png|jpe?g|pdf)$/i.exec(original) || [""])[0].toLowerCase();
  name = String(name).trim() || original;
  return !ext || name.toLowerCase().endsWith(ext) || (/\.jpe?g$/i.test(name) && /jpe?g/.test(ext)) ? name : name + ext;
}

/** The LaTeX for an uploaded image: {path, caption, label, width, placement, figure (false: \includegraphics only)}. */
export function figureLatex({ path, caption = "", label = "", width = "0.8\\linewidth", placement = "htbp", figure = true }) {
  const w = cleanWidth(width), graphic = `\\includegraphics${w ? `[width=${w}]` : ""}{${path}}`;
  if (!figure) return graphic + "\n";
  const lab = String(label).replace(/[^A-Za-z0-9:_.-]/g, "");
  return [`\\begin{figure}${PLACEMENTS.includes(placement) ? `[${placement}]` : ""}`, "  \\centering", "  " + graphic,
    `  \\caption{${caption}}`, ...(lab ? [`  \\label{${lab}}`] : []), "\\end{figure}", ""].join("\n");
}

/** Folders of the document (from file paths and empty folders), "" for the document folder itself. */
export function folderList(paths, empty = []) {
  const out = new Set([""]);
  for (const p of paths) { const parts = p.split("/").slice(0, -1); parts.forEach((_, i) => out.add(parts.slice(0, i + 1).join("/"))); }
  for (const d of empty) out.add(d.replace(/\/$/, ""));
  return [...out].sort();
}

/** The folder new images go to by default: figures/ or Figures/ (and the like) when there is one. */
export const defaultFolder = (folders) => folders.find((f) => /^(figures?|images?|img|graphics)$/i.test(f)) || "";

/**
 * Ask about one image. ctx = {el}; opts = {name, folders: [..], folder}. Resolves to
 * {name, folder, caption, label, width, placement, figure} or null (skipped).
 */
export function figureDialog(ctx, opts) {
  const { el } = ctx;
  const lab = (n, l) => { n.setAttribute("aria-label", l); return n; };
  return new Promise((resolve) => {
    const dlg = el("dialog", { className: "fig-dlg" });
    dlg.setAttribute("aria-labelledby", "figTitle");
    const field = (id, label, input) => { input.id = id; return el("div", { className: "field" }, el("label", { htmlFor: id, textContent: label }), input); };
    const name = el("input", { type: "text", value: opts.name, spellcheck: false, autocomplete: "off", maxLength: 120, required: true });
    const list = el("datalist", { id: "figFolders" }, ...opts.folders.filter(Boolean).map((f) => el("option", { value: f })));
    const folder = el("input", { type: "text", value: opts.folder, spellcheck: false, autocomplete: "off", maxLength: 200, placeholder: "(the document folder)" });
    folder.setAttribute("list", "figFolders");
    const caption = el("input", { type: "text", spellcheck: true, autocomplete: "off" });
    const label = el("input", { type: "text", value: defaultLabel(opts.name), spellcheck: false, autocomplete: "off" });
    const width = el("input", { type: "text", value: "0.8\\linewidth", spellcheck: false, autocomplete: "off" });
    const placement = el("select", {}, ...PLACEMENTS.map((p) => el("option", { value: p, textContent: p === "H" ? "H (exactly here, float package)" : p })));
    const asFigure = el("input", { type: "checkbox", checked: true, id: "figEnv" });
    let touched = false;
    label.oninput = () => { touched = true; };
    name.oninput = () => { if (!touched) label.value = defaultLabel(name.value); };
    const done = (v) => { dlg.close(); dlg.remove(); resolve(v); };
    dlg.append(el("form", { method: "dialog", onsubmit: (e) => {
      e.preventDefault();
      done({ name: name.value.trim(), folder: folder.value.trim().replace(/^\/+|\/+$/g, ""), caption: caption.value, label: label.value.trim(), width: width.value, placement: placement.value, figure: asFigure.checked });
    } },
    el("div", { className: "dlg-head" }, el("h2", { id: "figTitle", textContent: "Add image" }), lab(el("button", { type: "button", className: "icon", textContent: "×", onclick: () => done(null) }), "Skip this image")),
    el("div", { className: "fields" },
      field("figName", "File name", name), field("figFolder", "Folder", folder), list,
      el("div", { className: "field wide" }, el("label", { htmlFor: "figCaption", textContent: "Caption" }), Object.assign(caption, { id: "figCaption" })),
      field("figLabel", "Label", label), field("figWidth", "Width", width), field("figPlace", "Placement", placement),
      el("label", { className: "check wide" }, asFigure, " As a figure environment (otherwise only \\includegraphics)")),
    el("div", { className: "actions" }, el("button", { type: "button", className: "btn", textContent: "Skip", onclick: () => done(null) }),
      el("button", { type: "submit", className: "btn primary", textContent: "Save and insert" }))));
    dlg.addEventListener("cancel", (e) => { e.preventDefault(); done(null); });
    document.body.append(dlg);
    dlg.showModal();
    caption.focus();
  });
}
