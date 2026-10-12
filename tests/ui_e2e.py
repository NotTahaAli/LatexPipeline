"""
Every UI feature of the local editor (scripts/serve_ui) and of the hosted pages (scripts/host_ui) in a real browser
(not part of `unittest discover`):

    uv run --no-project --with playwright==1.56.0 python tests/ui_e2e.py [--part local|hosted|all] [--chromium PATH]
        [--axe PATH_OR_URL] [--insecure-no-sandbox]

local: serve.py on a throwaway source folder (a document with math, a citation, labels, a TODO, a table, a TikZ
picture, an \\input chapter and a .bib file, plus a second document to link a file from). It prints the inventory
(every command of the palette, every item of the More view (the palette grouped, opened by "..."), side and drawer
tabs, toolbar buttons, dialogs and keyboard shortcuts), runs every palette command and asserts its visible effect,
then the features one by one:
hover cards (math, \\cite, \\ref, suggestion with author, time, replies and accept / reject / withdraw by role),
folding, snippets, figure, table and TikZ dialogs, symbols, TODOs, bookmarks, file graph, zen mode, comments on the
selection and on the PDF (click and drag), the bell (empty and filled) and the @-mention list, version history
(name, zip, compare PDF), review markup PDF, lock and unlock, linked files, crash recovery, AI (an OpenAI-compatible
stub on loopback), grammar (a LanguageTool stub on loopback), Zotero (its fetch stubbed in the browser) and the
reference library, the Share dialog with a named link per role (view, review, edit) in separate browser contexts
(what each role can and cannot do), request access, and live co-editing between two contexts. Every step checks
for console errors and failed requests, and axe-core reports no violations on each panel and dialog it opens.

hosted: host.py (bubblewrap unless --insecure-no-sandbox) and every page of host_ui: sign-in, the two-step code
page, open sign-up, password reset, projects (new project and zip upload dialogs, delete), workspace members (roles,
remove, invites, an access request granted), account (profile, password, two-step, Zotero, Git token, API tokens,
AI clients), every site admin section, the error page; axe in both themes.

Needs LaTeX (latexmk, pdflatex, bibtex, latexdiff, synctex) and Chromium for Playwright. The editor loads its
libraries from CDNs; if they are blocked run `python3 scripts/vendor_ui.py` first.
"""

from __future__ import annotations

import argparse
import io
import json
import os
import re
import shutil
import socket
import subprocess
import sys
import tempfile
import threading
import time
import zipfile
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs

from playwright.sync_api import expect, sync_playwright

ROOT = Path(__file__).resolve().parent.parent
AXE = "https://cdnjs.cloudflare.com/ajax/libs/axe-core/4.10.2/axe.min.js"
LOADED = "() => window.__app && window.__app.active && window.__app.view.state.doc.length > 0"
BUILT = "() => { const d = window.__app.docs[window.__app.cur]; return d && d.status === 'ok' && d.pages > 0; }"
PASSWORD = "password-123"
CHECKS: list[str] = []

MAIN = r"""\documentclass{article}
\usepackage{amsmath}
\usepackage{tikz}
\begin{document}
\section{Introduction}\label{sec:intro}
The quick brown fox jumps over teh lazy dog. See Section~\ref{sec:intro} and \cite{knuth84}.
% TODO: check this sentence
\begin{equation}\label{eq:energy}
E = mc^2
\end{equation}
Inline math $a^2+b^2=c^2$ here.

\begin{tabular}{ll}
alpha & beta \\
gamma & delta \\
\end{tabular}

\begin{tikzpicture}
\draw (0,0) -- (1,1);
\end{tikzpicture}

\input{chapter}
\bibliographystyle{plain}
\bibliography{refs}
\end{document}
"""
CHAPTER = "\\section{Second}\\label{sec:second}\nA second section with some words in it.\n"
BIB = """@book{knuth84,
  author = {Knuth, Donald E.},
  title = {The {\\TeX}book},
  publisher = {Addison-Wesley},
  year = {1984}
}
"""
OTHER = "\\documentclass{article}\n\\begin{document}\nOther document.\n\\end{document}\n"


def remove_outputs(doc: str) -> None:
    """The cache, history and out/ files serve.py made for a document of a --source outside the repository
    (keyed `_x<hash>_2F<name>`, build.cache_dir_for)."""
    for base in (ROOT / ".latex-cache", ROOT / ".latex-history", *(ROOT / ".latex-cache").glob("_[a-wyz]*")):
        for found in base.glob(f"_x*_2F{doc}"):
            shutil.rmtree(found, ignore_errors=True)
    for found in (ROOT / "out").glob(f"{doc}.*"):
        shutil.rmtree(found) if found.is_dir() else found.unlink()


def ok(text: str) -> None:
    CHECKS.append(text)
    print(f"PASS  {text}", flush=True)


def free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def wait(cond, what, limit=20.0):
    end = time.monotonic() + limit
    while time.monotonic() < end:
        got = cond()
        if got:
            return got
        time.sleep(0.2)
    raise AssertionError(f"timed out: {what}")


# ---- stubs on loopback: an OpenAI-compatible chat completion endpoint and a LanguageTool server ----------------

STUB_REPLY = {
    "answer": "Stub answer from the test model.",
    "text": "\\textbf{stub}",
    "edits": [],
    "claims": [{"quote": "The quick brown fox", "reason": "A claim without a source.", "query": "quick brown fox"}],
}


class Stub(BaseHTTPRequestHandler):
    seen: list = []
    bodies: list = []  # chat completion requests, newest last

    def log_message(self, *a):  # quiet
        pass

    def _send(self, code, body, ctype="application/json"):
        data = body if isinstance(body, bytes) else json.dumps(body).encode()
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def do_GET(self):  # noqa: N802
        if self.path.endswith("/v2/languages"):
            self._send(200, [{"name": "English (US)", "code": "en", "longCode": "en-US"}])
        else:
            self._send(404, {"error": "no"})

    def do_POST(self):  # noqa: N802
        raw = self.rfile.read(int(self.headers.get("Content-Length", "0")))
        Stub.seen.append(self.path)
        if self.path.endswith("/v2/check"):
            text = parse_qs(raw.decode())["text"][0] if b"text=" in raw else ""
            at = text.find("teh")
            matches = (
                []
                if at < 0
                else [
                    {
                        "offset": at,
                        "length": 3,
                        "message": "Possible spelling mistake found.",
                        "replacements": [{"value": "the"}],
                        "rule": {"id": "MORFOLOGIK_RULE_EN_US", "category": {"id": "TYPOS"}},
                    }
                ]
            )
            self._send(200, {"matches": matches})
        elif self.path.endswith("/chat/completions"):
            body = json.loads(raw)
            Stub.bodies.append(raw.decode())
            reply = dict(STUB_REPLY)
            if "undefinedcmd" in raw.decode():  # "Explain first error" on the broken chapter: one fix
                reply["edits"] = [{"file": "chapter.tex", "old": "\\undefinedcmd{}", "new": ""}]
            content = json.dumps(reply)
            if body.get("stream"):
                chunks = [
                    {"model": "stub", "choices": [{"delta": {"content": content[i : i + 40]}}]}
                    for i in range(0, len(content), 40)
                ]
                chunks.append(
                    {
                        "model": "stub",
                        "choices": [{"delta": {}, "finish_reason": "stop"}],
                        "usage": {"prompt_tokens": 10, "completion_tokens": 20},
                    }
                )
                data = "".join(f"data: {json.dumps(c)}\n\n" for c in chunks) + "data: [DONE]\n\n"
                self._send(200, data.encode(), "text/event-stream")
            else:
                self._send(
                    200,
                    {
                        "model": "stub",
                        "choices": [{"message": {"content": content}, "finish_reason": "stop"}],
                        "usage": {"prompt_tokens": 10, "completion_tokens": 20},
                    },
                )
        else:
            self._send(404, {"error": "no"})


def start_stub() -> int:
    server = ThreadingHTTPServer(("127.0.0.1", 0), Stub)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    return server.server_address[1]


# ---- browser helpers -----------------------------------------------------------------------------------------


class Watch:
    """Console errors, uncaught exceptions and failed requests of one page; check() asserts there were none."""

    def __init__(self, page, name):
        self.page, self.name, self.bad = page, name, []
        # The live preview answers 409 for a file that is not a chapter (it then shows "off"): its protocol.
        # A quiet forward search (the PDF following the cursor) answers 400 for a file the PDF does not show.
        self.allow = [r"HTTP 409 POST .*/api/preview\?", r"HTTP 400 GET .*/forward\?.*quiet=1"]
        # "Failed to load resource" repeats an HTTP error the response handler below records with its URL.
        page.on(
            "console",
            lambda m: (
                m.type == "error"
                and not m.text.startswith("Failed to load resource")
                and self.bad.append(f"console: {m.text}")
            ),
        )
        page.on("pageerror", lambda e: self.bad.append(f"pageerror: {e}"))
        page.on("requestfailed", lambda r: self._failed(r))
        page.on(
            "response", lambda r: r.status >= 400 and self.bad.append(f"HTTP {r.status} {r.request.method} {r.url}")
        )

    def _failed(self, request):
        err = request.failure or ""
        # A long-poll cut by navigation, a superseded PDF load, a download handed to the browser, or the AI answer
        # stream the editor stops reading after its "done" line, or a live preview a newer keystroke superseded, is
        # not a failure.
        if "ERR_ABORTED" in err and re.search(
            r"/api/(events|poll|ws|preview)|/ui/|/pdf/|\.pdf|zip|/api/ai\?.*stream=1", request.url
        ):
            return
        self.bad.append(f"request failed: {request.method} {request.url} {err}")

    def check(self, step, allow=()):
        allow = list(allow) + self.allow
        bad = [b for b in self.bad if not any(re.search(a, b) for a in allow)]
        self.bad.clear()
        assert not bad, f"{self.name}, {step}: {bad}"


def axe(page, label, axe_src, themes=("light", "dark")):
    for theme in themes:
        page.evaluate("t => document.documentElement.dataset.theme = t", theme)
        if not page.evaluate("() => !!window.axe"):
            page.add_script_tag(url=axe_src) if axe_src.startswith("http") else page.add_script_tag(path=axe_src)
        result = page.evaluate("() => axe.run(document, {resultTypes: ['violations']})")
        bad = [
            f"{v['id']}: {v['help']} ({len(v['nodes'])}x: {v['nodes'][0]['target']}; "
            f"{(v['nodes'][0]['any'] or v['nodes'][0]['all'] or [{}])[0].get('message', '')})"
            for v in result["violations"]
        ]
        assert not bad, f"axe on {label} ({theme}): {bad}"
    page.evaluate("() => delete document.documentElement.dataset.theme")


def open_editor(page, url, path="main.tex"):
    page.goto(url)
    page.wait_for_function(LOADED, timeout=60000)
    page.evaluate("p => window.__app.openFile(p, 0)", path)
    page.wait_for_function("p => window.__app.active.path === p", arg=path)


def text_of(page) -> str:
    return page.evaluate("() => window.__app.view.state.doc.toString()")


def put_cursor(page, pos, to=None):
    page.evaluate(
        """([a, b]) => { const v = window.__app.view;
        v.dispatch({ selection: { anchor: a, head: b == null ? a : b }, scrollIntoView: true }); v.focus(); }""",
        [pos, to],
    )


def hover_at(page, pos):
    """Move the mouse onto the character at pos (CodeMirror hover tooltips)."""
    page.mouse.move(0, 0)
    box = page.evaluate(
        """p => { const v = window.__app.view; v.dispatch({ effects: [], scrollIntoView: true });
        const c = v.coordsAtPos(p); return c && { x: (c.left + c.right) / 2 + 2, y: (c.top + c.bottom) / 2 }; }""",
        pos,
    )
    assert box, f"no coordinates for {pos}"
    page.mouse.move(box["x"], box["y"])
    page.mouse.move(box["x"] + 1, box["y"])


def reset(page):
    """Back to a known state: no popup tab, dialog, menu, tooltip, side panel or drawer."""
    for other in page.context.pages:
        if other is not page:
            other.close()  # an open popup (texdoc, PDF tab) makes set_input_files on this page hang
    page.evaluate("""() => { for (const d of document.querySelectorAll('dialog[open]')) d.close();
        document.activeElement?.blur?.(); }""")
    page.mouse.move(0, 0)
    for _ in range(3):
        page.keyboard.press("Escape")


def palette(page, title):
    """Run a command through the command palette (Ctrl+K), by its exact title."""
    page.keyboard.press("Control+k")
    page.wait_for_selector("#palette[open]")
    page.fill("#paletteInput", title)
    item = page.locator("#paletteList li").filter(has_text=title).first
    expect(item).to_be_visible()
    item.click()
    if title != "Command palette":  # that one opens it again at once
        page.wait_for_function("() => !document.getElementById('palette').open")


def more_view(page):
    """The palette as "..." opens it: [[group, [titles]]], disabled rows marked."""
    page.click("#moreBtn")
    page.wait_for_selector("#palette[open]")
    groups = page.evaluate(
        "() => { const out = []; for (const li of document.querySelectorAll('#paletteList li')) {"
        " if (li.classList.contains('pal-group')) out.push([li.textContent, []]);"
        " else out.at(-1)[1].push(li.firstChild.textContent"
        " + (li.getAttribute('aria-disabled') ? ' (disabled)' : '')); }"
        " return out; }"
    )
    page.keyboard.press("Escape")
    return groups


def inventory(page, who):
    """Everything this role can reach: palette commands, More menu, tabs, toolbar buttons, dialogs, shortcuts."""
    page.keyboard.press("Control+k")
    page.wait_for_selector("#palette[open]")
    page.fill("#paletteInput", "")
    commands = page.evaluate(
        "() => [...document.querySelectorAll('#paletteList li[role=option]')].map((li) => "
        "[li.id.slice(4), li.firstChild.textContent, li.querySelector('.keys')?.textContent || ''])"
    )
    untold = page.evaluate(
        "() => [...document.querySelectorAll('#paletteList li[role=option]')]"
        ".filter((li) => !li.title).map((li) => li.id)"
    )
    assert not untold, f"palette rows without a tooltip (add them to serve_ui/cmdhelp.js): {untold}"
    page.keyboard.press("Escape")
    menu = more_view(page)
    tabs = page.evaluate(
        "() => [...document.querySelectorAll('#side [role=tab], #drawer [role=tab]')]"
        ".filter((t) => !t.hidden).map((t) => t.id + ': ' + t.textContent.trim())"
    )
    buttons = page.evaluate("""() => [...document.querySelectorAll(
        'header button, #editorPane button, #pdfPane button, #panel-files .tree-tools button')]
        .filter((b) => b.offsetParent && b.id !== '').map((b) =>
        b.id + ': ' + (b.getAttribute('aria-label') || b.title || b.textContent.trim()).slice(0, 60)
        + (b.disabled ? ' (disabled)' : ''))""")
    dialogs = page.evaluate("() => [...document.querySelectorAll('dialog[id]')].map((d) => d.id)")
    keys = page.evaluate(
        "() => [...document.querySelectorAll('#cheatList dt')].map((dt) => "
        "dt.textContent + ': ' + dt.nextElementSibling.textContent)"
    )
    print(f"\n==== inventory: {who} ====")
    print(f"palette commands ({len(commands)}):")
    for cid, title, k in commands:
        print(f"  {cid:14} {title}{'  [' + k + ']' if k else ''}")
    print("More (\"...\"):")
    for label, items in menu:
        print(f"  {label}: {' | '.join(items)}")
    print("tabs:", ", ".join(tabs))
    print("toolbar buttons:")
    for b in buttons:
        print("  " + b)
    print("dialogs:", ", ".join(dialogs))
    print("keyboard shortcuts:")
    for k in keys:
        print("  " + k)
    print()
    ids = [c[0] for c in commands]
    assert len(ids) == len(set(ids)), f"duplicate command ids: {sorted(i for i in ids if ids.count(i) > 1)}"
    return {c[0]: (c[1], c[2]) for c in commands}


