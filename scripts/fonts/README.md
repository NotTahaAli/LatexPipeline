# Fonts

Latin subsets of the editor fonts, served at `/fonts/<name>` by `serve.py` (and used by `host_ui/`).
`fonts.css` holds the `@font-face` rules. All three families are under the SIL Open Font License 1.1 (`OFL.txt`).

| File | Source (npm, exact version) | Font file in the package |
| --- | --- | --- |
| `newsreader.woff2` | `@fontsource-variable/newsreader@5.3.0` | `files/newsreader-latin-opsz-normal.woff2` (opsz + wght) |
| `newsreader-italic.woff2` | `@fontsource-variable/newsreader@5.3.0` | `files/newsreader-latin-opsz-italic.woff2` (opsz + wght) |
| `source-sans-3.woff2` | `@fontsource-variable/source-sans-3@5.3.0` | `files/source-sans-3-latin-wght-normal.woff2` (wght) |
| `ibm-plex-mono-400.woff2` | `@fontsource/ibm-plex-mono@5.3.0` | `files/ibm-plex-mono-latin-400-normal.woff2` |
| `ibm-plex-mono-500.woff2` | `@fontsource/ibm-plex-mono@5.3.0` | `files/ibm-plex-mono-latin-500-normal.woff2` |

To update, download the tarball (`npm pack <package>@<version>`), copy the files above under the names on the left,
and update the versions here. Characters outside Latin fall back to the system fonts.
