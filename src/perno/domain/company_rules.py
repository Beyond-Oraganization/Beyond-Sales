"""Reguły firm z wieloma adresami: blokada pozostałych adresów, odstęp między mailami do jednej firmy, id dodatkowych
maili i warunki usunięcia adresu. Czysta logika bez bazy i zegara (dzień przychodzi jako argument)."""

from __future__ import annotations

import re
from datetime import date, datetime, time, timedelta
from typing import Iterable, Mapping, Optional

from perno.domain import statuses as st

MAIL_ID_SEPARATOR = "~"  # id dodatkowego maila: `<id firmy>~<numer>`; adres główny ma id firmy
MAX_MAIL_ID_LENGTH = 32  # długość kolumny maile.id
MAX_EMAIL_LENGTH = 320  # długość kolumny maile.email
_EMAIL_SHAPE = re.compile(r"[^@\s]+@[^@\s]+\.[^@\s]+")


def email_key(email: Optional[str]) -> str:
    """Adres w postaci porównywanej: bez spacji i wielkości liter (MySQL porównuje bez wielkości liter, sqlite z nią)."""
    return (email or "").strip().lower()


def email_problem(address: str) -> Optional[str]:
    """Dlaczego tekst nie może być nowym adresem firmy (None = może). Sprawdzamy tylko kształt `a@b.c`: czy adres istnieje,
    wie dopiero serwer pocztowy."""
    if not address:
        return "Podaj adres e-mail."
    if len(address) > MAX_EMAIL_LENGTH:
        return f"Adres e-mail jest za długi (maks. {MAX_EMAIL_LENGTH} znaków)."
    if not _EMAIL_SHAPE.fullmatch(address):
        return f"„{address[:60]}” nie wygląda na adres e-mail (oczekiwano postaci nazwa@domena.pl)."
    return None


def hold_reason(sibling_statuses: Iterable[str]) -> Optional[str]:
    """Status z `COMPANY_STOPS` wśród statusów INNYCH adresów firmy (pierwszy według kolejności statusów) albo None.
    Własnego statusu wiersza nie liczymy: ten widać na samym wierszu."""
    present = set(sibling_statuses)
    return next((status for status in st.COMPANY_STOPS if status in present), None)


def hold_message(stop_status: str) -> str:
    """Komunikat dla użytkownika, gdy adres jest wstrzymany przez status innego adresu firmy."""
    label = st.STATUS_META[stop_status][0]
    return (f"Inny adres tej firmy ma status „{label}” — nie wysyłam. "
            "Jeśli to pomyłka, zmień tamten status ręcznie.")


def next_mail_id(company_id: str, existing_ids: Iterable[str]) -> str:
    """Id kolejnego dodatkowego maila firmy: najwyższy dotychczasowy numer + 1 (pierwszy dodatkowy ma numer 2).
    Rzuca `ValueError`, gdy wynik nie zmieści się w kolumnie id."""
    prefix = company_id + MAIL_ID_SEPARATOR
    numbers = [1]
    for mail_id in existing_ids:
        suffix = mail_id[len(prefix):] if mail_id.startswith(prefix) else ""
        if suffix.isdigit():
            numbers.append(int(suffix))
    new_id = f"{prefix}{max(numbers) + 1}"
    if len(new_id) > MAX_MAIL_ID_LENGTH:
        raise ValueError(f"Id firmy {company_id!r} jest za długie, żeby dopisać do niego kolejny adres.")
    return new_id


def gap_cutoff(today: date, gap_days: int) -> Optional[datetime]:
    """Od kiedy wysłany mail wstrzymuje kolejne maile do tej samej firmy: początek dnia minus (odstęp − 1) dni, czyli
    odstęp 1 = jeden mail na firmę na dzień kalendarzowy, 3 = kolejny najwcześniej trzeciego dnia. None = bez odstępu."""
    if gap_days <= 0:
        return None
    return datetime.combine(today, time.min) - timedelta(days=gap_days - 1)


def deletable_problem(row: Mapping, *, anchor: bool) -> Optional[str]:
    """Dlaczego adresu nie wolno usunąć (None = wolno). Adres główny jest kotwicą firmy, a adres, do którego już
    wysłano, zostaje w historii kampanii."""
    if anchor:
        return "To adres główny firmy — nie można go usunąć (możesz go zmienić albo ustawić status „Nie kontaktować”)."
    if row["status"] == st.STATUS_SENDING:
        return "Ten mail jest właśnie wysyłany — poczekaj na wynik."
    if row["status"] in st.KEEPS_SENT_AT or row.get("sent_at"):
        return "Do tego adresu już wysłano maila — zostaje w historii kampanii."
    return None
