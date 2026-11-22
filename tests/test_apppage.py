import ast
import contextlib
import datetime as dt
import io
import tempfile
import threading
import unittest
from pathlib import Path
from unittest import mock

from jared import __main__ as cli, apppage, config
from jared.archive import Archive
from jared.paperless import Client

from test_jared import TOKEN, FakeConnection, FakePaperless


def request(path: str, method: str = "GET") -> tuple[int, dict, bytes]:
    """One request through the page's handler over a fake connection; returns status, headers and body."""
    conn = FakeConnection(f"{method} {path} HTTP/1.1\r\nHost: merlin.example.ts.net:8443\r\n\r\n".encode())
    apppage.make_handler()(conn, ("127.0.0.1", 50000), None)
    head, _, body = conn.sent.getvalue().partition(b"\r\n\r\n")
    lines = head.decode().split("\r\n")
    headers = dict(line.split(": ", 1) for line in lines[1:])
    return int(lines[0].split()[1]), headers, body


class PageTests(unittest.TestCase):
    def test_a_document_number_gets_the_jump_page(self):
        status, headers, body = request("/app/252")
        self.assertEqual(status, 200)
        page = body.decode()
        self.assertIn('<meta http-equiv="refresh" content="0;url=x-paperless://v1/document/252">', page)
        self.assertIn('href="x-paperless://v1/document/252">In Swift Paperless öffnen</a>', page)
        self.assertIn('href="/documents/252/details">Im Browser öffnen</a>', page)
        self.assertEqual(int(headers["Content-Length"]), len(body))
        self.assertEqual(request("/app/252/")[0], 200)

    def test_headers_keep_the_page_private_and_inert(self):
        _, headers, _ = request("/app/7")
        self.assertEqual(headers["Content-Type"], "text/html; charset=utf-8")
        self.assertEqual(headers["Cache-Control"], "no-store")
        self.assertEqual(headers["Referrer-Policy"], "no-referrer")
        self.assertEqual(headers["X-Content-Type-Options"], "nosniff")
        csp = headers["Content-Security-Policy"]
        self.assertIn("default-src 'none'", csp)
        self.assertNotIn("script-src", csp)  # no script may run on the page

    def test_everything_but_a_number_is_not_found(self):
        for path in ("/app/", "/app/abc", "/app/0", "/app/0252", "/app/1234567890", "/app/252?x=1", "/app/252#x",
                     "/app/252/details", "/app/-1", "/app/2%2F..%2F", "/app/../api/documents/", "/252", "/",
                     "/mcp", "/api/documents/", "/documents/252/details"):
            with self.subTest(path=path):
                status, _, body = request(path)
                self.assertEqual(status, 404)
                self.assertEqual(body, b"")

    def test_head_has_no_body_and_other_methods_are_refused(self):
        status, headers, body = request("/app/9", method="HEAD")
        self.assertEqual((status, body), (200, b""))
        self.assertGreater(int(headers["Content-Length"]), 0)
        for method in ("POST", "PUT", "DELETE"):
            with self.subTest(method=method):
                status, _, body = request("/app/9", method=method)
                self.assertEqual((status, body), (501, b""))

    def test_refusals_never_echo_the_request(self):
        def send(raw: bytes) -> bytes:
            conn = FakeConnection(raw)
            apppage.make_handler()(conn, ("127.0.0.1", 50000), None)
            return conn.sent.getvalue()

        sent = send(b"GET /app/<b>x</b> HTTP/9\r\n\r\n")  # a bad version is answered like HTTP/0.9: no headers
        self.assertNotIn(b"<b>", sent)
        self.assertNotIn(b"HTTP/9", sent)
        sent = send(b"GET /app/1 HTTP/1.1\r\n" + b"".join(b"X-%d: <b>\r\n" % i for i in range(101)) + b"\r\n")
        self.assertTrue(sent.startswith(b"HTTP/1.0 431 "), sent[:40])
        self.assertTrue(sent.endswith(b"Content-Length: 0\r\nConnection: close\r\n\r\n"))  # headers only
        self.assertNotIn(b"<", sent)
        self.assertNotIn(b"Python", request("/app/1")[1].get("Server", "").encode())

    def test_the_page_never_reaches_paperless_and_logs_nothing(self):
        tree = ast.parse(Path(apppage.__file__).read_text())
        imported = {n.module if isinstance(n, ast.ImportFrom) else a.name
                    for n in ast.walk(tree) if isinstance(n, (ast.Import, ast.ImportFrom))
                    for a in n.names}
        self.assertEqual(imported, {"__future__", "re", "threading", "http.server"})  # nothing of Jared, no network
        err = io.StringIO()
        with contextlib.redirect_stderr(err):
            request("/app/252")
            request("/app/abc")
        self.assertEqual(err.getvalue(), "")  # the request line would carry the document number

    def test_serve_binds_loopback_only(self):
        with mock.patch.object(apppage._Server, "__init__", return_value=None) as init, \
                mock.patch.object(apppage.threading, "Thread"):
            apppage.serve(8772)
        self.assertEqual(init.call_args.args[0], ("127.0.0.1", 8772))

    def test_a_client_hanging_up_leaves_no_traceback(self):
        err = io.StringIO()
        with contextlib.redirect_stderr(err):
            apppage._Server.handle_error(mock.Mock(), None, ("127.0.0.1", 1))
        self.assertEqual(err.getvalue(), "")


