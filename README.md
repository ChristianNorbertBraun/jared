# Jared

A read-only bridge from a chat agent to a [Paperless-ngx](https://docs.paperless-ngx.com/) document archive. The
agent searches the archive, opens a document's details and reads its text over MCP, so it can answer "when can I
cancel the electricity contract?" or "which invoices for the house came in 2025?". It cannot add, change or delete
anything.

Named after Jared Dunn (Silicon Valley), who keeps everyone's paperwork in order.

```
chat agent ── archiv_suchen / archiv_inhalt / ... ──▶ Jared (unix user jared, 127.0.0.1:8769)
                                                        │  GET only, fixed list of paths
                                                        ▼
                                                      Paperless-ngx API on the same machine (view-only account)

phone ── <link>/app/<number> ──▶ proxy ──▶ Jared's app page (127.0.0.1:8772) ── x-paperless://… ──▶ Swift Paperless
```

## Tools

| Tool | What it does |
|---|---|
| `archiv_suchen` | Full-text search plus filters (type, tags, part of the sender's name, year, date range), with paging. Each result: number, title, date, sender, type, tags, custom fields, a highlighted hit and a link. |
| `archiv_dokument` | One document's details and notes, without its text. |
| `archiv_inhalt` | A document's text in parts of 40,000 characters, marked as data. |
| `archiv_posteingang` | The documents in the inbox (not reviewed yet). |
| `archiv_fristen` | Contract deadlines from a date custom field (default `Kündbar bis`), soonest first. |
| `archiv_ueberblick` | Counts, the types and tags with their numbers, the fields, and a sender lookup. |
| `jared_update_check`, `jared_update_apply` | Installed version, newest release; install a release the owner published. |

The tool names are German because the archive's labels are.

## Read-only, twice

- Jared's Paperless client (`jared/paperless.py`) sends only `GET` requests, to paths on a fixed list, with
  parameters it url-encodes itself. It refuses redirects and ignores proxy settings, so the token is only ever sent
  to the API on this machine (the config allows only `http://` on loopback: `127.0.0.1`, `localhost` or `::1`).
- Give Jared the token of a Paperless account that may only **view** documents and labels. Then a write would be
  refused by Paperless as well.

What the agent sees is what that account sees. Tags whose name starts with `_` (for example an owner marker used
in workflows) are never shown. Document text and notes come with a reminder that they are data, not instructions.

## Links that open the app

Chat apps like Telegram only make `http(s)` links tappable, so a link into the
[Swift Paperless](https://github.com/paulgessinger/swift-paperless) iPhone app (`x-paperless://v1/document/<number>`)
arrives as plain text. With `app_links = true` the tools hand out `<link>/app/<number>` instead, and Jared serves a
small page there (on its own loopback port, `app_port`, default 8772) that jumps on to the app, with a button for the
same jump and a link to the web view as a fallback. The page knows only the number in its address: it does not call
Paperless, needs no token and shows no data. Every other path gets a bare 404 (other methods a 501), with no page
that could echo the request.

The reverse proxy in front of Paperless must send `/app` to that port. With Tailscale:

```sh
sudo tailscale serve --bg --https=8443 --set-path=/app http://127.0.0.1:8772/app
```

Swift Paperless 1.10.4 has two quirks with these links (seen on a real iPhone): when a link starts the app, it says
the document is not accessible, because it checks the user's permissions before loading them (fixed in the app's
main branch, not released yet); and opening the same document by link twice in a row does nothing the second time.
With the app already open, a link to another document opens it.

## Set up

As the service user (here `jared`, with `loginctl enable-linger jared`):

1. Install the first release with agent-tool-selfupdate ("First installation" in its README):
   `--repo <owner>/jared --publisher <login>`.
2. `~/.config/jared/config.toml` from `examples/config.toml`: `[update]`, `[paperless] url` and optionally `link`
   (the address the user opens Paperless at, used for links in the answers) and `app_links` (see above).
3. The Paperless token, piped in so it never appears on screen:
   `<command that prints the token> | sudo -u jared bash ~jared/current/setup/set-paperless-token.sh`.
   With Docker: `docker compose exec -T webserver python3 manage.py drf_create_token <user>` prints it.
4. A bearer token for the chat client: `sudo -u jared bash ~jared/current/setup/new-client-token.sh merlin`, then
   copy `~/.config/jared/token-merlin` into the client's configuration.
5. `examples/jared.service` to `~/.config/systemd/user/`, `systemctl --user enable --now jared.service`.
6. `python3 -m jared check` (with `PYTHONPATH=~/current`) asks Paperless once and prints the document count.

For a Hermes agent, in `config.yaml`:

```yaml
mcp_servers:
  jared:
    url: "http://127.0.0.1:8769/mcp"
    headers:
      Authorization: "Bearer ${JARED_BRIDGE_TOKEN}"
    timeout: 60
```

## Updates

Through [agent-tool-selfupdate](https://github.com/ChristianNorbertBraun/agent-tool-selfupdate): a release (tag
`vX.Y.Z` = `jared/version.py`) published by the configured login, then `jared_update_apply` in the chat or
`python3 -m jared update` as the service user. The release's tests run in a sandbox first; the switch is atomic and
rolls back if the service does not come up.

## Tests

```sh
python3 -m unittest discover -s tests
```

Standard library only, no network (not even loopback): Paperless is a fake, the HTTP handler gets a fake
connection. The update adapter's tests need the library next to the package (it is in `.gitignore`):

```sh
python3 -m agent_tool_selfupdate fetch --publisher ChristianNorbertBraun --into .
```

## License

MIT
