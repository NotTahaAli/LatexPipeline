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

// page("Title", ...kids) or page({ title, eyebrow, lead, actions, layout, side }, ...kids). Layouts: "" (heading over the
// content), auth (title page beside the form), solo (one card in the middle), split, narrowpage; side: a menu on the left.
function page(opts, ...kids) {
  const o = typeof opts === "string" ? { title: opts } : opts;
  document.title = `${o.title} · ${info.site || "LaTeX Studio"}`;
  const h1 = el("h1", { textContent: o.title, tabIndex: -1 });
  const eyebrow = o.eyebrow ? el("p", { className: "eyebrow" + (o.bad ? " bad" : ""), textContent: o.eyebrow }) : null;
  main.className = o.layout || (o.side ? "withside" : "");
  delete main.dataset.page;
  if (o.layout === "auth") main.replaceChildren(hero(), el("section", { className: "panel" }, el("div", { className: "inner" }, eyebrow, h1, ...kids)));
  else if (o.layout === "solo") main.replaceChildren(el("div", { className: "card" }, eyebrow, h1, ...kids));
  else {
    const head = el("div", { className: "pagehead" }, el("div", {}, eyebrow, h1, o.lead ? el("p", { className: "lead", textContent: o.lead }) : null),
      o.actions ? el("div", { className: "row" }, o.actions) : null);
    if (o.side) main.replaceChildren(o.side, el("div", { className: "content" }, head, ...kids));
    else main.replaceChildren(head, ...kids);
  }
  for (const a of document.querySelectorAll("#nav a")) {
    if (location.hash.startsWith("#" + a.dataset.nav)) a.setAttribute("aria-current", "page"); else a.removeAttribute("aria-current");
  }
  h1.focus();
}

const when = (t) => new Date(t * 1000).toLocaleDateString(undefined, { year: "numeric", month: "short", day: "numeric" });
const roleName = { admin: "Admin", editor: "Editor", viewer: "Viewer" };
const go = (hash) => { if (location.hash === hash) route(); else location.hash = hash; };
const params = () => new URLSearchParams(location.hash.slice(1));

// A modal for a short form; it is removed when closed, and focus goes back to the button that opened it.
function dialog(title, body) {
  const opener = document.activeElement;
  const id = "dlg" + Math.random().toString(36).slice(2, 8);
  const d = el("dialog", {}, el("div", { className: "dlg-head" }, el("h2", { id, textContent: title }),
    button("\u00d7", () => d.close(), "x", { ariaLabel: "Close" })), el("div", { className: "dlg-body" }, body));
  d.setAttribute("aria-labelledby", id);
  for (const f of d.querySelectorAll("form > .row")) f.prepend(button("Cancel", () => d.close(), "btn ghost"));
  d.onclose = () => { d.remove(); opener?.focus?.(); };
  document.body.append(d);
  d.showModal();
  return d;
}

// ---- sign-in pages ---------------------------------------------------------------------------------------------
function hero() {
  return el("section", { className: "hero", ariaLabel: "About" },
    el("div", {}, el("p", { className: "eyebrow", textContent: "A writing room for papers and theses" }),
      el("p", { className: "display" }, "Write together. ", el("em", { textContent: "Typeset" }), " in seconds."),
      el("p", { className: "blurb", textContent: "Co-edit LaTeX with your group, preview one chapter at a time, and keep every build inside a sandbox on this server." })),
    el("p", { className: "foot", textContent: `${info.site || "LaTeX Studio"} at ${location.host}` }));
}

function providerButtons(invite) {
  if (!info.providers.length) return [];
  const q = invite ? `?invite=${encodeURIComponent(invite)}` : "";
  return [el("p", { className: "or", textContent: "or" }), el("div", { className: "providers" },
    info.providers.map((p) => el("a", { className: "btn lg", href: `/auth/${p.id}/start${q}`, textContent: `Continue with ${p.label}` })))];
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
  page({ title: "Two-step sign-in", eyebrow: "Step 2 of 2", layout: "solo" },
    el("p", { textContent: "Enter the 6-digit code from your authenticator app." }),
    form([field("Code or recovery code", input("code", "text", { required: true, autocomplete: "one-time-code", inputMode: "numeric", maxLength: 14, className: "code" }))],
      "Continue", async (v) => {
        await api("/api/login/code", "POST", { code: v.code });
        await loadMe(); after();
      }),
    el("hr"),
    el("p", { className: "mute", textContent: "Lost your phone? Enter one of your recovery codes in the same field. Each works once." }),
    el("p", {}, button("Back to sign in", async () => { try { await api("/api/logout", "POST"); } finally { await loadMe(); go("#login"); } }, "link")));
}

