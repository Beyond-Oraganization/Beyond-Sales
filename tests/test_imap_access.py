"""Dostęp do poczty przychodzącej: blokada bazy demo i próba połączenia ze skrzynkami (na atrapach, bez sieci)."""

from __future__ import annotations

import unittest

from perno.mailer.imap_client import KIND_AUTH, ImapError
from perno.services import imap_access
from perno.settings import ConfigError, Environment
from tests.fake_imap import FakeAccounts, FakeImap, make_eml

BASE = {"SMTP_HOST": "smtp.example.com", "SMTP_PORT": "465", "SMTP_USER": "roman@z3x.pl", "SMTP_PASSWORD": "smtp-haslo"}
TWO = {**BASE, "IMAP_HOST": "imap.example.com", "SMTP2_HOST": "s2", "SMTP2_PORT": "465", "SMTP2_USER": "drugi@z3x.pl",
       "SMTP2_PASSWORD": "drugie-haslo", "IMAP2_HOST": "imap2.example.com", "IMAP2_PASSWORD": "imap-haslo-2"}


def accounts() -> FakeAccounts:
    first = FakeImap({"INBOX": {1: make_eml(), 2: make_eml()}, "Sent": {}, "Junk": {}, "Drafts": {}}, roles={"Sent": "sent", "Junk": "junk"})
    return FakeAccounts({"roman@z3x.pl": first, "drugi@z3x.pl": FakeImap({"INBOX": {}})})


class FactoryTests(unittest.TestCase):
    def test_a_demo_database_refuses_real_connections(self):
        with self.assertRaisesRegex(ConfigError, "PERNO_ALLOW_REAL_IMAP"):
            imap_access.real_factory(Environment.from_environ({"PERNO_DB_URL": "sqlite://"}))

    def test_without_a_demo_database_or_with_the_unlock_it_gives_a_client(self):
        for env in ({}, {"PERNO_DB_URL": "sqlite://", "PERNO_ALLOW_REAL_IMAP": "1"}):
            factory = imap_access.real_factory(Environment.from_environ(env))
            self.assertTrue(callable(factory))


class TestImapTests(unittest.TestCase):
    def run_check(self, config, sessions=None):
        sessions = sessions or accounts()
        return imap_access.test_imap(config, lambda: sessions), sessions

    def test_every_mailbox_with_imap_is_checked_and_nothing_is_changed_on_the_server(self):
        result, sessions = self.run_check(TWO)
        self.assertTrue(result["ok"])
        self.assertEqual([(r["slot"], r["address"], r["ok"]) for r in result["results"]], [(1, "roman@z3x.pl", True), (2, "drugi@z3x.pl", True)])
        self.assertEqual(sessions.opened, ["roman@z3x.pl", "drugi@z3x.pl"])
        first = sessions.sessions["roman@z3x.pl"]
        self.assertEqual((first.full_fetches, first.header_fetches, first.appended), ([], [], []))
        self.assertIn("2 wiadomości", result["results"][0]["message"])
        self.assertIn("INBOX, Sent, Junk", result["results"][0]["message"])

    def test_a_mailbox_without_imap_is_not_contacted(self):
        result, sessions = self.run_check({**BASE, "IMAP_HOST": "imap.example.com", "SMTP2_USER": "x@z3x.pl", "SMTP2_HOST": "s", "SMTP2_PORT": "1",
                                           "SMTP2_PASSWORD": "p"})
        self.assertEqual(sessions.opened, ["roman@z3x.pl"])
        self.assertEqual(len(result["results"]), 1)

    def test_no_imap_anywhere_is_said_plainly_and_no_connection_is_made(self):
        called = []
        result = imap_access.test_imap(BASE, lambda: called.append(1))
        self.assertEqual((result["ok"], result["results"], called), (False, [], []))
        self.assertIn("IMAP", result["message"])

    def test_a_wrong_login_is_reported_per_mailbox_without_leaking_passwords(self):
        sessions = accounts()
        sessions.sessions["drugi@z3x.pl"].fail_with = ImapError("Authentication failed for imap-haslo-2", KIND_AUTH)
        result, _ = self.run_check(TWO, sessions)
        self.assertFalse(result["ok"])
        self.assertEqual([r["ok"] for r in result["results"]], [True, False])
        bad = result["results"][1]
        self.assertEqual(bad["kind"], KIND_AUTH)
        self.assertNotIn("imap-haslo-2", repr(result))
        self.assertIn("***", bad["message"])

    def test_an_unexpected_failure_is_a_result_too(self):
        sessions = accounts()
        sessions.sessions["roman@z3x.pl"].fail_with = TimeoutError("timed out smtp-haslo")
        result, _ = self.run_check(TWO, sessions)
        self.assertEqual([r["ok"] for r in result["results"]], [False, True])
        self.assertNotIn("smtp-haslo", repr(result))

    def test_only_passwords_are_scrubbed_not_host_names_or_users(self):
        sessions = accounts()
        sessions.sessions["roman@z3x.pl"].fail_with = ImapError("Nie można połączyć z imap.example.com jako roman@z3x.pl", "network")
        result, _ = self.run_check(TWO, sessions)
        self.assertIn("imap.example.com jako roman@z3x.pl", result["results"][0]["message"])

    def test_a_very_long_error_text_is_cut(self):
        sessions = accounts()
        sessions.sessions["roman@z3x.pl"].fail_with = OSError("x" * 500)
        result, _ = self.run_check(TWO, sessions)
        self.assertEqual(len(result["results"][0]["message"]), len("OSError: ") + 200)

    def test_a_mailbox_without_recognisable_folders_reports_zero_messages_not_a_crash(self):
        lonely = FakeAccounts({"roman@z3x.pl": FakeImap({"Drafts": {1: make_eml()}})})
        result = imap_access.test_imap({**BASE, "IMAP_HOST": "imap.example.com"}, lambda: lonely)
        self.assertTrue(result["ok"])
        self.assertIn("0 wiadomości", result["message"])
        self.assertIn("brak rozpoznanych folderów", result["message"])

    def test_one_mailbox_gives_its_bare_message_and_several_give_a_joined_summary(self):
        single = imap_access.test_imap({**BASE, "IMAP_HOST": "imap.example.com"}, lambda: accounts())
        self.assertTrue(single["message"].startswith("Zalogowano jako roman@z3x.pl"))
        both, _ = self.run_check(TWO)
        self.assertEqual(both["message"], "roman@z3x.pl: OK; drugi@z3x.pl: OK")

    def test_a_half_filled_entry_is_a_config_error_message_not_a_crash(self):
        result = imap_access.test_imap({**BASE, "IMAP_PORT": "993"}, lambda: None)
        self.assertEqual((result["ok"], result["results"]), (False, []))
        self.assertIn("IMAP_HOST", result["message"])

    def test_a_blocked_factory_is_reported_not_raised(self):
        def blocked():
            raise ConfigError("zablokowane")

        result = imap_access.test_imap(TWO, blocked)
        self.assertEqual((result["ok"], result["message"]), (False, "zablokowane"))


if __name__ == "__main__":
    unittest.main()
