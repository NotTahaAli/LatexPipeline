"""
End-to-end check of the hosted mode in a real browser (not part of `unittest discover`):

    uv run --no-project --with playwright==1.56.0 python tests/host_e2e.py [--insecure-no-sandbox] [--headed]

init -> site admin signs in, turns on two-step sign-in, signs in again with a code -> creates a workspace and
invites two editors and a viewer -> each signs up in their own browser -> an editor creates a project from the
report template, edits and builds it, a second editor co-edits it live -> the viewer can read but not write ->
someone from another workspace gets 404 for every URL of it -> sign-out and session expiry end access. axe-core
(pinned, from cdnjs, only here) must report no violations on every host page, light and dark, desktop and phone.
Needs LaTeX, and bubblewrap unless --insecure-no-sandbox. The editor itself loads its libraries from CDNs.
"""

from __future__ import annotations

import argparse
import os
import re
import socket
import sqlite3
import subprocess
import sys
import tempfile
import time
from pathlib import Path

from playwright.sync_api import expect, sync_playwright

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "scripts"))
import host  # noqa: E402

AXE = "https://cdnjs.cloudflare.com/ajax/libs/axe-core/4.10.2/axe.min.js"
PASSWORD = "password-123"
CHECKS: list[str] = []
SEEN_LINKS: list[str] = []
LOADED = "() => window.__app && window.__app.active && window.__app.view.state.doc.length > 0"
BUILT = "() => { const d = window.__app.docs[window.__app.cur]; return d && d.status === 'ok' && d.pages > 0; }"
REBUILT = ("() => { const d = window.__app.docs[window.__app.cur]; "
           "return d.status === 'ok' && d.finished * 1000 > Date.now() - 60000; }")


def ok(text: str) -> None:
    CHECKS.append(text)
    print(f"PASS  {text}", flush=True)


def free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


def axe(page, label: str) -> None:
    """No axe violations on this page, in the light and the dark theme."""
    for theme in ("light", "dark"):
        page.evaluate("t => document.documentElement.dataset.theme = t", theme)
        if not page.evaluate("() => !!window.axe"):
            page.add_script_tag(url=AXE)
        page.evaluate("() => document.querySelectorAll('details').forEach(d => d.open = true)")
        result = page.evaluate("() => axe.run(document, {resultTypes: ['violations']})")
        bad = [f"{v['id']}: {v['help']} ({len(v['nodes'])}x: {v['nodes'][0]['target']})" for v in result["violations"]]
        assert not bad, f"axe on {label} ({theme}): {bad}"
    page.evaluate("() => { delete document.documentElement.dataset.theme; "
                  "document.querySelectorAll('details').forEach(d => d.open = false); }")
    ok(f"axe: 0 violations on {label} (light and dark)")


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--insecure-no-sandbox", action="store_true")
    parser.add_argument("--headed", action="store_true")
    args = parser.parse_args()
    port = free_port()
    origin = f"http://127.0.0.1:{port}"
    with tempfile.TemporaryDirectory() as tmp:
        data = Path(tmp) / "data"
        env = {**os.environ, "LP_ADMIN_EMAIL": "root@example.org", "LP_ADMIN_PASSWORD": PASSWORD,
               "LP_ADMIN_NAME": "Root"}
        subprocess.run([sys.executable, str(ROOT / "scripts/host.py"), "init", "--data", str(data)], env=env,
                       check=True, stdout=subprocess.DEVNULL)
        (data / "config.toml").write_text(f'public_url = "{origin}"\nport = {port}\nsession_idle_hours = 1\n')
        command = [sys.executable, str(ROOT / "scripts/host.py"), "serve", "--data", str(data)]
        if args.insecure_no_sandbox:
            command.append("--insecure-no-sandbox")
        server = subprocess.Popen(command, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)
        try:
            deadline = time.monotonic() + 20
            while time.monotonic() < deadline:
                try:
                    socket.create_connection(("127.0.0.1", port), timeout=1).close()
                    break
                except OSError:
                    time.sleep(0.2)
            with sync_playwright() as pw:
                browser = pw.chromium.launch(headless=not args.headed)
                run(browser, origin, data, args.insecure_no_sandbox)
                browser.close()
        finally:
            server.terminate()
            out, _ = server.communicate(timeout=20)
            if os.environ.get("E2E_LOG"):
                print(out)
    print(f"\nAll {len(CHECKS)} checks passed.")
    return 0


def context(browser, origin, **kw):
    ctx = browser.new_context(base_url=origin, bypass_csp=True, **kw)  # bypass_csp: only so axe can be injected
    page = ctx.new_page()
    page.on("pageerror", lambda exc: print(f"page error: {exc}", flush=True))
    return ctx, page


