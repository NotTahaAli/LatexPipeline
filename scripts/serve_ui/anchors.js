// Text anchors for review comments and suggestions: offsets plus the quoted text and a little context on each
// side. An editor that has the file open maps anchors through every change (CodeMirror decorations) and sends the
// moved anchors back; everyone else finds them again from the quote and context. No imports: tests/collab_check.mjs
// runs these in Node.

const CONTEXT = 32;
const MAX_CANDIDATES = 2000;

/** The anchor of text[from, to). */
export function makeAnchor(text, from, to) {
  return { from, to, quote: text.slice(from, to), prefix: text.slice(Math.max(0, from - CONTEXT), from), suffix: text.slice(to, to + CONTEXT) };
}

/** How many characters of `a` end where `b` ends (backwards), and the same forwards. */
const tailMatch = (text, end, want) => { let n = 0; while (n < want.length && end - n - 1 >= 0 && text[end - n - 1] === want[want.length - 1 - n]) n++; return n; };
const headMatch = (text, start, want) => { let n = 0; while (n < want.length && start + n < text.length && text[start + n] === want[n]) n++; return n; };

/**
 * Where the anchored text is in `text` now: {from, to}, or null when it is gone. Exact old place first; otherwise
 * the occurrence of the quote whose surroundings match best (ties: nearest the old place). An empty quote (a
 * suggested insertion) is placed by its context alone.
 */
export function locate(text, a) {
  if (!a || typeof a.quote !== "string") return null;
  const len = a.quote.length, prefix = a.prefix || "", suffix = a.suffix || "";
  const score = (at) => tailMatch(text, at, prefix) + headMatch(text, at + len, suffix);
  const whole = prefix.length + suffix.length;
  if (a.from >= 0 && a.from + len <= text.length && text.slice(a.from, a.from + len) === a.quote && (len >= 8 || score(a.from) * 2 >= whole)) return { from: a.from, to: a.from + len };
  const seen = [];
  if (len) {
    for (let i = text.indexOf(a.quote); i >= 0 && seen.length < MAX_CANDIDATES; i = text.indexOf(a.quote, i + 1)) seen.push(i);
  } else {
    for (const [needle, shift] of [[prefix, prefix.length], [suffix, 0]]) {
      if (!needle) continue;
      for (let i = text.indexOf(needle); i >= 0 && seen.length < MAX_CANDIDATES; i = text.indexOf(needle, i + 1)) seen.push(i + shift);
    }
  }
  let best = null, bestScore = -1, bestDist = Infinity;
  for (const at of seen) {
    const s = score(at), d = Math.abs(at - a.from);
    if (s > bestScore || (s === bestScore && d < bestDist)) { best = at; bestScore = s; bestDist = d; }
  }
  if (best == null) return null;
  // A short or empty quote needs its context to agree, or any "the" in the file would do.
  const need = len >= 8 ? 0 : Math.min(whole, len ? 6 : 10);
  return bestScore >= need ? { from: best, to: best + len } : null;
}

/** Non-overlapping edits {from, to, ...} to apply together, in document order; the rest come back as `clash`. */
export function disjoint(edits) {
  const ok = [], clash = [];
  for (const e of [...edits].sort((x, y) => x.from - y.from || x.to - y.to)) {
    const last = ok[ok.length - 1];
    if (last && (e.from < last.to || (e.from === last.to && e.from === e.to && last.from === last.to))) clash.push(e); else ok.push(e);
  }
  return { ok, clash };
}
