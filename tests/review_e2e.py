"""
Suggest mode and named share links in a real browser (not part of `unittest discover`):

    uv run --no-project --with playwright==1.56.0 python tests/review_e2e.py [--chromium PATH] [--axe PATH_OR_URL]

Starts serve.py --share local on a throwaway document (a temp folder, its own config folder for the link records),
mints two named links in the owner's Share dialog (Alice can edit, Reviewer 2 views) and checks, with the owner and
both guests in separate browser contexts:
  - Alice's typing in suggest mode becomes a suggestion under the name "Alice" (not what her browser says), and
    neither the shared text nor the file on disk changes;
  - Ctrl+Z / Ctrl+Y shorten, withdraw and bring back her own drafts and suggestions, never the shared text;
  - typing with several cursors makes ONE suggestion with several ranges, which the owner accepts as a whole;
  - IME composition (Chrome DevTools Input.imeSetComposition / insertText) becomes a suggestion, and the text being
    composed never reaches the owner or the disk;
  - the owner's edits arriving in Alice's editor go through untouched while she is suggesting;
  - presence shows the link names; revoking Alice's link ends her session at once;
  - axe-core reports no violations (light, dark, phone; Share dialog and Review panel open).
If the CDNs are blocked, run `python3 scripts/vendor_ui.py` first (and delete scripts/serve_ui/vendor/ afterwards).
"""

from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import socket
import subprocess
import sys
import tempfile
import time
import urllib.request
from pathlib import Path

from playwright.sync_api import sync_playwright

ROOT = Path(__file__).resolve().parent.parent
AXE = "https://cdnjs.cloudflare.com/ajax/libs/axe-core/4.10.2/axe.min.js"
LOADED = "() => window.__app && window.__app.active && window.__app.view.state.doc.length > 0"
TEXT = ("\\documentclass{article}\n\\begin{document}\nThe quick brown fox.\nLine two here.\nLine three here.\n"
        "\\end{document}\n")


