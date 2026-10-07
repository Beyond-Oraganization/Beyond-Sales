"""Segment firm (typ, np. „eventy”, „kancelarie”) jako dowolna etykieta: nazwa w postaci bezpiecznego identyfikatora.

Segment trafia do identyfikatorów wierszy (`kancelarie-n001`), do filtrów panelu i ustawień Automatu, więc ma stały,
prosty kształt. Znane segmenty z `statuses.SEGMENT_ORDER` mają własną kolejność, pozostałe układają się za nimi."""

from __future__ import annotations

import re
import unicodedata

SEGMENT_RE = re.compile(r"[a-z][a-z0-9-]{1,30}")
_NON_SLUG = re.compile(r"[^a-z0-9]+")
_POLISH_MAP = str.maketrans({"ł": "l", "Ł": "L"})  # ł nie rozkłada się przy normalizacji NFKD


class SegmentError(ValueError):
    """Nazwa segmentu nie da się zapisać jako identyfikator."""


def is_valid(segment: object) -> bool:
    """Czy `segment` jest gotowym identyfikatorem (małe litery, cyfry i myślniki, 2-31 znaków, zaczyna się od litery)."""
    return isinstance(segment, str) and SEGMENT_RE.fullmatch(segment) is not None and not segment.endswith("-")


def slugify(text: str) -> str:
    """„Szkoły jazdy” → „szkoly-jazdy”: bez polskich znaków, spacje i znaki specjalne jako pojedynczy myślnik."""
    plain = unicodedata.normalize("NFKD", (text or "").translate(_POLISH_MAP)).encode("ascii", "ignore").decode("ascii")
    return _NON_SLUG.sub("-", plain.lower()).strip("-")


def normalize(text: str) -> str:
    """Identyfikator segmentu z podanej nazwy (wolno wpisać „Szkoły jazdy”) albo `SegmentError`."""
    segment = slugify(text)
    if not is_valid(segment):
        raise SegmentError(f"Nazwa segmentu {text!r} nie daje poprawnego identyfikatora: 2-31 znaków, litery, cyfry "
                           "i myślniki, pierwsza litera (np. „kancelarie”, „szkoly-jazdy”).")
    return segment
