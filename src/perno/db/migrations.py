"""Uaktualnianie schematu tabeli `maile`: wyłącznie dodawanie opcjonalnych kolumn (NULL), nigdy zmiany destrukcyjne."""

from __future__ import annotations

import sqlalchemy as sa
from sqlalchemy import exc as sa_exc
from sqlalchemy.engine import Engine

from perno.db.errors import DbError
from perno.db.schema import maile, metadata


def add_column_statements(dialect, columns) -> list:
    """Polecenia ALTER TABLE dla brakujących kolumn. MySQL: jedno polecenie na wszystkie kolumny (atomowo);
    sqlite przyjmuje tylko jedną kolumnę na polecenie. Obsługujemy wyłącznie kolumny opcjonalne."""
    quote = dialect.identifier_preparer.quote
    parts = []
    for column in columns:
        if not column.nullable or column.primary_key:
            raise DbError(f"Kolumny {column.name} nie da się dodać automatycznie (wymagana wartość) — zrób to ręcznie.")
        parts.append(f"ADD COLUMN {quote(column.name)} {column.type.compile(dialect=dialect)} NULL")
    table = quote(maile.name)
    if dialect.name == "sqlite":
        return [f"ALTER TABLE {table} {part}" for part in parts]
    return [f"ALTER TABLE {table} " + ", ".join(parts)]


def missing_columns(engine: Engine) -> list:
    """Kolumny z definicji tabeli, których nie ma jeszcze w istniejącej tabeli `maile` (pusta lista, gdy tabeli brak)."""
    inspector = sa.inspect(engine)
    if not inspector.has_table(maile.name):
        return []
    present = {column["name"] for column in inspector.get_columns(maile.name)}
    return [column for column in maile.columns if column.name not in present]


def upgrade_schema(engine: Engine) -> list:
    """Dodaje do istniejącej tabeli brakujące kolumny (idempotentnie, bez dotykania danych). Zwraca nazwy dodanych.

    `create_all` nie zmienia tabel, które już istnieją, więc uaktualnienie wykonuje to jawnie. Wszystkie nowe
    kolumny są opcjonalne (NULL), więc istniejące wiersze zostają bez zmian, a starsza wersja programu nadal działa."""
    missing = missing_columns(engine)
    if not missing:
        return []
    statements = add_column_statements(engine.dialect, missing)
    try:
        with engine.begin() as conn:
            for statement in statements:
                conn.execute(sa.text(statement))
    except sa_exc.DBAPIError:
        if missing_columns(engine):  # inny proces mógł dodać kolumny w międzyczasie — wtedy to nie błąd
            raise
    return [column.name for column in missing]


def ensure_schema(engine: Engine) -> None:
    """Tworzy tabelę, jeśli jej nie ma, i dodaje brakujące kolumny."""
    metadata.create_all(engine)
    upgrade_schema(engine)
