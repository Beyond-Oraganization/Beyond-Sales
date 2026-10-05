"""Push na telefon przez ntfy (https://docs.ntfy.sh/publish/): publikacja JSON, więc polskie znaki w tytule są bezpieczne."""

from __future__ import annotations

import json
import urllib.error
import urllib.request
from typing import Callable

from perno.domain import automat_events as ev
from perno.notify import Notification, NotifyError

PRIORITY = {ev.LEVEL_INFO: 3, ev.LEVEL_WARN: 4, ev.LEVEL_CRIT: 5}
TAGS = {ev.LEVEL_INFO: ["email"], ev.LEVEL_WARN: ["warning"], ev.LEVEL_CRIT: ["rotating_light"]}


def _post(url: str, data: bytes, headers: dict) -> None:
    request = urllib.request.Request(url, data=data, headers=headers, method="POST")  # noqa: S310 — adres https sprawdza settings
    try:
        with urllib.request.urlopen(request, timeout=10) as response:  # noqa: S310
            response.read(1024)
    except urllib.error.HTTPError as exc:
        raise NotifyError(f"serwer ntfy odpowiedział kodem {exc.code}") from None
    except (urllib.error.URLError, OSError) as exc:
        raise NotifyError(f"brak połączenia z serwerem ntfy ({type(exc).__name__})") from None


class NtfyChannel:
    """Kanał „ntfy”. Temat jest sekretem (kto go zna, może czytać i pisać), więc nie trafia do logów."""
    name = "ntfy"

    def __init__(self, topic: str, server: str = "https://ntfy.sh", post: Callable = _post):
        self._topic = topic
        self._server = server.rstrip("/")
        self._post = post
        self.secrets = (topic,)

    def send(self, notification: Notification) -> None:
        body = {"topic": self._topic, "title": notification.title[:200], "message": notification.message[:3500],
                "priority": PRIORITY.get(notification.level, 3), "tags": TAGS.get(notification.level, [])}
        self._post(self._server, json.dumps(body).encode("utf-8"), {"Content-Type": "application/json"})
