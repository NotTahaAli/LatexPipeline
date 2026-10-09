// PDF.js viewer: page boxes first, canvases only for pages near the viewport,
// so a 300-page document costs one getPage for the first paint.
import { PDFJS } from "./libs.js";

export class PdfView {
  constructor(viewer, pagesEl, { zoom, onZoom, onFirstPage }) {
    this.viewer = viewer; this.pagesEl = pagesEl;
    this.zoom = zoom; this.onZoom = onZoom; this.onFirstPage = onFirstPage;
    this.pdf = null; this.sizes = []; this.els = []; this.seq = 0; this.version = null;
    this.io = new IntersectionObserver((entries) => entries.forEach((e) => {
      const i = +e.target.dataset.i;
      if (e.isIntersecting) this.render(i); else this.release(i);
    }), { root: viewer, rootMargin: "1200px 0px" });
    this.ready = import(`${PDFJS}/pdf.min.mjs`).then((m) => {
      this.lib = m; m.GlobalWorkerOptions.workerSrc = `${PDFJS}/pdf.worker.min.mjs`;
    });
  }

  async render(i) {
    const el = this.els[i], doc = this.pdf;
    if (!el || !doc) return;
    const token = el.token = (el.token || 0) + 1;
    const page = await doc.getPage(i + 1);
    if (token !== el.token || doc !== this.pdf) return;
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
      if (!el) { el = this.els[i] = document.createElement("div"); el.className = "page"; el.dataset.i = i; el.setAttribute("aria-label", `Page ${i + 1}`); this.pagesEl.append(el); }
      this.box(el, i);
      this.io.unobserve(el); this.io.observe(el);  // Re-observe: fires again for pages already on screen.
    });
    while (this.els.length > this.sizes.length) { const el = this.els.pop(); this.io.unobserve(el); el.remove(); }
  }

  clear() { this.pdf = null; this.version = null; this.sizes = []; this.layout(); }

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
    this.pdf = next; this.version = version;
    this.sizes = Array.from({ length: next.numPages }, (_, i) => old[i] || [v.width, v.height]);
    this.firstDone = false;
    this.layout();
    this.viewer.scrollTop = top;
  }

  setZoom(z) {
    const ratio = Math.max(0.3, Math.min(5, z)) / this.zoom;
    this.zoom = this.zoom * ratio;
    const top = this.viewer.scrollTop * ratio;
    this.layout(); this.viewer.scrollTop = top;
    this.onZoom?.(this.zoom);
  }

  fit() { if (this.sizes.length) this.setZoom((this.viewer.clientWidth - 40) / this.sizes[0][0]); }

  async pageSize(i) {   // Exact size of page i (needed for a precise forward-search jump).
    const page = await this.pdf.getPage(i + 1);
    this.setSize(i, page.getViewport({ scale: 1 }));
  }

  async reveal(b) {   // Scroll to a SyncTeX box and highlight it.
    await this.pageSize(b.page - 1);
    const el = this.els[b.page - 1];
    if (!el) return;
    const hit = document.createElement("div");
    hit.className = "hit";
    Object.assign(hit.style, { left: b.x * this.zoom + "px", top: b.y * this.zoom + "px", width: Math.max(b.w, 4) * this.zoom + "px", height: Math.max(b.h, 4) * this.zoom + "px" });
    el.append(hit); setTimeout(() => hit.remove(), 3000);
    this.viewer.scrollTo({ top: el.offsetTop + b.y * this.zoom - this.viewer.clientHeight / 3, behavior: "smooth" });
  }
}
