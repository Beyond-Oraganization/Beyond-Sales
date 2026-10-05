"""Powiadomienie systemowe macOS przez `osascript` (Centrum powiadomień)."""

from __future__ import annotations

import subprocess
import sys
from typing import Callable

from perno.notify import Notification, NotifyError

OSASCRIPT = "/usr/bin/osascript"
# Treść i tytuł idą jako argumenty skryptu, nie jako tekst wklejony do AppleScriptu: nazwy firm pochodzą z bazy
# (czasem z cudzych stron), więc cudzysłów w nazwie nie może zmienić polecenia.
SCRIPT = ("on run argv", "display notification (item 1 of argv) with title (item 2 of argv)", "end run")


class MacosChannel:
    """Kanał „macos”. Wymaga jednorazowej zgody na powiadomienia dla aplikacji, która uruchamia osascript (Terminal)."""
    name = "macos"
    secrets: tuple = ()

    def __init__(self, run: Callable = subprocess.run, platform: str = sys.platform):
        self._run = run
        self._platform = platform

    def send(self, notification: Notification) -> None:
        if self._platform != "darwin":
            raise NotifyError("Powiadomienia systemowe działają tylko na macOS.")
        command = [OSASCRIPT, *[arg for line in SCRIPT for arg in ("-e", line)], "--",
                   notification.message[:500], notification.title[:100]]
        try:
            done = self._run(command, capture_output=True, text=True, timeout=10, check=False)
        except (OSError, subprocess.SubprocessError) as exc:
            raise NotifyError(f"osascript nie wystartował: {exc}") from exc
        if done.returncode != 0:
            raise NotifyError(f"osascript zwrócił kod {done.returncode}: {(done.stderr or '').strip()[:200]}")
