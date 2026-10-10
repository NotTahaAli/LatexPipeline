// Hosted LaTeX Studio: sign-in, projects, workspace and site administration. Plain DOM, no libraries.
// Every page is a function of the URL hash; the server decides what each user may see and do.

const $ = (id) => document.getElementById(id);
const el = (tag, props = {}, ...kids) => {
  const node = Object.assign(document.createElement(tag), props);
  for (const kid of kids.flat()) if (kid != null && kid !== false) node.append(kid);
  return node;
};
const main = $("main");
let me = null, csrf = null, info = {};

// ---- server ---------------------------------------------------------------------------------------------------
async function api(path, method = "GET", body, raw) {
  const headers = {};
  if (method !== "GET" && csrf) headers["X-CSRF-Token"] = csrf;
  if (body !== undefined) headers["Content-Type"] = "application/json";
  if (raw) headers["Content-Type"] = "application/zip";
  const res = await fetch(path, { method, headers, body: raw || (body !== undefined ? JSON.stringify(body) : undefined), credentials: "same-origin" });
  let data = {};
  try { data = await res.json(); } catch { /* empty body */ }
  if (data && data.csrf) csrf = data.csrf;
  if (!res.ok) throw Object.assign(new Error(data.error || res.statusText), { status: res.status });
  return data;
}

async function loadMe() {
  info = await api("/api/me");
  me = info.user; csrf = info.csrf;
  $("site").textContent = info.site;
  $("nav").hidden = $("logout").hidden = !me;
  $("adminLink").hidden = !me?.site_admin;
}

// ---- small UI helpers -------------------------------------------------------------------------------------------
const announce = (text) => { $("status").textContent = ""; setTimeout(() => { $("status").textContent = text; }, 30); };
const msgBox = () => el("div", { className: "msg", hidden: true });
function show(box, text, ok = false) {
  box.className = "msg " + (ok ? "ok" : "err");
  box.setAttribute("role", ok ? "status" : "alert");
  box.textContent = text; box.hidden = !text;
}
const field = (label, input, hint) => el("label", {}, label, input, hint ? el("span", { className: "hint", textContent: hint }) : null);
const input = (name, type = "text", extra = {}) => el("input", { name, type, ...extra });
const button = (text, onclick, cls = "btn", extra = {}) => el("button", { type: "button", className: cls, textContent: text, onclick, ...extra });

// A form whose submit runs fn(values); errors land in the form's message box, the button is busy meanwhile.
function form(fields, submitText, fn, cls = "") {
  const box = msgBox();
  const submit = el("button", { type: "submit", className: "btn primary", textContent: submitText });
  const f = el("form", { className: cls, noValidate: false }, box, ...fields, el("div", { className: "row" }, submit));
  f.onsubmit = async (e) => {
    e.preventDefault();
    if (!f.reportValidity()) return;
    const values = Object.fromEntries(new FormData(f).entries());
    submit.disabled = true; show(box, "");
    try { const done = await fn(values, f); if (typeof done === "string") show(box, done, true); }
    catch (err) { show(box, err.message); box.focus?.(); }
    finally { submit.disabled = false; }
  };
  return f;
}

function page(title, ...kids) {
  document.title = `${title} · ${info.site || "LaTeX Studio"}`;
  const h1 = el("h1", { textContent: title, tabIndex: -1 });
  main.replaceChildren(h1, ...kids);
  for (const a of document.querySelectorAll("#nav a")) {
    if (location.hash.startsWith("#" + a.dataset.nav)) a.setAttribute("aria-current", "page"); else a.removeAttribute("aria-current");
  }
  h1.focus();
}

const when = (t) => new Date(t * 1000).toLocaleDateString(undefined, { year: "numeric", month: "short", day: "numeric" });
const roleName = { admin: "Admin", editor: "Editor", viewer: "Viewer" };
const go = (hash) => { if (location.hash === hash) route(); else location.hash = hash; };
const params = () => new URLSearchParams(location.hash.slice(1));

// ---- sign-in pages ---------------------------------------------------------------------------------------------
function providerButtons(invite) {
  if (!info.providers.length) return [];
  const q = invite ? `?invite=${encodeURIComponent(invite)}` : "";
  return [el("p", { className: "or", textContent: "or" }), el("div", { className: "providers" },
    info.providers.map((p) => el("a", { className: "btn", href: `/auth/${p.id}/start${q}`, textContent: `Continue with ${p.label}` })))];
}

