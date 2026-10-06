"""Wspólny kształt zapytań o maile: każdy odczyt idzie przez złączenie `maile` → `podmioty`.

Pola firmy (segment, nazwa, kraj, miasto…) leżą w `podmioty`, pola adresu i wysyłki w `maile`. Złączenie jest zewnętrzne,
żeby mail bez firmy (wstawiony przez starszy kod, jeszcze nieprzejęty) był na liście widoczny, a nie znikał. Zapytanie,
które odwołuje się do obu tabel bez tego złączenia, zrobiłoby iloczyn kartezjański — dlatego testy traktują ostrzeżenie
SQLAlchemy o nim jako błąd.
"""

from __future__ import annotations

import sqlalchemy as sa

from perno.db.schema import COMPANY_COLUMNS, companies, maile

mail_company_join = maile.join(companies, companies.c.id == maile.c.company_id, isouter=True)


def column_for(name: str):
    """Kolumna pola o nazwie z API: pola firmy z `podmioty`, pozostałe z `maile`."""
    return companies.c[name] if name in COMPANY_COLUMNS else maile.c[name]


def select_mails(*columns):
    """`SELECT <columns>` z maili z dołączoną firmą: jedyny sposób czytania pól firmy i adresu w jednym zapytaniu."""
    return sa.select(*columns).select_from(mail_company_join)


def company_key():
    """Id firmy maila: `company_id`, a dla nieprzejętego maila jego własne id (tak zostałaby założona jego firma)."""
    return sa.func.coalesce(maile.c.company_id, maile.c.id)
