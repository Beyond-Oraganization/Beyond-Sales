"""Podsumowania i alerty e-mailem, tą samą skrzynką SMTP co wysyłka (jeden mail dziennie nie rusza limitu kampanii)."""

from __future__ import annotations

import html
from typing import Callable

from perno.mailer import mime_builder
from perno.notify import Notification, NotifyError


class EmailChannel:
    """Kanał „email”: wiadomość do siebie (`NOTIFY_EMAIL`)."""
    name = "email"

    def __init__(self, smtp_factory: Callable, from_addr: str, to_addr: str, from_name: str = "Perno automat"):
        self._smtp_factory = smtp_factory
        self._from_addr = from_addr
        self._from_name = from_name
        self._to_addr = to_addr
        self.secrets: tuple = ()

    def send(self, notification: Notification) -> None:
        text = notification.message
        paragraphs = "".join(f"<p>{html.escape(line)}</p>" for line in text.splitlines() if line.strip())
        message = mime_builder.build_message(
            from_addr=self._from_addr, from_name=self._from_name, to_addr=self._to_addr,
            subject=f"[Perno automat] {notification.title}", html_body=paragraphs or "<p></p>", text_body=text)
        try:
            with self._smtp_factory() as client:
                client.send(message)
        except Exception as exc:  # noqa: BLE001 — każdy błąd SMTP to „nie dostarczono”; powód trafia do dziennika
            raise NotifyError(f"wysyłka SMTP nie powiodła się ({type(exc).__name__})") from None