function signInForm(after) {
  return form([
    field("Email", input("email", "email", { required: true, autocomplete: "username" })),
    field("Password", input("password", "password", { required: true, autocomplete: "current-password" })),
  ], "Sign in", async (v) => {
    const r = await api("/api/login", "POST", { email: v.email, password: v.password });
    if (r.stage === "mfa") return codePage(after);
    await loadMe(); after();
  });
}

function codePage(after) {
  page("Two-step sign-in", el("div", { className: "card narrow" },
    el("p", { textContent: "Enter the 6-digit code from your authenticator app." }),
    form([field("Code", input("code", "text", { required: true, autocomplete: "one-time-code", inputMode: "numeric", maxLength: 14 }),
      "Lost your phone? Enter one of your recovery codes instead.")], "Continue", async (v) => {
      await api("/api/login/code", "POST", { code: v.code });
      await loadMe(); after();
    })));
}

const afterLogin = () => { const next = sessionStorage.getItem("next"); sessionStorage.removeItem("next"); if (next?.startsWith("/p/")) location.href = next; else go("#home"); };

function loginPage(error) {
  const box = msgBox();
  if (error) show(box, error);
  if (info.stage === "mfa") return codePage(afterLogin);
  page("Sign in", el("div", { className: "card narrow" }, box, signInForm(afterLogin), ...providerButtons(),
    info.signup !== "invite_only" ? el("p", {}, "New here? ", el("a", { href: "#signup", textContent: "Create an account" })) : null));
}

function signupForm(invite, email) {
  return form([
    field("Name", input("name", "text", { required: true, maxLength: 80, autocomplete: "name" })),
    field("Email", input("email", "email", { required: true, autocomplete: "email", value: email || "", readOnly: !!email })),
    field("Password", input("password", "password", { required: true, minLength: info.min_password, autocomplete: "new-password" }),
      `At least ${info.min_password} characters.`),
  ], "Create account", async (v) => {
    const r = await api("/api/signup", "POST", { ...v, invite: invite || undefined });
    if (r.signin) {  // Open sign-up never says whether the address had an account: sign in like anyone else.
      const login = await api("/api/login", "POST", { email: v.email, password: v.password });
      if (login.stage === "mfa") return codePage(() => go("#home"));
    }
    await loadMe(); go("#home");
  });
}

function signupPage() {
  const note = info.signup === "open_domains"
    ? el("p", { className: "mute", textContent: "Sign-up is open to some email domains: use a sign-in provider below so your address can be confirmed." }) : null;
  page("Create an account", el("div", { className: "card narrow" }, note,
    info.signup === "open" ? signupForm() : null, ...providerButtons(),
    el("p", {}, "Have an account? ", el("a", { href: "#login", textContent: "Sign in" }))));
}

async function invitePage(token) {
  let invite;
  try { invite = await api(`/api/invites/${encodeURIComponent(token)}`); }
  catch (err) { return page("Invitation", el("div", { className: "card narrow" }, el("p", { className: "msg err", role: "alert", textContent: err.message }))); }
  const what = el("p", {}, "You are invited to ", el("strong", { textContent: invite.tenant }), ` as ${roleName[invite.role].toLowerCase()}.`);
  if (me) {
    const box = msgBox();
    return page("Invitation", el("div", { className: "card narrow" }, what, box, button("Join workspace", async () => {
      try { await api(`/api/invites/${encodeURIComponent(token)}/accept`, "POST"); await loadMe(); go("#home"); } catch (err) { show(box, err.message); }
    }, "btn primary")));
  }
  page("Invitation", el("div", { className: "card narrow" }, what,
    el("h2", { textContent: "New to " + info.site + "?" }), signupForm(token, invite.email), ...providerButtons(token),
    el("details", {}, el("summary", { textContent: "I already have an account" }), signInForm(() => go("#invite=" + token)))));
}

function resetPage(token) {
  page("Choose a new password", el("div", { className: "card narrow" }, form([
    field("New password", input("password", "password", { required: true, minLength: info.min_password, autocomplete: "new-password" }),
      `At least ${info.min_password} characters.`),
  ], "Save password", async (v) => {
    await api("/api/reset", "POST", { token, password: v.password });
    history.replaceState(null, "", "#login"); loginPage(); announce("Password saved. Sign in with it.");
  })));
}

