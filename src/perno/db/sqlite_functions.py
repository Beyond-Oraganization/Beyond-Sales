"""Funkcje i kolacja, które sqlite nie ma, a wyszukiwarka i sortowanie panelu ich potrzebują (MySQL ma to w kolacjach).

Rejestrowane na każdym nowym połączeniu sqlite (baza demo i testy). Na MySQL nic się tu nie dzieje.
"""

from __future__ import annotations

from typing import Any

from perno.domain.search import fold, pl_sort_key

FOLD_FUNCTION = "perno_fold"
PL_COLLATION = "perno_pl"


def _compare_polish(left: str, right: str) -> int:
    a, b = pl_sort_key(left), pl_sort_key(right)
    return (a > b) - (a < b)


def register(dbapi_connection: Any, _record: Any = None) -> None:
    """Zdarzenie `connect` SQLAlchemy: dodaje `perno_fold(tekst)` i kolację `perno_pl` do nowego połączenia."""
    dbapi_connection.create_function(FOLD_FUNCTION, 1, fold, deterministic=True)
    dbapi_connection.create_collation(PL_COLLATION, _compare_polish)
