from __future__ import annotations

import io
import os
import unittest
from contextlib import redirect_stderr
from unittest import mock

import sqlalchemy.exc as sa_exc

from perno import settings as cfg
from perno.db import RowNotFound
from perno.domain import statuses as st
from perno.mailer import mime_builder
from perno.services import sending
from perno.settings import ConfigError, Environment
from tests.fixtures import FakeSmtp, make_db, put, sample_row

SENDER = sending.Sender("roman@z3x.pl", "Roman")
CID_BODY = f'<p>Cześć</p><img src="cid:{mime_builder.INLINE_IMAGE_CID}">'


class NoRealSmtpMixin:
    def setUp(self):
        boom = RuntimeError("test próbował otworzyć prawdziwe połączenie SMTP")
        patches = [mock.patch("smtplib.SMTP", side_effect=boom), mock.patch("smtplib.SMTP_SSL", side_effect=boom)]
        for p in patches:
            p.start()
            self.addCleanup(p.stop)


class SendRowTests(NoRealSmtpMixin, unittest.TestCase):
    def send(self, database, row_id, smtp, **kw):
        return sending.send_row(database, row_id, sending.Mailer(lambda: smtp, SENDER), **kw)

    def test_success_marks_sent_and_builds_message(self):
        database = make_db()
        put(database, sample_row(1, email="cel@example.com", subject="Temat X"))
        smtp = FakeSmtp()
        result = self.send(database, "eventy-001", smtp)
        self.assertEqual(result.outcome, "sent")
        self.assertEqual(database.get("eventy-001")["status"], st.STATUS_SENT)
        message = smtp.sent[0]
        self.assertEqual((message["To"], message["Subject"]), ("cel@example.com", "Temat X"))
        self.assertIn("Roman", message["From"])

    @unittest.skipUnless(os.path.exists(cfg.INLINE_IMAGE_SRC), "brak obrazka źródłowego")
    def test_inline_image_follows_the_body_not_the_column(self):
        database = make_db()
        put(database,
            sample_row(1, body_html=CID_BODY, inline_images=""),
            sample_row(2, body_html="<p>bez obrazka</p>", inline_images=mime_builder.INLINE_IMAGE_CID))
        with_image, without_image = FakeSmtp(), FakeSmtp()
        self.send(database, "eventy-001", with_image)
        self.send(database, "eventy-002", without_image)
        cids = lambda m: [p["Content-ID"] for p in m.walk() if p.get("Content-ID")]
        self.assertEqual(cids(with_image.sent[0]), [f"<{mime_builder.INLINE_IMAGE_CID}>"])
        self.assertEqual(cids(without_image.sent[0]), [])

    def test_failure_marks_error_and_counts_retry(self):
        database = make_db()
        put(database, sample_row(1))
        result = self.send(database, "eventy-001", FakeSmtp(fail_with=OSError("serwer padł")))
        row = database.get("eventy-001")
        self.assertEqual((result.outcome, row["status"], row["error"], row["retry_count"]),
                         ("error", st.STATUS_ERROR, "serwer padł", "1"))

    def test_double_click_sends_only_once(self):
        database = make_db()
        put(database, sample_row(1))
        smtp = FakeSmtp()
        first = self.send(database, "eventy-001", smtp)
        second = self.send(database, "eventy-001", smtp)
        self.assertEqual((first.outcome, second.outcome), ("sent", "skipped"))
        self.assertEqual(len(smtp.sent), 1)

    def test_rows_that_must_not_be_sent_are_skipped(self):
        database = make_db()
        put(database,
            sample_row(1, email=""),
            sample_row(2, status="wyslano", sent_at="2026-09-29T14:00:00"),
            sample_row(3, status="pominieto_klient"))
        smtp = FakeSmtp()
        for n in (1, 2, 3):
            self.assertEqual(self.send(database, f"eventy-{n:03d}", smtp).outcome, "skipped")
        self.assertEqual(smtp.sent, [])

    def test_cli_mode_does_not_retry_error_rows(self):
        database = make_db()
        put(database, sample_row(1, status="blad", error="x"))
        smtp = FakeSmtp()
        self.assertEqual(self.send(database, "eventy-001", smtp, allowed_from=(st.STATUS_PENDING,)).outcome, "skipped")
        self.assertEqual(self.send(database, "eventy-001", smtp).outcome, "sent")

    def test_ctrl_c_marks_error_and_reraises(self):
        database = make_db()
        put(database, sample_row(1))
        with self.assertRaises(KeyboardInterrupt):
            self.send(database, "eventy-001", FakeSmtp(fail_with=KeyboardInterrupt()))
        row = database.get("eventy-001")
        self.assertEqual(row["status"], st.STATUS_ERROR)
        self.assertIn("Przerwano", row["error"])

    def test_failed_bookkeeping_after_successful_send_is_reported_loudly(self):
        database = make_db()
        put(database, sample_row(1, email="cel@example.com"))
        smtp = FakeSmtp()
        broken = sa_exc.OperationalError("UPDATE", {}, Exception("baza zniknęła"))
        stderr = io.StringIO()
        with mock.patch.object(database, "mark_sent", side_effect=broken), \
                mock.patch("time.sleep"), redirect_stderr(stderr):
            result = self.send(database, "eventy-001", smtp)
        self.assertEqual(result.outcome, "sent_unrecorded")
        self.assertEqual(len(smtp.sent), 1)
        self.assertIn("WYSŁANO", stderr.getvalue())
        self.assertEqual(database.get("eventy-001")["status"], st.STATUS_SENDING)


