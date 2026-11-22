import contextlib
import datetime as dt
import io
import json
import re
import tempfile
import unittest
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path
from unittest import mock

from jared import __main__ as cli, archive as archive_mod, config, paperless
from jared.archive import CHUNK, Archive, Rejected
from jared.bridge import MAX_TEXT, TOOLS, Bridge, make_handler, tools
from jared.paperless import Client, NotFound, PaperlessError

TOKEN = "paperless" + "t" * 32
SECRET_TEXT = "Zebrakuchen Leuchtturm 7391"


class FakePaperless:
    """The Paperless API as far as Jared uses it. Records every request; answers only GET."""

    def __init__(self):
        self.requests = []
        self.types = [{"id": 1, "name": "Rechnung", "document_count": 2}, {"id": 2, "name": "Vertrag", "document_count": 1}]
        self.tags = [{"id": 10, "name": "Posteingang", "is_inbox_tag": True, "parent": None, "document_count": 3},
                     {"id": 11, "name": "Mietshaus", "is_inbox_tag": False, "parent": None, "document_count": 2},
                     {"id": 12, "name": "Instandhaltung", "is_inbox_tag": False, "parent": 11, "document_count": 1},
                     {"id": 13, "name": "_owner", "is_inbox_tag": False, "parent": None, "document_count": 3},
                     {"id": 14, "name": "_intern", "is_inbox_tag": False, "parent": 11, "document_count": 1},
                     {"id": 15, "name": "Dach", "is_inbox_tag": False, "parent": 12, "document_count": 1}]
        self.senders = [{"id": 20, "name": "Malerbetrieb Pinselstrich", "document_count": 1},
                        {"id": 21, "name": "Finanzamt Musterstadt", "document_count": 1},
                        {"id": 22, "name": "Finanzamt Beispieldorf", "document_count": 0}]
        self.fields = [{"id": 1, "name": "Kündbar bis", "data_type": "date"},
                       {"id": 2, "name": "Betrag", "data_type": "monetary"},
                       {"id": 3, "name": "Objekt", "data_type": "select",
                        "extra_data": {"select_options": [{"id": "aB3x", "label": "Haus"}, {"id": "q9", "label": "Wohnung"}]}},
                       {"id": 4, "name": "Notiz", "data_type": "longtext"}]
        self.docs = {
            5: {"id": 5, "title": "Rechnung Treppenhaus", "created": "2025-09-03", "added": "2026-11-19T15:20:00+01:00",
                "correspondent": 20, "document_type": 1, "tags": [10, 11, 12, 13],
                "custom_fields": [{"field": 2, "value": "EUR1240.00"}], "original_file_name": "maler.pdf",
                "page_count": 1, "archive_serial_number": None, "content": f"Rechnung {SECRET_TEXT}",
                "__search_hit__": {"highlights": "Treppenhaus <span class=\"match\">streichen</span> &amp; mehr"}},
            6: {"id": 6, "title": "Stromvertrag", "created": "2025-02-20", "added": "2026-11-19T15:21:00+01:00",
                "correspondent": None, "document_type": 2, "tags": [10, 13],
                "custom_fields": [{"field": 1, "value": "2026-12-01"}], "content": "x" * (CHUNK * 2 + 10)},
            7: {"id": 7, "title": "Alter Vertrag", "created": "2020-01-01", "correspondent": 21, "document_type": 2,
                "tags": [10], "custom_fields": [{"field": 1, "value": "2026-11-01"}, {"field": 2, "value": None},
                                                {"field": 3, "value": "aB3x"}],
                "content": "   "},
            8: {"id": 8, "title": "Ganz alter Vertrag", "created": "2019-01-01", "correspondent": None,
                "document_type": 2, "tags": [], "custom_fields": [{"field": 1, "value": "2026-09-01"}], "content": "x"},
        }

    def send(self, url, headers, timeout):
        u = urllib.parse.urlsplit(url)
        q = dict(urllib.parse.parse_qsl(u.query))
        self.requests.append((u.path, q, headers))
        assert u.scheme == "http" and u.hostname == "127.0.0.1", url
        assert headers["Authorization"] == f"Token {TOKEN}"
        lists = {"/api/document_types/": self.types, "/api/tags/": self.tags, "/api/correspondents/": self.senders,
                 "/api/custom_fields/": self.fields}
        if u.path in lists:
            return 200, json.dumps({"count": len(lists[u.path]), "next": None, "results": lists[u.path]}).encode()
        if u.path == "/api/statistics/":
            return 200, json.dumps({"documents_total": 3, "documents_inbox": 3}).encode()
        if u.path == "/api/documents/":
            docs = list(self.docs.values())
            if "custom_field_query" in q:  # [field id, "range", [from, to]], as Paperless 3.x filters it
                fid, op, (low, high) = json.loads(q["custom_field_query"])
                assert op == "range" and q.get("ordering") == f"custom_field_{fid}"
                docs = [d for d in docs if any(f["field"] == fid and low <= str(f["value"]) <= high
                                               for f in d["custom_fields"])]
            if int(q.get("page", 1)) > 1:
                return 404, b""
            return 200, json.dumps({"count": len(docs), "next": None, "results": docs}).encode()
        parts = u.path.strip("/").split("/")
        if parts[:2] == ["api", "documents"] and parts[2].isdigit():
            d = self.docs.get(int(parts[2]))
            if d is None:
                return 404, b""
            if parts[3:] == ["notes"]:
                return 200, json.dumps([{"note": "Bezahlt am 10.09.", "created": "2025-09-10T10:00:00Z"}]).encode()
            return 200, json.dumps(d).encode()
        return 404, b""

    def last(self, path):
        return [q for p, q, _ in self.requests if p == path][-1]