def local(args, browser) -> None:
    tmp = tempfile.TemporaryDirectory()
    base = Path(tmp.name)
    src, config = base / "docs", base / "config"
    doc, other = f"ui-e2e-{os.getpid()}", f"ui-e2e-other-{os.getpid()}"
    (src / doc).mkdir(parents=True)
    (src / other).mkdir(parents=True)
    (src / doc / "main.tex").write_text(MAIN, encoding="utf-8")
    (src / doc / "chapter.tex").write_text(CHAPTER, encoding="utf-8")
    (src / doc / "refs.bib").write_text(BIB, encoding="utf-8")
    (src / doc / "scratch.tex").write_text(SCRATCH, encoding="utf-8")  # not \input: the commands edit it
    (src / doc / "notes.md").write_text("# Notes\n\nSome **bold** text.\n", encoding="utf-8")
    (src / other / "main.tex").write_text(OTHER, encoding="utf-8")
    (src / other / "shared.bib").write_text(BIB.replace("knuth84", "shared01"), encoding="utf-8")
    stub = start_stub()
    (config / "latex-pipeline").mkdir(parents=True)
    (config / "latex-pipeline" / "ai.json").write_text(
        json.dumps(
            {
                "enabled": True,
                "provider": "openai",
                "base_url": f"http://127.0.0.1:{stub}/v1",
                "openai_model": "stub-model",
            }
        )
    )
    port = free_port()
    env = {**os.environ, "XDG_CONFIG_HOME": str(config)}
    env.pop("LANGUAGETOOL_URL", None)
    server = subprocess.Popen(
        [sys.executable, "-u", str(ROOT / "scripts/serve.py"), "--source", str(src), "--no-open", "--port", str(port)],
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        env=env,
    )
    origin = f"http://localhost:{port}"
    log: list[str] = []
    threading.Thread(target=lambda: [log.append(line) for line in server.stdout], daemon=True).start()
    wait(lambda: any("Serving" in line for line in log), "serve.py starts", 30)
    try:
        run_local(args, browser, origin, src, doc, other, stub, port)
    except BaseException:
        print("".join(log[-60:]))
        raise
    finally:
        server.terminate()
        server.wait(30)
        tmp.cleanup()
        for d in (doc, other):
            remove_outputs(d)


def new_page(browser, name, **kw):
    ctx = browser.new_context(
        bypass_csp=True, viewport=kw.pop("viewport", {"width": 1400, "height": 900}), accept_downloads=True, **kw
    )  # bypass_csp: only so axe can be injected
    page = ctx.new_page()
    w = Watch(page, name)
    w.answers = []  # replies to window.prompt, in order; confirm and alert are accepted
    w.dialogs = []
    # Every toast, for messages about a failed step (a toast hides itself after 5 s).
    page.add_init_script("""addEventListener('DOMContentLoaded', () => { window.__toasts = [];
        new MutationObserver(() => { const t = document.getElementById('toast');
                if (t && t.textContent) __toasts.push(t.textContent); })
            .observe(document.getElementById('toast') || document.body, { childList: true, subtree: true });
        // and every live-region announcement: a later one (a build ending) replaces the text being waited for
        window.__lives = []; const live = document.getElementById('live');
        if (live) new MutationObserver(() => { if (live.textContent) __lives.push(live.textContent); })
            .observe(live, { childList: true, characterData: true, subtree: true }); })""")

    def answer(d):
        w.dialogs.append(f"{d.type}: {d.message[:80]}")
        d.accept(w.answers.pop(0)) if d.type == "prompt" and w.answers else d.accept()

    page.on("dialog", answer)
    return page, w


def run_local(args, browser, origin, src, doc, other, stub, port):
    owner, w = new_page(browser, "owner")
    ctx = {
        "doc": doc,
        "answers": w.answers,
        "dialogs": w.dialogs,
        "popups": [],
        "popup_urls": [],
        "downloads": [],
        "allow": {},
        "other": other,
        "disk": lambda path: (src / doc / path).read_text(encoding="utf-8"),
    }
    owner.context.grant_permissions(["notifications"], origin=origin)
    owner.context.route(
        "https://texdoc.org/**", lambda r: r.fulfill(status=200, body="texdoc stub", content_type="text/plain")
    )
    owner.context.on("page", lambda p: ctx["popups"].append(p))
    owner.on("download", lambda d: ctx["downloads"].append(d.suggested_filename))
    # A popup's own requests: the PDF tab may turn into a download in headless Chromium, leaving its URL blank.
    owner.context.on("request", lambda r: r.is_navigation_request() and ctx["popup_urls"].append(r.url))

    def built_after(t):
        d = owner.evaluate("() => window.__app.docs[window.__app.cur]")
        return d["status"] == "ok" and d.get("finished", 0) > t

    ctx["built_after"] = built_after

    def ensure(rel):
        """A file (or folder/) the next step needs, made on disk if an earlier step did not leave it."""
        target = src / doc / rel
        if rel.endswith("/"):
            target.mkdir(exist_ok=True)
        elif not target.exists():
            target.write_text("% made by the test\n", encoding="utf-8")
        wait(
            lambda: owner.evaluate(
                "async (p) => { const r = await (await fetch('api/files?doc=' + window.__app.cur)).json(); "
                "return r.files.some((f) => f.path === p) || (r.dirs || []).includes(p); }",
                rel.rstrip("/"),
            ),
            f"{rel} listed",
        )

    ctx["ensure"] = ensure
    # A file change starts a build a moment later (the watcher), and TeX removes the .synctex.gz while it runs.
    ctx["idle"] = lambda: (
        owner.wait_for_timeout(2000)
        or owner.wait_for_function(
            "() => { const d = window.__app.docs[window.__app.cur]; return d.status !== 'building'; }", timeout=120000
        )
    )
    # The owner's browser settings: grammar from the LanguageTool stub; no tips.
    owner.add_init_script(f"""(() => {{ try {{ const s = JSON.parse(localStorage.getItem('lp.settings') || 'null');
        if (!s) {{ localStorage.setItem('lp.settings', JSON.stringify({{ grammar: 'local',
            grammarUrl: 'http://127.0.0.1:{stub}' }})); localStorage.setItem('lp.tips', 'done'); }} }}
        catch (e) {{}} }})()""")
    open_editor(owner, f"{origin}/#{doc}")
    owner.wait_for_function(BUILT, timeout=120000)
    w.check("load and first build")
    ok("owner opens the document; it builds")
    cmds = inventory(owner, "owner (not sharing)")
    assert len(cmds) > 80, len(cmds)
    only = os.environ.get("UI_E2E_ONLY")
    ids = only.split(",") if only else None
    done = sweep(owner, w, cmds, ctx, "palette", ids)
    ok(f"command palette: {len(done)} commands, each with its visible effect, no console error or failed request")
    done = sweep(owner, w, cmds, ctx, "keys", ids)
    ok(f"keyboard shortcuts: {len(done)} ({', '.join(done)}), each with the same effect")
    grouped = [items for label, items in more_view(owner) if label != "Other"]
    in_menu = {t for items in grouped for t in items if not t.endswith("(disabled)")}
    done = sweep(owner, w, {k: v for k, v in cmds.items() if v[0] in in_menu}, ctx, "menu", ids)
    ok(f"More (\"...\"): {len(done)} grouped items, each with the same effect")
    ctx.update(axe=args.axe, browser=browser, origin=origin, src=src, stub=stub, config=src.parent / "config")
    wanted = os.environ.get("UI_E2E_FEATURES")
    for name, feature in FEATURES:
        if wanted and name not in wanted.split(","):
            continue
        if os.environ.get("UI_E2E_VERBOSE"):
            print(f"  feature: {name}", flush=True)
        reset(owner)
        feature(owner, w, ctx)
        w.check(name)


SCRATCH = r"""\section{Scratch}\label{s-scratch}
See Section~\ref{sec:intro}. The quick brown fox jumps over the lazy dog.
\begin{equation}\label{s-eq}
f(x) = (a + b)
\end{equation}
\begin{tabular}{ll}
alpha & beta \\
g & delta \\
\end{tabular}
\begin{tikzpicture}
\draw (0,0) -- (1,1);
\end{tikzpicture}
Some **bold** words.

This paragraph is one long line that the rewrap command should break into lines shorter than the wrap column.
"""

SIDE = (
    "t => !document.getElementById('side').hidden && "
    "document.getElementById('tab-' + t).getAttribute('aria-selected') === 'true'"
)
DRAWER = (
    "t => !document.getElementById('drawer').hidden && "
    "document.getElementById('dtab-' + t).getAttribute('aria-selected') === 'true'"
)
TOAST = "re => { const t = document.getElementById('toast'); return !t.hidden && new RegExp(re).test(t.textContent); }"
AI_DONE = (
    "() => { const li = document.querySelector('#panel-ai .ai-log li'); return li && "
    "/Stub answer/.test(li.textContent) && /Replace selection/.test(li.textContent); }",
    None,
)
SETTING = "([k, v]) => window.__app.settings[k] === v"


def find(page, needle, offset=0, path=None):
    if path:
        open_file(page, path)
    at = text_of(page).index(needle)
    return at + offset


def open_file(page, path):
    if page.evaluate("() => window.__app.active?.path") != path:
        page.evaluate("p => window.__app.openFile(p, 0)", path)
        # Wait for its co-editing room too: text set before the room is ready would be replaced by the room's.
        page.wait_for_function(
            "p => { const a = window.__app.active; return a?.path === p && "
            "window.__app.view.state.doc.length > 0 && (!a.collab || a.collab.ready); }",
            arg=path,
        )


def set_text(page, text):
    page.evaluate(
        """t => { const v = window.__app.view;
        if (v.state.doc.toString() !== t) v.dispatch({ changes: { from: 0, to: v.state.doc.length, insert: t } }); }""",
        text,
    )


def select_tree(page, path):
    page.evaluate(
        "() => { if (document.getElementById('side').hidden || "
        "document.getElementById('tab-files').getAttribute('aria-selected') !== 'true') "
        "document.getElementById('sideBtn').click(); }"
    )
    page.click("#tab-files")
    row = f"#tree .row[data-path='{path}']"
    page.click(row)
    if "." in path.rsplit("/", 1)[-1]:
        # opening the file focuses the editor once it loads, where F2 is "Rename label": keys go to the row
        page.wait_for_function("p => window.__app.active?.path === p", arg=path)
        page.wait_for_timeout(100)
    page.focus(row)


