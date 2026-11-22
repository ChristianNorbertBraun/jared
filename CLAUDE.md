# Working on Jared

- Python 3.11+, standard library only. Do not add dependencies.
- Run the tests with `python3 -m unittest discover -s tests`. Every change needs a test.
- Tests must not need the network, not even loopback: sandboxes that run them block connections to 127.0.0.1. Paperless is `FakePaperless` in the tests; the HTTP handler is tested with a fake connection.
- The updater runs these tests inside a bubblewrap sandbox before it installs a release (home `/home/sandbox`, no access to the real home, system read-only). A test that depends on the real home or on files outside the checkout blocks every update.
- Self-update is agent-tool-selfupdate, installed next to the package and imported only in `jared/updater.py`. Do not copy it into this repository; `selfupdate.toml` names the version range. Its adapter tests are skipped without it (see README).
- A change that should reach an installation needs a new version: raise `__version__` in `jared/version.py` (patch for fixes, minor for a new feature) in the same pull request. The owner publishes the release tag by hand.
- Jared only READS. Do not add a tool, a client method or an allowed path that adds, changes or deletes anything in Paperless, and do not add a way to download original files. `jared/paperless.py` sends GET requests only, to the paths in `ALLOWED`, with parameters it encodes itself; the API must be on loopback. A test checks that the client has no other method.
- The app page (`jared/apppage.py`) answers anyone who reaches the proxy, without a token. It must stay inert: only the document number from its own address goes in, it never calls Paperless or imports the rest of Jared, runs no script and logs nothing. Keep it on its own port, never on the MCP endpoint's.
- What a client sees is what the Paperless account behind the token sees. Do not add a second token or a way to act as another Paperless user. The client is always the holder of the bearer token, never an argument.
- Document text and notes go to the client marked as data. Never log them, nor queries, nor tokens: logs carry the tool name, the client and sizes. Errors shown to a client must not contain internals (paths, addresses, tokens).
- Answers must stay below 50,000 characters (the chat agent moves larger tool results into a file): text comes in parts of `CHUNK` characters, lists are paged.
- Code, comments, commit messages and pull requests are in English. Tool names are German like the archive's labels.
