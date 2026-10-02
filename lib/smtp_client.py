"""Cienki wrapper nad smtplib — łączenie, wysyłka jednej wiadomości, rozłączenie."""

from __future__ import annotations

import smtplib
from email.message import EmailMessage


class SmtpClient:
    def __init__(self, host: str, port: int, user: str, password: str, use_ssl: bool):
        self.host = host
        self.port = port
        self.user = user
        self.password = password
        self.use_ssl = use_ssl
        self._conn = None

    def __enter__(self) -> "SmtpClient":
        if self.use_ssl:
            self._conn = smtplib.SMTP_SSL(self.host, self.port, timeout=30)
        else:
            self._conn = smtplib.SMTP(self.host, self.port, timeout=30)
            self._conn.starttls()
        self._conn.login(self.user, self.password)
        return self

    def __exit__(self, exc_type, exc_val, exc_tb) -> None:
        if self._conn is not None:
            try:
                self._conn.quit()
            except Exception:
                pass

    def send(self, message: EmailMessage) -> None:
        self._conn.send_message(message)