def command_specs(page, ctx):
    """id -> (prepare, check, cleanup). check is a JS predicate with its argument, or a Python callable; every
    command of the owner's palette is here (missing ones fail the sweep)."""
    scratch = lambda: (open_file(page, "scratch.tex"), set_text(page, SCRATCH))  # noqa: E731

    def at(needle, off=0, to=None):
        scratch()
        put_cursor(page, find(page, needle, off), None if to is None else find(page, needle, to))

    main_at = lambda needle, off=0: (open_file(page, "main.tex"), put_cursor(page, find(page, needle, off)))  # noqa: E731
    has = lambda t: lambda: t in text_of(page)  # noqa: E731
    setting = lambda k: lambda: page.evaluate("k => window.__app.settings[k]", k)  # noqa: E731

    def flip(k):
        before = setting(k)()
        return lambda: setting(k)() != before

    js = lambda expr, arg=None: (expr, arg)  # noqa: E731
    toast = lambda pattern: js(TOAST, pattern)  # noqa: E731
    side = lambda tab: js(SIDE, tab)  # noqa: E731
    drawer = lambda tab: js(DRAWER, tab)  # noqa: E731
    dlg = lambda sel: js(f"() => !!document.querySelector({sel!r})")  # noqa: E731
    popup = lambda part: lambda: any(part in u for u in ctx["popup_urls"])  # noqa: E731
    close_dialogs = lambda: page.evaluate("() => document.querySelectorAll('dialog[open]').forEach((d) => d.close())")  # noqa: E731
    specs = {
        "save": (
            lambda: (at("lazy dog", 8), page.keyboard.type(" Saved.")),
            lambda: "Saved." in ctx["disk"]("scratch.tex"),
            None,
        ),
        "palette": (None, dlg("#palette[open]"), close_dialogs),
        "files": (None, side("files"), None),
        "find": (None, dlg(".cm-panel.cm-search"), lambda: page.keyboard.press("Escape")),
        "search": (None, side("search"), None),
        "outline": (None, side("outline"), None),
        "references": (None, side("refs"), None),
        "symbols": (None, side("symbols"), None),
        "assistant": (None, side("ai"), None),
        "ai-explain": (None, toast("no error with a file and line"), None),
        "ai-rewrite": (
            lambda: at("quick brown fox", 0, 15),
            AI_DONE,
            lambda: (
                page.click("#panel-ai .ai-log li:first-child button:has-text('Replace selection')"),
                wait(has("\\textbf{stub}"), "AI reply applied"),
            ),
        ),
        "ai-shorten": (lambda: at("quick brown fox", 0, 15), AI_DONE, None),
        "ai-grammar": (lambda: at("quick brown fox", 0, 15), AI_DONE, None),
        "ai-translate": (lambda: at("quick brown fox", 0, 15), AI_DONE, None),
        "ai-write": (None, js("() => document.activeElement?.matches('#panel-ai textarea')"), None),
        "tableedit": (lambda: at("alpha"), dlg("dialog.tbl-dlg[open]"), close_dialogs),
        "tablealign": (lambda: at("alpha"), has("g     & delta"), None),
        "tikzedit": (lambda: at("\\draw"), dlg("dialog.tz-dlg[open] svg"), close_dialogs),
        "md2latex": (lambda: at("**bold**", 0, 8), has("\\textbf{bold}"), None),
        "mdpreview": (
            lambda: (open_file(page, "notes.md"), page.wait_for_selector(".md-preview:not([hidden]) strong")),
            dlg(".md-preview[hidden]"),
            "again",
        ),  # on by default for .md files: the command turns it off
        "problems": (None, js("() => !document.getElementById('drawer').hidden"), None),  # toggles, on its last tab
        "log": (
            None,
            js("() => /Latexmk|latexmk|SUCCESS/.test(document.getElementById('dpanel-log').textContent)"),
            None,
        ),
        "notify": (None, toast("notification"), None),
        "warnings": (None, drawer("warnings"), None),
        "lint": (None, drawer("lint"), None),
        "grammar": (lambda: scratch(), drawer("grammar"), None),
        "comment": (
            lambda: at("quick brown", 0, 11),
            dlg(".rv-new textarea, .cm-rv-new textarea, #dpanel-review textarea"),
            None,
        ),
        "suggest": (
            None,
            js("() => document.getElementById('suggestBtn').getAttribute('aria-checked') === 'true'"),
            lambda: page.click("#suggestBtn"),
        ),
        "review": (None, drawer("review"), None),
        "history": (None, drawer("history"), None),
        "definition": (lambda: at("sec:intro", 2), js("() => window.__app.active.path === 'main.tex'"), None),
        "findrefs": (lambda: main_at("sec:intro", 2), dlg("#navDlg[open] .nav-hit"), close_dialogs),
        "renamelabel": (
            lambda: (at("s-eq", 1), ctx["answers"].append("s-eq2")),
            lambda: "s-eq2" in ctx["disk"]("scratch.tex"),
            None,
        ),
        "pkgdoc": (lambda: main_at("amsmath", 2), popup("texdoc.org/pkg/amsmath"), None),
        "bookmark": (
            lambda: at("Some **bold**"),
            dlg(".cm-navMarks .cm-gutterElement:not([style*=visibility]) .cm-navMark"),
            None,
        ),
        "bmnext": (
            lambda: put_cursor(page, 0),
            lambda: page.evaluate("() => window.__app.view.state.selection.main.head") > 0,
            None,
        ),
        "bmprev": (
            lambda: put_cursor(page, find(page, "This paragraph")),
            lambda: page.evaluate("() => window.__app.view.state.selection.main.head") < find(page, "This paragraph"),
            None,
        ),
        "bookmarks": (None, drawer("bookmarks"), None),  # its rows: the bookmark section below
        "todos": (
            None,
            js("() => /check this sentence/.test(document.getElementById('dpanel-todos').textContent)"),
            None,
        ),
        "graph": (None, dlg("#navDlg[open] .nav-graph svg"), close_dialogs),
        "typeset": (lambda: at("f(x) = (a + b)", 0, 14), dlg("#navDlg[open] .nav-typeset canvas"), close_dialogs),
        "present": (None, dlg(".nav-present canvas"), lambda: page.keyboard.press("Escape")),
        "pdftab": (None, popup("/pdf/"), None),
        "pdfdownload": (
            None, lambda: any(n.endswith(".pdf") for n in ctx["downloads"]), lambda: ctx["downloads"].clear()
        ),
        "srczip": (
            None, lambda: f"{ctx['doc'].split('/')[-1]}.zip" in ctx["downloads"], lambda: ctx["downloads"].clear()
        ),
        "numbers": (None, flip("labelNumbers"), "again"),
        "mathprev": (None, flip("mathPreview"), "again"),
        "follow": (None, flip("follow"), "again"),
        "reviewpdf": (None, toast("suggestion\\(s\\) and .* comment\\(s\\) marked"), None),
        "sugcard": (lambda: at("quick"), lambda: True, None),  # no suggestion here: nothing opens (the card: below)
        "pdfcomment": (None, dlg("#viewer.pick"), lambda: page.keyboard.press("Escape")),
        "notifications": (None, dlg("#bellList:not([hidden])"), None),
        "lock": (None, dlg("#lockChip:not([hidden])"), "again"),
        "copy": (
            lambda: select_tree(page, "chapter.tex"),
            dlg("#fsDlg[open]"),
            lambda: (page.click("#fsOk"), page.wait_for_selector("#tree .row[data-path='chapter-copy.tex']")),
        ),
        "linkfile": (
            lambda: select_tree(page, "main.tex"),
            dlg("dialog[open] #lnkSrc"),
            lambda: (  # "Save as" fills itself from the source
                page.fill("#lnkSrc", f"{ctx['other']}:shared.bib"),
                page.press("#lnkSrc", "Tab"),
                expect(page.locator("#lnkTo")).to_have_value("shared.bib"),
                page.click("dialog[open] button[type=submit]"),
                dialog_done(page),
                page.wait_for_selector("#tree .row[data-path='shared.bib'] .tag.linked"),
            ),
        ),
        "refreshlinked": (
            lambda: select_tree(page, "shared.bib"),
            js("() => (window.__lives || []).some((m) => /refreshed/.test(m))"),
            None,
        ),
        "unlink": (
            lambda: select_tree(page, "shared.bib"),
            js(
                "() => document.querySelector(\"#tree .row[data-path='shared.bib']\") && "
                "!document.querySelector(\"#tree .row[data-path='shared.bib'] .tag.linked\")"
            ),
            None,
        ),
        "visual": (
            lambda: scratch(),
            js("() => document.getElementById('visualBtn').getAttribute('aria-checked') === 'true'"),
            "again",
        ),
        "prose": (lambda: at("quick"), dlg("#prose:not([hidden])"), "again"),
        "newfile": (
            lambda: select_tree(page, "main.tex"),
            dlg("#fsDlg[open]"),
            lambda: (
                page.fill("#fsDlg input", "made.tex"),
                page.click("#fsOk"),
                page.wait_for_selector("#tree .row[data-path='made.tex']"),
            ),
        ),
        "newdir": (
            lambda: select_tree(page, "main.tex"),
            dlg("#fsDlg[open]"),
            lambda: (
                page.fill("#fsDlg input", "madedir"),
                page.click("#fsOk"),
                page.wait_for_selector("#tree .row[data-path='madedir']"),
            ),
        ),
        "rename": (
            lambda: (ctx["ensure"]("made.tex"), select_tree(page, "made.tex")),
            dlg("#fsDlg[open]"),
            lambda: (
                page.fill("#fsDlg input", "renamed.tex"),
                page.click("#fsOk"),
                page.wait_for_selector("#tree .row[data-path='renamed.tex']"),
            ),
        ),
        "delete": (
            lambda: (ctx["ensure"]("renamed.tex"), ctx["ensure"]("madedir/"), select_tree(page, "renamed.tex")),
            dlg("#fsDlg[open]"),
            lambda: (
                page.click("#fsOk"),
                page.wait_for_selector("#tree .row[data-path='renamed.tex']", state="detached"),
                page.click("#tree .row[data-path='madedir']"),
                page.click("#fDelete"),
                page.click("#fsOk"),
                page.wait_for_selector("#tree .row[data-path='madedir']", state="detached"),
            ),
        ),
        "focus": (lambda: open_file(page, "chapter.tex"), dlg("#focusNote:not([hidden])"), None),
        "unfocus": (None, dlg("#focusNote[hidden]"), None),
        "rebuild": (None, lambda: ctx["built_after"](time.time() - 1), None),
        "format": (lambda: scratch(), toast("format|Formatted|tex-fmt"), None),
        "tidybib": (lambda: open_file(page, "refs.bib"), toast("[Tt]id"), None),
        "docx": (None, toast("DOCX ready"), None),
        "arxiv": (None, toast("arXiv archive downloading"), None),
        "html": (None, toast("HTML"), None),
        "closetab": (lambda: open_file(page, "notes.md"), js("() => !window.__app.tabs.has('notes.md')"), None),
        "cursor": (lambda: (ctx["idle"](), main_at("quick brown")), dlg("#pages .hit"), None),
        "zin": (
            lambda: ctx.update(zoom=page.evaluate("() => window.__app.pdfView.zoom")),
            lambda: page.evaluate("() => window.__app.pdfView.zoom") > ctx["zoom"],
            None,
        ),
        "zout": (
            lambda: ctx.update(zoom=page.evaluate("() => window.__app.pdfView.zoom")),
            lambda: page.evaluate("() => window.__app.pdfView.zoom") < ctx["zoom"],
            None,
        ),
        "fit": (None, lambda: True, None),
        "pdfdark": (
            None,
            js("() => document.getElementById('pdfDark').getAttribute('aria-pressed') === 'true'"),
            "again",
        ),
        "fold": (lambda: at("\\begin{equation}", 3), dlg(".cm-foldPlaceholder"), None),
        "unfold": (
            lambda: at("\\begin{equation}", 3),
            js("() => !document.querySelector('.cm-foldPlaceholder')"),
            None,
        ),
        "foldall": (lambda: scratch(), dlg(".cm-foldPlaceholder"), None),
        "unfoldall": (None, js("() => !document.querySelector('.cm-foldPlaceholder')"), None),
        "check": (lambda: scratch(), dlg(".cm-panel-lint"), lambda: page.keyboard.press("Escape")),
        "checktoggle": (None, flip("check"), "again"),
        "share": (None, dlg("#share[open] #shareStart"), close_dialogs),
        "settings": (None, dlg("#settings[open]"), close_dialogs),
        "cheat": (None, dlg("#cheat[open]"), close_dialogs),
        "tips": (None, dlg("#tips:not([hidden])"), lambda: page.click("#tips button:has-text('Got it')")),
        "theme": (None, flip("theme"), "thrice"),
        "keys-vim": (
            lambda: scratch(),
            dlg(".cm-vimMode, .cm-fat-cursor"),
            lambda: palette_by_id(page, "keys-default"),
        ),
        "keys-emacs": (lambda: scratch(), dlg(".cm-emacsMode"), lambda: palette_by_id(page, "keys-default")),
        "keys-default": (
            lambda: scratch(),
            js(
                "() => window.__app.settings.keys === 'default' && "
                "!document.querySelector('.cm-emacsMode, .cm-vimMode')"
            ),
            None,
        ),
        "bold": (lambda: at("brown", 0, 5), has("\\textbf{brown}"), None),
        "emph": (lambda: at("brown", 0, 5), has("\\emph{brown}"), None),
        "wrapenv": (lambda: (at("lazy dog", 0, 8), ctx["answers"].append("center")), has("\\begin{center}"), None),
        "wrapcmd": (
            lambda: (at("lazy dog", 0, 8), ctx["answers"].append("underline")),
            has("\\underline{lazy dog}"),
            None,
        ),
        "envrename": (lambda: (at("f(x)"), ctx["answers"].append("align")), has("\\begin{align}"), None),
        "envdelete": (
            lambda: at("f(x)"),
            lambda: "\\begin{equation}" not in text_of(page) and "f(x)" in text_of(page),
            None,
        ),
        "cmddelete": (lambda: at("sec:intro", 2), lambda: "\\ref{" not in text_of(page), None),
        "envstar": (lambda: at("f(x)"), has("\\begin{equation*}"), None),
        "leftright": (lambda: at("(a + b)", 2), has("\\left("), None),
        "rewrap": (
            lambda: at("This paragraph", 5),
            lambda: "very long line that\n" in text_of(page) or text_of(page).count("\n") > SCRATCH.count("\n"),
            None,
        ),
        "snippets": (None, dlg("#snippetsDlg[open]"), close_dialogs),
        "rellines": (None, flip("relLines"), "again"),
        "softwrap": (None, flip("wrap"), "again"),
        "zen": (None, dlg("#zenExit:not([hidden])"), lambda: page.click("#zenExit")),
    }
    return specs


def dialog_done(page):
    """The team dialog closed, or say what its error line says."""
    page.wait_for_function(
        "() => !document.querySelector('dialog[open]') || "
        "[...document.querySelectorAll('dialog[open] .err')].some((e) => !e.hidden)"
    )
    err = page.evaluate(
        "() => [...document.querySelectorAll('dialog[open] .err')].map((e) => e.hidden ? '' : e.textContent).join('')"
    )
    assert not err, f"dialog error: {err}"


def run_command(page, how, cid, title, keys):
    if how == "palette":
        palette(page, title)
    elif how == "keys":
        page.keyboard.press(keys)
    else:  # "...": the palette, grouped, without typing
        page.click("#moreBtn")
        page.wait_for_selector("#palette[open]")
        page.locator(f"#paletteList li#pal-{cid}").click()


def check_spec(page, check, cid, how, ctx):
    if callable(check):
        try:
            # wait_for_timeout, not a plain sleep: Playwright delivers events (a prompt to answer) only meanwhile.
            wait(lambda: page.wait_for_timeout(150) or check(), f"{cid} ({how})", 30)
        except AssertionError:
            print(
                "dialogs so far:",
                ctx["dialogs"][-3:],
                "answers left:",
                ctx["answers"],
                "toasts:",
                page.evaluate("() => (window.__toasts || []).slice(-3)"),
            )
            toast = page.evaluate(
                "() => document.getElementById('toast').textContent + ' | live: ' + "
                "document.getElementById('live').textContent + ' | active: ' + window.__app.active?.path + ' @' + "
                "window.__app.view.state.selection.main.head + ' modes: ' + window.__app.view.scrollDOM.className + "
                "' text: ' + "
                "JSON.stringify(window.__app.view.state.doc.toString().slice(0, 140))"
            )
            raise AssertionError(f"{cid} ({how}): no visible effect; toast: {toast!r}") from None
    else:
        expr, arg = check
        try:
            page.wait_for_function(expr, arg=arg, timeout=30000)
        except Exception as exc:
            if os.environ.get("UI_E2E_VERBOSE"):
                print(
                    "cache:",
                    [sorted(p.name for p in d.iterdir()) for d in (ROOT / ".latex-cache").glob(f"_x*_2F{ctx['doc']}")],
                    page.evaluate("() => JSON.stringify(window.__app.docs[window.__app.cur])")[:600],
                )
            toast = page.evaluate(
                "() => (window.__toasts || []).slice(-3).join(' || ') + ' | live: ' + "
                "(window.__lives || []).slice(-3).join(' || ')"
            )
            raise AssertionError(f"{cid} ({how}): no visible effect ({expr}); toasts: {toast!r}") from exc


# Shortcuts the editor itself handles: pressed with the focus in it.
EDITOR_KEYS = {
    "save",
    "definition",
    "findrefs",
    "renamelabel",
    "bookmark",
    "bmnext",
    "bmprev",
    "sugcard",
    "bold",
    "emph",
    "rewrap",
    "fold",
    "unfold",
    "foldall",
    "unfoldall",
    "check",
    "cursor",
}
KEY_NAMES = {
    "Ctrl+Shift+[": "Control+Shift+BracketLeft",
    "Ctrl+Shift+]": "Control+Shift+BracketRight",
    "Ctrl+Alt+[": "Control+Alt+BracketLeft",
    "Ctrl+Alt+]": "Control+Alt+BracketRight",
    "?": "Shift+Slash",
    "Ctrl+,": "Control+Comma",
}


def to_playwright(keys: str) -> str:
    keys = keys.replace(" (editor)", "")
    return KEY_NAMES.get(keys) or keys.replace("Ctrl", "Control")


def sweep(page, w, cmds, ctx, how, ids=None):
    """Run each command (palette, its shortcut, or the More menu) and wait for its visible effect."""
    specs = command_specs(page, ctx)
    missing = sorted(set(cmds) - set(specs))
    assert not missing, f"commands without a check: {missing}"
    done = []
    for cid, (title, keys) in cmds.items():
        if ids is not None and cid not in ids:
            continue
        if how == "keys" and not keys:
            continue
        prepare, check, cleanup = specs[cid]
        if os.environ.get("UI_E2E_VERBOSE"):
            print(f"  {how}: {cid}", flush=True)
        reset(page)
        page.evaluate(
            "() => { const t = document.getElementById('toast'); t.hidden = true; t.replaceChildren(); "
            "window.__toasts = window.__toasts || []; window.__lives = []; "
            "document.getElementById('live').textContent = ''; }"
        )
        if prepare:
            prepare()
        keyed = how == "keys" and keys
        if keyed and cid in EDITOR_KEYS:
            page.evaluate("() => window.__app.view.focus()")
        run_command(page, how, cid, title, to_playwright(keys) if keyed else None)
        check_spec(page, check, cid, how, ctx)
        if cleanup == "again" or cleanup == "thrice":
            for _ in range(1 if cleanup == "again" else 2):
                reset(page)
                palette_by_id(page, cid)
        elif cleanup:
            cleanup()
        w.check(f"{cid} via {how}", allow=ctx["allow"].get(cid, ()))
        done.append(cid)
    reset(page)
    return done