class Base(unittest.TestCase):
    def setUp(self):
        self.fake = FakePaperless()
        self.client = Client("http://127.0.0.1:8000", TOKEN, send=self.fake.send)
        self.archive = Archive(self.client, "https://archiv.example:8443", today=lambda: dt.date(2026, 11, 22))


def capture(handler):
    """Runs handler and returns what it logged to stderr (an unhandled error would print a traceback there)."""
    err = io.StringIO()
    with contextlib.redirect_stderr(err):
        handler()
    return err.getvalue()


class ClientTests(Base):
    def test_only_allowed_paths(self):
        for path in ("/api/documents/1/download/", "/api/documents/bulk_edit/", "/api/users/", "/api/token/",
                     "/api/documents/1/../../users/", "/api/documents/?x=1", "/admin/"):
            with self.subTest(path=path), self.assertRaises(PaperlessError):
                self.client.get(path)
        self.assertEqual(self.fake.requests, [])

    def test_there_is_no_way_to_write(self):
        self.assertEqual({n for n in vars(Client) if not n.startswith("_")}, {"get", "pages"})
        import inspect
        source = inspect.getsource(paperless)
        self.assertIn('method="GET"', source)
        for word in ('"POST"', '"PATCH"', '"PUT"', '"DELETE"', "data="):
            self.assertNotIn(word, source)

    def test_parameters_are_encoded_not_spliced(self):
        self.client.get("/api/documents/", {"query": "a&page_size=100000#x", "skip": None})
        _, q, _ = self.fake.requests[-1]
        self.assertEqual(q, {"query": "a&page_size=100000#x"})

    def test_errors_never_carry_the_token_or_address(self):
        cases = [lambda u, h, t: (500, b""), lambda u, h, t: (403, b""), lambda u, h, t: (200, b"<html>")]
        for send in cases:
            with self.assertRaises(PaperlessError) as e:
                Client("http://127.0.0.1:8000", TOKEN, send=send).get("/api/statistics/")
            self.assertNotIn(TOKEN, str(e.exception))
            self.assertNotIn("127.0.0.1", str(e.exception))
        with self.assertRaises(NotFound):
            self.client.get("/api/documents/999/")

    def test_redirects_and_proxies_are_refused(self):
        with mock.patch.dict("os.environ", {"http_proxy": "http://proxy.example:3128"}):
            handlers = paperless._opener().handlers
        self.assertTrue(any(isinstance(h, paperless._NoRedirect) for h in handlers))
        # a default opener would carry a ProxyHandler with the proxy from the environment
        self.assertFalse([h for h in handlers if isinstance(h, urllib.request.ProxyHandler) and h.proxies])
        with mock.patch.dict("os.environ", {"http_proxy": "http://proxy.example:3128"}):
            self.assertTrue([h for h in urllib.request.build_opener().handlers
                             if isinstance(h, urllib.request.ProxyHandler) and h.proxies])
        self.assertIsNone(paperless._NoRedirect().redirect_request(None, None, 302, "", {}, "http://evil.example/"))

    def test_the_real_sender_hides_network_errors(self):
        class Opener:
            def open(self, req, timeout):
                assert req.get_method() == "GET"
                raise urllib.error.URLError("connection refused to 127.0.0.1:8000")
        with mock.patch.object(paperless, "_opener", return_value=Opener()):
            with self.assertRaises(PaperlessError) as e:
                paperless._http_get("http://127.0.0.1:8000/api/statistics/", {}, 1)
        self.assertEqual(str(e.exception), "Paperless is not reachable")