const afterLogin = () => { const next = sessionStorage.getItem("next"); sessionStorage.removeItem("next"); if (next?.startsWith("/p/")) location.href = next; else go("#home"); };

function loginPage(error) {
  const box = msgBox();
  if (error) show(box, error);
  if (info.stage === "mfa") return codePage(afterLogin);
  page({ title: "Sign in", layout: "auth" }, box, signInForm(afterLogin), ...providerButtons(),
    info.signup !== "invite_only" ? el("p", { className: "center" }, "New here? ", el("a", { href: "#signup", textContent: "Create an account" })) : null);
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
  page({ title: "Create an account", layout: "auth" }, note,
    info.signup === "open" ? signupForm() : null, ...providerButtons(),
    el("p", { className: "center" }, "Have an account? ", el("a", { href: "#login", textContent: "Sign in" })));
}

async function invitePage(token) {
  let invite;
  try { invite = await api(`/api/invites/${encodeURIComponent(token)}`); }
  catch (err) { return errorPage("Invitation", err.message, "Ask whoever sent it for a new link; each works once and expires after 7 days."); }
  const what = el("p", {}, "You are invited to ", el("strong", { textContent: invite.tenant }), ` as ${roleName[invite.role].toLowerCase()}.`);
  if (me) {
    const box = msgBox();
    return page({ title: "Invitation", layout: "solo" }, what, box, button("Join workspace", async () => {
      try { await api(`/api/invites/${encodeURIComponent(token)}/accept`, "POST"); await loadMe(); go("#home"); } catch (err) { show(box, err.message); }
    }, "btn primary lg"));
  }
  page({ title: "Invitation", layout: "auth" }, what,
    el("h2", { textContent: "New to " + info.site + "?" }), signupForm(token, invite.email), ...providerButtons(token),
    el("details", {}, el("summary", { textContent: "I already have an account" }), signInForm(() => go("#invite=" + token))));
}

function resetPage(token) {
  page({ title: "Choose a new password", layout: "auth" }, form([
    field("New password", input("password", "password", { required: true, minLength: info.min_password, autocomplete: "new-password" }),
      `At least ${info.min_password} characters.`),
  ], "Save password", async (v) => {
    await api("/api/reset", "POST", { token, password: v.password });
    history.replaceState(null, "", "#login"); loginPage(); announce("Password saved. Sign in with it.");
  }));
}

// ---- projects --------------------------------------------------------------------------------------------------
async function homePage() {
  const cards = [];
  if (!me.tenants.length) cards.push(el("p", { className: "card", textContent: "You are not in a workspace yet. Ask a workspace admin for an invite link." }));
  page({ title: "Projects", eyebrow: me.tenants.length === 1 ? `${me.tenants[0].name} · workspace` : "Your workspaces" });
  for (const t of me.tenants) cards.push(await tenantCard(t.id));
  main.append(...cards);
}