def palette_by_id(page, cid):
    """Run a command again by id (its title may have changed, e.g. 'Label numbers: off (change)')."""
    page.keyboard.press("Control+k")
    page.wait_for_selector("#palette[open]")
    page.fill("#paletteInput", "")
    page.click(f"#paletteList li[id='pal-{cid}']")
    page.wait_for_function("() => !document.getElementById('palette').open")


# ---- the features, one by one (after the command sweeps) ---------------------------------------------------------

PNG = "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mNk+M9QDwADhgGAWjR9awAAAABJRU5ErkJggg=="  # 1x1


def side_tab(page, tab):
    page.evaluate("t => document.getElementById('tab-' + t).click()", tab)
    page.wait_for_function(SIDE, arg=tab)


def drawer_tab(page, tab):
    page.evaluate("t => document.getElementById('dtab-' + t).click()", tab)
    page.wait_for_function(DRAWER, arg=tab)


def axe_ok(page, ctx, label):
    axe(page, label, ctx["axe"])
    ok(f"axe: 0 violations, {label} (light and dark)")


def feature_panels(page, w, ctx):
    """Every side and drawer tab (Git is t-gitsync's), the More menu and each dialog: used once, then axe."""
    open_file(page, "main.tex")
    side_tab(page, "files")
    page.click("#tree .row[data-path='chapter.tex']")
    page.wait_for_function("() => window.__app.active.path === 'chapter.tex'")
    axe_ok(page, ctx, "Files panel")
    side_tab(page, "search")
    page.fill("#srQ", "quick brown")
    page.wait_for_selector("#panel-search .sr-hit")
    page.locator("#panel-search .sr-hit").first.click()
    page.wait_for_function(
        "() => window.__app.view.state.sliceDoc(window.__app.view.state.selection.main.from, "
        "window.__app.view.state.selection.main.to) === 'quick brown'"
    )
    axe_ok(page, ctx, "Search panel with results")
    page.fill("#srQ", "")
    side_tab(page, "outline")
    page.wait_for_selector("#outlineList .owrap")
    page.locator("#outlineList button, #outlineList a").filter(has_text="Second").first.click()
    page.wait_for_function("() => window.__app.active.path === 'chapter.tex'")
    axe_ok(page, ctx, "Outline panel")
    side_tab(page, "refs")
    page.wait_for_selector("#panel-refs .refs-list li:has-text('knuth84')")
    page.click("#panel-refs summary[aria-label='More reference actions']")
    axe_ok(page, ctx, "References panel, its menu open")
    page.click("#panel-refs summary[aria-label='More reference actions']")
    open_file(page, "scratch.tex")
    set_text(page, SCRATCH)
    put_cursor(page, 0)
    side_tab(page, "symbols")
    page.fill("#panel-symbols .nav-filter", "alpha")
    page.click("#panel-symbols .sym")
    wait(lambda: page.wait_for_timeout(100) or text_of(page).startswith("\\alpha"), "symbol inserted at the cursor")
    axe_ok(page, ctx, "Symbols panel")
    set_text(page, SCRATCH)
    side_tab(page, "ai")
    page.wait_for_selector("#panel-ai .ai-tools")
    page.evaluate(
        "() => { const d = document.querySelector('#panel-ai .ai-settings'); d.hidden = false; d.open = true; }"
    )
    axe_ok(page, ctx, "Assistant panel, settings open")
    ok("side tabs: files opens a file, search finds and selects, outline jumps, references list, symbols insert")
    for tab in ("problems", "warnings", "lint", "grammar", "review", "history", "todos", "bookmarks", "log"):
        drawer_tab(page, tab)
        page.wait_for_timeout(300)
        axe_ok(page, ctx, f"drawer: {tab}")
    drawer_tab(page, "todos")
    page.locator("#todoList .nav-row").filter(has_text="check this sentence").first.click()
    page.wait_for_function(
        "() => window.__app.active.path === 'main.tex' && window.__app.view.state.doc.lineAt("
        "window.__app.view.state.selection.main.head).text.includes('TODO')"
    )
    ok("TODO list: a row jumps to its line")
    page.click("#moreBtn")
    page.wait_for_selector("#palette[open] .pal-group")
    axe_ok(page, ctx, "More (palette grouped) open")
    page.keyboard.press("Escape")
    for cid, sel in (
        ("settings", "#settings[open]"),
        ("cheat", "#cheat[open]"),
        ("snippets", "#snippetsDlg[open]"),
        ("graph", "#navDlg[open] .nav-graph svg"),
        ("palette", "#palette[open]"),
    ):
        reset(page)
        palette_by_id(page, cid) if cid != "palette" else page.keyboard.press("Control+k")
        page.wait_for_selector(sel)
        axe_ok(page, ctx, f"dialog: {cid}")
    reset(page)
    palette_by_id(page, "graph")
    names = page.eval_on_selector_all("#navDlg .nav-node title", "els => els.map((e) => e.textContent)")
    assert "chapter.tex" in names, names
    reset(page)
    select_tree(page, "main.tex")
    palette_by_id(page, "newfile")
    page.wait_for_selector("#fsDlg[open]")
    axe_ok(page, ctx, "dialog: new file")
    reset(page)
    ok("file graph lists the \\input chapter")


def feature_hovers(page, w, ctx):
    """Math, \\cite, \\ref and package hover cards, and folding by the gutter."""
    open_file(page, "main.tex")
    page.evaluate("() => { window.__app.view.scrollDOM.scrollTop = 0; }")
    hover_at(page, find(page, "a^2+b^2", 3))
    page.wait_for_selector(".cm-mathHover .katex")
    axe_ok(page, ctx, "math hover card")
    hover_at(page, find(page, "knuth84", 2))
    page.wait_for_selector(".cm-texHover .cm-texHover-head:has-text('Knuth')")
    assert "TeXbook" in page.inner_text(".cm-texHover")
    hover_at(page, find(page, "ref{sec:intro}", 6))
    page.wait_for_selector(".cm-texHover:has-text('main.tex:')")
    hover_at(page, find(page, "{amsmath}", 3))
    page.wait_for_selector(".cm-texHover a:has-text('Open the documentation of amsmath')")
    axe_ok(page, ctx, "package hover card")
    page.mouse.move(0, 0)
    ok("hover cards: math rendered by KaTeX, \\cite with author and title, \\ref with file and line, package docs link")
    page.evaluate(
        "p => window.__app.view.dispatch({ effects: [], selection: { anchor: p }, scrollIntoView: true })",
        find(page, "\\begin{equation}"),
    )
    marker = page.locator(".cm-foldGutter .cm-activeLineGutter span[title='Fold line']")  # the cursor's line
    expect(marker).to_be_visible()
    marker.click()
    page.wait_for_selector(".cm-foldPlaceholder")
    assert "E = mc^2" not in page.inner_text(".cm-content")
    page.click(".cm-foldPlaceholder")
    page.wait_for_selector(".cm-foldPlaceholder", state="detached")
    ok("folding: the gutter marker folds the equation, its placeholder unfolds it")


def feature_edit_tools(page, w, ctx):
    """Snippets, the figure dialog of a pasted image, the table editor and the TikZ canvas, each through to the text."""
    open_file(page, "scratch.tex")
    set_text(page, SCRATCH)
    palette_by_id(page, "snippets")
    page.wait_for_selector("#snippetsDlg[open] .et-snip")
    page.click("#snippetsDlg button:has-text('Add snippet')")
    last = page.locator("#snippetsDlg .et-snip").last
    last.locator("input").fill("zzq")
    last.locator("input").press("Tab")
    last.locator("textarea").fill("ZZ-${1:here}")
    last.locator("textarea").press("Tab")
    page.click("#snippetsDlg button:has-text('Done')")
    put_cursor(page, len(text_of(page)))
    page.keyboard.type("\nzzq")
    page.keyboard.press("Tab")
    wait(lambda: page.wait_for_timeout(100) or "ZZ-here" in text_of(page), "snippet expanded")
    ok("snippets: a new one made in the dialog expands on Tab")
    set_text(page, SCRATCH)
    put_cursor(page, len(SCRATCH))
    page.evaluate(
        """png => {
        const b = Uint8Array.from(atob(png), (c) => c.charCodeAt(0)), d = new DataTransfer();
        d.items.add(new File([b], "dot.png", { type: "image/png" }));
        document.querySelector(".cm-content").dispatchEvent(new ClipboardEvent("paste",
          { clipboardData: d, bubbles: true, cancelable: true })); }""",
        PNG,
    )
    page.wait_for_selector("dialog.fig-dlg[open]")
    axe_ok(page, ctx, "figure dialog")
    page.fill("#figCaption", "A dot")
    page.click("dialog.fig-dlg[open] button:has-text('Save and insert')")
    wait(lambda: page.wait_for_timeout(100) or "\\caption{A dot}" in text_of(page), "figure inserted")
    pngs = [p.name for p in (ctx["src"] / ctx["doc"]).rglob("*.png")]
    assert pngs, "the pasted image was not saved"
    hover_at(page, find(page, "\\includegraphics", 3))
    page.wait_for_selector(".cm-texHover.et-thumb img")
    page.mouse.move(0, 0)
    ok(f"figure dialog: pasted image saved ({pngs[0]}), figure with caption inserted, thumbnail on hover")
    set_text(page, SCRATCH)
    put_cursor(page, find(page, "alpha"))
    palette_by_id(page, "tableedit")
    page.wait_for_selector("dialog.tbl-dlg[open]")
    axe_ok(page, ctx, "table editor")
    rows = text_of(page).count("\\\\")
    page.click("dialog.tbl-dlg[open] button[title='Insert a row below']")
    page.click("dialog.tbl-dlg[open] button:has-text('Apply')")
    wait(lambda: page.wait_for_timeout(100) or text_of(page).count("\\\\") == rows + 1, "table row added")
    ok("table editor: + below and Apply add a row")
    put_cursor(page, find(page, "\\draw"))
    palette_by_id(page, "tikzedit")
    page.wait_for_selector("dialog.tz-dlg[open] svg")
    axe_ok(page, ctx, "TikZ canvas")
    page.click("dialog.tz-dlg[open] button:has-text('Add node')")
    box = page.locator("dialog.tz-dlg[open] svg").first.bounding_box()
    page.mouse.click(box["x"] + box["width"] * 0.7, box["y"] + box["height"] * 0.3)
    page.click("dialog.tz-dlg[open] button:has-text('Apply')")
    wait(lambda: page.wait_for_timeout(100) or "\\node" in text_of(page), "TikZ node added")
    ok("TikZ canvas: Add node, click, Apply writes a \\node")
    set_text(page, SCRATCH)


def feature_marks_zen(page, w, ctx):
    """Bookmarks listed and followed from the drawer; zen mode hides the chrome and Escape leaves it."""
    open_file(page, "scratch.tex")
    set_text(page, SCRATCH)
    line = 5
    page.evaluate(
        "n => { const v = window.__app.view; v.dispatch({ selection: { anchor: v.state.doc.line(n).from } }); "
        "v.focus(); }",
        line,
    )
    wanted = f"scratch.tex:{line}"
    for _ in range(2):
        palette_by_id(page, "bookmark")
        drawer_tab(page, "bookmarks")
        page.wait_for_timeout(700)
        if wanted in page.inner_text("#bookmarkList"):
            break
        reset(page)
        page.evaluate(
            "n => { const v = window.__app.view; v.dispatch({ selection: { anchor: v.state.doc.line(n).from } }); }",
            line,
        )
    assert wanted in page.inner_text("#bookmarkList"), page.inner_text("#bookmarkList")
    put_cursor(page, 0)
    page.locator("#bookmarkList .nav-row").filter(has_text=wanted).first.click()
    page.wait_for_function(
        "n => window.__app.view.state.doc.lineAt(window.__app.view.state.selection.main.head).number === n", arg=line
    )
    ok("bookmarks: toggled on a line, listed in the drawer, the row jumps back to it")
    palette_by_id(page, "zen")
    page.wait_for_selector("#zenExit:not([hidden])")
    assert page.evaluate(
        "() => ['topbar', 'pdfPane', 'side'].every((id) => !document.getElementById(id).offsetParent)"
    ), "zen mode left the chrome"
    axe_ok(page, ctx, "zen mode")
    page.evaluate("() => window.__app.view.focus()")
    page.keyboard.press("Escape")
    page.wait_for_selector("#zenExit", state="hidden")
    ok("zen mode: on from the palette, Escape leaves it")


def review_data(page):
    return page.evaluate("async () => (await fetch('api/review?doc=' + encodeURIComponent(window.__app.cur))).json()")


def comment_on(page, needle, text, path="main.tex"):
    """Select needle, open a comment with the Comment button, send text with Ctrl+Enter."""
    open_file(page, path)
    at = find(page, needle)
    put_cursor(page, at, at + len(needle))
    page.click("#commentBtn")
    area = page.locator(".rv-compose textarea")
    area.fill(text)
    area.press("Control+Enter")
    wait(
        lambda: (
            page.wait_for_timeout(150)
            or any(c["text"] == text for t in review_data(page)["threads"] for c in t["comments"])
        ),
        f"comment {text!r} saved",
    )


def suggest_at(page, needle, insert, path="main.tex"):
    """Suggest mode on, type insert after needle, suggest mode off (sends the draft)."""
    open_file(page, path)
    before = len(review_data(page)["suggestions"])
    if page.get_attribute("#suggestBtn", "aria-checked") != "true":
        page.click("#suggestBtn")
    put_cursor(page, find(page, needle, len(needle)))
    page.keyboard.type(insert)
    if not page.is_disabled("#suggestBtn"):
        page.click("#suggestBtn")
    else:
        put_cursor(page, 0)
    wait(lambda: page.wait_for_timeout(150) or len(review_data(page)["suggestions"]) > before, "suggestion saved")
    return next(s for s in review_data(page)["suggestions"] if s["insert"] == insert)


def fetch_bytes(page, url):
    """GET url with the page's cookies: (status, content type, first bytes)."""
    r = page.request.get(url if url.startswith("http") else page.url.split("#")[0].rsplit("/", 1)[0] + "/" + url)
    return r.status, r.headers.get("content-type", ""), r.body()


def feature_history(page, w, ctx):
    """Name a version, change the file, view the version (diff dialog), download it as a zip, compare it with now as
    a latexdiff PDF, restore it."""
    open_file(page, "main.tex")
    drawer_tab(page, "history")
    page.click("#dpanel-history button:has-text('Name this version')")
    page.fill("#hvLabel", "Milestone one")
    page.press("#hvLabel", "Enter")
    page.wait_for_selector("#dpanel-history .hv-item.named:has-text('Milestone one')")
    before = text_of(page)
    put_cursor(page, find(page, "Inline math"))
    page.keyboard.type("Changed after the milestone. ")
    wait(lambda: page.wait_for_timeout(200) or "Changed after the milestone" in ctx["disk"]("main.tex"), "change saved")
    page.locator("#dpanel-history .hv-item.named:has-text('Milestone one') button:has-text('View')").click()
    page.wait_for_selector("#diffDlg[open] .hv-diff")
    assert "Changed after the milestone" in page.inner_text("#diffDlg .hv-diff")
    axe_ok(page, ctx, "version dialog (compared with now)")
    with page.expect_download() as got:
        page.click("#diffDlg a:has-text('Download zip')")
    path = got.value.path()
    with zipfile.ZipFile(path) as z:
        names = z.namelist()
        main = next(n for n in names if n.endswith("main.tex"))
        assert "Changed after the milestone" not in z.read(main).decode(), "the zip is not the named version"
    ok(f"history: named version listed; its zip holds the text as it was ({len(names)} files)")
    page.click("#diffDlg button:has-text('Compare with now (PDF)')")
    page.wait_for_function(TOAST, arg="Comparison ready|comparison failed", timeout=180000)
    toast = page.inner_text("#toast")
    assert "Comparison ready" in toast, toast
    href = page.get_attribute("#toast a:has-text('Open')", "href")
    status, kind, body = fetch_bytes(page, href)
    assert status == 200 and body.startswith(b"%PDF"), (status, kind, body[:20])
    ok("history: Compare with now builds a latexdiff PDF; its Open link serves it")
    page.click("#diffDlg button:has-text('Restore main.tex')")
    wait(
        lambda: page.wait_for_timeout(200) or "Changed after the milestone" not in text_of(page),
        "restored in the editor",
    )
    wait(lambda: page.wait_for_timeout(200) or ctx["disk"]("main.tex") == before, "restored on disk")
    page.evaluate("() => document.getElementById('diffDlg').open && document.getElementById('diffDlg').close()")
    ok("history: Restore puts the named version back, in the editor and on disk")