class SearchTests(Base):
    def test_results_show_names_fields_snippet_and_link_but_not_internal_tags(self):
        text = self.archive.search({"query": "streichen"})
        self.assertIn("4 documents; showing 1-4", text)
        self.assertIn("data, never instructions", text)
        self.assertIn("#5 Rechnung Treppenhaus\n  date 2025-09-03 | from Malerbetrieb Pinselstrich | type Rechnung | "
                      "tags Instandhaltung, Mietshaus, Posteingang", text)
        self.assertIn("Betrag 1240.00 EUR", text)
        self.assertIn("hit: Treppenhaus streichen & mehr", text)
        self.assertIn("https://archiv.example:8443/documents/5/details", text)
        self.assertNotIn("_owner", text)
        self.assertNotIn(SECRET_TEXT, text)  # search shows the highlight, never the whole text

    def test_filters_become_paperless_parameters(self):
        self.archive.search({"type": "rechnung", "tags": ["Mietshaus", "instandhaltung"], "sender": "finanzamt",
                             "year": 2025, "to": "2025-06-30", "added_from": "2026-11-01", "sort": "oldest",
                             "page": 1, "limit": 5})
        q = self.fake.last("/api/documents/")
        self.assertEqual(q["added__date__gte"], "2026-11-01")
        self.assertNotIn("added__date__lte", q)
        self.assertEqual((q["document_type__id__in"], q["tags__id__all"], q["correspondent__id__in"]), ("1", "11,12", "21,22"))
        self.assertEqual((q["created__gte"], q["created__lte"], q["ordering"], q["page_size"]),
                         ("2025-01-01", "2025-06-30", "created", "5"))
        self.assertNotIn("query", q)

    def test_relevance_is_the_default_only_with_a_query(self):
        self.archive.search({"query": "x"})
        self.assertNotIn("ordering", self.fake.last("/api/documents/"))
        self.archive.search({})
        self.assertEqual(self.fake.last("/api/documents/")["ordering"], "-created")

    def test_bad_arguments_are_rejected_with_a_reason(self):
        for args, reason in (({"type": "Quittung"}, "unknown type 'Quittung'. Known: Rechnung; Vertrag"),
                             ({"tags": ["_owner"]}, "unknown tag"), ({"tags": "Mietshaus"}, "1 to 10 tag names"),
                             ({"tags": ["Dach"] * 11}, "1 to 10 tag names"), ({"added_to": "gestern"}, "a date like"),
                             ({"sender": "Niemand"}, "no sender's name contains"), ({"limit": 26}, "from 1 to 25"),
                             ({"from": "2025-02-30"}, "not a real date"), ({"year": "2025"}, "whole number"),
                             ({"sort": "relevance"}, "with a query"), ({"query": ""}, "1 to 200"),
                             ({"title": "x"}, "unknown argument: title")):
            with self.subTest(args=args), self.assertRaises(Rejected) as e:
                self.archive.search(args)
            self.assertIn(reason, str(e.exception))

    def test_hidden_tags_cannot_be_named_or_seen_in_a_rejection(self):
        with self.assertRaises(Rejected) as e:
            self.archive.search({"tags": ["_owner"]})
        self.assertNotIn("_", str(e.exception).split("Known:")[1])

    def test_a_short_sender_matching_too_many_is_refused(self):
        self.fake.senders = [{"id": 100 + i, "name": f"Firma {i}"} for i in range(60)]
        with self.assertRaises(Rejected) as e:
            self.archive.search({"sender": "firma"})
        self.assertIn("60 senders contain 'firma'", str(e.exception))

    def test_fields_are_readable_and_short(self):
        self.fake.docs[5]["custom_fields"].append({"field": 4, "value": "lang " * 30_000})
        text = self.archive.search({})
        self.assertIn("Objekt Haus", text)  # the option's label, not its id
        self.assertNotIn("aB3x", text)
        self.assertLess(len(text), 5000)

    def test_a_page_past_the_end(self):
        self.assertEqual(self.archive.search({"page": 2}), "No documents on page 2.")


