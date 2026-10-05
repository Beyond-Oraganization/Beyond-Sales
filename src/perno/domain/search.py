"""Wyszukiwarka firm: składanie liter i zapytanie („fraza”, -wyklucz) oraz polska kolejność sortowania.

Jedna zasada dla panelu i eksportu. Dawniej żyła tylko w przeglądarce (`filters.js`); od przejścia na listę po stronie
serwera liczy ją serwer, a wektory z `tests/listing_cases.json` (wygenerowane z tamtej implementacji) pilnują, żeby
zachowanie się nie zmieniło. `countries._fold` robi coś innego (sprowadza nazwę kraju do liter i cyfr, żeby rozpoznać go
po nazwie) i celowo go tu nie używamy.
"""

from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass
from typing import Optional

SEARCH_FIELDS = ("all", "company", "email", "website", "place", "description")

# Litery, które po złożeniu liter nie są „a z ogonkiem”, tylko osobną literą: NFD ich nie rozkłada albo robi to inaczej niż
# oczekują ludzie szukający po polsku/niemiecku/duńsku. Tabela jest taka sama jak w dawnym `fold()` w przeglądarce.
_EXTRA_FOLDS = {"ł": "l", "ø": "o", "ß": "ss", "æ": "ae", "œ": "oe", "đ": "d", "ð": "d", "þ": "th", "ı": "i"}
_EXTRA_TABLE = str.maketrans(_EXTRA_FOLDS)
_TOKEN = re.compile(r'(-?)"([^"]*)"?|(-?)([^\s"]+)')


@dataclass(frozen=True)
class SearchTerms:
    """Zapytanie po rozbiciu: wszystkie słowa z `include` muszą pasować, żadne z `exclude` nie może; `field` mówi, gdzie
    szukać (`SEARCH_FIELDS`). Słowa są już złożone (`fold`)."""
    include: tuple = ()
    exclude: tuple = ()
    field: str = "all"

    def __bool__(self) -> bool:
        return bool(self.include or self.exclude)


def fold(text: object) -> str:
    """Małe litery, bez znaków diakrytycznych (ą→a, ü→u, ß→ss…), pojedyncze spacje; `None` to pusty tekst."""
    lowered = unicodedata.normalize("NFD", "" if text is None else str(text).lower())
    stripped = "".join(ch for ch in lowered if not "\u0300" <= ch <= "\u036f").translate(_EXTRA_TABLE)
    return " ".join(stripped.split())


def parse_query(text: object, field: str = "all") -> SearchTerms:
    """„ala ma” kota -pies -"stary kot" → include: [ala ma, kota], exclude: [pies, stary kot].
    Niedomknięty cudzysłów obejmuje resztę tekstu; sam „-” jest ignorowany."""
    include: list = []
    exclude: list = []
    for match in _TOKEN.finditer("" if text is None else str(text)):
        quoted = match.group(2) is not None
        negative = (match.group(1) if quoted else match.group(3)) == "-"
        term = fold(match.group(2) if quoted else match.group(4))
        if not term or term == "-":
            continue
        (exclude if negative else include).append(term)
    return SearchTerms(tuple(include), tuple(exclude), field if field in SEARCH_FIELDS else "all")


# Alfabet polski: a ą b c ć d e ę f g h i j k l ł m n ń o ó p q r s ś t u v w x y z ź ż — tak sortuje kolacja
# `utf8mb4_pl_0900_ai_ci` w MySQL i `Intl.Collator('pl')` w przeglądarce; ten klucz daje tę samą kolejność w sqlite i dla
# nazw krajów. Litery spoza alfabetu (ü, é…) liczą się jak ich podstawa (u, e…). Jak w UCA: spacje i znaki idą przed
# cyframi, cyfry przed literami. Cyfry porównujemy znak po znaku („Firma 10” przed „Firma 2”, jak w MySQL), a nie jako liczby.
_PL_ORDER = {ch: index for index, ch in enumerate("aąbcćdeęfghijklłmnńoópqrsśtuvwxyzźżþ")}  # þ na końcu, jak w ICU i MySQL
_PL_LETTERS = frozenset("ąćęłńóśźż")
_SORT_FOLDS = {ch: to for ch, to in _EXTRA_FOLDS.items() if ch not in _PL_ORDER}
_SYMBOLS_FIRST, _DIGITS_NEXT = -2_000_000, -1_000_000


def pl_sort_key(text: Optional[str]) -> tuple:
    """Klucz porównania tekstów po polsku, bez rozróżniania wielkości liter i akcentów spoza alfabetu polskiego."""
    key = []
    for ch in unicodedata.normalize("NFC", (text or "").lower()):
        base = ch if ch in _PL_LETTERS else unicodedata.normalize("NFD", ch)[0]
        base = _SORT_FOLDS.get(base, base) if base not in _PL_LETTERS else base
        for piece in base:
            if piece in _PL_ORDER:
                key.append(_PL_ORDER[piece])
            else:
                key.append((_DIGITS_NEXT if piece.isdigit() else _SYMBOLS_FIRST) + ord(piece))
    return tuple(key)