def feature_review_pdf(page, w, ctx):
    """A comment and a suggestion by the owner, then Build with suggestions (PDF): both marked, the PDF served."""
    comment_on(page, "quick brown", "Owner note on the fox.")
    sug = suggest_at(page, "lazy dog", " very")
    assert " very" not in ctx["disk"]("main.tex"), "a suggestion changed the file"
    palette_by_id(page, "reviewpdf")
    page.wait_for_function(TOAST, arg="suggestion\\(s\\) and .* comment\\(s\\) marked|failed", timeout=180000)
    toast = page.inner_text("#toast")
    found = re.search(r"(\d+) suggestion\(s\) and (\d+) comment\(s\) marked", toast)
    assert found and int(found.group(1)) >= 1 and int(found.group(2)) >= 1, toast
    href = page.get_attribute("#toast a:has-text('Open')", "href")
    status, kind, body = fetch_bytes(page, href)
    assert status == 200 and body.startswith(b"%PDF"), (status, kind, body[:20])
    if shutil.which("pdftotext"):
        with tempfile.NamedTemporaryFile(suffix=".pdf") as f:
            f.write(body)
            f.flush()
            text = subprocess.run(["pdftotext", f.name, "-"], capture_output=True, text=True).stdout
        assert "very" in text, text[:400]
    ok(f"review markup PDF: {toast.split('.')[0]}; served as a PDF with the suggested words")
    ctx["owner_suggestion"] = sug["id"]


def feature_lock(page, w, ctx):
    """Lock: the chip shows, the editor and the file API refuse writes; the chip unlocks."""
    open_file(page, "main.tex")
    palette_by_id(page, "lock")
    page.wait_for_selector("#lockChip:not([hidden])")
    axe_ok(page, ctx, "locked document")
    before = text_of(page)
    put_cursor(page, 0)
    page.keyboard.type("XYZ")
    page.wait_for_timeout(300)
    assert text_of(page) == before, "typed into a locked document"
    status = api_status(page, "PUT", "api/file?doc=DOC&path=chapter.tex", {"text": "x"})
    assert status in (403, 409, 423), status
    w.check("lock", allow=[r"HTTP 4\d\d PUT .*/api/file\?"])
    assert ctx["disk"]("chapter.tex") == CHAPTER
    page.click("#lockChip")
    page.wait_for_selector("#lockChip", state="hidden")
    put_cursor(page, 0)
    page.keyboard.type("Q")
    wait(lambda: page.wait_for_timeout(100) or text_of(page) != before, "typing after unlock")
    page.keyboard.press("Backspace")
    ok(f"lock: read-only in the editor, the file API answers {status}; the chip unlocks it")


def feature_linked(page, w, ctx):
    """A file linked from the other document is read-only in the editor; unlinking makes it ordinary."""
    select_tree(page, "main.tex")
    palette_by_id(page, "linkfile")
    page.fill("#lnkSrc", f"{ctx['other']}:shared.bib")
    page.press("#lnkSrc", "Tab")
    expect(page.locator("#lnkTo")).to_have_value("shared.bib")  # filled from the source
    page.fill("#lnkTo", "linked.bib")  # the command sweep left an ordinary shared.bib
    axe_ok(page, ctx, "Link a file dialog")
    page.click("dialog[open] button[type=submit]")
    dialog_done(page)
    page.wait_for_selector("#tree .row[data-path='linked.bib'] .tag.linked")
    open_file(page, "linked.bib")
    before = text_of(page)
    assert "shared01" in before
    put_cursor(page, 0)
    page.keyboard.type("XYZ")
    page.wait_for_timeout(300)
    assert text_of(page) == before, "typed into a linked file"
    select_tree(page, "linked.bib")
    palette_by_id(page, "unlink")
    page.wait_for_selector("#tree .row[data-path='linked.bib'] .tag.linked", state="detached")
    ok("linked file: copied from the other document, read-only in the editor, unlinked to an ordinary file")


def feature_folder_upload(page, w, ctx):
    """Files tab: Upload a folder (the tree tool's folder picker) puts the folder and its files into the document."""
    with tempfile.TemporaryDirectory() as tmp:
        folder = Path(tmp) / "figs"
        (folder / "sub").mkdir(parents=True)
        (folder / "a.tex").write_text("% uploaded a\n", encoding="utf-8")
        (folder / "sub" / "b.tex").write_text("% uploaded b\n", encoding="utf-8")
        select_tree(page, "main.tex")
        page.set_input_files("#panel-files .tree-tools input[type=file]", str(folder))
        wait(
            lambda: page.wait_for_timeout(200) or (ctx["src"] / ctx["doc"] / "figs" / "sub" / "b.tex").exists(),
            "folder uploaded",
        )
    assert (ctx["src"] / ctx["doc"] / "figs" / "a.tex").read_text(encoding="utf-8") == "% uploaded a\n"
    page.wait_for_selector("#tree .row[data-path='figs']")
    ok("Files tab: Upload a folder puts figs/a.tex and figs/sub/b.tex into the document")


def feature_recovery(page, w, ctx):
    """Crash recovery: unsaved text is kept in IndexedDB while it is unsaved; on the next visit the editor offers it
    back (Recover it), and recovering puts it into the file."""
    open_file(page, "chapter.tex")
    page.evaluate("() => { window.__app.settings.autosave = 600000; }")  # nothing saved while the test looks
    put_cursor(page, len(text_of(page)))
    page.keyboard.type("Unsaved words.")
    kept = """async (k) => await new Promise((ok) => { const r = indexedDB.open('latex-studio-recovery', 1);
        r.onsuccess = () => { const g = r.result.transaction('buffers').objectStore('buffers').get(k);
        g.onsuccess = () => ok(g.result || null); g.onerror = () => ok(null); }; r.onerror = () => ok(null); })"""
    key = f"{ctx['doc']}\nchapter.tex"
    found = wait(lambda: page.wait_for_timeout(200) or page.evaluate(kept, key), "unsaved text kept in IndexedDB", 10)
    assert "Unsaved words." in found["text"], found
    ok("crash recovery: unsaved text is kept in IndexedDB")
    # The crash: a tab that never saved. Its text is in IndexedDB, the file and the room still hold the old text.
    page.evaluate(
        """async ([k, t]) => await new Promise((ok) => { const r = indexedDB.open('latex-studio-recovery', 1);
        r.onsuccess = () => { const tx = r.result.transaction('buffers', 'readwrite');
        tx.objectStore('buffers').put({ text: t, time: Date.now() }, k); tx.oncomplete = () => ok(true); }; })""",
        [key, CHAPTER + "Recovered after a crash.\n"],
    )
    page.evaluate("() => { window.__app.settings.autosave = 1000; }")
    page.evaluate(
        "() => window.__app.view.dispatch({ changes: { from: 0, to: window.__app.view.state.doc.length, "
        "insert: window.__app.view.state.doc.toString().replace('Unsaved words.', '') } })"
    )
    wait(lambda: page.wait_for_timeout(200) or ctx["disk"]("chapter.tex") == CHAPTER, "chapter back to its text")
    page.evaluate(
        """async ([k, t]) => await new Promise((ok) => { const r = indexedDB.open('latex-studio-recovery', 1);
        r.onsuccess = () => { const tx = r.result.transaction('buffers', 'readwrite');
        tx.objectStore('buffers').put({ text: t, time: Date.now() }, k); tx.oncomplete = () => ok(true); }; })""",
        [key, CHAPTER + "Recovered after a crash.\n"],
    )
    page.reload()
    page.wait_for_function(LOADED, timeout=60000)
    w.check("reload", allow=[r"/api/(send|file)\?.*ERR_ABORTED"])  # a message or a last save on its way
    # The settings in this browser may have been stored while autosave was off above: on again, in memory.
    page.evaluate("() => { window.__app.settings.autosave = 1000; }")
    open_file(page, "chapter.tex")
    page.wait_for_selector("#toast button:has-text('Recover it')", timeout=15000)
    axe_ok(page, ctx, "recovery offer")
    page.click("#toast button:has-text('Recover it')")
    wait(lambda: page.wait_for_timeout(200) or "Recovered after a crash." in text_of(page), "recovered into the editor")
    wait(
        lambda: page.wait_for_timeout(200) or "Recovered after a crash." in ctx["disk"]("chapter.tex"),
        "recovered text saved",
    )
    # A copy kept while it was unsaved may stay; offer() drops it unasked once it equals the file.
    page.evaluate(
        "() => window.__app.view.dispatch({ changes: { from: 0, to: window.__app.view.state.doc.length, "
        "insert: window.__app.view.state.doc.toString().replace('Recovered after a crash.\\n', '') } })"
    )
    wait(lambda: page.wait_for_timeout(200) or ctx["disk"]("chapter.tex") == CHAPTER, "chapter back to its text")
    ok("crash recovery: after a reload the editor offers the kept text; Recover it restores and saves it")


NEWEST_AI = "#panel-ai .ai-log li:first-child"


def ai_answer(page, button, label):
    """Click an Assistant button and wait for its answer (the newest log entry, done)."""
    count = page.locator("#panel-ai .ai-log > li").count()
    page.click(f"#panel-ai button:has-text('{button}')")
    page.wait_for_function(
        "n => document.querySelectorAll('#panel-ai .ai-log > li').length > n && "
        "!document.querySelector('#panel-ai .ai-log').hasAttribute('aria-busy')",
        arg=count,
        timeout=30000,
    )
    text = page.inner_text(NEWEST_AI)
    assert "Stub answer" in text or "ai-claims" in page.inner_html(NEWEST_AI), f"{label}: {text[:300]}"
    return text


def feature_ai(page, w, ctx):
    """The Assistant panel against an OpenAI-compatible stub on loopback: Ask, Whole project, Write LaTeX at cursor,
    Find claims (Crossref candidates stubbed in the browser, added through the real /api/bib/edit), Explain first
    error with its Apply."""
    open_file(page, "main.tex")
    side_tab(page, "ai")
    page.fill("#panel-ai textarea", "What does this file say?")
    ai_answer(page, "Ask", "Ask")
    assert "What does this file say?" in Stub.bodies[-1] and "A second section" not in Stub.bodies[-1]
    page.check("#panel-ai input[type=checkbox][id^=ai-whole]")
    page.fill("#panel-ai textarea", "Where is the second section?")
    ai_answer(page, "Ask", "Whole project")
    assert "A second section" in Stub.bodies[-1], "Whole project did not send the chapter"
    page.uncheck("#panel-ai input[type=checkbox][id^=ai-whole]")
    ok("assistant: Ask sends this file; Whole project also sends the chapter's matching passage")
    open_file(page, "scratch.tex")
    set_text(page, SCRATCH)
    put_cursor(page, len(SCRATCH))
    page.fill("#panel-ai textarea", "a bold word")
    ai_answer(page, "Write LaTeX at cursor", "Write")
    page.click(f"{NEWEST_AI} button:has-text('Insert at cursor')")
    wait(
        lambda: (
            page.wait_for_timeout(100) or text_of(page).endswith("\\textbf{stub}") or "\\textbf{stub}" in text_of(page)
        ),
        "written LaTeX inserted",
    )
    page.wait_for_selector(f"{NEWEST_AI} .ok-mark:has-text('Applied')")
    set_text(page, SCRATCH)
    ok("assistant: Write LaTeX at cursor, Insert at cursor applies it")
    put_cursor(page, len(SCRATCH))
    page.set_input_files(
        "#panel-ai input[type=file]",
        files=[{"name": "eq.png", "mimeType": "image/png", "buffer": __import__("base64").b64decode(PNG)}],
    )
    page.wait_for_function(
        "() => /eq.png/.test(document.querySelector('#panel-ai .ai-log li:first-child')?.textContent || '') && "
        "!document.querySelector('#panel-ai .ai-log').hasAttribute('aria-busy')",
        timeout=30000,
    )
    assert '"image_url"' in Stub.bodies[-1] or "base64" in Stub.bodies[-1], Stub.bodies[-1][:300]
    page.click(f"{NEWEST_AI} button:has-text('Insert at cursor')")
    wait(lambda: page.wait_for_timeout(100) or "\\textbf{stub}" in text_of(page), "LaTeX from the image inserted")
    set_text(page, SCRATCH)
    ok("assistant: LaTeX from image sends the picture and inserts the answer at the cursor")
    page.evaluate(
        "() => { const d = document.querySelector('#panel-ai .ai-settings'); d.hidden = false; d.open = true; }"
    )
    page.locator("#panel-ai").get_by_label("API key").fill("sk-test-not-real-0123")
    page.click("#panel-ai button:has-text('Save key')")
    page.wait_for_selector("#panel-ai button:has-text('Forget')")
    settings_file = ctx["config"] / "latex-pipeline" / "ai.json"
    stored = settings_file.read_text()
    assert settings_file.stat().st_mode & 0o077 == 0, oct(settings_file.stat().st_mode)
    assert "sk-test-not-real-0123" in stored and "sk-test-not-real-0123" not in page.content()
    assert "sk-test-not-real-0123" not in page.evaluate("async () => (await fetch('api/ai')).text()")
    page.click("#panel-ai button:has-text('Forget')")
    page.wait_for_selector("#panel-ai button:has-text('Forget')", state="detached")
    assert "sk-test-not-real-0123" not in (ctx["config"] / "latex-pipeline" / "ai.json").read_text()
    ok("assistant settings: a key saved to the 0600 settings file, never sent back to the page; Forget removes it")
    open_file(page, "main.tex")
    page.route(
        "**/api/bib/search?**",
        lambda r: r.fulfill(
            status=200,
            content_type="application/json",
            body=json.dumps(
                {
                    "candidates": [
                        {
                            "type": "article",
                            "fields": {
                                "title": "On Quick Foxes",
                                "author": "Fox, Ada",
                                "year": "2001",
                                "journal": "Journal of Foxes",
                                "doi": "10.5555/fox.1",
                            },
                        }
                    ]
                }
            ),
        ),
    )
    ai_answer(page, "Find claims needing citations", "claims")
    page.wait_for_selector(f"{NEWEST_AI} .ai-claims li:has-text('The quick brown fox')")
    page.click(f"{NEWEST_AI} .ai-claims button:has-text('Search Crossref')")
    page.click(f"{NEWEST_AI} .ai-cand button:has-text('Add to .bib')")
    added = page.inner_text(f"{NEWEST_AI} .ok-mark:has-text('Added')")  # into the document's first .bib file
    target = added.rsplit(" to ", 1)[1].strip()
    wait(lambda: page.wait_for_timeout(200) or "On Quick Foxes" in ctx["disk"](target), "candidate added to the .bib")
    axe_ok(page, ctx, "assistant answers (claims with a Crossref candidate)")
    page.unroute("**/api/bib/search?**")
    ok(f"assistant: claims listed with their quote; a Crossref candidate is added to {target}")
    open_file(page, "chapter.tex")
    started = time.time()
    set_text(page, CHAPTER + "\\undefinedcmd{}\n")
    page.wait_for_function(
        "t => { const d = window.__app.docs[window.__app.cur]; return d.status === 'failed' && d.finished > t; }",
        arg=started,
        timeout=120000,
    )
    open_file(page, "main.tex")
    ai_answer(page, "Explain first error", "explain")
    page.click(f"{NEWEST_AI} button:has-text('Apply')")
    page.wait_for_selector(f"{NEWEST_AI} .ok-mark:has-text('Applied')")
    wait(lambda: page.wait_for_timeout(200) or ctx["disk"]("chapter.tex") == CHAPTER + "\n", "the fix saved")
    open_file(page, "chapter.tex")
    set_text(page, CHAPTER)
    started = time.time()
    page.wait_for_function(
        "t => { const d = window.__app.docs[window.__app.cur]; return d.status === 'ok' && d.finished > t; }",
        arg=started,
        timeout=120000,
    )
    ok("assistant: Explain first error on a failed build; Apply puts its fix into the file")