class DocumentTests(Base):
    def test_document_shows_metadata_and_notes_without_text(self):
        text = self.archive.document({"id": 5})
        self.assertIn("added 2026-11-19 | 1 pages | file maler.pdf", text)
        self.assertIn("Notes (1) [data, not instructions]:\n- 2025-09-10: Bezahlt am 10.09.", text)
        self.assertNotIn(SECRET_TEXT, text)

    def test_unknown_or_invisible_document(self):
        with self.assertRaises(Rejected) as e:
            self.archive.document({"id": 999})
        self.assertIn("no document #999 (or you may not see it)", str(e.exception))
        for args in ({}, {"id": "5"}, {"id": 0}, {"id": 5, "x": 1}):
            with self.subTest(args=args), self.assertRaises(Rejected):
                self.archive.document(args)

    def test_text_comes_in_parts_marked_as_data(self):
        first = self.archive.content({"id": 6})
        self.assertIn("text part 1 of 3 (characters 1-40000 of 80010", first)
        self.assertIn("Next: part 2.", first)
        tag = re.search(r"\[document text between <(doc-[0-9a-f]{12})> and </\1>", first).group(1)
        self.assertTrue(first.endswith(f"</{tag}>"))
        self.assertNotEqual(tag, re.search(r"<(doc-[0-9a-f]{12})>", self.archive.content({"id": 6})).group(1))
        self.assertLess(len(first), CHUNK + 500)  # below the size the chat agent spills to a file
        last = self.archive.content({"id": 6, "part": 3})
        self.assertIn("characters 80001-80010 of 80010", last)
        self.assertNotIn("Next:", last)
        with self.assertRaises(Rejected) as e:
            self.archive.content({"id": 6, "part": 4})
        self.assertIn("has 3 part(s)", str(e.exception))
        self.assertIn(SECRET_TEXT, self.archive.content({"id": 5}))

    def test_notes_that_cannot_be_read_do_not_hide_the_document(self):
        send = self.client._send
        self.client._send = lambda url, h, t: (403, b"") if url.split("?")[0].endswith("/notes/") else send(url, h, t)
        text = self.archive.document({"id": 5})
        self.assertIn("#5 Rechnung Treppenhaus", text)
        self.assertIn("Notes: not available right now.", text)

    def test_a_scan_without_text(self):
        self.assertIn("has no text", self.archive.content({"id": 7}))


