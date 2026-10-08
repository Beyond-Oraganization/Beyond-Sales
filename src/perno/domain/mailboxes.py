"""Rotacja skrzynek nadawczych: która skrzynka wysyła następny mail (czysta logika, bez bazy i SMTP).

Skrzynki idą po kolei: wybieramy tę, która dziś wysłała najmniej, a przy remisie tę, która wysyłała najdawniej (potem
kolejność z konfiguracji). Dzięki temu rotacja wynika ze stanu w bazie, więc przeżywa restart demona i działa tak samo
dla wysyłki ręcznej z panelu i dla automatu. Dzienny limit obowiązuje każdą skrzynkę osobno.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import datetime
from typing import Collection, Iterable, Mapping, Optional, Sequence

# Skrzynek może być dowolnie wiele: skrzynka 1 to SMTP_* / IMAP_*, kolejne SMTP2_* / IMAP2_*, SMTP3_* … Numery nie muszą być
# ciągłe. Sufit dotyczy tylko zapisu numeru w kluczu (cztery cyfry), nie liczby skrzynek w ruchu.
MAX_SLOT = 9999

_SLOT_KEY = re.compile(r"(SMTP|IMAP)(\d*)_[A-Z0-9_]+")


@dataclass(frozen=True)
class DayUse:
    """Ile maili skrzynka wysłała dziś i kiedy wysłała ostatni."""
    sent: int = 0
    last_sent: Optional[datetime] = None


def smtp_prefix(slot: int) -> str:
    """Przedrostek kluczy config.env skrzynki: `SMTP` dla pierwszej, `SMTP2`, `SMTP3`… dla kolejnych."""
    return "SMTP" if slot == 1 else f"SMTP{slot}"


def imap_prefix(slot: int) -> str:
    """Przedrostek kluczy IMAP skrzynki: `IMAP` dla pierwszej, `IMAP2`, `IMAP3`… dla kolejnych."""
    return "IMAP" if slot == 1 else f"IMAP{slot}"


def key_slot(key: str) -> Optional[int]:
    """Numer skrzynki, do której należy klucz `SMTPn_*` / `IMAPn_*` (1 dla `SMTP_*` / `IMAP_*`); None dla innych kluczy
    oraz dla numerów w złym zapisie (`SMTP1_`, `SMTP02_`, powyżej `MAX_SLOT`)."""
    match = _SLOT_KEY.fullmatch(key)
    if match is None:
        return None
    digits = match.group(2)
    if not digits:
        return 1
    number = int(digits)
    return number if str(number) == digits and 2 <= number <= MAX_SLOT else None


def slots_in_keys(keys: Iterable[str]) -> list:
    """Numery dodatkowych skrzynek (2 i dalej) występujące w kluczach SMTP / IMAP, rosnąco."""
    return sorted({slot for slot in map(key_slot, keys) if slot is not None and slot > 1})


def choose_mailbox(addresses: Sequence[str], use: Mapping[str, DayUse], daily_cap: Optional[int],
                   unavailable: Collection[str] = ()) -> Optional[str]:
    """Adres skrzynki, z której pójdzie następny mail; None, gdy żadna nie może (limit albo wyłączona).
    `use` ma klucze małymi literami; `daily_cap` None = bez limitu; `unavailable` to adresy wyłączone na dziś."""
    blocked = {address.casefold() for address in unavailable}
    candidates = []
    for position, address in enumerate(addresses):
        key = address.casefold()
        today = use.get(key, DayUse())
        if key in blocked or (daily_cap is not None and today.sent >= daily_cap):
            continue
        candidates.append(((today.sent, today.last_sent or datetime.min, position), address))
    return min(candidates, key=lambda item: item[0])[1] if candidates else None


def attribute_legacy(use: Mapping[str, DayUse], primary: str) -> dict:
    """Wysyłki bez zapisanej skrzynki (klucz "") pochodzą sprzed wielu skrzynek, kiedy była tylko główna: doliczamy je jej."""
    merged = dict(use)
    legacy = merged.pop("", None)
    if legacy is None:
        return merged
    key = primary.casefold()
    own = merged.get(key, DayUse())
    last = max((moment for moment in (own.last_sent, legacy.last_sent) if moment), default=None)
    merged[key] = DayUse(own.sent + legacy.sent, last)
    return merged


STATE_OFF = "off"  # automat wyłączył skrzynkę po błędach
STATE_ERROR = "error"  # ostatni test połączenia się nie udał
STATE_OK = "ok"  # test się udał albo skrzynka wysłała dziś maile
STATE_UNKNOWN = "unknown"  # jeszcze niczego o niej nie wiemy


def mailbox_state(off: Optional[Mapping], test: Optional[Mapping], sent_today: int) -> dict:
    """Stan skrzynki do pokazania w Konfiguracji. `off` to wpis demona o wyłączeniu (`{"blad", "do"}`), `test` ostatni test
    połączenia z panelu (`{"ok", "message", "at"}`). Wyłączenie przez automat jest ważniejsze od testu, bo to ono
    decyduje, czy skrzynka dziś wysyła; udany test nie zdejmuje go (robi to sam demon po upływie przerwy)."""
    state = {"state": STATE_UNKNOWN, "error": None, "until": None, "test": dict(test) if test else None,
             "sent_today": sent_today}
    if off:
        state.update(state=STATE_OFF, error=off.get("blad") or None, until=off.get("do"))
    elif test and not test.get("ok"):
        state.update(state=STATE_ERROR, error=test.get("message"))
    elif test or sent_today:
        state["state"] = STATE_OK
    return state
