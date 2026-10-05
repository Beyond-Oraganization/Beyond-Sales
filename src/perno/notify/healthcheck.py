"""Ping zewnętrznego „dead-man's switch” (np. healthchecks.io): alert przychodzi, gdy pingi znikną.

Wyłączony albo uśpiony komputer nikogo nie powiadomi sam, więc o jego milczeniu musi powiedzieć usługa z zewnątrz.
"""

from __future__ import annotations

import time
import urllib.error
import urllib.request
from typing import Callable

from perno.notify import NotifyError


def _get(url: str) -> None:
    try:
        with urllib.request.urlopen(url, timeout=10) as response:  # noqa: S310 — adres https sprawdza settings
            response.read(256)
    except (urllib.error.URLError, OSError) as exc:
        raise NotifyError(f"ping nie dotarł ({type(exc).__name__})") from None


class HealthcheckPinger:
    """Wysyła ping nie częściej niż co `min_interval` sekund; `fail()` zgłasza awarię od razu."""

    def __init__(self, url: str, *, get: Callable = _get, clock: Callable = time.monotonic, min_interval: float = 300.0):
        self._url = url.rstrip("/")
        self._get = get
        self._clock = clock
        self._min_interval = min_interval
        self._last: float | None = None
        self.secrets = (url,)

    def ping(self) -> bool:
        """Ping „żyję”; zwraca, czy faktycznie wysłano (False = jeszcze za wcześnie)."""
        now = self._clock()
        if self._last is not None and now - self._last < self._min_interval:
            return False
        self._get(self._url)
        self._last = now
        return True

    def fail(self) -> None:
        """Zgłasza awarię (healthchecks.io: adres + „/fail”)."""
        self._get(self._url + "/fail")
