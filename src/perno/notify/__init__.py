"""Powiadomienia automatu: kanały (macOS, ntfy, e-mail), ping zewnętrznego dead-man's switch i rozsyłanie.

Adaptery nie znają zasad biznesowych: które zdarzenie idzie którym kanałem ustala `perno.domain.automat_events`, a
treści tworzy `perno.services.automat`. Każdy kanał ma wstrzykiwany transport, więc testy nie dotykają sieci ani systemu.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Mapping, Protocol, Sequence

logger = logging.getLogger(__name__)


class NotifyError(Exception):
    """Kanał nie zdołał dostarczyć powiadomienia (komunikat bez sekretów)."""


@dataclass(frozen=True)
class Notification:
    """Treść powiadomienia; `level` to „info”, „warn” albo „crit” (z `perno.domain.automat_events`)."""
    title: str
    message: str
    level: str = "info"


class Channel(Protocol):
    """Jeden sposób dostarczenia powiadomienia."""
    name: str
    secrets: tuple  # fragmenty (adres, temat), które nie mogą trafić do logów ani komunikatów błędów

    def send(self, notification: Notification) -> None:
        """Dostarcza powiadomienie albo rzuca `NotifyError`."""
        ...


@dataclass
class DispatchResult:
    """Które kanały dostarczyły powiadomienie, a które zawiodły (z powodem) albo nie są skonfigurowane."""
    sent: list = field(default_factory=list)
    failed: dict = field(default_factory=dict)
    skipped: list = field(default_factory=list)


def scrub(text: str, secrets: Sequence[str]) -> str:
    """Zastępuje sekrety w tekście gwiazdkami (komunikaty błędów sieci potrafią zawierać adres z tematem ntfy)."""
    for secret in secrets:
        if secret:
            text = text.replace(secret, "***")
    return text


class Notifier:
    """Rozsyła powiadomienie do wskazanych kanałów; awaria jednego nie przeszkadza pozostałym ani wołającemu."""

    def __init__(self, channels: Mapping[str, Channel]):
        self.channels = dict(channels)

    def dispatch(self, notification: Notification, wanted: Sequence[str]) -> DispatchResult:
        """Wysyła do kanałów z `wanted`, które są skonfigurowane; nigdy nie rzuca wyjątku."""
        result = DispatchResult()
        for name in wanted:
            channel = self.channels.get(name)
            if channel is None:
                result.skipped.append(name)
                continue
            try:
                channel.send(notification)
            except Exception as exc:  # noqa: BLE001 — granica powiadomień: żaden kanał nie może zatrzymać wysyłki maili
                reason = scrub(f"{type(exc).__name__}: {exc}", channel.secrets)
                logger.warning("Powiadomienie (%s) nie powiodło się: %s", name, reason)
                result.failed[name] = reason
            else:
                result.sent.append(name)
        return result
