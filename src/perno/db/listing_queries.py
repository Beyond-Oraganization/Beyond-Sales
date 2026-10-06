"""Zapytania listy firm w panelu: strona wyników, id w kolejności widoku, liczniki i zliczanie wielu warunków naraz.

Wszystko liczy baza (przy 50 tys. firm lista w przeglądarce miałaby 27 MB i otwierała się kilka sekund): strona to
`LIMIT/OFFSET` po filtrze i sortowaniu, a liczniki to jedno `GROUP BY`. `RowFilter.ids` zawęża do zaznaczonych wierszy
(widok „tylko zaznaczone”), ale w odróżnieniu od `iter_rows` kolejność wyznacza sortowanie, nie kolejność `ids`.
"""

from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from typing import Any, Mapping, Optional, Sequence

import sqlalchemy as sa
from sqlalchemy.engine import Engine

from perno.db.codec import format_value
from perno.db.company_sql import column_for, mail_company_join, select_mails
from perno.db.listing_sql import sort_clauses
from perno.db.queries import blank, filled, filter_clauses, ordering, trimmed_filled
from perno.db.schema import LIGHT_COLUMNS, companies, maile
from perno.domain.rowfilter import FOREIGN_COUNTRY, HOME_COUNTRY, NO_COUNTRY, RowFilter, country_selected

FACETS = {
    "statuses": maile.c.status,
    "segments": companies.c.segment,
    "countries": sa.case((blank(companies.c.country), NO_COUNTRY), else_=companies.c.country),
}


def has_body_expression() -> Any:
    """1, gdy wiersz ma treść maila; lista nie pobiera samej treści (to większość danych), tylko tę flagę."""
    return sa.case((filled(maile.c.body_html), 1), else_=0)


def light_columns() -> list:
    """Kolumny listy: wszystko poza treścią maili, plus `has_body`."""
    return [*[column_for(name) for name in LIGHT_COLUMNS], has_body_expression().label("has_body")]


def to_light_row(row: Mapping) -> dict:
    """Wiersz listy w formacie API (daty jako tekst ISO) i z `has_body` jako wartością logiczną."""
    light: dict[str, Any] = {name: format_value(row[name]) for name in LIGHT_COLUMNS}
    light["has_body"] = bool(row["has_body"])
    return light


