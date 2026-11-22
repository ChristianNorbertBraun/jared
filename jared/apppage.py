"""The page behind a document link that opens the document in the Swift Paperless app.

Chat apps such as Telegram only make http(s) links tappable, not the app's own `x-paperless://` links. So with
`app_links` on, the tools hand out `<link>/app/<number>`, and the reverse proxy in front of Paperless (for example
`tailscale serve --set-path=/app`) sends that path here. The answer is a small page that jumps on to
`x-paperless://v1/document/<number>`, with a button for the same jump and a link to the web view as a fallback.

The page knows nothing but the number in its address: it never calls Paperless, needs no token and shows no data.
It runs on its own loopback port, apart from the MCP endpoint, so the proxy never reaches the endpoint.
"""
from __future__ import annotations

import re
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

PATH = re.compile(r"/app/([1-9][0-9]{0,8})/?")

HEADERS = (
    ("Content-Type", "text/html; charset=utf-8"),
    ("Cache-Control", "no-store"),
    ("X-Content-Type-Options", "nosniff"),
    ("Referrer-Policy", "no-referrer"),
    ("Content-Security-Policy", "default-src 'none'; style-src 'unsafe-inline'; base-uri 'none'; form-action 'none'; "
                                "frame-ancestors 'none'"),
)

_PAGE = """<!doctype html>
<html lang="de"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<meta name="color-scheme" content="light dark">
<meta http-equiv="refresh" content="0;url={app}">
<title>Dokument {id}</title>
<style>
body{{font-family:-apple-system,system-ui,sans-serif;margin:0;padding:48px 16px;text-align:center;background:#f6f6f4;color:#1b1b1b}}
p{{margin:20px 0}}
a.app{{display:inline-block;padding:14px 22px;border-radius:12px;background:#17541f;color:#fff;text-decoration:none;font-size:18px}}
a.web{{color:#555}}
@media (prefers-color-scheme:dark){{body{{background:#161616;color:#eee}}a.web{{color:#aaa}}}}
</style></head><body>
<p>Dokument {id}</p>
<p><a class="app" href="{app}">In Swift Paperless öffnen</a></p>
<p><a class="web" href="/documents/{id}/details">Im Browser öffnen</a></p>
</body></html>
"""


def app_url(doc_id: int) -> str:
    return f"x-paperless://v1/document/{doc_id}"


def page(doc_id: int) -> bytes:
    """The page for one document: only its number goes in (an int, so nothing to escape)."""
    return _PAGE.format(id=int(doc_id), app=app_url(int(doc_id))).encode()


def make_handler() -> type[BaseHTTPRequestHandler]:
    class Handler(BaseHTTPRequestHandler):
        timeout = 10  # seconds a client may take to send its request
        server_version, sys_version = "jared", ""

        def log_message(self, *args):  # the address holds a document number: keep it out of the log
            pass

        def send_error(self, code, message=None, explain=None):
            """Every refusal (404, and the stdlib's 400/414/501) is a bare status: no page that echoes the request."""
            self.send_response(code)
            self.send_header("Content-Length", "0")
            self.send_header("Connection", "close")
            self.end_headers()

        def _answer(self, with_body: bool) -> None:
            m = PATH.fullmatch(self.path)
            if not m:
                self.send_error(404)
                return
            body = page(int(m.group(1)))
            self.send_response(200)
            for k, v in HEADERS:
                self.send_header(k, v)
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            if with_body:
                self.wfile.write(body)

        def do_GET(self):
            self._answer(True)

        def do_HEAD(self):
            self._answer(False)

    return Handler


class _Server(ThreadingHTTPServer):
    daemon_threads = True

    def handle_error(self, request, client_address):  # a client hanging up is no news; keep the log empty
        pass


def serve(port: int) -> ThreadingHTTPServer:
    """Start the page on 127.0.0.1 in a daemon thread. Call .shutdown() to stop it."""
    server = _Server(("127.0.0.1", port), make_handler())
    threading.Thread(target=server.serve_forever, name="apppage", daemon=True).start()
    return server