def free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--chromium", default=None)
    parser.add_argument("--axe", default=AXE)
    args = parser.parse_args()

    tmp = tempfile.TemporaryDirectory()
    src = Path(tmp.name) / "docs"
    doc = f"review-e2e-{os.getpid()}"  # its history and review data go to .latex-history/<doc>, removed at the end
    (src / doc).mkdir(parents=True)
    main_tex = src / doc / "main.tex"
    main_tex.write_text(TEXT, encoding="utf-8")
    port = free_port()
    env = {**os.environ, "XDG_CONFIG_HOME": str(Path(tmp.name) / "config")}
    server = subprocess.Popen([sys.executable, "-u", str(ROOT / "scripts/serve.py"), doc, "--source", str(src),
                               "--no-open", "--port", str(port), "--share", "local"],
                              stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, env=env)
    origin = f"http://localhost:{port}"
    owner_url = None
    for line in server.stdout:
        found = re.search(r"you \(owner\)\s*: (\S+)", line)
        if found:
            owner_url = found.group(1).replace("127.0.0.1", "localhost")
            break
    assert owner_url, "no owner link"
    owner_cookie = f"lp_{port}=" + owner_url.split("token=")[1].split("#")[0]

    def review() -> dict:
        req = urllib.request.Request(f"{origin}/api/review?doc={doc}", headers={"Cookie": owner_cookie})
        return json.load(urllib.request.urlopen(req, timeout=10))

    def wait(cond, what, limit=15.0):
        end = time.monotonic() + limit
        while time.monotonic() < end:
            got = cond()
            if got:
                return got
            time.sleep(0.2)
        raise AssertionError(f"timed out: {what}")

    def axe(page, label):
        if not page.evaluate("() => !!window.axe"):
            page.add_script_tag(url=args.axe) if args.axe.startswith("http") else page.add_script_tag(path=args.axe)
        result = page.evaluate("() => axe.run(document, {resultTypes: ['violations']})")
        bad = [f"{v['id']}: {v['help']} ({v['nodes'][0]['target']})" for v in result["violations"]]
        assert not bad, f"axe ({label}): {bad}"

    try:
        with sync_playwright() as pw:
            browser = pw.chromium.launch(executable_path=args.chromium)
            new_page = lambda: browser.new_context(bypass_csp=True, viewport={"width": 1400, "height": 900}).new_page()  # noqa: E731

            def open_editor(page, url):
                page.goto(url)
                page.wait_for_function(LOADED, timeout=60000)
                page.evaluate("() => window.__app.openFile('main.tex', 0)")
                page.wait_for_function("() => window.__app.active.path === 'main.tex' && window.__app.active.collab")

            owner = new_page()
            owner.on("dialog", lambda d: d.accept())
            open_editor(owner, owner_url)

            # The owner mints two named links in the Share dialog.
            owner.click("#shareBtn")
            urls = {}
            for name, role in (("Alice", "edit"), ("Reviewer 2", "view")):
                owner.fill("#shareLinkName", name)
                owner.select_option("#shareLinkRole", role)
                owner.click("#shareLinkCreate")
                owner.wait_for_function("n => [...document.querySelectorAll('.named-list b')]"
                                        ".some((b) => b.textContent.startsWith(n))", arg=name)
            for row in owner.query_selector_all(".named-list .link-row"):
                urls[row.query_selector("b").text_content().split(" (")[0]] = row.query_selector("input").input_value()
            assert set(urls) == {"Alice", "Reviewer 2"}, urls
            for theme in ("light", "dark"):
                owner.evaluate("t => document.documentElement.dataset.theme = t", theme)
                axe(owner, f"owner, share dialog, {theme}")
            owner.keyboard.press("Escape")

            alice = new_page()
            alice.on("dialog", lambda d: d.accept())
            open_editor(alice, urls["Alice"].replace("127.0.0.1", "localhost"))
            reviewer = new_page()
            open_editor(reviewer, urls["Reviewer 2"].replace("127.0.0.1", "localhost"))
            assert "View only as Reviewer 2" in reviewer.text_content("#roleChip")
            assert "Can edit as Alice" in alice.text_content("#roleChip")
            names = wait(lambda: (lambda t: "Alice" in t and "Reviewer 2" in t and t)(owner.text_content("#usersList")),
                         "presence names")
            assert "unverified" not in names, names
            print("named links: minted in the Share dialog; presence and role chips show the link names")

            text_of = lambda page: page.evaluate("() => window.__app.view.state.doc.toString()")  # noqa: E731

            def put_cursor(page, *positions):
                page.evaluate("""(ps) => { const v = window.__app.view, S = v.state.selection.constructor;
                    v.dispatch({ selection: S.create(ps.map((p) => S.cursor(p))) }); v.focus(); }""", list(positions))

            def draft_text(page):
                return page.evaluate("() => [...document.querySelectorAll('.cm-sug-ins.draft')]"
                                     ".map((e) => e.textContent)")

            alice.click("#suggestBtn")
            # 1. Typing: one suggestion by "Alice"; nothing changes in the shared text or on disk.
            put_cursor(alice, TEXT.index("quick") + 5)
            alice.keyboard.type(" very")
            assert text_of(alice) == TEXT and draft_text(alice) == [" very"]
            alice.keyboard.press("Control+z")  # undo the draft being typed: gone, text untouched
            assert draft_text(alice) == [] and text_of(alice) == TEXT
            alice.keyboard.press("Control+y")  # redo: back
            assert draft_text(alice) == [" very"]
            sugg = wait(lambda: review()["suggestions"], "suggestion saved after the idle pause")
            assert [(s["insert"], s["name"]) for s in sugg] == [(" very", "Alice")], sugg
            assert text_of(owner) == TEXT and main_tex.read_text("utf-8") == TEXT
            alice.keyboard.press("Control+z")  # undo a sent suggestion: withdrawn
            wait(lambda: not review()["suggestions"], "withdrawn by undo")
            alice.keyboard.press("Control+y")  # redo: suggested again
            wait(lambda: [s["insert"] for s in review()["suggestions"]] == [" very"], "re-suggested by redo")
            assert text_of(alice) == TEXT == text_of(owner) == main_tex.read_text("utf-8")
            print("suggest mode: typing, undo and redo of drafts and sent suggestions; shared text untouched")

            # 2. Several cursors, one keystroke each: one suggestion with two ranges.
            put_cursor(alice, TEXT.index("Line two") + 4, TEXT.index("Line three") + 4)
            alice.keyboard.type("X")
            put_cursor(alice, 0)  # moving away sends it
            group = wait(lambda: [s for s in review()["suggestions"] if s.get("more")], "grouped suggestion")[0]
            assert (group["insert"], [m["insert"] for m in group["more"]]) == ("X", ["X"]), group
            assert text_of(alice) == TEXT
            print("multi-cursor: one suggestion with 2 ranges")

            # 3. IME composition: held back while composing, a suggestion afterwards.
            cdp = alice.context.new_cdp_session(alice)
            put_cursor(alice, TEXT.index("fox") + 3)
            cdp.send("Input.imeSetComposition", {"text": "に", "selectionStart": 1, "selectionEnd": 1})
            cdp.send("Input.imeSetComposition", {"text": "にほ", "selectionStart": 2, "selectionEnd": 2})
            time.sleep(1.5)
            assert "に" in text_of(alice), "the browser shows what is being composed"
            assert "に" not in text_of(owner) and "に" not in main_tex.read_text("utf-8"), "composition leaked"
            cdp.send("Input.insertText", {"text": "日本"})
            alice.wait_for_function("() => !window.__app.view.state.doc.toString().includes('日本')")
            put_cursor(alice, 0)
            wait(lambda: any(s["insert"] == "日本" and s["name"] == "Alice" for s in review()["suggestions"]),
                 "IME suggestion")
            assert text_of(alice) == TEXT == text_of(owner), text_of(alice)
            time.sleep(1.5)
            assert main_tex.read_text("utf-8") == TEXT
            print("IME: the composition became one suggestion; co-editors and the disk never saw it")

            # 4. The owner types (not suggesting): it reaches Alice's editor as text, not as her suggestion.
            count = len(review()["suggestions"])
            put_cursor(owner, TEXT.index("Line three"))
            owner.keyboard.type("Owner: ")
            alice.wait_for_function("() => window.__app.view.state.doc.toString().includes('Owner: Line three')")
            time.sleep(2)
            assert len(review()["suggestions"]) == count
            print("remote edits pass through suggest mode (Yjs transaction origin)")

            # 5. The owner accepts the grouped suggestion: both ranges at once.
            owner.evaluate("id => window.__app.REV.focusItem(id)", group["id"])
            owner.click(f"li[data-id='{group['id']}'] button:has-text('Accept')")
            owner.wait_for_function("() => { const t = window.__app.view.state.doc.toString(); "
                                    "return t.includes('LineX two') && t.includes('LineX three'); }")
            wait(lambda: "LineX three" in main_tex.read_text("utf-8"), "accepted group saved")
            for theme in ("light", "dark"):
                owner.evaluate("t => document.documentElement.dataset.theme = t", theme)
                axe(owner, f"owner, review panel, {theme}")
                alice.evaluate("t => document.documentElement.dataset.theme = t", theme)
                alice.evaluate("id => window.__app.REV.focusItem(id)", review()["suggestions"][0]["id"])
                axe(alice, f"Alice, {theme}")
            phone = browser.new_context(bypass_csp=True, viewport={"width": 390, "height": 844}).new_page()
            phone.on("dialog", lambda d: d.accept())
            phone.goto(owner_url)
            phone.wait_for_function("() => window.__app && window.__app.docs && window.__app.cur")
            phone.evaluate("() => document.getElementById('shareBtn').click()")
            phone.wait_for_selector(".named-list")
            axe(phone, "owner, phone, share dialog")
            print("accepted the group as a whole; axe: 0 violations (light, dark, phone)")

            # 6. Revoking Alice ends her session at once; Reviewer 2 stays.
            owner.click("#shareBtn")
            owner.click(".named-list li:has-text('Alice') button:has-text('Revoke')")
            began = time.monotonic()
            alice.wait_for_selector("#revoked", state="visible", timeout=20000)
            print(f"revoked: Alice's tab locked after {time.monotonic() - began:.1f} s")
            cookie = f"lp_{port}=" + urls["Reviewer 2"].split("token=")[1].split("#")[0]
            status = urllib.request.urlopen(urllib.request.Request(f"{origin}/api/health", headers={"Cookie": cookie}),
                                            timeout=10).status
            assert status == 200
            assert reviewer.evaluate("() => document.getElementById('revoked').hidden")
            browser.close()
    finally:
        server.terminate()
        server.wait(30)
        tmp.cleanup()
        shutil.rmtree(ROOT / ".latex-history" / doc, ignore_errors=True)
    print("review e2e ok")
    return 0


if __name__ == "__main__":
    sys.exit(main())
