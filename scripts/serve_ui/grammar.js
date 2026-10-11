// Grammar findings in the editor: wavy underlines and a hover card with quick fixes.
// The checking itself happens on the server (LanguageTool); positions arrive as UTF-16 offsets, which is what CodeMirror counts.

export function grammarSupport(S, V) {
  const setFindings = S.StateEffect.define();
  const field = S.StateField.define({
    create: () => V.Decoration.none,
    update(deco, tr) {
      deco = deco.map(tr.changes);
      for (const e of tr.effects) if (e.is(setFindings)) deco = e.value;
      return deco;
    },
    provide: (f) => V.EditorView.decorations.from(f),
  });

  /** The current range of every finding (they move with the text between checks). */
  function list(state) {
    const out = [];
    state.field(field, false)?.between(0, state.doc.length, (from, to, value) => { out.push({ from, to, finding: value.spec.finding }); });
    return out;
  }

  function apply(view, range, text) {
    const rest = view.state.field(field).update({ filter: (_f, _t, v) => v.spec.finding !== range.finding });
    const changes = view.state.changes({ from: range.from, to: range.to, insert: text });
    view.dispatch({ changes, effects: setFindings.of(rest.map(changes)), userEvent: "input.complete" });
  }

  const card = V.hoverTooltip((view, pos) => {
    const hit = list(view.state).find((r) => r.from <= pos && pos <= r.to);
    if (!hit) return null;
    return {
      pos: hit.from, end: hit.to, above: true,
      create(v) {
        const dom = document.createElement("div");
        dom.className = "cm-grammar-tip";
        const msg = document.createElement("div");
        msg.textContent = hit.finding.message;
        dom.append(msg);
        const row = document.createElement("div");
        row.className = "fixes";
        for (const r of hit.finding.replacements) {
          const b = document.createElement("button");
          b.type = "button"; b.className = "btn"; b.textContent = r || "(delete)";
          b.onclick = () => { const now = list(v.state).find((x) => x.finding === hit.finding); if (now) apply(v, now, r); };
          row.append(b);
        }
        if (row.children.length) dom.append(row);
        const rule = document.createElement("div");
        rule.className = "rule"; rule.textContent = hit.finding.rule;
        dom.append(rule);
        return { dom };
      },
    };
  }, { hideOnChange: true });

  return {
    extension: [field, card],
    list: (view) => list(view.state),
    apply,
    set(view, findings) {
      const len = view.state.doc.length;
      const marks = findings.filter((f) => f.to > f.from && f.to <= len)
        .map((f) => V.Decoration.mark({ class: "cm-grammar", finding: f }).range(f.from, f.to));
      view.dispatch({ effects: setFindings.of(V.Decoration.set(marks, true)) });
    },
  };
}
