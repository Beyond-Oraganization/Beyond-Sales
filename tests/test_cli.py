from __future__ import annotations

import argparse
import io
import os
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from datetime import datetime
from unittest import mock

from perno import db as dbmod
from perno import settings as cfg
from perno.cli import send as send_emails
from perno.settings import ConfigError
from tests.fixtures import put, sample_row


class FakeClient:
    created = []
    sent = []

    def __init__(self, host, port, user, password, use_ssl):
        FakeClient.created.append((host, port, user))

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def send(self, message):
        FakeClient.sent.append(message)


class CliTests(unittest.TestCase):
    def setUp(self):
        FakeClient.created, FakeClient.sent = [], []
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.tmp = tmp.name
        self.config_path = os.path.join(self.tmp, "fake.env")
        with open(self.config_path, "w", encoding="utf-8") as f:
            f.write("SMTP_HOST=smtp.invalid\nSMTP_PORT=465\nSMTP_USER=demo@example.com\n"
                    "SMTP_PASSWORD=fake\nSENDER_NAME=Demo\n")
        env = mock.patch.dict(os.environ, {
            "PERNO_DB_URL": f"sqlite:///{os.path.join(self.tmp, 'demo.db')}",
            "PERNO_ALLOW_REAL_SMTP": "1",
        })
        env.start()
        self.addCleanup(env.stop)
        for name, value in (("INDEX_HTML", os.path.join(self.tmp, "index.html")),
                            ("LOG_DIR", os.path.join(self.tmp, "logs"))):
            patcher = mock.patch.object(cfg, name, value)
            patcher.start()
            self.addCleanup(patcher.stop)
        patcher = mock.patch("perno.mailer.smtp_client.SmtpClient", FakeClient)
        patcher.start()
        self.addCleanup(patcher.stop)

        self.database = dbmod.Database.from_config({})
        self.database.ensure_schema()

    def run_cli(self, *argv):
        args = send_emails.build_parser().parse_args(["--config", self.config_path, *argv])
        with redirect_stdout(io.StringIO()) as out:
            code = send_emails.run(args)
        return code, out.getvalue()

    def test_refuses_to_generate_into_an_empty_database(self):
        with self.assertRaises(dbmod.EmptyDatabase):
            self.run_cli("--dry-run")
        self.assertEqual(self.database.count_rows(), 0)

    def test_dry_run_generates_rows_but_sends_nothing(self):
        code, out = self.run_cli("--dry-run", "--allow-empty-db")
        self.assertEqual(code, 0)
        self.assertGreater(self.database.count_rows(), 90)
        self.assertEqual(FakeClient.sent, [])
        self.assertFalse(os.path.exists(cfg.INDEX_HTML))  # podgląd powstaje tylko na żądanie (dashboard.py)
        self.assertIn("nic nie wysłano", out)

    def test_sends_selected_row_and_records_status(self):
        put(self.database, sample_row(901, email="cel@example.com"))
        code, out = self.run_cli("--id", "eventy-901", "--yes", "--delay-min", "0", "--delay-max", "0")
        self.assertEqual(code, 0)
        self.assertEqual([m["To"] for m in FakeClient.sent], ["cel@example.com"])
        self.assertEqual(self.database.get("eventy-901")["status"], "wyslano")
        self.assertIn("OK    eventy-901", out)
        self.assertEqual(len(os.listdir(cfg.LOG_DIR)), 1)

    def test_daily_cap_blocks_sending(self):
        now = datetime.now().strftime("%Y-%m-%dT%H:%M:%S")
        put(self.database,
            sample_row(900, status="wyslano", sent_at=now),
            sample_row(901, email="cel@example.com"))
        code, out = self.run_cli("--id", "eventy-901", "--yes", "--daily-cap", "1")
        self.assertIn("Brak maili do wysłania", out)
        self.assertEqual(FakeClient.sent, [])

    def test_real_sending_is_blocked_for_a_test_database_without_override(self):
        put(self.database, sample_row(901))
        os.environ.pop("PERNO_ALLOW_REAL_SMTP")
        with self.assertRaises(ConfigError):
            self.run_cli("--id", "eventy-901", "--yes")
        self.assertEqual(FakeClient.sent, [])

    def test_test_send_goes_to_given_address_and_keeps_status(self):
        put(self.database, sample_row(902, email="firma@example.com"))
        self.run_cli("--test-send", "ja@example.com", "--id", "eventy-902")
        self.assertEqual([m["To"] for m in FakeClient.sent], ["ja@example.com"])
        self.assertEqual(self.database.get("eventy-902")["status"], "do_wyslania")

    def put_countries(self):
        put(self.database,
            sample_row(901, email="pl@example.com", country="PL"),
            sample_row(902, email="gb@example.com", country="GB"),
            sample_row(903, email="de@example.com", country="DE"),
            sample_row(904, email="none@example.com"))

    def send(self, *argv):
        return self.run_cli(*argv, "--yes", "--delay-min", "0", "--delay-max", "0", "--limit", "50")

    def test_country_filter_sends_only_to_those_countries(self):
        self.put_countries()
        code, out = self.send("--country", "GB,de")
        self.assertEqual(sorted(m["To"] for m in FakeClient.sent), ["de@example.com", "gb@example.com"])
        self.assertEqual([self.database.get(f"eventy-{n}")["status"] for n in (901, 904)], ["do_wyslania"] * 2)
        self.assertIn("Kraje: Niemcy 1, Wielka Brytania 1.", out)
        self.assertIn("UWAGA: 2 z tych maili jedzie do firm spoza Polski", out)

    def test_polish_only_batch_has_no_warning(self):
        self.put_countries()
        code, out = self.send("--country", "Polska")
        self.assertEqual([m["To"] for m in FakeClient.sent], ["pl@example.com"])
        self.assertIn("Kraje: Polska 1.", out)
        self.assertNotIn("UWAGA", out)

    def test_confirmation_shows_the_breakdown_before_asking_and_can_be_declined(self):
        self.put_countries()
        with mock.patch("builtins.input", return_value="n") as ask:
            code, out = self.run_cli("--country", "PL,GB", "--limit", "50")
        self.assertEqual(code, 0)
        ask.assert_called_once()
        self.assertIn("Kraje: Polska 1, Wielka Brytania 1.", out)
        self.assertIn("UWAGA: 1 z tych maili", out)
        self.assertLess(out.index("UWAGA"), out.index("Przerwano."))
        self.assertEqual(FakeClient.sent, [])

    def test_country_summary_puts_poland_first_and_does_not_count_unknown_as_foreign(self):
        rows = [{"country": c} for c in ("GB", "PL", "", "DE", "PL", "", "")]
        self.assertEqual(send_emails.country_summary(rows), ("Polska 2, Niemcy 1, Wielka Brytania 1, bez kraju 3", 2))
        self.assertEqual(send_emails.country_summary([{"country": None}]), ("bez kraju 1", 0))
        self.assertEqual(send_emails.country_summary([]), ("", 0))

    def test_without_the_option_every_country_is_included(self):
        self.put_countries()
        self.send("--id", "eventy-903")
        self.assertEqual([m["To"] for m in FakeClient.sent], ["de@example.com"])

    def test_parse_countries(self):
        self.assertEqual(send_emails.parse_countries("PL, gb"), ("PL", "GB"))
        self.assertEqual(send_emails.parse_countries("Polska,polska,Niemcy"), ("PL", "DE"))
        self.assertEqual(send_emails.parse_countries("none"), (dbmod.NO_COUNTRY,))
        for bad in ("", " , ", "Atlantyda", "PL,XX"):
            with self.assertRaises(argparse.ArgumentTypeError, msg=bad):
                send_emails.parse_countries(bad)

    def test_unknown_country_is_rejected_by_the_parser(self):
        with redirect_stderr(io.StringIO()) as err, self.assertRaises(SystemExit):
            send_emails.build_parser().parse_args(["--country", "Atlantyda"])
        self.assertIn("nieznany kraj", err.getvalue())

    def test_dry_run_counts_only_pending_rows_of_the_chosen_countries(self):
        self.put_countries()
        code, out = self.run_cli("--dry-run", "--country", "GB,DE")
        self.assertIn("2 maili w wybranych krajach", out)

    def test_test_send_respects_the_country_filter(self):
        self.put_countries()
        with self.assertRaises(SystemExit):
            self.run_cli("--test-send", "ja@example.com", "--id", "eventy-901", "--country", "GB")
        self.assertEqual(FakeClient.sent, [])
        self.run_cli("--test-send", "ja@example.com", "--id", "eventy-902", "--country", "GB")
        self.assertEqual([m["To"] for m in FakeClient.sent], ["ja@example.com"])

    def test_missing_table_gives_a_helpful_error(self):
        fresh = os.path.join(self.tmp, "fresh.db")
        with mock.patch.dict(os.environ, {"PERNO_DB_URL": f"sqlite:///{fresh}"}):
            with self.assertRaises(dbmod.DbError) as ctx:
                self.run_cli("--dry-run")
        self.assertIn("migrate_csv_to_sql.py", str(ctx.exception))


if __name__ == "__main__":
    unittest.main()
