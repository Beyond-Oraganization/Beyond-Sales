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
    not_statuses: tuple = ()  # statusy, których NIE chcemy (np. „Wysyłanie…”, którym zarządza program, nie da się zaznaczyć)
    countries: tuple = ()  # kody ISO; NO_COUNTRY = bez kraju, FOREIGN_COUNTRY = spoza Polski (można łączyć z kodami)
    has_email: Optional[bool] = None
    has_body: Optional[bool] = None
    has_city: Optional[bool] = None
    has_address: Optional[bool] = None
    has_sent: Optional[bool] = None  # czy jest data wysłania
    multi: Optional[bool] = None  # czy firma ma więcej niż jeden adres
    held: Optional[bool] = None  # czy adres jest wstrzymany przez status innego adresu firmy (`statuses.COMPANY_STOPS`)
    origin: str = ""  # jedna z ORIGINS; pusty = bez ograniczenia
    sent_from: Optional[date] = None  # dni kalendarzowe, oba końce włącznie
    sent_to: Optional[date] = None
    generated_from: Optional[date] = None
    generated_to: Optional[date] = None
    search: Optional[SearchTerms] = None  # wyszukiwarka (słowa, „frazy”, -wykluczenia) w wybranym polu
    ready: bool = False  # tylko firmy gotowe do wysyłki: status „Do wysłania”/„Błąd”, jest e-mail i treść maila (i adres nie jest wstrzymany)
    unheld: bool = False  # bez adresów wstrzymanych przez status innego adresu tej firmy (`statuses.COMPANY_STOPS`)
    companies: Optional[tuple] = None  # tylko adresy tych firm (id firm); None = bez ograniczenia, () = żaden
    exclude_companies: tuple = ()  # id firm, których adresów nie chcemy (np. firmy, do których wysłano już dziś: odstęp)
    # Jawna lista wierszy. None = bez ograniczenia, () = żaden. Wynik idzie w kolejności `ids` (nie w domyślnej),
    # więc plik może wyglądać tak jak lista na ekranie; id, których nie ma w bazie, są pomijane.
    ids: Optional[tuple] = None


def country_selected(selected: tuple, key: str) -> bool:
    """Czy kraj (kod albo `NO_COUNTRY`) mieści się w wyborze z filtra krajów; pusty wybór = każdy. To samo, co warunek SQL
    `db.queries._country_clause`, tylko dla pojedynczej wartości (liczniki grupują wiersze w bazie, a wybór stosują w pamięci)."""
    if not selected:
        return True
    if key in selected:
        return True
    return FOREIGN_COUNTRY in selected and key not in (NO_COUNTRY, HOME_COUNTRY)


ALL_ROWS = RowFilter()  # bez ograniczeń; niezmienny, więc bezpieczny jako wartość domyślna argumentów
