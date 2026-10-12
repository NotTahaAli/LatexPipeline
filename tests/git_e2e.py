"""
Browser check of the Git features against a real https Git server on loopback (the one tests/test_git_live.py starts:
git http-backend behind a self-signed certificate, checking the Authorization header). Not part of `unittest discover`:

    uv run --no-project --with playwright==1.56.0 python tests/git_e2e.py [--chromium PATH] [--axe PATH_OR_URL]
        [--insecure-no-sandbox] [--headed]

Hosted: an editor saves a Git token on the Account page, connects a new project's Git tab to the repository, pulls
(a differing file is listed, never overwritten), pulls again once they agree, edits in the editor and pushes, sees the
push refused after the host rotated the token, saves the new token and pushes, removes the token and disconnects;
the audit rows are checked in host.db. The gateway runs with host_git.public_address answering 127.0.0.1 for git.test
only and the test certificate in git's config (everything else as in production). Local: serve.py on a clone of the
same server; every button of the Git panel: Refresh, a file's diff and Close, Stage, Unstage, Stage all, Commit,
Push, Pull, a conflicting Pull and Abort merge, switching to a remote branch, New branch, and the history. axe-core
reports no violations on each panel state, light and dark. Needs LaTeX, git, openssl and bubblewrap (hosted part)
unless --insecure-no-sandbox. The editor loads its libraries from CDNs.
"""

from __future__ import annotations

import argparse
import base64
import os
import re
import sqlite3
import subprocess
import sys
import tempfile
import time
import urllib.request
from pathlib import Path

from playwright.sync_api import expect, sync_playwright

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "tests"))
import host_e2e  # noqa: E402
import test_git_live as live  # noqa: E402

AXE = "https://cdnjs.cloudflare.com/ajax/libs/axe-core/4.10.2/axe.min.js"
TOKEN, ROTATED, LOCAL_TOKEN = "ghp_E2eGitTokenOfEd123", "ghp_E2eRotatedToken456", "local-e2e-token-789"
LOADED = host_e2e.LOADED
STUB = """
import sys
sys.path.insert(0, {scripts!r})
import host_git
real_check, real_pairs = host_git.public_address, host_git.config_pairs
host_git.public_address = lambda h, p: "127.0.0.1" if h == {host!r} else real_check(h, p)
host_git.config_pairs = lambda d: real_pairs(d) + [("http.sslCAInfo", {cert!r})]
import host
sys.exit(host.main())
"""
CHECKS: list = []
AXE_SOURCE = {"arg": AXE}


def ok(text: str) -> None:
    CHECKS.append(text)
    print(f"PASS  {text}", flush=True)


def axe(page, label: str) -> None:
    """No axe violations on this page, in the light and the dark theme."""
    source = AXE_SOURCE["arg"]
    for theme in ("light", "dark"):
        page.evaluate("t => document.documentElement.dataset.theme = t", theme)
        if not page.evaluate("() => !!window.axe"):
            page.add_script_tag(**({"url": source} if source.startswith("http") else {"path": source}))
        result = page.evaluate("() => axe.run(document, {resultTypes: ['violations']})")
        bad = [f"{v['id']}: {v['help']} ({len(v['nodes'])}x: {v['nodes'][0]['target']})" for v in result["violations"]]
        assert not bad, f"axe on {label} ({theme}): {bad}"
    page.evaluate("() => { delete document.documentElement.dataset.theme; }")
    ok(f"axe: 0 violations on {label} (light and dark)")


def wait_for(check, what: str, timeout: float = 30) -> None:
    deadline = time.monotonic() + timeout
    while not check():
        assert time.monotonic() < deadline, f"timed out waiting for {what}"
        time.sleep(0.3)


def wait_port(port: int, path: str = "/") -> None:
    deadline = time.monotonic() + 30
    while True:
        try:
            urllib.request.urlopen(f"http://127.0.0.1:{port}{path}", timeout=1)
            return
        except urllib.error.HTTPError:
            return
        except OSError:
            assert time.monotonic() < deadline, "the server did not start"
            time.sleep(0.2)


def dialogs(page, answers: dict) -> None:
    """Accept confirm() and answer prompt() with answers['prompt']."""
    page.on("dialog", lambda d: d.accept(answers.get("prompt", "")) if d.type == "prompt" else d.accept())


