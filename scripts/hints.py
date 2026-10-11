"""
Plain-language hints for LaTeX log problems, and overfull-box parsing.

Pure functions, stdlib only:

- explain(message, context="") -> hint string, or None when no rule matches.
- overfull_boxes(log_text) -> [(file or None, line, points), ...]
"""

from __future__ import annotations

import re
from typing import Callable, Union

# Debian/Ubuntu package that ships each .sty, checked with dpkg -S on Ubuntu 24.04 TeX Live.
STY_PACKAGES = {
    "tikz.sty": "texlive-pictures",
    "pgfplots.sty": "texlive-pictures",
    "booktabs.sty": "texlive-latex-recommended",
    "xcolor.sty": "texlive-latex-recommended",
    "fontspec.sty": "texlive-latex-recommended",
    "caption.sty": "texlive-latex-recommended",
    "subcaption.sty": "texlive-latex-recommended",
    "float.sty": "texlive-latex-recommended",
    "microtype.sty": "texlive-latex-recommended",
    "setspace.sty": "texlive-latex-recommended",
    "listings.sty": "texlive-latex-recommended",
    "amsmath.sty": "texlive-latex-base",
    "geometry.sty": "texlive-latex-base",
    "hyperref.sty": "texlive-latex-base",
    "graphicx.sty": "texlive-latex-base",
    "natbib.sty": "texlive-latex-base",
    "tabularx.sty": "texlive-latex-base",
    "longtable.sty": "texlive-latex-base",
    "amssymb.sty": "texlive-base",
    "siunitx.sty": "texlive-science",
    "algorithm2e.sty": "texlive-science",
    "cleveref.sty": "texlive-latex-extra",
    "enumitem.sty": "texlive-latex-extra",
    "lipsum.sty": "texlive-latex-extra",
    "multirow.sty": "texlive-latex-extra",
    "csquotes.sty": "texlive-latex-extra",
    "glossaries.sty": "texlive-latex-extra",
    "minted.sty": "texlive-latex-extra",
}


def _missing_file(match: re.Match) -> str:
    name = match.group("name")
    package = STY_PACKAGES.get(name)
    if package:
        return (f"`{name}` is installed by the Debian/Ubuntu package `{package}` "
                f"(sudo apt-get install {package}); TeX Live users can run tlmgr install.")
    return (f"`{name}` is not installed. Find the package with `tlmgr search --global --file {name}` "
            f"or `apt-file search {name}`, then install it.")


Hint = Union[str, Callable[[re.Match], str]]

