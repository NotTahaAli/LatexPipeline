// Account page: personal API tokens for the remote compile API (POST /api/v1/compile, compile_api.py).
// A new token is shown once; the server keeps only its hash. Everything a person or the server typed goes in via
// textContent (the el() helper sets properties, never HTML).

export async function apiTokensSection(sect, { api, el, button, form, field, input, when, announce, rerender }) {
  let data;
  try { data = await api("/api/account/tokens"); } catch (err) { if (err.status === 404) return null; throw err; }
  const shown = el("div", { hidden: true });
  const list = data.tokens.length ? el("ul", { className: "list" }, data.tokens.map((t) => el("li", {},
    el("span", { className: "grow" }, el("span", { className: "who", textContent: t.name }),
      el("span", { className: "hint block", textContent: `made ${when(t.created)} · ${t.expires ? `expires ${when(t.expires)}` : "never expires"} · ${t.used ? `last used ${when(t.used)}` : "not used yet"}` })),
    t.scopes.map((s) => el("span", { className: "tag info", textContent: s })),
    button("Revoke", async () => {
      if (!confirm(`Revoke the token "${t.name}"? Scripts using it stop working at once.`)) return;
      try { await api(`/api/account/tokens/${t.id}`, "DELETE"); announce(`Token ${t.name} revoked.`); rerender(); } catch (err) { alert(err.message); }
    }, "btn ghost danger", { ariaLabel: `Revoke token ${t.name}` }))))
    : el("p", { className: "empty", textContent: "No API tokens." });
  const days = el("select", { name: "days" }, [["30", "30 days"], ["90", "90 days"], ["366", "1 year"], ["", "Never"]]
    .map(([value, text]) => el("option", { value, textContent: text, selected: value === "90" })));
  const create = form([
    field("Token name", input("name", "text", { required: true, maxLength: 80, autocomplete: "off", placeholder: "laptop script" })),
    field("Expires after", days),
  ], "Create token", async (v, f) => {
    const made = await api("/api/account/tokens", "POST", { name: v.name, scopes: ["compile"], days: v.days ? Number(v.days) : null });
    f.reset();
    const code = el("code", { className: "key", textContent: made.token });
    shown.replaceChildren(el("p", { className: "msg ok", role: "status", textContent: "Copy the token now: it is not shown again." }), code,
      el("p", { className: "hint", textContent: `curl --data-binary @paper.zip -H "Content-Type: application/zip" -H "Authorization: Bearer ${made.token.slice(0, 9)}..." -o paper.pdf ${data.endpoint}` }),
      button("Done", () => rerender()));
    shown.hidden = false;
    return "";
  }, "inline");
  return sect("API tokens", `For scripts and CI: POST a zip, a tar archive or a JSON files map to ${data.endpoint} with "Authorization: Bearer <token>"; the answer is the PDF, or the log when the build fails. A token acts as you; at most ${data.max}. Changing your password revokes them all.`,
    shown, list, create);
}
