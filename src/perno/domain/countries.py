"""Kraje firm: kod ISO 3166-1 alpha-2 jako wartość w bazie, nazwa po polsku i flaga do wyświetlania.

W bazie (`maile.country`) zawsze jest kod ("PL", "GB", "DE"…), bo kod nie zależy od języka ani pisowni.
Wartości wpisywane ręcznie lub z arkuszy ("Polska", "UK", "Wielka Brytania", "pl") sprowadza do kodu
`normalize_country`. Lista jest celowo skończona — nieznany kraj to błąd, a nie cicho zapisana literówka;
nowy kraj dopisujesz jedną linijką w `_COUNTRIES`.
"""

from __future__ import annotations

import re
import unicodedata
from typing import Optional

# kod, nazwa po polsku, nazwa po angielsku
_COUNTRIES = (
    ("PL", "Polska", "Poland"),
    ("GB", "Wielka Brytania", "United Kingdom"),
    ("IE", "Irlandia", "Ireland"),
    ("DE", "Niemcy", "Germany"),
    ("AT", "Austria", "Austria"),
    ("CH", "Szwajcaria", "Switzerland"),
    ("LI", "Liechtenstein", "Liechtenstein"),
    ("FR", "Francja", "France"),
    ("BE", "Belgia", "Belgium"),
    ("NL", "Holandia", "Netherlands"),
    ("LU", "Luksemburg", "Luxembourg"),
    ("DK", "Dania", "Denmark"),
    ("SE", "Szwecja", "Sweden"),
    ("NO", "Norwegia", "Norway"),
    ("FI", "Finlandia", "Finland"),
    ("IS", "Islandia", "Iceland"),
    ("EE", "Estonia", "Estonia"),
    ("LV", "Łotwa", "Latvia"),
    ("LT", "Litwa", "Lithuania"),
    ("CZ", "Czechy", "Czech Republic"),
    ("SK", "Słowacja", "Slovakia"),
    ("HU", "Węgry", "Hungary"),
    ("RO", "Rumunia", "Romania"),
    ("BG", "Bułgaria", "Bulgaria"),
    ("HR", "Chorwacja", "Croatia"),
    ("SI", "Słowenia", "Slovenia"),
    ("RS", "Serbia", "Serbia"),
    ("BA", "Bośnia i Hercegowina", "Bosnia and Herzegovina"),
    ("ME", "Czarnogóra", "Montenegro"),
    ("MK", "Macedonia Północna", "North Macedonia"),
    ("AL", "Albania", "Albania"),
    ("GR", "Grecja", "Greece"),
    ("CY", "Cypr", "Cyprus"),
    ("MT", "Malta", "Malta"),
    ("IT", "Włochy", "Italy"),
    ("ES", "Hiszpania", "Spain"),
    ("PT", "Portugalia", "Portugal"),
    ("AD", "Andora", "Andorra"),
    ("UA", "Ukraina", "Ukraine"),
    ("BY", "Białoruś", "Belarus"),
    ("MD", "Mołdawia", "Moldova"),
    ("GE", "Gruzja", "Georgia"),
    ("TR", "Turcja", "Turkey"),
    ("US", "USA", "United States"),
    ("CA", "Kanada", "Canada"),
    ("MX", "Meksyk", "Mexico"),
    ("BR", "Brazylia", "Brazil"),
    ("AU", "Australia", "Australia"),
    ("NZ", "Nowa Zelandia", "New Zealand"),
    ("IN", "Indie", "India"),
    ("CN", "Chiny", "China"),
    ("JP", "Japonia", "Japan"),
    ("AE", "Zjednoczone Emiraty Arabskie", "United Arab Emirates"),
    ("IL", "Izrael", "Israel"),
    ("MA", "Maroko", "Morocco"),
    ("EG", "Egipt", "Egypt"),
    ("ZA", "RPA", "South Africa"),
)

# dodatkowe zapisy spotykane w arkuszach i ręcznie wpisywane
_ALIASES = {
    "GB": ("uk", "u k", "great britain", "britain", "gb", "anglia", "england", "szkocja", "scotland", "walia", "wales",
           "irlandia polnocna", "northern ireland", "zjednoczone krolestwo", "wielka brytania i irlandia polnocna"),
    "US": ("usa", "u s a", "us", "stany zjednoczone", "stany zjednoczone ameryki", "united states of america", "ameryka"),
    "CZ": ("czechia", "republika czeska"),
    "NL": ("niderlandy", "the netherlands", "holland"),
    "DE": ("deutschland", "rfn", "niemcy"),
    "AT": ("osterreich",),
    "PL": ("polen", "rzeczpospolita polska", "rp"),
    "AE": ("zea", "emiraty arabskie", "uae"),
    "ZA": ("republika poludniowej afryki", "afryka poludniowa"),
    "MK": ("macedonia",),
    "CH": ("schweiz",),
}

COUNTRY_NAMES = {code: pl for code, pl, _ in _COUNTRIES}


def _fold(text: str) -> str:
    """Małe litery, bez polskich i innych znaków diakrytycznych, tylko litery i cyfry rozdzielone spacją."""
    text = unicodedata.normalize("NFKD", str(text).strip().lower().replace("ł", "l"))
    text = "".join(ch for ch in text if not unicodedata.combining(ch))
    return re.sub(r"[^a-z0-9]+", " ", text).strip()


def _build_lookup() -> dict:
    lookup = {}
    for code, polish, english in _COUNTRIES:
        for name in (polish, english):
            lookup[_fold(name)] = code
    for code, names in _ALIASES.items():
        for name in names:
            lookup[_fold(name)] = code
    return lookup


_LOOKUP = _build_lookup()


def normalize_country(value: object) -> Optional[str]:
    """Kod ISO z nazwy lub kodu ("Polska", "poland", "UK", "de"…). Nieznane lub puste → None."""
    if value is None:
        return None
    text = str(value).strip()
    if not text:
        return None
    if len(text) == 2 and text.isalpha() and text.upper() in COUNTRY_NAMES:
        return text.upper()
    return _LOOKUP.get(_fold(text))


def country_name(code: Optional[str]) -> str:
    """Nazwa po polsku dla kodu; pusta, gdy kod jest pusty lub nieznany."""
    return COUNTRY_NAMES.get(str(code or "").strip().upper(), "")


def flag(code: Optional[str]) -> str:
    """Flaga jako emoji (dwa wskaźniki regionalne); pusta dla nieznanego kodu."""
    code = str(code or "").strip().upper()
    if code not in COUNTRY_NAMES:
        return ""
    return "".join(chr(0x1F1E6 + ord(letter) - ord("A")) for letter in code)


def choices() -> list:
    """[(kod, nazwa, flaga)] do list rozwijanych: Polska pierwsza, reszta alfabetycznie po polsku."""
    rest = sorted((item for item in _COUNTRIES if item[0] != "PL"), key=lambda item: _fold(item[1]))
    ordered = [item for item in _COUNTRIES if item[0] == "PL"] + rest
    return [(code, polish, flag(code)) for code, polish, _ in ordered]
