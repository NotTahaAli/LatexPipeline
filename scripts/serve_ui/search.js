// Project-wide search and replace: the sidebar's Search tab (Ctrl+Shift+F). The server searches the document's text
// files on disk (GET api/search) and writes the files nobody has open (POST api/replace, with history). A file open in
// a co-editing room is never written by the server: it answers with the room's path, the editor sends the room's text,
// gets the replaced text back and applies it to the room's shared text as one edit, like every other edit.
// Regular expressions are Python's and for the owner only (a guest's pattern could keep the server busy).

/** ctx = {api, el, doc(), role, readOnly, open(path, line, col, len), room(path) -> Promise<Room|null>, toast(msg), live(msg)} */
export function searchPanel(root, ctx) {
  const { el } = ctx;
  const S = { data: null, busy: false, timer: null };
  const check = (id, label, title) => {
    const box = el("input", { type: "checkbox", id });
    return [box, el("label", { className: "check", htmlFor: id, title }, box, label)];
  };
  const q = el("input", { type: "search", id: "srQ", placeholder: "Search in all files", autocomplete: "off", spellcheck: false });
  q.setAttribute("aria-label", "Search in all files");
  const [caseBox, caseLab] = check("srCase", "Aa", "Match case");
  const [wordBox, wordLab] = check("srWord", "Word", "Whole words only");
  const [rxBox, rxLab] = check("srRx", ".*", "Regular expression (Python syntax)");
  rxLab.hidden = ctx.role !== "owner";
  const rep = el("input", { type: "text", id: "srR", placeholder: "Replace with", autocomplete: "off", spellcheck: false });
  rep.setAttribute("aria-label", "Replace with");
  const allBtn = el("button", { type: "button", className: "btn", textContent: "Replace all", disabled: true });
  const status = el("p", { className: "mute sr-status" });
  status.setAttribute("role", "status");
  const list = el("ul", { className: "plist sr-list" });
  root.replaceChildren(el("div", { className: "sr-form" }, q, el("div", { className: "sr-opts" }, caseLab, wordLab, rxLab),
    ...(ctx.readOnly ? [] : [el("div", { className: "sr-rep" }, rep, allBtn)])), status, list);

  const opts = () => ({ q: q.value, case: caseBox.checked, word: wordBox.checked, regex: rxBox.checked && ctx.role === "owner" });
  const flags = (o) => Object.fromEntries(["case", "word", "regex"].filter((k) => o[k]).map((k) => [k, 1]));

  async function run() {
    clearTimeout(S.timer);
    const o = opts();
    if (!o.q) { S.data = null; status.textContent = ""; list.replaceChildren(); allBtn.disabled = true; return; }
    try { S.data = await ctx.api.search(ctx.doc(), o.q, flags(o)); }
    catch (e) { S.data = null; status.textContent = e.message; list.replaceChildren(); allBtn.disabled = true; return; }
    render();
  }

  function render() {
    const d = S.data, n = d.files.length;
    status.textContent = d.total ? `${d.truncated ? "First " : ""}${d.total} ${d.total === 1 ? "match" : "matches"} in ${n} ${n === 1 ? "file" : "files"}`
      + (d.skipped.length ? `; ${d.skipped.length} large or binary files skipped` : "") : "No matches.";
    allBtn.disabled = !d.total || S.busy;
    list.replaceChildren(...d.files.map((f) => el("li", { className: "sr-file" },
      el("div", { className: "sr-path", textContent: `${f.path} (${f.matches.length})` }),
      el("ul", {}, ...f.matches.map((m, i) => {
        const go = el("button", { type: "button", className: "link sr-hit", title: `${f.path}:${m.line}`, onclick: () => ctx.open(f.path, m.line, m.col, m.len) },
          el("span", { className: "mute", textContent: m.line + ": " }), m.before, el("mark", { textContent: m.match }), m.after);
        const one = ctx.readOnly ? [] : [el("button", { type: "button", className: "link sr-one", textContent: "Replace", title: `Replace this match in ${f.path}`, onclick: () => replace({ path: f.path, nth: i, line: m.line }) })];
        return el("li", {}, go, ...one);
      })))));
  }

  // One match ({path, nth, line}) or every match. Files with an open room come back in `rooms` and go through the room.
  async function replace(which) {
    if (S.busy) return;
    const o = opts(), body = { ...o, replace: rep.value, ...(which || {}) };
    if (!which && !confirm(`Replace ${S.data.truncated ? "every" : S.data.total} ${S.data.total === 1 ? "match" : "matches"} of "${o.q}" with "${rep.value}" in all files?`)) return;
    S.busy = true; allBtn.disabled = true;
    let count = 0, missed = [];
    try {
      const res = await ctx.api.replace(ctx.doc(), body);
      count += res.count;
      for (const path of res.rooms) {
        const room = await ctx.room(path).catch(() => null);
        const base = room?.text();
        const out = room ? await ctx.api.replace(ctx.doc(), { ...body, path, text: base }) : null;
        if (out && room.applyText(out.text, base)) { count += out.count; await room.save(); }   // so the next search sees it (a no-op unless this tab writes the room's file)
        else missed.push(path);
      }
    } catch (e) { ctx.toast(e.message); }
    S.busy = false;
    const msg = `Replaced ${count} ${count === 1 ? "match" : "matches"}` + (missed.length ? `; not changed (open and busy): ${missed.join(", ")}` : "");
    ctx.live(msg);
    if (missed.length) ctx.toast(msg);
    run();
  }

  q.addEventListener("input", () => { clearTimeout(S.timer); S.timer = setTimeout(run, 400); });
  q.addEventListener("keydown", (e) => { if (e.key === "Enter") { e.preventDefault(); run(); } });
  for (const box of [caseBox, wordBox, rxBox]) box.addEventListener("change", run);
  rep.addEventListener("keydown", (e) => { if (e.key === "Enter" && (e.ctrlKey || e.metaKey)) { e.preventDefault(); replace(); } });
  allBtn.onclick = () => replace();

  return {
    /** Show the tab with the selected text (if any) as the query. */
    focus(selected) {
      if (selected && !selected.includes("\n")) { q.value = selected; run(); }
      q.focus(); q.select();
    },
    refresh: () => S.data && run(),
  };
}
