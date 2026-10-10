// Live preview: after a pause in typing, the server typesets the chapter around the cursor from the editor's text
// (a warm TeX process, scripts/preview.py) and its pages replace that chapter's pages in the PDF view, in place.
// A newer keystroke aborts the request in flight; the next request also stops it on the server.

const DELAY = 700;   // ms of quiet before a preview starts

export class LivePreview {
  /**
   * view: the PdfView. opts: doc() current document, tab() {path, text} of the file being typed or null,
   * enabled() whether to run now, onState({busy, error, seconds}) for the status line.
   */
  constructor(view, opts) {
    this.view = view; this.opts = opts;
    this.timer = null; this.ctl = null; this.last = null; this.labels = new WeakMap();
    this.cid = Math.random().toString(36).slice(2, 10);   // A newer preview of this page stops its older one on the server.
  }

  /** The person typed: start over the wait, and drop the preview in flight (it is already out of date). */
  edited() {
    clearTimeout(this.timer);
    this.ctl?.abort();
    if (this.opts.enabled()) this.timer = setTimeout(() => this.run(), DELAY);
  }

  stop() { clearTimeout(this.timer); this.ctl?.abort(); }

  async run() {
    const doc = this.opts.doc(), tab = this.opts.tab();
    if (!tab || !this.opts.enabled()) return;
    const ctl = this.ctl = new AbortController();
    this.opts.onState({ busy: true });
    try {
      const res = await fetch(`api/preview?${new URLSearchParams({ doc, cid: this.cid })}`, {
        method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ path: tab.path, text: tab.text }), signal: ctl.signal,
      });
      if (!(res.headers.get("Content-Type") || "").startsWith("application/pdf")) {
        const data = await res.json().catch(() => ({}));
        if (data.cancelled || ctl.signal.aborted) return;
        const first = data.errors?.[0];
        this.opts.onState({ busy: false, error: data.error || (first ? `${first.file}:${first.line}: ${first.message}` : "The preview failed."), off: res.status === 409 });
        return;
      }
      const meta = JSON.parse(res.headers.get("X-Preview") || "{}");
      const data = await res.arrayBuffer();
      if (ctl.signal.aborted) return;
      await this.view.ready;
      const pdf = await this.view.lib.getDocument({ data }).promise;
      if (ctl.signal.aborted || doc !== this.opts.doc()) return;
      this.last = { doc, pdf, meta };
      await this.apply();
      this.opts.onState({ busy: false, seconds: meta.seconds, warm: meta.warm });
    } catch (e) {
      if (e.name !== "AbortError") this.opts.onState({ busy: false, error: e.message });
    } finally { if (this.ctl === ctl) this.ctl = null; }
  }

  async pageLabels(pdf) {
    if (!this.labels.has(pdf)) this.labels.set(pdf, await pdf.getPageLabels().catch(() => null));
    return this.labels.get(pdf);
  }

  /**
   * Splice the last preview over its chapter: its first page label (hyperref writes them) is found in the full PDF,
   * and the chapter ends at the page the full build recorded. Without labels the preview is shown on its own.
   */
  async apply() {
    const last = this.last, view = this.view;
    if (!last || !view.pdf || last.doc !== this.opts.doc()) return;
    const full = await this.pageLabels(view.pdf), mine = await this.pageLabels(last.pdf);
    let from = -1, to = -1;
    if (full && mine) {
      from = full.indexOf(mine[0]);
      to = from < 0 ? -1 : full.indexOf(String(last.meta.pages?.end ?? ""), from);
    }
    if (from < 0 || to < from) { from = 0; to = view.pdf.numPages - 1; }
    view.setSplice({ doc: last.pdf, from, count: to - from + 1 });
  }

  /** A new full PDF was loaded: keep showing the preview only if it was typeset after that build started. */
  async afterLoad(buildStarted) {
    if (this.last && this.last.meta.t > (buildStarted || 0)) await this.apply();
    else this.last = null;
  }
}