FULL_CONFIG = {"SMTP_HOST": "smtp.invalid", "SMTP_PORT": "465", "SMTP_USER": "demo@example.com",
               "SMTP_PASSWORD": "fake", "SENDER_NAME": "Demo"}


class BuildMailerTests(NoRealSmtpMixin, unittest.TestCase):
    def test_sender_comes_from_the_configuration(self):
        mailer = sending.build_mailer(FULL_CONFIG, __file__, smtp_factory=lambda: None)
        self.assertEqual(mailer.sender, sending.Sender("demo@example.com", "Demo"))

    def test_injected_factory_bypasses_the_real_smtp_gate(self):
        env = Environment.from_environ({"PERNO_DB_URL": "sqlite://"})
        fake = object
        self.assertIs(sending.build_mailer(FULL_CONFIG, __file__, smtp_factory=fake, env=env).smtp_factory, fake)
        with self.assertRaises(ConfigError):
            sending.build_mailer(FULL_CONFIG, __file__, env=env)

    def test_incomplete_configuration_is_refused(self):
        with self.assertRaisesRegex(ConfigError, "SMTP_PASSWORD"):
            sending.build_mailer({**FULL_CONFIG, "SMTP_PASSWORD": ""}, __file__)
        with self.assertRaisesRegex(ConfigError, "Brak config.env"):
            sending.build_mailer({}, "/nie/ma/takiego/config.env")


class SendOneTests(NoRealSmtpMixin, unittest.TestCase):
    def setUp(self):
        super().setUp()
        self.database = make_db()
        self.smtp = FakeSmtp()
        self.mailer_calls = 0

    def mailer(self):
        self.mailer_calls += 1
        return sending.Mailer(lambda: self.smtp, SENDER)

    def send(self, row_id, **kw):
        return sending.send_one(self.database, row_id, self.mailer, **kw)

    def test_sends_and_records(self):
        put(self.database, sample_row(1))
        self.assertEqual(self.send("eventy-001").outcome, "sent")
        self.assertEqual(len(self.smtp.sent), 1)

    def test_unknown_row_is_reported_before_anything_else(self):
        with self.assertRaises(RowNotFound):
            self.send("brak")
        self.assertEqual(self.mailer_calls, 0)

    def test_row_problems_win_over_configuration_problems(self):
        put(self.database, sample_row(1, status="wyslano", sent_at="2026-09-29T10:00:00"))
        def broken():
            raise ConfigError("zła konfiguracja")

        with self.assertRaisesRegex(sending.NotSendable, "już został wysłany"):
            sending.send_one(self.database, "eventy-001", broken)

    def test_configuration_problem_stops_before_claiming_the_row(self):
        put(self.database, sample_row(1))

        def broken():
            raise ConfigError("brak hasła")

        with self.assertRaises(ConfigError):
            sending.send_one(self.database, "eventy-001", broken)
        self.assertEqual(self.database.get("eventy-001")["status"], st.STATUS_PENDING)

    def test_daily_cap_counts_what_was_already_sent_today(self):
        put(self.database, sample_row(1, status="wyslano", sent_at="2026-09-30T08:00:00"), sample_row(2))
        with self.assertRaises(sending.DailyCapReached):  # FakeClock stoi na 2026-09-30
            self.send("eventy-002", daily_cap=1)
        self.assertEqual(self.smtp.sent, [])
        self.assertEqual(self.send("eventy-002", daily_cap=2).outcome, "sent")

    def test_no_cap_means_no_limit(self):
        put(self.database, sample_row(1))
        self.assertEqual(self.send("eventy-001", daily_cap=None).outcome, "sent")


