"""A Paperless-ngx API client that can only READ.

Every request is a GET to a path on a fixed list, built from numbers and url-encoded parameters, sent to the API on
this machine. Redirects and proxies are refused, so the token never leaves for another address. The Paperless
account behind the token should be view-only as well: then writing is refused twice.
"""
from __future__ import annotations

import json
import re
import urllib.error
import urllib.parse
import urllib.request

TIMEOUT = 30
MAX_RESPONSE = 20_000_000  # a page of documents with their text stays far below this

ALLOWED = [re.compile(p) for p in (
    r"/api/documents/",
    r"/api/documents/\d{1,9}/",
    r"/api/documents/\d{1,9}/notes/",
    r"/api/(tags|document_types|correspondents|custom_fields)/",
    r"/api/statistics/",
)]


class PaperlessError(Exception):
    """A failure that is safe to show and log: no token, no address, no document text."""


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *args, **kwargs):
        return None  # a 3xx becomes an HTTPError instead of a request to wherever it points


def _opener() -> urllib.request.OpenerDirector:
    """No proxy (an environment variable must not route the token elsewhere), no redirects."""
    return urllib.request.build_opener(urllib.request.ProxyHandler({}), _NoRedirect)


def _http_get(url: str, headers: dict, timeout: int) -> tuple[int, bytes]:
    req = urllib.request.Request(url, headers=headers, method="GET")
    try:
        with _opener().open(req, timeout=timeout) as resp:
            return resp.status, resp.read(MAX_RESPONSE + 1)
    except urllib.error.HTTPError as e:
        e.close()
        return e.code, b""
    except (urllib.error.URLError, OSError, TimeoutError):
        raise PaperlessError("Paperless is not reachable") from None


class Client:
    def __init__(self, base: str, token: str, send=_http_get, timeout: int = TIMEOUT):
        self.base, self._token, self._send, self.timeout = base.rstrip("/"), token, send, timeout

    def get(self, path: str, params: dict | None = None):
        """GET one allowed path; params are url-encoded here, never taken as text from a caller."""
        if not any(p.fullmatch(path) for p in ALLOWED):
            raise PaperlessError("this path is not allowed")
        query = urllib.parse.urlencode({k: v for k, v in (params or {}).items() if v is not None}, doseq=True)
        url = f"{self.base}{path}" + (f"?{query}" if query else "")
        status, body = self._send(url, {"Authorization": f"Token {self._token}", "Accept": "application/json"},
                                  self.timeout)
        if status == 404:
            raise NotFound()
        if status in (401, 403):
            raise PaperlessError(f"Paperless refused the request (HTTP {status})")
        if status != 200:
            raise PaperlessError(f"Paperless answered HTTP {status}")
        if len(body) > MAX_RESPONSE:
            raise PaperlessError("the answer from Paperless is too large")
        try:
            return json.loads(body)
        except ValueError:
            raise PaperlessError("Paperless sent an answer that is not JSON") from None

    def pages(self, path: str, params: dict | None = None, limit: int = 2000) -> list:
        """All results of a list endpoint, page by page (at most `limit` items)."""
        out, page = [], 1
        while True:
            js = self.get(path, {**(params or {}), "page": page, "page_size": 100})
            out += js.get("results") or []
            if not js.get("next") or len(out) >= limit:
                return out[:limit]
            page += 1


class NotFound(PaperlessError):
    def __init__(self):
        super().__init__("not found")
