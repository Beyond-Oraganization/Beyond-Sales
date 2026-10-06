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
    """Kolumna pola o nazwie z API: pola firmy z `podmioty`, pozostałe z `maile`."""
    return companies.c[name] if name in COMPANY_COLUMNS else maile.c[name]


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


def extra_columns() -> list:
    """Pola wyliczane (`schema.EXTRA_COLUMNS`) w kolejności, w jakiej dołączamy je do wiersza API."""
    return [company_key().label("company_id"), maile.c.position, mail_count_expression().label("mail_count"),
            hold_expression().label("hold")]


def company_key():
    """Id firmy maila: `company_id`, a dla nieprzejętego maila jego własne id (tak zostałaby założona jego firma)."""
    return sa.func.coalesce(maile.c.company_id, maile.c.id)
