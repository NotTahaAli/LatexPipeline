// Version history panel: the versions the server keeps (serve.py, history.py), a line diff of any of them, labels
// ("name this version"), restore and clean-up (the owner, or a workspace admin when hosted). Restoring writes files
// on the server, except files open in a co-editing room: those stay pending on the server until one editor in the
// room claims one (claim-once), applies it as one edit and says "restore-done" on the bus after its Yjs update
// (resume). The server finishes what nobody in an emptied room applied. Deleting versions has no undo.
import { hunk } from "./collab.js";

export function ago(t) {
  const s = Math.max(0, Date.now() / 1000 - t);
  if (s < 50) return "just now";
  if (s < 3600) return `${Math.round(s / 60)} min ago`;
  if (s < 86400) return `${Math.round(s / 3600)} h ago`;
  return new Date(t * 1000).toLocaleDateString(undefined, { day: "numeric", month: "short", year: s > 300 * 86400 ? "numeric" : undefined });
}

const KINDS = { auto: "Edited", outside: "From disk", delete: "Deleted", rename: "Renamed", restore: "Restored", label: "Named version" };

const AGES = [["7", "1 week"], ["30", "1 month"], ["90", "3 months"], ["365", "1 year"]];

/**
 * ctx = {api, el, doc(), activePath(), canEdit, canClean, toast(msg), live(msg), saveAll(), dialog: <dialog>,
 *        inRoom(path) -> bool (open in a ready co-editing room), roomApply(path, text) -> bool (one edit),
 *        send(message) (the bus)}
 */
