"""
End-to-end check of AI clients on the hosted mode (MCP + OAuth) in a real browser (not part of `unittest discover`):

    uv run --no-project --with playwright==1.56.0 python tests/mcp_e2e.py [--insecure-no-sandbox] [--headed]
        [--chromium PATH]

host.py with [mcp] enabled -> a person with two-step sign-in and a project -> this script plays the AI client:
401 challenge -> protected resource and authorization server metadata -> registration (RFC 7591, loopback
redirect) -> the browser opens /oauth/authorize -> sign-in with password and TOTP -> consent page (axe: light, dark,
phone) -> Allow -> code at the redirect, state and iss checked -> token (PKCE) -> MCP over Streamable HTTP:
initialize, tools/list, build, read_file, edit_file, render_page (a PNG), search -> refresh rotation -> the account
page lists the connection (axe) and Disconnect ends it. Needs LaTeX, pdftoppm, and bubblewrap unless
--insecure-no-sandbox.
"""

from __future__ import annotations

import argparse
import base64
import hashlib
import http.client
import json
import os
import secrets
import socket
import subprocess
import sys
import tempfile
import time
from pathlib import Path
from urllib.parse import parse_qs, urlencode, urlsplit

from playwright.sync_api import expect, sync_playwright

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "scripts"))
import host  # noqa: E402

AXE = "https://cdnjs.cloudflare.com/ajax/libs/axe-core/4.10.2/axe.min.js"
PASSWORD = "password-123"
CHECKS: list[str] = []


def ok(text: str) -> None:
    CHECKS.append(text)
    print(f"PASS  {text}", flush=True)


def free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


class Http:
    """A tiny client for the gateway (no proxies): JSON or form bodies, a cookie jar and the CSRF token."""

    def __init__(self, port: int) -> None:
        self.port, self.cookies, self.csrf = port, {}, None

    def __call__(self, method, path, body=None, form=None, headers=None, origin=True):
        conn = http.client.HTTPConnection("127.0.0.1", self.port, timeout=600)
        hdrs = {"Host": f"127.0.0.1:{self.port}", **(headers or {})}
        raw = None
        if body is not None:
            raw, hdrs["Content-Type"] = json.dumps(body).encode(), "application/json"
        if form is not None:
            raw, hdrs["Content-Type"] = urlencode(form).encode(), "application/x-www-form-urlencoded"
        if origin and method != "GET":
            hdrs.setdefault("Origin", f"http://127.0.0.1:{self.port}")
        if self.cookies:
            hdrs["Cookie"] = "; ".join(f"{k}={v}" for k, v in self.cookies.items())
        if self.csrf and method != "GET":
            hdrs["X-CSRF-Token"] = self.csrf
        conn.request(method, path, raw, hdrs)
        res = conn.getresponse()
        data = res.read()
        for cookie in res.headers.get_all("Set-Cookie") or []:
            name, value = cookie.split(";")[0].split("=", 1)
            self.cookies[name] = value
        try:
            data = json.loads(data)
        except ValueError:
            pass
        if isinstance(data, dict) and data.get("csrf"):
            self.csrf = data["csrf"]
        return res.status, data, res


def axe(page, label: str) -> None:
    shots = os.environ.get("E2E_SHOTS")  # a folder: screenshots of each checked page, for a look by eye
    if shots:
        page.screenshot(path=str(Path(shots) / (label.replace(" ", "-") + ".png")), full_page=True)
    for theme in ("light", "dark"):
        page.evaluate("t => document.documentElement.dataset.theme = t", theme)
        if not page.evaluate("() => !!window.axe"):
            page.add_script_tag(url=AXE)
        result = page.evaluate("() => axe.run(document, {resultTypes: ['violations']})")
        bad = [f"{v['id']}: {v['help']} ({len(v['nodes'])}x: {v['nodes'][0]['target']})" for v in result["violations"]]
        assert not bad, f"axe on {label} ({theme}): {bad}"
    page.evaluate("() => { delete document.documentElement.dataset.theme; }")
    ok(f"axe: 0 violations on {label} (light and dark)")


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--insecure-no-sandbox", action="store_true")
    parser.add_argument("--headed", action="store_true")
    parser.add_argument("--chromium", help="Chromium executable (default: Playwright's)")
    args = parser.parse_args()
    port = free_port()
    origin = f"http://127.0.0.1:{port}"
    with tempfile.TemporaryDirectory() as tmp:
        data = Path(tmp) / "data"
        env = {**os.environ, "LP_ADMIN_EMAIL": "root@example.org", "LP_ADMIN_PASSWORD": PASSWORD,
               "LP_ADMIN_NAME": "Root"}
        subprocess.run([sys.executable, str(ROOT / "scripts/host.py"), "init", "--data", str(data)], env=env,
                       check=True, stdout=subprocess.DEVNULL)
        (data / "config.toml").write_text(f'public_url = "{origin}"\nport = {port}\n\n[mcp]\nenabled = true\n')
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
                browser = pw.chromium.launch(headless=not args.headed, executable_path=args.chromium)
                run(browser, origin, port)
                browser.close()
        finally:
            server.terminate()
            out, _ = server.communicate(timeout=20)
            if os.environ.get("E2E_LOG"):
                print(out)
    print(f"\nAll {len(CHECKS)} checks passed.")
    return 0