def feature_grammar(page, w, ctx):
    """Grammar from a LanguageTool stub on loopback: the finding is underlined, listed, and fixed from its card."""
    open_file(page, "main.tex")
    palette_by_id(page, "grammar")
    page.wait_for_selector(".cm-grammar", timeout=30000)
    page.wait_for_function("() => /spelling mistake/.test(document.getElementById('dpanel-grammar').textContent)")
    hover_at(page, find(page, "teh", 1))
    page.wait_for_selector(".cm-grammar-tip button:has-text('the')")
    axe_ok(page, ctx, "grammar card")
    page.click(".cm-grammar-tip button:has-text('the')")
    wait(lambda: page.wait_for_timeout(100) or "over the lazy" in text_of(page), "grammar fix applied")
    page.mouse.move(0, 0)
    ok("grammar: underline, drawer list and the card's fix")


def refs_action(page, label):
    side_tab(page, "refs")
    page.wait_for_selector("#panel-refs .refs-list li")
    page.click("#panel-refs summary[aria-label='More reference actions']")
    page.click(f"#panel-refs .refs-ovf-menu button:has-text('{label}')")


def feature_zotero_library(page, w, ctx):
    """Zotero settings (saved locally, key never echoed), Sync from Zotero (its fetch stubbed in the browser; the
    apply step is real) and My library (add BibTeX, insert into refs.bib)."""
    open_file(page, "main.tex")
    refs_action(page, "Zotero settings...")
    form = page.locator("#panel-refs form.refs-form")
    form.get_by_label("Library ID").fill("123456")
    form.get_by_label("API key").fill("testKeyNotReal0123")
    axe_ok(page, ctx, "Zotero settings")
    form.locator("button:has-text('Save')").click()
    try:
        page.wait_for_function(TOAST, arg="Zotero settings saved", timeout=10000)
    except Exception:
        raise AssertionError(f"Zotero settings: {page.inner_text('#panel-refs .refs-err')}") from None
    stored = ctx["config"] / "latex-pipeline" / "zotero.json"
    assert stored.exists() and stored.stat().st_mode & 0o077 == 0, "zotero.json missing or readable by others"
    info = page.evaluate("async () => (await fetch('api/zotero')).text()")
    assert "testKeyNotReal0123" not in info and '"has_key": true' in info.replace('":true', '": true'), info
    page.route(
        "**/api/zotero/preview?**",
        lambda r: r.fulfill(
            status=200,
            content_type="application/json",
            body=json.dumps(
                {
                    "new": [
                        {
                            "type": "book",
                            "key": "zot01",
                            "fields": {
                                "author": "Zed, Zoe",
                                "title": "A Zotero Book",
                                "year": "2020",
                                "publisher": "Press",
                            },
                        }
                    ],
                    "changed": [],
                    "same": 1,
                    "elsewhere": [],
                    "local_only": [],
                    "skipped": 0,
                }
            ),
        ),
    )
    refs_action(page, "Sync from Zotero...")
    page.locator("#panel-refs form.refs-form").get_by_label("Add to and update").select_option("refs.bib")
    page.click("#panel-refs form.refs-form button:has-text('Fetch from Zotero')")
    page.wait_for_selector("#panel-refs button:has-text('Apply 1 selected')")
    axe_ok(page, ctx, "Zotero sync preview")
    page.click("#panel-refs button:has-text('Apply 1 selected')")
    wait(lambda: page.wait_for_timeout(200) or "A Zotero Book" in ctx["disk"]("refs.bib"), "Zotero entry applied")
    page.unroute("**/api/zotero/preview?**")
    ok("Zotero: settings saved (0600, key never sent back); sync preview applied into refs.bib")
    refs_action(page, "My library...")
    page.click("#panel-refs summary:has-text('Add by DOI, BibTeX or RIS')")
    page.fill(
        "#panel-refs textarea",
        "@article{lib01, author = {Lib, Lena}, title = {Library Paper}, journal = {J}, year = {2019}}",
    )
    page.click("#panel-refs details[open] button:has-text('Add')")
    page.wait_for_selector("#panel-refs .lib-list li:has-text('lib01')")
    axe_ok(page, ctx, "My library")
    page.check("#panel-refs .lib-list li:has-text('lib01') input[type=checkbox]")
    page.locator("#panel-refs").get_by_label("Insert into").select_option("refs.bib")
    page.click("#panel-refs button:has-text('Insert ticked')")
    wait(lambda: page.wait_for_timeout(200) or "Library Paper" in ctx["disk"]("refs.bib"), "library entry inserted")
    assert (ctx["config"] / "latex-pipeline" / "library.json").exists()
    ok("My library: BibTeX added to the personal library, inserted into refs.bib")


def card_at(page, pos):
    """Hover a suggestion and return its card's text (the hover card, not the pinned one)."""
    hover_at(page, pos)
    page.wait_for_selector(".cm-sug-card .rv-who")
    return page.inner_text(".cm-sug-card")


def card_buttons(page):
    return page.eval_on_selector_all(
        ".cm-sug-card .rv-head button", "bs => bs.map((b) => b.getAttribute('aria-label'))"
    )


def api_status(page, method, url, body=None):
    return page.evaluate(
        """async ([m, u, b]) => (await fetch(u.replace('DOC', encodeURIComponent(window.__app.cur)),
        { method: m, headers: { 'Content-Type': 'application/json' },
          body: b == null ? undefined : JSON.stringify(b) })).status""",
        [method, url, body],
    )


def pdf_point(page, fx, fy):
    """A point on the first PDF page, as fractions of its box."""
    page.wait_for_selector("#pages .page canvas", timeout=60000)
    box = page.locator("#pages .page").first.bounding_box()
    return box["x"] + box["width"] * fx, box["y"] + box["height"] * fy


def feature_sharing(page, w, ctx):
    """Share (no tunnel), a named link per role in its own browser context, and what each role can and cannot do:
    co-editing, suggestions and their card by role, comments with an @mention, the bell empty and filled, comments on
    the PDF by click and by drag, a request for more access granted by the owner, exports off while sharing, and
    a revoked link's tab locked at once."""
    owner, browser = page, ctx["browser"]
    open_file(owner, "main.tex")
    palette_by_id(owner, "share")
    owner.select_option("#shareProvider", "local")
    owner.click("#shareStart")
    try:
        owner.wait_for_selector("#link-view", timeout=30000)
    except Exception:
        print("share dialog:", owner.inner_text("#share"), "toasts:", owner.evaluate("() => window.__toasts"))
        raise
    for name, role in (("Edith", "edit"), ("Rita", "review"), ("Vic", "view")):
        owner.fill("#shareLinkName", name)
        owner.select_option("#shareLinkRole", role)
        owner.click("#shareLinkCreate")
        owner.wait_for_function(
            "n => [...document.querySelectorAll('.named-list b')].some((b) => b.textContent.startsWith(n))", arg=name
        )
    urls = {
        row.query_selector("b").text_content().split(" (")[0]: row.query_selector("input").input_value()
        for row in owner.query_selector_all(".named-list .link-row")
    }
    axe_ok(owner, ctx, "Share dialog while sharing, three named links")
    owner.keyboard.press("Escape")
    off = [t for _, items in more_view(owner) for t in items if t.endswith("(disabled)")]
    assert any("arXiv" in t for t in off), off
    ok(f"sharing on (no tunnel); named links for Edith, Rita and Vic; off while sharing: {', '.join(off)}")

    guests = {}
    for name in ("Edith", "Rita", "Vic"):
        gpage, gw = new_page(browser, name)
        gw.allow.append(r"HTTP 403 GET .*/api/library$")  # the References panel asks whether this viewer has a library
        open_editor(gpage, urls[name].replace("127.0.0.1", "localhost"))
        gpage.wait_for_function("() => !window.__app.active.collab || window.__app.active.collab.ready")
        guests[name] = (gpage, gw)
    edith, rita, vic = (guests[n][0] for n in ("Edith", "Rita", "Vic"))
    chips = {n: guests[n][0].inner_text("#roleChip") for n in guests}
    assert (
        "Can edit as Edith" in chips["Edith"]
        and "Reviewer as Rita" in chips["Rita"]
        and "View only as Vic" in chips["Vic"]
    ), chips
    names = wait(
        lambda: (lambda t: all(n in t for n in guests) and t)(owner.text_content("#usersList")), "presence names"
    )
    assert "unverified" not in names, names
    cmds = {
        n: inventory(guests[n][0], f"{n} ({ {'Edith': 'edit', 'Rita': 'review', 'Vic': 'view'}[n] } link)")
        for n in guests
    }
    for n, gone in (
        ("Edith", {"share", "lock", "linkfile", "docx", "arxiv"}),
        ("Rita", {"share", "save", "rename", "delete", "newfile", "lock"}),
        ("Vic", {"share", "save", "comment", "suggest", "newfile", "delete", "lock"}),
    ):
        assert not gone & set(cmds[n]), f"{n} has {gone & set(cmds[n])}"
    assert "access" in cmds["Vic"] and "access" in cmds["Rita"] and "access" not in cmds["Edith"]
    for n, (gpage, _) in guests.items():
        assert gpage.is_hidden("#shareBtn"), f"{n} sees Share"
    axe_ok(vic, ctx, "Vic (view link)")
    axe_ok(rita, ctx, "Rita (review link)")
    ok("roles: chips and presence show the link names; each palette lacks what the role may not do")

    # Co-editing: Edith's typing reaches the owner and the disk; the owner's reaches Edith.
    put_cursor(edith, find(edith, "Inline math"))
    edith.keyboard.type("Edith was here. ")
    wait(lambda: owner.wait_for_timeout(100) or "Edith was here." in text_of(owner), "Edith's edit at the owner")
    wait(lambda: owner.wait_for_timeout(200) or "Edith was here." in ctx["disk"]("main.tex"), "Edith's edit saved")
    put_cursor(owner, find(owner, "Inline math"))
    owner.keyboard.type("Owner too. ")
    wait(lambda: edith.wait_for_timeout(100) or "Owner too." in text_of(edith), "the owner's edit at Edith")
    wait(lambda: rita.wait_for_timeout(100) or "Owner too." in text_of(rita), "the owner's edit at Rita")
    ok("co-editing: Edith and the owner see each other's typing; it is saved")

    # View: nothing changes, the server refuses writes and comments.
    before = text_of(vic)
    put_cursor(vic, 0)
    vic.keyboard.type("VVV")
    vic.wait_for_timeout(300)
    assert (
        text_of(vic) == before
        and vic.is_hidden("#commentBtn")
        and vic.is_hidden("#suggestBtn")
        and vic.is_disabled("#rebuildBtn")
    )
    assert api_status(vic, "PUT", "api/file?doc=DOC&path=main.tex", {"text": "x"}) == 403
    assert (
        api_status(
            vic,
            "POST",
            "api/review?doc=DOC",
            {"op": "comment", "path": "main.tex", "text": "x", "anchor": {"from": 0, "to": 1, "quote": "\\"}},
        )
        == 403
    )
    guests["Vic"][1].check("view refusals", allow=[r"HTTP 403 (PUT|POST) .*/api/(file|review)\?"])
    ok("view link: read-only editor, no comment or suggest buttons, rebuild disabled; the server answers 403 to writes")

    # The bell, empty: Edith has no notifications yet.
    edith.click("#bellBtn")
    edith.wait_for_selector("#bellList:not([hidden])")
    assert "Nothing yet" in edith.inner_text("#bellList")
    axe_ok(edith, ctx, "bell, empty")
    edith.keyboard.press("Escape")  # the focus is still on the bell: nothing in the list to focus
    edith.wait_for_selector("#bellList", state="hidden")

    # Review: Rita's typing becomes a suggestion; her comment @mentions Edith from the completion list.
    assert rita.get_attribute("#suggestBtn", "aria-checked") == "true" and rita.is_disabled("#suggestBtn")
    sug = suggest_at(rita, "fox jumps", " quickly")
    assert " quickly" not in text_of(owner) and " quickly" not in ctx["disk"]("main.tex")
    assert sug["name"] == "Rita", sug
    at = find(rita, "Inline math")
    put_cursor(rita, at, at + len("Inline math"))
    rita.click("#commentBtn")
    area = rita.locator(".rv-compose textarea")
    area.type("Please check @Edi")
    rita.wait_for_selector(".mention-list:not([hidden]) li:has-text('Edith')")
    axe_ok(rita, ctx, "@-mention list open")
    area.press("Enter")
    assert area.input_value() == "Please check @Edith ", area.input_value()
    area.type("this line.")
    area.press("Control+Enter")
    wait(
        lambda: (
            rita.wait_for_timeout(150)
            or any(
                c["text"].startswith("Please check @Edith") for t in review_data(rita)["threads"] for c in t["comments"]
            )
        ),
        "Rita's comment saved",
    )
    ok("review link: typing becomes a suggestion under Rita's name; the @-list completes Edith in a comment")
    edith.wait_for_function("() => document.querySelector('#bellBtn .count').textContent !== ''", timeout=20000)
    edith.click("#bellBtn")
    edith.wait_for_selector("#bellList:not([hidden]) .row")
    assert "Rita" in edith.inner_text("#bellList"), edith.inner_text("#bellList")
    axe_ok(edith, ctx, "bell, with a mention")
    edith.keyboard.press("Escape")
    ok("bell: empty at first, then lists Rita's @mention for Edith")

    # The suggestion card by role.
    rita_pos = find(rita, "fox jumps", len("fox jumps"))
    text = card_at(rita, rita_pos)
    assert "Rita" in text and rita.locator(".cm-sug-card .rv-time").count(), text
    assert card_buttons(rita) == ["Withdraw suggestion"], card_buttons(rita)
    axe_ok(rita, ctx, "suggestion card (Rita, her own)")
    rita.mouse.move(0, 0)
    assert api_status(rita, "POST", "api/review?doc=DOC", {"op": "accept", "ids": [sug["id"]]}) == 403
    guests["Rita"][1].check("reviewer accept refused", allow=[r"HTTP 403 POST .*/api/review\?"])
    open_file(owner, "main.tex")
    text = card_at(owner, find(owner, "fox jumps", len("fox jumps")))
    assert "Rita" in text, text
    assert set(card_buttons(owner)) == {"Accept suggestion", "Reject suggestion"}, card_buttons(owner)
    owner.locator(".cm-sug-card textarea").fill("@Rita fine by me")
    owner.click(".cm-sug-card button:has-text('Reply')")
    owner.wait_for_selector(".cm-sug-card:has-text('fine by me')")
    axe_ok(owner, ctx, "suggestion card with a reply (owner)")
    owner.mouse.move(0, 0)
    rita.wait_for_function("() => document.querySelector('#bellBtn .count').textContent !== ''", timeout=20000)
    rita.click("#bellBtn")
    rita.wait_for_selector("#bellList:not([hidden]) .row:has-text('fine by me')")
    rita.keyboard.press("Escape")
    vic_text = card_at(vic, find(vic, "fox jumps", len("fox jumps")))
    assert "fine by me" in vic_text and card_buttons(vic) == [] and not vic.locator(".cm-sug-card textarea").count()
    vic.mouse.move(0, 0)
    edith_text = card_at(edith, find(edith, "fox jumps", len("fox jumps")))
    assert "fine by me" in edith_text and set(card_buttons(edith)) == {"Accept suggestion", "Reject suggestion"}
    edith.click(".cm-sug-card button[aria-label='Accept suggestion']")
    for p in (owner, edith, rita):
        wait(lambda p=p: p.wait_for_timeout(100) or "fox jumps quickly" in text_of(p), "accepted everywhere")
    wait(lambda: owner.wait_for_timeout(200) or "jumps quickly" in ctx["disk"]("main.tex"), "accepted text saved")
    ok(
        "suggestion card: author, time and replies for every role; Rita may only withdraw, the owner and Edith "
        "accept or reject (Edith accepted), Vic only reads; the server refuses a reviewer's accept"
    )
    sug2 = suggest_at(rita, "Second}", " part", path="chapter.tex")
    card_at(rita, find(rita, "Second}", len("Second}")))
    rita.click(".cm-sug-card button[aria-label='Withdraw suggestion']")
    wait(
        lambda: rita.wait_for_timeout(150) or all(s["id"] != sug2["id"] for s in review_data(rita)["suggestions"]),
        "withdrawn",
    )
    rita.mouse.move(0, 0)
    sug3 = suggest_at(rita, "some words", " here", path="chapter.tex")
    open_file(owner, "chapter.tex")
    card_at(owner, find(owner, "some words", len("some words")))
    owner.click(".cm-sug-card button[aria-label='Reject suggestion']")
    wait(
        lambda: owner.wait_for_timeout(150) or all(s["id"] != sug3["id"] for s in review_data(owner)["suggestions"]),
        "rejected",
    )
    assert " here" not in ctx["disk"]("chapter.tex")
    owner.mouse.move(0, 0)
    ok("suggestions: Rita withdraws her own; the owner rejects another")

    # Comments on the PDF: Rita clicks a point, Edith drags over a region; each opens a comment on those lines.
    for gpage, gname, drag in ((rita, "Rita", False), (edith, "Edith", True)):
        gpage.wait_for_function("() => window.__app.docs[window.__app.cur].status !== 'building'", timeout=120000)
        open_file(gpage, "main.tex")
        x, y = pdf_point(gpage, 0.4, 0.2)
        palette_by_id(gpage, "pdfcomment")
        gpage.wait_for_selector("#viewer.pick")
        gpage.mouse.move(x, y)
        gpage.mouse.down()
        if drag:
            gpage.mouse.move(x + 40, y + 60, steps=5)
        gpage.mouse.up()
        gpage.wait_for_selector(".rv-compose textarea", timeout=20000)
        sel = gpage.evaluate(
            "() => { const s = window.__app.view.state.selection.main; "
            "return window.__app.view.state.sliceDoc(s.from, s.to); }"
        )
        assert sel.strip(), f"{gname}: no lines selected from the PDF"
        note = f"{gname} on the PDF"
        gpage.locator(".rv-compose textarea").fill(note)
        gpage.locator(".rv-compose textarea").press("Control+Enter")
        wait(
            lambda gpage=gpage, note=note: (
                gpage.wait_for_timeout(150)
                or any(c["text"] == note for t in review_data(gpage)["threads"] for c in t["comments"])
            ),
            f"{gname}'s PDF comment",
        )
        ok(
            f"PDF comment by {'drag' if drag else 'click'} ({gname}): opened on {len(sel.splitlines())} line(s) "
            "of main.tex, saved"
        )

    # Request access: Vic asks for review, the owner grants it, Vic's tab offers the new link.
    vic.click("#accessBtn")
    vic.wait_for_selector("dialog[open] #accRole")
    axe_ok(vic, ctx, "Request access dialog")
    vic.select_option("#accRole", "review")
    vic.fill("#accNote", "I would like to comment.")
    vic.click("dialog[open] button[type=submit]")
    vic.wait_for_function("() => document.getElementById('accessBtn').textContent === 'Access requested'")
    owner.wait_for_function(TOAST, arg="asks for more access", timeout=20000)
    palette_by_id(owner, "share")
    owner.wait_for_selector("#shareBody section:has-text('Access requests') button:has-text('Grant')")
    assert "I would like to comment." in owner.inner_text("#shareBody")
    axe_ok(owner, ctx, "Share dialog with an access request")
    owner.click("#shareBody section:has-text('Access requests') button:has-text('Grant')")
    owner.keyboard.press("Escape")
    vic.wait_for_selector("#toast a:has-text('Open your new link')", timeout=30000)
    new_link = vic.get_attribute("#toast a:has-text('Open your new link')", "href")
    vic.goto(new_link)
    vic.wait_for_function(LOADED, timeout=60000)
    assert "Reviewer" in vic.inner_text("#roleChip"), vic.inner_text("#roleChip")
    ok("request access: Vic asks for review, the owner grants it in Share, Vic's tab opens the new reviewer link")

    # Revoking Rita's link locks her tab at once; then sharing stops.
    palette_by_id(owner, "share")
    owner.click("button[aria-label='Revoke the link for Rita']")
    rita.wait_for_selector("#revoked", state="visible", timeout=20000)
    axe_ok(rita, ctx, "revoked tab")
    owner.click("#shareStop")
    owner.wait_for_selector("#shareStart")
    owner.keyboard.press("Escape")
    for n, (_, gw) in guests.items():
        # Rita's link was revoked. Once sharing stops the server drops the guests' connections, aborting what was
        # on its way.
        gw.check(
            f"{n}: sharing",
            allow=[r"ERR_ABORTED", r"WebSocket connection .* failed"]
            + ([r"HTTP 401 ", r"WebSocket connection"] if n == "Rita" else []),
        )
    for gpage, _ in guests.values():
        gpage.context.close()
    w.check("stop sharing", allow=[r"WebSocket connection .* failed"])  # the server closes every socket once
    ok("revoke: Rita's tab says the link no longer works at once; sharing stops")


