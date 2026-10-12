"""
Browser check of the editor's navigation features (serve_ui/nav.js, navpanels.js) on a throwaway document: label
numbers, go to definition, find references, rename, outline extras, TODOs, bookmarks and cursor sessions, symbol
palette, file graph, math preview, typeset selection, presentation mode. Then axe-core (light and dark) on the
editor's tool dialogs: snippets, the figure dialog of a pasted image, the table editor, the TikZ canvas and the Git
panel (the document folder is a throwaway repository). Needs LaTeX, git and Chromium:

    uv run --no-project --with playwright==1.56.0 python tests/nav_e2e.py [--chromium PATH] [--axe PATH_OR_URL]
"""
from __future__ import annotations

import argparse
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
LINE = "window.__app.view.state.doc.lineAt(window.__app.view.state.selection.main.head).number"  # the cursor's line
MARK = ".cm-navMarks .cm-gutterElement:not([style*=visibility]) .cm-navMark"  # not the hidden spacer
MAIN = r"""\documentclass{article}
\usepackage{amsmath}
\usepackage{graphicx}
\newcommand{\R}{\mathbb{R}}
\begin{document}
\section{Intro}\label{sec:intro}
See Section~\ref{sec:intro} and Equation~\eqref{eq:one}. % TODO cite more
\begin{equation}
  E = mc^2 \label{eq:one}
\end{equation}
\input{chap}
\end{document}
"""
CHAP = r"""\section{More}
Back to \ref{sec:intro}.
\begin{figure}\centering\fbox{x}\caption{A box}\label{fig:box}\end{figure}
"""
EXTRA = r"""\begin{tabular}{lc}
a & b \\
c & d \\
\end{tabular}

\begin{tikzpicture}
\node (a) at (0,0) {A};
\node (b) at (2,0) {B};
\draw[->] (a) -- (b);
\end{tikzpicture}
"""
PNG = "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mNk+M9QDwADhgGAWjR9awAAAABJRU5ErkJggg=="  # 1x1


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
    doc = Path(tmp.name) / "demo"
    doc.mkdir()
    (doc / "main.tex").write_text(MAIN, encoding="utf-8")
    (doc / "chap.tex").write_text(CHAP, encoding="utf-8")
    (doc / "extra.tex").write_text(EXTRA, encoding="utf-8")  # not built: only for the table and TikZ editors
    git = ["git", "-C", str(doc), "-c", "user.name=E2E", "-c", "user.email=e2e@example.org"]
    subprocess.run([*git, "init", "-q"], check=True)
    subprocess.run([*git, "add", "main.tex"], check=True)
    subprocess.run([*git, "commit", "-q", "-m", "first"], check=True)
    port = free_port()
    server = subprocess.Popen([sys.executable, str(ROOT / "scripts/serve.py"), "demo", "--source", tmp.name,
                               "--no-open", "--port", str(port)], stdout=subprocess.DEVNULL, stderr=subprocess.STDOUT)
    origin = f"http://127.0.0.1:{port}"
    errors = []
    try:
        for _ in range(100):
            try:
                urllib.request.urlopen(origin + "/api/health", timeout=1)
                break
            except OSError:
                time.sleep(0.1)
        with sync_playwright() as pw:
            browser = pw.chromium.launch(executable_path=args.chromium)
            page = browser.new_context(bypass_csp=True, viewport={"width": 1400, "height": 900}).new_page()
            page.on("pageerror", lambda e: errors.append(str(e)))
            page.goto(f"{origin}/#demo")
            page.wait_for_function(LOADED, timeout=60000)
            page.wait_for_function("() => window.__app.pdfView.pdf", timeout=180000)

            # Label numbers from the last build, next to \label and \ref.
            page.wait_for_function("() => document.querySelectorAll('.cm-navNum').length >= 3", timeout=30000)
            nums = page.eval_on_selector_all(".cm-navNum", "els => els.map(e => e.textContent)")
            print("label numbers:", nums)
            assert "1" in nums, nums

            # F12 on \ref{sec:intro} (line 7) goes to its \label on line 6.
            def cursor_at(line: int, col: int) -> None:
                page.evaluate(f"""() => {{ const v = window.__app.view, l = v.state.doc.line({line});
                    v.dispatch({{ selection: {{ anchor: l.from + {col} }} }}); v.focus(); }}""")
            cursor_at(7, 20)
            page.keyboard.press("F12")
            page.wait_for_function(f"() => {LINE} === 6")
            print("F12: jumped to the \\label")

            # Shift+F12 lists the definition and both uses (one in chap.tex).
            cursor_at(7, 20)
            page.keyboard.press("Shift+F12")
            page.wait_for_selector("#navDlg[open] .nav-hit")
            hits = page.eval_on_selector_all("#navDlg .nav-hit", "els => els.map(e => e.textContent)")
            print("references:", hits)
            assert len(hits) == 3 and any("chap.tex:2" in h for h in hits), hits
            page.keyboard.press("Escape")

            # Outline: figures with their caption, labels, TODOs; the filter narrows them.
            page.evaluate("() => document.getElementById('tab-outline').click()")
            page.wait_for_selector("#outlineExtra .nav-group")
            extra = page.inner_text("#outlineExtra")
            assert "A box" in extra and "sec:intro" in extra and "cite more" in extra, extra
            page.fill("#panel-outline .nav-filter", "box")
            assert "sec:intro" not in page.inner_text("#outlineExtra")
            page.fill("#panel-outline .nav-filter", "")
            print("outline extras: ok")

            # TODO drawer.
            page.evaluate("() => document.getElementById('dtab-todos').click()")
            page.wait_for_selector("#todoList .nav-row")
            assert "cite more" in page.inner_text("#todoList")
            print("TODO panel: ok")

            # Bookmarks: toggle on a line, listed, kept in this browser.
            cursor_at(4, 0)
            page.keyboard.press("Control+Alt+K")
            page.wait_for_selector(MARK)
            page.wait_for_timeout(600)
            page.evaluate("() => document.getElementById('dtab-bookmarks').click()")
            assert "main.tex:4" in page.inner_text("#bookmarkList")
            print("bookmarks: ok")

            # Symbol palette inserts at the cursor.
            cursor_at(1, 0)
            page.evaluate("() => document.getElementById('tab-symbols').click()")
            page.fill("#panel-symbols .nav-filter", "alpha")
            page.click("#panel-symbols .sym")
            assert page.evaluate("() => window.__app.view.state.doc.line(1).text").startswith("\\alpha")
            page.keyboard.press("Control+z")
            print("symbol palette: ok")

            # File graph.
            page.evaluate("() => window.__app.navUi.graph()")
            page.wait_for_selector("#navDlg[open] .nav-graph svg")
            labels = page.eval_on_selector_all(".nav-node title", "els => els.map(e => e.textContent)")
            assert "chap.tex" in labels, labels
            page.keyboard.press("Escape")
            print("file graph:", labels)

            # Math preview under the equation (source mode).
            page.evaluate("() => { window.__app.settings.mathPreview = true; window.__app.applyConfig(); }")
            page.wait_for_selector(".cm-navMath .katex", timeout=20000)
            print("math preview: ok")

            # Typeset selection: the equation (amsmath from the preamble), shown cropped.
            page.evaluate("""() => { const v = window.__app.view, d = v.state.doc;
                v.dispatch({ selection: { anchor: d.line(8).from, head: d.line(10).to } }); }""")
            page.evaluate("() => window.__app.nav.run.typesetSelection(window.__app.view)")
            page.wait_for_selector("#navDlg[open] .nav-typeset canvas", timeout=60000)
            print("typeset selection: ok")
            page.keyboard.press("Escape")

            # Presentation mode.
            page.click("#presentBtn")
            page.wait_for_selector(".nav-present canvas")
            page.wait_for_function("() => /1 \\/ \\d/.test(document.querySelector('.nav-present-n').textContent)")
            page.keyboard.press("Escape")
            page.wait_for_function("() => !document.querySelector('.nav-present')")
            print("presentation: ok")

            # The PDF follows the cursor once that is on (SyncTeX forward search on a pause).
            page.evaluate("() => { window.__app.settings.follow = true; }")
            cursor_at(9, 2)
            page.wait_for_selector("#pages .hit", state="attached", timeout=15000)
            print("follow cursor: ok")

            # Rename the label everywhere (the prompt answers sec:start).
            page.on("dialog", lambda d: d.accept("sec:start"))
            cursor_at(6, 22)
            page.keyboard.press("F2")
            page.wait_for_function("() => /Renamed/.test(document.getElementById('toast').textContent)", timeout=20000)
            page.wait_for_timeout(1500)
            chap = (doc / "chap.tex").read_text(encoding="utf-8")
            text = page.evaluate("() => window.__app.view.state.doc.toString()")
            assert "\\ref{sec:start}" in chap, chap
            assert "\\label{sec:start}" in text and "\\ref{sec:start}" in text, text
            print("rename: ok")

            # Cursor session: a reload puts the cursor back where it was.
            cursor_at(9, 3)
            page.wait_for_timeout(700)
            page.reload()
            page.wait_for_function(LOADED, timeout=60000)
            line = page.evaluate(f"() => {LINE}")
            assert line == 9, line
            assert page.wait_for_selector(MARK), "bookmark lost on reload"
            print("session restore: ok")

            def axe(label: str) -> None:
                for theme in ("light", "dark"):
                    page.evaluate("t => document.documentElement.dataset.theme = t", theme)
                    if not page.evaluate("() => !!window.axe"):
                        if args.axe.startswith("http"):
                            page.add_script_tag(url=args.axe)
                        else:
                            page.add_script_tag(path=args.axe)
                    result = page.evaluate("() => axe.run(document, {resultTypes: ['violations']})")
                    bad = [f"{v['id']}: {v['help']} ({v['nodes'][0]['target']})" for v in result["violations"]]
                    assert not bad, f"axe ({label}, {theme}): {bad}"
                print(f"axe: 0 violations, {label} (light and dark)")

            # Snippets, from the Settings dialog.
            page.keyboard.press("Control+,")
            page.wait_for_selector("#settings[open]")
            page.click("#sEditTools summary")
            page.click("#settings button:has-text('Edit snippets...')")
            page.wait_for_selector("#snippetsDlg[open] .et-snip")
            axe("snippets dialog")
            page.click("#snippetsDlg button:has-text('Done')")
            page.evaluate("() => document.getElementById('settings').close()")

            # A pasted image asks for its name, folder, caption... first.
            page.evaluate("""png => {
                const b = Uint8Array.from(atob(png), (c) => c.charCodeAt(0)), d = new DataTransfer();
                d.items.add(new File([b], "dot.png", { type: "image/png" }));
                document.querySelector(".cm-content").dispatchEvent(new ClipboardEvent("paste",
                  { clipboardData: d, bubbles: true, cancelable: true })); }""", PNG)
            page.wait_for_selector("dialog.fig-dlg[open]")
            axe("figure dialog")
            page.keyboard.press("Escape")
            page.wait_for_selector("dialog.fig-dlg[open]", state="detached")

            # Table editor and TikZ canvas, from the status-bar chips.
            page.evaluate("() => window.__app.openFile('extra.tex', 0)")
            page.wait_for_function("() => window.__app.active.path === 'extra.tex'")
            cursor_at(2, 1)
            page.click("#editorStatus button:has-text('Edit table')")
            page.wait_for_selector("dialog.tbl-dlg[open]")
            axe("table editor")
            page.keyboard.press("Escape")
            cursor_at(7, 1)
            page.click("#editorStatus button:has-text('Edit picture')")
            page.wait_for_selector("dialog.tz-dlg[open] svg")
            axe("TikZ canvas")
            page.keyboard.press("Escape")

            # Git panel (the owner's repository).
            page.evaluate("() => document.getElementById('tab-git').click()")
            page.wait_for_function("() => /first/.test(document.getElementById('panel-git').textContent)",
                                   timeout=20000)
            axe("Git panel")
            browser.close()
        assert not errors, errors
        print("nav_e2e: ok")
        return 0
    finally:
        server.terminate()
        server.wait(10)
        tmp.cleanup()


if __name__ == "__main__":
    raise SystemExit(main())