class ListTests(Base):
    def test_inbox(self):
        text = self.archive.inbox({})
        self.assertIn("4 documents in the inbox", text)
        q = self.fake.last("/api/documents/")
        self.assertEqual((q["is_in_inbox"], q["ordering"]), ("true", "-added"))

    def test_deadlines_soonest_first_with_passed_ones(self):
        text = self.archive.deadlines({})
        self.assertIn("2 deadline(s), today is 2026-11-22", text)
        self.assertLess(text.index("2026-11-01 (PASSED)"), text.index("2026-12-01 (in 9 days)"))
        self.assertIn("Kündbar bis 2026-12-01", text)
        self.assertNotIn("2026-09-01", text)  # passed more than 30 days ago
        q = self.fake.last("/api/documents/")
        self.assertEqual(json.loads(q["custom_field_query"]), [1, "range", ["2026-10-23", "2027-11-22"]])
        soon = self.archive.deadlines({"days": 5})
        self.assertIn("1 deadline(s)", soon)  # only the passed one; 2026-12-01 is 9 days away
        self.assertNotIn("2026-12-01", soon)

    def test_overview_lists_labels_without_internal_tags(self):
        text = self.archive.overview({})
        self.assertIn("Documents you can see: 3, in the inbox: 3.", text)
        self.assertIn("Types (documents): Rechnung (2); Vertrag (1)", text)
        self.assertIn("- Mietshaus (2)\n  - Instandhaltung (1)\n    - Dach (1)", text)  # every level
        self.assertNotIn("_owner", text)
        self.assertNotIn("_intern", text)
        self.assertIn("Senders: 3", text)
        self.assertEqual(self.archive.overview({"sender": "finanz"}),
                         "Senders matching: Finanzamt Beispieldorf (0); Finanzamt Musterstadt (1)")

    def test_old_label_names_stay_when_a_refresh_fails(self):
        now = [0.0]
        a = Archive(self.client, today=lambda: dt.date(2026, 11, 22), clock=lambda: now[0])
        a.search({})
        now[0] += archive_mod.LABEL_TTL + 1
        send = self.client._send
        self.client._send = lambda url, h, t: (500, b"") if "/api/tags/" in url else send(url, h, t)
        self.assertIn("type Rechnung", a.search({}))

    def test_labels_are_cached_for_a_while(self):
        now = [0.0]
        a = Archive(self.client, today=lambda: dt.date(2026, 11, 22), clock=lambda: now[0])
        a.search({})
        a.search({})
        self.assertEqual(sum(p == "/api/tags/" for p, _, _ in self.fake.requests), 1)
        now[0] += archive_mod.LABEL_TTL + 1
        a.search({})
        self.assertEqual(sum(p == "/api/tags/" for p, _, _ in self.fake.requests), 2)


class FakeConnection:
    """Stands in for the client socket, so the request handler runs without any network."""

    def __init__(self, raw: bytes):
        self._in, self.sent = io.BytesIO(raw), io.BytesIO()

    def makefile(self, mode, *args, **kwargs):
        return self._in if "r" in mode else self.sent

    def sendall(self, data):
        self.sent.write(data)

    def settimeout(self, seconds):
        self.timeout = seconds


