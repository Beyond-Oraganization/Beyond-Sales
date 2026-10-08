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

    def test_an_empty_file_lists_the_main_mailbox_and_fixed_groups_only(self):
        data = ed.describe({}, file_exists=False)
        self.assertEqual([g["key"] for g in data["groups"]], ["mail", "replies", "notify", "scraper"])
        self.assertEqual([g["mailbox"] for g in data["groups"]], [1, None, None, None])
        self.assertEqual(sum(len(g["fields"]) for g in data["groups"]), len(cf.FIELDS))
        self.assertEqual(data["next_slot"], 2)
        self.assertFalse(data["file_exists"])

    def test_every_mailbox_in_the_file_is_listed_in_numeric_order(self):
        config = {f"SMTP{n}_USER": f"u{n}@x.pl" for n in (12, 3, 40)}
        data = ed.describe(config, file_exists=True)
        self.assertEqual([g["key"] for g in data["groups"]], ["mail", "mail3", "mail12", "mail40", "replies", "notify", "scraper"])
        self.assertEqual(data["next_slot"], 2)

    def test_a_card_for_a_new_mailbox_has_the_same_fields_and_no_values(self):
        card = ed.describe_mailbox({"SMTP2_USER": "b@b.pl"}, 57)
        self.assertEqual((card["key"], card["mailbox"]), ("mail57", 57))
        names = ("HOST", "PORT", "USE_SSL", "USER", "PASSWORD")
        self.assertEqual([f["key"] for f in card["fields"]], [f"SMTP57_{n}" for n in names] + [f"IMAP57_{n}" for n in names])
        self.assertTrue(all(not f["set"] and f.get("value", "") == "" for f in card["fields"]))

    def test_a_card_for_a_slot_out_of_range_is_refused(self):
        for slot in (0, 1, -3, 10000):
            with self.subTest(slot=slot), self.assertRaises(cf.ConfigValueError):
                ed.describe_mailbox({}, slot)


