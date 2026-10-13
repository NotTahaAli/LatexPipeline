# Grammar

How grammar and style checking works from the command line, in CI and in the editor, and what each mode sends where; for anyone who wants checks without leaking text by accident.

## How the check works

Checks come from [LanguageTool](https://languagetool.org). The LaTeX is reduced to prose, and every character remembers where it came from, so a finding is reported as `file:line:col`.

- Removed: comments, commands, math, `verbatim`, `lstlisting`, `minted` and `tikzpicture`.
- Kept: headings, captions, footnotes and the text of `\emph{}` and `\textbf{}`.
- `\ref` and `\cite` become a number; math becomes `X`.
- Spacing and quote rules that misfire on extracted text are ignored, as is the Typography category.

The language is `lang` from `build.toml` (default `en-US`).

## Choose a mode

Set `grammar` in the document's `build.toml`, or use Settings > Grammar in the editor.

| Mode | What happens | Network |
| --- | --- | --- |
| `"auto"` (default) | Uses a local server if one answers `/v2/languages` within 300 ms; otherwise no check. Never picks the public API. | Local only |
| `"local"` | Uses the local server. | Local only |
| `"public"` | Uses `https://api.languagetool.org/v2/check`. | **Your text is sent to languagetool.org** |
| `"off"` | No check. | None |

When no server is found there is no check and no network traffic.

Other `build.toml` keys: `grammar_url` (local server) and `disabled_rules` (LanguageTool rule ids to ignore). See [build.toml](../reference/build-toml.md).

## Run a local server

Your text never leaves your machine, and local requests bypass any proxy. Start one of these:

```bash
docker run -d -p 8081:8010 erikvl87/languagetool
```

```bash
java -cp languagetool-server.jar org.languagetool.server.HTTPServer --port 8081
```

The second uses the LanguageTool zip. The URL is `grammar_url` in `build.toml`, else the `LANGUAGETOOL_URL` environment variable, else `http://localhost:8081`.

## Use the public API

The public API is opt-in only:

- It is never chosen automatically; you must set `grammar = "public"` or pick it in Settings > Grammar.
- The editor asks for confirmation when you choose public mode.
- While sharing, it is refused for everyone, `grammar = "public"` in `build.toml` included, unless the owner allowed it in Settings > Grammar.
- In GitHub Actions, `grammar = "public"` is ignored unless `GRAMMAR_PUBLIC_OK=1` is set too.

Limits:

- Requests are paragraph batches of at most 15 KB.
- They are throttled below the free tier (20 requests and 75 KB a minute).
- Results are cached by paragraph, so re-checking an edited file only sends the changed paragraphs.

## Check in CI or on the command line

```bash
python scripts/ci_report.py lint --grammar
```

This adds the findings to the lint report, annotations and `.lint-baseline` like the other lint findings, as notices. A server that cannot be reached is skipped with a message. The workflow does not pass `--grammar`.

## Check in the editor

The Problems panel has a Grammar tab.

- The open `.tex` file is checked 2 s after you stop typing, using the text of the buffer, saved or not.
- Findings are wavy underlines. Hover one to see the message and up to three replacements to click.
- No replacement is offered where the span holds commands or math.

Settings > Grammar is owner-only. It sets:

- the mode: automatic, off, local or public
- the local server URL
- whether the public API may be used while sharing

## Next

- [build.toml reference](../reference/build-toml.md)
- [CI](ci.md)
- [Writing in the editor](../editor/writing.md)
