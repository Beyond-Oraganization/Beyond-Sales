"""Dziennik zdarzeń automatu z powiadomieniami: zapis w bazie, a potem (jeśli trzeba) rozesłanie kanałami.

Wspólny dla demona (`services.automat`) i sterowania z panelu (`services.automat_control`), żeby każde zdarzenie
wyglądało tak samo niezależnie od tego, kto je wywołał.
"""

from __future__ import annotations

import logging
from typing import Callable, Optional

from perno.db.automat_store import AutomatStore
from perno.domain import automat_events as ev
from perno.notify import Notification, Notifier

logger = logging.getLogger(__name__)

TITLES = {
    ev.EVENT_DAEMON_START: "Automat: demon wystartował",
    ev.EVENT_ARMED: "Automat: plan uzbrojony",
    ev.EVENT_RESUMED: "Automat: wznowiono po przerwie",
    ev.EVENT_DAILY_SUMMARY: "Automat: podsumowanie dnia",
    ev.EVENT_SKIPPED_DAY: "Automat: pominięty dzień wysyłki",
    ev.EVENT_PAUSED: "Automat: WSTRZYMANY po błędach",
    ev.EVENT_PAUSED_BY_USER: "Automat: wstrzymany",
    ev.EVENT_STUCK_ROW: "Automat: sprawdź folder Wysłane",
    ev.EVENT_QUEUE_EMPTY: "Automat: kolejka pusta, koniec planu",
    ev.EVENT_HORIZON_END: "Automat: plan zakończony",
    ev.EVENT_DISARMED: "Automat: plan zatrzymany",
    ev.EVENT_DB_DOWN: "Automat: baza niedostępna",
    ev.EVENT_SETTINGS_CHANGED: "Automat: zmieniono limity",
    ev.EVENT_INTERNAL_ERROR: "Automat: nieoczekiwany błąd",
    ev.EVENT_ON_BATTERY: "Automat: Mac na baterii",
}


class EventLog:
    """Zapisuje zdarzenia i powiadamia o nich zgodnie z `perno.domain.automat_events` (z ograniczeniem częstości)."""

    def __init__(self, store: AutomatStore, notifier: Notifier, now: Callable):
        self.store = store
        self.notifier = notifier
        self._now = now

    def emit(self, event_type: str, text: str, *, key: Optional[str] = None, notify: bool = True) -> int:
        """Zapisuje zdarzenie; gdy `notify`, rozsyła je kanałami tego typu. Zwraca id zdarzenia."""
        level = ev.level_for(event_type)
        event_id = self.store.add_event(level, event_type, text, key)
        if notify and ev.channels_for(event_type) and ev.should_notify(
                event_type, self.store.last_notified_at(event_type), self._now()):
            result = self.notifier.dispatch(Notification(TITLES.get(event_type, "Automat"), text, level),
                                            ev.channels_for(event_type))
            if result.sent:
                self.store.mark_notified(event_id, result.sent)
            for channel, reason in result.failed.items():
                logger.warning("Kanał %s nie dostarczył zdarzenia %s: %s", channel, event_type, reason)
        return event_id