def feature_layout(page, w, ctx):
    """Tooltips on hover (icons and palette rows), the PDF and source downloads, the sidebar's per-tab width and
    scrolling."""
    page.mouse.move(0, 300)
    page.hover("#toCursor")
    page.wait_for_selector("#tip:popover-open")
    assert "Show cursor in PDF" in page.inner_text("#tip"), page.inner_text("#tip")
    assert page.get_attribute("#toCursor", "title") is None, "the native tooltip would double ours"
    page.mouse.move(0, 300)
    page.wait_for_function("() => !document.getElementById('tip').matches(':popover-open')")
    assert "Show cursor in PDF" in (page.get_attribute("#toCursor", "title") or ""), "title not given back"
    page.click("#moreBtn")
    page.wait_for_selector("#palette[open]")
    page.hover("#pal-cursor")
    page.wait_for_selector("#tip:popover-open")
    assert "Scroll the PDF" in page.inner_text("#tip"), page.inner_text("#tip")
    page.keyboard.press("Escape")
    page.keyboard.press("Escape")
    ok("tooltips: icon buttons and palette rows show theirs on hover, over the dialog too")
    with page.expect_download() as got:
        page.click("#pdfDlBtn")
    assert got.value.suggested_filename.endswith(".pdf"), got.value.suggested_filename
    assert Path(got.value.path()).read_bytes()[:5] == b"%PDF-"
    pdf_name = got.value.suggested_filename
    with page.expect_download() as got:
        palette(page, "Download source (zip)")
    with zipfile.ZipFile(got.value.path()) as z:
        names = z.namelist()
    assert {"main.tex", "chapter.tex"} <= set(names), names
    ok(f"downloads: the PDF ({pdf_name}) and the source zip ({len(names)} files)")
    side_tab(page, "files")
    width = lambda: page.evaluate("() => document.getElementById('side').offsetWidth")  # noqa: E731
    start = width()
    box = page.locator("#sideResize").bounding_box()
    page.mouse.move(box["x"] + 3, box["y"] + 200)
    page.mouse.down()
    page.mouse.move(box["x"] + 153, box["y"] + 200, steps=5)
    page.mouse.up()
    dragged = width()
    assert abs(dragged - (start + 150)) <= 8, (start, dragged)
    side_tab(page, "refs")
    assert width() != dragged, "each tab keeps its own width"
    side_tab(page, "files")
    assert width() == dragged, (width(), dragged)
    page.dblclick("#sideResize")
    assert width() == start, (width(), start)
    page.focus("#sideResize")
    page.keyboard.press("ArrowRight")
    assert width() == start + 20, (width(), start)
    page.dblclick("#sideResize")
    side_tab(page, "symbols")
    page.wait_for_selector("#panel-symbols .sym-grid")
    scroll = page.evaluate(
        "() => { const p = document.getElementById('panel-symbols'); return [p.scrollHeight > p.clientHeight,"
        " getComputedStyle(p).overflowY]; }"
    )
    assert scroll == [True, "auto"], scroll
    side_tab(page, "files")
    ok(f"sidebar: dragged {start}px to {dragged}px for Files only, reset by double-click, arrow keys, panels scroll")
    open_file(page, "main.tex")
    put_cursor(page, 0)
    page.click("#visualBtn")
    page.wait_for_selector(".v-table-box .v-table td")
    cells = page.evaluate("() => [...document.querySelectorAll('.v-table td')].map((td) => td.textContent)")
    assert cells[:4] == ["alpha", "beta", "gamma", "delta"], cells
    axe_ok(page, ctx, "visual mode with a table")
    page.click(".v-table-box")
    page.wait_for_function("() => !document.querySelector('.v-table-box')")
    assert "\\begin{tabular}" in page.inner_text(".cm-content")
    page.click("#visualBtn")
    ok("visual mode: the tabular is drawn as a table; a click shows its source again")
    for phone in (320, 360):
        page.set_viewport_size({"width": phone, "height": 800})
        page.wait_for_timeout(300)
        wide = page.evaluate("() => [document.documentElement.scrollWidth, innerWidth]")
        assert wide[0] <= wide[1], f"page wider than a {phone}px phone: {wide}"
    page.set_viewport_size({"width": 1400, "height": 900})
    ok("phone: the page is never wider than a 320 or 360 px screen")
    w.check("layout")


FEATURES = [
    ("layout", feature_layout),
    ("panels", feature_panels),
    ("hovers", feature_hovers),
    ("edittools", feature_edit_tools),
    ("marks", feature_marks_zen),
    ("history", feature_history),
    ("reviewpdf", feature_review_pdf),
    ("lock", feature_lock),
    ("linked", feature_linked),
    ("folder", feature_folder_upload),
    ("recovery", feature_recovery),
    ("ai", feature_ai),
    ("grammar", feature_grammar),
    ("zotero", feature_zotero_library),
    ("sharing", feature_sharing),
]


# ---- hosted: host.py and every page of host_ui ------------------------------------------------------------------


def hosted(args, browser) -> None:
    sys.path.insert(0, str(ROOT / "scripts"))
    import host  # noqa: E402  (only for totp_code: the test plays the authenticator app)

    port = free_port()
    origin = f"http://127.0.0.1:{port}"
    with tempfile.TemporaryDirectory() as tmp:
        data = Path(tmp) / "data"
        env = {
            **os.environ,
            "LP_ADMIN_EMAIL": "root@example.org",
            "LP_ADMIN_PASSWORD": PASSWORD,
            "LP_ADMIN_NAME": "Root",
        }
        subprocess.run(
            [sys.executable, str(ROOT / "scripts/host.py"), "init", "--data", str(data)],
            env=env,
            check=True,
            stdout=subprocess.DEVNULL,
        )
        (data / "config.toml").write_text(
            f'public_url = "{origin}"\nport = {port}\n[api]\nenabled = true\n[mcp]\nenabled = true\n'
        )
        command = [sys.executable, str(ROOT / "scripts/host.py"), "serve", "--data", str(data)]
        if args.insecure_no_sandbox:
            command.append("--insecure-no-sandbox")
        server = subprocess.Popen(command, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)
        log: list[str] = []
        threading.Thread(target=lambda: [log.append(line) for line in server.stdout], daemon=True).start()
        try:
            wait(lambda: socket.socket().connect_ex(("127.0.0.1", port)) == 0, "host.py listens", 30)
            run_hosted(args, browser, origin, host)
        except BaseException:
            print("".join(log[-60:]))
            raise
        finally:
            server.terminate()
            server.wait(30)


def host_page(browser, origin, name, **kw):
    ctx = browser.new_context(base_url=origin, bypass_csp=True, **kw)  # bypass_csp: only so axe can be injected
    page = ctx.new_page()
    w = Watch(page, name)
    w.answers = []
    page.on("dialog", lambda d: d.accept(w.answers.pop(0)) if d.type == "prompt" and w.answers else d.accept())
    return page, w


def host_axe(page, args, label):
    page.evaluate("() => document.querySelectorAll('details').forEach((d) => d.open = true)")
    axe(page, label, args.axe)
    page.evaluate("() => document.querySelectorAll('details').forEach((d) => d.open = false)")
    ok(f"axe: 0 violations, {label} (light and dark)")


def host_sign_in(page, email, password=PASSWORD, code=None):
    page.goto("/#login")
    page.get_by_label("Email").fill(email)
    page.get_by_label("Password").fill(password)
    page.get_by_role("button", name="Sign in").click()
    if code:
        expect(page.get_by_role("heading", name="Two-step sign-in")).to_be_visible()
        page.get_by_label("Code or recovery code").fill(code())
        page.get_by_role("button", name="Continue").click()
    expect(page.get_by_role("heading", name="Projects")).to_be_visible()


def saved(form_locator, text="Saved."):
    expect(form_locator.locator(".msg.ok")).to_contain_text(text)


