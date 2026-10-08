"""Reguły ocen odpowiedzi i ich skutków (czysta logika)."""

from __future__ import annotations

from typing import Optional

from perno.domain import replies as rp
from perno.domain import statuses as st

# Statusy maila, które z zakładki Odpowiedzi ustawia człowiek jednym kliknięciem (inne zmienia się w Wysyłce).
SETTABLE_STATUSES = (st.STATUS_REPLIED, st.STATUS_INTERESTED, st.STATUS_DO_NOT_CONTACT)
MAX_NOTE = 200


class RuleError(ValueError):
    """Operacja niezgodna z regułami (komunikat po polsku, dla użytkownika)."""


def check_rating(value: object) -> Optional[str]:
    """Ocena do zapisu: jedna z `RATINGS` albo None (wyczyszczenie). Cokolwiek innego to błąd."""
    if value is None or value == "":
        return None
    if value not in rp.RATINGS:
        raise RuleError(f"Ocena musi być jedną z: {', '.join(rp.RATINGS)} (albo pusta).")
    return str(value)


def check_note(value: object) -> str:
    """Notatka do oceny: tekst do `MAX_NOTE` znaków, bez znaków sterujących."""
    if value is None:
        return ""
    if not isinstance(value, str):
        raise RuleError("Notatka musi być tekstem.")
    return "".join(ch for ch in value if ch >= " " and ch != "\x7f").strip()[:MAX_NOTE]


def ai_may_rate(current_by: Optional[str]) -> bool:
    """Ocena AI nie nadpisuje oceny człowieka (może nadpisać swoją poprzednią albo uzupełnić brak)."""
    return current_by != rp.BY_HUMAN


def status_suggestion(rating: Optional[str], optout: bool, current_status: str) -> Optional[str]:
    """Jaki status maila podpowiedzieć po ocenie (człowiek zatwierdza jednym kliknięciem; AI nigdy go nie ustawia):
    prośba o brak kontaktu → „Nie kontaktować”, ocena pozytywna → „Zainteresowany” (z „wysłano” albo „odpowiedział”)."""
    if optout and current_status != st.STATUS_DO_NOT_CONTACT:
        return st.STATUS_DO_NOT_CONTACT
    if rating == rp.RATING_POSITIVE and current_status in (st.STATUS_SENT, st.STATUS_REPLIED):
        return st.STATUS_INTERESTED
    return None


def check_settable_status(value: object) -> str:
    """Status maila możliwy do ustawienia z zakładki Odpowiedzi (`SETTABLE_STATUSES`); inny to błąd reguły."""
    if value not in SETTABLE_STATUSES:
        raise RuleError("Z tej zakładki można ustawić status: Odpowiedział, Zainteresowany albo Nie kontaktować.")
    return str(value)
