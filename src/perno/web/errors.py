"""Zamiana wyjątków na odpowiedzi HTTP — jedno miejsce dla całej aplikacji.

Handlery stron tylko rzucają wyjątki (domenowe, usług i magazynów); tutaj ustalamy, jaki to kod i komunikat.
Nieznany wyjątek (`None`) obsługuje rdzeń aplikacji jako błąd 500.
"""

from __future__ import annotations

from typing import Optional

from perno.db import EmptyDatabase, InvalidValue, RowLocked, RowNotFound
from perno.scraper.settings import SettingsError
from perno.scraper.store import DomainTaken, FirmaBusy, InvalidFirma, NotFound, RunActive
from perno.services.bulk import AckRequired
from perno.services.sending import DailyCapReached, NotSendable
from perno.settings import ConfigError
from perno.web.http import HttpError, Response, error_response

# (typy wyjątków, kod HTTP): komunikatem jest tekst wyjątku
STATUS_BY_ERROR: tuple = (
    ((RowNotFound, NotFound), 404),
    ((EmptyDatabase, RunActive, FirmaBusy, DomainTaken), 409),
    ((InvalidValue, ConfigError, InvalidFirma, SettingsError, NotSendable), 400),
    ((DailyCapReached,), 429),
)


def to_response(exc: Exception) -> Optional[Response]:
    """Odpowiedź dla znanego wyjątku albo `None`."""
    if isinstance(exc, HttpError):
        return error_response(exc.status, exc.message, **exc.extra)
    if isinstance(exc, RowLocked):
        return error_response(409, str(exc), row=exc.row)
    if isinstance(exc, AckRequired):
        return error_response(409, str(exc), **exc.plan.to_json())
    for types, status in STATUS_BY_ERROR:
        if isinstance(exc, types):
            return error_response(status, str(exc))
    return None