def run_hosted(args, browser, origin, host):
    admin, aw = host_page(browser, origin, "site admin")
    admin.goto("/")
    expect(admin.get_by_role("heading", name="Sign in")).to_be_visible()
    pressed = admin.get_attribute("#theme", "aria-pressed")
    admin.click("#theme")
    assert admin.get_attribute("#theme", "aria-pressed") != pressed
    assert admin.evaluate("() => document.documentElement.dataset.theme") in ("light", "dark")
    admin.click("#theme")
    ok("theme button switches the theme and says so (aria-pressed)")
    host_sign_in(admin, "root@example.org")
    admin.goto("/#admin")
    expect(admin.get_by_role("heading", name="Site admin")).to_be_visible()
    host_axe(admin, args, "site admin, every section")
    for sid in ("signup", "workspaces", "users", "quotas", "providers", "ai", "audit"):
        admin.click(f".side a[href='#admin/{sid}']")
        admin.wait_for_function("id => document.activeElement === document.querySelector(`#s-${id} h2`)", arg=sid)
        assert admin.get_attribute(f".side a[href='#admin/{sid}']", "aria-current") == "true"
    ok("site admin: each section link scrolls to its heading and marks itself current")
    signup = admin.locator("#s-signup form")
    signup.get_by_text("Open to everyone").click()
    signup.get_by_role("button", name="Save sign-up").click()
    saved(signup)
    quotas = admin.locator("#s-quotas form")
    quotas.get_by_label("Projects per workspace", exact=True).fill("25")
    quotas.get_by_role("button", name="Save limits").click()
    saved(quotas)
    providers = admin.locator("#s-providers form")
    providers.get_by_role("button", name="Save").click()
    saved(providers)
    admin.goto("/#admin")
    expect(admin.locator("#s-quotas").get_by_label("Projects per workspace", exact=True)).to_have_value("25")
    assert "settings" in admin.inner_text("#s-audit").lower() or admin.locator("#s-audit tbody tr").count() > 0
    ok("site admin: sign-up opened, a limit and the provider setting saved and shown again, the audit log lists events")
    for name in ("Lab", "Temp"):
        admin.goto("/#admin")
        admin.get_by_text("New workspace").click()
        admin.locator("#s-workspaces").get_by_label("Name").fill(name)
        admin.get_by_role("button", name="Create workspace").click()
        expect(admin.get_by_role("heading", name=name)).to_be_visible()
        if name == "Lab":
            lab = admin.url.split("#t/")[1]
    admin.goto("/#admin")
    aw.answers.append("Temp")
    admin.get_by_role("button", name="Delete Temp").click()
    expect(admin.get_by_role("button", name="Delete Temp")).to_have_count(0)
    ok("site admin: workspaces made from the admin page; one deleted after typing its name")

    # Open sign-up: a new person makes an account (and a personal workspace).
    sam, sw = host_page(browser, origin, "Sam")
    sam.goto("/#signup")
    expect(sam.get_by_role("heading", name="Create an account")).to_be_visible()
    host_axe(sam, args, "sign-up page (open)")
    sam.get_by_label("Name").fill("Sam Signup")
    sam.get_by_label("Email").fill("sam@example.org")
    sam.get_by_label("Password").fill(PASSWORD)
    sam.get_by_role("button", name="Create account").click()
    expect(sam.get_by_role("heading", name="Projects")).to_be_visible()
    ok("open sign-up: Sam makes an account and lands on the projects page")

    # Members page: invites (one revoked), Sam joins through one while signed in.
    admin.goto(f"/#t/{lab}")
    expect(admin.get_by_role("heading", name="Lab")).to_be_visible()
    links = []
    for role in ("viewer", "editor", "admin"):
        admin.locator("form select[name=role]").select_option(role)
        admin.get_by_role("button", name="Create invite link").click()
        admin.wait_for_function(
            "seen => { const f = document.querySelector('input[name=link]'); return f && !seen.includes(f.value); }",
            arg=links,
        )
        links.append(admin.get_by_label("Invite link").input_value())
    expect(admin.locator("li:has-text('expires')")).to_have_count(3)
    host_axe(admin, args, "workspace members page with open invites")
    admin.get_by_role("button", name="Revoke invite for editor").click()
    expect(admin.locator("li:has-text('expires')")).to_have_count(2)
    admin.goto("/" + links[2].split("/", 3)[3])  # the site admin joins Lab too, to make projects in it
    admin.get_by_role("button", name="Join workspace").click()
    expect(admin.get_by_role("heading", name="Projects")).to_be_visible()
    sam.goto("/" + links[1].split("/", 3)[3] if links[1].startswith("http") else links[1])
    expect(sam.get_by_role("heading", name="Invitation")).to_be_visible()
    expect(sam.get_by_role("alert")).to_be_visible()  # the revoked link no longer works
    host_axe(sam, args, "error page (a revoked invite)")
    sam.goto("/" + links[0].split("/", 3)[3])
    sam.get_by_role("button", name="Join workspace").click()
    expect(sam.get_by_role("heading", name="Projects")).to_be_visible()
    expect(sam.get_by_text("Viewer · workspace")).to_be_visible()
    sw.check("invites", allow=[r"HTTP (404|410) GET .*/api/invites/"])
    ok(
        "members page: invite links made, one revoked (its page says so); Sam joins Lab as a viewer while signed in, "
        "the site admin as an admin"
    )

    # Projects: New project and Upload a zip dialogs, delete.
    admin.goto("/#home")
    lab_card = admin.locator("section.workspace[aria-label='Lab']")
    lab_card.get_by_role("button", name="Upload a zip").click()
    dlg = admin.locator("dialog[open]")
    expect(dlg.get_by_role("heading", name="Upload a zip")).to_be_visible()
    host_axe(admin, args, "Upload a zip dialog")
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as z:
        z.writestr("main.tex", OTHER)
    dlg.get_by_label("Name for the upload").fill("Uploaded")
    dlg.get_by_label("Zip file").set_input_files(
        {"name": "up.zip", "mimeType": "application/zip", "buffer": buf.getvalue()}
    )
    dlg.get_by_role("button", name="Upload").click()
    expect(lab_card.get_by_role("heading", name="Uploaded")).to_be_visible()
    lab_card.get_by_role("button", name="Delete Uploaded").click()
    expect(admin.locator("section.workspace[aria-label='Lab']").get_by_role("heading", name="Uploaded")).to_have_count(
        0
    )
    ok("projects: a zip uploaded through its dialog becomes a project; Delete removes it")
    lab_card = admin.locator("section.workspace[aria-label='Lab']")
    lab_card.get_by_role("button", name="New project").click()
    dlg = admin.locator("dialog[open]")
    host_axe(admin, args, "New project dialog")
    dlg.get_by_label("Project name").fill("Paper")
    dlg.get_by_role("button", name="Create").click()
    admin.wait_for_url(re.compile(r"/p/[0-9a-f]{16}/$"))
    project = admin.url
    admin.wait_for_function(LOADED, timeout=60000)
    ok("projects: New project from a template opens it in the editor")

    # Request access from the editor; the workspace admin grants it on the members page.
    sam.goto(project)
    sam.wait_for_function(LOADED, timeout=60000)
    expect(sam.locator("#roleChip")).to_contain_text("View only")
    sam.click("#accessBtn")
    sam.select_option("#accRole", "edit")
    sam.fill("#accNote", "May I edit?")
    sam.click("dialog[open] button[type=submit]")
    sam.wait_for_function("() => document.getElementById('accessBtn').textContent === 'Access requested'")
    admin.goto(f"/#t/{lab}")
    req = admin.locator("section[aria-label='Access requests']")
    expect(req).to_contain_text("May I edit?")
    host_axe(admin, args, "members page with an access request")
    req.get_by_role("button", name="Make Sam Signup editor").click()
    expect(admin.get_by_label("Role of Sam Signup")).to_have_value("editor")
    sam.reload()
    sam.wait_for_function(LOADED, timeout=60000)
    expect(sam.locator("#roleChip")).to_contain_text("Can edit")
    sam.wait_for_timeout(3000)
    sw.check("Sam as an editor", allow=[r"HTTP (404|410) GET .*/api/invites/", r"/api/send\?.*ERR_ABORTED"])
    admin.get_by_label("Role of Sam Signup").select_option("reviewer")
    admin.wait_for_function("() => /is now reviewer/.test(document.body.textContent)")
    admin.reload()
    expect(admin.get_by_label("Role of Sam Signup")).to_have_value("reviewer")
    ok("request access: Sam (viewer) asks in the editor, the admin grants it; a role select changes the role")

    # A member signs up through an invite, is disabled and enabled by the site admin, then removed.
    admin.locator("form select[name=role]").select_option("viewer")
    admin.get_by_role("button", name="Create invite link").click()
    link = admin.get_by_label("Invite link").input_value()
    rob, rw = host_page(browser, origin, "Rob")
    rob.goto("/" + link.split("/", 3)[3])
    form = rob.locator("form").first
    form.get_by_label("Name").fill("Rob Removed")
    form.get_by_label("Email").fill("rob@example.org")
    form.get_by_label("Password").fill(PASSWORD)
    form.get_by_role("button", name="Create account").click()
    expect(rob.get_by_role("heading", name="Projects")).to_be_visible()
    admin.goto("/#admin/users")
    admin.get_by_role("button", name="Disable rob@example.org").click()
    expect(admin.get_by_role("button", name="Enable rob@example.org")).to_be_visible()
    assert rob.request.get(origin + "/api/me").status in (401, 403) or not rob.request.get(
        origin + "/api/me"
    ).json().get("user")
    admin.get_by_role("button", name="Enable rob@example.org").click()
    expect(admin.get_by_role("button", name="Disable rob@example.org")).to_be_visible()
    admin.get_by_role("button", name="Make site admin: rob@example.org").click()
    expect(admin.get_by_role("button", name="Remove site admin from rob@example.org")).to_be_visible()
    admin.get_by_role("button", name="Remove site admin from rob@example.org").click()
    expect(admin.get_by_role("button", name="Make site admin: rob@example.org")).to_be_visible()
    admin.goto(f"/#t/{lab}")
    admin.get_by_role("button", name="Remove Rob Removed").click()
    expect(admin.get_by_role("button", name="Remove Rob Removed")).to_have_count(0)
    rw.allow.append(r"HTTP 401 ")
    rob.context.close()
    ok("users: disable ends Rob's session, enable, make and remove site admin; Remove takes him out of Lab")

    # Account page: profile, password, Git token, API token, AI clients, two-step on and off.
    sam.goto("/#account")
    expect(sam.get_by_role("heading", name="Account")).to_be_visible()
    expect(sam.get_by_role("region", name="AI clients")).to_contain_text("No AI client is connected.")
    host_axe(sam, args, "account page")
    prof = sam.locator("section[aria-label='Profile'] form")
    prof.get_by_label("Name").fill("Sam Renamed")
    prof.get_by_role("button", name="Save").click()
    saved(prof)
    pw = sam.locator("section[aria-label='Password'] form")
    pw.get_by_label("Current password").fill(PASSWORD)
    pw.get_by_label("New password").fill(PASSWORD + "-new")
    pw.get_by_role("button", name="Change password").click()
    saved(pw, "Password saved.")
    git = sam.locator("section[aria-label='Git'] form")
    git.get_by_label("User name on the Git host").fill("sam")
    git.get_by_label("Access token").fill("ghp_notarealtoken0123456789")
    git.get_by_role("button", name="Save Git token").click()
    saved(git, "Saved. Pull and Push")
    assert "notarealtoken" not in sam.content()
    sam.reload()
    sam.get_by_role("button", name="Remove the stored token").click()
    expect(sam.get_by_role("button", name="Remove the stored token")).to_have_count(0)
    tok = sam.locator("section[aria-label='API tokens'] form")
    tok.get_by_label("Token name").fill("ui e2e")
    tok.get_by_label(re.compile(r"^Paper \(")).check()
    tok.get_by_role("button", name="Create token").click()
    expect(sam.get_by_text("Copy the token now")).to_be_visible()
    sam.get_by_role("button", name="Done").click()
    sam.get_by_role("button", name="Revoke token ui e2e").click()
    expect(sam.get_by_role("button", name="Revoke token ui e2e")).to_have_count(0)
    ok(
        "account: name saved, password changed, Git token saved (never shown) and removed, an API token made "
        "and revoked"
    )
    two = sam.locator("section[aria-label='Two-step sign-in']")
    two.get_by_role("button", name="Set up two-step sign-in").click()
    secret = two.locator("code").inner_text().replace(" ", "")
    code = lambda: host.totp_code(secret, int(time.time() // 30))  # noqa: E731
    two.get_by_label("Code from the app").fill(code())
    two.get_by_role("button", name="Turn on").click()
    expect(two.get_by_text("Two-step sign-in is on.")).to_be_visible()
    two.get_by_role("button", name="I saved them").click()
    sam.click("#logout")
    expect(sam.get_by_role("heading", name="Sign in")).to_be_visible()
    time.sleep(31 - time.time() % 30)  # the code that turned it on was used; wait for the next one
    sam.get_by_label("Email").fill("sam@example.org")
    sam.get_by_label("Password").fill(PASSWORD + "-new")
    sam.get_by_role("button", name="Sign in").click()
    expect(sam.get_by_role("heading", name="Two-step sign-in")).to_be_visible()
    host_axe(sam, args, "two-step code page")
    sam.get_by_label("Code or recovery code").fill(code())
    sam.get_by_role("button", name="Continue").click()
    expect(sam.get_by_role("heading", name="Projects")).to_be_visible()
    sam.goto("/#account")
    two = sam.locator("section[aria-label='Two-step sign-in']")
    two.get_by_text("Turn off two-step sign-in").click()
    off = two.locator("details[open] form")
    off.get_by_label("Current password").fill(PASSWORD + "-new")
    time.sleep(31 - time.time() % 30)
    off.get_by_label("Code or recovery code").fill(code())
    off.get_by_role("button", name="Turn off").click()
    expect(two.get_by_role("button", name="Set up two-step sign-in")).to_be_visible()
    ok("two-step: turned on, the next sign-in asks for a code (page passes axe), turned off with a code")

    # Password reset link from the site admin; the reset page sets a new password.
    admin.goto("/#admin/users")
    admin.get_by_role("button", name="Password reset link for sam@example.org").click()
    reset_link = admin.get_by_label("Reset link", exact=True).input_value()
    anon, nw = host_page(browser, origin, "reset")
    anon.goto("/" + reset_link.split("/", 3)[3])
    expect(anon.get_by_role("heading", name="Choose a new password")).to_be_visible()
    host_axe(anon, args, "password reset page")
    anon.get_by_label("New password").fill(PASSWORD + "-reset")
    anon.get_by_role("button", name="Save password").click()
    expect(anon.get_by_role("heading", name="Sign in")).to_be_visible()
    host_sign_in(anon, "sam@example.org", PASSWORD + "-reset")
    anon.goto("/#invite=not-a-real-invite")
    expect(anon.get_by_role("heading", name="Invitation")).to_be_visible()
    host_axe(anon, args, "error page (unknown invite)")
    nw.check("reset", allow=[r"HTTP (404|410) GET .*/api/invites/"])
    anon.context.close()
    ok("password reset: the admin's one-time link opens the reset page; the new password signs in")
    # Sam's editor tab stayed open while the admin made him a reviewer (its grammar check is then refused), and
    # leaving it for the account page cut a message on its way.
    sw.check(
        "Sam",
        allow=[r"HTTP (404|410) GET .*/api/invites/", r"HTTP 403 POST .*/api/grammar\?", r"/api/send\?.*ERR_ABORTED"],
    )
    aw.check("site admin")
    sam.context.close()
    admin.context.close()


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--part", choices=("local", "hosted", "all"), default="all")
    parser.add_argument("--chromium", default=None)
    parser.add_argument("--axe", default=AXE)
    parser.add_argument("--insecure-no-sandbox", action="store_true")
    parser.add_argument("--headed", action="store_true")
    args = parser.parse_args()
    with sync_playwright() as pw:
        browser = pw.chromium.launch(executable_path=args.chromium, headless=not args.headed)
        if args.part in ("local", "all"):
            local(args, browser)
        if args.part in ("hosted", "all"):
            hosted(args, browser)
        browser.close()
    print(f"\nui_e2e: all {len(CHECKS)} checks passed.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
