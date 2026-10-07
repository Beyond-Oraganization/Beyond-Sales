"""Język maila wg kraju siedziby firmy: kod ISO kraju -> język urzędowy/ojczysty, plik szablonu."""

from __future__ import annotations

import os
import re
from typing import Optional

from perno import settings as cfg

LANGUAGE_BY_COUNTRY = {
    "PL": "pl",
    "GB": "en",
    "IE": "en",
    "DE": "de",
    "AT": "de",
    "ES": "es",
}

ICEBREAKER = "icebreaker"  # pierwszy mail do zimnego kontaktu: jeden szablon dla wszystkich segmentów

LANGUAGE_NAMES = {"pl": "polski", "en": "angielski", "de": "niemiecki", "es": "hiszpański"}

# bez „ó": występuje też po hiszpańsku (información), więc nie jest cechą wyłącznie polską
_POLISH_LETTERS = re.compile("[ąćęłńśźżĄĆĘŁŃŚŹŻ]")


def language_for(country: Optional[str]) -> Optional[str]:
    """None, gdy kraj jest pusty albo nie mamy dla niego szablonu (nie zgadujemy — po cichu polski mail do obcego kraju to błąd)."""
    return LANGUAGE_BY_COUNTRY.get((country or "").strip().upper())


def template_path(name: str, language: str) -> str:
    """Ścieżka szablonu o danej nazwie w danym języku (`nazwa.html` dla polskiego, `nazwa.xx.html` dla pozostałych)."""
    file_name = f"{name}.html" if language == "pl" else f"{name}.{language}.html"
    return os.path.join(cfg.BASE_DIR, "templates", file_name)


def looks_polish(*texts: str) -> bool:
    """Wykrywa polskie litery w treści, która ma być w innym języku (zabezpieczenie przed wklejeniem polskiego opisu)."""
    return any(_POLISH_LETTERS.search(t or "") for t in texts)