async function tenantCard(tid) {
  const t = await api(`/api/tenants/${tid}`);
  const canEdit = t.role !== "viewer";
  const box = msgBox();
  const refresh = async () => card.replaceWith(await tenantCard(tid));
  const projects = t.projects.map((p) => el("article", { className: "card project", ariaLabel: p.name },
    el("h3", {}, el("a", { href: `/p/${p.id}/`, textContent: p.name })),
    el("p", { textContent: `Created ${when(p.created)}${p.creator ? " by " + p.creator : ""}` }),
    el("div", { className: "thumb", ariaHidden: "true" }),
    el("div", { className: "row" },
      el("a", { className: "btn primary open", href: `/p/${p.id}/`, textContent: "Open", ariaLabel: `Open ${p.name}` }),
      el("a", { className: "btn", href: `/api/projects/${p.id}/zip`, textContent: "Download", ariaLabel: `Download ${p.name} as zip` }),
      canEdit ? button("Delete", async () => {
        if (!confirm(`Delete "${p.name}" and all its files? This cannot be undone.`)) return;
        try { await api(`/api/projects/${p.id}`, "DELETE"); announce(`Deleted ${p.name}.`); await refresh(); } catch (err) { show(box, err.message); }
      }, "btn ghost danger", { ariaLabel: `Delete ${p.name}` }) : null)));
  const create = () => dialog("New project", form([
    field("Project name", input("name", "text", { required: true, maxLength: 80 })),
    field("Template", el("select", { name: "template" }, info.templates.map((n) => el("option", { value: n, textContent: n[0].toUpperCase() + n.slice(1) })))),
  ], "Create", async (v) => { const r = await api(`/api/tenants/${tid}/projects`, "POST", v); location.href = `/p/${r.id}/`; }));
  const upload = () => { const d = dialog("Upload a zip", form([
    field("Name for the upload", input("name", "text", { required: true, maxLength: 80 })),
    field("Zip file", input("file", "file", { required: true, accept: ".zip,application/zip" }), `main.tex at the top (or in one folder); up to ${t.limits.max_project_mb} MB.`),
  ], "Upload", async (v, f) => {
    const r = await api(`/api/tenants/${tid}/upload?name=${encodeURIComponent(v.name)}`, "POST", undefined, f.file.files[0]);
    d.close(); await refresh();
    const left = r.skipped?.length ? ` Left out (build settings cannot be uploaded): ${r.skipped.join(", ")}.` : "";
    announce(`Uploaded ${v.name}.${left}`);
    if (left) alert(`Uploaded ${v.name}.${left}`);
  })); };
  if (canEdit) projects.push(el("button", { type: "button", className: "start", onclick: create },
    el("span", { className: "title", textContent: "Start a project" }),
    el("span", { className: "hint", textContent: `From a template: ${info.templates.join(", ")}.` }),
    el("span", { className: "btn", ariaHidden: "true", textContent: "Choose a template" })));
  const card = el("section", { className: "workspace", ariaLabel: t.name },
    el("div", { className: "head" }, el("div", {}, el("p", { className: "eyebrow", textContent: `${roleName[t.role]} · workspace` }), el("h2", { textContent: t.name })),
      el("div", { className: "row" },
        t.role === "admin" ? el("a", { className: "btn ghost", href: `#t/${tid}`, textContent: "Members and invites" }) : null,
        canEdit ? button("Upload a zip", upload) : null,
        canEdit ? button("New project", create, "btn primary") : null)),
    box, projects.length ? el("div", { className: "grid" }, projects) : el("p", { className: "empty", textContent: "No projects yet." }),
    el("p", { className: "note", textContent: `${t.projects.length} of ${t.limits.max_projects_per_tenant} projects in this workspace` }));
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
    el("thead", {}, el("tr", {}, el("th", { scope: "col", textContent: "Member" }),
      el("th", { scope: "col", textContent: "Role" }), el("th", { scope: "col", className: "actions" }, el("span", { className: "sr", textContent: "Actions" })))),
    el("tbody", {}, t.members.map((m) => el("tr", {}, el("td", {}, el("div", { className: "who", textContent: m.id === me.id ? `${m.name} (you)` : m.name }),
      el("div", { className: "mono", textContent: m.email })),
      el("td", {}, roleSelect(m)), el("td", { className: "actions" }, button("Remove", async () => {
        if (!confirm(`Remove ${m.name} from ${t.name}?`)) return;
        try { await api(`/api/tenants/${tid}/members/${m.id}`, "DELETE"); await reload(); announce(`Removed ${m.name}.`); } catch (err) { show(box, err.message); }
      }, "btn ghost danger", { ariaLabel: `Remove ${m.name}` })))),
      t.members.length ? null : el("tr", {}, el("td", { colSpan: 3, className: "empty", textContent: "No members yet. Invite people with a link." })))));
  const linkOut = el("div", { hidden: true });
  const roleHelp = { editor: "edit and build", viewer: "read only", admin: "manage members" };
  const invite = form([
    field("Email (optional)", input("email", "email"), "Only this address can use the link."),
    field("Role", el("select", { name: "role" }, ["editor", "viewer", "admin"].map((r) => el("option", { value: r, textContent: `${roleName[r]} \u2014 ${roleHelp[r]}` })))),
  ], "Create invite link", async (v) => {
    const r = await api(`/api/tenants/${tid}/invites`, "POST", { role: v.role, email: v.email || undefined });
    const out = input("link", "text", { readOnly: true, value: r.link, ariaLabel: "Invite link" });
    linkOut.replaceChildren(el("p", { className: "hint", textContent: "Send this link; it works once and expires in 7 days:" }),
      el("div", { className: "row" }, out, button("Copy", async () => { try { await navigator.clipboard.writeText(r.link); announce("Copied."); } catch { out.select(); } })));
    linkOut.hidden = false; out.select();
    renderInvites(await api(`/api/tenants/${tid}`));
  });
  const pending = el("div");
  const renderInvites = (data) => pending.replaceChildren(data.invites.length ? el("ul", { className: "list card" }, data.invites.map((i) => el("li", {},
    el("span", { className: "tag", textContent: roleName[i.role] }),
    el("span", { className: "grow", textContent: `${i.email || "anyone with the link"} · expires ${when(i.expires)} · single use` }),
    button("Revoke", async () => { await api(`/api/tenants/${tid}/invites/${i.id}`, "DELETE"); renderInvites(await api(`/api/tenants/${tid}`)); }, "btn ghost", { ariaLabel: `Revoke invite for ${i.email || i.role}` }))))
    : el("p", { className: "empty", textContent: "No open invites." }));
  renderInvites(t);
  page({ title: t.name, eyebrow: "Workspace · members and invites", layout: "split" },
    el("div", { className: "content" }, box,
      el("section", { ariaLabel: "Members" }, el("h2", { className: "sr", textContent: "Members" }), members),
      el("section", { ariaLabel: "Open invites" }, el("h2", { textContent: "Open invites" }), pending)),
    el("aside", { ariaLabel: "Invites" }, el("section", { className: "card" }, el("h2", { textContent: "Invite people" }), invite, linkOut,
      el("p", { className: "hint", textContent: "Invite links are not e-mailed. Copy and send them yourself." }))));
}

