// Git panel: the sidebar's Git tab. Locally (the owner only; serve.py refuses everyone else) it drives the document
// folder's repository through the owner's own git: status, a diff per file, stage and unstage, commit (author from
// git config), pull (fast-forward or merge; conflicts are listed, never resolved for you), push, branches and log.
// Hosted (editors) it syncs the project with a remote https repository through the gateway, which holds each
// person's Git token (Account page) and runs git itself: workers never see the token.
// Pulled files that are open in a co-editing room are merged into the room by its leader like any outside edit.
// Every text that came from git or the server is set with textContent.

const qs = (o) => new URLSearchParams(o).toString();

async function call(path, body) {
  const res = await fetch(path, body === undefined ? {} : { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(body) });
  let data = null;
  try { data = await res.json(); } catch { /* not JSON */ }
  if (!res.ok) throw Object.assign(new Error(data?.error || res.statusText), { status: res.status, data });
  return data;
}

const CODES = { M: "modified", A: "added", D: "deleted", R: "renamed", C: "copied", T: "type changed", U: "conflict", "?": "new", ".": "" };

/** Short relative time for a unix time. */
export function since(t, now = Date.now() / 1000) {
  const s = Math.max(0, now - t);
  if (s < 60) return "just now";
  if (s < 3600) return `${Math.round(s / 60)} min ago`;
  if (s < 86400) return `${Math.round(s / 3600)} h ago`;
  return `${Math.round(s / 86400)} d ago`;
}

/** Split `git status` files into staged and unstaged rows (a file with both kinds of change is in both lists). */
export function splitChanges(files) {
  const staged = [], unstaged = [];
  for (const f of files) {
    if (f.conflict) { unstaged.push({ path: f.path, code: "U" }); continue; }
    if (f.index === "?") { unstaged.push({ path: f.path, code: "?" }); continue; }
    if (f.index !== ".") staged.push({ path: f.path, code: f.index, from: f.from });
    if (f.worktree !== ".") unstaged.push({ path: f.path, code: f.worktree });
  }
  return { staged, unstaged };
}

/** One diff line -> its class: added, removed, hunk header or context. */
export function lineKind(line) {
  if (line.startsWith("+++") || line.startsWith("---") || line.startsWith("diff ") || line.startsWith("index ")) return "meta";
  if (line.startsWith("@@")) return "hunk";
  if (line.startsWith("+")) return "add";
  if (line.startsWith("-")) return "del";
  return "ctx";
}

