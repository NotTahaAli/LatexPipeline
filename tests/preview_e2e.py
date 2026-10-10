"""
Live preview in a real browser (not part of `unittest discover`):

    uv run --no-project --with playwright==1.56.0 python tests/preview_e2e.py [--doc sample-report --source bench]
        [--file Chapters/chapter3/subsection3.tex] [--runs 5] [--chromium PATH] [--axe PATH_OR_URL]

Starts serve.py on a free port, opens the chapter file, types a word five times with a pause, and measures
keystroke -> preview pages spliced into the PDF view (which includes the 700 ms idle wait). Checks that the new
word is in the spliced pages, that the scroll position did not move, that a newer keystroke aborts the request in
flight, and axe-core reports no violations (light and dark). The edited file is restored afterwards. Needs LaTeX
and a full build of the document (python scripts/build.py --source bench sample-report).
"""

from __future__ import annotations

import argparse
import json
import socket
import statistics
import subprocess
import sys
import time
import urllib.request
from pathlib import Path

from playwright.sync_api import sync_playwright

ROOT = Path(__file__).resolve().parent.parent
AXE = "https://cdnjs.cloudflare.com/ajax/libs/axe-core/4.10.2/axe.min.js"
LOADED = "() => window.__app && window.__app.active && window.__app.view.state.doc.length > 0"
READY = "() => { const d = window.__app.docs[window.__app.cur]; return d && d.version && window.__app.pdfView.pdf; }"
HOOK = """() => { const v = window.__app.pdfView, set = v.setSplice.bind(v);
  window.__splices = []; v.setSplice = (s) => { set(s); window.__splices.push({ at: performance.now(), s }); }; }"""


def free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--doc", default="sample-report")
    parser.add_argument("--source", default="bench")
    parser.add_argument("--file", default="Chapters/chapter3/subsection3.tex")
    parser.add_argument("--runs", type=int, default=5)
    parser.add_argument("--chromium", default=None)
    parser.add_argument("--axe", default=AXE)
    args = parser.parse_args()

    path = ROOT / args.source / args.doc / args.file
    original = path.read_bytes()
    port = free_port()
    server = subprocess.Popen([sys.executable, str(ROOT / "scripts/serve.py"), args.doc, "--source", args.source,
                               "--no-open", "--port", str(port)], stdout=subprocess.DEVNULL, stderr=subprocess.STDOUT)
    origin = f"http://127.0.0.1:{port}"
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
            page.goto(f"{origin}/#{args.doc}")
            page.wait_for_function(LOADED, timeout=60000)
            page.wait_for_function(READY, timeout=300000)
            page.evaluate(f"() => window.__app.openFile({json.dumps(args.file)}, 0)")
            page.wait_for_function(f"() => window.__app.active.path === {json.dumps(args.file)}")
            page.evaluate(HOOK)
            # Scroll the PDF into the chapter first, so the splice happens in view.
            page.evaluate("() => { const v = window.__app.pdfView; "
                          "v.viewer.scrollTop = v.els[Math.min(70, v.els.length - 1)].offsetTop; }")
            page.click(".cm-content")
            page.keyboard.press("Control+End")
            times = []
            for i in range(args.runs + 1):
                top = page.evaluate("() => window.__app.pdfView.viewer.scrollTop")
                before = page.evaluate("() => window.__splices.length")
                page.keyboard.type(f" zq{i}x")
                typed = page.evaluate("() => performance.now()")
                page.wait_for_function(f"() => window.__splices.length > {before}", timeout=60000)
                at = page.evaluate(f"() => window.__splices[{before}].at")
                found = page.evaluate(f"""async () => {{ const s = window.__splices[{before}].s;
                    for (let n = 1; n <= s.doc.numPages; n++) {{
                      const t = await (await s.doc.getPage(n)).getTextContent();
                      if (t.items.some((x) => x.str.includes("zq{i}x"))) return true; }}
                    return false; }}""")
                moved = abs(page.evaluate("() => window.__app.pdfView.viewer.scrollTop") - top)
                state = page.inner_text("#liveState")
                print(f"run {i}: keystroke -> spliced {at - typed:.0f} ms; word in preview: {found}; "
                      f"scroll moved {moved:.0f} px; status '{state}'")
                assert found and moved < 2
                if i:
                    times.append(at - typed)
                page.wait_for_timeout(1500)
            # A keystroke during a preview aborts it: the request in flight never splices.
            before = page.evaluate("() => window.__splices.length")
            page.keyboard.type(" a")
            page.wait_for_timeout(800)  # The preview has started...
            page.keyboard.type("b")  # ...and is out of date now.
            page.wait_for_function(f"() => window.__splices.length > {before}", timeout=60000)
            page.wait_for_timeout(1500)
            assert page.evaluate("() => window.__splices.length") == before + 1, "an aborted preview was shown"
            print(f"median keystroke -> spliced pages: {statistics.median(times):.0f} ms "
                  f"(min {min(times):.0f}, includes the 700 ms pause)")
            for theme in ("light", "dark"):
                page.evaluate("t => document.documentElement.dataset.theme = t", theme)
                if not page.evaluate("() => !!window.axe"):
                    if args.axe.startswith("http"):
                        page.add_script_tag(url=args.axe)
                    else:
                        page.add_script_tag(path=args.axe)
                page.evaluate("() => document.getElementById('settings').showModal()")
                result = page.evaluate("() => axe.run(document, {resultTypes: ['violations']})")
                page.evaluate("() => document.getElementById('settings').close()")
                bad = [f"{v['id']}: {v['help']} ({v['nodes'][0]['target']})" for v in result["violations"]]
                assert not bad, f"axe ({theme}): {bad}"
            print("axe: 0 violations (light and dark, settings open)")
            browser.close()
    finally:
        server.terminate()
        server.wait(30)
        path.write_bytes(original)
    return 0


if __name__ == "__main__":
    sys.exit(main())
