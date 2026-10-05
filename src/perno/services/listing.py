"""Lista firm w panelu: strona wyników z licznikami, id widoku i podsumowanie zaznaczenia.

Wszystko liczy baza (przy 50 tys. firm lista w przeglądarce miałaby 27 MB i otwierała się kilka sekund). Przeglądarka
wysyła stan filtrów w tej samej postaci, w jakiej trzyma go w adresie URL i w zapisanych widokach (`domain.listing`),
a dostaje jedną stronę wierszy i liczniki. Zaznaczenie to lista id; widok „tylko zaznaczone” zawęża wyniki do niej.
"""

from __future__ import annotations

from dataclasses import dataclass, replace
from math import ceil
from typing import Any, Mapping, Optional

from perno.db import Database, InvalidValue
from perno.domain.listing import ListQuery, parse_state
from perno.domain.rowfilter import ALL_ROWS, FOREIGN_COUNTRY, HOME_COUNTRY, NO_COUNTRY, RowFilter

DEFAULT_PAGE_SIZE = 100
MAX_PAGE_SIZE = 1000
MAX_LIST_IDS = 50_000  # ≈0,8 MB JSON: mieści się w limicie żądania (2 MB)
MAX_PRESETS = 30


@dataclass(frozen=True)
class ListRequest:
    """Zwalidowane żądanie strony listy."""
    query: ListQuery
    page: int
    size: int
    ids: Optional[tuple]  # widok „tylko zaznaczone”; None = cała lista
    counts: bool  # suma i liczniki z jednego przejścia (zmiana filtrów); inaczej tylko suma
    totals: bool  # liczniki bez żadnych filtrów (jakie wartości w ogóle istnieją)
    presets: tuple  # filtry szybkich widoków do policzenia


def _int(value: Any, name: str, default: int, low: int, high: int) -> int:
    if value is None:
        return default
    if isinstance(value, bool) or not isinstance(value, int) or not low <= value <= high:
        raise InvalidValue(f"Pole {name} musi być liczbą całkowitą od {low} do {high}.")
    return value


def parse_ids(value: Any, *, name: str = "ids") -> Optional[tuple]:
    """Lista id (bez powtórzeń, w podanej kolejności) albo `None`, gdy pola nie podano."""
    if value is None:
        return None
    if not isinstance(value, list) or not all(isinstance(item, str) and item for item in value):
        raise InvalidValue(f"Pole {name} musi być listą identyfikatorów firm.")
    unique = tuple(dict.fromkeys(value))
    if len(unique) > MAX_LIST_IDS:
        raise InvalidValue(f"Za dużo wierszy naraz (maks. {MAX_LIST_IDS}) — zawęź zaznaczenie.")
    return unique


def parse_strings(value: Any, name: str, limit: int = 20) -> tuple:
    """Krótka lista niepustych tekstów (np. statusów); brak pola to pusta lista."""
    if value is None:
        return ()
    if not isinstance(value, list) or len(value) > limit or not all(isinstance(item, str) and item for item in value):
        raise InvalidValue(f"Pole {name} musi być listą nazw (maks. {limit}).")
    return tuple(dict.fromkeys(value))


def order_ids(db: Database, ids: tuple, sort: Any) -> tuple:
    """Wskazane id w kolejności widoku (klucz sortowania z panelu; nieznany daje kolejność domyślną). Id, których nie
    ma w bazie, znikają — plik z zaznaczonymi wierszami ma wyglądać tak, jak lista na ekranie."""
    return tuple(db.list_ids(RowFilter(ids=ids), parse_state({"sort": sort}).sort))


def parse_list_request(payload: Mapping) -> ListRequest:
    """Waliduje żądanie `{state, page, size, ids, counts, totals, presets}`; brakujące pola mają wartości domyślne."""
    presets = payload.get("presets") or []
    if not isinstance(presets, list) or len(presets) > MAX_PRESETS:
        raise InvalidValue(f"Pole presets musi być listą stanów filtrów (maks. {MAX_PRESETS}).")
    return ListRequest(
        query=parse_state(payload.get("state")),
        page=_int(payload.get("page"), "page", 1, 1, 10**7),
        size=_int(payload.get("size"), "size", DEFAULT_PAGE_SIZE, 1, MAX_PAGE_SIZE),
        ids=parse_ids(payload.get("ids")),
        counts=payload.get("counts") is True, totals=payload.get("totals") is True,
        presets=tuple(parse_state(state).flt for state in presets),
    )


def _view(query: ListQuery, ids: Optional[tuple]) -> RowFilter:
    return replace(query.flt, ids=ids) if ids is not None else query.flt