export function historyPanel(root, ctx) {
  const { el } = ctx;
  let rows = [], scope = "file", more = false, labeling = false, cleaning = false, full = false, loading = 0;
  const dlg = ctx.dialog;

  const btn = (text, onclick, cls = "btn", title) => el("button", { type: "button", className: cls, textContent: text, onclick, ...(title ? { title } : {}) });

  async function load(append = false) {
    const doc = ctx.doc(), path = scope === "file" ? ctx.activePath() : null, run = ++loading;
    if (!doc) return;
    try {
      const r = await ctx.api.history(doc, path, append && rows.length ? rows[rows.length - 1].id : null);
      if (run !== loading) return;
      rows = append ? [...rows, ...r.versions] : r.versions;
      more = r.versions.length >= 200;
      full = !!r.full;
    } catch (e) { if (run === loading) { rows = []; root.replaceChildren(el("p", { className: "gnote", textContent: e.message })); } return; }
    render();
  }

  function render() {
    const path = ctx.activePath();
    const pick = el("select", { id: "hvScope", onchange: () => { scope = pick.value; load(); } },
      new Option(path ? `This file (${path.split("/").pop()})` : "This file", "file"), new Option("All files", "all"));
    pick.value = scope;
    const tools = [];
    if (ctx.canEdit) tools.push(btn("Name this version", () => { labeling = true; cleaning = false; render(); root.querySelector("#hvLabel")?.focus(); }, "btn", "Save the project as it is now under a name"));
    if (ctx.canClean) tools.push(btn("Clean up", () => { cleaning = !cleaning; labeling = false; render(); root.querySelector("#hvAge")?.focus(); }, "btn ghost", "Delete old versions"));
    const parts = [el("div", { className: "rv-bar" }, el("label", { htmlFor: "hvScope", className: "sr", textContent: "Versions of" }), pick, el("span", { className: "spacer" }), ...tools)];
    if (full) parts.push(el("p", { className: "hv-full", role: "status", textContent: "The history is full: new versions are not being kept. Saving still works. " + (ctx.canClean ? "Delete old or named versions with Clean up to make room." : "Ask the owner to delete old versions.") }));
    if (cleaning) parts.push(cleanForm());
    if (labeling) {
      const input = el("input", { type: "text", id: "hvLabel", maxLength: 120, placeholder: "e.g. Sent to supervisor", autocomplete: "off" });
      const save = btn("Save", async () => {
        if (!input.value.trim()) { input.focus(); return; }
        save.disabled = true;
        try { await ctx.saveAll(); await ctx.api.historyLabel(ctx.doc(), input.value.trim()); labeling = false; ctx.live("Version saved"); await load(); }
        catch (e) { ctx.toast(e.message); save.disabled = false; }
      }, "btn primary");
      input.addEventListener("keydown", (e) => { if (e.key === "Enter") { e.preventDefault(); save.click(); } else if (e.key === "Escape") { e.stopPropagation(); labeling = false; render(); } });
      parts.push(el("div", { className: "rv-form hv-label" }, el("label", { htmlFor: "hvLabel", textContent: "Name for this version" }), input,
        el("div", { className: "rv-actions" }, save, btn("Cancel", () => { labeling = false; render(); }, "btn ghost"))));
    }
    const items = rows.map((v) => {
      const what = v.kind === "label" ? v.label : v.label || (v.deleted ? `${v.path} deleted` : v.path);
      const who = v.authors.length ? v.authors.join(", ") : v.kind === "outside" ? "outside the editor" : "";
      const li = el("li", { className: "hv-item" + (v.kind === "label" ? " named" : "") },
        el("div", { className: "hv-main" },
          el("span", { className: "hv-kind " + v.kind, textContent: KINDS[v.kind] || v.kind }),
          el("span", { className: "hv-what", textContent: what, title: v.path || (v.files || []).join(", ") }),
          el("span", { className: "mute hv-meta", textContent: [who, ago(v.time)].filter(Boolean).join(" · "), title: new Date(v.time * 1000).toLocaleString() })),
        ...(v.deleted ? [] : [btn("View", () => openDiff(v), "btn", `Show the changes of version ${v.id}`)]),
        ...(v.kind === "label" && ctx.canClean ? [btn("Delete", () => dropLabel(v), "btn ghost danger", `Delete the named version “${v.label}”`)] : []));
      return li;
    });
    parts.push(el("ul", { className: "plist hv-list" }, ...(items.length ? items : [el("li", { className: "none", textContent: scope === "file" && !path ? "Open a file to see its versions." : "No versions yet. Every save in the editor adds one." })])));
    if (more) parts.push(el("div", { className: "rv-actions hv-more" }, btn("Show older versions", () => load(true))));
    root.replaceChildren(...parts);
  }

  // ---- clean-up (owner, workspace admins) ---------------------------------------------------------------------------
  function cleanForm() {
    const age = el("select", { id: "hvAge" }, ...AGES.map(([d, t]) => new Option(t, d)));
    age.value = "90";
    const named = el("input", { type: "checkbox", id: "hvNamed" });
    const go = btn("Delete old versions", async () => {
      const span = age.selectedOptions[0].textContent;
      if (!confirm(`Delete every version older than ${span}${named.checked ? ", named versions included" : " (named versions stay)"}? Each file's newest version stays. This cannot be undone.`)) return;
      go.disabled = true;
      try {
        const r = await ctx.api.historyClean(ctx.doc(), { op: "clear", days: Number(age.value), named: named.checked });
        cleaning = false;
        ctx.live(`${r.removed} ${r.removed === 1 ? "version" : "versions"} deleted`);
        await load();
      } catch (e) { ctx.toast(e.message); go.disabled = false; }
    }, "btn danger");
    return el("div", { className: "rv-form hv-label hv-clean" },
      el("label", { htmlFor: "hvAge", textContent: "Delete versions older than" }), age,
      el("label", { className: "hv-check" }, named, " Also delete named versions"),
      el("p", { className: "mute", textContent: "Older versions are already thinned out automatically: everything for a day, then one per hour for a week, one per day for 90 days, then one per week." }),
      el("div", { className: "rv-actions" }, go, btn("Cancel", () => { cleaning = false; render(); }, "btn ghost")));
  }

  async function dropLabel(v) {
    if (!confirm(`Delete the named version “${v.label}”? Its text stays only where other versions still use it. This cannot be undone.`)) return;
    try { await ctx.api.historyClean(ctx.doc(), { op: "drop", id: v.id }); ctx.live("Named version deleted"); await load(); }
    catch (e) { ctx.toast(e.message); }
  }

  // ---- pending restores of files open in a co-editing room -----------------------------------------------------------
  let resuming = false, again = false;
  async function resume() {
    const doc = ctx.doc();
    if (!ctx.canEdit || !doc) return;
    if (resuming) { again = true; return; }
    resuming = true;
    try {
      const { pending } = await ctx.api.historyPending(doc);
      for (const p of pending) {
        if (p.claimed || !ctx.inRoom(p.path)) continue;
        let got;
        try { got = await ctx.api.historyClaim(doc, p.path, "claim"); } catch { continue; }   // another editor has it
        if (ctx.doc() === doc && ctx.roomApply(p.path, got.text)) ctx.send({ type: "restore-done", topic: "y", data: { doc, path: p.path, token: got.token } });
        else await ctx.api.historyClaim(doc, p.path, "release").catch(() => {});
      }
    } catch { /* the next history message tries again; the server finishes it when the room empties */ }
    resuming = false;
    if (again) { again = false; resume(); }
  }

  // ---- diff dialog ------------------------------------------------------------------------------------------------
  let shown = null;   // {v, path, against}
  async function openDiff(v, path = v.path || (v.files || []).find((f) => f === ctx.activePath()) || (v.files || [])[0], against = v.kind === "label" ? "current" : "previous") {
    shown = { v, path, against };
    const body = dlg.querySelector(".hv-body");
    dlg.querySelector("h2").textContent = v.kind === "label" ? `“${v.label}”` : `Version ${v.id}`;
    body.replaceChildren(el("p", { className: "mute", textContent: "Loading..." }));
    if (!dlg.open) dlg.showModal();
    renderControls();
    if (!path) { body.replaceChildren(el("p", { className: "mute", textContent: "This version has no files you can see." })); return; }
    try { renderDiff(await ctx.api.historyDiff(ctx.doc(), v.id, path, against)); }
    catch (e) { body.replaceChildren(el("p", { className: "err", textContent: e.message })); }
  }

  function renderControls() {
    const { v, path, against } = shown, bar = dlg.querySelector(".hv-controls");
    const kids = [];
    if (v.kind === "label") {
      const files = el("select", { id: "hvFile", onchange: () => openDiff(v, files.value, against) }, ...(v.files || []).map((f) => new Option(f, f)));
      files.value = path || "";
      kids.push(el("label", { htmlFor: "hvFile", textContent: "File" }), files);
    } else {
      const seg = (id, label) => { const b = btn(label, () => openDiff(v, path, id), "chip"); b.setAttribute("aria-pressed", String(against === id)); return b; };
      kids.push(el("div", { className: "rv-chips", role: "group" }, seg("previous", "Changes in this version"), seg("current", "Compared with now")));
      kids[0].setAttribute("aria-label", "Show");
    }
    bar.replaceChildren(...kids);
    const foot = dlg.querySelector(".hv-foot");
    const acts = [];
    if (ctx.canEdit && path) acts.push(btn(`Restore ${path.split("/").pop()}`, () => restore(v, path), "btn", "Put this file back as it was in this version; the current text stays in the history"));
    if (ctx.canEdit && v.kind === "label") acts.push(btn("Restore all files", () => restore(v, null), "btn", "Put every file back as it was in this version"));
    foot.replaceChildren(el("span", { className: "mute", textContent: [v.authors.join(", "), new Date(v.time * 1000).toLocaleString()].filter(Boolean).join(" · ") }), el("span", { className: "spacer" }), ...acts, btn("Close", () => dlg.close(), "btn primary"));
  }

  function renderDiff(d) {
    const body = dlg.querySelector(".hv-body");
    const words = shown.against === "current" ? ["in this version", "now"] : ["before", "in this version"];
    const summary = el("p", { className: "hv-sum" }, el("b", { textContent: d.path }), ` — ${d.removed} ${d.removed === 1 ? "line" : "lines"} only ${words[0]}, ${d.added} only ${words[1]}`);
    if (!d.hunks.length) { body.replaceChildren(summary, el("p", { className: "mute", textContent: "No differences." })); return; }
    const table = el("div", { className: "hv-diff" });
    table.setAttribute("role", "table");
    table.setAttribute("aria-label", `Changes to ${d.path}`);
    d.hunks.forEach((rows, i) => {
      if (i) table.append(el("div", { className: "hv-gap", role: "row", textContent: "⋯" }));
      for (let k = 0; k < rows.length; k++) {
        // A block of removed lines followed by as many added ones: highlight what changed inside each pair.
        let mark = null;
        if (rows[k][0] === "-") {
          let a = k; while (a < rows.length && rows[a][0] === "-") a++;
          let b = a; while (b < rows.length && rows[b][0] === "+") b++;
          if (b - a === a - k) mark = { pair: a - k };
        }
        if (mark) {
          for (let j = 0; j < mark.pair; j++) {
            const old = rows[k + j], neu = rows[k + mark.pair + j], h = hunk(old[3], neu[3]);
            rows[k + j] = [...old, [h.from, h.to]];
            rows[k + mark.pair + j] = [...neu, [h.from, h.from + h.insert.length]];
          }
        }
        table.append(line(rows[k]));
      }
    });
    body.replaceChildren(summary, table, ...(d.truncated ? [el("p", { className: "mute", textContent: "The diff is long; only its first part is shown." })] : []));
  }

  function line([op, a, b, text, span]) {
    const sign = { "-": "−", "+": "+", " ": "" }[op];
    const content = el("span", { className: "hv-text" });
    if (span && span[1] > span[0] && (span[0] > 0 || span[1] < text.length)) content.append(text.slice(0, span[0]), el("mark", { textContent: text.slice(span[0], span[1]) }), text.slice(span[1]));
    else content.textContent = text || "​";
    const row = el("div", { className: "hv-row " + ({ "-": "del", "+": "add", " ": "same" }[op]), role: "row" },
      el("span", { className: "hv-n", role: "cell", textContent: a ?? "" }), el("span", { className: "hv-n", role: "cell", textContent: b ?? "" }),
      el("span", { className: "hv-sign", role: "cell", textContent: sign }), content);
    content.setAttribute("role", "cell");
    if (op !== " ") row.setAttribute("aria-label", `${op === "-" ? "Removed" : "Added"} line ${op === "-" ? a : b}: ${text}`);
    return row;
  }

  async function restore(v, path) {
    const what = path ? path : "every file";
    if (!confirm(`Restore ${what} to ${v.kind === "label" ? `“${v.label}”` : `version ${v.id}`}? The current text is kept in the history, so this can be undone.`)) return;
    try {
      await ctx.saveAll();
      const r = await ctx.api.historyRestore(ctx.doc(), v.id, path);
      const done = r.written.length + r.pending.length;
      resume();
      ctx.toast([done ? `Restored ${done} ${done === 1 ? "file" : "files"}.` : "Nothing to restore: the files already match.",
        r.pending.length ? ` ${r.pending.length === 1 ? "One is" : r.pending.length + " are"} open in shared editing and ${r.pending.length === 1 ? "goes" : "go"} in through the editor.` : "",
        r.skipped.length ? ` ${r.skipped.length} build configuration ${r.skipped.length === 1 ? "file stays" : "files stay"} as they are (owner only).` : "",
        r.failed.length ? ` Failed: ${r.failed.map((f) => `${f.path} (${f.error})`).join(", ")}.` : ""].join(""));
      dlg.close();
      load();
    } catch (e) { ctx.toast(e.message); }
  }

  dlg.addEventListener("click", (e) => { if (e.target === dlg) dlg.close(); });
  return { load, render, resume, reset() { rows = []; full = false; cleaning = false; render(); } };
}
