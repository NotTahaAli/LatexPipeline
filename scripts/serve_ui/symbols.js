// Data for the symbol palette (navpanels.js): [command, Unicode look-alike, words to search by]. Math-mode commands
// of LaTeX and amssymb/amsmath; "{}" marks where the cursor goes for accents. The glyph is only a picture.

const G = {
  Greek: [
    ["\\alpha", "α"], ["\\beta", "β"], ["\\gamma", "γ"], ["\\delta", "δ"], ["\\epsilon", "ϵ"], ["\\varepsilon", "ε"],
    ["\\zeta", "ζ"], ["\\eta", "η"], ["\\theta", "θ"], ["\\vartheta", "ϑ"], ["\\iota", "ι"], ["\\kappa", "κ"],
    ["\\lambda", "λ"], ["\\mu", "μ"], ["\\nu", "ν"], ["\\xi", "ξ"], ["\\pi", "π"], ["\\varpi", "ϖ"], ["\\rho", "ρ"],
    ["\\varrho", "ϱ"], ["\\sigma", "σ"], ["\\varsigma", "ς"], ["\\tau", "τ"], ["\\upsilon", "υ"], ["\\phi", "ϕ"],
    ["\\varphi", "φ"], ["\\chi", "χ"], ["\\psi", "ψ"], ["\\omega", "ω"],
    ["\\Gamma", "Γ"], ["\\Delta", "Δ"], ["\\Theta", "Θ"], ["\\Lambda", "Λ"], ["\\Xi", "Ξ"], ["\\Pi", "Π"],
    ["\\Sigma", "Σ"], ["\\Upsilon", "Υ"], ["\\Phi", "Φ"], ["\\Psi", "Ψ"], ["\\Omega", "Ω"],
  ],
  Operators: [
    ["\\pm", "±", "plus minus"], ["\\mp", "∓", "minus plus"], ["\\times", "×", "multiply"], ["\\div", "÷", "divide"],
    ["\\cdot", "⋅", "dot multiply"], ["\\ast", "∗"], ["\\star", "⋆"], ["\\circ", "∘", "compose"], ["\\bullet", "∙"],
    ["\\oplus", "⊕"], ["\\ominus", "⊖"], ["\\otimes", "⊗", "tensor"], ["\\oslash", "⊘"], ["\\odot", "⊙"],
    ["\\cap", "∩", "intersection"], ["\\cup", "∪", "union"], ["\\setminus", "∖", "difference"], ["\\wedge", "∧", "and"],
    ["\\vee", "∨", "or"], ["\\neg", "¬", "not"], ["\\sum", "∑", "sum"], ["\\prod", "∏", "product"], ["\\coprod", "∐"],
    ["\\int", "∫", "integral"], ["\\iint", "∬", "double integral"], ["\\oint", "∮", "contour integral"],
    ["\\bigcup", "⋃", "union"], ["\\bigcap", "⋂", "intersection"], ["\\bigoplus", "⨁"], ["\\bigotimes", "⨂"],
    ["\\partial", "∂", "derivative"], ["\\nabla", "∇", "gradient"], ["\\sqrt{}", "√", "root"], ["\\frac{}{}", "½", "fraction"],
  ],
  Relations: [
    ["\\leq", "≤", "less equal"], ["\\geq", "≥", "greater equal"], ["\\neq", "≠", "not equal"], ["\\approx", "≈", "approximately"],
    ["\\equiv", "≡", "equivalent"], ["\\sim", "∼", "similar"], ["\\simeq", "≃"], ["\\cong", "≅", "congruent"],
    ["\\propto", "∝", "proportional"], ["\\ll", "≪", "much less"], ["\\gg", "≫", "much greater"], ["\\prec", "≺"],
    ["\\succ", "≻"], ["\\in", "∈", "element"], ["\\notin", "∉", "not element"], ["\\ni", "∋"], ["\\subset", "⊂", "subset"],
    ["\\supset", "⊃", "superset"], ["\\subseteq", "⊆", "subset"], ["\\supseteq", "⊇", "superset"], ["\\mid", "∣", "divides"],
    ["\\parallel", "∥", "parallel"], ["\\perp", "⊥", "perpendicular"], ["\\vdash", "⊢", "proves"], ["\\models", "⊨"],
    ["\\coloneqq", "≔", "define mathtools"], ["\\doteq", "≐"],
  ],
  Arrows: [
    ["\\to", "→", "right arrow"], ["\\gets", "←", "left arrow"], ["\\leftrightarrow", "↔"], ["\\Rightarrow", "⇒", "implies"],
    ["\\Leftarrow", "⇐"], ["\\Leftrightarrow", "⇔", "iff"], ["\\implies", "⟹", "implies"], ["\\iff", "⟺", "if and only if"],
    ["\\mapsto", "↦", "maps to"], ["\\uparrow", "↑"], ["\\downarrow", "↓"], ["\\Uparrow", "⇑"], ["\\Downarrow", "⇓"],
    ["\\longrightarrow", "⟶"], ["\\longleftarrow", "⟵"], ["\\hookrightarrow", "↪", "injection"],
    ["\\twoheadrightarrow", "↠", "surjection amssymb"], ["\\rightharpoonup", "⇀"], ["\\nearrow", "↗"], ["\\searrow", "↘"],
  ],
  Accents: [
    ["\\hat{}", "â", "hat"], ["\\widehat{}", "â", "wide hat"], ["\\bar{}", "ā", "bar"], ["\\overline{}", "a̅", "overline"],
    ["\\tilde{}", "ã", "tilde"], ["\\widetilde{}", "ã", "wide tilde"], ["\\vec{}", "a⃗", "vector"], ["\\dot{}", "ȧ", "dot"],
    ["\\ddot{}", "ä", "double dot"], ["\\acute{}", "á"], ["\\grave{}", "à"], ["\\breve{}", "ă"], ["\\check{}", "ǎ"],
    ["\\underline{}", "a̲", "underline"], ["\\mathbf{}", "𝐚", "bold"], ["\\mathbb{}", "ℝ", "blackboard amssymb"],
    ["\\mathcal{}", "𝒜", "calligraphic"], ["\\mathfrak{}", "𝔄", "fraktur amssymb"], ["\\mathrm{}", "a", "roman upright"],
  ],
  Misc: [
    ["\\infty", "∞", "infinity"], ["\\forall", "∀", "for all"], ["\\exists", "∃", "exists"], ["\\nexists", "∄"],
    ["\\emptyset", "∅", "empty set"], ["\\varnothing", "∅", "empty set amssymb"], ["\\aleph", "ℵ"], ["\\hbar", "ℏ"],
    ["\\ell", "ℓ"], ["\\Re", "ℜ", "real part"], ["\\Im", "ℑ", "imaginary part"], ["\\angle", "∠"], ["\\triangle", "△"],
    ["\\ldots", "…", "dots"], ["\\cdots", "⋯", "dots"], ["\\vdots", "⋮", "dots"], ["\\ddots", "⋱", "dots"],
    ["\\prime", "′"], ["\\langle", "⟨", "angle bracket"], ["\\rangle", "⟩", "angle bracket"], ["\\lfloor", "⌊", "floor"],
    ["\\rfloor", "⌋", "floor"], ["\\lceil", "⌈", "ceiling"], ["\\rceil", "⌉", "ceiling"], ["\\|", "‖", "norm"],
    ["\\top", "⊤"], ["\\bot", "⊥"], ["\\dagger", "†"], ["\\checkmark", "✓", "amssymb"], ["\\S", "§", "section sign"],
  ],
};

/** [{group, cmd, ch, name}] */
export const SYMBOLS = Object.entries(G).flatMap(([group, rows]) => rows.map(([cmd, ch, words]) => ({ group, cmd, ch, name: `${cmd.replace(/[\\{}]/g, "")} ${words || ""}`.trim() })));
export const GROUPS = Object.keys(G);
