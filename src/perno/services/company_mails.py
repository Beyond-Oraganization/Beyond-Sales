"""Adresy firmy w panelu: dodawanie i usuwanie z kontrolą, że Automat zna reguły wielu adresów.

Dodatkowy adres jest zwykłym wierszem „Do wysłania”, więc demon Automatu wyśle go jak każdy inny. Demon w starej wersji
nie zna blokady firmy ani odstępu między mailami do jednej firmy, dlatego dodawanie adresów jest odmawiane, dopóki taki
demon działa (nowy demon zgłasza w znaku życia wersję `DAEMON_VERSION`).
"""

from __future__ import annotations

from typing import Any, Mapping

from perno.db import Database, InvalidValue
from perno.db.automat_store import AutomatStore
from perno.domain.automat_rules import DAEMON_VERSION


class DaemonOutdated(Exception):
    """Działa demon Automatu w starej wersji; komunikat jest dla użytkownika."""


def require_current_daemon(store: AutomatStore) -> None:
    """Odmawia, gdy działa demon bez wiedzy o firmach z wieloma adresami (brak demona jest w porządku)."""
    run = store.active_run()
    if run and run["statystyki"].get("v") != DAEMON_VERSION:
        raise DaemonOutdated(
            "Działa demon Automatu w starej wersji, który nie zna blokady firmy ani odstępu między mailami do jednej "
            "firmy. Zrestartuj go (automat.py install) i spróbuj ponownie.")


def _text(payload: Mapping, name: str) -> str:
    value = payload.get(name)
    if not isinstance(value, str) or not value.strip():
        raise InvalidValue(f"Brak pola {name}.")
    return value.strip()


def add_address(db: Database, store: AutomatStore, payload: Mapping) -> dict:
    """`{company_id, email, copy_text?}` → `{row, mails, warnings}`. Ostrzeżenie, gdy adres ma już inna firma."""
    company_id, email = _text(payload, "company_id"), _text(payload, "email")
    copy_text: Any = payload.get("copy_text", True)
    if not isinstance(copy_text, bool):
        raise InvalidValue("Pole copy_text musi być wartością logiczną.")
    require_current_daemon(store)
    row = db.add_mail(company_id, email, copy_text=copy_text)
    others = db.companies_with_address(email, company_id)
    warnings = [f"Ten adres ma też firma: {', '.join(others)}."] if others else []
    return {"row": row, "mails": db.company_mails(company_id), "warnings": warnings}


def delete_address(db: Database, payload: Mapping) -> dict:
    """`{id}` → `{deleted, company_id, mails}`."""
    return db.delete_mail(_text(payload, "id"))
