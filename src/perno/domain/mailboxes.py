"""Rotacja skrzynek nadawczych: która skrzynka wysyła następny mail (czysta logika, bez bazy i SMTP).

Skrzynki idą po kolei: wybieramy tę, która dziś wysłała najmniej, a przy remisie tę, która wysyłała najdawniej (potem
kolejność z konfiguracji). Dzięki temu rotacja wynika ze stanu w bazie, więc przeżywa restart demona i działa tak samo
dla wysyłki ręcznej z panelu i dla automatu. Dzienny limit obowiązuje każdą skrzynkę osobno.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Collection, Mapping, Optional, Sequence

MAX_MAILBOXES = 10  # skrzynka 1 to SMTP_*, kolejne SMTP2_* … SMTP10_*


@dataclass(frozen=True)
class DayUse:
    """Ile maili skrzynka wysłała dziś i kiedy wysłała ostatni."""
    sent: int = 0
    last_sent: Optional[datetime] = None


def smtp_prefix(slot: int) -> str:
    """Przedrostek kluczy config.env skrzynki: `SMTP` dla pierwszej, `SMTP2`, `SMTP3`… dla kolejnych."""
    return "SMTP" if slot == 1 else f"SMTP{slot}"


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
