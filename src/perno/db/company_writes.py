"""Zapisy pól firmy: jedno miejsce prawdy w `podmioty` i lustro w starych kolumnach `maile` dla wszystkich adresów firmy.

Lustro istnieje, bo poprzednia wersja programu (panel, demon Automatu) nadal czyta kolumny firmowe z `maile`, a wycofanie
zmian musi działać. Kolejność blokad wszędzie ta sama: najpierw firma (`lock_company`), potem jej maile rosnąco po id;
dzięki temu dwa równoległe zapisy nie zakleszczą się nawzajem.
"""

from __future__ import annotations

from datetime import datetime
from typing import Iterable, Mapping, Sequence

import sqlalchemy as sa
from sqlalchemy.engine import Connection

from perno.db.queries import ID_CHUNK, id_chunks
from perno.db.schema import COMPANY_COLUMNS, companies, legacy_key, maile


def split_values(values: Mapping) -> tuple:
    """`(pola maila, pola firmy)` z jednego słownika zmian (np. status i kraj w tej samej edycji)."""
    mail = {name: value for name, value in values.items() if name not in COMPANY_COLUMNS}
    company = {name: value for name, value in values.items() if name in COMPANY_COLUMNS}
    return mail, company


def mirror(values: Mapping) -> dict:
    """Pola firmy w postaci do zapisu w starych kolumnach `maile` (pod ich kluczami w kodzie)."""
    return {legacy_key(name): values[name] for name in COMPANY_COLUMNS if name in values}


def lock_company(conn: Connection, company_id: str) -> None:
    """Zakłada blokadę zapisu na firmie do końca transakcji. Sam `SELECT … FOR UPDATE` nie blokuje na sqlite, a zapis
    „bez zmiany” blokuje na obu bazach (MySQL: blokada wiersza, sqlite: blokada zapisu na plik)."""
    conn.execute(sa.update(companies).where(companies.c.id == company_id).values(updated_at=companies.c.updated_at))


def company_ids_of(conn: Connection, mail_ids: Iterable[str]) -> dict:
    """`{id maila: id firmy}`; mail bez firmy (nieprzejęty) ma firmę o własnym id, bo tak by ją założono."""
    found: dict = {}
    for part in id_chunks(mail_ids):
        stmt = sa.select(maile.c.id, maile.c.company_id).where(maile.c.id.in_(part))
        found.update({row_id: company_id or row_id for row_id, company_id in conn.execute(stmt)})
    return found


def write_company_values(conn: Connection, company_ids: Sequence[str], values: Mapping, now: datetime) -> None:
    """Zapisuje pola firmy w `podmioty` i w lustrze `maile` (wszystkie adresy tych firm); partiami po `ID_CHUNK`."""
    if not values:
        return
    for start in range(0, len(company_ids), ID_CHUNK):
        part = list(company_ids[start:start + ID_CHUNK])
        conn.execute(sa.update(companies).where(companies.c.id.in_(part)).values(**values, updated_at=now))
        conn.execute(sa.update(maile).where(maile.c.company_id.in_(part)).values(**mirror(values)))


def insert_new_companies(conn: Connection, rows: Sequence[Mapping], now: datetime) -> None:
    """Wstawia firmy i ich adresy główne z płaskich wierszy (`from_api`): firma ma id wiersza, adres numer 1. Wiersz
    w `maile` zachowuje wszystkie pola, więc stare kolumny firmowe są od razu spójne z firmą."""
    if not rows:
        return
    conn.execute(sa.insert(companies), [
        {"id": row["id"], **{name: row.get(name) for name in COMPANY_COLUMNS}, "updated_at": now} for row in rows])
    mail_rows = [{**{name: value for name, value in row.items() if name not in COMPANY_COLUMNS}, **mirror(row),
                  "company_id": row["id"], "position": 1, "updated_at": now} for row in rows]
    conn.execute(sa.insert(maile), mail_rows)
