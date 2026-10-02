"""Klient SMTP: połączenie, logowanie, wysyłka i zamknięcie — na atrapie smtplib, bez sieci."""

from __future__ import annotations

import smtplib
import unittest
from email.message import EmailMessage
from unittest import mock

from perno.mailer.smtp_client import SmtpClient


def message() -> EmailMessage:
    msg = EmailMessage()
    msg["From"], msg["To"], msg["Subject"] = "a@example.com", "b@example.com", "Temat"
    msg.set_content("treść")
    return msg


class SmtpClientTests(unittest.TestCase):
    def test_tests_cannot_reach_a_real_smtp_server_by_accident(self):
        with self.assertRaisesRegex(RuntimeError, "prawdziwe połączenie SMTP"):
            smtplib.SMTP("smtp.example.com", 25)
        with self.assertRaisesRegex(RuntimeError, "prawdziwe połączenie SMTP"):
            SmtpClient("smtp.example.com", 465, "u", "p", True).__enter__()

    def test_ssl_connection_logs_in_sends_and_quits(self):
        with mock.patch("smtplib.SMTP_SSL") as ssl_class, mock.patch("smtplib.SMTP") as plain_class:
            with SmtpClient("smtp.example.com", 465, "user", "haslo", True) as client:
                client.send(message())
        ssl_class.assert_called_once_with("smtp.example.com", 465, timeout=30)
        plain_class.assert_not_called()
        conn = ssl_class.return_value
        conn.login.assert_called_once_with("user", "haslo")
        conn.send_message.assert_called_once()
        conn.quit.assert_called_once()

    def test_plain_connection_upgrades_with_starttls_before_logging_in(self):
        with mock.patch("smtplib.SMTP") as plain_class:
            with SmtpClient("smtp.example.com", 587, "user", "haslo", False):
                pass
        conn = plain_class.return_value
        plain_class.assert_called_once_with("smtp.example.com", 587, timeout=30)
        self.assertEqual([call[0] for call in conn.method_calls[:2]], ["starttls", "login"])

    def test_a_failing_quit_is_logged_at_debug_and_never_hides_the_result(self):
        with mock.patch("smtplib.SMTP_SSL") as ssl_class:
            ssl_class.return_value.quit.side_effect = smtplib.SMTPServerDisconnected("połączenie zerwane")
            with self.assertLogs("perno.mailer.smtp_client", level="DEBUG") as logs:
                with SmtpClient("smtp.example.com", 465, "user", "haslo", True):
                    pass
        self.assertIn("quit()", logs.output[0])
        self.assertTrue(logs.records[0].exc_info)

    def test_exceptions_from_the_body_still_propagate(self):
        with mock.patch("smtplib.SMTP_SSL") as ssl_class:
            with self.assertRaises(ValueError), SmtpClient("h", 465, "u", "p", True):
                raise ValueError("błąd w treści")
        ssl_class.return_value.quit.assert_called_once()


if __name__ == "__main__":
    unittest.main()
