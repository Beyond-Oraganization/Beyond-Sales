"""Zapytania listy i widoku wątku odpowiedzi (SQL tylko tutaj).

Lista jest liczona w bazie, tak jak lista firm: zakładki, filtry, wyszukiwanie, sortowanie i liczniki. Wątek łączy się z
mailem kampanii (`maile`) i firmą (`podmioty`) przez wspólny kształt zapytań `company_sql.select_mails`.
"""

from __future__ import annotations

import re
from datetime import date, datetime, timedelta
from typing import Optional

import sqlalchemy as sa

from perno.db.company_sql import column_for, select_mails
from perno.db.replies_schema import odp_oceny, odp_watki, odp_wiadomosci
from perno.db.schema import maile
from perno.domain import replies as rp
from perno.domain import reply_listing as rl

_LIKE_SPECIAL = re.compile(r"([\\%_])")
HUMAN = odp_watki.c.human_in_count > 0


def _like(word: str) -> str:
    return "%" + _LIKE_SPECIAL.sub(r"\\\1", word) + "%"


def _any_message(*conditions):
    return sa.exists().where(odp_wiadomosci.c.thread_id == odp_watki.c.id, *conditions)


def tab_condition(tab: str):
    """Warunek zakładki na wątku (zakładka „Nieprzypisane” dotyczy wiadomości, nie wątków: osobne zapytanie)."""
    conditions = {
        rl.TAB_TODO: sa.and_(HUMAN, odp_watki.c.awaiting_reply.is_(True), odp_watki.c.closed_at.is_(None)),
        rl.TAB_ALL: HUMAN,
        rl.TAB_POSITIVE: sa.and_(HUMAN, odp_watki.c.rating == rp.RATING_POSITIVE),
        rl.TAB_NEUTRAL: sa.and_(HUMAN, odp_watki.c.rating == rp.RATING_NEUTRAL),
        rl.TAB_NEGATIVE: sa.and_(HUMAN, odp_watki.c.rating == rp.RATING_NEGATIVE),
        rl.TAB_UNRATED: sa.and_(HUMAN, odp_watki.c.rating.is_(None)),
        rl.TAB_MACHINE: sa.and_(~HUMAN, _any_message(odp_wiadomosci.c.kind.in_((rp.KIND_AUTO, rp.KIND_BOUNCE)))),
    }
    return conditions[tab]


def _word_condition(word: str):
    pattern = _like(word)
    columns = (column_for("company"), maile.c.email, maile.c.subject, odp_watki.c.last_snippet, odp_watki.c.last_from)
    in_messages = _any_message(sa.func.lower(odp_wiadomosci.c.body_text).like(pattern, escape="\\"))
    return sa.or_(*(sa.func.lower(column).like(pattern, escape="\\") for column in columns), in_messages)


def thread_filters(flt: rl.ReplyFilter, *, with_tab: bool = True) -> list:
    """Warunki WHERE dla wątków (tab opcjonalnie: liczniki zakładek liczy się bez niej)."""
    clauses: list = [tab_condition(flt.tab)] if with_tab else []
    clauses += [_word_condition(word) for word in flt.words]
    if flt.mailboxes:
        clauses.append(sa.func.lower(odp_watki.c.mailbox).in_(flt.mailboxes))
    if flt.segments:
        clauses.append(column_for("segment").in_(flt.segments))
    if flt.countries:
        clauses.append(column_for("country").in_(flt.countries))
    if flt.since:
        clauses.append(odp_watki.c.last_activity_at >= _midnight(flt.since))
    if flt.until:
        clauses.append(odp_watki.c.last_activity_at < _midnight(flt.until) + timedelta(days=1))
    return clauses


def _midnight(day: date) -> datetime:
    return datetime(day.year, day.month, day.day)


def _threads_select(*columns):
    return select_mails(*columns).join(odp_watki, odp_watki.c.id == maile.c.id)