// ---- projects --------------------------------------------------------------------------------------------------
async function homePage() {
  const cards = [];
  if (!me.tenants.length) cards.push(el("p", { className: "card", textContent: "You are not in a workspace yet. Ask a workspace admin for an invite link." }));
  page("Projects");
  for (const t of me.tenants) cards.push(await tenantCard(t.id));
  main.append(...cards);
}

async function tenantCard(tid) {
  const t = await api(`/api/tenants/${tid}`);
  const canEdit = t.role !== "viewer";
  const box = msgBox();
  const refresh = async () => card.replaceWith(await tenantCard(tid));
  const rows = t.projects.map((p) => el("tr", {},
    el("td", {}, el("a", { href: `/p/${p.id}/`, textContent: p.name })),
    el("td", { className: "optional mute", textContent: `${when(p.created)}${p.creator ? " · " + p.creator : ""}` }),
    el("td", { className: "actions" },
      el("a", { className: "btn small", href: `/api/projects/${p.id}/zip`, textContent: "Download", ariaLabel: `Download ${p.name} as zip` }),
      canEdit ? button("Delete", async () => {
        if (!confirm(`Delete "${p.name}" and all its files? This cannot be undone.`)) return;
        try { await api(`/api/projects/${p.id}`, "DELETE"); announce(`Deleted ${p.name}.`); await refresh(); } catch (err) { show(box, err.message); }
      }, "btn small danger", { ariaLabel: `Delete ${p.name}` }) : null)));
  const table = t.projects.length
    ? el("div", { className: "table-wrap" }, el("table", {}, el("caption", { className: "sr", textContent: `Projects in ${t.name}` }),
      el("thead", {}, el("tr", {}, el("th", { scope: "col", textContent: "Project" }), el("th", { scope: "col", className: "optional", textContent: "Created" }),
        el("th", { scope: "col", className: "actions", textContent: "Actions" }))), el("tbody", {}, rows)))
    : el("p", { className: "empty", textContent: "No projects yet." });
  const create = canEdit ? el("details", {}, el("summary", { textContent: "New project" }), form([
    field("Project name", input("name", "text", { required: true, maxLength: 80 })),
    field("Template", el("select", { name: "template" }, info.templates.map((n) => el("option", { value: n, textContent: n[0].toUpperCase() + n.slice(1) })))),
  ], "Create", async (v) => { const r = await api(`/api/tenants/${tid}/projects`, "POST", v); location.href = `/p/${r.id}/`; }, "inline")) : null;
  const upload = canEdit ? el("details", {}, el("summary", { textContent: "Upload a zip" }), form([
    field("Name for the upload", input("name", "text", { required: true, maxLength: 80 })),
    field("Zip file", input("file", "file", { required: true, accept: ".zip,application/zip" }), `main.tex at the top (or in one folder); up to ${t.limits.max_project_mb} MB.`),
  ], "Upload", async (v, f) => {
    await api(`/api/tenants/${tid}/upload?name=${encodeURIComponent(v.name)}`, "POST", undefined, f.file.files[0]);
    announce(`Uploaded ${v.name}.`); await refresh();
  }, "inline")) : null;
  const card = el("section", { className: "card", ariaLabel: t.name },
    el("div", { className: "head" }, el("h2", { textContent: t.name }),
      el("span", { className: "row" }, el("span", { className: "badge", textContent: roleName[t.role] }),
        t.role === "admin" ? el("a", { href: `#t/${tid}`, textContent: "Members and invites" }) : null)),
    box, table, create, upload);
  return card;
}

