// Build controls: a Stop button while a build runs (edit role), a desktop notification when a build ends (opt-in,
// Notification API), and the Log tab's "only lines that need action" filter: texfot's rules, the same as
// build.py --quiet-log (QUIET_IGNORE, QUIET_NEXT, QUIET_SHOW there; keep the two in step).

const IGNORE = /^(?:LaTeX Warning: You have requested package|LaTeX Font Warning: (?:Some font shapes|Size substitutions)|Package caption Warning: Un(?:supported|known) document class|pdfTeX warning:.*inclusion: (?:fou|mul)|libpng warning: iCCP: Not recognizing|! $)/;
const NEXT = /^(?:.*?:\d+:|!|.*pdfTeX warning|LaTeX Font Warning: Font shape|Package hyperref Warning: Token not allowed|removed on input line|Runaway argument)/;
const SHOW = /^(?:Output written|No pages of output|\(.*end occurred|(?:Und|Ov)erfull|(?:LaTeX|Package|Class|Module).*(?:Error|Warning)|.*Citation.*undefined|.* Error|Missing character:|\*\*\*\s|l\.\d+ |all text was ignored after line|.*Fatal error|.*for symbol.*on input line|Latexmk: (?:Summary|Errors|Failure|.*failed)|=====|ERROR: |Build of )/;

/** The lines of a build log that need action, plus the line after each error (texfot-like). */
export function filterLog(text) {
  const out = [];
  let after = false;
  for (const line of text.split("\n")) {
    let keep;
    if (after) { after = false; keep = true; }
    else if (IGNORE.test(line)) keep = false;
    else if (NEXT.test(line)) { after = true; keep = true; }
    else keep = SHOW.test(line);
    if (keep) out.push(line);
  }
  return out.join("\n");
}

/** ctx: { api, $, el, store, toast, canEdit, doc: () => current document name } */
export function buildControls(ctx) {
  const { $, el, store, toast } = ctx;
  const say = (text) => toast(el("span", { textContent: text }));

  const stop = el("button", { id: "stopBtn", type: "button", className: "btn quiet", hidden: true, textContent: "Stop", title: "Stop the running build" });
  $("rebuildBtn").after(stop);
  stop.onclick = async () => {
    stop.disabled = true;
    try { if (!(await ctx.api.cancelBuild(ctx.doc())).ok) say("No build is running."); }
    catch (e) { say(e.message); }
    finally { stop.disabled = false; }
  };

  const pre = $("logText");
  const box = el("input", { type: "checkbox", checked: store.get("logFiltered") === "1" });
  pre.before(el("label", { className: "logfilter" }, box, " Only lines that need action"));
  let raw = "";
  const render = () => { pre.textContent = box.checked ? (filterLog(raw) || "Nothing that needs action.") : raw; };
  box.onchange = () => { store.set("logFiltered", box.checked ? "1" : "0"); render(); };

  const last = {};
  const notifying = () => store.get("notify") === "1" && "Notification" in window && Notification.permission === "granted";
  async function toggleNotify() {
    if (!("Notification" in window)) { say("This browser cannot show notifications."); return; }
    if (store.get("notify") === "1") { store.set("notify", "0"); say("Build notifications off."); return; }
    const answer = Notification.permission === "granted" ? "granted" : await Notification.requestPermission();
    if (answer !== "granted") { say("Notifications are blocked for this page in the browser."); return; }
    store.set("notify", "1");
    say("You will get a notification when a build ends while this window is in the background.");
  }

  return {
    /** The Log tab's text, shown whole or filtered. */
    showLog(text) { raw = text; render(); },
    /** Every state message: the Stop button for the open document, a notification when a build ended. */
    update(docs, cur) {
      const d = docs[cur];
      stop.hidden = !ctx.canEdit || !d || d.status !== "building";
      for (const [name, s] of Object.entries(docs)) {
        const was = last[name]; last[name] = s.status;
        if (was !== "building" || (s.status !== "ok" && s.status !== "failed") || !notifying() || document.hasFocus()) continue;
        new Notification(`${name}: ${s.status === "ok" ? "built" : "build failed"}`, {
          body: s.status === "ok" ? (s.pages != null ? `${s.pages} pages` : "") : (s.error || "Open the editor for the errors."),
          tag: "build-" + name,
        });
      }
    },
    commands: [{ id: "notify", title: "Desktop notification when a build ends (on/off)", run: toggleNotify }],
  };
}