def list_columns() -> list:
    return [
        odp_watki.c.id, column_for("company").label("company"), column_for("segment").label("segment"),
        column_for("country").label("country"), maile.c.email.label("email"), maile.c.subject.label("campaign_subject"),
        maile.c.status.label("mail_status"), odp_watki.c.mailbox, odp_watki.c.rating, odp_watki.c.rating_by, odp_watki.c.rating_note,
        odp_watki.c.optout, odp_watki.c.read_at, odp_watki.c.closed_at, odp_watki.c.awaiting_reply, odp_watki.c.weak,
        odp_watki.c.other_mailbox, odp_watki.c.human_in_count, odp_watki.c.last_in_at, odp_watki.c.last_out_at,
        odp_watki.c.last_activity_at, odp_watki.c.last_snippet, odp_watki.c.last_from, odp_watki.c.last_direction,
        sa.case((_any_message(odp_wiadomosci.c.kind == rp.KIND_BOUNCE), True), else_=False).label("has_bounce"),
        sa.case((_any_message(odp_wiadomosci.c.kind == rp.KIND_AUTO), True), else_=False).label("has_auto"),
    ]


def list_threads(conn, flt: rl.ReplyFilter, page: int, size: int) -> tuple:
    """`(wiersze, suma)` jednej strony zakładki wątków, od najświeższej aktywności."""
    clauses = thread_filters(flt)
    total = conn.execute(_threads_select(sa.func.count()).where(*clauses)).scalar() or 0
    stmt = (_threads_select(*list_columns()).where(*clauses)
            .order_by(odp_watki.c.last_activity_at.desc(), odp_watki.c.id).limit(size).offset((page - 1) * size))
    return [dict(row) for row in conn.execute(stmt).mappings()], int(total)


def tab_counts(conn, flt: rl.ReplyFilter) -> dict:
    """Liczba wątków w każdej zakładce przy tych samych zawężeniach (bez zakładki) i liczba nieprzypisanych wiadomości."""
    base = thread_filters(flt, with_tab=False)
    counts = {}
    for tab in rl.TABS:
        if tab == rl.TAB_UNASSIGNED:
            continue
        counts[tab] = int(conn.execute(_threads_select(sa.func.count()).where(*base, tab_condition(tab))).scalar() or 0)
    counts[rl.TAB_UNASSIGNED] = int(conn.execute(
        sa.select(sa.func.count()).select_from(odp_wiadomosci).where(*message_filters(flt))).scalar() or 0)
    return counts


def message_filters(flt: rl.ReplyFilter) -> list:
    """Warunki dla wiadomości bez wątku (zakładka „Nieprzypisane”)."""
    m = odp_wiadomosci.c
    clauses: list = [m.thread_id.is_(None), m.direction == rp.DIRECTION_IN]
    for word in flt.words:
        pattern = _like(word)
        clauses.append(sa.or_(*(sa.func.lower(column).like(pattern, escape="\\") for column in (m.subject, m.from_addr, m.from_name, m.snippet))))
    if flt.mailboxes:
        clauses.append(sa.func.lower(m.mailbox).in_(flt.mailboxes))
    return clauses


def list_unassigned(conn, flt: rl.ReplyFilter, page: int, size: int) -> tuple:
    """`(wiersze, suma)` wiadomości, które nie trafiły do żadnego wątku."""
    clauses = message_filters(flt)
    total = conn.execute(sa.select(sa.func.count()).select_from(odp_wiadomosci).where(*clauses)).scalar() or 0
    m = odp_wiadomosci.c
    stmt = (sa.select(m.id, m.from_addr, m.from_name, m.subject, m.snippet, m.date_at, m.mailbox, m.kind)
            .where(*clauses).order_by(m.date_at.desc(), m.id.desc()).limit(size).offset((page - 1) * size))
    return [dict(row) for row in conn.execute(stmt).mappings()], int(total)


def thread_row(conn, thread_id: str) -> Optional[dict]:
    """Jeden wątek w kształcie wiersza listy (None, gdy go nie ma)."""
    row = conn.execute(_threads_select(*list_columns()).where(odp_watki.c.id == thread_id)).mappings().first()
    return dict(row) if row else None