def run(browser, origin: str, port: int) -> None:
    # --- the person: a workspace with a project, two-step sign-in ---------------------------------------------------
    admin = Http(port)
    assert admin("POST", "/api/login", {"email": "root@example.org", "password": PASSWORD})[0] == 200
    tid = admin("POST", "/api/admin/tenants", {"name": "Lab"})[1]["id"]
    status, made, _ = admin("POST", f"/api/tenants/{tid}/invites", {"role": "editor"})
    token = made["link"].split("#invite=")[1]
    person = Http(port)
    assert person("POST", "/api/signup", {"name": "Ada", "email": "ada@example.org", "password": PASSWORD,
                                          "invite": token})[0] == 200
    pid = person("POST", f"/api/tenants/{tid}/projects", {"name": "Paper", "template": "article"})[1]["id"]
    secret = person("POST", "/api/account/totp/start")[1]["secret"]
    code = lambda: host.totp_code(secret, int(time.time() // 30))  # noqa: E731
    assert person("POST", "/api/account/totp/enable", {"code": code()})[0] == 200
    ok("a person with a project and two-step sign-in")

    # --- the AI client: discovery and registration ----------------------------------------------------------------
    client = Http(port)
    status, _, res = client("POST", "/mcp", {"jsonrpc": "2.0", "id": 1, "method": "initialize"}, origin=False)
    challenge = res.getheader("WWW-Authenticate")
    assert status == 401 and "resource_metadata=" in challenge, challenge
    prm_url = challenge.split('resource_metadata="')[1].split('"')[0]
    prm = client("GET", urlsplit(prm_url).path)[1]
    meta = client("GET", "/.well-known/oauth-authorization-server")[1]
    assert prm["resource"] == origin + "/mcp" and prm["authorization_servers"] == [origin]
    assert "S256" in meta["code_challenge_methods_supported"] and meta["authorization_response_iss_parameter_supported"]
    ok("401 challenge -> protected resource metadata -> authorization server metadata")
    callback = f"http://127.0.0.1:{free_port()}/callback"
    status, reg, _ = client("POST", "/oauth/register", {"client_name": "E2E Client", "redirect_uris": [callback],
                                                        "token_endpoint_auth_method": "none"}, origin=False)
    assert status == 201, reg
    ok("dynamic client registration with a loopback redirect")

    # --- the browser: authorize, sign in with 2FA, consent ---------------------------------------------------------
    verifier, state = secrets.token_urlsafe(48), secrets.token_urlsafe(12)
    challenge_s256 = base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).decode().rstrip("=")
    authorize = meta["authorization_endpoint"] + "?" + urlencode({
        "response_type": "code", "client_id": reg["client_id"], "redirect_uri": callback, "state": state,
        "code_challenge": challenge_s256, "code_challenge_method": "S256", "resource": prm["resource"],
        "scope": " ".join(prm["scopes_supported"])})
    ctx = browser.new_context(base_url=origin, bypass_csp=True)  # bypass_csp: only so axe can be injected
    page = ctx.new_page()
    page.on("pageerror", lambda exc: print(f"page error: {exc}", flush=True))
    landed: list[str] = []
    page.route(callback.rsplit("/", 1)[0] + "/**", lambda route: (landed.append(route.request.url),
                                                                  route.fulfill(body="You can close this tab.")))
    page.goto(authorize)
    expect(page.get_by_role("heading", name="Sign in")).to_be_visible()
    page.get_by_label("Email").fill("ada@example.org")
    page.get_by_label("Password").fill(PASSWORD)
    page.get_by_role("button", name="Sign in").click()
    expect(page.get_by_role("heading", name="Two-step sign-in")).to_be_visible()
    time.sleep(31 - time.time() % 30)  # the enabling code was used; wait for the next one
    page.get_by_label("Code").fill(code())
    page.get_by_role("button", name="Continue").click()
    expect(page.get_by_role("heading", name="Connect an AI client")).to_be_visible()
    expect(page.get_by_text("E2E Client")).to_be_visible()
    expect(page.get_by_text("This app runs on a computer")).to_be_visible()  # loopback redirect: a warning
    ok("authorize -> sign-in with password and TOTP -> consent page for the client")
    axe(page, "consent page")
    page.set_viewport_size({"width": 390, "height": 844})
    axe(page, "consent page at phone width")
    page.set_viewport_size({"width": 1280, "height": 800})
    page.get_by_role("button", name="Allow").click()
    expect(page.get_by_text("Choose at least one workspace or project.")).to_be_visible()
    page.get_by_label("Paper").check()
    page.get_by_role("button", name="Allow").click()
    page.wait_for_function("() => true")
    deadline = time.monotonic() + 15
    while not landed and time.monotonic() < deadline:
        page.wait_for_timeout(100)
    query = parse_qs(urlsplit(landed[0]).query)
    assert query["state"] == [state] and query["iss"] == [origin], query
    ok("Allow -> redirect with the code, state and iss")

    # --- tokens and MCP ---------------------------------------------------------------------------------------------
    status, tokens, _ = client("POST", "/oauth/token", form={
        "grant_type": "authorization_code", "code": query["code"][0], "code_verifier": verifier,
        "client_id": reg["client_id"], "redirect_uri": callback, "resource": prm["resource"]}, origin=False)
    assert status == 200 and tokens["scope"] == "read write review", tokens
    ok("code + PKCE verifier -> access and refresh token")

    def mcp(message, token=None, version="2025-06-18"):
        hdrs = {"Authorization": f"Bearer {token or tokens['access_token']}", "MCP-Protocol-Version": version,
                "Accept": "application/json, text/event-stream"}
        return client("POST", "/mcp", message, headers=hdrs, origin=False)

    def call(name, **arguments):
        status, data, _ = mcp({"jsonrpc": "2.0", "id": 3, "method": "tools/call",
                               "params": {"name": name, "arguments": arguments}})
        assert status == 200 and not data["result"]["isError"], (name, data)
        return data["result"]

    status, init, res = mcp({"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {
        "protocolVersion": "2025-06-18", "capabilities": {}, "clientInfo": {"name": "e2e", "version": "1"}}})
    assert status == 200 and init["result"]["protocolVersion"] == "2025-06-18"
    names = {t["name"] for t in mcp({"jsonrpc": "2.0", "id": 2, "method": "tools/list"})[1]["result"]["tools"]}
    assert {"build", "read_file", "edit_file", "render_page", "search", "fetch"} <= names, names
    ok(f"MCP initialize and tools/list ({len(names)} tools)")
    assert [p["project"] for p in call("list_projects")["structuredContent"]["projects"]] == [pid]
    report = call("build", project=pid)["structuredContent"]
    assert report["ok"] and report["pages"] >= 1, report
    ok(f"tools/call build in the sandboxed worker: {report['pages']} page(s)")
    text = call("read_file", project=pid, path="main.tex")["content"][0]["text"]
    assert "\\begin{document}" in text
    call("edit_file", project=pid, path="main.tex", old_text="\\begin{document}",
         new_text="\\begin{document}\nWritten by an AI client over MCP.")
    report = call("build", project=pid)["structuredContent"]
    assert report["ok"], report
    found = call("search", query="AI client over MCP")["structuredContent"]["results"]
    assert found and found[0]["id"] == f"{pid}::main.tex", found
    ok("read_file, edit_file, build again, search finds the new text")
    image = call("render_page", project=pid, page=1, size=800)["content"][1]
    png = base64.b64decode(image["data"])
    assert image["mimeType"] == "image/png" and png.startswith(b"\x89PNG"), image["mimeType"]
    ok(f"render_page returns a PNG ({len(png) // 1024} KiB)")
    modern = {"jsonrpc": "2.0", "id": 4, "method": "server/discover", "params": {"_meta": {
        "io.modelcontextprotocol/protocolVersion": "2026-07-28"}}}
    status, data, _ = client("POST", "/mcp", modern, origin=False, headers={
        "Authorization": f"Bearer {tokens['access_token']}", "MCP-Protocol-Version": "2026-07-28",
        "Mcp-Method": "server/discover"})
    assert status == 200 and data["result"]["resultType"] == "complete", data
    ok("protocol 2026-07-28: server/discover")
    status, fresh, _ = client("POST", "/oauth/token", origin=False, form={
        "grant_type": "refresh_token", "client_id": reg["client_id"], "refresh_token": tokens["refresh_token"]})
    assert status == 200 and fresh["refresh_token"] != tokens["refresh_token"]
    tokens = fresh
    ok("refresh token rotates")

    # --- account page: the connection, and Disconnect ---------------------------------------------------------------
    page.goto("/#account")
    expect(page.get_by_role("heading", name="AI clients")).to_be_visible()
    expect(page.get_by_text("E2E Client")).to_be_visible()
    axe(page, "account page with a connected AI client")
    page.set_viewport_size({"width": 390, "height": 844})
    axe(page, "account page at phone width")
    page.once("dialog", lambda dialog: dialog.accept())
    page.get_by_role("button", name="Disconnect E2E Client").click()
    expect(page.get_by_text("No AI client is connected.")).to_be_visible()
    assert mcp({"jsonrpc": "2.0", "id": 5, "method": "tools/list"})[0] == 401
    ok("Disconnect on the account page ends the connection")


if __name__ == "__main__":
    sys.exit(main())
