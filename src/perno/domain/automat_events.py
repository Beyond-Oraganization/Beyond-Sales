"""Zdarzenia automatu i to, którymi kanałami o nich powiadamiamy (czysta tabela reguł, bez wysyłania)."""

from __future__ import annotations

from datetime import datetime
from typing import Optional

LEVEL_INFO = "info"
LEVEL_WARN = "warn"
LEVEL_CRIT = "crit"

CHANNEL_MACOS = "macos"
CHANNEL_NTFY = "ntfy"
CHANNEL_EMAIL = "email"

EVENT_DAEMON_START = "daemon_start"  # demon wystartował (po zalogowaniu albo restarcie)
EVENT_ARMED = "armed"
EVENT_RESUMED = "resumed"  # wznowienie po uśpieniu komputera albo po pauzie
EVENT_DAILY_SUMMARY = "daily_summary"
EVENT_SKIPPED_DAY = "skipped_day"
EVENT_PAUSED = "paused"  # automatyczna pauza po błędach
EVENT_PAUSED_BY_USER = "paused_by_user"
EVENT_STUCK_ROW = "stuck_row"  # mail mógł wyjść, ale baza go nie zapisała
EVENT_QUEUE_EMPTY = "queue_empty"
EVENT_HORIZON_END = "horizon_end"
EVENT_DISARMED = "disarmed"
EVENT_DB_DOWN = "db_down"
EVENT_SETTINGS_CHANGED = "settings_changed"
EVENT_SENT = "sent"  # jeden udany mail (tylko dziennik, bez powiadomienia)
EVENT_ROW_ERROR = "row_error"  # nieudany mail (dziennik; o serii błędów informuje EVENT_PAUSED)
EVENT_INTERNAL_ERROR = "internal_error"  # nieoczekiwany wyjątek w pętli demona
EVENT_ON_BATTERY = "on_battery"  # okno wysyłki zaczęło się, gdy Mac był na baterii

_ALL = (CHANNEL_MACOS, CHANNEL_NTFY, CHANNEL_EMAIL)

# typ zdarzenia -> (poziom, kanały, minimalny odstęp między kolejnymi powiadomieniami tego samego typu w sekundach)
EVENTS: dict = {
    EVENT_DAEMON_START: (LEVEL_INFO, (CHANNEL_MACOS,), 0),
    EVENT_ARMED: (LEVEL_INFO, (CHANNEL_MACOS,), 0),
    EVENT_RESUMED: (LEVEL_INFO, (CHANNEL_MACOS,), 1800),
    EVENT_DAILY_SUMMARY: (LEVEL_INFO, _ALL, 0),
    EVENT_SKIPPED_DAY: (LEVEL_WARN, (CHANNEL_MACOS, CHANNEL_NTFY), 0),
    EVENT_PAUSED: (LEVEL_CRIT, _ALL, 0),
    EVENT_PAUSED_BY_USER: (LEVEL_INFO, (CHANNEL_MACOS,), 0),
    EVENT_STUCK_ROW: (LEVEL_CRIT, _ALL, 0),
    EVENT_QUEUE_EMPTY: (LEVEL_INFO, _ALL, 0),
    EVENT_HORIZON_END: (LEVEL_INFO, _ALL, 0),
    EVENT_DISARMED: (LEVEL_INFO, (CHANNEL_MACOS,), 0),
    EVENT_DB_DOWN: (LEVEL_CRIT, (CHANNEL_MACOS, CHANNEL_NTFY), 3600),
    EVENT_SETTINGS_CHANGED: (LEVEL_INFO, (CHANNEL_MACOS,), 0),
    EVENT_SENT: (LEVEL_INFO, (), 0),
    EVENT_ROW_ERROR: (LEVEL_WARN, (), 0),
    EVENT_INTERNAL_ERROR: (LEVEL_CRIT, (CHANNEL_MACOS, CHANNEL_NTFY), 3600),
    EVENT_ON_BATTERY: (LEVEL_WARN, (CHANNEL_MACOS,), 21600),
}


def level_for(event_type: str) -> str:
    """Poziom zdarzenia (nieznany typ traktujemy jak ostrzeżenie, żeby nie zginął po cichu)."""
    return EVENTS.get(event_type, (LEVEL_WARN, (), 0))[0]


def channels_for(event_type: str) -> tuple:
    """Kanały powiadomień dla typu zdarzenia (pusta krotka = tylko zapis w dzienniku)."""
    return EVENTS.get(event_type, (LEVEL_WARN, (), 0))[1]


def should_notify(event_type: str, last_notified: Optional[datetime], now: datetime) -> bool:
    """Czy minął wymagany odstęp od ostatniego powiadomienia tego samego typu (zapobiega lawinie przy powtarzającej się awarii)."""
    gap = EVENTS.get(event_type, (LEVEL_WARN, (), 0))[2]
    if gap <= 0 or last_notified is None:
        return True
    return (now - last_notified).total_seconds() >= gap
