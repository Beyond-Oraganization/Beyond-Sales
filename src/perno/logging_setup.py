"""Konfiguracja logowania dla punktów wejścia (serwer, CLI).

Biblioteka tylko tworzy loggery (`logging.getLogger(__name__)`) i niczego nie konfiguruje; format i poziom ustawia
punkt wejścia przez `configure_logging()`. Logi skrapera (`RunLogger`) to osobny, plikowy dziennik przebiegu.
"""

from __future__ import annotations

import logging

LOG_FORMAT = "%(asctime)s %(levelname)s %(name)s: %(message)s"


def configure_logging(level: int = logging.INFO) -> None:
    """Loguje na stderr z czasem i nazwą modułu. Idempotentne: gdy root logger jest już skonfigurowany (np. przez
    pytest albo osadzającą aplikację), niczego nie zmienia."""
    logging.basicConfig(level=level, format=LOG_FORMAT, datefmt="%H:%M:%S")