class LinkTests(unittest.TestCase):
    def archive(self, link="https://archiv.example:8443", app_links=False):
        fake = FakePaperless()
        client = Client("http://127.0.0.1:8000", TOKEN, send=fake.send)
        return Archive(client, link, today=lambda: dt.date(2026, 11, 22), app_links=app_links)

    def test_results_link_to_the_app_page_with_app_links(self):
        text = self.archive(app_links=True).search({"query": "streichen"})
        self.assertIn("https://archiv.example:8443/app/5", text)
        self.assertNotIn("/documents/5/details", text)
        self.assertIn("https://archiv.example:8443/app/5", self.archive(app_links=True).document({"id": 5}))

    def test_without_app_links_the_web_view_stays(self):
        self.assertIn("https://archiv.example:8443/documents/5/details", self.archive().search({"query": "streichen"}))
        self.assertNotIn("/app/", self.archive(link="", app_links=True).search({"query": "streichen"}))


class AppConfigTests(unittest.TestCase):
    def test_app_links_need_a_link_and_a_bool(self):
        with tempfile.TemporaryDirectory() as d:
            path = Path(d) / "config.toml"
            self.assertFalse(config.load_paperless(path).app_links)
            path.write_text('[paperless]\nlink = "https://merlin.example.ts.net:8443"\napp_links = true\n')
            self.assertTrue(config.load_paperless(path).app_links)
            for bad in ('app_links = true', 'link = "https://merlin.example.ts.net:8443"\napp_links = "yes"'):
                path.write_text(f"[paperless]\n{bad}\n")
                with self.subTest(bad=bad), self.assertRaises(config.ConfigError):
                    config.load_paperless(path)

    def test_app_port(self):
        with tempfile.TemporaryDirectory() as d:
            path = Path(d) / "config.toml"
            self.assertEqual(config.load_app_port(path), 8772)
            path.write_text("[server]\napp_port = 9000\n")
            self.assertEqual(config.load_app_port(path), 9000)
            for bad in ("app_port = 80", "app_port = true", "port = 9000\napp_port = 9000", "app_port = 8769"):
                path.write_text(f"[server]\n{bad}\n")
                with self.subTest(bad=bad), self.assertRaises(config.ConfigError):
                    config.load_app_port(path)


class ServeTests(unittest.TestCase):
    def run_serve(self, paperless_section: str, patch_app: bool = True):
        with tempfile.TemporaryDirectory() as d, \
                mock.patch.dict("os.environ", {"XDG_CONFIG_HOME": d, "XDG_STATE_HOME": d}):
            conf = Path(d) / "jared"
            conf.mkdir()
            (conf / "token-merlin").write_text("m" * 40)
            (conf / "paperless-token").write_text("p" * 40)
            (conf / "config.toml").write_text(f"[paperless]\n{paperless_section}\n")
            stop = threading.Event()
            stop.set()
            app_patch = mock.patch.object(apppage, "serve") if patch_app else contextlib.nullcontext()
            with mock.patch("jared.bridge.serve") as bridge_serve, app_patch as app_serve, \
                    mock.patch.object(cli.threading, "Event", return_value=stop), \
                    mock.patch.object(cli.signal, "signal"), contextlib.redirect_stdout(io.StringIO()) as out:
                self.assertEqual(cli.cmd_serve(mock.Mock()), 0)
            bridge_serve.return_value.shutdown.assert_called_once()
            return app_serve, out.getvalue()

    def test_the_page_runs_only_with_app_links(self):
        app_serve, out = self.run_serve('link = "https://merlin.example.ts.net:8443"\napp_links = true')
        app_serve.assert_called_once_with(8772)
        app_serve.return_value.shutdown.assert_called_once()
        self.assertIn("app page on 127.0.0.1:8772", out)
        app_serve, out = self.run_serve('link = "https://merlin.example.ts.net:8443"')
        app_serve.assert_not_called()
        self.assertNotIn("app page", out)

    def test_a_busy_app_port_is_a_clear_config_error(self):
        with mock.patch.object(apppage, "serve", side_effect=OSError(98, "Address already in use")), \
                self.assertRaises(config.ConfigError) as e:
            self.run_serve('link = "https://merlin.example.ts.net:8443"\napp_links = true', patch_app=False)
        self.assertIn("app_port 8772 is not free", str(e.exception))

    def test_config_check_names_the_kind_of_links(self):
        with tempfile.TemporaryDirectory() as d, mock.patch.dict("os.environ", {"XDG_CONFIG_HOME": d}):
            conf = Path(d) / "jared"
            conf.mkdir()
            (conf / "token-merlin").write_text("m" * 40)
            for section, kind in (("", "off"), ('link = "https://a.example"', "web"),
                                  ('link = "https://a.example"\napp_links = true', "app")):
                (conf / "config.toml").write_text(f"[paperless]\n{section}\n")
                with self.subTest(kind=kind), contextlib.redirect_stdout(io.StringIO()) as out:
                    self.assertEqual(cli.cmd_config_check(mock.Mock(strict=False)), 0)
                self.assertIn(f"links {kind})", out.getvalue())


if __name__ == "__main__":
    unittest.main()