def show(bare: Path, path: str, ref: str = "main") -> str:
    return live.run_git(bare, "show", f"{ref}:{path}", check=False).stdout


def type_at_end(page, text: str) -> None:
    page.locator(".cm-content").first.click()
    page.keyboard.press("Control+End")
    page.keyboard.type(text)


# --- hosted ---------------------------------------------------------------------------------------------------------

def hosted(browser, git: live.GitServer, base: Path, insecure: bool) -> None:
    port = host_e2e.free_port()
    origin = f"http://127.0.0.1:{port}"
    data = base / "data"
    env = {**live.local_env(), "LP_ADMIN_EMAIL": "root@example.org", "LP_ADMIN_PASSWORD": host_e2e.PASSWORD,
           "LP_ADMIN_NAME": "Root"}
    subprocess.run([sys.executable, str(ROOT / "scripts/host.py"), "init", "--data", str(data)], env=env, check=True,
                   stdout=subprocess.DEVNULL)
    (data / "config.toml").write_text(f'public_url = "{origin}"\nport = {port}\n')
    stub = STUB.format(scripts=str(ROOT / "scripts"), host=live.HOST, cert=str(git.cert))
    command = [sys.executable, "-c", stub, "serve", "--data", str(data)]
    command += ["--insecure-no-sandbox"] if insecure else []
    server = subprocess.Popen(command, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, env=env)
    try:
        wait_port(port)
        run_hosted(browser, git, base, origin, data)
    finally:
        server.terminate()
        out, _ = server.communicate(timeout=20)
        if os.environ.get("E2E_LOG"):
            print(out)