def origin_mail(conn, thread_id: str) -> Optional[dict]:
    """Mail kampanii, na który odpowiedziano: temat, treść, kiedy i skąd wyszedł, firma."""
    stmt = select_mails(maile.c.id, maile.c.email, maile.c.subject, maile.c.body_text, maile.c.sent_at, maile.c.sent_from, maile.c.status,
                        maile.c.message_id, maile.c.mode, column_for("company").label("company"), column_for("website").label("website"),
                        column_for("country").label("country"), column_for("segment").label("segment"),
                        column_for("company_id").label("company_id"), column_for("mail_count").label("mail_count"),
                        ).where(maile.c.id == thread_id)
    row = conn.execute(stmt).mappings().first()
    return dict(row) if row else None


def thread_messages(conn, thread_id: str) -> list:
    """Wszystkie wiadomości wątku (przychodzące, nasze i szkice) od najstarszej."""
    m = odp_wiadomosci.c
    stmt = sa.select(m.id, m.direction, m.kind, m.state, m.author, m.mailbox, m.folder, m.message_id, m.in_reply_to, m.references_hdr,
                     m.from_addr, m.from_name, m.to_addr, m.subject, m.date_at, m.body_text, m.attachments, m.match_method,
                     m.match_weak, m.bounce_type, m.optout_hint, m.error).where(m.thread_id == thread_id).order_by(
        sa.func.coalesce(m.date_at, m.fetched_at), m.id)
    return [dict(row) for row in conn.execute(stmt).mappings()]


def rating_log(conn, thread_id: str) -> list:
    """Historia ocen wątku od najnowszej."""
    c = odp_oceny.c
    stmt = sa.select(c.id, c.rating, c.by, c.note, c.at).where(c.thread_id == thread_id).order_by(c.at.desc(), c.id.desc())
    return [dict(row) for row in conn.execute(stmt).mappings()]


def unread_total(conn) -> int:
    """Wątki z odpowiedzią człowieka czekające na nas i nieprzeczytane (do licznika w menu)."""
    stmt = sa.select(sa.func.count()).select_from(odp_watki).where(
        HUMAN, odp_watki.c.awaiting_reply.is_(True), odp_watki.c.closed_at.is_(None), odp_watki.c.read_at.is_(None))
    return int(conn.execute(stmt).scalar() or 0)


def unassigned_message(conn, message_id: int) -> Optional[dict]:
    """Jedna wiadomość bez wątku (do widoku „Nieprzypisane”); None, gdy nie istnieje albo ma już wątek."""
    m = odp_wiadomosci.c
    stmt = sa.select(m.id, m.direction, m.kind, m.state, m.author, m.mailbox, m.folder, m.message_id, m.in_reply_to, m.references_hdr,
                     m.from_addr, m.from_name, m.to_addr, m.subject, m.date_at, m.body_text, m.attachments, m.match_method,
                     m.match_weak, m.bounce_type, m.optout_hint, m.error).where(m.id == message_id, m.thread_id.is_(None))
    row = conn.execute(stmt).mappings().first()
    return dict(row) if row else None


def search_sent_mails(conn, words: tuple, limit: int) -> list:
    """Wysłane maile kampanii pasujące do słów (firma, adres, temat): kandydaci do ręcznego przypisania wiadomości."""
    columns = (column_for("company"), maile.c.email, maile.c.subject)
    clauses = [sa.or_(*(sa.func.lower(column).like(_like(word), escape="\\") for column in columns)) for word in words]
    stmt = (select_mails(maile.c.id, column_for("company").label("company"), maile.c.email, maile.c.subject, maile.c.sent_at, maile.c.status)
            .where(maile.c.sent_at.is_not(None), *clauses).order_by(maile.c.sent_at.desc(), maile.c.id).limit(limit))
    return [dict(row) for row in conn.execute(stmt).mappings()]