class BridgeTests(Base):
    def setUp(self):
        super().setUp()
        self.logged = []
        self.bridge = Bridge(self.archive, log=self.logged.append)
        self.handler = make_handler(self.bridge, {"merlin": "T" * 40})

    def call(self, name, args=None):
        out = self.bridge.handle({"jsonrpc": "2.0", "id": 1, "method": "tools/call",
                                  "params": {"name": name, "arguments": args or {}}})
        return out["result"]["content"][0]["text"], out["result"]["isError"]

    def test_tools_only_read(self):
        names = [t["name"] for t in TOOLS]
        self.assertEqual(names, ["archiv_suchen", "archiv_dokument", "archiv_inhalt", "archiv_posteingang",
                                 "archiv_fristen", "archiv_ueberblick", "jared_update_check", "jared_update_apply"])
        for word in ("upload", "add", "delete", "change", "edit", "create", "ablegen"):
            self.assertFalse(any(word in n for n in names), word)
        for t in TOOLS:
            self.assertLessEqual(len(t["description"].split(". ")[0]), 200, t["name"])  # first sentence = search text
            self.assertFalse(t["inputSchema"].get("additionalProperties", True))
        self.assertIn('"Frist"', tools("Frist")[4]["description"])
        listed = self.bridge.handle({"jsonrpc": "2.0", "id": 1, "method": "tools/list"})["result"]["tools"]
        self.assertIn('"Kündbar bis"', listed[4]["description"])

    def test_answers_stay_below_the_spill_limit(self):
        self.archive.overview = lambda args: "x" * 100_000
        text, error = self.call("archiv_ueberblick")
        self.assertFalse(error)
        self.assertLess(len(text), MAX_TEXT + 200)
        self.assertIn("answer was too long", text)

    def test_a_tool_call_and_what_is_logged(self):
        text, error = self.call("archiv_inhalt", {"id": 5})
        self.assertFalse(error)
        self.assertIn(SECRET_TEXT, text)
        self.call("archiv_suchen", {"query": SECRET_TEXT})
        self.call("archiv_suchen", {"sender": SECRET_TEXT})
        self.assertTrue(self.logged)
        for line in self.logged:
            self.assertNotIn(SECRET_TEXT, line)
            self.assertNotIn(TOKEN, line)

    def test_rejections_and_failures_are_tool_errors(self):
        text, error = self.call("archiv_dokument", {"id": 999})
        self.assertEqual((error, text), (True, "rejected: no document #999 (or you may not see it)"))
        self.client._send = lambda *a: (_ for _ in ()).throw(PaperlessError("Paperless is not reachable"))
        text, error = self.call("archiv_posteingang")
        self.assertEqual((error, text), (True, "failed: Paperless is not reachable"))
        self.client._send = lambda *a: (_ for _ in ()).throw(KeyError("/home/jared/.config/jared/paperless-token"))
        text, error = self.call("archiv_fristen")
        self.assertEqual((error, text), (True, "failed: KeyError"))

    def test_unknown_tool_and_methods(self):
        out = self.bridge.handle({"jsonrpc": "2.0", "id": 1, "method": "tools/call",
                                  "params": {"name": "archiv_ablegen", "arguments": {}}})
        self.assertEqual(out["error"]["code"], -32602)
        self.assertEqual(self.bridge.handle({"jsonrpc": "2.0", "id": 2, "method": "resources/list"})["error"]["code"], -32601)
        init = self.bridge.handle({"jsonrpc": "2.0", "id": 3, "method": "initialize", "params": {"protocolVersion": "2025-06-18"}})
        self.assertEqual(init["result"]["serverInfo"]["name"], "jared")
        self.assertEqual(init["result"]["protocolVersion"], "2025-06-18")

    def request(self, method="POST", path="/mcp", token="T" * 40, body=None, headers=None):
        payload = json.dumps(body if body is not None else {"jsonrpc": "2.0", "id": 1, "method": "ping"}).encode()
        hdrs = {"Host": "127.0.0.1:8769", "Content-Type": "application/json", "Content-Length": str(len(payload))}
        if token:
            hdrs["Authorization"] = f"Bearer {token}"
        hdrs.update(headers or {})
        raw = (f"{method} {path} HTTP/1.1\r\n".encode() + "".join(f"{k}: {v}\r\n" for k, v in hdrs.items()).encode()
               + b"\r\n" + payload)
        conn = FakeConnection(raw)
        self.handler(conn, ("127.0.0.1", 50000), None)
        head, _, rest = conn.sent.getvalue().partition(b"\r\n\r\n")
        return int(head.split()[1]), rest

    def test_http_guards(self):
        self.assertEqual(self.request()[0], 200)
        self.assertEqual(self.request(token="x" * 40)[0], 401)
        self.assertEqual(self.request(token=None)[0], 401)
        self.assertEqual(self.request(headers={"Host": "evil.example"})[0], 403)
        self.assertEqual(self.request(headers={"Origin": "https://evil.example"})[0], 403)
        self.assertEqual(self.request(path="/other")[0], 404)
        self.assertEqual(self.request(method="GET")[0], 405)
        self.assertEqual(self.request(body={"pad": "x" * 200_000})[0], 413)
        self.assertEqual(self.request(body={"jsonrpc": "2.0", "method": "notifications/initialized"})[0], 202)
        self.assertEqual(self.request(headers={"Content-Length": "-1"})[0], 400)

    def test_batches_are_small_and_odd_names_do_not_crash(self):
        ping = {"jsonrpc": "2.0", "id": 1, "method": "ping"}
        status, body = self.request(body=[ping, ping])
        self.assertEqual((status, len(json.loads(body))), (200, 2))
        self.assertEqual(self.request(body=[ping] * 11)[0], 400)
        self.assertEqual(self.request(body=[])[0], 400)
        odd = {"jsonrpc": "2.0", "id": 2, "method": "tools/call", "params": {"name": ["x"], "arguments": {}}}
        out = []
        self.assertEqual(capture(lambda: out.append(self.request(body=odd))), "")
        self.assertEqual(json.loads(out[0][1])["error"]["code"], -32602)
        self.bridge.handle = lambda m, who: (_ for _ in ()).throw(RuntimeError("boom"))
        out.clear()
        self.assertEqual(capture(lambda: out.append(self.request())), "")
        self.assertEqual(out[0][0], 500)

    def test_umlauts_travel_as_utf8(self):
        call = {"jsonrpc": "2.0", "id": 7, "method": "tools/call", "params": {"name": "archiv_fristen", "arguments": {}}}
        status, body = self.request(body=call)
        self.assertEqual(status, 200)
        self.assertIn("Kündbar bis", json.loads(body.decode("utf-8"))["result"]["content"][0]["text"])