def list_view(db: Database, request: ListRequest) -> dict:
    """Strona wyników: `{rows, total, page, pages, size}` oraz na życzenie `overview` (liczniki statusów, segmentów
    i krajów przy bieżącym filtrze), `totals` (te same liczniki bez filtrów) i `presets` (liczba wierszy szybkich widoków)."""
    flt, sort = _view(request.query, request.ids), request.query.sort
    offset = (request.page - 1) * request.size
    jobs: dict = {"rows": lambda: db.list_page(flt, sort, offset, request.size)}
    jobs["overview" if request.counts else "total"] = (
        (lambda: db.overview(flt)) if request.counts else (lambda: db.count_matching(flt)))
    if request.totals:
        jobs["totals"] = lambda: db.overview(ALL_ROWS)
    if request.presets:
        jobs["presets"] = lambda: db.match_counts(request.presets)
    done = db.run_together(jobs)
    total = done["overview"]["total"] if request.counts else done["total"]
    pages = max(1, ceil(total / request.size))
    page, rows = request.page, done["rows"]
    if page > pages:  # np. po usunięciu wierszy albo zawężeniu filtra: ostatnia strona zamiast pustej
        page = pages
        rows = db.list_page(flt, sort, (page - 1) * request.size, request.size)
    result: dict = {"rows": rows, "total": total, "page": page, "pages": pages, "size": request.size}
    if request.counts:
        result["overview"] = {facet: done["overview"][facet] for facet in ("statuses", "segments", "countries")}
    if request.totals:
        result["totals"] = {**{facet: done["totals"][facet] for facet in ("statuses", "segments", "countries")},
                            "all": done["totals"]["total"]}
    if request.presets:
        result["presets"] = done["presets"]
    return result


def view_ids(db: Database, payload: Mapping) -> dict:
    """Id wierszy widoku w kolejności widoku: `{ids, truncated}`. `ready: true` zawęża do firm gotowych do wysyłki,
    `exclude_statuses` wyklucza podane statusy (np. „Wysyłanie…”, którego nie da się zaznaczyć), `limit` bierze pierwsze N
    (np. „zaznacz pierwsze 10”); bez limitu bierzemy najwyżej `MAX_LIST_IDS`."""
    query = parse_state(payload.get("state"))
    flt = replace(_view(query, parse_ids(payload.get("ids"))),
                  not_statuses=parse_strings(payload.get("exclude_statuses"), "exclude_statuses"))
    if payload.get("ready") is True:
        flt = replace(flt, ready=True)
    limit = _int(payload.get("limit"), "limit", MAX_LIST_IDS, 1, MAX_LIST_IDS)
    ids = db.list_ids(flt, query.sort, limit + 1)
    return {"ids": ids[:limit], "truncated": len(ids) > limit}


def selection_summary(db: Database, payload: Mapping) -> dict:
    """Co jest w zaznaczeniu: `{count, ready, foreign, in_view?, ready_ids?}`. `foreign` to liczba firm z każdego kraju poza
    Polską; `in_view` (gdy podano `state`) mówi, ile zaznaczonych mieści się w bieżącym filtrze; `ready_ids` (na życzenie)
    to firmy gotowe do wysyłki w kolejności widoku — w niej idzie wysyłka."""
    ids = parse_ids(payload.get("ids")) or ()
    query = parse_state(payload.get("state"))
    selection = RowFilter(ids=ids)
    ready = replace(selection, ready=True)
    jobs: dict = {
        "overview": lambda: db.overview(selection),
        "ready": (lambda: db.list_ids(ready, query.sort)) if payload.get("ready_ids") is True
        else (lambda: db.count_matching(ready)),
    }
    if "state" in payload:
        jobs["in_view"] = lambda: db.count_matching(replace(query.flt, ids=ids))
    done = db.run_together(jobs)
    countries = done["overview"]["countries"]
    result: dict = {
        "count": done["overview"]["total"],
        "foreign": {code: n for code, n in countries.items() if code not in (HOME_COUNTRY, NO_COUNTRY, FOREIGN_COUNTRY)},
    }
    if payload.get("ready_ids") is True:
        result["ready_ids"], result["ready"] = done["ready"], len(done["ready"])
    else:
        result["ready"] = done["ready"]
    if "in_view" in done:
        result["in_view"] = done["in_view"]
    return result


def selected_emails(db: Database, payload: Mapping) -> dict:
    """Różne adresy e-mail zaznaczonych firm: `{emails, rows, without_email}` (do skopiowania do schowka)."""
    ids = parse_ids(payload.get("ids")) or ()
    selection = RowFilter(ids=ids)
    done = db.run_together({
        "emails": lambda: db.distinct_emails(ids),
        "rows": lambda: db.count_matching(selection),
        "with_email": lambda: db.count_matching(replace(selection, has_email=True)),
    })
    return {"emails": done["emails"], "rows": done["rows"], "without_email": done["rows"] - done["with_email"]}
