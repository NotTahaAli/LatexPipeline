# build.toml

Every key a document's `build.toml` accepts, for anyone configuring how one document builds.

## Rules

- `build.toml` sits next to `main.tex`. Every key is optional.
- An unknown key, a wrong type or an unsupported value fails that document only. The reason is at the top of its log.
- The engine can also come from a magic comment in the first 20 lines of `main.tex` (`% !TEX program = xelatex`).

```toml
engine = "lualatex"
pdfa = "2b"
timeout = 900
```

## Keys

| Key | Values | Default | Effect |
| --- | --- | --- | --- |
| `engine` | `"pdflatex"`, `"xelatex"`, `"lualatex"` | magic comment, else `"pdflatex"` | LaTeX engine; overrides the magic comment. |
| `shell_escape` | boolean | `false` | Allow `\write18`. Forced off while sharing. |
| `latexmk_args` | list of strings | `[]` | Extra latexmk arguments. While sharing, ones that run code or move output fail the build. |
| `externalize` | boolean | `true` | Compile TikZ/pgfplots figures once and cache them, see [Large documents](../guides/large-documents.md). |
| `pdfa` | `"2b"`, `"a-2b"`; part 1, 2 or 3 with conformance `a`, `b` or `u` (such as `"3b"`); part 4 as `"4"`, `"4e"`, `"4f"` | off | PDF/A level, see [PDF/A and tagged PDF](../guides/pdfa-tagged.md). |
| `tagged` | boolean | `false` | Tagged PDF (accessibility, PDF/UA-1; UA-2 with PDF/A-4), see [PDF/A and tagged PDF](../guides/pdfa-tagged.md). |
| `lang` | language tag | `"en-US"` | Document language, used for PDF/A metadata and the grammar checker. |
| `timeout` | seconds, 10 to 7200 | `600` | Time before a latexmk run is killed. `--timeout` changes the default; sharing caps it at 300. |
| `html` | boolean | `false` | Also export HTML (`out/<name>.html.zip`) after each build and publish it to the release, see [Export](../guides/export.md). |
| `review` | `"off"`, `"show"`, `"clean"` | `"off"` | The [review markup](../guides/review-markup.md) macros; `"off"` defines none. `--review` overrides it. |
| `draft` | boolean | `false` | graphicx draws a box for each image instead of the image. `--draft` does this for every document. See [Large documents](../guides/large-documents.md) for what it saves. |
| `strict` | boolean | `false` | Fail the document on undefined or multiply defined references and citations, and on overfull boxes over 1 pt (`--strict`). |
| `anonymous` | boolean | `false` | Double-blind check, see [below](#anonymous-check). |
| `[[output]]` | table, repeatable | none | An extra PDF of the document, see [below](#several-pdfs-from-one-document). |
| `grammar` | `"auto"`, `"off"`, `"local"`, `"public"` | `"auto"` | Grammar checking mode, see [Grammar](../guides/grammar.md). |
| `grammar_url` | URL | `http://localhost:8081` | LanguageTool server for local mode (`LANGUAGETOOL_URL` is the fallback). |
| `disabled_rules` | list of strings | `[]` | LanguageTool rule ids to ignore. |

## Anonymous check

With `anonymous = true` the log lists anything that could reveal the authors:

- `\author`, `\affiliation`, `\email` or `\thanks` text that is not "Anonymous"
- e-mail addresses
- acknowledgements sections
- phrases such as "our previous work"

These are notes, never failures. Comments are skipped.

## Several PDFs from one document

Each `[[output]]` table builds one more PDF after the main one: `out/<name>-<output name>.pdf` and its `.log`, in its own cache directory.

| Key | Required | Effect |
| --- | --- | --- |
| `name` | yes | Letters, digits and `_`. Becomes the suffix of the PDF name. |
| `define` | no | Macro names defined empty before the document starts (`\def\handout{}`), so the document can test `\ifdefined\handout`. |
| `engine`, `review`, `draft`, `anonymous` | no | Override the document's value for this output. |

- CI builds and publishes outputs with the document, under the same release naming rule. A failed output fails the document.
- The editor, `--watch` and `--focus` build the main PDF only.
- An output whose PDF would be another document's fails (`files/a` with output `b` next to `files/a-b`).

```toml
anonymous = true

[[output]]
name = "camera_ready"
anonymous = false
define = ["cameraready"]

[[output]]
name = "handout"
define = ["handout"]
```

## Next

- [Documents](../guides/documents.md)
- [Command-line reference](cli.md)
- [PDF/A and tagged PDF](../guides/pdfa-tagged.md)
