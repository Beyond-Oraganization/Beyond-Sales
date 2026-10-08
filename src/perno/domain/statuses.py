"""Statusy maili — jedno źródło prawdy dla bazy, panelu (JS) i dashboardu."""

from __future__ import annotations

from datetime import datetime
from typing import Optional

STATUS_PENDING = "do_wyslania"
STATUS_SENDING = "wysylanie"
STATUS_SENT = "wyslano"
STATUS_REPLIED = "odpowiedzial"
STATUS_INTERESTED = "zainteresowany"
STATUS_ERROR = "blad"
STATUS_BOUNCED = "zwrot"  # twardy zwrot poczty (adres nie istnieje): ten adres odpada, inne adresy firmy bez zmian
STATUS_SKIPPED_NO_EMAIL = "pominieto_brak_email"
STATUS_SKIPPED_CUSTOMER = "pominieto_klient"
STATUS_AWAITING_COPY = "oczekuje_na_tresc"
STATUS_DO_NOT_CONTACT = "nie_kontaktowac"
STATUS_SKIPPED_FIT = "pominieto_dopasowanie"  # odrzucona przy analizie: oferta nie pasuje, nie wraca do kolejki treści

# kod -> (etykieta, kolor); kolejność = kolejność na liście rozwijanej
STATUS_META = {
    STATUS_PENDING: ("Do wysłania", "#2f6feb"),
    STATUS_SENDING: ("Wysyłanie…", "#8e44ad"),
    STATUS_SENT: ("Wysłano", "#1a7f4b"),
    STATUS_REPLIED: ("Odpowiedział", "#0e7490"),
    STATUS_INTERESTED: ("Zainteresowany", "#be185d"),
    STATUS_ERROR: ("Błąd", "#c0392b"),
    STATUS_BOUNCED: ("Zwrot", "#9a3412"),
    STATUS_SKIPPED_NO_EMAIL: ("Brak e-maila", "#6b7280"),
    STATUS_SKIPPED_CUSTOMER: ("Już klient", "#8a5a00"),
    STATUS_AWAITING_COPY: ("Czeka na treść", "#b8860b"),
    STATUS_DO_NOT_CONTACT: ("Nie kontaktować", "#7f1d1d"),
    STATUS_SKIPPED_FIT: ("Nie pasuje", "#475569"),
}

VALID_STATUSES = frozenset(STATUS_META)

# "Wysyłanie…" ustawia wyłącznie program (zajęcie wiersza przed wysyłką) —
# nie można go wybrać ręcznie.
MANUAL_STATUSES = tuple(s for s in STATUS_META if s != STATUS_SENDING)

# Statusy, z których panel pozwala wysłać maila (zaznaczenie checkboxem).
SENDABLE_STATUSES = (STATUS_PENDING, STATUS_ERROR)

# Statusy firm, do których mail już poszedł (a potem ktoś odpisał, poczta wróciła albo zdecydowano, że dalej nie piszemy):
# przejście między nimi nie kasuje daty wysłania, więc historia „kiedy wysłano" przeżywa oznaczenie „Odpowiedział".
# Każdy inny status ją czyści — oznacza, że mail dopiero ma pójść (albo pójść ponownie).
KEEPS_SENT_AT = (STATUS_SENT, STATUS_REPLIED, STATUS_INTERESTED, STATUS_DO_NOT_CONTACT, STATUS_BOUNCED)

# Tych statusów nie nadpisuje ponowne generowanie treści z dossier: przeliczenie zniszczyłoby wynik wysyłki
# albo decyzję człowieka (np. firma, której nie wolno pisać, wróciłaby na „Do wysłania").
PROTECTED_FROM_REGENERATION = (STATUS_SENT, STATUS_SENDING, STATUS_REPLIED, STATUS_INTERESTED, STATUS_DO_NOT_CONTACT,
                               STATUS_BOUNCED)

# Statusy, które dotyczą całej firmy, nie jednego adresu: firma odpowiedziała, jest zainteresowana, prosi o spokój albo
# już jest klientem. Pozostałe adresy tej firmy są wtedy wstrzymane (`company_rules.hold_reason`).
COMPANY_STOPS = (STATUS_REPLIED, STATUS_INTERESTED, STATUS_DO_NOT_CONTACT, STATUS_SKIPPED_CUSTOMER)

SEGMENT_ORDER = ("eventy", "driverzy", "spoldzielnie")


def resolve_sent_at(new_status: str, current_sent_at: Optional[datetime], now: datetime) -> Optional[datetime]:
    """Data wysłania po ręcznej zmianie statusu — jedna reguła dla edycji pojedynczej i zbiorczej.

    „Wysłano" zachowuje istniejącą datę, a gdy jej brak, ustawia `now`. Pozostałe statusy z `KEEPS_SENT_AT`
    zostawiają datę taką, jaka jest (nie zmyślamy daty, której nie znamy). Wszystkie inne ją czyszczą."""
    if new_status == STATUS_SENT:
        return current_sent_at or now
    if new_status in KEEPS_SENT_AT:
        return current_sent_at
    return None
