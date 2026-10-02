"""Filtr wierszy tabeli maile — sama specyfikacja (dane); zamianę na SQL robi `perno.db`."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date
from typing import Optional

from perno.domain.search import SearchTerms

NO_COUNTRY = "_none"  # w filtrze krajów: firmy, którym nie przypisano kraju
FOREIGN_COUNTRY = "_foreign"  # w filtrze krajów: firmy z krajem innym niż Polska (kod HOME_COUNTRY)
HOME_COUNTRY = "PL"
ORIGINS = ("", "dossier", "import")  # skąd firma: z dossier (id `eventy-051`) albo z importu CSV (id `eventy-n001`)


@dataclass(frozen=True)
class RowFilter:
    """Które wiersze wybrać. Puste pole = bez ograniczenia; wszystkie warunki łączą się przez AND."""
    segments: tuple = ()
    statuses: tuple = ()
    countries: tuple = ()  # kody ISO; NO_COUNTRY = bez kraju, FOREIGN_COUNTRY = spoza Polski (można łączyć z kodami)
    has_email: Optional[bool] = None
    has_body: Optional[bool] = None
    has_city: Optional[bool] = None
    has_address: Optional[bool] = None
    has_sent: Optional[bool] = None  # czy jest data wysłania
    origin: str = ""  # jedna z ORIGINS; pusty = bez ograniczenia
    sent_from: Optional[date] = None  # dni kalendarzowe, oba końce włącznie
    sent_to: Optional[date] = None
    generated_from: Optional[date] = None
    generated_to: Optional[date] = None
    search: Optional[SearchTerms] = None  # wyszukiwarka (słowa, „frazy”, -wykluczenia) w wybranym polu
    # Jawna lista wierszy. None = bez ograniczenia, () = żaden. Wynik idzie w kolejności `ids` (nie w domyślnej),
    # więc plik może wyglądać tak jak lista na ekranie; id, których nie ma w bazie, są pomijane.
    ids: Optional[tuple] = None


ALL_ROWS = RowFilter()  # bez ograniczeń; niezmienny, więc bezpieczny jako wartość domyślna argumentów
