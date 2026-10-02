"""Fragmenty zapytań do tabeli `maile`: kolejność panelu i zamiana `RowFilter` na warunki WHERE."""

from __future__ import annotations

from datetime import date, datetime, timedelta
from typing import Iterator

import sqlalchemy as sa
from sqlalchemy import ColumnElement

from perno.db.schema import maile
from perno.domain import statuses as st
from perno.domain.rowfilter import NO_COUNTRY, RowFilter

ID_CHUNK = 500  # tyle id naraz w jednym zapytaniu `IN (…)` (limit zmiennych sqlite, rozmiar zapytania MySQL)


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


def day_start(day: date) -> datetime:
    return datetime(day.year, day.month, day.day)


def filter_clauses(flt: RowFilter) -> list:
    """Warunki WHERE dla wszystkich pól filtra poza `ids` (te obsługują `count_rows` i `iter_rows` partiami)."""
    clauses: list[ColumnElement[bool]] = []
    if flt.segments:
        clauses.append(maile.c.segment.in_(flt.segments))
    if flt.statuses:
        clauses.append(maile.c.status.in_(flt.statuses))
    if flt.countries:
        options = []
        codes = [code for code in flt.countries if code != NO_COUNTRY]
        if codes:
            options.append(maile.c.country.in_(codes))
        if NO_COUNTRY in flt.countries:
            options.append(blank(maile.c.country))
        clauses.append(sa.or_(*options))
    if flt.has_email is not None:
        trimmed = sa.func.trim(maile.c.email)  # adres złożony ze spacji to brak adresu (tak samo liczy lista w panelu)
        clauses.append(sa.and_(maile.c.email.is_not(None), trimmed != "") if flt.has_email
                       else sa.or_(maile.c.email.is_(None), trimmed == ""))
    if flt.has_body is not None:
        clauses.append(filled(maile.c.body_html) if flt.has_body else blank(maile.c.body_html))
    if flt.sent_from is not None:
        clauses.append(maile.c.sent_at >= day_start(flt.sent_from))
    if flt.sent_to is not None:
        clauses.append(maile.c.sent_at < day_start(flt.sent_to) + timedelta(days=1))
    return clauses


def id_chunks(ids) -> Iterator[list]:
    unique = list(dict.fromkeys(ids))  # duplikaty znikają, kolejność pierwszych wystąpień zostaje
    for start in range(0, len(unique), ID_CHUNK):
        yield unique[start:start + ID_CHUNK]
