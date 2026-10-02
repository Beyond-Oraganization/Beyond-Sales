from __future__ import annotations

import http.client
import json
import os
import tempfile
import threading
import unittest
import unittest.mock
from http.server import ThreadingHTTPServer

import sqlalchemy.exc as sa_exc

from perno.cli import server
from perno.web import app as appmod
from perno.web import layout
from perno.web.app import MAX_BODY, App, Request, json_response
from perno.web.pages import PAGES
from perno.web.pages.base import Page
from tests.fixtures import FakeSmtp, make_db, put, sample_row

PORT = 8799
HOSTS = {f"localhost:{PORT}", f"127.0.0.1:{PORT}"}
SECRET = "hasło-którego-nie-wolno-ujawnić"


def write_config(directory: str, password: str = SECRET) -> str:
    path = os.path.join(directory, "fake.env")
    with open(path, "w", encoding="utf-8") as f:
        f.write(f"SMTP_HOST=smtp.invalid\nSMTP_PORT=465\nSMTP_USER=demo@example.com\n"
                f"SMTP_PASSWORD={password}\nSENDER_NAME=Demo\n")
    return path


class WebCase(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.tmp = tmp.name
        self.database = make_db()
        self.smtp = FakeSmtp()
        self.app = App(self.database, PAGES, config_path=write_config(self.tmp),
                       smtp_factory=lambda: self.smtp, allowed_hosts=set(HOSTS))

    def call(self, method, path, payload=None, headers=None, raw=None, query=None, content_length=None):
        base = {"host": f"localhost:{PORT}", "content-type": "application/json"}
        base.update({k.lower(): v for k, v in (headers or {}).items()})
        body = raw if raw is not None else (b"" if payload is None else json.dumps(payload).encode("utf-8"))
        return self.app.dispatch(Request(method, path, query=query or {}, headers=base, body=body,
                                         content_length=content_length))

    def data(self, resp):
        return json.loads(resp.body.decode("utf-8"))


class PageTests(WebCase):
    def test_home_page_has_menu_with_active_tab_and_db_tag(self):
        resp = self.call("GET", "/")
        html = resp.body.decode("utf-8")
        self.assertEqual(resp.status, 200)
        self.assertIn('<a class="topnav-link" href="/" aria-current="page">Wysyłka</a>', html)
        self.assertIn("sqlite (test)", html)
        self.assertIn("wysylanie", html)

    def test_security_headers_on_every_response(self):
        for resp in (self.call("GET", "/"), self.call("GET", "/nie-ma"), self.call("GET", "/api/maile")):
            for name in appmod.SECURITY_HEADERS:
                self.assertIn(name, resp.headers)

    def test_new_page_appears_in_menu_and_serves_its_routes(self):
        def render(app):
            return layout.render_page(app, title="Raporty", active_key="raporty", body="<main>raport</main>")

        page = Page(key="raporty", label="Raporty", path="/raporty", render=render, order=50,
                    routes={("GET", "/api/raporty"): lambda req, app: json_response({"ok": True})})
        self.app = App(self.database, [*PAGES, page], config_path=self.app.config_path,
                       allowed_hosts=set(HOSTS))
        html = self.call("GET", "/raporty").body.decode("utf-8")
        self.assertIn('href="/raporty" aria-current="page">Raporty</a>', html)
        self.assertIn('href="/">Wysyłka</a>', self.call("GET", "/raporty").body.decode("utf-8"))
        self.assertEqual(self.data(self.call("GET", "/api/raporty")), {"ok": True})
        self.assertEqual(self.call("GET", "/raporty/").status, 200)

    def test_registry_rejects_duplicates(self):
        page = PAGES[0]
        with self.assertRaises(ValueError):
            App(self.database, [page, page])
        clash = Page(key="inna", label="Inna", path="/inna", render=lambda app: "",
                     routes={("GET", "/api/maile"): lambda req, app: json_response({})})
        with self.assertRaises(ValueError):
            App(self.database, [page, clash])
        with self.assertRaises(ValueError):
            App(self.database, [Page(key="zla", label="Zła", path="zla", render=lambda app: "")])


class SecurityTests(WebCase):
    def test_host_allow_list_applies_to_get_and_post(self):
        for host in ("evil.test:8799", f"localhost:{PORT + 1}", "localhost", ""):
            self.assertEqual(self.call("GET", "/api/maile", headers={"host": host}).status, 403, host)
            self.assertEqual(self.call("POST", "/api/update-row", {"id": "x"}, headers={"host": host}).status, 403)
        self.assertEqual(self.call("GET", "/api/maile", headers={"host": f"127.0.0.1:{PORT}"}).status, 200)

    def test_origin_must_match_when_present(self):
        for origin in ("http://evil.test", "null", f"https://localhost:{PORT}", f"http://localhost:{PORT + 1}"):
            resp = self.call("POST", "/api/update-row", {"id": "x"}, headers={"origin": origin})
            self.assertEqual(resp.status, 403, origin)
        ok = self.call("POST", "/api/update-row", {"id": "eventy-404"}, headers={"origin": f"http://localhost:{PORT}"})
        self.assertEqual(ok.status, 404)

    def test_post_requires_json_content_type(self):
        for ctype in ("text/plain", "application/x-www-form-urlencoded", ""):
            self.assertEqual(self.call("POST", "/api/update-row", raw=b"{}", headers={"content-type": ctype}).status, 415)
        self.assertEqual(self.call("POST", "/api/update-row", {"id": "eventy-404"},
                                   headers={"content-type": "application/json; charset=utf-8"}).status, 404)

    def test_body_limits_and_shape(self):
        self.assertEqual(self.call("POST", "/api/update-row", raw=b"{}", content_length=MAX_BODY + 1).status, 413)
        self.assertEqual(self.call("POST", "/api/update-row", raw=b"{nie json").status, 400)
        self.assertEqual(self.call("POST", "/api/update-row", raw=b"[1, 2]").status, 400)
        self.assertEqual(self.call("POST", "/api/update-row", raw=b"\xff\xfe").status, 400)

    def test_other_methods_get_405_without_cors(self):
        for method in ("OPTIONS", "PUT", "DELETE", "PATCH", "HEAD"):
            resp = self.call(method, "/api/maile")
            self.assertEqual(resp.status, 405, method)
            self.assertFalse([h for h in resp.headers if h.lower().startswith("access-control")])

    def test_status_never_leaks_the_password(self):
        resp = self.call("GET", "/api/status")
        self.assertNotIn(SECRET, resp.body.decode("utf-8"))
        data = self.data(resp)
        self.assertTrue(data["smtp_configured"])
        self.assertEqual(data["smtp_user"], "demo@example.com")
        self.assertEqual((data["sent_today"], data["row_count"]), (0, 0))

    def test_internal_errors_do_not_leak_details(self):
        def boom(req, app):
            raise RuntimeError("tajny stack trace")

        def db_down(req, app):
            raise sa_exc.OperationalError("SELECT", {}, Exception("connection refused"))

        page = Page(key="t", label="T", path="/t", render=lambda app: "",
                    routes={("GET", "/boom"): boom, ("GET", "/down"): db_down})
        self.app = App(self.database, [page], allowed_hosts=set(HOSTS))
        with unittest.mock.patch("traceback.print_exc"):
            resp = self.call("GET", "/boom")
            down = self.call("GET", "/down")
        self.assertEqual(resp.status, 500)
        self.assertNotIn("tajny", resp.body.decode("utf-8"))
        self.assertEqual(down.status, 503)
        self.assertNotIn("connection refused", down.body.decode("utf-8"))


class UpdateRowTests(WebCase):
    def test_edit_and_errors(self):
        put(self.database, sample_row(1, status="pominieto_brak_email", email=""), sample_row(2, status="wysylanie"))
        row = self.data(self.call("POST", "/api/update-row", {"id": "eventy-001", "email": "a@b.pl"}))["row"]
        self.assertEqual((row["email"], row["status"]), ("a@b.pl", "do_wyslania"))

        locked = self.call("POST", "/api/update-row", {"id": "eventy-002", "subject": "X"})
        self.assertEqual(locked.status, 409)
        self.assertEqual(self.data(locked)["row"]["id"], "eventy-002")

        self.assertEqual(self.call("POST", "/api/update-row", {"id": "eventy-001", "status": "nonsens"}).status, 400)
        self.assertEqual(self.call("POST", "/api/update-row", {"id": "eventy-001", "status": "wysylanie"}).status, 400)
        self.assertEqual(self.call("POST", "/api/update-row", {"id": "eventy-001", "subject": 5}).status, 400)
        self.assertEqual(self.call("POST", "/api/update-row", {"subject": "x"}).status, 400)
        self.assertEqual(self.call("POST", "/api/update-row", {"id": "eventy-999", "subject": "x"}).status, 404)


class SendOneTests(WebCase):
    def test_successful_send(self):
        put(self.database, sample_row(1, email="cel@example.com"))
        resp = self.call("POST", "/api/send-one", {"id": "eventy-001"})
        data = self.data(resp)
        self.assertEqual((resp.status, data["outcome"], data["row"]["status"]), (200, "sent", "wyslano"))
        self.assertEqual([m["To"] for m in self.smtp.sent], ["cel@example.com"])

    def test_refusals(self):
        put(self.database,
            sample_row(1, status="wyslano", sent_at="2026-09-29T14:00:00"),
            sample_row(2, email=""),
            sample_row(3, status="wysylanie"))
        self.assertEqual(self.call("POST", "/api/send-one", {"id": "eventy-001"}).status, 400)
        self.assertEqual(self.call("POST", "/api/send-one", {"id": "eventy-002"}).status, 400)
        self.assertEqual(self.call("POST", "/api/send-one", {"id": "eventy-999"}).status, 404)
        skipped = self.call("POST", "/api/send-one", {"id": "eventy-003"})
        self.assertEqual(skipped.status, 409)
        self.assertEqual(self.data(skipped)["row"]["status"], "wysylanie")
        self.assertEqual(self.smtp.sent, [])

    def test_daily_cap_is_enforced_server_side(self):
        now = self.database.now().strftime("%Y-%m-%dT%H:%M:%S")  # ten sam (zamrożony) zegar, którego używa aplikacja
        put(self.database, sample_row(1, status="wyslano", sent_at=now), sample_row(2))
        resp = self.call("POST", "/api/send-one", {"id": "eventy-002", "daily_cap": 1})
        self.assertEqual(resp.status, 429)
        self.assertEqual(self.smtp.sent, [])
        self.assertEqual(self.call("POST", "/api/send-one", {"id": "eventy-002", "daily_cap": 2}).status, 200)

    def test_missing_smtp_password_gives_clear_error_before_claiming(self):
        put(self.database, sample_row(1))
        self.app.config_path = write_config(self.tmp, password="")
        resp = self.call("POST", "/api/send-one", {"id": "eventy-001"})
        self.assertEqual(resp.status, 400)
        self.assertIn("SMTP_PASSWORD", self.data(resp)["error"])
        self.assertEqual(self.database.get("eventy-001")["status"], "do_wyslania")

    def test_smtp_failure_is_recorded(self):
        put(self.database, sample_row(1))
        self.smtp.fail_with = OSError("serwer padł")
        data = self.data(self.call("POST", "/api/send-one", {"id": "eventy-001"}))
        self.assertEqual((data["outcome"], data["row"]["status"], data["row"]["retry_count"]), ("error", "blad", "1"))
        self.smtp.fail_with = None
        self.assertEqual(self.data(self.call("POST", "/api/send-one", {"id": "eventy-001"}))["outcome"], "sent")


class RegenerateTests(WebCase):
    def test_refuses_on_empty_database_and_works_otherwise(self):
        resp = self.call("POST", "/api/regenerate")
        self.assertEqual(resp.status, 409)
        self.assertIn("migrate_csv_to_sql.py", self.data(resp)["error"])
        put(self.database, sample_row(901))
        data = self.data(self.call("POST", "/api/regenerate"))
        self.assertGreater(data["eventy"]["created"], 40)
        self.assertGreater(len(data["rows"]), 90)


class SocketTests(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        database = make_db()
        put(database, sample_row(1))
        self.app = App(database, PAGES, config_path=write_config(tmp.name))
        server.Handler.app = self.app
        self.httpd = ThreadingHTTPServer(("127.0.0.1", 0), server.Handler)
        self.port = self.httpd.server_address[1]
        self.app.set_port(self.port)
        thread = threading.Thread(target=self.httpd.serve_forever, daemon=True)
        thread.start()
        self.addCleanup(thread.join, 5)
        self.addCleanup(self.httpd.server_close)
        self.addCleanup(self.httpd.shutdown)

    def request(self, method, path, body=None, headers=None):
        conn = http.client.HTTPConnection("127.0.0.1", self.port, timeout=5)
        try:
            conn.request(method, path, body=body, headers=headers or {})
            resp = conn.getresponse()
            return resp.status, {k.lower(): v for k, v in resp.getheaders()}, resp.read()
        finally:
            conn.close()

    def test_real_http_behaviour(self):
        status, headers, body = self.request("GET", "/")
        self.assertEqual(status, 200)
        self.assertIn(b"topnav", body)
        self.assertEqual(headers["x-frame-options"], "DENY")
        self.assertEqual(self.request("GET", "/api/maile", headers={"Host": "evil.test:1"})[0], 403)
        self.assertEqual(self.request("POST", "/api/send-one", b'{"id":"eventy-001"}',
                                      {"Content-Type": "text/plain"})[0], 415)
        self.assertEqual(self.request("POST", "/api/update-row", b'{"id":"eventy-001"}',
                                      {"Content-Type": "application/json", "Origin": "http://evil.test"})[0], 403)
        ok = self.request("POST", "/api/update-row", b'{"id":"eventy-001","subject":"Nowy"}',
                          {"Content-Type": "application/json", "Origin": f"http://127.0.0.1:{self.port}"})
        self.assertEqual(ok[0], 200)
        self.assertEqual(json.loads(ok[2])["row"]["subject"], "Nowy")
        status, headers, _ = self.request("OPTIONS", "/api/maile")
        self.assertEqual(status, 405)
        self.assertFalse([h for h in headers if h.startswith("access-control")])
        self.assertEqual(self.request("PUT", "/api/maile", b"{}", {"Content-Type": "application/json"})[0], 405)


if __name__ == "__main__":
    unittest.main()