class ListingQueries:
    """Odczyty listy; część `MailRepository` (korzysta z jego `engine`)."""

    engine: Engine

    def run_together(self, jobs: Mapping) -> dict:
        """Wykonuje niezależne odczyty (`{nazwa: funkcja}`) i zwraca `{nazwa: wynik}`. Na serwerze bazy działają one naraz
        w osobnych połączeniach (strona wyników i liczniki to dwa przejścia po tabeli, więc czas żądania to dłuższe z nich,
        a nie suma); sqlite ma jedno połączenie, więc tam idą po kolei."""
        if len(jobs) < 2 or self.engine.dialect.name == "sqlite":
            return {name: job() for name, job in jobs.items()}
        with ThreadPoolExecutor(max_workers=len(jobs)) as pool:
            futures = {name: pool.submit(job) for name, job in jobs.items()}
            return {name: future.result() for name, future in futures.items()}

    def _where(self, flt: RowFilter) -> list:
        clauses = filter_clauses(flt)
        if flt.ids is not None:
            clauses.append(maile.c.id.in_(list(dict.fromkeys(flt.ids))) if flt.ids else sa.false())
        return clauses

    def _order(self, sort: str) -> list:
        return sort_clauses(sort, self.engine.dialect.name, ordering())

    def list_page(self, flt: RowFilter, sort: str, offset: int, limit: int) -> list:
        """Jedna strona listy: wiersze (bez treści maili) po filtrze, w kolejności `sort` (remisy: kolejność panelu).
        Najpierw sortujemy same id (wąskie wiersze: ok. 6 razy szybciej niż z całymi wierszami przy głębokiej stronie na
        50 tys. firm), potem pobieramy tylko wiersze tej strony."""
        ids = self.list_ids(flt, sort, limit, offset)
        if not ids:
            return []
        stmt = select_mails(*light_columns()).where(maile.c.id.in_(ids))
        with self.engine.connect() as conn:
            found = {row["id"]: to_light_row(row) for row in conn.execute(stmt).mappings()}
        return [found[row_id] for row_id in ids if row_id in found]

    def list_ids(self, flt: RowFilter, sort: str, limit: Optional[int] = None, offset: int = 0) -> list:
        """Id wierszy po filtrze w kolejności `sort`; `limit` zawęża do pierwszych (np. „zaznacz pierwsze 10”)."""
        stmt = select_mails(maile.c.id).where(*self._where(flt)).order_by(*self._order(sort))
        if limit is not None:
            stmt = stmt.limit(limit).offset(offset)
        with self.engine.connect() as conn:
            return [row[0] for row in conn.execute(stmt)]

    def count_matching(self, flt: RowFilter) -> int:
        """Ile wierszy pasuje do filtra (w odróżnieniu od `count_rows` bez dzielenia `ids` na partie)."""
        stmt = select_mails(sa.func.count()).where(*self._where(flt))
        with self.engine.connect() as conn:
            return conn.execute(stmt).scalar() or 0

    def overview(self, flt: RowFilter) -> dict:
        """Jedno przejście po tabeli: `{"total", "statuses", "segments", "countries"}`. Suma to liczba wierszy pasujących
        do całego filtra; każdy licznik pokazuje, ilu wierszy dotyczyłaby dana wartość przy pozostałych filtrach (bez
        filtra tego pola), żeby przy nieaktywnej wartości widać było, ile wierszy doszłoby po jej wybraniu. Wyszukiwarkę
        i inne warunki liczy baza raz (`GROUP BY` po trzech polach), a rozdzielenie na liczniki robimy tu, w pamięci."""
        base = replace(flt, statuses=(), segments=(), countries=())
        columns = [maile.c.status, companies.c.segment, FACETS["countries"].label("country_key")]
        stmt = select_mails(*columns, sa.func.count()).where(*self._where(base)).group_by(*columns)
        with self.engine.connect() as conn:
            groups = [(row[0], row[1], row[2], row[3]) for row in conn.execute(stmt)]
        statuses, segments, countries = set(flt.statuses), set(flt.segments), flt.countries
        counts: dict[str, dict] = {"statuses": {}, "segments": {}, "countries": {}}
        total = 0
        for status, segment, country, n in groups:
            status_ok = not statuses or status in statuses
            segment_ok = not segments or segment in segments
            country_ok = country_selected(countries, country)
            if status_ok and segment_ok and country_ok:
                total += n
            if segment_ok and country_ok:
                counts["statuses"][status] = counts["statuses"].get(status, 0) + n
            if status_ok and country_ok:
                counts["segments"][segment] = counts["segments"].get(segment, 0) + n
            if status_ok and segment_ok:
                counts["countries"][country] = counts["countries"].get(country, 0) + n
        counts["countries"][FOREIGN_COUNTRY] = sum(n for code, n in counts["countries"].items()
                                                   if code not in (NO_COUNTRY, HOME_COUNTRY, FOREIGN_COUNTRY))
        return {"total": total, **counts}

    def facet_counts(self, flt: RowFilter, facet: str) -> dict:
        """Ile wierszy ma każdą wartość pola (`statuses`, `segments`, `countries`) przy filtrze `flt`. Kraj bez wartości
        to `NO_COUNTRY`; dla krajów dochodzi `FOREIGN_COUNTRY` = wszystkie spoza Polski. Wołający zdejmuje z filtra pole,
        które liczy, żeby licznik przy nieaktywnej wartości mówił, ile wierszy doszłoby po jej wybraniu."""
        value = FACETS[facet].label("value")
        stmt = select_mails(value, sa.func.count()).where(*self._where(flt)).group_by(value)
        with self.engine.connect() as conn:
            counts = {row[0]: row[1] for row in conn.execute(stmt)}
        if facet == "countries":
            counts[FOREIGN_COUNTRY] = sum(n for code, n in counts.items() if code not in (NO_COUNTRY, HOME_COUNTRY))
        return counts

    def match_counts(self, flts: Sequence[RowFilter]) -> list:
        """Ile wierszy pasuje do każdego z filtrów: jedno przejście po tabeli zamiast jednego zapytania na filtr."""
        if not flts:
            return []
        columns = [sa.func.coalesce(sa.func.sum(sa.case((sa.and_(sa.true(), *self._where(flt)), 1), else_=0)), 0)
                   for flt in flts]
        with self.engine.connect() as conn:
            return [int(value) for value in conn.execute(sa.select(*columns).select_from(mail_company_join)).one()]

    def distinct_emails(self, ids: Sequence[str]) -> list:
        """Różne adresy e-mail wskazanych wierszy (bez pustych), w kolejności alfabetycznej."""
        if not ids:
            return []
        stmt = (sa.select(sa.func.trim(maile.c.email)).where(maile.c.id.in_(list(dict.fromkeys(ids))),
                                                             trimmed_filled(maile.c.email)).distinct())
        with self.engine.connect() as conn:
            return sorted(row[0] for row in conn.execute(stmt))
