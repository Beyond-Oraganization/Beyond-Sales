"""Wyjątki warstwy danych (komunikaty po polsku — trafiają do użytkownika)."""

from __future__ import annotations

from typing import Optional

from sqlalchemy import exc as sa_exc

DB_ERRORS = (sa_exc.OperationalError, sa_exc.InterfaceError)  # baza niedostępna / połączenie zerwane


class DbError(Exception):
    pass


class DbUnavailable(DbError):
    pass


class RowNotFound(DbError):
    pass


class InvalidValue(DbError):
    pass


class EmptyDatabase(DbError):
    pass


class RowLocked(DbError):
    def __init__(self, message: str, row: Optional[dict] = None):
        super().__init__(message)
        self.row = row