// ---- workspace admin -------------------------------------------------------------------------------------------
async function tenantPage(tid) {
  const t = await api(`/api/tenants/${tid}`);
  if (t.role !== "admin") return go("#home");
  const box = msgBox();
  const reload = () => tenantPage(tid);
  const roleSelect = (m) => {
    const s = el("select", { ariaLabel: `Role of ${m.name}` }, ["admin", "editor", "viewer"].map((r) => el("option", { value: r, textContent: roleName[r], selected: r === m.role })));
    s.onchange = async () => { try { await api(`/api/tenants/${tid}/members/${m.id}`, "POST", { role: s.value }); announce(`${m.name} is now ${s.value}.`); } catch (err) { show(box, err.message); s.value = m.role; } };
    return s;
  };
  const members = el("div", { className: "table-wrap" }, el("table", {}, el("caption", { className: "sr", textContent: "Members" }),
    el("thead", {}, el("tr", {}, el("th", { scope: "col", textContent: "Name" }), el("th", { scope: "col", className: "optional", textContent: "Email" }),
      el("th", { scope: "col", textContent: "Role" }), el("th", { scope: "col", className: "actions", textContent: "Actions" }))),
    el("tbody", {}, t.members.map((m) => el("tr", {}, el("td", { textContent: m.name }), el("td", { className: "optional mono", textContent: m.email }),
      el("td", {}, roleSelect(m)), el("td", { className: "actions" }, button("Remove", async () => {
        if (!confirm(`Remove ${m.name} from ${t.name}?`)) return;
        try { await api(`/api/tenants/${tid}/members/${m.id}`, "DELETE"); await reload(); announce(`Removed ${m.name}.`); } catch (err) { show(box, err.message); }
      }, "btn small danger", { ariaLabel: `Remove ${m.name}` })))))));
  const linkOut = el("div", { hidden: true });
  const invite = form([
    field("Role", el("select", { name: "role" }, ["editor", "viewer", "admin"].map((r) => el("option", { value: r, textContent: roleName[r] })))),
    field("Email (optional)", input("email", "email"), "Only this address can use the link."),
  ], "Create invite link", async (v) => {
    const r = await api(`/api/tenants/${tid}/invites`, "POST", { role: v.role, email: v.email || undefined });
    const out = input("link", "text", { readOnly: true, value: r.link, ariaLabel: "Invite link" });
    linkOut.replaceChildren(el("p", { textContent: "Send this link; it works once and expires in 7 days:" }),
      el("div", { className: "row" }, out, button("Copy", async () => { try { await navigator.clipboard.writeText(r.link); announce("Copied."); } catch { out.select(); } })));
    linkOut.hidden = false; out.select();
    renderInvites(await api(`/api/tenants/${tid}`));
  }, "inline");
  const pending = el("div");
  const renderInvites = (data) => pending.replaceChildren(data.invites.length ? el("ul", {}, data.invites.map((i) => el("li", { className: "row" },
    el("span", { textContent: `${roleName[i.role]} · ${i.email || "anyone with the link"} · expires ${when(i.expires)}` }),
    button("Revoke", async () => { await api(`/api/tenants/${tid}/invites/${i.id}`, "DELETE"); renderInvites(await api(`/api/tenants/${tid}`)); }, "btn small", { ariaLabel: `Revoke invite for ${i.email || i.role}` }))))
    : el("p", { className: "empty", textContent: "No open invites." }));
  renderInvites(t);
  page(t.name, el("p", {}, el("a", { href: "#home", textContent: "Back to projects" })), box,
    el("section", { className: "card", ariaLabel: "Members" }, el("h2", { textContent: "Members" }), members),
    el("section", { className: "card", ariaLabel: "Invites" }, el("h2", { textContent: "Invite people" }), invite, linkOut,
      el("h3", { textContent: "Open invites" }), pending));
}