// ---- account ---------------------------------------------------------------------------------------------------
async function accountPage() {
  const a = await api("/api/account");
  const profile = form([field("Name", input("name", "text", { required: true, maxLength: 80, value: a.name, autocomplete: "name" }))],
    "Save", async (v) => { await api("/api/account", "POST", v); await loadMe(); return "Saved."; }, "inline");
  const password = form([
    a.password ? field("Current password", input("current", "password", { required: true, autocomplete: "current-password" })) : null,
    field("New password", input("password", "password", { required: true, minLength: info.min_password, autocomplete: "new-password" }), `At least ${info.min_password} characters.`),
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
            el("ol", { className: "steps" }, el("li", {}, "Add this account to your authenticator app: ", el("a", { href: s.uri, textContent: "open in authenticator app" }),
              " on your phone, or type the key."), el("li", { textContent: "Enter the 6-digit code it shows." })),
            el("p", { className: "hint", textContent: "Key" }), el("code", { className: "key", textContent: s.secret.replace(/(.{4})/g, "$1 ").trim() }),
            form([field("Code from the app", input("code", "text", { required: true, inputMode: "numeric", autocomplete: "one-time-code", maxLength: 6 }))],
              "Turn on", async (v) => {
                const r = await api("/api/account/totp/enable", "POST", { code: v.code });
                a.totp = true; twoStep.replaceChildren(el("p", { className: "msg ok", role: "status", textContent: "Two-step sign-in is on." }), codesList(r.codes),
                  button("I saved them", () => renderTwoStep()));
              }, "inline"));
          twoStep.querySelector("input")?.focus();
        }, "btn primary"));
    } else {
      twoStep.replaceChildren(el("p", {}, el("span", { className: "tag ok", textContent: "On" }), ` ${a.recovery_left ?? ""} recovery codes left.`),
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
  const sect = (title, about, ...body) => el("section", { className: "sect", ariaLabel: title },
    el("div", {}, el("h2", { textContent: title }), el("p", { textContent: about })), el("div", { className: "card" }, ...body));
  const providers = info.providers.length ? sect("Connected sign-in", "Sign in with a provider instead of a password.",
    el("ul", { className: "list" }, info.providers.map((p) => el("li", {}, el("span", { className: "grow" }, el("span", { className: "who", textContent: p.label }),
      linked.has(p.id) ? el("span", { className: "mute", textContent: ` · ${linked.get(p.id).email}` }) : null),
      linked.has(p.id) ? [el("span", { className: "tag ok", textContent: "Connected" }),
        button("Disconnect", async () => { try { await api(`/api/account/identities/${p.id}`, "DELETE"); accountPage(); } catch (err) { alert(err.message); } }, "btn ghost", { ariaLabel: `Disconnect ${p.label}` })]
        : el("a", { className: "btn", href: `/auth/${p.id}/start?intent=link`, textContent: "Connect", ariaLabel: `Connect ${p.label}` }))))) : null;

  page({ title: "Account", eyebrow: a.email, layout: "narrowpage" },
    sect("Profile", "Your name as other members see it.", profile),
    sect("Two-step sign-in", "A code from an authenticator app at every sign-in.", twoStep),
    providers,
    sect("Password", "Changing it signs out your other sessions.", password));
}

// ---- site admin ------------------------------------------------------------------------------------------------
const adminSections = [["signup", "Sign-up"], ["workspaces", "Workspaces"], ["users", "Users"], ["quotas", "Quotas and limits"],
  ["providers", "Sign-in providers"], ["ai", "AI assistant"], ["audit", "Audit log"]];

// One page with every section; the menu on the left (#admin/<section>) scrolls to one.
function adminSection(name) {
  const known = adminSections.some(([id]) => id === name);
  for (const a of document.querySelectorAll(".side a")) {
    if (known && a.hash === "#admin/" + name) a.setAttribute("aria-current", "true"); else a.removeAttribute("aria-current");
  }
  if (!known) return;
  const h2 = document.querySelector(`#s-${name} h2`);
  h2?.scrollIntoView(); h2?.focus();
}

async function adminPage() {
  const d = await api("/api/admin");
  const s = d.settings;
  const box = msgBox();
  const save = (fn) => async (v, f) => { await api("/api/admin/settings", "POST", fn(v, f)); return "Saved."; };
  const num = (name, label, hint) => field(label, input(name, "number", { value: s[name], min: 0, required: true }), hint);
  const modes = [["invite_only", "By invitation only", "Workspace admins send invite links. The default."],
    ["open", "Open to everyone", "Anyone can sign up; open sign-up creates one personal workspace per person."],
    ["open_domains", "Open to these email domains", "Needs a sign-in provider that confirms the address; a password alone cannot prove it."]];
  const signup = form([el("fieldset", { className: "card" }, el("legend", { className: "sr", textContent: "Sign-up mode" }),
    modes.map(([v, t, about]) => el("label", { className: "choice" }, input("signup_mode", "radio", { value: v, checked: v === s.signup_mode }),
      el("span", {}, el("b", { textContent: t }), el("span", { className: "hint", textContent: about })))),
    el("div", { className: "domains" }, field("Email domains", input("signup_domains", "text", { value: s.signup_domains.join(", "), placeholder: "example.org, example.com" }),
      "For the last option, separated by commas.")))],
  "Save sign-up", save((v) => ({ signup_mode: v.signup_mode, signup_domains: v.signup_domains })));
  const providers = form([
    info.providers.length ? el("ul", { className: "list card" }, info.providers.map((p) => el("li", {}, el("span", { className: "grow who", textContent: p.label }),
      el("span", { className: "tag", textContent: p.id }))))
      : el("p", { className: "card", textContent: "None configured. Sign-in providers are set in config.toml on the server (see the README)." }),
    el("label", { className: "check" }, input("auto_link", "checkbox", { checked: s.auto_link }), "Link sign-in providers to existing accounts with the same confirmed email"),
  ], "Save", save((v, f) => ({ auto_link: f.auto_link.checked })));
  const quotaKeys = ["max_projects_per_tenant", "max_project_mb", "max_workers", "max_workers_per_tenant", "worker_idle_minutes", "build_timeout", "max_tenants"];
  const quotas = form([el("div", { className: "card numbers" },
    num("max_projects_per_tenant", "Projects per workspace"), num("max_project_mb", "Megabytes per project"),
    num("max_workers", "Open projects on the server", "Each builds one document at a time."), num("max_workers_per_tenant", "Open projects per workspace"),
    num("worker_idle_minutes", "Close idle projects after (minutes)"), num("build_timeout", "Build time limit (seconds)"),
    num("max_tenants", "Workspaces in total (0 = no limit)", "Open sign-up creates one per person."))],
  "Save limits", save((v) => Object.fromEntries(quotaKeys.map((k) => [k, Number(v[k])]))));
  const tenants = el("div", { className: "table-wrap" }, el("table", {}, el("caption", { className: "sr", textContent: "Workspaces" }),
    el("thead", {}, el("tr", {}, ["Workspace", "Members", "Projects"].map((h) => el("th", { scope: "col", textContent: h })), el("th", { scope: "col", className: "actions" }, el("span", { className: "sr", textContent: "Actions" })))),
    el("tbody", {}, d.tenants.map((t) => el("tr", {}, el("td", {}, el("a", { href: `#t/${t.id}`, textContent: t.name })), el("td", { textContent: t.members }), el("td", { textContent: t.projects }),
      el("td", { className: "actions" }, button("Delete", async () => {
        if (prompt(`This deletes "${t.name}" with all ${t.projects} projects. Type the workspace name to confirm.`) !== t.name) return;
        try { await api(`/api/admin/tenants/${t.id}`, "DELETE"); adminPage(); } catch (err) { show(box, err.message); }
      }, "btn ghost danger", { ariaLabel: `Delete ${t.name}` })))))));
  const newTenant = form([field("Name", input("name", "text", { required: true, maxLength: 80 }))], "Create workspace",
    async (v) => { const r = await api("/api/admin/tenants", "POST", v); await loadMe(); go(`#t/${r.id}`); }, "inline");
  const act = (u, path, body, done) => async () => { try { const r = await api(`/api/admin/users/${u.id}${path}`, "POST", body); await done(r); } catch (err) { show(box, err.message); } };
  const resetOut = el("div", { className: "card", hidden: true });
  const flags = (u) => [u.site_admin ? ["site admin", "info"] : null, u.disabled ? ["disabled", "err"] : null, u.totp ? ["two-step on", "ok"] : null].filter(Boolean);
  const users = el("div", { className: "table-wrap" }, el("table", {}, el("caption", { className: "sr", textContent: "Users" }),
    el("thead", {}, el("tr", {}, el("th", { scope: "col", textContent: "User" }), el("th", { scope: "col", className: "optional", textContent: "Status" }), el("th", { scope: "col", className: "actions" }, el("span", { className: "sr", textContent: "Actions" })))),
    el("tbody", {}, d.users.map((u) => el("tr", {},
      el("td", {}, el("div", { className: "who", textContent: u.name }), el("div", { className: "mono", textContent: u.email })),
      el("td", { className: "optional" }, (flags(u).length ? flags(u) : [["active", ""]]).map(([text, kind]) => el("span", { className: `tag ${kind}`, textContent: text }))),
      el("td", { className: "actions" }, u.id === me.id ? el("span", { className: "mute", textContent: "you" }) : [
        button(u.disabled ? "Enable" : "Disable", act(u, "", { disabled: !u.disabled }, adminPage), "btn small", { ariaLabel: `${u.disabled ? "Enable" : "Disable"} ${u.email}` }),
        button(u.site_admin ? "Remove admin" : "Make admin", act(u, "", { site_admin: !u.site_admin }, adminPage), "btn small", { ariaLabel: `${u.site_admin ? "Remove site admin from" : "Make site admin:"} ${u.email}` }),
        button("Reset link", act(u, "/reset", undefined, (r) => { resetOut.hidden = false; resetOut.replaceChildren(el("p", { textContent: `One-time password reset link for ${u.email} (24 hours):` }), input("reset", "text", { readOnly: true, value: r.link, ariaLabel: "Reset link" })); resetOut.querySelector("input").select(); }),
          "btn small", { ariaLabel: `Password reset link for ${u.email}` }),
        u.totp ? button("Remove two-step", act(u, "/totp-off", undefined, adminPage), "btn small", { ariaLabel: `Remove two-step sign-in from ${u.email}` }) : null,
      ]))))));
  const audit = el("div", { className: "table-wrap", tabIndex: 0, role: "region", ariaLabel: "Audit log entries" }, el("table", {}, el("caption", { className: "sr", textContent: "Audit log" }),
    el("thead", {}, el("tr", {}, ["When", "Who", "What", "Details"].map((h) => el("th", { scope: "col", textContent: h })))),
    el("tbody", {}, d.audit.map((r) => el("tr", {}, el("td", { className: "mute", textContent: new Date(r.at * 1000).toLocaleString() }), el("td", { className: "mono", textContent: r.email || r.ip || "" }),
      el("td", {}, r.action.includes("fail") ? el("span", { className: "tag warn", textContent: r.action.replace(/_/g, " ") }) : r.action.replace(/_/g, " ")),
      el("td", { className: "mono", textContent: r.detail || "" }))))));
  const a = d.ai;
  const aiUsage = el("div", { className: "table-wrap", tabIndex: 0, role: "region", ariaLabel: "AI requests in the last 30 days" }, el("table", {}, el("caption", { className: "sr", textContent: "AI requests in the last 30 days" }),
    el("thead", {}, el("tr", {}, ["Who", "Workspace", "Requests", "Tokens in / out", "Last"].map((h) => el("th", { scope: "col", textContent: h })))),
    el("tbody", {}, a.usage.map((r) => el("tr", {}, el("td", { className: "mono", textContent: r.email || "(deleted)" }), el("td", { textContent: r.workspace || "(deleted)" }),
      el("td", { textContent: r.requests }), el("td", { className: "mono", textContent: `${r.input_tokens.toLocaleString()} / ${r.output_tokens.toLocaleString()}` }),
      el("td", { className: "mute", textContent: r.last_day }))))));
  const aiLead = a.enabled ? `On, model ${a.model}. At most ${a.daily_per_user} requests per person and ${a.daily_per_workspace} per workspace a day. Set in config.toml.`
    : a.configured ? "Enabled in config.toml, but the API key is missing (api_key_env)." : "Off. The operator turns it on in config.toml ([ai]).";
  const sect = (id, title, lead, ...body) => el("section", { className: "adm", id: "s-" + id, ariaLabel: title },
    el("h2", { textContent: title, tabIndex: -1 }), lead ? el("p", { className: "lead", textContent: lead }) : null, ...body);
  const side = el("nav", { className: "side", ariaLabel: "Site admin sections" }, el("p", { className: "eyebrow", textContent: "Sections" }),
    adminSections.map(([id, label]) => el("a", { href: `#admin/${id}`, textContent: label })));
  page({ title: "Site admin", eyebrow: info.site, lead: `${d.workers} project editors running now.`, side }, box,
    d.sandbox ? null : el("p", { className: "msg err", role: "alert", textContent: "Builds are NOT sandboxed (--insecure-no-sandbox). Development use only." }),
    sect("signup", "Sign-up", "Who can create an account on this server.", signup),
    sect("workspaces", "Workspaces", null, tenants, el("details", {}, el("summary", { textContent: "New workspace" }), newTenant)),
    sect("users", "Users", null, resetOut, users),
    sect("quotas", "Quotas and limits", "Limits for every workspace and for the server.", quotas),
    sect("providers", "Sign-in providers", "Configured by the operator in config.toml.", providers),
    sect("ai", "AI assistant", aiLead, a.usage.length ? aiUsage : el("p", { className: "card", textContent: "No AI requests in the last 30 days." })),
    sect("audit", "Audit log", `The last ${d.audit.length} events.`, audit));
  main.dataset.page = "admin";
  const section = location.hash.slice(7);
  if (section) adminSection(section);
}

function errorPage(title, text, help) {
  page({ title, eyebrow: "Error", bad: true, layout: "solo" }, el("p", { className: "msg err", role: "alert", textContent: text }),
    help ? el("p", { className: "mute", textContent: help }) : null, el("hr"),
    el("p", {}, el("a", { href: me ? "#home" : "#login", textContent: me ? "Back to projects" : "Go to sign in" })));
}

// ---- router ----------------------------------------------------------------------------------------------------
async function route() {
  const hash = location.hash.slice(1);
  const p = params();
  try {
    if (p.has("next")) { sessionStorage.setItem("next", p.get("next")); history.replaceState(null, "", "#login"); }
    if (p.has("invite")) return await invitePage(p.get("invite"));
    if (p.has("reset")) return resetPage(p.get("reset"));
    if (p.has("error")) { history.replaceState(null, "", "#login"); return loginPage(p.get("error").slice(0, 300)); }  // shown as text only
    if (hash === "signup") return me ? go("#home") : signupPage();
    if (!me) return loginPage();
    if (sessionStorage.getItem("next")) return afterLogin();
    if (hash === "account") return await accountPage();
    if (hash.startsWith("admin/") && main.dataset.page === "admin") return adminSection(hash.slice(6));
    if ((hash === "admin" || hash.startsWith("admin/")) && me.site_admin) return await adminPage();
    if (hash.startsWith("t/")) return await tenantPage(hash.slice(2));
    if (hash !== "home") { history.replaceState(null, "", "#home"); }
    return await homePage();
  } catch (err) {
    if (err.status === 401) { await loadMe(); return loginPage(); }
    errorPage("Something went wrong", err.message);
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
