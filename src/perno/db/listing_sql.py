"""SQL widoku listy w panelu: wyszukiwarka, kolejność sortowania i pomocnicze wyrażenia warunków.

Wyszukiwanie liczy się w bazie, bez dodatkowej kolumny: `LIKE` po złożonym tekście kilku pól przeszukuje 50 tys. wierszy
w ok. 130 ms (zmierzone na MySQL 9.6), a kolumna pochodna wymagałaby pilnowania w każdej ścieżce zapisu. Złożenie liter
(`domain.search.fold`) każda baza robi po swojemu: sqlite wołanym z Pythona `perno_fold` (dokładnie ta sama funkcja co w
testach wektorów), a MySQL kolacją `utf8mb4_0900_ai_ci` (akcenty i wielkość liter znikają same). Kolacja traktuje jako
osobne litery ß, æ, œ i þ, więc dla fraz zawierających `ss`, `ae`, `oe` albo `th` tekst jest dodatkowo przepuszczany przez
`REPLACE`; robimy to tylko wtedy, bo każdy `REPLACE` kosztuje na 50 tys. wierszy kilkadziesiąt milisekund. Jedyna litera, której
MySQL nie złoży (turecka ı), nie występuje w danych firm i nie jest obsługiwana.
"""

from __future__ import annotations

import sqlalchemy as sa
from sqlalchemy.ext.compiler import compiles
from sqlalchemy.sql.expression import FunctionElement

from perno.db.schema import maile
from perno.db.sqlite_functions import FOLD_FUNCTION, PL_COLLATION
from perno.domain import statuses as st
from perno.domain.countries import COUNTRY_NAMES
from perno.domain.listing import SORT_KEYS
from perno.domain.search import SearchTerms, pl_sort_key

MYSQL_PL_COLLATION = "utf8mb4_pl_0900_ai_ci"
# Litery, które kolacja MySQL uznaje za osobne: (złożenie w zapytaniu, litera w tekście). REPLACE jest potrzebny tylko wtedy,
# gdy szukana fraza zawiera dane złożenie.
_MYSQL_EXPANSIONS = (("ss", "ß"), ("ae", "æ"), ("oe", "œ"), ("th", "þ"))


class Folded(FunctionElement):
    """Tekst złożony tak jak `domain.search.fold`: małe litery i bez znaków diakrytycznych. `letters` to litery, które
    MySQL ma osobno i trzeba je złożyć jawnie (pusta krotka = wystarczy sama kolacja)."""
    type = sa.String()
    inherit_cache = False
    name = "perno_fold"

    def __init__(self, text, letters: tuple = ()):
        super().__init__(text)
        self.letters = letters


@compiles(Folded)
def _folded_default(element, compiler, **kw):
    return f"lower({compiler.process(element.clauses, **kw)})"


@compiles(Folded, "sqlite")
def _folded_sqlite(element, compiler, **kw):
    return f"{FOLD_FUNCTION}({compiler.process(element.clauses, **kw)})"


@compiles(Folded, "mysql")
def _folded_mysql(element, compiler, **kw):
    sql = compiler.process(element.clauses, **kw)
    if not element.letters:
        return sql  # kolacja utf8mb4_0900_ai_ci sama pomija wielkość liter i akcenty
    sql = f"lower({sql})"  # REPLACE rozróżnia wielkość liter
    for letter, replacement in element.letters:
        sql = f"replace({sql}, '{letter}', '{replacement}')"
    return sql


def _letters_for(term: str) -> tuple:
    """Litery do jawnego złożenia w MySQL dla danej frazy: tylko te, których fraza mogłaby szukać."""
    return tuple((letter, sequence) for sequence, letter in _MYSQL_EXPANSIONS if sequence in term)


def _joined(*parts):
    """Teksty złączone spacjami; brakująca wartość (NULL) liczy się jak pusty tekst, tak jak `join(' ')` w przeglądarce."""
    expression = sa.func.coalesce(parts[0], "")
    for part in parts[1:]:
        expression = expression + " " + sa.func.coalesce(part, "")
    return expression


def _country_name():
    """Polska nazwa kraju z kodu (puste dla nieznanego), żeby „niemcy” znajdowało firmy z kodem DE."""
    return sa.case(COUNTRY_NAMES, value=maile.c.country, else_="")


def haystack(field: str):
    """Tekst, w którym szuka wyszukiwarka dla danego pola (`SEARCH_FIELDS`); kolejność i pola jak w dawnym widoku."""
    company = _joined(maile.c.company, maile.c.contact_person)
    place = _joined(maile.c.country, _country_name(), maile.c.city, maile.c.address)
    if field == "company":
        return company
    if field == "email":
        return _joined(maile.c.email)
    if field == "website":
        return _joined(maile.c.website)
    if field == "place":
        return place
    if field == "description":
        return _joined(maile.c.description)
    return _joined(maile.c.id, company, maile.c.email, maile.c.website, place, maile.c.description)


def search_clauses(terms: SearchTerms) -> list:
    """Warunki WHERE: każde słowo z `include` musi wystąpić w tekście pola, żadne z `exclude` nie może."""
    if not terms:
        return []
    text = haystack(terms.field)

    def contains(term: str):
        return Folded(text, _letters_for(term)).contains(term, autoescape=True)

    return [contains(term) for term in terms.include] + [sa.not_(contains(term)) for term in terms.exclude]


# ---- kolejność ------------------------------------------------------------------------------------------------------

def _rank(column, order, fallback: int = 999):
    """Numer pozycji wartości w podanej kolejności (nieznane na końcu)."""
    return sa.case({value: index for index, value in enumerate(order)}, value=column, else_=fallback)


def _empty_last(column):
    return sa.case((sa.or_(column.is_(None), column == ""), 1), else_=0)


def _text_key(column, dialect: str):
    """Kolumna tekstowa porównywana po polsku (kolacja bazy); bez tej kolacji zostaje domyślna."""
    if dialect == "mysql":
        return column.collate(MYSQL_PL_COLLATION)
    if dialect == "sqlite":
        return column.collate(PL_COLLATION)
    return column


def _country_order():
    ranked = sorted(COUNTRY_NAMES, key=lambda code: pl_sort_key(COUNTRY_NAMES[code]))
    return _rank(maile.c.country, ranked, fallback=len(ranked))


def sort_clauses(sort: str, dialect: str, tiebreak: list) -> list:
    """ORDER BY dla klucza sortowania panelu: `klucz` rosnąco, `-klucz` malejąco, puste wartości zawsze na końcu;
    remisy rozstrzyga domyślna kolejność panelu (`tiebreak`). Nieznany klucz i „default” dają samą kolejność domyślną."""
    key = sort.lstrip("-")
    if key not in SORT_KEYS:
        return list(tiebreak)

    def direct(expression):
        return expression.desc() if sort.startswith("-") else expression.asc()

    if key in ("company", "city", "email"):
        column = getattr(maile.c, key)
        return [_empty_last(column), direct(_text_key(column, dialect)), *tiebreak]
    if key == "segment":
        return [direct(_rank(maile.c.segment, st.SEGMENT_ORDER)), direct(_text_key(maile.c.segment, dialect)), *tiebreak]
    if key == "status":
        return [direct(_rank(maile.c.status, tuple(st.STATUS_META))), *tiebreak]
    if key == "country":
        return [_empty_last(maile.c.country), direct(_country_order()), *tiebreak]
    column = getattr(maile.c, key)
    return [sa.case((column.is_(None), 1), else_=0), direct(column), *tiebreak]