class ConfigTests(unittest.TestCase):
    def test_paperless_section(self):
        with tempfile.TemporaryDirectory() as d:
            path = Path(d) / "config.toml"
            self.assertEqual(config.load_paperless(path), config.PaperlessConfig())
            path.write_text('[paperless]\nurl = "http://127.0.0.1:8770/"\nlink = "https://merlin.example.ts.net:8443"\n')
            cfg = config.load_paperless(path)
            self.assertEqual((cfg.url, cfg.link), ("http://127.0.0.1:8770", "https://merlin.example.ts.net:8443"))
            for bad in ('url = "https://127.0.0.1:8770"', 'url = "http://paperless.example:8000"',
                        'url = "http://user:pw@127.0.0.1:8770"', 'url = "http://127.0.0.1:8770/api?x=1"',
                        'link = "javascript:alert(1)"', 'link = "https://a.example/path?x"', 'deadline_field = ""'):
                path.write_text(f"[paperless]\n{bad}\n")
                with self.subTest(bad=bad), self.assertRaises(config.ConfigError):
                    config.load_paperless(path)

    def test_tokens(self):
        with tempfile.TemporaryDirectory() as d:
            paths = config.Paths(Path(d), Path(d) / "state")
            with self.assertRaises(config.ConfigError):
                paths.tokens()
            (Path(d) / "token-merlin").write_text("m" * 40 + "\n")
            self.assertEqual(paths.tokens(), {"merlin": "m" * 40})
            (Path(d) / "token-Bad").write_text("b" * 40)
            with self.assertRaises(config.ConfigError):
                paths.tokens()
            (Path(d) / "paperless-token").write_text("short")
            with self.assertRaises(config.ConfigError):
                config.load_token(paths.paperless_token)
            (Path(d) / "paperless-token").write_text("a b" * 20)
            with self.assertRaises(config.ConfigError):
                config.load_token(paths.paperless_token)

    def test_config_check_as_the_updater_runs_it(self):
        with tempfile.TemporaryDirectory() as d, mock.patch.dict("os.environ", {"XDG_CONFIG_HOME": d}):
            conf = Path(d) / "jared"
            conf.mkdir()
            (conf / "token-merlin").write_text("m" * 40)
            (conf / "config.toml").write_text('[update]\nrepo = "o/jared"\npublisher = "o"\n[paperless]\n'
                                              'url = "http://127.0.0.1:8770"\n')
            out = io.StringIO()
            with contextlib.redirect_stdout(out):
                self.assertEqual(cli.cmd_config_check(mock.Mock(strict=False)), 0)
            self.assertIn("config ok (clients: merlin; updates on; paperless http://127.0.0.1:8770", out.getvalue())
            with self.assertRaises(config.ConfigError):  # strict: the Paperless token is missing
                cli.cmd_config_check(mock.Mock(strict=True))
            (conf / "paperless-token").write_text("p" * 40)
            with contextlib.redirect_stdout(io.StringIO()):
                self.assertEqual(cli.cmd_config_check(mock.Mock(strict=True)), 0)

    def test_port(self):
        with tempfile.TemporaryDirectory() as d:
            path = Path(d) / "config.toml"
            self.assertEqual(config.load_port(path), 8769)
            path.write_text("[server]\nport = 80\n")
            with self.assertRaises(config.ConfigError):
                config.load_port(path)


if __name__ == "__main__":
    unittest.main()