// ---- account ---------------------------------------------------------------------------------------------------
async function accountPage() {
  const a = await api("/api/account");
  const profile = form([field("Name", input("name", "text", { required: true, maxLength: 80, value: a.name, autocomplete: "name" }))],
    "Save", async (v) => { await api("/api/account", "POST", v); await loadMe(); return "Saved."; }, "inline");
  const password = form([
    a.password ? field("Current password", input("current", "password", { required: true, autocomplete: "current-password" })) : null,
    field("New password", input("password", "password", { required: true, minLength: info.min_password, autocomplete: "new-password" }), `At least ${info.min_password} characters. Signs out your other sessions.`),
  ].filter(Boolean), a.password ? "Change password" : "Set password", async (v, f) => { await api("/api/account/password", "POST", v); f.reset(); return "Password saved."; });

  const twoStep = el("div");
  const codesList = (codes) => el("div", {}, el("p", { textContent: "Recovery codes: each works once if you lose your phone. Store them somewhere safe now; they are not shown again." }),
    el("ol", { className: "codes" }, codes.map((c) => el("li", { textContent: c }))));
  const renderTwoStep = () => {
    if (!a.totp) {
      twoStep.replaceChildren(el("p", { textContent: "Off. With it on, signing in also needs a code from an authenticator app." }),
        button("Set up two-step sign-in", async () => {
          const s = await api("/api/account/totp/start", "POST");
          twoStep.replaceChildren(
            el("p", { textContent: "Add this account to your authenticator app (open the link on your phone, or type the key):" }),
            el("p", {}, el("a", { href: s.uri, textContent: "Open in authenticator app" })),
            el("p", {}, "Key: ", el("code", { textContent: s.secret.replace(/(.{4})/g, "$1 ").trim() })),
            form([field("Code from the app", input("code", "text", { required: true, inputMode: "numeric", autocomplete: "one-time-code", maxLength: 6 }))],
              "Turn on", async (v) => {
                const r = await api("/api/account/totp/enable", "POST", { code: v.code });
                a.totp = true; twoStep.replaceChildren(el("p", { className: "msg ok", role: "status", textContent: "Two-step sign-in is on." }), codesList(r.codes),
                  button("I saved them", () => renderTwoStep()));
              }, "inline"));
          twoStep.querySelector("input")?.focus();
        }, "btn primary"));
    } else {
      twoStep.replaceChildren(el("p", { textContent: `On. ${a.recovery_left ?? ""} recovery codes left.` }),
        el("details", {}, el("summary", { textContent: "New recovery codes" }), form([
          a.password ? field("Current password", input("current", "password", { required: true, autocomplete: "current-password" })) : null,
        ].filter(Boolean), "Make new codes", async (v, f) => { const r = await api("/api/account/recovery", "POST", v); f.replaceWith(codesList(r.codes)); })),
        el("details", {}, el("summary", { textContent: "Turn off two-step sign-in" }), form([
          a.password ? field("Current password", input("current", "password", { required: true, autocomplete: "current-password" })) : null,
          field("Code or recovery code", input("code", "text", { required: true, autocomplete: "one-time-code" })),
        ].filter(Boolean), "Turn off", async (v) => { await api("/api/account/totp/disable", "POST", v); a.totp = false; await loadMe(); renderTwoStep(); announce("Two-step sign-in is off."); })));
    }
  };
  renderTwoStep();

  const linked = new Map(a.identities.map((i) => [i.provider, i]));
  const providers = info.providers.length ? el("section", { className: "card", ariaLabel: "Connected sign-in" }, el("h2", { textContent: "Sign-in providers" }),
    el("ul", {}, info.providers.map((p) => el("li", { className: "row" }, el("span", { textContent: p.label + (linked.has(p.id) ? ` · ${linked.get(p.id).email}` : "") }),
      linked.has(p.id) ? button("Disconnect", async () => { try { await api(`/api/account/identities/${p.id}`, "DELETE"); accountPage(); } catch (err) { alert(err.message); } }, "btn small", { ariaLabel: `Disconnect ${p.label}` })
        : el("a", { className: "btn small", href: `/auth/${p.id}/start?intent=link`, textContent: "Connect", ariaLabel: `Connect ${p.label}` }))))) : null;

  page("Account",
    el("section", { className: "card", ariaLabel: "Profile" }, el("h2", { textContent: "Profile" }), el("p", { className: "mute", textContent: a.email }), profile),
    el("section", { className: "card", ariaLabel: "Password" }, el("h2", { textContent: "Password" }), password),
    el("section", { className: "card", ariaLabel: "Two-step sign-in" }, el("h2", { textContent: "Two-step sign-in" }), twoStep),
    providers);
}