def run_hosted(browser, git, base, origin, data) -> None:
    actx, admin = host_e2e.context(browser, origin)
    host_e2e.sign_in(admin, "root@example.org")
    admin.goto("/#admin")
    admin.get_by_text("New workspace").click()
    admin.get_by_label("Name").fill("Team")
    admin.get_by_role("button", name="Create workspace").click()
    expect(admin.get_by_role("heading", name="Team")).to_be_visible()
    link = host_e2e.invite(admin, "editor")
    ectx, ed = host_e2e.sign_up_with(browser, origin, link, "Ed Itor", "ed@a.org")
    dialogs(ed, {})

    # Account > Git: the token is saved server-side and never shown again.
    ed.goto("/#account")
    gform = ed.locator("form").filter(has=ed.get_by_label("Access token"))
    gform.get_by_label("User name on the Git host").fill("ed")
    gform.get_by_label("Access token").fill(TOKEN)
    gform.get_by_role("button", name="Save Git token").click()
    expect(ed.get_by_text("Saved. Pull and Push are in each project's Git tab.")).to_be_visible()
    assert TOKEN not in ed.content()
    ed.reload()  # the Remove button comes with the next rendering of the page
    expect(ed.get_by_role("button", name="Remove the stored token")).to_be_visible()
    expect(ed.get_by_label("Access token")).to_have_attribute("placeholder", re.compile("Stored"))
    axe(ed, "account page with a stored Git token")
    ok("an editor saves a Git token on the Account page; the page never shows it again")

    ed.goto("/#home")
    ed.get_by_text("New project").click()
    ed.get_by_label("Project name").fill("Thesis")
    ed.get_by_role("button", name="Create").click()
    ed.wait_for_url(re.compile(r"/p/[0-9a-f]{16}/$"))
    pid = ed.url.rstrip("/").split("/p/")[1]
    ed.wait_for_function(LOADED, timeout=60000)
    source = next(data.glob(f"projects/*/{pid}/Thesis"))

    repo = "team/thesis.git"
    seed = base / "hosted-seed"
    bare = git.repo(repo, seed)
    git.private.add(repo)
    git.users["ed"] = TOKEN
    live.commit(seed, {"main.tex": "\\documentclass{article}\n\\begin{document}\nRemote\n\\end{document}\n",
                       "chapter.tex": "A chapter from the repository.\n"})

    ed.locator("#tab-git").click()
    panel = ed.locator("#panel-git")
    expect(panel.get_by_label("Repository URL (https)")).to_be_visible()
    axe(ed, "hosted Git panel, not connected")
    panel.get_by_label("Repository URL (https)").fill(git.url(repo))
    panel.get_by_role("button", name="Connect").click()
    expect(panel.get_by_text(f"{git.url(repo)} (main)")).to_be_visible()
    expect(panel.get_by_text("Not synced yet.")).to_be_visible()
    axe(ed, "hosted Git panel, connected")
    ok("the Git tab connects the project to the https repository")

    mine = (source / "main.tex").read_text(encoding="utf-8")
    panel.get_by_role("button", name="Pull").click()
    expect(panel.get_by_text("These files changed both here and in the repository")).to_be_visible(timeout=30000)
    expect(panel.locator(".git-result li")).to_have_text(["main.tex"])
    assert (source / "main.tex").read_text(encoding="utf-8") == mine
    assert not (source / "chapter.tex").exists()
    axe(ed, "hosted Git panel listing a differing file")
    ok("the first pull lists the differing main.tex and overwrites nothing")

    live.commit(seed, {"main.tex": mine}, message="agree with the project")
    panel.get_by_role("button", name="Pull").click()
    expect(panel.get_by_text("Pulled the repository's files.")).to_be_visible(timeout=30000)
    assert (source / "chapter.tex").read_text(encoding="utf-8") == "A chapter from the repository.\n"
    expect(panel.get_by_text(re.compile(r"Last sync .*, commit [0-9a-f]{7}"))).to_be_visible()
    ok("once the files agree, Pull brings the repository's files in")

    type_at_end(ed, "% pushed from the browser\n")
    wait_for(lambda: "pushed from the browser" in (source / "main.tex").read_text(encoding="utf-8"), "the save")
    panel.get_by_role("button", name="Push").click()
    expect(panel.get_by_text("Pushed.", exact=True)).to_be_visible(timeout=30000)
    assert "pushed from the browser" in show(bare, "main.tex")
    assert live.run_git(bare, "log", "-1", "--format=%an", "main").stdout.strip() == "Ed Itor"
    ok("an edit in the editor is pushed as the editor's own commit")

    git.users["ed"] = ROTATED  # the Git host rotated the token
    type_at_end(ed, "% after the rotation\n")
    wait_for(lambda: "after the rotation" in (source / "main.tex").read_text(encoding="utf-8"), "the save")
    panel.get_by_role("button", name="Push").click()
    expect(panel.locator(".git-status")).to_contain_text("Could not push", timeout=30000)
    axe(ed, "hosted Git panel showing a refused push")
    ed.goto("/#account")
    gform = ed.locator("form").filter(has=ed.get_by_label("Access token"))
    gform.get_by_label("Access token").fill(ROTATED)
    gform.get_by_role("button", name="Save Git token").click()
    expect(ed.get_by_text("Saved. Pull and Push are in each project's Git tab.")).to_be_visible()
    ed.goto(f"/p/{pid}/")
    ed.wait_for_function(LOADED, timeout=60000)
    ed.locator("#tab-git").click()
    panel.get_by_role("button", name="Push").click()
    # The refused push already made the commit, so this one reports no new changes (and delivers that commit).
    expect(panel.get_by_text(re.compile(r"^Pushed"))).to_be_visible(timeout=30000)
    assert "after the rotation" in show(bare, "main.tex")
    assert git.requests[-1][2] == "Basic " + base64.b64encode(f"ed:{ROTATED}".encode()).decode()
    ok("after the host rotated the token the push is refused; the new token from the Account page pushes")

    ed.goto("/#account")
    ed.get_by_role("button", name="Remove the stored token").click()
    expect(ed.get_by_role("button", name="Remove the stored token")).to_have_count(0)
    assert ed.request.get(origin + "/api/account/git").json() == {"has_key": False, "username": ""}
    ed.goto(f"/p/{pid}/")
    ed.wait_for_function(LOADED, timeout=60000)
    ed.locator("#tab-git").click()
    expect(panel.get_by_text("Add a Git access token on your Account page")).to_be_visible()
    panel.get_by_role("button", name="Disconnect").click()
    expect(panel.get_by_label("Repository URL (https)")).to_be_visible()
    assert (source / "chapter.tex").is_file()  # the files stay
    ok("the token is removed on the Account page and the project disconnected; its files stay")

    with sqlite3.connect(data / "host.db") as db:
        actions = [r[0] for r in db.execute("SELECT action FROM audit WHERE action LIKE 'git%' ORDER BY id")]
    assert actions == ["git_token_set", "git_connected", "git_pull", "git_pull", "git_push", "git_push",
                       "git_token_set", "git_push", "git_token_removed", "git_disconnected"], actions
    ok("every Git step is in the audit log")
    for ctx in (actx, ectx):
        ctx.close()


