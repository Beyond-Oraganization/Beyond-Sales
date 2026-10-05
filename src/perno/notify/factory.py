"""Składanie kanałów powiadomień z ustawień (`NotifySettings`); transporty można podmienić w testach."""

from __future__ import annotations

from typing import Callable, Optional

from perno.notify import Notifier
from perno.notify.email_channel import EmailChannel
from perno.notify.healthcheck import HealthcheckPinger
from perno.notify.macos import MacosChannel
from perno.notify.ntfy import NtfyChannel
from perno.settings import NotifySettings


def build_notifier(settings: NotifySettings, *, smtp_factory: Optional[Callable] = None, from_addr: str = "") -> Notifier:
    """Notifier z kanałami, które są skonfigurowane (kanał bez ustawień po prostu nie istnieje i jest pomijany)."""
    channels: dict = {}
    if settings.macos:
        channels["macos"] = MacosChannel()
    if settings.ntfy_topic:
        channels["ntfy"] = NtfyChannel(settings.ntfy_topic, settings.ntfy_server)
    if settings.email_to and smtp_factory is not None and from_addr:
        channels["email"] = EmailChannel(smtp_factory, from_addr, settings.email_to)
    return Notifier(channels)


def build_pinger(settings: NotifySettings) -> Optional[HealthcheckPinger]:
    """Pinger dead-man's switch albo `None`, gdy `HEALTHCHECK_URL` nie jest ustawiony."""
    return HealthcheckPinger(settings.healthcheck_url) if settings.healthcheck_url else None
