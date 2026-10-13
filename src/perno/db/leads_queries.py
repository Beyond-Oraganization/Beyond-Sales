"""Zapytania leadów (SQL tylko tutaj).

Firma jest aktywnym leadem, gdy ma wątek z odpowiedzią człowieka i oceną pozytywną albo neutralną (`qualifying_query`: jeden wiersz
na firmę, wątki łączą się z firmą przez mail kampanii jak w `replies_queries`). Dane leada leżą w `odp_leady`; lista łączy je z
tym podzapytaniem (lead bez kwalifikującego się wątku to „wycofany”) i z `podmioty` (segment, kraj, opis z bazy).
Filtrowanie, sortowanie i liczniki liczy baza, nie przeglądarka.
"""

from __future__ import annotations

import functools
import operator
import re
from typing import Iterable, Optional

import sqlalchemy as sa

from perno.db.company_sql import company_key, in_companies, select_mails
from perno.db.replies_schema import odp_leady, odp_watki, odp_wiadomosci
from perno.db.schema import companies, maile
from perno.domain import lead_listing as ll
from perno.domain import lead_rules as lr
from perno.domain import replies as rp
from perno.domain import statuses as st

_LIKE_SPECIAL = re.compile(r"([\\%_])")
HUMAN = odp_watki.c.human_in_count > 0
QUALIFYING = sa.and_(HUMAN, odp_watki.c.rating.in_(lr.LEAD_RATINGS))
RANK_POSITIVE, RANK_NEUTRAL = 2, 1  # `qualifying_query.best_rank`: najlepsza ocena wątków firmy


def _like(word: str) -> str:
    return "%" + _LIKE_SPECIAL.sub(r"\\\1", word) + "%"


def qualifying_query():
    """Jeden wiersz na firmę z kwalifikującym się wątkiem: najlepsza ocena, ostatnia odpowiedź i prośba o brak kontaktu."""
    key = company_key()
    best = sa.func.max(sa.case((odp_watki.c.rating == rp.RATING_POSITIVE, RANK_POSITIVE), else_=RANK_NEUTRAL))
    optout = sa.func.max(sa.case((odp_watki.c.optout.is_(True), 1), else_=0))
    return (select_mails(key.label("company_id"), best.label("best_rank"), sa.func.max(odp_watki.c.last_in_at).label("last_in_at"),
                         optout.label("optout"))
            .join(odp_watki, odp_watki.c.id == maile.c.id).where(QUALIFYING).group_by(key))


def _filled(column):
    return sa.case((sa.and_(column.is_not(None), column != ""), 1), else_=0)


def completeness_expression():
    """Ile z pól wymaganych leada jest wypełnionych (0-5), liczone w bazie."""
    return functools.reduce(operator.add, (_filled(odp_leady.c[name]) for name in lr.REQUIRED_FIELDS))


def _do_not_contact():
    """Czy jakiś adres firmy ma status „Nie kontaktować” (po indeksie `company_id`, jak `in_companies`)."""
    mail = maile.alias("lead_stop")
    belongs = sa.or_(mail.c.company_id == odp_leady.c.company_id,
                     sa.and_(mail.c.company_id.is_(None), mail.c.id == odp_leady.c.company_id))
    return sa.case((sa.exists().where(belongs, mail.c.status == st.STATUS_DO_NOT_CONTACT), 1), else_=0)


def _joined(q):
    """Leady z podzapytaniem kwalifikujących się firm i danymi firmy (oba złączenia zewnętrzne: lead nie znika bez żadnego z nich)."""
    return (odp_leady.outerjoin(q, q.c.company_id == odp_leady.c.company_id)
            .outerjoin(companies, companies.c.id == odp_leady.c.company_id))


def tab_condition(q, tab: str):
    active = q.c.company_id.is_not(None)
    conditions = {
        ll.TAB_ALL: active,
        ll.TAB_MISSING: sa.and_(active, completeness_expression() < len(lr.REQUIRED_FIELDS)),
        ll.TAB_AI: sa.and_(active, odp_leady.c.src.like('%"ai"%')),
        ll.TAB_WITHDRAWN: ~active,
    }
    return conditions[tab]


def _word_condition(word: str):
    pattern = _like(word)
    columns = (odp_leady.c.name, odp_leady.c.website, odp_leady.c.contact, odp_leady.c.phone, odp_leady.c.email, odp_leady.c.about,
               odp_leady.c.note, companies.c.company)
    return sa.or_(*(sa.func.lower(column).like(pattern, escape="\\") for column in columns))


