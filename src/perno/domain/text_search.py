"""Szukanie w zeskrapowanym tekście: fragmenty wokół fraz, pola z wyrażeń regularnych i gotowe wzorce (NIP, ceny…).

Czysta logika bez plików i sieci. Frazy dopasowujemy po złożeniu liter (`search.fold`: bez wielkości liter i ogonków),
ale pozycje liczymy w tekście oryginalnym, żeby fragment dało się wyciąć w całości."""

from __future__ import annotations

import re
from dataclasses import dataclass
from functools import lru_cache
from typing import Iterable

from perno.domain.search import fold

SNIPPET_WIDTH = 100
MAX_VALUES_PER_FIELD = 3
MAX_VALUE_LEN = 160
_SPACES = re.compile(r"\s+")
_FIELD_NAME = re.compile(r"^[a-z][a-z0-9_]{0,30}$")

# Gotowe wzorce dla pól `--pole nazwa` (wartość to grupa 1). Dopasowanie bez względu na wielkość liter.
PRESETS = {
    "nip": r"\bNIP\b[\s:.\-]*(?:PL)?\s*(\d(?:[\s-]?\d){9})\b",
    "regon": r"\bREGON\b[\s:.\-]*(\d{9}(?:\d{5})?)\b",
    "krs": r"\bKRS\b[\s:.\-]*(\d{10})\b",
    "cena": r"(\d[\d  .]{0,9}(?:,\d{1,2})?\s?(?:zł|PLN|€|EUR|euro|£|GBP|\$|USD))",
}


@dataclass(frozen=True)
class Span:
    """Trafienie w tekście oryginalnym: pozycje [start, end) i fraza (albo wzorzec), która pasowała."""
    start: int
    end: int
    term: str


@dataclass(frozen=True)
class Snippet:
    """Fragment tekstu wokół trafienia (po oczyszczeniu spacji) i fraza, która pasowała."""
    term: str
    text: str


class SpecError(ValueError):
    """Błędna specyfikacja pola lub wzorca (komunikat jest dla użytkownika)."""


@lru_cache(maxsize=4096)
def _fold_char(char: str) -> str:
    return fold(char) if not char.isspace() else " "


def folded_with_index(text: str) -> tuple:
    """(tekst po złożeniu liter, lista pozycji w oryginale dla każdego znaku złożonego). Białe znaki → spacja."""
    out: list = []
    index: list = []
    for position, char in enumerate(text):
        piece = _fold_char(char)
        out.append(piece)
        index.extend([position] * len(piece))
    return "".join(out), index


def phrase_spans(text: str, terms: Iterable[str]) -> list:
    """Wszystkie wystąpienia fraz (już złożonych przez `fold`) jako `Span` w tekście oryginalnym, rosnąco."""
    wanted = [term for term in terms if term]
    if not wanted:
        return []
    folded, index = folded_with_index(text)
    found: list = []
    for term in wanted:
        at = folded.find(term)
        while at != -1:
            last = at + len(term) - 1
            found.append(Span(index[at], index[last] + 1, term))
            at = folded.find(term, at + len(term))
    return sorted(found, key=lambda span: (span.start, span.end))


def regex_spans(text: str, patterns: Iterable) -> list:
    """Wszystkie niepuste dopasowania skompilowanych wzorców jako `Span`, rosnąco."""
    spans = [Span(m.start(), m.end(), pattern.pattern) for pattern in patterns for m in pattern.finditer(text) if m.end() > m.start()]
    return sorted(spans, key=lambda span: (span.start, span.end))


def compile_patterns(patterns: Iterable[str]) -> list:
    """Kompiluje wzorce (bez wielkości liter, `^`/`$` na linię); błąd składni to `SpecError`."""
    compiled = []
    for pattern in patterns:
        try:
            compiled.append(re.compile(pattern, re.IGNORECASE | re.MULTILINE))
        except re.error as exc:
            raise SpecError(f"Błędne wyrażenie regularne {pattern!r}: {exc}") from None
    return compiled


def _squash(text: str) -> str:
    return _SPACES.sub(" ", text).strip()


def snippets(text: str, spans: list, width: int = SNIPPET_WIDTH, limit: int = 3) -> list:
    """Fragmenty tekstu wokół trafień (po `width` znaków z każdej strony); zachodzące na siebie łączy w jeden."""
    merged: list = []
    for span in spans:
        start, end = max(0, span.start - width), min(len(text), span.end + width)
        if merged and start <= merged[-1][1]:
            merged[-1][1] = max(merged[-1][1], end)
        else:
            merged.append([start, end, span.term])
        if len(merged) >= limit:
            break
    return [Snippet(term, ("…" if start > 0 else "") + _squash(text[start:end]) + ("…" if end < len(text) else ""))
            for start, end, term in merged]


def parse_name(spec: str, what: str) -> tuple:
    """„nazwa=wartość” → (nazwa, wartość); nazwa to małe litery, cyfry i _ (ma być nagłówkiem kolumny CSV)."""
    name, sign, value = spec.partition("=")
    name = name.strip()
    if not sign or not value.strip():
        raise SpecError(f"{what}: oczekiwano postaci nazwa=wartość, jest {spec!r}.")
    if not _FIELD_NAME.match(name):
        raise SpecError(f"{what}: nazwa {name!r} musi zaczynać się od małej litery i mieć tylko a-z, 0-9 i _ (do 31 znaków).")
    return name, value.strip()


def parse_phrases(value: str) -> tuple:
    """„a|b|c” → złożone frazy; puste pomijamy, a brak jakiejkolwiek frazy to błąd."""
    phrases = tuple(dict.fromkeys(fold(part) for part in value.split("|") if fold(part)))
    if not phrases:
        raise SpecError("Brak fraz (oddzielaj je znakiem |).")
    return phrases


def extract_values(text: str, pattern: re.Pattern, limit: int = MAX_VALUES_PER_FIELD) -> list:
    """Różne wartości dopasowane przez wzorzec (grupa 1, a bez grup całe dopasowanie), po oczyszczeniu spacji."""
    values: list = []
    for match in pattern.finditer(text):
        value = _squash(match.group(1) if pattern.groups else match.group(0))[:MAX_VALUE_LEN]
        if value and value not in values:
            values.append(value)
            if len(values) >= limit:
                break
    return values


def has_phrase(text: str, phrases: Iterable[str]) -> bool:
    """Czy tekst zawiera którąkolwiek ze złożonych fraz (bez wielkości liter i ogonków)."""
    folded = fold(text)
    return any(phrase in folded for phrase in phrases)
