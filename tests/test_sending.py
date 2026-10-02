from __future__ import annotations

import io
import os
import unittest
from contextlib import redirect_stderr
from unittest import mock

import sqlalchemy.exc as sa_exc

from lib import config as cfg
from lib import mime_builder, sending
from lib import statuses as st
from lib.config import ConfigError
from tests.fixtures import FakeSmtp, make_db, put, sample_row

CONFIG = {"SMTP_USER": "roman@z3x.pl"}
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
        return sending.send_row(database, row_id, smtp_factory=lambda: smtp, config=CONFIG,
                                sender_name="Roman", **kw)

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


class RealSmtpGateTests(NoRealSmtpMixin, unittest.TestCase):
    CONFIG = {"SMTP_HOST": "h", "SMTP_PORT": "465", "SMTP_USER": "u", "SMTP_PASSWORD": "p"}

    def test_blocked_when_a_test_database_is_configured(self):
        with mock.patch.dict(os.environ, {"PERNO_DB_URL": "sqlite://"}):
            os.environ.pop("PERNO_ALLOW_REAL_SMTP", None)
            self.assertFalse(sending.real_smtp_allowed())
            with self.assertRaises(ConfigError):
                sending.smtp_factory_from_config(self.CONFIG)

    def test_allowed_with_explicit_override_or_without_test_database(self):
        with mock.patch.dict(os.environ, {"PERNO_DB_URL": "sqlite://", "PERNO_ALLOW_REAL_SMTP": "1"}):
            self.assertTrue(sending.real_smtp_allowed())
            self.assertTrue(callable(sending.smtp_factory_from_config(self.CONFIG)))
        with mock.patch.dict(os.environ):
            os.environ.pop("PERNO_DB_URL", None)
            self.assertTrue(sending.real_smtp_allowed())


if __name__ == "__main__":
    unittest.main()
