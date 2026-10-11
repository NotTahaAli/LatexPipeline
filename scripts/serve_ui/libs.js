// Third-party code at exact versions. Every URL lives in index.html's import map (the one place to bump them);
// serve.py swaps in local copies when scripts/vendor_ui.py has run. esm.sh URLs use ?external=..., so there
// is exactly one copy of state/view/language/lezer.
import * as state from "@codemirror/state";
import * as view from "@codemirror/view";
import * as language from "@codemirror/language";
import * as commands from "@codemirror/commands";
import * as search from "@codemirror/search";
import * as autocomplete from "@codemirror/autocomplete";
import * as highlight from "@lezer/highlight";

import * as Y from "yjs";
export { Y };
export { state, view, language, commands, search, autocomplete, highlight };
export const { stex } = await import("stex");

// Loaded on demand (progressive disclosure: most sessions never need them).
export const loadVim = () => import("vim");
// Raw dist file, not esm.sh: its minifier drops the key-binding registration loop (marked pure), which leaves emacs mode inert.
export const loadEmacs = () => import("emacs");
let katexPromise;
export function loadKatex() {
  katexPromise ||= (async () => {
    const css = document.createElement("link");
    css.rel = "stylesheet"; css.href = import.meta.resolve("katex.css");
    document.head.append(css);
    return (await import("katex")).default;
  })();
  return katexPromise;
}

// Real-time co-editing. yjs is mapped once in the import map and marked external here, so the three packages
// share one copy of it (and one lib0 via deps). If the CDN is unreachable the editor still works, single-user.
export const collabLibs = await Promise.all([
  import("y-codemirror.next"),
  import("y-protocols/awareness"),
]).then(([cm, awareness]) => ({ yCollab: cm.yCollab, yUndoManagerKeymap: cm.yUndoManagerKeymap, awareness }), (e) => { console.warn("co-editing unavailable", e); return null; });
export const awarenessProtocol = collabLibs?.awareness;