def sign_in(page, email, code_for=None):
    page.goto("/#login")
    page.get_by_label("Email").fill(email)
    page.get_by_label("Password").fill(PASSWORD)
    page.get_by_role("button", name="Sign in").click()
    if code_for:
        expect(page.get_by_role("heading", name="Two-step sign-in")).to_be_visible()
        page.get_by_label("Code").fill(code_for())
        page.get_by_role("button", name="Continue").click()
    expect(page.get_by_role("heading", name="Projects")).to_be_visible()


def sign_up_with(browser, origin, link, name, email):
    ctx, page = context(browser, origin)
    page.goto(link)
    expect(page.get_by_text("You are invited to")).to_be_visible()
    if not any("invite page" in c for c in CHECKS):
        axe(page, "invite page")
    signup = page.locator("form").first
    signup.get_by_label("Name").fill(name)
    signup.get_by_label("Email").fill(email)
    signup.get_by_label("Password").fill(PASSWORD)
    signup.get_by_role("button", name="Create account").click()
    expect(page.get_by_role("heading", name="Projects")).to_be_visible()
    return ctx, page


def invite(page, role):
    page.locator("form select[name=role]").select_option(role)
    page.get_by_role("button", name="Create invite link").click()
    field = page.get_by_label("Invite link")
    expect(field).to_have_value(re.compile("#invite="))
    links = SEEN_LINKS
    page.wait_for_function("seen => { const f = document.querySelector('input[name=link]'); "
                           "return f && !seen.includes(f.value); }", arg=links)
    links.append(field.input_value())
    return links[-1]


def editor_text(page) -> str:
    return page.evaluate("() => window.__app.view.state.doc.toString()")


