"""Fragmenty zapytań do tabeli `maile`: kolejność panelu i zamiana `RowFilter` na warunki WHERE."""

from __future__ import annotations

from datetime import date, datetime, timedelta
from typing import Iterator

import sqlalchemy as sa
from sqlalchemy import ColumnElement

from perno.db.listing_sql import search_clauses
from perno.db.schema import maile
from perno.domain import statuses as st
from perno.domain.rowfilter import FOREIGN_COUNTRY, HOME_COUNTRY, NO_COUNTRY, RowFilter

ID_CHUNK = 500  # tyle id naraz w jednym zapytaniu `IN (…)` (limit zmiennych sqlite, rozmiar zapytania MySQL)
IMPORT_ID_PATTERN = r"-n[0-9]+$"  # id firm z importu CSV (`eventy-n001`); firmy z dossier mają `eventy-051`


def ordering() -> list:
    segment_rank = sa.case(
        *[(maile.c.segment == name, i) for i, name in enumerate(st.SEGMENT_ORDER)],
        else_=len(st.SEGMENT_ORDER),
    )
    no_number_last = sa.case((maile.c.row_number.is_(None), 1), else_=0)  # NULL sortuje się różnie zależnie od bazy
    return [segment_rank, no_number_last, maile.c.row_number, maile.c.id]


def filled(column):
    return sa.and_(column.is_not(None), column != "")


def blank(column):
    return sa.or_(column.is_(None), column == "")


def trimmed_filled(column):
    """Wartość z czymś więcej niż spacje (pole złożone ze spacji to brak wartości, tak jak liczy lista w panelu)."""
    return sa.and_(column.is_not(None), sa.func.trim(column) != "")


def trimmed_blank(column):
    return sa.or_(column.is_(None), sa.func.trim(column) == "")


def tristate(column, wanted):
    """Warunek „jest”/„brak” dla pola tekstowego; `None` = bez ograniczenia."""
    if wanted is None:
        return []
    return [trimmed_filled(column) if wanted else trimmed_blank(column)]


def day_range(column, first, last) -> list:
    """Dni kalendarzowe, oba końce włącznie; wiersz bez daty nigdy nie mieści się w zakresie (NULL)."""
    clauses = []
    if first is not None:
        clauses.append(column >= day_start(first))
    if last is not None:
        clauses.append(column < day_start(last) + timedelta(days=1))
    return clauses


def day_start(day: date) -> datetime:
    return datetime(day.year, day.month, day.day)


def _country_clause(selected: tuple) -> ColumnElement[bool]:
    options: list[ColumnElement[bool]] = []
    codes = [code for code in selected if code not in (NO_COUNTRY, FOREIGN_COUNTRY)]
    if codes:
        options.append(maile.c.country.in_(codes))
    if NO_COUNTRY in selected:
        options.append(blank(maile.c.country))
    if FOREIGN_COUNTRY in selected:
        options.append(sa.and_(filled(maile.c.country), maile.c.country != HOME_COUNTRY))
    return sa.or_(*options)


def filter_clauses(flt: RowFilter) -> list:
    """Warunki WHERE dla wszystkich pól filtra poza `ids` (te obsługują `count_rows` i `iter_rows` partiami)."""
    clauses: list[ColumnElement[bool]] = []
    if flt.segments:
        clauses.append(maile.c.segment.in_(flt.segments))
    if flt.statuses:
        clauses.append(maile.c.status.in_(flt.statuses))
    if flt.not_statuses:
        clauses.append(maile.c.status.not_in(flt.not_statuses))
    if flt.countries:
        clauses.append(_country_clause(flt.countries))
    clauses += tristate(maile.c.email, flt.has_email)  # adres złożony ze spacji to brak adresu
    if flt.has_body is not None:
        clauses.append(filled(maile.c.body_html) if flt.has_body else blank(maile.c.body_html))
    clauses += tristate(maile.c.city, flt.has_city)
    clauses += tristate(maile.c.address, flt.has_address)
    if flt.has_sent is not None:
        clauses.append(maile.c.sent_at.is_not(None) if flt.has_sent else maile.c.sent_at.is_(None))
    if flt.origin:
        imported = maile.c.id.regexp_match(IMPORT_ID_PATTERN)
        clauses.append(imported if flt.origin == "import" else sa.not_(imported))
    clauses += day_range(maile.c.sent_at, flt.sent_from, flt.sent_to)
    clauses += day_range(maile.c.generated_at, flt.generated_from, flt.generated_to)
    if flt.search:
        clauses += search_clauses(flt.search)
    if flt.ready:
        clauses += [maile.c.status.in_(st.SENDABLE_STATUSES), trimmed_filled(maile.c.email), filled(maile.c.body_html)]
    return clauses


def id_chunks(ids) -> Iterator[list]:
    unique = list(dict.fromkeys(ids))  # duplikaty znikają, kolejność pierwszych wystąpień zostaje
    for start in range(0, len(unique), ID_CHUNK):
        yield unique[start:start + ID_CHUNK]
