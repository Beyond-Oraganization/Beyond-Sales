"""Lista odpowiedzi w panelu: zakładki, filtry i wyszukiwanie w postaci, którą liczy baza (czysta logika).

Przeglądarka wysyła `{tab, q, mailboxes, segments, countries, from, to, page, size}`; `parse_list_request` zamienia to na
`ReplyFilter`. Nieznana zakładka wraca do domyślnej, a wartości o złym typie są błędem żądania (nie cichym „bez filtra”):
lista odpowiedzi to dane roboczo ważne, więc pomyłka ma być widoczna.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import date
from typing import Any, Mapping, Optional

TAB_TODO = "todo"  # czeka na naszą odpowiedź
TAB_ALL = "all"
TAB_POSITIVE = "positive"
TAB_NEUTRAL = "neutral"
TAB_NEGATIVE = "negative"
TAB_UNRATED = "unrated"
TAB_MACHINE = "machine"  # zwroty i autoodpowiedzi
TAB_UNASSIGNED = "unassigned"  # wiadomości, które nie wiadomo, czyje są
TABS = (TAB_TODO, TAB_ALL, TAB_POSITIVE, TAB_NEUTRAL, TAB_NEGATIVE, TAB_UNRATED, TAB_MACHINE, TAB_UNASSIGNED)
TAB_LABELS = {TAB_TODO: "Do odpowiedzi", TAB_ALL: "Wszystkie", TAB_POSITIVE: "Pozytywne", TAB_NEUTRAL: "Neutralne",
              TAB_NEGATIVE: "Negatywne", TAB_UNRATED: "Nieocenione", TAB_MACHINE: "Zwroty i automaty", TAB_UNASSIGNED: "Nieprzypisane"}
DEFAULT_TAB = TAB_TODO

DEFAULT_PAGE_SIZE = 50
MAX_PAGE_SIZE = 200
MAX_WORDS = 5
MAX_WORD = 40
MAX_CHOICES = 200  # skrzynek, segmentów, krajów w jednym filtrze
_DAY = re.compile(r"^\d{4}-\d{2}-\d{2}$")
_UNSAFE = re.compile("[\x00-\x1f\x7f-\x9f]")


class ReplyRequestError(ValueError):
    """Żądanie listy odpowiedzi jest niepoprawne (komunikat po polsku, dla użytkownika)."""


@dataclass(frozen=True)
class ReplyFilter:
    """Co pokazać: zakładka plus zawężenia (puste = bez ograniczenia)."""

    tab: str = DEFAULT_TAB
    words: tuple = ()  # wszystkie słowa muszą pasować
    mailboxes: tuple = ()
    segments: tuple = ()
    countries: tuple = ()
    since: Optional[date] = None  # dzień ostatniej aktywności, włącznie
    until: Optional[date] = None


@dataclass(frozen=True)
class ReplyListRequest:
    """Zwalidowane żądanie strony listy."""

    flt: ReplyFilter
    page: int = 1
    size: int = DEFAULT_PAGE_SIZE


def _choices(value: Any, name: str, *, lower: bool = False) -> tuple:
    if value is None:
        return ()
    if not isinstance(value, list) or len(value) > MAX_CHOICES or not all(isinstance(item, str) and item for item in value):
        raise ReplyRequestError(f"Pole {name} musi być listą nazw (najwyżej {MAX_CHOICES}).")
    return tuple(dict.fromkeys(item.strip().lower() if lower else item.strip() for item in value))


def _day(value: Any, name: str) -> Optional[date]:
    if value in (None, ""):
        return None
    if not isinstance(value, str) or not _DAY.match(value):
        raise ReplyRequestError(f"Pole {name} musi być datą RRRR-MM-DD.")
    try:
        return date.fromisoformat(value)
    except ValueError:
        raise ReplyRequestError(f"Pole {name} nie jest poprawną datą.") from None


def _integer(value: Any, name: str, default: int, low: int, high: int) -> int:
    if value is None:
        return default
    if isinstance(value, bool) or not isinstance(value, int) or not low <= value <= high:
        raise ReplyRequestError(f"Pole {name} musi być liczbą całkowitą od {low} do {high}.")
    return value


def split_words(query: Any) -> tuple:
    """Słowa do wyszukiwania: małymi literami, bez znaków sterujących, najwyżej `MAX_WORDS` po `MAX_WORD` znaków."""
    if query is None:
        return ()
    if not isinstance(query, str):
        raise ReplyRequestError("Pole q musi być tekstem.")
    words = [word[:MAX_WORD] for word in _UNSAFE.sub(" ", query).lower().split()]
    return tuple(dict.fromkeys(words))[:MAX_WORDS]


def parse_list_request(payload: Mapping) -> ReplyListRequest:
    """Waliduje `{tab, q, mailboxes, segments, countries, from, to, page, size}`; brakujące pola mają wartości domyślne."""
    tab = payload.get("tab")
    since, until = _day(payload.get("from"), "from"), _day(payload.get("to"), "to")
    if since and until and since > until:
        raise ReplyRequestError("Data początkowa jest późniejsza niż końcowa.")
    flt = ReplyFilter(
        tab=tab if tab in TABS else DEFAULT_TAB, words=split_words(payload.get("q")),
        mailboxes=_choices(payload.get("mailboxes"), "mailboxes", lower=True), segments=_choices(payload.get("segments"), "segments"),
        countries=_choices(payload.get("countries"), "countries"), since=since, until=until)
    return ReplyListRequest(flt, _integer(payload.get("page"), "page", 1, 1, 10**6),
                            _integer(payload.get("size"), "size", DEFAULT_PAGE_SIZE, 1, MAX_PAGE_SIZE))


@dataclass(frozen=True)
class ReportFilter:
    """Co obejmuje raport: maile kampanii wysłane w danym okresie (dzień wysyłki, włącznie), z zawężeniami (puste = wszystkie)."""

    since: Optional[date] = None
    until: Optional[date] = None
    segments: tuple = ()
    countries: tuple = ()
    mailboxes: tuple = ()


def parse_report_request(payload: Mapping) -> ReportFilter:
    """Waliduje `{from, to, segments, countries, mailboxes}`; zły typ albo odwrócony okres to błąd żądania."""
    since, until = _day(payload.get("from"), "from"), _day(payload.get("to"), "to")
    if since and until and since > until:
        raise ReplyRequestError("Data początkowa jest późniejsza niż końcowa.")
    return ReportFilter(since, until, _choices(payload.get("segments"), "segments"), _choices(payload.get("countries"), "countries"),
                        _choices(payload.get("mailboxes"), "mailboxes", lower=True))
