"""Stan skrzynek nadawczych w Konfiguracji: wyłączenie przez demon automatu, ostatni test połączenia i dzisiejsze wysyłki.

Demon zapisuje wyłączone skrzynki (adres, powód, chwilę powrotu) w znaku życia; test z panelu zapamiętujemy w pamięci
serwera (`AppContext.mailbox_tests`). Do przeglądarki nie trafiają hasła, a komunikaty błędów są już oczyszczone.
"""

from __future__ import annotations

from datetime import datetime
from typing import Mapping, MutableMapping, Sequence

from perno.db import Database
from perno.db.automat_store import AutomatStore
from perno.domain import mailboxes as mb
from perno.services import sending
from perno.settings import SmtpSettings

_STAMP = "%Y-%m-%dT%H:%M:%S"


def record_tests(tests: MutableMapping, results: Sequence[Mapping], now: datetime) -> None:
    """Zapamiętuje wyniki testu połączenia (po adresie, małymi literami)."""
    for result in results:
        tests[result["address"].casefold()] = {"ok": bool(result["ok"]), "message": result["message"], "at": now.strftime(_STAMP)}


def _off_entries(store: AutomatStore, now: datetime) -> dict:
    """Wyłączone skrzynki według demona: adres (małymi literami) → `{"blad", "do"}`; wygasłe wpisy pomijamy."""
    run = store.active_run() or {}
    info = (run.get("statystyki") or {}).get("wylaczone_info") or []
    return {entry["address"].casefold(): entry for entry in info
            if isinstance(entry, dict) and entry.get("address") and (entry.get("do") or "") > now.strftime(_STAMP)}


def describe(db: Database, store: AutomatStore, boxes: Sequence[SmtpSettings], tests: Mapping, now: datetime) -> list:
    """Stan każdej skrzynki z konfiguracji: `[{"slot", "address", "state", "error", "until", "test", "sent_today"}]`."""
    off = _off_entries(store, now)
    usage = {box["address"].casefold(): box["sent_today"] for box in sending.mailbox_usage(db, [b.user for b in boxes], now)}
    result = []
    for box in boxes:
        key = box.user.casefold()
        state = mb.mailbox_state(off.get(key), tests.get(key), usage.get(key, 0))
        result.append({"slot": box.slot, "address": box.user, **state})
    return result

