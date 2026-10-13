# AI assistant

This page explains the editor's AI assistant: what it does, what it sends where, how keys are kept, its limits and providers.

The assistant lives in the sidebar's Assistant tab (or "Toggle AI assistant" and the "AI: ..." commands in Ctrl+K). It uses the Anthropic Messages API by default, or an OpenAI-compatible endpoint you choose.

## Turn it on

The assistant is **off until the owner turns it on** in the panel's settings. Turning it on asks for confirmation, because **the text you ask about is sent to Anthropic** (`api.anthropic.com`), or to the provider you chose.

It needs an API key: a Claude subscription cannot be used here. With a subscription, connect Claude to the [MCP server](../guides/mcp.md) instead.

## What is sent

Nothing is sent until you click. Then only this is sent:

| Action | Sent |
|---|---|
| Selection actions | The selection and 2,000 characters around it |
| Ask | The open file and the outline |
| Explain | The build error, about 40 lines of log, the source around the error line, and the top of `main.tex` |
| Ask with "Whole project" | The open file plus up to 12 matching passages (see below) |

`build.toml`, `latexmkrc` and `.latexmkrc` are never sent and never edited this way, for anyone (a build can print any file name into its log).

## What it can do

- **Selection actions:** Rewrite, Shorten, Fix grammar, Translate (into a language you name).
- **Write LaTeX at cursor:** turns a description (a table, an equation, a TikZ figure) into LaTeX.
- **Ask:** answers questions about the open file, with the last six turns as context.
- **Explain with AI** in a Problems row (or "Explain first error"): explains a build error and may propose a fix.
- **Whole project:** with "Whole project" ticked, Ask also searches the document's other text files (`.tex`, `.bib`, `.sty`, `.cls`, ...; up to 200 files and 3 million characters, read in the browser, unsaved text included). The open file plus the 12 passages of 30 lines that share the most rare words with the question (40,000 characters at most) go to the model, which names the file and lines it draws on. This is plain keyword scoring: no index, nothing stored.
- **Find claims needing citations:** lists sentences of the open file that state facts or prior work without a citation, each with a reason and a jump link. Its "Search Crossref" button sends only the suggested search phrase to Crossref (edit role, the Look up rate limits) and shows up to five real records as candidates; "Add to .bib" inserts the one you pick. The model never names a reference itself.
- **LaTeX from image...** (or pasting an image into the prompt): turns a picture of an equation, table or diagram into LaTeX, shown as a diff to insert at the cursor. PNG, JPEG, GIF or WebP. The browser scales it to at most 2576 pixels on the long side and re-encodes it; the server refuses more than 4 MB of base64 or a file whose bytes do not match its type.

## Nothing is applied until you click

- Every answer is shown as a diff first. **Nothing is applied until you click** Replace selection, Insert at cursor or Apply. The change then goes into the editor like typing (undo, co-editing and autosave as usual).
- If the text changed in the meantime, the change is refused.
- Answers are plain text, never HTML.
- Proposed fixes must quote text that occurs once in the part of the file the model saw, and may only touch files that were sent.
- New LaTeX that looks like it runs programs or writes files (`\write18`, `\directlua`, `luacode`, `\openout`, `\input|`) is dropped. That filter is best effort: TeX can spell anything indirectly, so read a diff before applying it.
- The document is treated as data: instructions inside it are not followed.

## Streaming and Stop

- Answers stream: the explanation appears as the model writes it. The diff and Apply buttons appear only once the whole reply has been checked. When hosted, the gateway streams the answer itself.
- **Stop** ends the request, and the server closes the connection to Anthropic at once. Tokens already generated are still billed.
- A refused or cut-off answer replaces the partial text with the error.
- A failed connection is reported, never retried, because the first request may already have reached Anthropic.
- A server without streaming answers with its plain reply, and a tunnel that buffers shows the answer at the end.

## Store the key

Use one of:

- `ANTHROPIC_API_KEY` in the environment of `serve.py`. It is taken out of the environment at start, so builds do not inherit it.
- Paste it into the settings, which store it in `~/.config/latex-pipeline/ai.json` (or under `$XDG_CONFIG_HOME`), mode 600, outside every project.

The server never sends the key to a browser or over the co-editing channel.

> **Warning:** the key is still on this computer. Without `--sandbox`, code that runs in a build as you (LuaLaTeX from an edit link while sharing) can read `ai.json`, or the original environment in `/proc`. The Share dialog says so when a key is set.

## Models and providers

| Provider | Models | Key |
|---|---|---|
| Anthropic (default) | `claude-opus-5-5` (default), `claude-sonnet-5-5`, `claude-haiku-5-5`; no others are accepted | `ANTHROPIC_API_KEY` or `ai.json` |
| OpenAI-compatible | Any model name | `OPENAI_API_KEY` (taken out of the environment at start like the Anthropic key) or `ai.json`; a local server may need none |

For "OpenAI-compatible", set in the settings' Provider:

- a base URL, such as `https://api.openai.com/v1`, OpenRouter, Mistral, or a model server on your own computer or network such as Ollama `http://localhost:11434/v1`. `http://` is allowed only for loopback and private addresses;
- a model name;
- a key.

Requests use Chat Completions with a strict JSON schema (`response_format`) and the same caps, checks and streaming. An endpoint that does not report usage is counted as a whole reply. A model server on a local address is for the owner only: shared links never use it.

## Limits

| Limit | Value |
|---|---|
| Requests | 20 a minute for the whole server |
| Reply size | 16,000 tokens, thinking included |
| File size | 120,000 characters |
| Whole project | 200 files, 3 million characters; 40,000 characters of passages sent |
| Image | 2576 pixels on the long side; 4 MB of base64 |

## Next

- [MCP: use with AI clients](../guides/mcp.md)
- [Sharing](sharing.md)
- [Security model](../explanation/security.md)
