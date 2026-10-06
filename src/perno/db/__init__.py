"""Warstwa danych: tabele `maile` (adresy z treścią) i `podmioty` (firmy) w MySQL (SQLAlchemy Core + PyMySQL).

Moduły: `schema` (definicje tabel), `codec` (wiersz bazy ↔ JSON panelu), `engine` (adres i silnik), `migrations`
(dodawanie tabeli, kolumn i indeksów), `adoption` (przypisanie maili do firm), `queries` (kolejność i filtry w SQL), `mail_repository` (operacje na wierszach), `database`
(fasada `Database`), `errors`. Ten pakiet wystawia to, czego używa reszta programu.

Diagnostyka z terminala:  python3 -m perno.db --check
"""

from __future__ import annotations

from perno.db.adoption import NotAdopted
from perno.db.codec import format_value, from_api, to_api
from perno.db.database import Database
from perno.db.engine import build_url, make_engine
from perno.db.errors import DB_ERRORS, DbError, DbUnavailable, EmptyDatabase, InvalidValue, RowLocked, RowNotFound
from perno.db.mail_repository import STALE_CLAIM_MESSAGE
from perno.db.migrations import add_column_statements
from perno.db.queries import ID_CHUNK
from perno.db.schema import (
    COLUMNS,
    COMPANY_COLUMNS,
    LEGACY_COLUMNS,
    LIGHT_COLUMNS,
    LINK_COLUMNS,
    MAIL_COLUMNS,
    PROFILE_COLUMNS,
    companies,
    legacy_key,
    maile,
    metadata,
)

__all__ = [
    "COLUMNS", "COMPANY_COLUMNS", "DB_ERRORS", "ID_CHUNK", "LEGACY_COLUMNS", "LIGHT_COLUMNS", "LINK_COLUMNS",
    "MAIL_COLUMNS", "PROFILE_COLUMNS", "STALE_CLAIM_MESSAGE", "Database", "DbError", "DbUnavailable", "EmptyDatabase",
    "InvalidValue", "NotAdopted", "RowLocked", "RowNotFound", "add_column_statements", "build_url", "companies",
    "format_value", "from_api", "legacy_key", "maile", "make_engine", "metadata", "to_api",
]
