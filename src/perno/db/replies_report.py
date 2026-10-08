"""Zapytania raportu odpowiedzi (SQL tylko tutaj): zestawienia liczone w bazie warunkowymi sumami, bez pobierania wierszy.

Jednostką jest mail kampanii: maile, które wyszły (`KEEPS_SENT_AT` i data wysłania), z lewym złączeniem wątku (jeden wątek na
mail, więc nic się nie dubluje). Skrzynka maila bez zapisanej skrzynki (sprzed wielu skrzynek) to skrzynka główna.
"""

from __future__ import annotations

from datetime import date, datetime, timedelta
from typing import Optional

import sqlalchemy as sa

from perno.db.company_sql import column_for, company_key, select_mails
from perno.db.replies_queries import HUMAN
from perno.db.replies_schema import odp_watki, odp_wiadomosci
from perno.db.schema import maile
from perno.domain import replies as rp
from perno.domain import reply_listing as rl
from perno.domain import statuses as st

_HARD = maile.c.status == st.STATUS_BOUNCED


def _flag(condition):
    return sa.func.sum(sa.case((condition, 1), else_=0))


def _mailbox_expression(primary: str):
    return sa.func.lower(sa.func.coalesce(sa.func.nullif(maile.c.sent_from, ""), primary))


def _midnight(day: date) -> datetime:
    return datetime(day.year, day.month, day.day)


def _scope(flt: rl.ReportFilter, primary: str) -> list:
    clauses: list = [maile.c.status.in_(st.KEEPS_SENT_AT), maile.c.sent_at.is_not(None)]
    if flt.since:
        clauses.append(maile.c.sent_at >= _midnight(flt.since))
    if flt.until:
        clauses.append(maile.c.sent_at < _midnight(flt.until) + timedelta(days=1))
    if flt.segments:
        clauses.append(column_for("segment").in_(flt.segments))
    if flt.countries:
        clauses.append(column_for("country").in_(flt.countries))
    if flt.mailboxes:
        clauses.append(_mailbox_expression(primary).in_(flt.mailboxes))
    return clauses


def _base(*columns):
    return select_mails(*columns).outerjoin(odp_watki, odp_watki.c.id == maile.c.id)


def _counters() -> list:
    return [
        sa.func.count().label("sent"), _flag(_HARD).label("bounced"), _flag(HUMAN).label("replied"),
        _flag(sa.and_(HUMAN, odp_watki.c.rating == rp.RATING_POSITIVE)).label("positive"),
        _flag(sa.and_(HUMAN, odp_watki.c.rating == rp.RATING_NEUTRAL)).label("neutral"),
        _flag(sa.and_(HUMAN, odp_watki.c.rating == rp.RATING_NEGATIVE)).label("negative"),
        _flag(sa.and_(HUMAN, odp_watki.c.rating.is_(None))).label("unrated"),
        _flag(sa.and_(HUMAN, odp_watki.c.optout.is_(True))).label("optout"),
        _flag(sa.and_(HUMAN, odp_watki.c.awaiting_reply.is_(True), odp_watki.c.closed_at.is_(None))).label("awaiting"),
    ]


def totals(conn, flt: rl.ReportFilter, primary: str) -> dict:
    """Jedna linia z całością oraz liczby dodatkowe: firmy z odpowiedzią, autoodpowiedzi, zwroty czasowe, od kiedy czeka najstarsza."""
    exists = lambda *conditions: sa.exists().where(odp_wiadomosci.c.thread_id == odp_watki.c.id, *conditions)
    stmt = _base(*_counters(),
                 sa.func.count(sa.distinct(sa.case((HUMAN, company_key()), else_=None))).label("replied_companies"),
                 _flag(exists(odp_wiadomosci.c.kind == rp.KIND_AUTO)).label("auto_threads"),
                 _flag(exists(odp_wiadomosci.c.bounce_type == rp.BOUNCE_SOFT)).label("soft_bounces"),
                 sa.func.min(sa.case((sa.and_(HUMAN, odp_watki.c.awaiting_reply.is_(True), odp_watki.c.closed_at.is_(None)),
                                     odp_watki.c.last_in_at), else_=None)).label("awaiting_since")).where(*_scope(flt, primary))
    return dict(conn.execute(stmt).mappings().one())


def grouped(conn, flt: rl.ReportFilter, group: str, primary: str) -> list:
    """Zestawienie po jednym wymiarze: `segment`, `country`, `mailbox` albo `day`; największe grupy pierwsze (dni chronologicznie)."""
    key = {"segment": column_for("segment"), "country": column_for("country"), "mailbox": _mailbox_expression(primary),
           "day": sa.func.date(maile.c.sent_at)}[group]
    stmt = _base(key.label("key"), *_counters()).where(*_scope(flt, primary)).group_by(key)
    stmt = stmt.order_by(key) if group == "day" else stmt.order_by(sa.func.count().desc(), key)
    return [dict(row) for row in conn.execute(stmt).mappings()]


def latency_pairs(conn, flt: rl.ReportFilter, primary: str) -> list:
    """Pary (wysłano, pierwsza odpowiedź człowieka) dla maili z odpowiedzią: z nich liczy się czas reakcji."""
    first = sa.func.min(odp_wiadomosci.c.date_at)
    stmt = (select_mails(maile.c.sent_at, first).join(odp_watki, odp_watki.c.id == maile.c.id)
            .join(odp_wiadomosci, sa.and_(odp_wiadomosci.c.thread_id == odp_watki.c.id, odp_wiadomosci.c.direction == rp.DIRECTION_IN,
                                          odp_wiadomosci.c.kind == rp.KIND_HUMAN))
            .where(*_scope(flt, primary)).group_by(maile.c.id, maile.c.sent_at))
    return [(sent, replied) for sent, replied in conn.execute(stmt)]


def mailbox_usage(conn, flt: rl.ReportFilter, primary: str) -> dict:
    """Ile maili wysłano z każdej skrzynki w okresie (do oceny pokrycia odbioru)."""
    return {str(row["key"]): int(row["sent"]) for row in grouped(conn, flt, "mailbox", primary)}


def first_sent_day(conn) -> Optional[date]:
    """Dzień pierwszej wysyłki (domyślny początek okresu raportu)."""
    value = conn.execute(sa.select(sa.func.min(maile.c.sent_at)).where(maile.c.status.in_(st.KEEPS_SENT_AT))).scalar()
    return value.date() if isinstance(value, datetime) else None
