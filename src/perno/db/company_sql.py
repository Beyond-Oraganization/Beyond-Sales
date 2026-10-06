"""Wspólny kształt zapytań o maile: każdy odczyt idzie przez złączenie `maile` → `podmioty`.

Pola firmy (segment, nazwa, kraj, miasto…) leżą w `podmioty`, pola adresu i wysyłki w `maile`. Złączenie jest zewnętrzne,
żeby mail bez firmy (wstawiony przez starszy kod, jeszcze nieprzejęty) był na liście widoczny, a nie znikał. Zapytanie,
które odwołuje się do obu tabel bez tego złączenia, zrobiłoby iloczyn kartezjański — dlatego testy traktują ostrzeżenie
SQLAlchemy o nim jako błąd.
"""

from __future__ import annotations

import sqlalchemy as sa

from perno.db.schema import COMPANY_COLUMNS, companies, maile
from perno.domain import statuses as st

mail_company_join = maile.join(companies, companies.c.id == maile.c.company_id, isouter=True)


def column_for(name: str):
    """Kolumna pola o nazwie z API: pola firmy z `podmioty`, pola wyliczane (`schema.EXTRA_COLUMNS`) jako wyrażenia, pozostałe
    z `maile`."""
    if name in COMPANY_COLUMNS:
        return companies.c[name]
    computed = {"company_id": company_key, "mail_count": mail_count_expression, "hold": hold_expression}
    if name in computed:
        return computed[name]().label(name)
    return maile.c[name]


def select_mails(*columns):
    """`SELECT <columns>` z maili z dołączoną firmą: jedyny sposób czytania pól firmy i adresu w jednym zapytaniu."""
    return sa.select(*columns).select_from(mail_company_join)


def mail_count_expression():
    """Ile adresów ma firma tego maila (mail bez firmy liczy się jako jedyny)."""
    sibling = maile.alias("sibling_count")
    count = (sa.select(sa.func.count()).select_from(sibling).where(sibling.c.company_id == maile.c.company_id)
             .correlate(maile).scalar_subquery())
    return sa.case((maile.c.company_id.is_(None), 1), else_=count)


def hold_expression():
    """Status innego adresu tej firmy z `COMPANY_STOPS` (ważniejszy pierwszy) albo pusty tekst: ten adres jest wtedy
    wstrzymany. Własnego statusu wiersza nie liczymy."""
    sibling = maile.alias("sibling_hold")
    rank = sa.case({status: i for i, status in enumerate(st.COMPANY_STOPS)}, value=sibling.c.status, else_=len(st.COMPANY_STOPS))
    stop = (sa.select(sibling.c.status)
            .where(sibling.c.company_id == maile.c.company_id, sibling.c.id != maile.c.id,
                   sibling.c.status.in_(st.COMPANY_STOPS))
            .order_by(rank).limit(1).correlate(maile).scalar_subquery())
    return sa.func.coalesce(stop, "")


def has_sibling():
    """Warunek: firma tego maila ma jeszcze inny adres."""
    other = maile.alias("sibling_other")
    return sa.exists().where(other.c.company_id == maile.c.company_id, other.c.id != maile.c.id).correlate(maile)


def no_holding_sibling():
    """Warunek: żaden INNY adres tej firmy nie ma statusu z `COMPANY_STOPS` (adres nie jest wstrzymany). Skorelowane
    `NOT EXISTS` — MySQL robi z tego anti-join po indeksie `(company_id, status)`."""
    sibling = maile.alias("sibling_stop")
    return ~sa.exists().where(sibling.c.company_id == maile.c.company_id, sibling.c.id != maile.c.id,
                              sibling.c.status.in_(st.COMPANY_STOPS)).correlate(maile)


def first_of_company(statuses: tuple):
    """Warunek: ten adres jest pierwszym (wg numeru, potem id) spośród adresów firmy w podanych statusach — z każdej firmy
    wybieramy jeden adres naraz."""
    earlier = maile.alias("earlier_mail")
    before = sa.or_(earlier.c.position < maile.c.position,
                    sa.and_(earlier.c.position == maile.c.position, earlier.c.id < maile.c.id))
    return ~sa.exists().where(earlier.c.company_id == maile.c.company_id, earlier.c.id != maile.c.id,
                              earlier.c.status.in_(statuses), before).correlate(maile)


def extra_columns() -> list:
    """Pola wyliczane (`schema.EXTRA_COLUMNS`) w kolejności, w jakiej dołączamy je do wiersza API."""
    return [company_key().label("company_id"), maile.c.position, mail_count_expression().label("mail_count"),
            hold_expression().label("hold")]


def company_key():
    """Id firmy maila: `company_id`, a dla nieprzejętego maila jego własne id (tak zostałaby założona jego firma)."""
    return sa.func.coalesce(maile.c.company_id, maile.c.id)
