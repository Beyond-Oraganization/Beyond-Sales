"""Język maila wg kraju siedziby firmy: kod ISO kraju -> język urzędowy/ojczysty, plik szablonu."""

from __future__ import annotations

import os
import re
from typing import Optional

from lib import config as cfg

LANGUAGE_BY_COUNTRY = {
    "PL": "pl",
    "GB": "en",
    "IE": "en",
    "DE": "de",
    "AT": "de",
    "ES": "es",
}

LANGUAGE_NAMES = {"pl": "polski", "en": "angielski", "de": "niemiecki", "es": "hiszpański"}

# bez „ó": występuje też po hiszpańsku (información), więc nie jest cechą wyłącznie polską
_POLISH_LETTERS = re.compile("[ąćęłńśźżĄĆĘŁŃŚŹŻ]")


def language_for(country: Optional[str]) -> Optional[str]:
    """None, gdy kraj jest pusty albo nie mamy dla niego szablonu (nie zgadujemy — po cichu polski mail do obcego kraju to błąd)."""
    return LANGUAGE_BY_COUNTRY.get((country or "").strip().upper())


def template_path(segment: str, language: str) -> str:
    name = f"{segment}.html" if language == "pl" else f"{segment}.{language}.html"
    return os.path.join(cfg.BASE_DIR, "templates", name)


def looks_polish(*texts: str) -> bool:
    """Wykrywa polskie litery w treści, która ma być w innym języku (zabezpieczenie przed wklejeniem polskiego opisu)."""
    return any(_POLISH_LETTERS.search(t or "") for t in texts)
