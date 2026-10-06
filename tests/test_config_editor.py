"""Usługa edycji config.env: opis pól bez sekretów, budowa zmian, zapis i próba połączenia SMTP."""

from __future__ import annotations

import os
import tempfile
import unittest

from perno import settings as cfg
from perno.domain import config_fields as cf
from perno.services import config_editor as ed
from perno.services import sending
from tests.fixtures import FakeSmtp

SECRET = "hasło-którego-nie-wolno-ujawnić"


class DescribeTests(unittest.TestCase):
    def test_secret_values_never_appear_but_their_state_does(self):
        config = {"SMTP_HOST": "smtp.example.com", "SMTP_PASSWORD": SECRET, "NTFY_TOPIC": "bardzo-tajny-temat"}
        data = ed.describe(config, file_exists=True)
        text = repr(data)
        self.assertNotIn(SECRET, text)
        self.assertNotIn("bardzo-tajny-temat", text)
        fields = {f["key"]: f for g in data["groups"] for f in g["fields"]}
        self.assertEqual((fields["SMTP_PASSWORD"]["set"], "value" in fields["SMTP_PASSWORD"]), (True, False))
        self.assertEqual((fields["HEALTHCHECK_URL"]["set"], "value" in fields["HEALTHCHECK_URL"]), (False, False))
        self.assertEqual((fields["SMTP_HOST"]["value"], fields["SMTP_PORT"]["value"]), ("smtp.example.com", ""))

    def test_groups_and_all_fields_are_listed_in_order(self):
        data = ed.describe({}, file_exists=False)
        self.assertEqual([g["key"] for g in data["groups"]], ["mail", "notify", "scraper"])
        self.assertEqual(sum(len(g["fields"]) for g in data["groups"]), len(cf.FIELDS))
        self.assertFalse(data["file_exists"])


class ChangesTests(unittest.TestCase):
    def test_values_are_validated_and_normalised(self):
        self.assertEqual(ed.build_changes({"SMTP_PORT": " 587 ", "SMTP_USE_SSL": "FALSE"}, []),
                         {"SMTP_PORT": "587", "SMTP_USE_SSL": "false"})

    def test_empty_plain_value_clears_but_empty_secret_is_left_alone(self):
        self.assertEqual(ed.build_changes({"SKRAPER_MAX_PAGES": "", "SMTP_PASSWORD": "  ", "NTFY_TOPIC": ""}, []),
                         {"SKRAPER_MAX_PAGES": None})

    def test_clear_removes_even_a_secret(self):
        self.assertEqual(ed.build_changes({}, ["SMTP_PASSWORD", "SKRAPER_MAX_MB"]), {"SMTP_PASSWORD": None, "SKRAPER_MAX_MB": None})

    def test_setting_and_clearing_the_same_key_is_refused(self):
        with self.assertRaises(cf.ConfigValueError):
            ed.build_changes({"NTFY_TOPIC": "bardzo-tajny-temat"}, ["NTFY_TOPIC"])

    def test_unknown_and_database_keys_are_refused(self):
        for values, clear in (({"DB_PASSWORD": "x"}, []), ({"PERNO_DB_URL": "x"}, []), ({}, ["DB_HOST"]), ({1: "x"}, []), ({}, [None])):
            with self.subTest(values=values, clear=clear), self.assertRaises(cf.ConfigValueError):
                ed.build_changes(values, clear)

    def test_non_text_value_is_refused(self):
        for bad in (5, None, ["a"], True):
            with self.subTest(bad=bad), self.assertRaises(cf.ConfigValueError):
                ed.build_changes({"SMTP_PORT": bad}, [])

    def test_invalid_value_stops_the_whole_batch_before_writing(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "c.env")
            with open(path, "w", encoding="utf-8") as f:
                f.write("SMTP_PORT=465\n")
            with self.assertRaises(cf.ConfigValueError):
                ed.apply_changes(path, {"SMTP_PORT": "587", "SMTP_HOST": "ma spację"}, [])
            self.assertEqual(cfg.load_config(path), {"SMTP_PORT": "465"})


class ApplyTests(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.path = os.path.join(tmp.name, "c.env")
        with open(self.path, "w", encoding="utf-8") as f:
            f.write("SMTP_HOST=smtp.example.com\nSMTP_PASSWORD=stare\n# NTFY_TOPIC=\n")

    def test_reports_changed_keys_and_which_need_a_daemon_restart(self):
        result = ed.apply_changes(self.path, {"SMTP_HOST": "smtp.nowy.pl", "NTFY_TOPIC": "bardzo-tajny-temat"}, [])
        self.assertEqual(result, {"changed": ["NTFY_TOPIC", "SMTP_HOST"], "restart_demon": ["NTFY_TOPIC"]})
        self.assertEqual(cfg.load_config(self.path)["NTFY_TOPIC"], "bardzo-tajny-temat")

    def test_unchanged_values_report_nothing(self):
        self.assertEqual(ed.apply_changes(self.path, {"SMTP_HOST": "smtp.example.com"}, []), {"changed": [], "restart_demon": []})

    def test_empty_request_writes_nothing(self):
        self.assertEqual(ed.apply_changes(self.path, {}, []), {"changed": [], "restart_demon": []})
        self.assertFalse(os.path.exists(self.path + ".bak"))

    def test_secret_can_be_replaced_and_cleared(self):
        ed.apply_changes(self.path, {"SMTP_PASSWORD": "nowe"}, [])
        self.assertEqual(cfg.load_config(self.path)["SMTP_PASSWORD"], "nowe")
        ed.apply_changes(self.path, {}, ["SMTP_PASSWORD"])
        self.assertNotIn("SMTP_PASSWORD", cfg.load_config(self.path))


class SmtpTests(unittest.TestCase):
    def mailer(self, smtp):
        return lambda: sending.Mailer(lambda: smtp, sending.Sender("me@example.com"))

    def test_success(self):
        result = ed.test_smtp(self.mailer(FakeSmtp()))
        self.assertEqual(result, {"ok": True, "message": "Połączono i zalogowano jako me@example.com."})

    def test_missing_configuration_is_a_result_not_an_exception(self):
        def broken():
            raise cfg.ConfigError("W config.env brakuje: SMTP_HOST.")

        self.assertEqual(ed.test_smtp(broken), {"ok": False, "message": "W config.env brakuje: SMTP_HOST."})

    def test_connection_failure_is_reported_without_the_password(self):
        class Failing:
            def __enter__(self):
                raise RuntimeError(f"(535, b'Authentication failed for pass={SECRET}')")

            def __exit__(self, *exc):
                return False

        result = ed.test_smtp(lambda: sending.Mailer(lambda: Failing(), sending.Sender("a@b.pl")), SECRET)
        self.assertFalse(result["ok"])
        self.assertIn("535", result["message"])
        self.assertNotIn(SECRET, result["message"])


if __name__ == "__main__":
    unittest.main()
