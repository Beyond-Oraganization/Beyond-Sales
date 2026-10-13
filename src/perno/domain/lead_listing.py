"""Lista leadów w panelu: karty, filtry, wyszukiwanie i sortowanie w postaci, którą liczy baza (czysta logika).

Przeglądarka wysyła `{tab, q, segments, stages, sort, page, size}`; `parse_list_request` zamienia to na `LeadFilter`. Nieznana
karta albo sortowanie wraca do domyślnych, a wartość o złym typie to błąd żądania (jak w `reply_listing`).
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Mapping

from perno.domain import lead_rules as lr
from perno.domain import reply_listing as rl

TAB_ALL = "all"  # aktywne leady: firma ma pozytywny albo neutralny wątek
TAB_MISSING = "missing"  # aktywne, którym brakuje któregoś z pól wymaganych
TAB_AI = "ai"  # aktywne z polami wpisanymi przez AI i niezatwierdzonymi
TAB_WITHDRAWN = "withdrawn"  # były leadami, ale oceny wątków zmieniono (dane zostają)
TABS = (TAB_ALL, TAB_MISSING, TAB_AI, TAB_WITHDRAWN)
TAB_LABELS = {TAB_ALL: "Wszystkie", TAB_MISSING: "Do uzupełnienia", TAB_AI: "Do zatwierdzenia", TAB_WITHDRAWN: "Wycofane"}
DEFAULT_TAB = TAB_ALL

SORT_RECENT, SORT_NAME, SORT_RATING, SORT_COMPLETE = "recent", "name", "rating", "complete"
SORTS = (SORT_RECENT, SORT_NAME, SORT_RATING, SORT_COMPLETE)
SORT_LABELS = {SORT_RECENT: "Najnowsze", SORT_NAME: "Firma A-Z", SORT_RATING: "Ocena", SORT_COMPLETE: "Najmniej kompletne"}
DEFAULT_SORT = SORT_RECENT

DEFAULT_PAGE_SIZE = 50
MAX_PAGE_SIZE = 200
EXPORT_LIMIT = 5000  # tyle leadów najwyżej w jednym pliku


class LeadRequestError(rl.ReplyRequestError):
    """Żądanie listy leadów jest niepoprawne (komunikat po polsku); mapowane na HTTP 400 razem z błędami listy odpowiedzi."""


@dataclass(frozen=True)
class LeadFilter:
    """Co pokazać: karta plus zawężenia (puste = bez ograniczenia)."""

    tab: str = DEFAULT_TAB
    words: tuple = ()  # wszystkie słowa muszą pasować
    segments: tuple = ()
    stages: tuple = ()
    sort: str = DEFAULT_SORT


@dataclass(frozen=True)
class LeadListRequest:
    """Zwalidowane żądanie strony listy."""

    flt: LeadFilter
    page: int = 1
    size: int = DEFAULT_PAGE_SIZE


def _stages(value: object) -> tuple:
    chosen = rl.choices(value, "stages")
    unknown = [stage for stage in chosen if stage not in lr.STAGES]
    if unknown:
        raise LeadRequestError(f"Nieznany etap: {', '.join(unknown)}.")
    return chosen


def parse_filter(payload: Mapping) -> LeadFilter:
    """Filtr z `{tab, q, segments, stages, sort}`; brakujące pola mają wartości domyślne."""
    tab, sort = payload.get("tab"), payload.get("sort")
    return LeadFilter(tab=tab if tab in TABS else DEFAULT_TAB, words=rl.split_words(payload.get("q")),
                      segments=rl.choices(payload.get("segments"), "segments"), stages=_stages(payload.get("stages")),
                      sort=sort if sort in SORTS else DEFAULT_SORT)


def parse_list_request(payload: Mapping) -> LeadListRequest:
    """Waliduje `{tab, q, segments, stages, sort, page, size}`."""
    return LeadListRequest(parse_filter(payload), rl.integer(payload.get("page"), "page", 1, 1, 10**6),
                           rl.integer(payload.get("size"), "size", DEFAULT_PAGE_SIZE, 1, MAX_PAGE_SIZE))
