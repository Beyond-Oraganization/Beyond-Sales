"""Warstwa danych: tabela `maile` w MySQL (SQLAlchemy Core + PyMySQL).

Moduły: `schema` (definicja tabeli), `codec` (wiersz bazy ↔ JSON panelu), `engine` (adres i silnik), `migrations`
(dodawanie kolumn), `queries` (kolejność i filtry w SQL), `mail_repository` (operacje na wierszach), `database`
(fasada `Database`), `errors`. Ten pakiet wystawia to, czego używa reszta programu.

Diagnostyka z terminala:  python3 -m perno.db --check
"""

from __future__ import annotations

from perno.db.codec import format_value, from_api, to_api
from perno.db.database import Database
from perno.db.engine import build_url, make_engine
from perno.db.errors import DB_ERRORS, DbError, DbUnavailable, EmptyDatabase, InvalidValue, RowLocked, RowNotFound
from perno.db.mail_repository import STALE_CLAIM_MESSAGE
from perno.db.migrations import add_column_statements
from perno.db.queries import ID_CHUNK
from perno.db.schema import COLUMNS, LEGACY_COLUMNS, LIGHT_COLUMNS, PROFILE_COLUMNS, maile, metadata

__all__ = [
    "COLUMNS", "DB_ERRORS", "ID_CHUNK", "LEGACY_COLUMNS", "LIGHT_COLUMNS", "PROFILE_COLUMNS", "STALE_CLAIM_MESSAGE",
    "Database", "DbError", "DbUnavailable", "EmptyDatabase", "InvalidValue", "RowLocked", "RowNotFound",
    "add_column_statements", "build_url", "format_value", "from_api", "maile", "make_engine", "metadata", "to_api",
]
