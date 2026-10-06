"""Reguły dotyczące wierszy tabeli maile: czysta logika bez bazy i SMTP.

Jedno miejsce dla zasad, które muszą być takie same w edycji pojedynczej, zmianie zbiorczej, wysyłce z panelu
i wysyłce z terminala. Funkcje nie rzucają wyjątków warstwy danych: zwracają powód odmowy (komunikat dla
użytkownika) albo wynik, a decyzję, jak go zgłosić, podejmuje wywołujący.
"""

from __future__ import annotations

from datetime import datetime
from typing import Iterable, Mapping, Optional

from perno.domain import company_rules
from perno.domain import statuses as st


def manual_status_problem(status: object) -> Optional[str]:
    """Dlaczego tego statusu nie wolno ustawić ręcznie (None = wolno). „Wysyłanie…” ustawia wyłącznie program."""
    if not isinstance(status, str) or status not in st.VALID_STATUSES:
        return f"Nieprawidłowy status: {status!r}."
    if status == st.STATUS_SENDING:
        return "Statusu „Wysyłanie…” nie można ustawić ręcznie."
    return None


def edit_blocked_by_sending(current_status: str, edited_fields: Iterable[str]) -> bool:
    """Wiersz, który właśnie jest wysyłany, przyjmuje tylko zmianę statusu — treść i dane zostawiamy w spokoju."""
    return current_status == st.STATUS_SENDING and any(name != "status" for name in edited_fields)


def resolve_edit(current: Mapping, values: Mapping, *, explicit_status: bool, now: datetime) -> dict:
    """Pola do zapisania po edycji wiersza: wartości z `values` plus `status` i `sent_at` wynikające z reguł.

    Bez jawnej zmiany statusu wiersz sam przechodzi między „Brak e-maila” a „Do wysłania” zależnie od tego,
    czy ma e-mail (i treść). Przy jawnej zmianie statusu data wysłania wynika z `st.resolve_sent_at`."""
    merged = {**current, **values}
    if not explicit_status:
        if merged["status"] == st.STATUS_SKIPPED_NO_EMAIL and merged["email"] and merged["body_html"]:
            merged["status"] = st.STATUS_PENDING
        elif merged["status"] == st.STATUS_PENDING and not merged["email"]:
            merged["status"] = st.STATUS_SKIPPED_NO_EMAIL
    else:
        merged["sent_at"] = st.resolve_sent_at(merged["status"], merged["sent_at"], now)
    return {name: merged[name] for name in [*values, "status", "sent_at"]}


def sendability_problem(row: Mapping) -> Optional[str]:
    """Dlaczego tego wiersza nie wolno wysłać z panelu (None = można próbować; ostateczną decyzję podejmuje zajęcie
    wiersza w bazie, które pilnuje statusu przy wyścigu dwóch kliknięć)."""
    if row["status"] == st.STATUS_SENT:
        return "Ten mail już został wysłany."
    if row["status"] in st.KEEPS_SENT_AT:
        return (f"Ten wiersz ma status „{st.STATUS_META[row['status']][0]}” — nie wysyłam. "
                "Jeśli to pomyłka, zmień status ręcznie.")
    if not row["email"] or not row["body_html"]:
        return "Brak e-maila lub treści — uzupełnij przed wysyłką."
    if row.get("hold"):  # status innego adresu tej firmy (`COMPANY_STOPS`), wyliczany przez bazę
        return company_rules.hold_message(row["hold"])
    return None


def daily_quota_left(sent_today: int, daily_cap: int) -> int:
    """Ile maili wolno jeszcze dziś wysłać przy dziennym limicie `daily_cap`."""
    return max(0, daily_cap - sent_today)