# (pattern, hint). The first matching rule wins, so keep specific patterns above general ones.
RULES: list[tuple[re.Pattern, Hint]] = [
    (re.compile(r"Undefined control sequence"),
     r"A command name is not defined. Check the spelling (commands are case-sensitive) or load the "
     r"package that provides it with \usepackage."),
    (re.compile(r"Missing \$ inserted"),
     r"A math-only symbol (such as _, ^ or a math command) appears in text. Wrap it in $...$ or use the "
     r"text form, for example \_ for an underscore."),
    (re.compile(r"File [`'](?P<name>[^'`]+)' not found"), _missing_file),
    (re.compile(r"Runaway argument"),
     r"A macro argument never closed, usually a missing } or a blank line inside a \caption, \section "
     r"or similar. Check the reported line and the one before it."),
    (re.compile(r"Missing \\begin\{document\}"),
     "Text or a command appears before \\begin{document}. Move it into the body, or remove stray text "
     "after \\documentclass or \\usepackage lines in the preamble."),
    (re.compile(r"Environment \S+ undefined"),
     r"The \begin{...} name is not defined. Check the spelling, or load the package that defines it "
     r"(amsmath for align, tikz for tikzpicture)."),
    (re.compile(r"Too many \}'s"),
     "There are more closing braces than opening ones. Look for an extra } or a \\end that does not "
     "match a \\begin, often near the reported line."),
    (re.compile(r"Extra alignment tab"),
     r"A table or align row has more & separators than its column count. Add column specs or remove "
     r"the extra &."),
    (re.compile(r"Misplaced alignment tab character &"),
     r"An & appears outside a tabular or align environment. In running text write \& instead."),
    (re.compile(r"Citation [`'].*? undefined"),
     "The \\cite key is not in any .bib file, or it is misspelled. Check the key against the @entry "
     "name, then run latexmk again."),
    (re.compile(r"Reference [`'].*? undefined"),
     "The \\ref target label does not exist, or it is misspelled. Check the \\label name, and run "
     "latexmk again if the label is new."),
    (re.compile(r"There were undefined (references|citations)"),
     "Labels or citations were not resolved on this pass. latexmk normally runs enough passes; if this "
     "persists, check the names listed above."),
    (re.compile(r"Label [`'].*? multiply defined"),
     "Two \\label commands use the same name. Rename one of them, or check that a file is not "
     "\\input twice."),
    (re.compile(r"Overfull \\hbox"),
     "A line sticks out into the margin. Rephrase, allow hyphenation, or wrap long URLs and code with "
     "\\url or \\texttt{\\seqsplit}; for tables, use tabularx or resize with \\resizebox."),
    (re.compile(r"Underfull \\hbox"),
     "A line is stretched with loose spacing. Usually harmless; rephrase or allow hyphenation if it "
     "looks bad in the PDF."),
    (re.compile(r"Float too large|Float\(s\) lost"),
     "A figure or table is taller than a page. Reduce its size, for example width=0.9\\textwidth, or "
     "allow the float to be split."),
    (re.compile(r"Font shape [`'].*? undefined"),
     "The font shape is not installed for this engine. Load a font package that supplies it, or pick a "
     "different \\fontfamily or \\series."),
    (re.compile(r"Option clash for package"),
     "A package was loaded twice with different options. Put the options in the first \\usepackage, or "
     "use \\PassOptionsToPackage before the first load."),
    (re.compile(r"Dimension too large"),
     "A length is beyond TeX's limit (about 16383pt). Check the units and values in \\hspace, "
     "\\setlength, or a column width."),
    (re.compile(r"TeX capacity exceeded"),
     "TeX ran out of an internal resource, usually from a recursive macro or unclosed group. Look for a "
     "\\def or \\newcommand that calls itself, or an environment that never ends."),
    (re.compile(r"Emergency stop"),
     "TeX stopped, usually because it could not read a file. Fix the error printed just above it first."),
    (re.compile(r"Unicode character|not set up for use with LaTeX"),
     "A non-ASCII character has no LaTeX mapping under pdfLaTeX. Use XeLaTeX or LuaLaTeX, or replace "
     "the character with a LaTeX command such as \\'e."),
    (re.compile(r"inputenc Error"),
     "The source file encoding does not match the input encoding. Save the file as UTF-8 and check the "
     "\\usepackage[utf8]{inputenc} line."),
    (re.compile(r"I can't write on file"),
     "LaTeX cannot write an .aux, .toc or .out file. Close any PDF viewer that has it open, or check the "
     "directory permissions."),
    (re.compile(r"Paragraph ended before"),
     "A blank line sits inside an argument such as \\caption or \\footnote. Remove the blank line there."),
    (re.compile(r"Missing number"),
     "A length or count has no number, for example \\hspace{} or an empty width=. Check the reported line."),
    (re.compile(r"Illegal unit of measure"),
     "A length is missing a unit or uses an unknown one such as px. Use pt, cm, em, or a fraction of "
     "\\textwidth."),
    (re.compile(r"Lonely \\item"),
     "A \\item sits outside a list. Wrap the items in itemize, enumerate or description, or check that an "
     "earlier \\end did not close the list early."),
    (re.compile(r"Command \\\S+ already defined"),
     "Two definitions use the same command name. Use \\renewcommand to replace a command, or pick a new "
     "name for your own macro."),
    (re.compile(r"Missing [{}] inserted"),
     "A brace is unbalanced, often a missing } after an argument. Check the reported line."),
    (re.compile(r"Double (subscript|superscript)"),
     "x^a^b is ambiguous. Group the first script with braces, for example {x^a}^b or x^{a^b}."),
    (re.compile(r"didn't find a database entry for"),
     "A cited key is not in the bibliography. Check the spelling against the .bib file, and that the "
     "file is listed in \\bibliography or \\addbibresource."),
    (re.compile(r"couldn't open database file"),
     "BibTeX cannot find a .bib file. Check its name and that it sits in the document directory."),
    (re.compile(r"Rerun to get|Label\(s\) may have changed"),
     "Not an error: LaTeX needs another pass to settle references. latexmk reruns it automatically."),
]


def explain(message: str, context: str = "") -> str | None:
    """Hint for a log message, looking at the message first and then its context lines."""
    for text in (message, context):
        if not text:
            continue
        for pattern, hint in RULES:
            match = pattern.search(text)
            if match:
                return hint(match) if callable(hint) else hint
    return None


# A file path opened by TeX, "(./main.tex" or "(Chapters/a.tex". Other parentheses (fonts,
# packages, text) push None, so the innermost .tex file is the one the box came from.
# ponytail: a path wrapped across log lines is cut short and attributed to nothing; the lint reports it as main.tex.
_OVERFULL_TOKEN = re.compile(
    r"Overfull \\hbox \((?P<pt>[\d.]+)pt too wide\)[^\n]*?\blines? (?P<line>\d+)"
    r"|\((?P<open>[^\s()]*)"
    r"|\)"
)


def overfull_boxes(log_text: str) -> list[tuple[str | None, int, float]]:
    """(file, line, points) for each overfull box: "Overfull \\hbox (12.3pt too wide) ... at lines 10--12"."""
    stack: list[str | None] = []
    found: list[tuple[str | None, int, float]] = []
    for match in _OVERFULL_TOKEN.finditer(log_text):
        if match.group("pt") is not None:
            current = next((name for name in reversed(stack) if name), None)
            found.append((current, int(match.group("line")), float(match.group("pt"))))
        elif match.group("open") is not None:
            name = match.group("open")
            if name.startswith("./"):
                name = name[2:]
            stack.append(name if name.endswith(".tex") else None)
        elif stack:
            stack.pop()
    return found
