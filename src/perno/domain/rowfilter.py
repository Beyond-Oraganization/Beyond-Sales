"""Filtr wierszy tabeli maile — sama specyfikacja (dane); zamianę na SQL robi `perno.db`."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date
from typing import Optional

NO_COUNTRY = "_none"  # w filtrze krajów: firmy, którym nie przypisano kraju


@dataclass(frozen=True)
class RowFilter:
    """Które wiersze wybrać. Puste pole = bez ograniczenia; wszystkie warunki łączą się przez AND."""
    segments: tuple = ()
    statuses: tuple = ()
    countries: tuple = ()  # kody ISO; NO_COUNTRY = bez kraju (można łączyć z kodami)
    has_email: Optional[bool] = None
    has_body: Optional[bool] = None
    sent_from: Optional[date] = None  # dni kalendarzowe, oba końce włącznie
    sent_to: Optional[date] = None
    # Jawna lista wierszy. None = bez ograniczenia, () = żaden. Wynik idzie w kolejności `ids` (nie w domyślnej),
    # więc plik może wyglądać tak jak lista na ekranie; id, których nie ma w bazie, są pomijane.
    ids: Optional[tuple] = None


ALL_ROWS = RowFilter()  # bez ograniczeń; niezmienny, więc bezpieczny jako wartość domyślna argumentów
