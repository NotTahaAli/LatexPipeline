// Export HTML (owner): the server runs make4ht (TeX4ht, MathML math, figures as SVG) on a copy of the document in the
// sandbox and writes out/<name>.html/ and out/<name>.html.zip; the browser downloads the zip. The pages are never
// served by the editor: they are the document's own HTML.

export async function exportHtml(api, doc, toast, el) {
  toast(el("span", { textContent: "Converting to HTML with make4ht..." }));
  try {
    const { message } = await api.html(doc);
    el("a", { href: api.htmlUrl(doc), download: "" }).click();
    toast(el("span", { textContent: `HTML ready (${message}); your browser is downloading the zip.` }));
  } catch (e) { toast(el("span", { textContent: e.message })); }
}
