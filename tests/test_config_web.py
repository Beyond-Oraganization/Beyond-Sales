"""Zakładka „Konfiguracja” przez aplikację: opis bez sekretów, zapis do pliku, błędy i próba SMTP."""

from __future__ import annotations

import os
import unittest

from perno import settings as cfg
from tests.test_web import SECRET, WebCase


class ConfigWebCase(WebCase):
    @property
    def path(self):
        return self.app.ctx.config.path

    def read_file(self):
        with open(self.path, encoding="utf-8") as handle:
            return handle.read()

    def fields(self):
        data = self.data(self.call("GET", "/api/config"))
        return {f["key"]: f for g in data["groups"] for f in g["fields"]}


class ReadTests(ConfigWebCase):
    def test_page_is_in_the_menu_and_renders(self):
        resp = self.call("GET", "/konfiguracja")
        html = resp.body.decode("utf-8")
        self.assertEqual(resp.status, 200)
        self.assertIn('href="/konfiguracja" aria-current="page">Konfiguracja</a>', html)
        self.assertIn("config.env.bak", html)

    def test_no_response_ever_contains_the_password(self):
        for method, path, payload in (("GET", "/api/config", None), ("POST", "/api/config", {"values": {"SENDER_NAME": "Nowy"}}),
                                      ("POST", "/api/config", {"values": {"SMTP_PORT": "zły"}}), ("GET", "/konfiguracja", None)):
            with self.subTest(path=path, payload=payload):
                self.assertNotIn(SECRET, self.call(method, path, payload).body.decode("utf-8"))

    def test_current_values_are_shown_and_the_secret_is_only_flagged(self):
        fields = self.fields()
        self.assertEqual((fields["SMTP_HOST"]["value"], fields["SMTP_PORT"]["value"], fields["SENDER_NAME"]["value"]),
                         ("smtp.invalid", "465", "Demo"))
        self.assertEqual((fields["SMTP_PASSWORD"]["set"], "value" in fields["SMTP_PASSWORD"]), (True, False))

    def test_get_only_on_read_route_and_post_only_on_write(self):
        self.assertEqual(self.call("POST", "/api/config/test-smtp", {}).status, 200)
        self.assertEqual(self.call("GET", "/api/config/test-smtp").status, 404)


class SaveTests(ConfigWebCase):
    def test_changes_are_written_and_returned(self):
        resp = self.call("POST", "/api/config", {"values": {"SMTP_PORT": "587", "NTFY_TOPIC": "bardzo-tajny-temat"}})
        data = self.data(resp)
        self.assertEqual((resp.status, data["changed"], data["restart_demon"]), (200, ["NTFY_TOPIC", "SMTP_PORT"], ["NTFY_TOPIC"]))
        self.assertEqual(cfg.load_config(self.path)["SMTP_PORT"], "587")
        self.assertNotIn("bardzo-tajny-temat", resp.body.decode("utf-8"))
        self.assertTrue(os.path.exists(self.path + ".bak"))

    def test_the_new_value_is_used_at_once_without_restart(self):
        self.call("POST", "/api/config", {"values": {"SMTP_HOST": "smtp.nowy.pl"}})
        self.assertEqual(self.fields()["SMTP_HOST"]["value"], "smtp.nowy.pl")
        self.assertEqual(self.app.ctx.config.load()["SMTP_HOST"], "smtp.nowy.pl")

    def test_secret_left_empty_stays_and_clear_removes_it(self):
        self.call("POST", "/api/config", {"values": {"SMTP_PASSWORD": ""}})
        self.assertEqual(cfg.load_config(self.path)["SMTP_PASSWORD"], SECRET)
        self.call("POST", "/api/config", {"clear": ["SMTP_PASSWORD"]})
        self.assertNotIn("SMTP_PASSWORD", cfg.load_config(self.path))
        self.assertFalse(self.fields()["SMTP_PASSWORD"]["set"])

    def test_invalid_value_is_a_400_and_nothing_is_written(self):
        before = self.read_file()
        resp = self.call("POST", "/api/config", {"values": {"SMTP_PORT": "99999", "SENDER_NAME": "Nowy"}})
        self.assertEqual(resp.status, 400)
        self.assertIn("Port SMTP", self.data(resp)["error"])
        self.assertEqual(self.read_file(), before)

    def test_database_and_unknown_keys_are_refused(self):
        for payload in ({"values": {"DB_PASSWORD": "x"}}, {"values": {"PERNO_DB_URL": "x"}}, {"clear": ["DB_HOST"]}):
            with self.subTest(payload=payload):
                self.assertEqual(self.call("POST", "/api/config", payload).status, 400)

    def test_malformed_payloads(self):
        for payload in ({"values": []}, {"values": {}, "clear": "SMTP_PORT"}, {"values": {"SMTP_PORT": 5}}):
            with self.subTest(payload=payload):
                self.assertEqual(self.call("POST", "/api/config", payload).status, 400)
        self.assertEqual(self.call("POST", "/api/config", raw=b"[]").status, 400)

    def test_empty_request_is_harmless(self):
        data = self.data(self.call("POST", "/api/config", {}))
        self.assertEqual((data["changed"], data["restart_demon"]), ([], []))

    def test_cross_origin_post_is_refused_by_the_core(self):
        resp = self.call("POST", "/api/config", {"values": {"SENDER_NAME": "X"}}, headers={"Origin": "http://evil.example"})
        self.assertEqual(resp.status, 403)
        self.assertEqual(self.fields()["SENDER_NAME"]["value"], "Demo")


class SmtpTests(ConfigWebCase):
    def test_smtp_check_uses_the_injected_client(self):
        data = self.data(self.call("POST", "/api/config/test-smtp", {}))
        self.assertEqual(data, {"ok": True, "message": "Połączono i zalogowano jako demo@example.com."})

    def test_smtp_check_reports_missing_configuration(self):
        os.unlink(self.path)
        self.app.ctx.smtp_factory = None
        data = self.data(self.call("POST", "/api/config/test-smtp", {}))
        self.assertFalse(data["ok"])


if __name__ == "__main__":
    unittest.main()