def lead_filters(q, flt: ll.LeadFilter, *, with_tab: bool = True) -> list:
    """Warunki WHERE dla leadów (karta opcjonalnie: liczniki kart liczy się bez niej)."""
    clauses: list = [tab_condition(q, flt.tab)] if with_tab else []
    clauses += [_word_condition(word) for word in flt.words]
    if flt.segments:
        clauses.append(companies.c.segment.in_(flt.segments))
    if flt.stages:
        clauses.append(odp_leady.c.stage.in_(flt.stages))
    return clauses


def _order(q, sort: str) -> list:
    recent = sa.func.coalesce(q.c.last_in_at, odp_leady.c.updated_at)
    tail = [odp_leady.c.company_id]
    if sort == ll.SORT_NAME:
        return [sa.func.lower(sa.func.coalesce(odp_leady.c.name, "")), *tail]
    if sort == ll.SORT_RATING:
        return [sa.func.coalesce(q.c.best_rank, 0).desc(), recent.desc(), *tail]
    if sort == ll.SORT_COMPLETE:
        return [completeness_expression().asc(), recent.desc(), *tail]
    return [recent.desc(), *tail]


def list_columns(q) -> list:
    c = odp_leady.c
    return [c.company_id.label("id"), c.name, c.website, c.contact, c.phone, c.email, c.about, c.note, c.stage, c.src, c.created_at,
            c.updated_at, c.updated_by, companies.c.segment, companies.c.country, companies.c.description.label("base_description"),
            sa.case((q.c.company_id.is_not(None), 1), else_=0).label("active"), q.c.best_rank, q.c.last_in_at,
            sa.case((q.c.optout > 0, 1), else_=0).label("optout"), _do_not_contact().label("do_not_contact"),
            completeness_expression().label("complete")]


def list_leads(conn, flt: ll.LeadFilter, page: int, size: int) -> tuple:
    """`(wiersze, suma)` jednej strony wybranej karty, posortowane wg `flt.sort`."""
    q = qualifying_query().subquery("q")
    clauses = lead_filters(q, flt)
    total = conn.execute(sa.select(sa.func.count()).select_from(_joined(q)).where(*clauses)).scalar() or 0
    stmt = (sa.select(*list_columns(q)).select_from(_joined(q)).where(*clauses)
            .order_by(*_order(q, flt.sort)).limit(size).offset((page - 1) * size))
    return [dict(row) for row in conn.execute(stmt).mappings()], int(total)


def tab_counts(conn, flt: ll.LeadFilter) -> dict:
    """Liczba leadów na każdej karcie przy tych samych zawężeniach (bez karty)."""
    q = qualifying_query().subquery("q")
    base = lead_filters(q, flt, with_tab=False)
    counts = {}
    for tab in ll.TABS:
        stmt = sa.select(sa.func.count()).select_from(_joined(q)).where(*base, tab_condition(q, tab))
        counts[tab] = int(conn.execute(stmt).scalar() or 0)
    return counts


def lead_row(conn, company_id: str) -> Optional[dict]:
    """Jeden lead w kształcie wiersza listy (None, gdy go nie ma)."""
    q = qualifying_query().subquery("q")
    row = conn.execute(sa.select(*list_columns(q)).select_from(_joined(q)).where(odp_leady.c.company_id == company_id)).mappings().first()
    return dict(row) if row else None


def segments(conn) -> list:
    """Segmenty firm, które mają leady (do filtra)."""
    stmt = (sa.select(companies.c.segment).select_from(odp_leady.join(companies, companies.c.id == odp_leady.c.company_id))
            .where(companies.c.segment.is_not(None), companies.c.segment != "").distinct().order_by(companies.c.segment))
    return [row[0] for row in conn.execute(stmt)]


def threads_of(conn, company_ids: Iterable[str]) -> list:
    """Wątki z odpowiedzią człowieka tych firm, od najświeższej odpowiedzi (do wyboru wątku reprezentującego leada i do widoku)."""
    ids = list(company_ids)
    if not ids:
        return []
    p = odp_watki.c
    stmt = (select_mails(company_key().label("company_id"), p.id, p.rating, p.rating_by, p.rating_note, p.optout, p.awaiting_reply,
                         p.last_in_at, p.last_snippet, p.last_from, p.mailbox, maile.c.email.label("campaign_email"))
            .join(odp_watki, odp_watki.c.id == maile.c.id).where(HUMAN, in_companies(ids)).order_by(p.last_in_at.desc(), p.id))
    return [dict(row) for row in conn.execute(stmt).mappings()]


