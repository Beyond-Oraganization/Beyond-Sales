"""Błędy magazynu skrapera (podklasy `DbError`, więc obsługują je te same granice co błędy bazy maili)."""

from __future__ import annotations

from perno.db import DbError
from perno.scraper import statuses as st


class StoreError(DbError):
    pass


class RunActive(StoreError):
    pass


class LeaseLost(StoreError):
    pass


class FirmaBusy(StoreError):
    pass


class DomainTaken(StoreError):
    pass


class InvalidFirma(StoreError):
    def __init__(self, code: str, message: str = ""):
        super().__init__(message or st.PRZYCZYNY.get(code, code))
        self.code = code


class NotFound(StoreError):
    pass
