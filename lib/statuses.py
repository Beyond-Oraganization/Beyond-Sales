"""Statusy maili — jedno źródło prawdy dla bazy, panelu (JS) i dashboardu."""

from __future__ import annotations

STATUS_PENDING = "do_wyslania"
STATUS_SENDING = "wysylanie"
STATUS_SENT = "wyslano"
STATUS_REPLIED = "odpowiedzial"
STATUS_INTERESTED = "zainteresowany"
STATUS_ERROR = "blad"
STATUS_SKIPPED_NO_EMAIL = "pominieto_brak_email"
STATUS_SKIPPED_CUSTOMER = "pominieto_klient"
STATUS_AWAITING_COPY = "oczekuje_na_tresc"
STATUS_DO_NOT_CONTACT = "nie_kontaktowac"

# kod -> (etykieta, kolor); kolejność = kolejność na liście rozwijanej
STATUS_META = {
    STATUS_PENDING: ("Do wysłania", "#2f6feb"),
    STATUS_SENDING: ("Wysyłanie…", "#8e44ad"),
    STATUS_SENT: ("Wysłano", "#1a7f4b"),
    STATUS_REPLIED: ("Odpowiedział", "#0e7490"),
    STATUS_INTERESTED: ("Zainteresowany", "#be185d"),
    STATUS_ERROR: ("Błąd", "#c0392b"),
    STATUS_SKIPPED_NO_EMAIL: ("Brak e-maila", "#6b7280"),
    STATUS_SKIPPED_CUSTOMER: ("Już klient", "#8a5a00"),
    STATUS_AWAITING_COPY: ("Czeka na treść", "#b8860b"),
    STATUS_DO_NOT_CONTACT: ("Nie kontaktować", "#7f1d1d"),
}

VALID_STATUSES = frozenset(STATUS_META)

# "Wysyłanie…" ustawia wyłącznie program (zajęcie wiersza przed wysyłką) —
# nie można go wybrać ręcznie.
MANUAL_STATUSES = tuple(s for s in STATUS_META if s != STATUS_SENDING)

# Statusy, z których panel pozwala wysłać maila (zaznaczenie checkboxem).
SENDABLE_STATUSES = (STATUS_PENDING, STATUS_ERROR)

# Statusy firm, do których mail już poszedł (a potem ktoś odpisał albo zdecydował, że dalej nie piszemy):
# przejście między nimi nie kasuje daty wysłania, więc historia „kiedy wysłano" przeżywa oznaczenie „Odpowiedział".
# Każdy inny status ją czyści — oznacza, że mail dopiero ma pójść (albo pójść ponownie).
KEEPS_SENT_AT = (STATUS_SENT, STATUS_REPLIED, STATUS_INTERESTED, STATUS_DO_NOT_CONTACT)

# Tych statusów nie nadpisuje ponowne generowanie treści z dossier: przeliczenie zniszczyłoby wynik wysyłki
# albo decyzję człowieka (np. firma, której nie wolno pisać, wróciłaby na „Do wysłania").
PROTECTED_FROM_REGENERATION = (STATUS_SENT, STATUS_SENDING, STATUS_REPLIED, STATUS_INTERESTED, STATUS_DO_NOT_CONTACT)

SEGMENT_ORDER = ("eventy", "driverzy")


def resolve_sent_at(new_status, current_sent_at, now):
    """Data wysłania po ręcznej zmianie statusu — jedna reguła dla edycji pojedynczej i zbiorczej.

    „Wysłano" zachowuje istniejącą datę, a gdy jej brak, ustawia `now`. Pozostałe statusy z `KEEPS_SENT_AT`
    zostawiają datę taką, jaka jest (nie zmyślamy daty, której nie znamy). Wszystkie inne ją czyszczą."""
    if new_status == STATUS_SENT:
        return current_sent_at or now
    if new_status in KEEPS_SENT_AT:
        return current_sent_at
    return None