def run(browser, origin, data: Path, insecure: bool) -> None:
    # --- site admin: sign in, two-step, a workspace, invites ------------------------------------------------
    actx, admin = context(browser, origin)
    admin.goto("/")
    expect(admin.get_by_role("heading", name="Sign in")).to_be_visible()
    axe(admin, "sign-in page")
    sign_in(admin, "root@example.org")
    ok("site admin signs in with a password")
    admin.goto("/#account")
    admin.get_by_role("button", name="Set up two-step sign-in").click()
    secret = admin.locator("code").inner_text().replace(" ", "")
    code = lambda: host.totp_code(secret, int(time.time() // 30))  # noqa: E731
    admin.get_by_label("Code from the app").fill(code())
    admin.get_by_role("button", name="Turn on").click()
    expect(admin.get_by_text("Two-step sign-in is on.")).to_be_visible()
    assert admin.locator("ol.codes li").count() == 10
    admin.get_by_role("button", name="I saved them").click()
    axe(admin, "account page")
    admin.get_by_role("button", name="Sign out").click()
    expect(admin.get_by_role("heading", name="Sign in")).to_be_visible()
    time.sleep(31 - time.time() % 30)  # the enabling code was used; wait for the next one
    sign_in(admin, "root@example.org", code)
    ok("two-step sign-in: password, then a TOTP code")

    admin.goto("/#admin")
    expect(admin.get_by_role("heading", name="Site admin")).to_be_visible()
    if insecure:
        expect(admin.get_by_text("NOT sandboxed")).to_be_visible()
    axe(admin, "site admin page")
    for name in ("Team A", "Team B"):
        admin.goto("/#admin")
        admin.get_by_text("New workspace").click()
        admin.get_by_label("Name").fill(name)
        admin.get_by_role("button", name="Create workspace").click()
        expect(admin.get_by_role("heading", name=name)).to_be_visible()
        if name == "Team A":
            tenant_a = admin.url.split("#t/")[1]
            editor_link, editor2_link = invite(admin, "editor"), invite(admin, "editor")
            viewer_link = invite(admin, "viewer")
            axe(admin, "workspace members page")
        else:
            outsider_link = invite(admin, "admin")
    ok("site admin creates workspaces and invite links")

    # --- members sign up in their own browsers --------------------------------------------------------------
    ectx, ed = sign_up_with(browser, origin, editor_link, "Ed Itor", "ed@a.org")
    e2ctx, ed2 = sign_up_with(browser, origin, editor2_link, "Eve Two", "eve@a.org")
    vctx, vi = sign_up_with(browser, origin, viewer_link, "Vic Viewer", "vic@a.org")
    octx, out = sign_up_with(browser, origin, outsider_link, "Olga Other", "olga@b.org")
    ok("two editors, a viewer and another workspace's admin sign up through invite links")
    axe(vi, "projects page (viewer)")
    expect(vi.get_by_text("New project")).to_have_count(0)

    # --- an editor creates a project from the report template, edits, builds --------------------------------
    ed.get_by_text("New project").click()
    ed.get_by_label("Project name").fill("Thesis")
    ed.locator("select[name=template]").first.select_option("report")
    ed.get_by_role("button", name="Create").click()
    ed.wait_for_url(re.compile(r"/p/[0-9a-f]{16}/$"))
    pid = ed.url.rstrip("/").split("/p/")[1]
    project_url = f"/p/{pid}/"
    ed.wait_for_function(LOADED, timeout=30000)
    assert "\\tableofcontents" in editor_text(ed)
    ed.wait_for_function(BUILT,
                         timeout=120000)
    ok("editor creates a project from the report template; it builds to a PDF")

    ed2.goto(project_url)
    ed2.wait_for_function(LOADED, timeout=30000)
    ed.locator(".cm-content").first.click()
    ed.keyboard.press("Control+End")
    ed.keyboard.type("\n% co-edited line from Ed\n")
    ed2.wait_for_function("() => window.__app.view.state.doc.toString().includes('co-edited line from Ed')",
                          timeout=20000)
    ed2.locator(".cm-content").first.click()
    ed2.keyboard.press("Control+End")
    ed2.keyboard.type("% and a reply from Eve\n")
    ed.wait_for_function("() => window.__app.view.state.doc.toString().includes('reply from Eve')", timeout=20000)
    ok("two editors co-edit the same file live")
    source = next((data / "projects" / tenant_a / pid).glob("*/main.tex"))
    deadline = time.monotonic() + 30
    while "reply from Eve" not in source.read_text() and time.monotonic() < deadline:
        time.sleep(0.5)
    assert "reply from Eve" in source.read_text(), "the shared edit was not saved"
    ed.wait_for_function(REBUILT,
                         timeout=120000)
    ok("the shared edit is saved to disk and rebuilt")
    if not insecure:
        log = next((data / "projects" / tenant_a / pid / ".out").glob("*.log")).read_text()
        assert "SUCCESS" in log.splitlines()[0], log[:300]

    # --- the viewer can read but not write --------------------------------------------------------------------
    vi.goto(project_url)
    vi.wait_for_function("() => window.__app && window.__app.active", timeout=30000)
    expect(vi.locator("#roleChip")).to_contain_text("View only")
    expect(vi.locator("#rebuildBtn")).to_be_disabled()
    status = vi.evaluate("""async () => (await fetch('api/file?doc=Thesis&path=main.tex', {method: 'PUT',
        headers: {'Content-Type': 'application/json'}, body: JSON.stringify({text: 'pwned'})})).status""")
    assert status == 403, status
    assert "pwned" not in source.read_text()
    ok("viewer opens the project read-only; writes are refused (403)")

    # --- tenant isolation -------------------------------------------------------------------------------------
    for path in (project_url, f"/p/{pid}/api/file?doc=Thesis&path=main.tex", f"/api/projects/{pid}/zip",
                 f"/api/tenants/{tenant_a}", f"/p/{pid}/pdf/Thesis"):
        status = out.request.get(origin + path).status
        assert status == 404, (path, status)
    guess = f"/p/{pid[:-1]}{'0' if pid[-1] != '0' else '1'}/"
    assert out.request.get(origin + guess).status == 404
    ok("a user of another workspace gets 404 for the project by URL or ID guess")

    # --- sign-out and session expiry -----------------------------------------------------------------------
    ed2.goto("/#home")
    ed2.get_by_role("button", name="Sign out").click()
    expect(ed2.get_by_role("heading", name="Sign in")).to_be_visible()
    assert ed2.request.get(origin + f"/p/{pid}/api/health").status == 401
    ok("after signing out, the project answers 401")
    with sqlite3.connect(data / "host.db") as db:
        db.execute("UPDATE sessions SET expires = ? WHERE user_id = (SELECT id FROM users WHERE email = 'vic@a.org')",
                   (time.time() - 1,))
    assert vi.request.get(origin + f"/p/{pid}/api/health").status == 401
    vi.goto("/#home")
    expect(vi.get_by_role("heading", name="Sign in")).to_be_visible()
    ok("an expired session is signed out")

    # --- phone width ----------------------------------------------------------------------------------------
    mctx, phone = context(browser, origin, viewport={"width": 375, "height": 740}, is_mobile=True)
    sign_in(phone, "ed@a.org")
    axe(phone, "projects page on a phone")
    width = phone.evaluate("() => document.documentElement.scrollWidth")
    assert width <= 375, f"horizontal scroll on a phone: {width}px"
    ok("projects page fits a phone screen")
    for ctx in (actx, ectx, e2ctx, vctx, octx, mctx):
        ctx.close()


if __name__ == "__main__":
    sys.exit(main())
