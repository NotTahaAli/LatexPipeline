# Keyboard shortcuts

This page lists the local editor's keyboard shortcuts and its math shortcuts, for quick reference.

## Editor and commands

| Action | Windows/Linux | macOS |
|---|---|---|
| Open the command palette | Ctrl+K | Cmd+K |
| Save (applies `.editorconfig` trimming) | Ctrl+S | Cmd+S |
| Bold: toggle `\textbf{...}` | Ctrl+B | Cmd+B |
| Emphasis: toggle `\emph{...}` | Ctrl+I | Cmd+I |
| Toggle the file list (outside the editor) | Ctrl+B | Cmd+B |
| Fold the section or environment at the cursor | Ctrl+Shift+[ | Cmd+Alt+[ |
| Unfold at the cursor | Ctrl+Shift+] | Cmd+Alt+] |
| Fold all | Ctrl+Alt+[ | Ctrl+Alt+[ |
| Unfold all | Ctrl+Alt+] | Ctrl+Alt+] |
| List code-check findings | Ctrl+Shift+M | Cmd+Shift+M |
| Jump to the next code-check finding | F8 | F8 |
| Rewrap paragraph | Alt+Q | Option+Q |
| Open the completion list (snippets) | Ctrl+Space | Ctrl+Space |
| Expand a snippet trigger | Tab | Tab |
| Next / previous snippet tab stop | Tab / Shift+Tab | Tab / Shift+Tab |
| Leave snippet tab stops | Esc | Esc |
| Leave zen mode (when nothing in the editor uses Esc) | Esc | Esc |

## Files, sidebar and PDF

| Action | Windows/Linux | macOS |
|---|---|---|
| Rename or move the selected file (Files tab) | F2 | F2 |
| Delete the selected file (Files tab, asks first) | Delete | Delete |
| Resize the focused sidebar edge | Arrow keys | Arrow keys |
| Jump from the PDF to the source | Double-click | Double-click |

## TikZ picture canvas

| Action | Windows/Linux | macOS |
|---|---|---|
| Move the selected node | Arrow keys | Arrow keys |
| Remove the selected node | Delete | Delete |

## Math shortcuts

Turn them on in Settings > Editing (off by default). In math mode, type `@` and a key. Math mode is `$...$`, `$$...$$`, `\(...\)`, `\[...\]` and the display environments, but not `\text{...}` inside them. The table follows LaTeX Workshop's.

### Greek letters

| Type | Get | Type | Get |
|---|---|---|---|
| `@a` | `\alpha` | `@n` | `\nu` |
| `@b` | `\beta` | `@p` | `\pi` |
| `@c` | `\chi` | `@vp` | `\varpi` |
| `@d` | `\delta` | `@q` | `\theta` |
| `@e` | `\epsilon` | `@vq` | `\vartheta` |
| `@ve` | `\varepsilon` | `@r` | `\rho` |
| `@f` | `\phi` | `@vr` | `\varrho` |
| `@vf` | `\varphi` | `@s` | `\sigma` |
| `@g` | `\gamma` | `@vs` | `\varsigma` |
| `@h` | `\eta` | `@t` | `\tau` |
| `@i` | `\iota` | `@u` | `\upsilon` |
| `@k` | `\kappa` | `@o` | `\omega` |
| `@l` | `\lambda` | `@x` | `\xi` |
| `@m` | `\mu` | `@y` | `\psi` |
| | | `@z` | `\zeta` |

### Capital Greek letters

| Type | Get | Type | Get |
|---|---|---|---|
| `@D` | `\Delta` | `@X` | `\Xi` |
| `@F` | `\Phi` | `@Y` | `\Psi` |
| `@G` | `\Gamma` | `@S` | `\Sigma` |
| `@Q` | `\Theta` | `@U` | `\Upsilon` |
| `@L` | `\Lambda` | `@W` | `\Omega` |
| `@P` | `\Pi` | | |

### Symbols

| Type | Get | Type | Get |
|---|---|---|---|
| `@.` | `\cdot` | `@*` | `\times` |
| `@8` | `\infty` | `@<` | `\leq` |
| `@6` | `\partial` | `@>` | `\geq` |
| `@@` | `\circ` | `@+` | `\bigcup` |
| `@0` | `^\circ` | `@-` | `\bigcap` |
| `@=` | `\equiv` | `@&` | `\wedge` |
| `@,` | `\nonumber` | | |

## Next

- [Writing LaTeX](../editor/writing.md)
- [Editor overview](../editor/overview.md)
- [Navigating the source](../editor/navigation.md)
