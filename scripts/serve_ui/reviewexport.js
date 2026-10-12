// Export comments and suggestions (the Review tab's "Export…"): every thread with its replies and every open
// suggestion, as Markdown or JSON, built in the browser from the review data the panel already has (GET api/review,
// which leaves out build-config files for guests) plus each file's text for line numbers. Anyone who can read the
// review can export it. The formatters are pure: tests/reviewexport_check.mjs runs them in Node.
import { locate, partsOf } from "./anchors.js";

/** 1-based line of an anchor in text (found again from its quote and context), or null when it is gone. */
export function lineOf(text, anchor) {
  if (typeof text !== "string") return null;
  const at = locate(text, anchor);
  return at ? text.slice(0, at.from).split("\n").length : null;
}

const iso = (t) => new Date(t * 1000).toISOString();
const count = (n, what) => `${n} ${what}${n === 1 ? "" : "s"}`;
const where = (path, line) => (line ? `${path}:${line}` : path);

/** {doc, exported, threads: [...], suggestions: [...]}; texts: {path: file text}. */
export function reviewJson(doc, data, texts, now = Date.now() / 1000) {
  return {
    doc, exported: iso(now),
    threads: data.threads.map((t) => ({
      path: t.path, line: lineOf(texts[t.path], t.anchor), quote: t.anchor.quote, resolved: !!t.resolved, resolved_by: t.resolved_by || null,
      comments: t.comments.map((c) => ({ author: c.name, time: iso(c.time), text: c.text })),
    })),
    suggestions: data.suggestions.map((s) => ({
      path: s.path, author: s.name, time: iso(s.time),
      changes: partsOf(s).map((p) => ({ line: lineOf(texts[s.path], p.anchor), old: p.anchor.quote, new: p.insert })),
    })),
  };
}

// Quoted text and comments go into > quotes line by line, unchanged (a text file, so no escaping is needed for safety).
const quote = (s) => (s === "" ? "> (nothing)" : s.split("\n").map((l) => "> " + l).join("\n"));

export function reviewMarkdown(json) {
  const out = [`# Review of ${json.doc}`, "", `Exported ${json.exported}: ${count(json.threads.length, "comment thread")}, ${count(json.suggestions.length, "open suggestion")}.`, ""];
  out.push("## Comments", "");
  if (!json.threads.length) out.push("None.", "");
  for (const t of json.threads) {
    out.push(`### ${where(t.path, t.line)}${t.resolved ? ` (resolved${t.resolved_by ? " by " + t.resolved_by : ""})` : ""}`, "", quote(t.quote), "");
    for (const c of t.comments) out.push(`**${c.author}**, ${c.time}:`, "", quote(c.text), "");
  }
  out.push("## Suggestions", "");
  if (!json.suggestions.length) out.push("None.", "");
  for (const s of json.suggestions) {
    out.push(`### ${where(s.path, s.changes[0]?.line)}: ${s.author}, ${s.time}`, "");
    for (const c of s.changes) out.push(...(s.changes.length > 1 ? [`Line ${c.line ?? "?"}:`, ""] : []), "Old:", "", quote(c.old), "", "New:", "", quote(c.new), "");
  }
  return out.join("\n");
}

function download(name, type, text) {
  const url = URL.createObjectURL(new Blob([text], { type }));
  const a = Object.assign(document.createElement("a"), { href: url, download: name });
  document.body.append(a); a.click(); a.remove();
  setTimeout(() => URL.revokeObjectURL(url), 1000);
}

/** Ask Markdown or JSON (a small dialog), then download. ctx = {api, el, doc(), toast(msg)} */
export function exportReview(ctx, data) {
  const { el } = ctx;
  const dlg = el("dialog", { className: "rx-dlg" });
  dlg.setAttribute("aria-label", "Export comments and suggestions");
  const go = (kind) => async () => {
    dlg.close();
    const doc = ctx.doc(), texts = {};
    for (const path of new Set([...data.threads, ...data.suggestions].map((i) => i.path))) {
      try { texts[path] = (await ctx.api.read(doc, path)).text; } catch { /* gone: no line numbers */ }
    }
    const json = reviewJson(doc, data, texts), base = `${doc.replace(/[^\w.-]+/g, "_")}-review`;
    if (kind === "md") download(base + ".md", "text/markdown", reviewMarkdown(json));
    else download(base + ".json", "application/json", JSON.stringify(json, null, 2));
    ctx.toast(`Exported ${count(json.threads.length, "thread")} and ${count(json.suggestions.length, "suggestion")}.`);
  };
  dlg.append(el("form", { method: "dialog" },
    el("div", { className: "dlg-head" }, el("h2", { textContent: "Export comments and suggestions" })),
    el("p", { className: "mute", textContent: "Every thread (open and resolved, with replies) and every open suggestion." }),
    el("div", { className: "rv-actions" },
      el("button", { type: "button", className: "btn primary", textContent: "Markdown", onclick: go("md") }),
      el("button", { type: "button", className: "btn", textContent: "JSON", onclick: go("json") }),
      el("button", { value: "cancel", className: "btn ghost", textContent: "Cancel" }))));
  dlg.addEventListener("close", () => dlg.remove());
  document.body.append(dlg);
  dlg.showModal();
}