class MailboxChangesTests(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.path = os.path.join(tmp.name, "c.env")
        with open(self.path, "w", encoding="utf-8") as f:
            f.write("SMTP_HOST=smtp.a.pl\nSMTP_PORT=465\nSMTP_USER=a@a.pl\nSMTP_PASSWORD=pa\n")

    def read(self):
        with open(self.path, encoding="utf-8") as handle:
            return handle.read()

    BOX2 = {"SMTP2_HOST": "smtp.b.pl", "SMTP2_PORT": "465", "SMTP2_USER": "b@b.pl", "SMTP2_PASSWORD": "pb"}

    def test_a_complete_second_mailbox_is_saved(self):
        result = ed.apply_changes(self.path, self.BOX2, [])
        self.assertEqual(result["changed"], sorted(self.BOX2))
        self.assertEqual(cfg.load_config(self.path)["SMTP2_USER"], "b@b.pl")

    def test_a_half_filled_mailbox_is_refused_and_the_file_is_untouched(self):
        before = self.read()
        with self.assertRaisesRegex(cfg.ConfigError, "SMTP2_PORT, SMTP2_PASSWORD"):
            ed.apply_changes(self.path, {"SMTP2_HOST": "smtp.b.pl", "SMTP2_USER": "b@b.pl"}, [])
        self.assertEqual(self.read(), before)

    def test_imap_needs_a_host_and_some_password_and_the_smtp_address(self):
        with self.assertRaisesRegex(cfg.ConfigError, "IMAP2_HOST"):
            ed.apply_changes(self.path, {**self.BOX2, "IMAP2_PORT": "993"}, [])
        with self.assertRaisesRegex(cfg.ConfigError, "SMTP3_USER"):
            ed.apply_changes(self.path, {"IMAP3_HOST": "imap.b.pl"}, [])

    def test_imap_falls_back_to_the_smtp_login_and_can_be_saved_with_its_own(self):
        result = ed.apply_changes(self.path, {**self.BOX2, "IMAP2_HOST": "imap.b.pl"}, [])
        self.assertIn("IMAP2_HOST", result["changed"])
        own = {"IMAP2_USER": "login-b", "IMAP2_PASSWORD": "inne-haslo", "IMAP2_PORT": "143", "IMAP2_USE_SSL": "false"}
        ed.apply_changes(self.path, own, [])
        imap = cfg.imap_slots(cfg.load_config(self.path))
        self.assertEqual([(box.slot, box.host, box.port, box.user, box.use_ssl, box.address) for box in imap],
                         [(2, "imap.b.pl", 143, "login-b", False, "b@b.pl")])

    def test_the_same_address_twice_is_refused(self):
        with self.assertRaisesRegex(cfg.ConfigError, "ten sam adres"):
            ed.apply_changes(self.path, {**self.BOX2, "SMTP2_USER": "A@a.pl"}, [])

    def test_removing_a_mailbox_clears_all_its_keys(self):
        ed.apply_changes(self.path, self.BOX2, [])
        result = ed.apply_changes(self.path, {"SMTP2_HOST": "", "SMTP2_PORT": "", "SMTP2_USER": "", "SMTP2_USE_SSL": ""}, ["SMTP2_PASSWORD"])
        self.assertEqual(result["changed"], sorted(self.BOX2))
        self.assertEqual([k for k in cfg.load_config(self.path) if k.startswith("SMTP2")], [])

    def test_unrelated_changes_do_not_revalidate_mailboxes(self):
        with open(self.path, "a", encoding="utf-8") as f:
            f.write("SMTP3_USER=ktos@x.pl\n")  # zepsuta skrzynka sprzed zmiany nie blokuje zapisu innych ustawień
        self.assertEqual(ed.apply_changes(self.path, {"SKRAPER_DELAY": "3"}, [])["changed"], ["SKRAPER_DELAY"])

    def test_describe_marks_mailbox_groups_with_their_slot(self):
        data = ed.describe({**self.BOX2}, file_exists=True)
        by_key = {g["key"]: g for g in data["groups"]}
        self.assertEqual((by_key["mail"]["mailbox"], by_key["mail2"]["mailbox"], by_key["notify"]["mailbox"]), (1, 2, None))
        fields = {f["key"]: f for f in by_key["mail2"]["fields"]}
        self.assertEqual((fields["SMTP2_USER"]["value"], fields["SMTP2_PASSWORD"]["set"], "value" in fields["SMTP2_PASSWORD"]),
                         ("b@b.pl", True, False))
        self.assertNotIn("pb", repr(data))

    def test_connection_test_reports_every_mailbox_and_hides_all_passwords(self):
        class Failing:
            def __enter__(self):
                raise RuntimeError("(535, b'Authentication failed pass=pb')")

            def __exit__(self, *exc):
                return False

        good = sending.Mailer(lambda: FakeSmtp(), sending.Sender("a@a.pl"))
        bad = sending.Mailer(lambda: Failing(), sending.Sender("b@b.pl"))
        result = ed.test_smtp(lambda: [good, bad], ["pa", "pb"])
        self.assertFalse(result["ok"])
        self.assertEqual([(r["address"], r["ok"]) for r in result["results"]], [("a@a.pl", True), ("b@b.pl", False)])
        self.assertIn("a@a.pl: OK", result["message"])
        self.assertNotIn("pb", repr(result))


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
        return lambda: [sending.Mailer(lambda: smtp, sending.Sender("me@example.com"))]

    def test_success(self):
        result = ed.test_smtp(self.mailer(FakeSmtp()))
        self.assertEqual(result["message"], "Połączono i zalogowano jako me@example.com.")
        self.assertEqual((result["ok"], [r["address"] for r in result["results"]]), (True, ["me@example.com"]))

    def test_missing_configuration_is_a_result_not_an_exception(self):
        def broken():
            raise cfg.ConfigError("W config.env brakuje: SMTP_HOST.")

        self.assertEqual(ed.test_smtp(broken), {"ok": False, "message": "W config.env brakuje: SMTP_HOST.", "results": []})

    def test_connection_failure_is_reported_without_the_password(self):
        class Failing:
            def __enter__(self):
                raise RuntimeError(f"(535, b'Authentication failed for pass={SECRET}')")

            def __exit__(self, *exc):
                return False

        result = ed.test_smtp(lambda: [sending.Mailer(lambda: Failing(), sending.Sender("a@b.pl"))], [SECRET])
        self.assertFalse(result["ok"])
        self.assertIn("535", result["message"])
        self.assertNotIn(SECRET, result["message"])


if __name__ == "__main__":
    unittest.main()
