// Account page: personal API tokens (host_tokens.py), one system for every use. A token carries the scopes picked
// here: "projects" opens the chosen projects with your role in them (VS Code extension, scripts); "compile" calls
// the remote compile API (POST /api/v1/compile, compile_api.py), offered only when the server has it on.
// A new token is shown once; the server keeps only its hash. Everything a person or the server typed goes in via
// textContent (the el() helper sets properties, never HTML).

export async function apiTokensSection(sect, { api, el, button, form, field, input, when, announce, rerender, tenants }) {
  const data = await api("/api/account/tokens");
  const projects = [];
  for (const t of tenants) {
    try { for (const p of (await api(`/api/tenants/${t.id}`)).projects) projects.push({ ...p, tenant: t.name }); } catch { /* not readable */ }
  }
  const shown = el("div", { hidden: true });
  const expiry = (t) => t.expires ? (t.expires * 1000 < Date.now() ? "expired" : `expires ${when(t.expires)}`) : "never expires";
  const list = data.tokens.length ? el("ul", { className: "list" }, data.tokens.map((t) => el("li", {},
    el("span", { className: "grow" }, el("span", { className: "who", textContent: t.name }),
      el("span", { className: "hint block", textContent: [t.projects.length ? t.projects.map((p) => p.name).join(", ") : null,
        `made ${when(t.created)}`, expiry(t), t.used ? `last used ${when(t.used)}` : "not used yet"].filter(Boolean).join(" · ") })),
    t.scopes.map((s) => el("span", { className: "tag info", textContent: s })),
    button("Revoke", async () => {
      if (!confirm(`Revoke the token "${t.name}"? Anything using it stops working at once.`)) return;
      try { await api(`/api/account/tokens/${t.id}`, "DELETE"); announce(`Token ${t.name} revoked.`); rerender(); } catch (err) { alert(err.message); }
    }, "btn ghost danger", { ariaLabel: `Revoke token ${t.name}` }))))
    : el("p", { className: "empty", textContent: "No API tokens." });
  const scopes = el("fieldset", { className: "card" }, el("legend", { className: "eyebrow", textContent: "What it may do" }),
    Object.entries(data.scopes).map(([name, text]) => el("label", { className: "check" },
      input("scope", "checkbox", { value: name, checked: name === "projects" }), text)));
  const boxes = el("fieldset", { className: "card" }, el("legend", { className: "eyebrow", textContent: "Projects it may open (projects scope)" }),
    projects.length ? projects.map((p) => el("label", { className: "check" }, input("project", "checkbox", { value: p.id }), `${p.name} (${p.tenant})`))
      : el("p", { className: "empty", textContent: "No projects yet." }));
  const days = el("select", { name: "days" }, [["30", "30 days"], ["90", "90 days"], ["366", "1 year"], ["", "Never"]]
    .map(([value, text]) => el("option", { value, textContent: text, selected: value === "90" })));
  const create = form([
    field("Token name", input("name", "text", { required: true, maxLength: 80, autocomplete: "off", placeholder: "VS Code on my laptop" })),
    scopes, boxes, field("Expires after", days),
  ], "Create token", async (v, f) => {
    const picked = (name) => [...f.querySelectorAll(`[name=${name}]:checked`)].map((b) => b.value);
    const scope = picked("scope"), chosen = scope.includes("projects") ? picked("project") : [];
    if (!scope.length) throw new Error("Pick what the token may do.");
    if (scope.includes("projects") && !chosen.length) throw new Error("Pick at least one project.");
    const made = await api("/api/account/tokens", "POST", { name: v.name, scopes: scope, projects: chosen, days: v.days ? Number(v.days) : null });
    f.reset();
    shown.replaceChildren(el("p", { className: "msg ok", role: "status", textContent: "Copy the token now: it is not shown again." }),
      el("code", { className: "key", textContent: made.token }),
      made.scopes.includes("compile") && data.compile_url ? el("p", { className: "hint", textContent: `curl --data-binary @paper.zip -H "Content-Type: application/zip" -H "Authorization: Bearer ${made.token.slice(0, 9)}..." -o paper.pdf ${data.compile_url}` }) : null,
      button("Done", () => rerender()));
    shown.hidden = false;
    return "";
  });
  const compile = data.compile_url ? ` With the compile scope, POST a zip, a tar archive or a JSON files map to ${data.compile_url}; the answer is the PDF, or the log when the build fails.` : "";
  return sect("API tokens", `For the VS Code extension and scripts: send a token as "Authorization: Bearer <token>". It acts as you: the projects scope opens only the projects you pick, with your role in them.${compile} At most ${data.max}. Changing your password ends all of them.`,
    shown, list, create);
}