# --- local ----------------------------------------------------------------------------------------------------------

def local(browser, git: live.GitServer, base: Path) -> None:
    seed = base / "local-seed"
    bare = git.repo("local.git", seed)
    git.users["ada"] = LOCAL_TOKEN
    live.commit(seed, {"main.tex": "\\documentclass{article}\n\\begin{document}\n\\input{notes}\n\\end{document}\n",
                       "notes.tex": "one\ntwo\nthree\n"}, message="first")
    config = base / "gitconfig"
    auth = base64.b64encode(f"ada:{LOCAL_TOKEN}".encode()).decode()
    config.write_text(f"[http]\n\tsslCAInfo = {git.cert}\n\tcurloptResolve = {live.HOST}:{git.port}:127.0.0.1\n"
                      f"\textraHeader = Authorization: Basic {auth}\n"
                      "[user]\n\tname = Ada\n\temail = ada@example.org\n")
    env = live.local_env({"GIT_CONFIG_GLOBAL": str(config), "GIT_CONFIG_NOSYSTEM": "1", "NO_PROXY": "*",
                          "no_proxy": "*"})
    work = base / "work"
    work.mkdir()
    doc = work / "demo"
    subprocess.run(["git", "clone", "-q", git.url("local.git"), str(doc)], check=True, env=env)
    port = host_e2e.free_port()
    server = subprocess.Popen([sys.executable, str(ROOT / "scripts/serve.py"), "demo", "--source", str(work),
                               "--no-open", "--port", str(port)], stdout=subprocess.DEVNULL, stderr=subprocess.STDOUT,
                              env=env)
    try:
        wait_port(port, "/api/health")
        page = browser.new_context(bypass_csp=True, viewport={"width": 1400, "height": 900}).new_page()
        answers: dict = {}
        dialogs(page, answers)
        page.goto(f"http://127.0.0.1:{port}/#demo")
        page.wait_for_function(LOADED, timeout=60000)
        page.locator("#tab-git").click()
        panel = page.locator("#panel-git")
        expect(panel.get_by_text("On main - origin/main")).to_be_visible()
        expect(panel.locator(".git-log li")).to_have_count(1)
        axe(page, "local Git panel, clean")

        (doc / "notes.tex").write_text("one\nTWO\nthree\n", encoding="utf-8")
        (doc / "new.tex").write_text("a new file\n", encoding="utf-8")
        panel.get_by_role("button", name="Refresh").click()
        expect(panel.get_by_text("Changes (2)")).to_be_visible()
        panel.get_by_role("button", name="notes.tex").click()
        expect(panel.locator(".git-diff .dl-add")).to_have_text(["+TWO\n"])
        axe(page, "local Git panel with a diff")
        panel.locator(".git-diff").get_by_role("button", name="Close").click()
        expect(panel.locator(".git-diff")).to_be_hidden()
        row = lambda name: panel.locator("li.git-file").filter(has_text=name)  # noqa: E731
        row("notes.tex").get_by_role("button", name="Stage").click()
        expect(panel.get_by_text("Staged (1)")).to_be_visible()
        row("notes.tex").get_by_role("button", name="Unstage").click()
        expect(panel.get_by_text("Staged (0)")).to_be_visible()
        panel.get_by_role("button", name="Stage all").click()
        expect(panel.get_by_text("Staged (2)")).to_be_visible()
        axe(page, "local Git panel with staged files")
        panel.get_by_label("Commit message").fill("Edit notes in the browser")
        panel.get_by_role("button", name="Commit").click()
        expect(panel.get_by_text("Staged (0)")).to_be_visible()
        expect(panel.get_by_text(re.compile("1 to push"))).to_be_visible()
        expect(panel.locator(".git-log li").first).to_contain_text("Edit notes in the browser")
        ok("Refresh, diff, Close, Stage, Unstage, Stage all and Commit")

        panel.get_by_role("button", name="Push").click()
        expect(page.locator("#toast")).to_contain_text("Pushed.")
        assert show(bare, "notes.tex") == "one\nTWO\nthree\n"
        assert live.run_git(bare, "log", "-1", "--format=%an %s", "main").stdout.strip() == \
            "Ada Edit notes in the browser"
        ok("Push sends the commit over https with the owner's credentials")

        live.commit(seed, {"notes.tex": "one\nTWO\nthree\nfour\n"}, message="four upstream")
        panel.get_by_role("button", name="Pull").click()
        expect(page.locator("#toast")).to_contain_text("Pulled.")
        wait_for(lambda: (doc / "notes.tex").read_text(encoding="utf-8").endswith("four\n"), "the pull")
        ok("Pull fast-forwards to the remote's commit")

        live.commit(seed, {"notes.tex": "one\ntheirs\nthree\nfour\n"}, message="theirs")
        live.run_git(seed, "push", "-q", "origin", "main:refs/heads/remote-only")
        (doc / "notes.tex").write_text("one\nmine\nthree\nfour\n", encoding="utf-8")
        live.run_git(doc, "commit", "-qam", "mine", env={"GIT_CONFIG_GLOBAL": str(config)})
        panel.get_by_role("button", name="Pull").click()
        expect(page.locator("#toast")).to_contain_text("Merge conflicts in 1 file(s)")
        alert = panel.get_by_role("alert")
        expect(alert).to_contain_text("A merge is in progress")
        expect(row("notes.tex").locator(".git-code")).to_have_text("conflict")
        axe(page, "local Git panel during a merge with a conflict")
        alert.get_by_role("button", name="Abort merge").click()
        expect(panel.get_by_role("alert")).to_have_count(0)
        assert (doc / "notes.tex").read_text(encoding="utf-8") == "one\nmine\nthree\nfour\n"
        ok("a conflicting Pull shows the merge and Abort merge undoes it")

        pick = panel.get_by_label("Branch")
        pick.select_option("origin/remote-only")
        expect(panel.get_by_text(re.compile("^On remote-only"))).to_be_visible()
        answers["prompt"] = "draft"
        panel.get_by_role("button", name="New branch").click()
        expect(panel.get_by_text(re.compile("^On draft - no upstream"))).to_be_visible()
        expect(pick).to_have_value("draft")
        panel.get_by_role("button", name="Push").click()
        expect(page.locator("#toast")).to_contain_text("Pushed.")
        assert show(bare, "notes.tex", "draft") == "one\ntheirs\nthree\nfour\n"
        pick.select_option("main")
        expect(panel.get_by_text(re.compile("^On main"))).to_be_visible()
        subjects = panel.locator(".git-log .git-subject").all_inner_texts()
        assert subjects[:3] == ["mine", "four upstream", "Edit notes in the browser"], subjects
        axe(page, "local Git panel after switching branches")
        ok("switch to a remote branch, New branch (pushed with its upstream), switch back; the history lists commits")
    finally:
        server.terminate()
        server.wait(10)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--chromium", default=None)
    parser.add_argument("--axe", default=AXE)
    parser.add_argument("--insecure-no-sandbox", action="store_true")
    parser.add_argument("--headed", action="store_true")
    parser.add_argument("--only", choices=("hosted", "local"))
    args = parser.parse_args()
    AXE_SOURCE["arg"] = args.axe
    host_e2e.axe = axe  # the invite page check there uses this one too
    with tempfile.TemporaryDirectory() as tmp:
        base = Path(tmp).resolve()
        git = live.GitServer(base / "server")
        try:
            with sync_playwright() as pw:
                browser = pw.chromium.launch(executable_path=args.chromium, headless=not args.headed)
                if args.only != "local":
                    hosted(browser, git, base, args.insecure_no_sandbox)
                if args.only != "hosted":
                    local(browser, git, base)
                browser.close()
        finally:
            git.close()
    print(f"\nAll {len(CHECKS)} checks passed.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
