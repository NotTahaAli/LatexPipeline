# Sharing a document

How to share a document from the local editor with other people through links, and what each link lets them do.

## Start sharing

Run this, or press Share in the top bar:

```
python scripts/serve.py --share [auto|local|cloudflared|ngrok|localtunnel|pinggy|localhost.run]
```

It starts a tunnel and prints two links for the first document:

| Link | What it allows |
|---|---|
| View link | Read-only source, PDF and outline |
| Edit link | Edit the document's files and rebuild it |

| Option | Meaning |
|---|---|
| `auto` | Picks the first installed of cloudflared, ngrok, npx localtunnel, then ssh (pinggy, localhost.run); with none installed it tells you what to install |
| `local` | Token links without a tunnel (same network or your own tunnel) |
| `cloudflared`, `ngrok`, `localtunnel`, `pinggy`, `localhost.run` | That tunnel |

No link works after you stop sharing or quit. Treat the edit link like a password and give it only to people you trust; read the [security model](../explanation/security.md) first.

## Install a tunnel

| Tunnel | Install |
|---|---|
| [cloudflared](https://developers.cloudflare.com/cloudflare-one/networks/connectors/cloudflare-tunnel/downloads/) | No account; the default |
| [ngrok](https://ngrok.com/download) | Free account and `ngrok config add-authtoken` |
| localtunnel | Node.js |
| pinggy, localhost.run | ssh |

- The ssh and ngrok parsers follow each tool's documented output but are untested here; if one fails, please report its output.
- ngrok's free plan shows a warning page on the first visit; each visitor clicks Visit Site once.
- localtunnel may ask for a tunnel password (your public IP).
- Cloudflare quick tunnels do not support Server-Sent Events. The editor uses WebSocket with a long-poll fallback, so it works.

## Test the tunnel before a session

```
python scripts/serve.py --share-selftest [--share PROVIDER]
```

This starts the tunnel and checks `/api/health`, a WebSocket echo and a 30 s long-poll through the public URL, then prints a table. Run it on your own machine; it needs outbound access that restricted networks do not give.

## Make a link for a named person

The Share dialog can make a link per person, for example "Alice", can edit; "Bob", can review; "Reviewer 2", view only.

- Each link has its own token.
- Comments, suggestions, history authors and presence show the name you gave it. The server sets that name; the person cannot change it, and nothing the browser sends (no header, field or client id) can.
- People on the anonymous view and edit links pick their own name, which everyone sees marked "(unverified name)" ("Guest (unverified name)" without one).

| Action | Effect |
|---|---|
| Revoke | Ends a named link at once: its open tabs are disconnected and its cookie stops working; the other links keep working. A revoke made from another running editor holds at once |
| New links | Replaces only the anonymous pair, and disconnects everyone on the old ones |

Named links are kept in `share-links.json` in your config folder (`$XDG_CONFIG_HOME` or `~/.config`, in `latex-pipeline`; mode 600, folder 700; never in a document directory). They work again each time you share the same document of the same checkout, until you revoke them. A link is bound to the document's name and the full path of its `main.tex`, so a clone elsewhere with a document of the same name does not accept it.

Hosted mode has accounts instead and ignores named links.

## Roles

| Role | Can |
|---|---|
| View | Open existing files of the shared document, the PDF and outline; browse history and comments |
| Review | Comment and suggest, but never change the text |
| Edit | Create and change any file of the shared document and rebuild it, except `build.toml`, `latexmkrc` and `.latexmkrc` |

The full table of what each role may do is in the [security model](../explanation/security.md).

## Request access

On a view or review link, "Request access" asks the owner for the edit or review role, with an optional note.

- **Locally**, the owner sees requests in the Share dialog. Grant makes a named link with that role, which the visitor's browser opens. Requests are kept in memory only (lost when the editor stops; at most 50, a week each). Requests are limited to 5 a minute.
- **Hosted**, the workspace admins see requests on the workspace page, and Grant changes the member's role.

## Next

- [Security model](../explanation/security.md)
- [Working together](collaboration.md)
- [Version history](history.md)
