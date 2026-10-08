"""ai.py odpowiedzi: pobierz, doctor, stan (sqlite i atrapa poczty, bez sieci)."""

from __future__ import annotations

import contextlib
import io
import os
import tempfile
import unittest
from datetime import datetime
from unittest import mock

import sqlalchemy as sa

from perno import settings as cfg
from perno.cli import ai, ai_replies
from perno.db.schema import maile
from tests import eml_samples as eml
from tests.fake_imap import FakeAccounts, FakeImap
from tests.fixtures import FakeClock, make_db, put, sample_row

CONFIG = ("SMTP_HOST=s\nSMTP_PORT=465\nSMTP_USER=roman@z3x.pl\nSMTP_PASSWORD=p\nIMAP_HOST=imap.example.com\n"
          "SMTP2_HOST=s\nSMTP2_PORT=465\nSMTP2_USER=drugi@z3x.pl\nSMTP2_PASSWORD=p2\nIMAP2_HOST=imap2.example.com\n"
          "SMTP3_HOST=s\nSMTP3_PORT=465\nSMTP3_USER=trzeci@z3x.pl\nSMTP3_PASSWORD=p3\n"
          "REPLIES_IGNORE=z3x.io\n")


class CliCase(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.tmp = tmp.name
        self.url = f"sqlite:///{os.path.join(self.tmp, 'replies.db')}"
        self.database = make_db(FakeClock(datetime(2026, 10, 8, 10, 0)), url=self.url)
        self.addCleanup(self.database.engine.dispose)
        self.config = os.path.join(self.tmp, "config.env")
        with open(self.config, "w", encoding="utf-8") as handle:
            handle.write(CONFIG)
        self.backups = os.path.join(self.tmp, "backup")
        for patcher in (mock.patch.dict(os.environ, {"PERNO_DB_URL": self.url}), mock.patch.object(cfg, "BACKUP_DIR", self.backups)):
            patcher.start()
            self.addCleanup(patcher.stop)
        put(self.database, sample_row(1, status="wyslano", sent_at="2026-09-29T13:00:00", email="anna@mylimo.pl", subject="Rezerwacje limuzyn, Kraków"))
        with self.database.engine.begin() as conn:
            conn.execute(sa.update(maile).values(sent_from="roman@z3x.pl", message_id="orig1@z3x.pl"))
        first = FakeImap({"INBOX": {}, "Sent": {}}, roles={"Sent": "sent"})
        first.add("INBOX", 1, eml.plain_reply(in_reply_to="<orig1@z3x.pl>", body="Dzień dobry, proszę o cennik."))
        first.add("INBOX", 2, eml.newsletter())
        self.sessions = FakeAccounts({"roman@z3x.pl": first, "drugi@z3x.pl": FakeImap({"INBOX": {}})})
        patch = mock.patch.object(ai_replies, "imap_factory", lambda: self.sessions)
        patch.start()
        self.addCleanup(patch.stop)

    def ai(self, *argv):
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            code = ai.main(["--config", self.config, "odpowiedzi", *argv])
        return code, out.getvalue(), err.getvalue()

    def status(self):
        return self.database.get("eventy-001")["status"]


class FetchTests(CliCase):
    def test_a_trial_run_reports_and_writes_nothing_not_even_a_backup(self):
        code, out, _ = self.ai("pobierz", "--dry-run")
        self.assertEqual(code, 0)
        self.assertIn("PRÓBA", out)
        self.assertIn("dotyczą nas 1", out)
        self.assertEqual((self.status(), os.path.exists(self.backups)), ("wyslano", False))
        self.assertNotIn("Kopia zapasowa", out)

    def test_a_real_run_makes_a_backup_stores_the_reply_and_marks_the_company(self):
        code, out, _ = self.ai("pobierz")
        self.assertEqual(code, 0)
        self.assertIn("Kopia zapasowa:", out)
        self.assertIn("zapisano 1", out)
        self.assertIn("„odpowiedział” 1", out)
        self.assertEqual(self.status(), "odpowiedzial")
        self.assertTrue(any(name.startswith("Maile_") for name in os.listdir(self.backups)))

    def test_the_output_never_names_third_parties_or_quotes_their_text(self):
        _, out, _ = self.ai("pobierz")
        for secret in ("anna@mylimo.pl", "proszę o cennik", "Anna Nowak", "newsletter"):
            self.assertNotIn(secret, out)

    def test_a_second_run_finds_nothing_new(self):
        self.ai("pobierz")
        _, out, _ = self.ai("pobierz")
        self.assertIn("nowych wiadomości 0", out)
        self.assertIn("zapisano 0", out)

    def test_the_backup_covers_the_reply_tables_after_the_first_run(self):
        self.ai("pobierz")
        self.ai("pobierz")
        names = os.listdir(self.backups)
        self.assertTrue(any(n.startswith("OdpWiadomosci_") for n in names), names)
        self.assertTrue(any(n.startswith("OdpWatki_") for n in names), names)

    def test_a_failing_mailbox_gives_a_nonzero_exit_and_a_readable_line(self):
        self.sessions.sessions["drugi@z3x.pl"].fail_with = TimeoutError("timed out")
        code, out, _ = self.ai("pobierz", "--dry-run")
        self.assertEqual(code, 1)
        self.assertIn("drugi@z3x.pl: BŁĄD (network)", out)
        self.assertIn("roman@z3x.pl: OK", out)

    def test_one_mailbox_can_be_chosen(self):
        self.ai("pobierz", "--dry-run", "--skrzynka", "drugi@z3x.pl")
        self.assertEqual(self.sessions.opened, ["drugi@z3x.pl"])

    def test_a_bad_date_is_a_clear_error(self):
        code, out, err = self.ai("pobierz", "--od", "wczoraj")
        self.assertEqual((code, out), (1, ""))
        self.assertIn("--od", err)

    def test_a_demo_database_refuses_real_connections_when_no_fake_is_injected(self):
        with mock.patch.object(ai_replies, "imap_factory", ai_replies.imap_access.real_factory):
            code, _, err = self.ai("pobierz", "--dry-run")
        self.assertEqual(code, 1)
        self.assertIn("PERNO_ALLOW_REAL_IMAP", err)


class DoctorAndStateTests(CliCase):
    def test_doctor_checks_every_imap_mailbox_and_names_the_ones_without(self):
        code, out, _ = self.ai("doctor")
        self.assertEqual(code, 0)
        self.assertIn("roman@z3x.pl: OK", out)
        self.assertIn("drugi@z3x.pl: OK", out)
        self.assertIn("Skrzynki bez IMAP (ich odpowiedzi nie są pobierane): 1, np. trzeci@z3x.pl", out)

    def test_doctor_fails_when_a_login_fails(self):
        self.sessions.sessions["roman@z3x.pl"].fail_with = TimeoutError("timed out")
        code, out, _ = self.ai("doctor")
        self.assertEqual(code, 1)
        self.assertIn("roman@z3x.pl: BŁĄD", out)

    def test_state_before_and_after_a_fetch(self):
        _, before, _ = self.ai("stan")
        self.assertIn("Wątki z odpowiedzią człowieka: 0", before)
        self.assertIn("Odbiór poczty: 2 z 3 skrzynek ma ustawiony IMAP.", before)
        self.assertIn("Ostatni przebieg: nigdy.", before)
        self.ai("pobierz")
        _, after, _ = self.ai("stan")
        self.assertIn("Wątki z odpowiedzią człowieka: 1 (czeka na odpowiedź 1, nieprzeczytane 1, nieocenione 1)", after)
        self.assertIn("roman@z3x.pl: ostatnio", after)
        self.assertNotIn("Ostatni przebieg: nigdy", after)

    def test_the_help_lists_the_new_commands_without_breaking_the_old_ones(self):
        out = io.StringIO()
        with contextlib.redirect_stdout(out), self.assertRaises(SystemExit):
            ai.main(["--help"])
        self.assertIn("odpowiedzi", out.getvalue())
        self.assertIn("pobierz", out.getvalue())
        self.assertIn("brief", out.getvalue())


class SettingsTests(unittest.TestCase):
    def test_defaults_and_parsing(self):
        self.assertEqual(cfg.ReplySettings.from_config({}), cfg.ReplySettings((), (), 4))
        got = cfg.ReplySettings.from_config({"REPLIES_IGNORE": " Jakub@Z3X.io, z3x.io ,", "REPLIES_FOLDERS": "INBOX, Wysłane", "REPLIES_FETCH_WORKERS": "8"})
        self.assertEqual((got.ignore, got.folders, got.workers), (("jakub@z3x.io", "z3x.io"), ("INBOX", "Wysłane"), 8))

    def test_bad_worker_counts_are_config_errors(self):
        for value in ("x", "0", "17", "-1"):
            with self.subTest(value=value), self.assertRaises(cfg.ConfigError):
                cfg.ReplySettings.from_config({"REPLIES_FETCH_WORKERS": value})
        for value in ("1", "16"):
            self.assertEqual(cfg.ReplySettings.from_config({"REPLIES_FETCH_WORKERS": value}).workers, int(value))


if __name__ == "__main__":
    unittest.main()
