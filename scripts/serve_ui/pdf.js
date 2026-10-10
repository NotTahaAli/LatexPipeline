// PDF.js viewer: page boxes first, canvases only for pages near the viewport,
// so a 300-page document costs one getPage for the first paint.

export class PdfView {
  constructor(viewer, pagesEl, { zoom, onZoom, onFirstPage }) {
    this.viewer = viewer; this.pagesEl = pagesEl;
    this.zoom = zoom; this.onZoom = onZoom; this.onFirstPage = onFirstPage;
    this.pdf = null; this.sizes = []; this.els = []; this.seq = 0; this.version = null;
    this.io = new IntersectionObserver((entries) => entries.forEach((e) => {
      const i = +e.target.dataset.i;
      if (e.isIntersecting) this.render(i); else this.release(i);
    }), { root: viewer, rootMargin: "1200px 0px" });
    this.ready = import("pdfjs").then((m) => {
      this.lib = m; m.GlobalWorkerOptions.workerSrc = import.meta.resolve("pdfjs-worker");
    });
  }

  /** [document, page number] shown at box i: the full PDF, or a live preview spliced over some of its pages. */
  source(i) {
    const s = this.splice;
    if (!s || i < s.from) return [this.pdf, i + 1];
    if (i < s.from + s.doc.numPages) return [s.doc, i - s.from + 1];
    return [this.pdf, i - s.doc.numPages + s.count + 1];
  }

  /** Box index of full-PDF page n (1-based); a page under the splice maps to the splice's first box. */
  index(n) {
    const s = this.splice;
    if (!s || n <= s.from) return n - 1;
    return n <= s.from + s.count ? s.from : n - 1 - s.count + s.doc.numPages;
  }

  /** Show s.doc in place of s.count full pages from box s.from (null: the full PDF again), in place, without a blank. */
  setSplice(s) {
    if (!this.pdf) return;
    const top = this.viewer.scrollTop, first = this.sizes[0];
    this.splice = s;
    const n = this.pdf.numPages + (s ? s.doc.numPages - s.count : 0);
    this.sizes = Array.from({ length: n }, (_, i) => this.sizes[i] || first);
    this.layout();
    this.els.forEach((el, i) => { el.classList.toggle("spliced", !!s && i >= s.from && i < s.from + s.doc.numPages); if (el.querySelector("canvas")) this.render(i); });
    this.viewer.scrollTop = top;
  }

  async render(i) {
    const el = this.els[i], [doc, n] = this.source(i);
    if (!el || !doc) return;
    const token = el.token = (el.token || 0) + 1;
    const page = await doc.getPage(n);
    if (token !== el.token || doc !== this.source(i)[0]) return;
    this.setSize(i, page.getViewport({ scale: 1 }));
    const vp = page.getViewport({ scale: this.zoom * (window.devicePixelRatio || 1) });
    const canvas = document.createElement("canvas");
    canvas.width = vp.width; canvas.height = vp.height;
    await page.render({ canvasContext: canvas.getContext("2d"), viewport: vp }).promise;
    if (token !== el.token) return;
    el.querySelector("canvas")?.remove();  // Swap in one step: the old render stays until now.
    el.prepend(canvas);
    if (!this.firstDone) { this.firstDone = true; this.onFirstPage?.(); }
  }

  release(i) {   // Free canvases far from view (memory on long documents).
    const el = this.els[i];
    if (!el) return;
    el.token = (el.token || 0) + 1;
    el.querySelector("canvas")?.remove();
  }

  setSize(i, viewport) {
    const [w, h] = this.sizes[i] || [];
    if (w === viewport.width && h === viewport.height) return;
    this.sizes[i] = [viewport.width, viewport.height];
    const el = this.els[i];
    const before = el.offsetHeight;
    this.box(el, i);
    const delta = el.offsetHeight - before;
    if (delta && el.offsetTop + before < this.viewer.scrollTop) this.viewer.scrollTop += delta;  // Keep what you read in place.
  }

  box(el, i) {
    const [w, h] = this.sizes[i];
    el.style.width = w * this.zoom + "px"; el.style.height = h * this.zoom + "px";
  }

  layout() {
    this.sizes.forEach((_, i) => {
      let el = this.els[i];
      if (!el) { el = this.els[i] = document.createElement("div"); el.className = "page"; el.dataset.i = i; el.setAttribute("role", "img"); el.setAttribute("aria-label", `Page ${i + 1}`); this.pagesEl.append(el); }
      this.box(el, i);
      this.io.unobserve(el); this.io.observe(el);  // Re-observe: fires again for pages already on screen.
    });
    while (this.els.length > this.sizes.length) { const el = this.els.pop(); this.io.unobserve(el); el.remove(); }
  }

  clear() { this.pdf = null; this.version = null; this.splice = null; this.sizes = []; this.layout(); }

  /** Full-PDF page number of box i, or null for a page of a spliced live preview. */
  fullPage(i) { const [doc, n] = this.source(i); return doc === this.pdf ? n : null; }

  async load(url, version) {
    await this.ready;
    const mine = ++this.seq;
    const next = await this.lib.getDocument(url).promise;
    if (mine !== this.seq) return;
    const first = await next.getPage(1);
    if (mine !== this.seq) return;
    const v = first.getViewport({ scale: 1 });
    const top = this.viewer.scrollTop;
    const old = this.sizes;
    // Estimate every page from the first one (documents are nearly uniform); real sizes fill in as pages render.
    this.pdf = next; this.version = version; this.splice = null;
    this.els.forEach((el) => el.classList.remove("spliced"));
    this.sizes = Array.from({ length: next.numPages }, (_, i) => old[i] || [v.width, v.height]);
    this.firstDone = false;
    this.layout();
    this.viewer.scrollTop = top;
  }

  setZoom(z, keepFit) {
    if (!keepFit) this.fitMode = false;   // A manual zoom stops following the pane width.
    const ratio = Math.max(0.3, Math.min(5, z)) / this.zoom;
    this.zoom = this.zoom * ratio;
    const top = this.viewer.scrollTop * ratio;
    this.layout(); this.viewer.scrollTop = top;
    this.onZoom?.(this.zoom, this.fitMode);
  }

  fit() { this.fitMode = true; if (this.sizes.length) this.setZoom((this.viewer.clientWidth - 40) / this.sizes[0][0], true); }

  async pageSize(i) {   // Exact size of page i (needed for a precise forward-search jump).
    const [doc, n] = this.source(i);
    const page = await doc.getPage(n);
    this.setSize(i, page.getViewport({ scale: 1 }));
  }

  async reveal(b) {   // Scroll to a SyncTeX box and highlight it.
    const i = this.index(b.page);
    await this.pageSize(i);
    const el = this.els[i];
    if (!el) return;
    const hit = document.createElement("div");
    hit.className = "hit";
    Object.assign(hit.style, { left: b.x * this.zoom + "px", top: b.y * this.zoom + "px", width: Math.max(b.w, 4) * this.zoom + "px", height: Math.max(b.h, 4) * this.zoom + "px" });
    el.append(hit); setTimeout(() => hit.remove(), 3000);
    this.viewer.scrollTo({ top: el.offsetTop + b.y * this.zoom - this.viewer.clientHeight / 3, behavior: "smooth" });
  }
}
