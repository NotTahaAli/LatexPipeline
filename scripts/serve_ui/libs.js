// Third-party code, loaded from CDNs at exact versions. One place to bump them.
// The CodeMirror packages are mapped once in index.html's import map and every esm.sh URL is
// built with ?external=..., so there is exactly one copy of state/view/language/lezer.
import * as state from "@codemirror/state";
import * as view from "@codemirror/view";
import * as language from "@codemirror/language";
import * as commands from "@codemirror/commands";
import * as search from "@codemirror/search";
import * as autocomplete from "@codemirror/autocomplete";
import * as highlight from "@lezer/highlight";

const ESM = "https://esm.sh";
const EXTERNAL = "@codemirror/state,@codemirror/view,@codemirror/language,@codemirror/commands,@codemirror/search,@codemirror/autocomplete,@lezer/common,@lezer/highlight,@lezer/lr";
const pin = (pkg) => `${ESM}/${pkg}?external=${EXTERNAL}`;

export const PDFJS = "https://cdnjs.cloudflare.com/ajax/libs/pdf.js/4.4.168";
export const KATEX = "https://cdn.jsdelivr.net/npm/katex@0.16.11/dist";
import * as Y from "yjs";
export { Y };
export { state, view, language, commands, search, autocomplete, highlight };
export const { stex } = await import(pin("@codemirror/legacy-modes@6.5.1/mode/stex"));

// Loaded on demand (progressive disclosure: most sessions never need them).
export const loadVim = () => import(pin("@replit/codemirror-vim@6.3.0"));
// Raw dist file, not esm.sh: its minifier drops the key-binding registration loop (marked pure), which leaves emacs mode inert.
export const loadEmacs = () => import("https://cdn.jsdelivr.net/npm/@replit/codemirror-emacs@6.1.0/dist/index.js");
let katexPromise;
export function loadKatex() {
  katexPromise ||= (async () => {
    const css = document.createElement("link");
    css.rel = "stylesheet"; css.href = `${KATEX}/katex.min.css`;
    document.head.append(css);
    return (await import(`${KATEX}/katex.mjs`)).default;
  })();
  return katexPromise;
}

// Real-time co-editing. yjs is mapped once in the import map and marked external here, so the three packages
// share one copy of it (and one lib0 via deps). If the CDN is unreachable the editor still works, single-user.
const COLLAB_DEPS = "deps=lib0@0.2.99";
export const collabLibs = await Promise.all([
  import(`${ESM}/y-codemirror.next@0.3.5?external=yjs,@codemirror/state,@codemirror/view&${COLLAB_DEPS}`),
  import(`${ESM}/y-protocols@1.0.6/awareness?external=yjs&${COLLAB_DEPS}`),
]).then(([cm, awareness]) => ({ yCollab: cm.yCollab, yUndoManagerKeymap: cm.yUndoManagerKeymap, awareness }), (e) => { console.warn("co-editing unavailable", e); return null; });
export const awarenessProtocol = collabLibs?.awareness;