// ---- site admin ------------------------------------------------------------------------------------------------
async function adminPage() {
  const d = await api("/api/admin");
  const s = d.settings;
  const box = msgBox();
  const num = (name, label, hint) => field(label, input(name, "number", { value: s[name], min: 0, required: true }), hint);
  const settings = form([
    field("Sign-up", el("select", { name: "signup_mode" }, [["invite_only", "By invitation only"], ["open", "Open to everyone"], ["open_domains", "Open to these email domains"]]
      .map(([v, t]) => el("option", { value: v, textContent: t, selected: v === s.signup_mode })))),
    field("Email domains", input("signup_domains", "text", { value: s.signup_domains.join(", "), placeholder: "example.org, example.com" }), "For open-to-domains sign-up (needs a sign-in provider that confirms the address)."),
    el("label", { className: "check" }, input("auto_link", "checkbox", { checked: s.auto_link }), "Link sign-in providers to existing accounts with the same confirmed email"),
    el("details", {}, el("summary", { textContent: "Quotas and limits" }), el("div", { className: "row" },
      num("max_projects_per_tenant", "Projects per workspace"), num("max_project_mb", "Megabytes per project"),
      num("max_workers", "Open projects on the server", "Each builds one document at a time."), num("max_workers_per_tenant", "Open projects per workspace"),
      num("worker_idle_minutes", "Close idle projects after (minutes)"), num("build_timeout", "Build time limit (seconds)"),
      num("max_tenants", "Workspaces in total (0 = no limit)", "Open sign-up creates one per person."))),
  ], "Save settings", async (v, f) => {
    const out = { signup_mode: v.signup_mode, signup_domains: v.signup_domains, auto_link: f.auto_link.checked };
    for (const k of ["max_projects_per_tenant", "max_project_mb", "max_workers", "max_workers_per_tenant", "worker_idle_minutes", "build_timeout", "max_tenants"]) out[k] = Number(v[k]);
    await api("/api/admin/settings", "POST", out); return "Saved.";
  });
  const tenants = el("div", { className: "table-wrap" }, el("table", {}, el("caption", { className: "sr", textContent: "Workspaces" }),
    el("thead", {}, el("tr", {}, ["Workspace", "Members", "Projects"].map((h) => el("th", { scope: "col", textContent: h })), el("th", { scope: "col", className: "actions", textContent: "Actions" }))),
    el("tbody", {}, d.tenants.map((t) => el("tr", {}, el("td", {}, el("a", { href: `#t/${t.id}`, textContent: t.name })), el("td", { textContent: t.members }), el("td", { textContent: t.projects }),
      el("td", { className: "actions" }, button("Delete", async () => {
        if (prompt(`This deletes "${t.name}" with all ${t.projects} projects. Type the workspace name to confirm.`) !== t.name) return;
        try { await api(`/api/admin/tenants/${t.id}`, "DELETE"); adminPage(); } catch (err) { show(box, err.message); }
      }, "btn small danger", { ariaLabel: `Delete ${t.name}` })))))));
  const newTenant = form([field("Name", input("name", "text", { required: true, maxLength: 80 }))], "Create workspace",
    async (v) => { const r = await api("/api/admin/tenants", "POST", v); await loadMe(); go(`#t/${r.id}`); }, "inline");
  const act = (u, path, body, done) => async () => { try { const r = await api(`/api/admin/users/${u.id}${path}`, "POST", body); await done(r); } catch (err) { show(box, err.message); } };
  const resetOut = el("div", { hidden: true });
  const users = el("div", { className: "table-wrap" }, el("table", {}, el("caption", { className: "sr", textContent: "Users" }),
    el("thead", {}, el("tr", {}, el("th", { scope: "col", textContent: "User" }), el("th", { scope: "col", className: "optional", textContent: "Status" }), el("th", { scope: "col", className: "actions", textContent: "Actions" }))),
    el("tbody", {}, d.users.map((u) => el("tr", {},
      el("td", {}, el("div", { textContent: u.name }), el("div", { className: "mono mute", textContent: u.email })),
      el("td", { className: "optional", textContent: [u.site_admin ? "site admin" : "", u.disabled ? "disabled" : "", u.totp ? "two-step on" : ""].filter(Boolean).join(", ") || "active" }),
      el("td", { className: "actions" }, u.id === me.id ? el("span", { className: "mute", textContent: "you" }) : [
        button(u.disabled ? "Enable" : "Disable", act(u, "", { disabled: !u.disabled }, adminPage), "btn small", { ariaLabel: `${u.disabled ? "Enable" : "Disable"} ${u.email}` }),
        button(u.site_admin ? "Remove admin" : "Make admin", act(u, "", { site_admin: !u.site_admin }, adminPage), "btn small", { ariaLabel: `${u.site_admin ? "Remove site admin from" : "Make site admin:"} ${u.email}` }),
        button("Reset link", act(u, "/reset", undefined, (r) => { resetOut.hidden = false; resetOut.replaceChildren(el("p", { textContent: `One-time password reset link for ${u.email} (24 hours):` }), input("reset", "text", { readOnly: true, value: r.link, ariaLabel: "Reset link" })); resetOut.querySelector("input").select(); }),
          "btn small", { ariaLabel: `Password reset link for ${u.email}` }),
        u.totp ? button("Remove two-step", act(u, "/totp-off", undefined, adminPage), "btn small", { ariaLabel: `Remove two-step sign-in from ${u.email}` }) : null,
      ]))))));
  const audit = el("div", { className: "table-wrap" }, el("table", {}, el("caption", { className: "sr", textContent: "Audit log" }),
    el("thead", {}, el("tr", {}, ["When", "Who", "What", "Details"].map((h) => el("th", { scope: "col", textContent: h })))),
    el("tbody", {}, d.audit.map((r) => el("tr", {}, el("td", { textContent: new Date(r.at * 1000).toLocaleString() }), el("td", { className: "mono", textContent: r.email || r.ip || "" }),
      el("td", { textContent: r.action.replace(/_/g, " ") }), el("td", { className: "mono", textContent: r.detail || "" }))))));
  page("Site admin", box,
    d.sandbox ? null : el("p", { className: "msg err", role: "alert", textContent: "Builds are NOT sandboxed (--insecure-no-sandbox). Development use only." }),
    el("section", { className: "card", ariaLabel: "Settings" }, el("h2", { textContent: "Settings" }), el("p", { className: "mute", textContent: `${d.workers} project editors running now.` }), settings),
    el("section", { className: "card", ariaLabel: "Workspaces" }, el("h2", { textContent: "Workspaces" }), tenants, el("details", {}, el("summary", { textContent: "New workspace" }), newTenant)),
    el("section", { className: "card", ariaLabel: "Users" }, el("h2", { textContent: "Users" }), resetOut, users),
    el("section", { className: "card", ariaLabel: "Audit log" }, el("details", {}, el("summary", { textContent: `Audit log (last ${d.audit.length} events)` }), audit)));
}