/** ctx = {el, doc(), hosted, canEdit, toast(msg), live(msg), saveAll()} */
export function gitPanel(root, ctx) {
  const { el } = ctx;
  let busy = false;
  const btn = (text, onclick, cls = "btn", title) => el("button", { type: "button", className: cls, textContent: text, onclick, ...(title ? { title } : {}) });
  const note = (text, cls = "mute") => el("p", { className: cls, textContent: text });
  const status = el("p", { className: "mute git-status" });
  status.setAttribute("role", "status");
  const body = el("div", { className: "git-body" });
  root.replaceChildren(status, body);

  async function act(label, fn) {
    if (busy) return null;
    busy = true; status.textContent = label + "...";
    try { const r = await fn(); status.textContent = ""; return r; }
    catch (e) { status.textContent = e.message; return null; }
    finally { busy = false; }
  }

  // ---- local: the owner's repository ------------------------------------------------------------------------------
  const url = (path, extra = {}) => `${path}?${qs({ doc: ctx.doc(), ...extra })}`;
  const post = (data) => call(url("api/git"), data);

  function fileRow(f, staged) {
    const label = `${f.path}${f.from ? ` (from ${f.from})` : ""}`;
    const open = el("button", { type: "button", className: "link git-path", textContent: label, title: "Show the changes", onclick: () => showDiff(f.path, staged) });
    const code = el("span", { className: "git-code mute", textContent: CODES[f.code] ?? f.code });
    const toggle = staged
      ? btn("Unstage", () => act("Unstaging", () => post({ op: "unstage", paths: [f.path] })).then(load), "btn quiet")
      : btn("Stage", () => act("Staging", () => post({ op: "stage", paths: [f.path] })).then(load), "btn quiet");
    return el("li", { className: "git-file" }, open, code, toggle);
  }

  const diffBox = el("div", { className: "git-diff", hidden: true });
  async function showDiff(path, staged) {
    const d = await act("Loading the diff", () => call(url("api/git/diff", { path, staged: staged ? 1 : 0 })));
    if (!d) return;
    const lines = d.untracked ? d.text.split("\n").map((l) => "+" + l) : d.diff.split("\n");
    const pre = el("pre", { className: "git-pre" }, ...lines.map((l) => el("span", { className: "dl-" + lineKind(l), textContent: l + "\n" })));
    diffBox.replaceChildren(el("div", { className: "git-row" }, el("strong", { textContent: path }), el("span", { className: "spacer" }),
      btn("Close", () => { diffBox.hidden = true; }, "btn ghost")), pre, ...(d.cut ? [note("The diff is cut short.")] : []));
    diffBox.hidden = false;
  }

  async function branchPicker() {
    const b = await call(url("api/git/branches")).catch(() => null);
    if (!b) return el("span");
    const pick = el("select", { id: "gitBranch", title: "Switch branch" });
    pick.setAttribute("aria-label", "Branch");
    for (const x of b.local) pick.append(el("option", { value: x.name, textContent: x.name, selected: x.current }));
    const locals = new Set(b.local.map((x) => x.name));
    for (const r of b.remote.filter((r) => !locals.has(r.split("/").slice(1).join("/")))) pick.append(el("option", { value: r, textContent: r }));
    pick.onchange = async () => {
      await ctx.saveAll();
      if (await act(`Switching to ${pick.value}`, () => post({ op: "switch", name: pick.value }))) ctx.toast(`Switched to ${pick.value}.`);
      load();
    };
    const make = btn("New branch", async () => {
      const name = prompt("Name of the new branch (made from the current one):");
      if (name && await act("Creating the branch", () => post({ op: "create", name }))) ctx.toast(`On the new branch ${name}.`);
      load();
    }, "btn quiet");
    return el("span", { className: "git-row" }, pick, make);
  }

  async function loadLocal() {
    const s = await call(url("api/git")).catch((e) => { status.textContent = e.message; return null; });
    if (!s) return;
    if (!s.repo) { body.replaceChildren(note("This document's folder is not in a Git repository. Run `git init` in it (or clone it) to use this panel.")); return; }
    const { staged, unstaged } = splitChanges(s.files);
    const where = s.branch ? `On ${s.branch}` : "Not on a branch (detached)";
    const track = s.upstream ? ` - ${s.upstream}${s.ahead ? `, ${s.ahead} to push` : ""}${s.behind ? `, ${s.behind} to pull` : ""}` : " - no upstream";
    const pull = btn("Pull", async () => {
      await ctx.saveAll();
      const r = await act("Pulling", () => post({ op: "pull" }));
      if (r) ctx.toast(r.conflicts.length ? `Merge conflicts in ${r.conflicts.length} file(s): fix them, stage them and commit.` : "Pulled.");
      load();
    }, "btn", "Fetch and merge the upstream branch (fast-forward when possible)");
    const push = btn("Push", async () => {
      let remote;
      if (!s.upstream && s.remotes.length > 1) remote = prompt(`Push to which remote? (${s.remotes.join(", ")})`, s.remotes[0]) || undefined;
      if (await act("Pushing", () => post({ op: "push", remote }))) ctx.toast("Pushed.");
      load();
    });
    const parts = [el("div", { className: "git-row" }, el("span", { className: "git-where", textContent: where + track })),
      await branchPicker(), el("div", { className: "git-row" }, pull, push, btn("Refresh", load, "btn ghost"))];
    if (s.merging) {
      parts.push(el("div", { className: "git-merge", role: "alert" },
        note("A merge is in progress. Edit the files with conflicts (marked <<<<<<< ... >>>>>>>), stage them and commit, or abort the merge.", ""),
        btn("Abort merge", async () => { if (confirm("Abort the merge and go back to the state before the pull?")) await act("Aborting", () => post({ op: "abort" })); load(); }, "btn danger")));
    }
    const list = (rows, isStaged) => el("ul", { className: "plist git-list" }, ...(rows.length ? rows.map((f) => fileRow(f, isStaged))
      : [el("li", { className: "none", textContent: isStaged ? "Nothing staged." : "No changes." })]));
    const stageAll = unstaged.length ? [btn("Stage all", () => act("Staging", () => post({ op: "stage", paths: unstaged.map((f) => f.path) })).then(load), "btn quiet")] : [];
    parts.push(el("div", { className: "git-row" }, el("span", { className: "eyebrow", textContent: `Changes (${unstaged.length})` }), el("span", { className: "spacer" }), ...stageAll),
      list(unstaged, false), el("div", { className: "git-row" }, el("span", { className: "eyebrow", textContent: `Staged (${staged.length})` })), list(staged, true));
    const msg = el("textarea", { id: "gitMsg", rows: 3, placeholder: "Commit message" });
    msg.setAttribute("aria-label", "Commit message");
    const commit = btn("Commit", async () => {
      if (!msg.value.trim()) { status.textContent = "Write a commit message first."; msg.focus(); return; }
      await ctx.saveAll();
      if (await act("Committing", () => post({ op: "commit", message: msg.value }))) { msg.value = ""; ctx.toast("Committed."); }
      load();
    }, "btn primary");
    commit.disabled = !staged.length;
    parts.push(el("div", { className: "git-commit" }, msg, commit), diffBox);
    const log = await call(url("api/git/log")).catch(() => ({ commits: [] }));
    parts.push(el("div", { className: "git-row" }, el("span", { className: "eyebrow", textContent: "History" })),
      el("ul", { className: "plist git-log" }, ...(log.commits.length ? log.commits.map((c) => el("li", { title: `${c.id}\n${c.author} <${c.email}>` },
        el("span", { className: "git-subject", textContent: c.subject }),
        el("span", { className: "mute", textContent: ` ${c.id.slice(0, 7)} - ${c.author}, ${since(c.time)}` })))
        : [el("li", { className: "none", textContent: "No commits yet." })])));
    body.replaceChildren(...parts);
  }

  // ---- hosted: sync with a remote repository through the gateway -----------------------------------------------------
  const remote = (data) => call(`api/git/remote?${qs({ doc: ctx.doc() })}`, data);

  async function loadHosted() {
    const s = await remote().catch((e) => { status.textContent = e.message; return null; });
    if (!s) return;
    const tokenNote = note(s.has_token ? "Your Git token is saved on your Account page; it is used for your pulls and pushes."
      : "Add a Git access token on your Account page to push, or to pull from a private repository.");
    if (!s.connected) {
      if (!ctx.canEdit) { body.replaceChildren(note("This project is not connected to a Git repository.")); return; }
      const u = el("input", { type: "url", id: "gitUrl", placeholder: "https://example.org/you/thesis.git", autocomplete: "off", spellcheck: false });
      u.setAttribute("aria-label", "Repository URL (https)");
      const b = el("input", { type: "text", id: "gitBranchName", value: "main", autocomplete: "off", spellcheck: false });
      b.setAttribute("aria-label", "Branch");
      const go = btn("Connect", async () => {
        if (await act("Connecting", () => remote({ op: "connect", url: u.value.trim(), branch: b.value.trim() }))) ctx.toast("Connected. Pull to bring the repository's files in.");
        load();
      }, "btn primary");
      body.replaceChildren(note("Connect this project to a Git repository over https. Pull brings its files here; push sends this project's files there as one commit."),
        el("label", { className: "git-field", htmlFor: "gitUrl" }, "Repository", u), el("label", { className: "git-field", htmlFor: "gitBranchName" }, "Branch", b),
        el("div", { className: "git-row" }, go), tokenNote);
      return;
    }
    const result = el("div", { className: "git-result" });
    const show = (r) => {
      if (!r) return;
      result.replaceChildren(...(r.conflicts?.length ? [note("These files changed both here and in the repository, so nothing was merged. Change them so they agree (or keep one version), then pull again:", ""),
        el("ul", { className: "plist" }, ...r.conflicts.map((p) => el("li", { textContent: p })))] : [note(r.message || "Done.")]));
    };
    const pull = btn("Pull", async () => { await ctx.saveAll(); show(await act("Pulling", () => remote({ op: "pull" }))); load(); }, "btn");
    const push = btn("Push", async () => { await ctx.saveAll(); show(await act("Pushing", () => remote({ op: "push" }))); load(); }, "btn");
    const drop = btn("Disconnect", async () => { if (confirm("Disconnect this project from the repository? Its files stay as they are.")) await act("Disconnecting", () => remote({ op: "disconnect" })); load(); }, "btn danger");
    for (const b of [pull, push, drop]) b.disabled = !ctx.canEdit;
    body.replaceChildren(el("p", { className: "git-where", textContent: `${s.url} (${s.branch})` }),
      note(s.synced ? `Last sync ${since(s.synced)}${s.commit ? `, commit ${s.commit.slice(0, 7)}` : ""}.` : "Not synced yet."),
      el("div", { className: "git-row" }, pull, push, drop), result, tokenNote);
  }

  const load = () => (ctx.hosted ? loadHosted() : loadLocal());
  return { load };
}
