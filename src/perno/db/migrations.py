"""Uaktualnianie schematu: wyłącznie dodawanie (tabela `podmioty`, opcjonalne kolumny NULL i indeksy), nigdy zmiany
destrukcyjne. Poprzednia wersja programu musi nadal działać na uaktualnionej tabeli."""

from __future__ import annotations

import sqlalchemy as sa
from sqlalchemy import exc as sa_exc
from sqlalchemy.engine import Engine
from sqlalchemy.schema import CreateIndex

from perno.db.errors import DbError
from perno.db.schema import companies, maile, metadata


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


def missing_indexes(engine: Engine) -> list:
    """Indeksy z definicji tabeli `maile`, których jeszcze nie ma w bazie; unikalne na końcu, bo ich założenie może się
    nie udać na danych z duplikatami (wtedy zwykłe indeksy już działają)."""
    inspector = sa.inspect(engine)
    if not inspector.has_table(maile.name):
        return []
    present = {index["name"] for index in inspector.get_indexes(maile.name)}
    missing = [index for index in maile.indexes if index.name not in present]
    return sorted(missing, key=lambda index: bool(index.unique))


def _create_companies(engine: Engine) -> list:
    """Zakłada tabelę `podmioty`, jeśli jej nie ma (`create_all` nie rusza tabel, które już są)."""
    if sa.inspect(engine).has_table(companies.name):
        return []
    companies.create(engine, checkfirst=True)
    return [companies.name]


def _add_columns(engine: Engine) -> list:
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


def _add_indexes(engine: Engine) -> list:
    added = []
    for index in missing_indexes(engine):
        try:
            with engine.begin() as conn:
                conn.execute(CreateIndex(index))
        except sa_exc.DBAPIError:
            if any(other.name == index.name for other in missing_indexes(engine)):  # nie założył go też inny proces
                raise
        added.append(index.name)
    return added


def upgrade_schema(engine: Engine) -> list:
    """Dodaje do istniejącej tabeli `maile` brakujące kolumny i indeksy oraz zakłada tabelę `podmioty` (idempotentnie,
    bez dotykania danych). Zwraca nazwy tego, co dodano.

    `create_all` nie zmienia tabel, które już istnieją, więc uaktualnienie wykonuje to jawnie. Wszystkie nowe kolumny
    są opcjonalne (NULL), więc istniejące wiersze zostają bez zmian, a starsza wersja programu nadal działa. Przypisanie
    wierszy do firm to osobny krok (`perno.db.adoption`)."""
    if not sa.inspect(engine).has_table(maile.name):
        return []
    return _create_companies(engine) + _add_columns(engine) + _add_indexes(engine)


def ensure_schema(engine: Engine) -> None:
    """Tworzy tabele, jeśli ich nie ma, i dodaje brakujące kolumny oraz indeksy."""
    metadata.create_all(engine)
    upgrade_schema(engine)