def lead_ids(conn, after: str, *, only_missing: bool) -> list:
    """Id aktywnych leadów (firm), rosnąco, od id większego niż `after`; `only_missing`: tylko niekompletne."""
    q = qualifying_query().subquery("q")
    clauses = [odp_leady.c.company_id > after, q.c.company_id.is_not(None)]
    if only_missing:
        clauses.append(completeness_expression() < len(lr.REQUIRED_FIELDS))
    stmt = sa.select(odp_leady.c.company_id).select_from(_joined(q)).where(*clauses).order_by(odp_leady.c.company_id)
    return [row[0] for row in conn.execute(stmt)]


def unsynced(conn) -> list:
    """Firmy z kwalifikującym się wątkiem, które nie mają jeszcze wiersza leada (id firmy, ostatnia odpowiedź)."""
    q = qualifying_query().subquery("q")
    stmt = (sa.select(q.c.company_id, q.c.last_in_at).select_from(q.outerjoin(odp_leady, odp_leady.c.company_id == q.c.company_id))
            .where(odp_leady.c.company_id.is_(None)).order_by(q.c.company_id))
    return [dict(row) for row in conn.execute(stmt).mappings()]


def pending_total(conn) -> int:
    """Aktywne leady, którym czegoś brakuje, razem z firmami bez wiersza leada (do licznika w menu, bez zapisu)."""
    q = qualifying_query().subquery("q")
    incomplete = sa.or_(odp_leady.c.company_id.is_(None), completeness_expression() < len(lr.REQUIRED_FIELDS))
    stmt = sa.select(sa.func.count()).select_from(q.outerjoin(odp_leady, odp_leady.c.company_id == q.c.company_id)).where(incomplete)
    return int(conn.execute(stmt).scalar() or 0)


def company_names(conn, ids: list) -> dict:
    """id firmy → (nazwa, strona) z `podmioty`."""
    stmt = sa.select(companies.c.id, companies.c.company, companies.c.website).where(companies.c.id.in_(ids))
    return {row[0]: (row[1] or "", row[2] or "") for row in conn.execute(stmt)}


def first_addresses(conn, ids: list) -> dict:
    """id firmy → adres e-mail jej pierwszego adresu (wg numeru, potem id): zapasowy adres leada."""
    stmt = (select_mails(company_key().label("company_id"), maile.c.email).where(in_companies(ids))
            .order_by(maile.c.position, maile.c.id))
    found: dict = {}
    for row in conn.execute(stmt):
        found.setdefault(row[0], row[1] or "")
    return found


def incoming_messages(conn, company_ids: Iterable[str], *, newest_first: bool = False) -> list:
    """Wiadomości człowieka od tych firm (z wątków kwalifikujących się albo nie): nadawca, data, treść."""
    ids = list(company_ids)
    if not ids:
        return []
    m = odp_wiadomosci.c
    order = (sa.func.coalesce(m.date_at, m.fetched_at).desc(), m.id.desc()) if newest_first else (sa.func.coalesce(m.date_at, m.fetched_at), m.id)
    stmt = (select_mails(company_key().label("company_id"), m.id, m.thread_id, m.from_addr, m.from_name, m.date_at, m.subject, m.body_text)
            .join(odp_watki, odp_watki.c.id == maile.c.id).join(odp_wiadomosci, odp_wiadomosci.c.thread_id == odp_watki.c.id)
            .where(in_companies(ids), m.direction == rp.DIRECTION_IN, m.kind == rp.KIND_HUMAN).order_by(*order))
    return [dict(row) for row in conn.execute(stmt).mappings()]


def company_addresses(conn, company_id: str) -> list:
    """Wszystkie adresy e-mail firmy z kolejki maili."""
    stmt = select_mails(maile.c.email).where(in_companies([company_id])).order_by(maile.c.position, maile.c.id)
    return [row[0] for row in conn.execute(stmt) if row[0]]


def thread_company(conn, thread_id: str) -> Optional[dict]:
    """Firma wątku i to, czy wątek kwalifikuje się na leada (None, gdy wątku nie ma)."""
    stmt = (select_mails(company_key().label("company_id"), sa.case((QUALIFYING, 1), else_=0).label("qualifies"))
            .join(odp_watki, odp_watki.c.id == maile.c.id).where(odp_watki.c.id == thread_id))
    row = conn.execute(stmt).mappings().first()
    return dict(row) if row else None
