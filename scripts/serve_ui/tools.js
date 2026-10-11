// Editor hooks for the visual tools: the table editor and "Align &" (tableedit.js), the TikZ canvas (tikzedit.js),
// Markdown highlighting, preview and "Insert as LaTeX" (markdown.js). A status-bar chip offers the editor that fits
// where the cursor is. Edits go through the editor as ordinary changes, so suggest mode turns them into suggestions.
import { findTable, openTableEditor, alignAmpersands } from "./tableedit.js";
import { findPicture, openTikzEditor } from "./tikzedit.js";
import { isMarkdown, markdownMode, render, toLatex } from "./markdown.js";

/**
 * ctx = {el, toast(msg), view(), preamble() -> Promise<string>, canEdit (edit role, or a reviewer in suggest mode),
 *        reviewer, status: element the chips go in, mdPreview() -> bool, setMdPreview(on)}
 */
export function editorTools(L, V, stex, ctx) {
  const say = (m) => ctx.toast(m);
  const tctx = { el: ctx.el, toast: say, view: ctx.view, preamble: ctx.preamble };
  const chips = ctx.el("span", { className: "env-tools" });
  ctx.status.append(chips);
  const languages = { md: L.StreamLanguage.define(markdownMode), tex: L.StreamLanguage.define(stex) };

  function chip(label, title, run) { return ctx.el("button", { type: "button", className: "mini", textContent: label, title, onclick: run }); }
  let timer = 0, mdActive = false;
  function showChips(view) {
    clearTimeout(timer);
    timer = setTimeout(() => {
      const text = view.state.doc.toString(), head = view.state.selection.main.head, kids = [];
      if (ctx.canEdit && findTable(text, head)) kids.push(chip("Edit table", "Edit this table as a grid", () => openTableEditor(tctx)), chip("Align &", "Line up the columns of this table in the source", () => alignAmpersands(tctx)));
      if (ctx.canEdit && findPicture(text, head)) kids.push(chip("Edit picture", "Edit this TikZ picture visually", () => openTikzEditor(tctx)));
      chips.replaceChildren(...kids);
    }, 150);
  }

  // The Markdown preview sits beside the editor inside #cm, so it hides with it (image tabs).
  const pane = ctx.el("div", { className: "md-preview", tabIndex: 0, hidden: true });
  pane.setAttribute("role", "document");
  pane.setAttribute("aria-label", "Markdown preview");
  function showPreview(view, on) {
    const host = view.dom.parentElement;
    if (pane.parentElement !== host) host.append(pane);
    pane.hidden = !on;
    host.classList.toggle("md-split", on);
    if (on) pane.replaceChildren(render(view.state.doc.toString()));
  }

  /** Extensions for one tab: its language, the chips and (Markdown files) the preview. */
  function extension(path) {
    const md = isMarkdown(path);
    return [md ? languages.md : languages.tex, V.ViewPlugin.define((view) => {
      showChips(view);
      let renderTimer = 0;
      mdActive = md;
      if (md) showPreview(view, ctx.mdPreview());
      else showPreview(view, false);
      return {
        update(u) {
          if (u.docChanged || u.selectionSet) showChips(u.view);
          if (md && u.docChanged && !pane.hidden) { clearTimeout(renderTimer); renderTimer = setTimeout(() => showPreview(u.view, true), 250); }
        },
        destroy() { clearTimeout(renderTimer); chips.replaceChildren(); mdActive = false; },
      };
    })];
  }

  function insertAsLatex() {
    const view = ctx.view(), { from, to } = view.state.selection.main;
    if (from === to) { say("Select the Markdown to convert first."); return; }
    const insert = toLatex(view.state.sliceDoc(from, to));
    view.dispatch({ changes: { from, to, insert }, selection: { anchor: from, head: from + insert.length }, userEvent: "input.markdown" });
  }

  const commands = [
    { id: "tableedit", edit: !ctx.reviewer, title: "Edit table (grid)", run: () => openTableEditor(tctx) },
    { id: "tablealign", edit: !ctx.reviewer, title: "Align & in table", run: () => alignAmpersands(tctx) },
    { id: "tikzedit", edit: !ctx.reviewer, title: "Edit TikZ picture visually", run: () => openTikzEditor(tctx) },
    { id: "md2latex", edit: !ctx.reviewer, title: "Insert Markdown selection as LaTeX", run: insertAsLatex },
    { id: "mdpreview", title: "Markdown preview on/off", run: () => { const on = !ctx.mdPreview(); ctx.setMdPreview(on); showPreview(ctx.view(), on && mdActive); } },
  ];
  return { extension, commands };
}