class SendBatchTests(NoRealSmtpMixin, unittest.TestCase):
    def setUp(self):
        super().setUp()
        self.database = make_db()
        put(self.database, sample_row(1), sample_row(2), sample_row(3))
        self.smtp = FakeSmtp()
        self.connections = 0

        def factory():
            self.connections += 1
            return self.smtp

        self.mailer = sending.Mailer(factory, SENDER)
        self.rows = self.database.select_candidates(limit=None)

    def test_sends_everything_over_one_connection_with_pauses_between_mails(self):
        pauses, seen = [], []
        results = sending.send_batch(self.database, self.mailer, self.rows, delay=(3.0, 9.0),
                                     on_result=lambda row, result: seen.append((row["id"], result.outcome)),
                                     sleep=pauses.append, jitter=lambda low, high: (low + high) / 2)
        self.assertEqual([result.outcome for _, result in results], ["sent"] * 3)
        self.assertEqual(seen, [("eventy-001", "sent"), ("eventy-002", "sent"), ("eventy-003", "sent")])
        self.assertEqual(pauses, [6.0, 6.0])  # między mailami, nie po ostatnim
        self.assertEqual(self.connections, 1)
        self.assertEqual(len(self.smtp.sent), 3)

    def test_only_pending_rows_are_sent_and_the_rest_is_skipped(self):
        self.database.apply_edit("eventy-002", {"status": "blad"})
        results = sending.send_batch(self.database, self.mailer, self.rows, sleep=lambda s: None)
        self.assertEqual([result.outcome for _, result in results], ["sent", "skipped", "sent"])

    def test_ctrl_c_propagates_and_the_row_in_flight_becomes_an_error(self):
        self.smtp.fail_with = KeyboardInterrupt()
        with self.assertRaises(KeyboardInterrupt):
            sending.send_batch(self.database, self.mailer, self.rows, sleep=lambda s: None)
        self.assertEqual(self.database.get("eventy-001")["status"], st.STATUS_ERROR)
        self.assertEqual(self.database.get("eventy-002")["status"], st.STATUS_PENDING)


class RealSmtpGateTests(NoRealSmtpMixin, unittest.TestCase):
    CONFIG = {"SMTP_HOST": "h", "SMTP_PORT": "465", "SMTP_USER": "u", "SMTP_PASSWORD": "p"}

    def test_blocked_when_a_test_database_is_configured(self):
        env = Environment.from_environ({"PERNO_DB_URL": "sqlite://"})
        self.assertFalse(env.real_smtp_allowed)
        with self.assertRaises(ConfigError):
            sending.smtp_factory_from_config(self.CONFIG, env)

    def test_allowed_with_explicit_override_or_without_test_database(self):
        env = Environment.from_environ({"PERNO_DB_URL": "sqlite://", "PERNO_ALLOW_REAL_SMTP": "1"})
        self.assertTrue(env.real_smtp_allowed)
        self.assertTrue(callable(sending.smtp_factory_from_config(self.CONFIG, env)))
        self.assertTrue(Environment.from_environ({}).real_smtp_allowed)

    def test_process_environment_is_the_default(self):
        with mock.patch.dict(os.environ, {"PERNO_DB_URL": "sqlite://"}):
            os.environ.pop("PERNO_ALLOW_REAL_SMTP", None)
            with self.assertRaises(ConfigError):
                sending.smtp_factory_from_config(self.CONFIG)

    def test_incomplete_or_malformed_smtp_settings_are_rejected_up_front(self):
        env = Environment()
        with self.assertRaisesRegex(ConfigError, "SMTP_PASSWORD"):
            sending.smtp_factory_from_config({**self.CONFIG, "SMTP_PASSWORD": ""}, env)
        with self.assertRaisesRegex(ConfigError, "SMTP_PORT"):
            sending.smtp_factory_from_config({**self.CONFIG, "SMTP_PORT": "abc"}, env)


if __name__ == "__main__":
    unittest.main()
