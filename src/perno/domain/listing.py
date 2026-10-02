"""Stan listy firm w panelu (filtry, wyszukiwarka, sortowanie) w postaci, którą liczy serwer.

Przeglądarka trzyma stan jako obiekt `{q, qFields, statuses, segments, countries, email, body, city, address, sent, origin,
sentFrom, sentTo, generatedFrom, generatedTo, sort}` (to samo trafia do adresu URL i zapisanych widoków); `parse_state`
zamienia go na `RowFilter` i klucz sortowania. Nieznane pola i wartości o złym typie wracają do domyślnych, tak jak w
przeglądarce: stan z adresu URL bywa stary albo ręcznie zmieniony.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, replace
from datetime import date
from typing import Any, Mapping, Optional

from perno.domain import statuses as st
from perno.domain.rowfilter import ALL_ROWS, ORIGINS, RowFilter
from perno.domain.search import SEARCH_FIELDS, parse_query

SORT_KEYS = ("company", "city", "email", "segment", "status", "country", "generated_at", "sent_at")
DEFAULT_SORT = "default"
MAX_QUERY_LENGTH = 200
MAX_LIST_ITEMS = 100
MAX_ITEM_LENGTH = 64
_TRI = {"": None, "yes": True, "no": False}
_DAY = re.compile(r"^\d{4}-\d{2}-\d{2}$")
_SORT = re.compile(r"^-?[a-z_]+$")
LIST_FACETS = ("statuses", "segments", "countries")  # pola, dla których lista pokazuje liczniki


@dataclass(frozen=True)
class ListQuery:
    """Co pokazać na liście: filtr wierszy i kolejność (`klucz`, `-klucz` albo „default”)."""
    flt: RowFilter = ALL_ROWS
    sort: str = DEFAULT_SORT


def ready_filter() -> RowFilter:
    """Firmy gotowe do wysyłki: status „Do wysłania” lub „Błąd”, jest e-mail i jest treść maila."""
    return RowFilter(statuses=st.SENDABLE_STATUSES, has_email=True, has_body=True)


def _string_list(value: Any) -> tuple:
    if not isinstance(value, list):
        return ()
    out: list = []
    for item in value:
        if isinstance(item, str) and item and len(item) <= MAX_ITEM_LENGTH and item not in out and len(out) < MAX_LIST_ITEMS:
            out.append(item)
    return tuple(out)


def _tri(value: Any) -> Optional[bool]:
    return _TRI.get(value) if isinstance(value, str) else None


def _day(value: Any) -> Optional[date]:
    if not isinstance(value, str) or not _DAY.match(value):
        return None
    try:
        return date.fromisoformat(value)
    except ValueError:  # 2026-13-45 ma kształt daty, ale nią nie jest
        return None


def parse_state(payload: object) -> ListQuery:
    """Stan z przeglądarki → `ListQuery`. Wszystko, czego nie rozumiemy, znaczy „bez ograniczenia”."""
    data: Mapping = payload if isinstance(payload, dict) else {}
    query = data.get("q")
    field = data.get("qFields")
    terms = parse_query(query[:MAX_QUERY_LENGTH] if isinstance(query, str) else "",
                        field if field in SEARCH_FIELDS else "all")
    origin = data.get("origin")
    raw_sort = data.get("sort")
    sort = raw_sort if isinstance(raw_sort, str) and _SORT.match(raw_sort) else ""
    flt = RowFilter(
        segments=_string_list(data.get("segments")), statuses=_string_list(data.get("statuses")),
        countries=_string_list(data.get("countries")),
        has_email=_tri(data.get("email")), has_body=_tri(data.get("body")),
        has_city=_tri(data.get("city")), has_address=_tri(data.get("address")), has_sent=_tri(data.get("sent")),
        origin=origin if origin in ORIGINS else "",
        sent_from=_day(data.get("sentFrom")), sent_to=_day(data.get("sentTo")),
        generated_from=_day(data.get("generatedFrom")), generated_to=_day(data.get("generatedTo")),
        search=terms if terms else None,
    )
    return ListQuery(flt, sort if sort.lstrip("-") in SORT_KEYS else DEFAULT_SORT)


def without_facet(flt: RowFilter, facet: str) -> RowFilter:
    """Filtr bez ograniczenia po polu, które liczy licznik (`LIST_FACETS`)."""
    if facet == "statuses":
        return replace(flt, statuses=())
    if facet == "segments":
        return replace(flt, segments=())
    if facet == "countries":
        return replace(flt, countries=())
    raise ValueError(f"nieznane pole liczników: {facet}")