// ---- router ----------------------------------------------------------------------------------------------------
async function route() {
  const hash = location.hash.slice(1);
  const p = params();
  try {
    if (p.has("next")) { sessionStorage.setItem("next", p.get("next")); history.replaceState(null, "", "#login"); }
    if (p.has("invite")) return await invitePage(p.get("invite"));
    if (p.has("reset")) return resetPage(p.get("reset"));
    if (p.has("error")) { history.replaceState(null, "", "#login"); return loginPage(p.get("error")); }
    if (hash === "signup") return me ? go("#home") : signupPage();
    if (!me) return loginPage();
    if (sessionStorage.getItem("next")) return afterLogin();
    if (hash === "account") return await accountPage();
    if (hash === "admin" && me.site_admin) return await adminPage();
    if (hash.startsWith("t/")) return await tenantPage(hash.slice(2));
    if (hash !== "home") { history.replaceState(null, "", "#home"); }
    return await homePage();
  } catch (err) {
    if (err.status === 401) { await loadMe(); return loginPage(); }
    page("Something went wrong", el("p", { className: "msg err", role: "alert", textContent: err.message }), el("p", {}, el("a", { href: "#home", textContent: "Back to projects" })));
  }
}

// ---- boot ------------------------------------------------------------------------------------------------------
const themeKey = "lp-host-theme";
function applyTheme(theme) {
  const dark = theme ? theme === "dark" : matchMedia("(prefers-color-scheme: dark)").matches;
  if (theme) document.documentElement.dataset.theme = theme; else delete document.documentElement.dataset.theme;
  $("theme").setAttribute("aria-pressed", String(dark));
}
try { applyTheme(localStorage.getItem(themeKey)); } catch { applyTheme(null); }
$("theme").onclick = () => {
  const theme = $("theme").getAttribute("aria-pressed") === "true" ? "light" : "dark";
  try { localStorage.setItem(themeKey, theme); } catch { /* private mode */ }
  applyTheme(theme);
};
$("logout").onclick = async () => { try { await api("/api/logout", "POST"); } finally { await loadMe(); go("#login"); } };
window.addEventListener("hashchange", route);
await loadMe();
route();
